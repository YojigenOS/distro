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
   `PARTLABEL=`。所以 cmdline 用 **`root=PARTLABEL=yojigenos-root`**——顺带可读、
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
等 packages 那边的 `grub` 配方建出来，就把 `--directory` 指向我们自己包里的模块，
交付物里不再带别人的二进制。接口留好了，改的是一行。
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


# ── 容器里跑的脚本 ────────────────────────────────────────────────────────
#
# 需要 --privileged：losetup + mount。所有偏移都在容器里处理——macOS 上
# 根本没有 loop 设备，这些活只能在 Linux 里干。
CONTAINER_SCRIPT = """
set -eu
apk add --no-cache util-linux parted e2fsprogs e2fsprogs-extra dosfstools zstd >/dev/null 2>&1
apk add --no-cache grub >/dev/null 2>&1 || true
case "$GRUB_TARGETS" in *i386-pc*) apk add --no-cache grub-bios >/dev/null 2>&1 || true ;; esac
case "$GRUB_TARGETS" in *efi*)     apk add --no-cache grub-efi  >/dev/null 2>&1 || true ;; esac

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
parted -s "$IMG" mkpart yojigenos-root ext4 ${ROOT_START}MiB ${ROOT_END}MiB

DISK=$(losetup -f --show "$IMG")
ESP=$(losetup -f --show --offset $((ESP_START*1048576))  --sizelimit $(( (ESP_END-ESP_START)*1048576 ))   "$IMG")
ROOT=$(losetup -f --show --offset $((ROOT_START*1048576)) --sizelimit $(( (ROOT_END-ROOT_START)*1048576 )) "$IMG")
echo "  盘 $DISK · ESP $ESP · root $ROOT"

mkfs.vfat -F32 -n ESP "$ESP" >/dev/null
# metadata_csum_seed 会让老 GRUB 掉 rescue；orphan_file 是另一个著名的
# GRUB 兼容性坑。关掉两个都是零成本保险。
mkfs.ext4 -q -F -b 4096 -L yojigenos-root -O ^metadata_csum_seed,^orphan_file "$ROOT"

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

menuentry "YojigenOS" {
    search --fs-uuid --set=root $ROOT_UUID
    linux /boot/$KERNEL_NAME console=tty0 console=$CONSOLE,115200 root=PARTLABEL=yojigenos-root rootfstype=ext4 rw init=/init
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

    log("风味", f"{name}  [{v['_kind']}]")
    log("架构", arch)
    log("根", f"{root_dir.relative_to(ROOT)}/")
    log("内核", f"boot/{kname}  ({os.path.getsize(root_dir / 'boot' / kname) / 1048576:.1f} MB)")
    log("镜像", f"{img}  （{size}）")
    print()

    work.mkdir(parents=True, exist_ok=True)
    img.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "run", "--rm", "--privileged",
        "-v", f"{root_dir}:/rootfs:ro",
        "-v", f"{img.parent}:/out",
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
