"""把装出来的根变成一张能 DD 的磁盘镜像。

这是决策记录 0013 那条路的落点：**唯一一种「对方不需要认识我们」的交付形式**。
reinstall 的 dd 模式把这张镜像原样写进硬盘，一个字节都不改——所以镜像得自带一切：
分区表、引导器、内核、`/init`。

## 分区表照谁的抄

腾讯云那套模板（实测过两次，连文件系统 UUID 都一样）：

    vda1  512M  EFI System    ← SeaBIOS 下用不到，但模板里有
    vda2    1M  BIOS boot     ← grub-pc 的 core.img 住这儿
    vda3  39.5G Linux root

所以我们的镜像也做成**混合 GPT**：两个都在。TX 只能 BIOS 引导（2026-10-12 实测：
把 ESP 填好、MBR 清成 0，SeaBIOS 直接卡死在 `Booting from Hard Disk...`），但混合
布局让同一张镜像换台机器也能起。

## 2026-10-12：管线已跑通（aarch64 + UEFI，QEMU 里真启动过）

     edk2 UEFI → ESP 里的 GRUB → 我们的内核 → ext4 根 → /init → 串口问候

下面四个坑全是**建得出、起不来**那一类，只有在 QEMU 里真启动才暴露。写在最前面，
因为每一个都是"看起来成功了"：

**① GRUB 看不见分区。** `grub-install` 给 EFI 目标嵌的模块集里**没有 `part_gpt`**
   （实测嵌的只有 ext2/linux/normal/search_fs_uuid）→ GRUB 读不到任何分区 →
   按 UUID 找不到根 → 掉 rescue。改用 **`grub-mkstandalone`**：模块和 grub.cfg
   一起塞进一个 EFI 文件，不依赖从 `$prefix` 加载任何东西。

**② 裸内核的 `root=` 不认文件系统 UUID。** 没有 initramfs 就没有用户态去解析
   `UUID=…`；内核源码 `block/early-lookup.c` 的注释原话是
   *"This does not search by filesystem UUIDs."*。它只认 `/dev/…`、`PARTUUID=`、
   `PARTLABEL=`。所以 cmdline 用 **`root=PARTLABEL=yojigen-os-root`**——顺带可读、
   可复现（不用读随机 UUID）。内核 panic 时打印的那串其实就是 PARTUUID，是提示。

**③ 文件系统比分区大 1 MiB。** `parted` 的 `100%` 会**忽略末尾不足 1 MiB 的部分**，
   而 loop 设备的 `--sizelimit` 按"总大小 − 起始"算就会多出那 1 MiB →
   内核报 `EXT4-fs (vda3): bad geometry: block count N exceeds size of device` →
   挂不上根。**两边必须用同一组 MiB 数字**，并且把它做成静态自检。

**④ `console=` 的最后一个才是 `/dev/console`。** `console=ttyAMA0 console=tty0`
   会让 PID 1 的输出跑到虚拟终端上——串口里一个字都没有，看起来就像 /init 没跑。
   串口要写在**最后**。

## 容器环境本身的三个坑（也踩过）

    losetup 要用 util-linux 的（Alpine 自带的 BusyBox 版不认 --show）
    losetup -P 在 Docker Desktop 的 VM 里不生成分区设备（loop 的 max_part=0）
      → 每个分区一个带 --offset/--sizelimit 的 loop 设备，不依赖内核扫分区
    Docker Desktop 挂【单个文件】会把宿主那个路径建成目录（挂目录才对）

## 引导器从哪来（这一版是借的）

`grub-mkstandalone` / `grub-install` 跑在借来的 Alpine 容器里。**这是暂时的**：
把 `--directory` 指向我们自己包里的模块，交付物里就不再带别人的二进制。
接口留好了，改的是一行。

★ 而 2026-10-15 核了一遍：**配方早就建出来了**（`packages/recipes/tool/grub`），
而这一句里"等 grub 配方建出来"的前提已经不成立——**没做的是接线那一步**。
所以这不是"等上游"，是一条开着的活。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import flavor
from .common import (
    DEFAULT_REPO, ROOT, TOOL_IMAGE, VAR_DIR, die, log, resolve_arch, run_streaming,
)

#: 控制台设备。★ 写在 cmdline 的【最后】，它才会成为 /dev/console（坑 ④）。
CONSOLE = {"x86_64": "ttyS0", "aarch64": "ttyAMA0"}

#: grub target。x86_64 两个都装（BIOS 是 TX 唯一能用的那条），aarch64 只有 efi。
GRUB_TARGETS = {"x86_64": "i386-pc x86_64-efi", "aarch64": "arm64-efi"}

#: UEFI 可移动介质路径下的文件名（DD 不写 NVRAM 启动项，只能靠这个约定）。
EFI_NAME = {"x86_64": "BOOTX64.EFI", "aarch64": "BOOTAA64.EFI"}

DEFAULT_SIZE = "1G"

#: 分区表本身要占掉的空间：ESP 512 + bios_grub 1 + 两端各 1 MiB 对齐。
#: 根分区从 514 MiB 开始，到（镜像大小 − 1 MiB）结束。
LAYOUT_MIB = 515


def _mib(size: str) -> int:
    """把 1G / 1024M / 768 都读成 MiB。读不出来就报错，别猜。"""
    s = size.strip().upper()
    try:
        if s.endswith("G"):
            return int(s[:-1]) * 1024
        if s.endswith("M"):
            return int(s[:-1])
        return int(s)
    except ValueError:
        die(f"看不懂的大小: {size}（写 1G / 1024M / 768 都行）")


def _tree_mib(path) -> float:
    """目录里所有内容的实际大小（MB）。不调 du：那读的是块数，在 APFS 和
    ext4 上不是一回事，而这里要的是"装进去要占多少"。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            try:
                total += os.lstat(p).st_size
            except OSError:
                pass
    return total / 1048576


# ── 容器里跑的脚本 ────────────────────────────────────────────────────────
#
# 需要 --privileged：losetup + mount。所有偏移都在容器里处理——macOS 上
# 根本没有 loop 设备，这些活只能在 Linux 里干。
CONTAINER_SCRIPT = """
set -eu
apk add --no-cache util-linux parted e2fsprogs e2fsprogs-extra dosfstools zstd </dev/null >/dev/null 2>&1
apk add --no-cache grub </dev/null >/dev/null 2>&1 || true
case "$GRUB_TARGETS" in *i386-pc*) apk add --no-cache grub-bios </dev/null >/dev/null 2>&1 || true ;; esac
case "$GRUB_TARGETS" in *efi*)     apk add --no-cache grub-efi </dev/null  >/dev/null 2>&1 || true ;; esac

# ★ 兜底：脚本中途死掉也要把所有 loop 设备放掉。实测过——失败的运行会把
#   它们留在 Docker VM 里，攒够几个之后 losetup 就再也拿不到设备了
#   （报 "device node /dev/loopN is lost"）。
trap 'losetup -D >/dev/null 2>&1 || true' EXIT

# 尺寸统一用 MiB 算：分区表和 loop 设备必须用【同一组数字】（坑 ③）。
case "$IMG_SIZE" in
  *G|*g) IMG_MIB=$(( ${IMG_SIZE%[Gg]} * 1024 )) ;;
  *M|*m) IMG_MIB=${IMG_SIZE%[Mm]} ;;
  *)     IMG_MIB=$IMG_SIZE ;;
esac
ESP_START=1;    ESP_END=513
BIOS_START=513; BIOS_END=514
ROOT_START=514; ROOT_END=$((IMG_MIB - 1))     # 末尾留 1 MiB 给 GPT 备份表

IMG=/out/$IMG_NAME
rm -f "$IMG"
truncate -s $((IMG_MIB * 1048576)) "$IMG"

parted -s "$IMG" mklabel gpt
parted -s "$IMG" mkpart esp fat32 ${ESP_START}MiB ${ESP_END}MiB
parted -s "$IMG" set 1 esp on
parted -s "$IMG" mkpart bios ${BIOS_START}MiB ${BIOS_END}MiB
parted -s "$IMG" set 2 bios_grub on
parted -s "$IMG" mkpart yojigen-os-root ext4 ${ROOT_START}MiB ${ROOT_END}MiB

DISK=$(losetup -f --show "$IMG")
ESP=$(losetup -f --show --offset $((ESP_START*1048576))  --sizelimit $(( (ESP_END-ESP_START)*1048576 ))   "$IMG")
ROOT=$(losetup -f --show --offset $((ROOT_START*1048576)) --sizelimit $(( (ROOT_END-ROOT_START)*1048576 )) "$IMG")
echo "  盘 $DISK · ESP $ESP · root $ROOT"

mkfs.vfat -F32 -n ESP "$ESP" >/dev/null
# metadata_csum_seed 会让老 GRUB 掉 rescue；orphan_file 是另一个著名的
# GRUB 兼容性坑。关掉两个都是零成本保险。
mkfs.ext4 -q -F -b 4096 -L yojigen-os-root -O ^metadata_csum_seed,^orphan_file "$ROOT"

mkdir -p /os /esp
mount "$ROOT" /os
cp -a /rootfs/. /os/
chown -R 0:0 /os
ROOT_UUID=$(blkid -p -s UUID -o value "$ROOT")
echo "  root fs UUID: $ROOT_UUID"
# fstab 用文件系统 UUID（那是【用户态】的 mount 解析的，和内核的 root= 无关）。
printf 'UUID=%s  /  ext4  rw,relatime  0 1\\n' "$ROOT_UUID" > /os/etc/fstab

mount "$ESP" /esp
mkdir -p /esp/EFI/BOOT /os/boot/grub

# ★ 串口写在最后（坑 ④）；root= 用 PARTLABEL（坑 ②）。
cat > /os/boot/grub/grub.cfg <<EOF
set timeout=1
set default=0
serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial

# ★★ 读 custom.cfg —— 2026-10-15 加的，而它是【GRUB 通用的那个扩展点】。
#
#   少了这三行的后果是实打实的：一台机器【装不进去】。
#
#   实测（那天晚上在真机 TX 上跑 reinstall.sh）：它把一次性入口写进了
#   /boot/grub/custom.cfg、把内核和 initrd 放在根上、把 grubenv 的
#   next_entry 设成 "reinstall (dd)" —— **三样都对**，而 GRUB 什么都没做，
#   因为**它从来没读过那个文件**。于是重启之后又回到原来的系统，
#   而日志里看不出任何异常。
#
#   ★ 而这条只有【真装一次】才会走到：pantsu vm 默认走的是 --mode direct
#     （内核 + initramfs 直启），那条路根本不经过 GRUB ✓。所以 CI 全绿，
#     而盘上的引导链是断的。
#
#   ★★ 而这十几行注释本身踩过一次坑（2026-10-15 晚，建盘当场失败）。
#     这个 heredoc 是【不带引号】的，所以它里面的【注释】也会被展开 ——
#     而展开发生在【这里】，不是 GRUB 那边。当时报的是：
#
#         sh: pantsu: not found
#         sh: config_directory: parameter not set
#
#     第一句来自注释里的「pantsu vm」被反引号括着（在本脚本里那是命令替换）；
#     第二句来自注释里写了一个带花括号的变量名。而这个脚本是 set -eu 的，
#     所以第二句直接把建盘杀掉。
#
#     **规律：heredoc 外面的注释是注释，里面的注释是【代码】。**
#     要在这十几行里写美元号、反引号、反斜杠，都得先转义。
#
#   config_directory 是 GRUB 给"主配置所在目录"设的那个变量；而
#   grub-mkstandalone 那条路（我们用的就是它）可能没设它，所以补一个
#   prefix 的退路。这两行合起来是各发行版 /etc/grub.d/40_custom 里的标准写法。
if [ -f \\${config_directory}/custom.cfg ]; then
    source \\${config_directory}/custom.cfg
elif [ -z "\\${config_directory}" -a -f \\$prefix/custom.cfg ]; then
    source \\$prefix/custom.cfg
fi

menuentry "Yojigen OS" {
    search --fs-uuid --set=root $ROOT_UUID
    linux /boot/$KERNEL_NAME console=tty0 console=$CONSOLE,115200 root=PARTLABEL=yojigen-os-root rootfstype=ext4 rw init=/init
}
EOF
cp /os/boot/grub/grub.cfg /tmp/efi.cfg

for t in $GRUB_TARGETS; do
    case "$t" in
    i386-pc)
        # BIOS：boot.img 进 MBR，core.img 进 bios_grub 分区。
        # ★ 这条路还没有在真启动里验过（本地没有 x86_64 的内核），
        #   等 packages 发出 x86_64 的 linux 包再验。
        grub-install --target=i386-pc --boot-directory=/os/boot --recheck \\
          --modules="part_gpt part_msdos ext2 normal linux search search_fs_uuid" "$DISK" >/dev/null 2>&1 \\
          && echo "  ✓ i386-pc（BIOS）" || echo "  ✗ i386-pc 失败" ;;
    *)
        # UEFI：mkstandalone，不用 grub-install（坑 ①）。
        grub-mkstandalone --format="$t" \\
          --modules="part_gpt part_msdos fat ext2 normal linux search search_fs_uuid echo test serial" \\
          --output="/esp/EFI/BOOT/$EFI_NAME" "boot/grub/grub.cfg=/tmp/efi.cfg" 2>/tmp/efi.err \\
          && echo "  ✓ $t（mkstandalone → $EFI_NAME）" \\
          || { echo "  ✗ $t 失败:"; head -5 /tmp/efi.err | sed 's/^/      /'; } ;;
    esac
done

# ── 静态自检：装得出来挂不上，是最贵的一类错误 ────────────────────────
echo "── 自检 ──"
FS_BYTES=$(dumpe2fs -h "$ROOT" 2>/dev/null | awk '
    /^Block count:/ { c = $3 }
    /^Block size:/  { s = $3 }
    END { if (c && s) print c * s; else print 0 }')
PART_BYTES=$(( (ROOT_END - ROOT_START) * 1048576 ))
echo "  根分区 $PART_BYTES 字节 · 文件系统 $FS_BYTES 字节"
if [ "$FS_BYTES" -gt 0 ] && [ "$FS_BYTES" -le "$PART_BYTES" ]; then
    echo "  ✓ 文件系统没有超出分区"
else
    echo "  ✗ 文件系统大小不对（$FS_BYTES）——内核会报 bad geometry，挂不上根"
    exit 1
fi

sync
umount /esp /os
losetup -d "$ESP" "$ROOT" "$DISK"

parted -s "$IMG" print | sed 's/^/   /'
zstd -q -T0 -19 -f "$IMG" -o "$IMG.zst"
echo "  压缩后 $(stat -c%s "$IMG.zst") / 未压缩 $(stat -c%s "$IMG")"

# ★ 属主还给宿主。
#
#   这个容器是 --privileged 以 root 跑的，写出来的 $IMG 和 $IMG.zst 都归 root。
#   而下一步 QEMU 是【以宿主用户】跑的，于是打不开自己刚建出来的盘：
#
#       qemu-system-x86_64: -drive file=…/minimal-x86_64.img,…:
#           Could not open '…': Permission denied
#
#   实测撞上了。**这是同一类问题的第三次**：
#     · build 那边还原 payload 之后 skirt 删不掉自己的产物
#     · vm 那边解压出来的 .img QEMU 打不开（那个分支还没被走到）
#     · 这里建盘
#
#   ★ 规律：**在容器里以 root 写、拿到容器外用 → 必须还属主。**
#     不能指望"文件是 644 所以能读"——宿主用户对 root 建的文件确实可读，问题
#     出在 docker 挂载的目录上（容器内 root 建的文件，宿主 uid 常常连目录都进不去）。
if [ -n "$HOST_UID" ]; then
    chown "$HOST_UID:$HOST_GID" "$IMG" "$IMG.zst" 2>/dev/null || true
fi
"""


def build(name: str, arch_requested: str | None = None, repo: str = DEFAULT_REPO,
          out: str | None = None, size: str = DEFAULT_SIZE,
          kernel: str | None = None) -> Path:
    v = flavor.load(name)
    flavor.require_producible(v)
    if v["_kind"] != "disk":
        die(
            f"风味 {name} 的 kind 是 {v['_kind']!r}，产不出磁盘镜像。\n"
            f"       磁盘镜像要的是 kind = \"disk\""
        )

    arch = resolve_arch(arch_requested)
    work = VAR_DIR / name
    root_dir = work / f"rootfs-{arch}"
    img_name = f"{name}-{arch}.img"
    img = Path(out) if out else (work / img_name)

    if not (root_dir / "init").is_file():
        die(
            f"{root_dir.relative_to(ROOT)}/ 里没有 /init。\n"
            f"       先装根：pantsu rootfs {name} --arch {arch} --repo {repo}"
        )

    kname = kernel or "vmlinuz"
    if not (root_dir / "boot" / kname).is_file():
        die(
            f"{root_dir.relative_to(ROOT)}/boot/{kname} 不存在。\n"
            f"       可引导风味要有内核——把 linux 列进 {name}.toml，或用 --kernel 指一个"
        )

    # ── 装得下吗（在起容器之前算，报的是缘由不是规矩）────────────────────
    #
    # ★ 为什么要有这一段：--size 太小会一直走到容器里，然后失败在
    #   "文件系统比分区大 / parted 拒绝 / 根装不进去"这些看不懂的地方。
    #   而这件事宿主上就能算清楚：根有多大是知道的。
    mib = _mib(size)
    if mib <= LAYOUT_MIB:
        die(
            f"--size {size} 太小：分区表本身就要 {LAYOUT_MIB} MiB\n"
            f"       （ESP 512 + bios_grub 1 + 两端各 1 MiB 对齐）"
        )
    room = mib - LAYOUT_MIB
    need = _tree_mib(root_dir) * 1.15 + 32      # 15% 给 ext4 的块开销，32 MB 留白
    if room < need:
        want = int((need + LAYOUT_MIB) / 256 + 1) * 256
        die(
            f"镜像装不下根：--size {size} 只给根留 {room:.0f} MB，"
            f"而这份根要 {need:.0f} MB\n"
            f"       （{root_dir.relative_to(ROOT)}/ 实际 "
            f"{_tree_mib(root_dir):.0f} MB，加 15% 块开销和 32 MB 留白）\n"
            f"       起步用 --size {want}M"
        )

    log("风味", f"{name}  [{v['_kind']}]")
    log("架构", arch)
    log("根", f"{root_dir.relative_to(ROOT)}/")
    log("内核", f"boot/{kname}  ({os.path.getsize(root_dir / 'boot' / kname) / 1048576:.1f} MB)")
    log("空间", f"根要 {need:.0f} MB / 分区给 {room:.0f} MB（{size}）")
    log("镜像", f"{img}")
    print()

    work.mkdir(parents=True, exist_ok=True)
    img.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "run", "--rm", "--privileged",
        # ★ 必须钉在【目标架构】上，不能让它跟着宿主走。
        #
        #   这一步要跑 grub-install / grub-mkstandalone，而 GRUB 的模块是
        #   架构相关的：arm64 的 Alpine 镜像里根本没有 x86_64-efi 那套模块。
        #   在 Apple Silicon 上建 x86_64 的盘，不加这行会以"找不到模块"失败。
        #
        #   CI 里两者一致（ubuntu-26.04 上建 x86_64），所以这行在那里是空操作——
        #   它服务的是【本地预演】：Mac 上先把镜像启一遍，再拿去 DD。
        "--platform", f"linux/{arch}",
        "-v", f"{root_dir}:/rootfs:ro",
        "-v", f"{img.parent}:/out",
        # 建完把属主还回来（见 CONTAINER_SCRIPT 末尾那段注释）。
        # 用宿主自己的 uid/gid，不写死——CI 上是 1001，开发者本机可能是别的。
        "-e", f"HOST_UID={os.getuid()}",
        "-e", f"HOST_GID={os.getgid()}",
        "-e", f"IMG_NAME={img.name}",
        "-e", f"IMG_SIZE={size}",
        "-e", f"KERNEL_NAME={kname}",
        "-e", f"CONSOLE={CONSOLE[arch]}",
        "-e", f"GRUB_TARGETS={GRUB_TARGETS[arch]}",
        "-e", f"EFI_NAME={EFI_NAME[arch]}",
        TOOL_IMAGE, "sh", "-c", CONTAINER_SCRIPT,
    ]
    rc, _ = run_streaming(cmd)
    if rc != 0:
        die(f"建盘失败（退出码 {rc}）")

    zst = img.with_suffix(img.suffix + ".zst")
    if not zst.is_file():
        die(f"建盘跑完了，但没找到 {zst}")

    print()
    log("完成", f"{zst}  ({zst.stat().st_size / 1048576:.1f} MB)")
    print()
    print("  在本地验它（QEMU + UEFI 固件；TX 那台是 BIOS，cmdline 里的 root= 两种都能用）：")
    if arch == "x86_64":
        print(f"      qemu-system-x86_64 -machine q35 -cpu host -accel hvf -m 1G -no-reboot \\")
        print(f"          -drive file=<解压后的 .img>,if=virtio,format=raw -nographic")
    else:
        print(f"      qemu-system-aarch64 -machine virt -cpu host -accel hvf -m 1G -no-reboot \\")
        print(f"          -bios /opt/homebrew/share/qemu/edk2-aarch64-code.fd \\")
        print(f"          -drive file=<解压后的 .img>,if=virtio,format=raw \\")
        print(f"          -display none -serial file:/tmp/boot.log")
    return zst
