"""在 QEMU 里把系统跑起来——两种方式。

**① 启动磁盘镜像（默认，也是交付物的验收方式）**

     edk2 UEFI / SeaBIOS → ESP 或 MBR 里的 GRUB → 内核 → 根 → /init

x86_64 用 QEMU 默认的 **SeaBIOS**，和 TX 那台完全同构（BIOS + virtio-blk + ttyS0）——
所以本地这一跑，能提前回答"DD 上去能不能起"。

**② 内核 + initramfs 直启（`--direct`）**

    -kernel vmlinuz -initrd initramfs.cpio.gz

把装好的根打成 cpio 塞给内核，不需要分区表、不需要文件系统驱动、不需要引导器。
这是"改了用户态马上看效果"的最短回路——不建盘、不烧镜像。

## 为什么 initramfs 在一个容器里打

原来那套（scripts/run-vm.sh）要向 packages 要 cpio 的产物，那等于读
`var/output/`——这个仓库的规矩是不许。Alpine 容器里 `apk add cpio` 就有，
和建盘那边一样：**借工具，不借包。**

## 两个曾经咬过人的地方（都在下面留着防线）

    find | cpio | gzip 里 cpio 没找到也照样"成功"（退出码取自 gzip）
      → 开 pipefail，并且打完【量一下大小】，太小就报错
    QEMU 的 hvf 只能加速【同架构】的客户机
      → 在 Apple Silicon 上跑 x86_64 必须退回 TCG（慢，但能跑）
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import flavor
from .common import (
    ARCH_ALIASES, ROOT, TOOL_IMAGE, VAR_DIR, die, log, resolve_arch, run_streaming,
)

#: 客户机控制台。顺序很要紧：写在【最后】的那个才是 /dev/console。
CONSOLE = {"x86_64": "ttyS0", "aarch64": "ttyAMA0"}

#: aarch64 的 UEFI 固件（Homebrew 装 qemu 时一起装的 edk2）。
EDK2_AARCH64 = [
    "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
    "/usr/local/share/qemu/edk2-aarch64-code.fd",
    "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
    "/usr/share/AAVMF/AAVMF_CODE.fd",
]

INITRAMFS_MIN_BYTES = 100_000


def _host_arch() -> str:
    return ARCH_ALIASES.get(platform.machine(), platform.machine())


def _accel_name(guest_arch: str) -> str | None:
    """宿主上能用的加速器。没有就返回 None（退回 TCG，慢但能跑）。

    ★ 两件事都要在【宿主】上判断，第二件是踩过的：

    1. **宿主是什么平台**：macOS 用 hvf，Linux 用 kvm。写死 hvf 会让 CI
       （Linux）那一步直接失败——而那是镜像唯一的自动断言。

    2. **客户机架构必须和宿主一致**。Apple Silicon 的 HVF 只能虚拟化 arm64
       客户机：在 arm64 Mac 上启 x86_64 镜像，加 `-accel hvf` 会直接起不来。
       反过来，x86_64 的 Linux 上启 arm64 客户机也不能用 kvm。

       不一致时返回 None → 退回 TCG（纯模拟）。慢，但那是"本地先验一遍，
       再拿去 DD"唯一的路——而 DD 会重写整块盘，值得先验。
    """
    if guest_arch != _host_arch():
        return None
    if sys.platform == "darwin":
        return "hvf"
    if Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "kvm"
    return None


def _find_firmware(explicit: str | None, arch: str) -> str | None:
    if arch != "aarch64":
        return None          # x86_64 用 QEMU 自带的 SeaBIOS，什么都不用给
    if explicit:
        if not Path(explicit).is_file():
            die(f"--firmware 指的文件不存在: {explicit}")
        return explicit
    for p in EDK2_AARCH64:
        if Path(p).is_file():
            return p
    die(
        "找不到 aarch64 的 UEFI 固件（edk2）。\n"
        "       Homebrew 装 qemu 时应该一起装了；找不到就用 --firmware 指一个。\n"
        "       试过：" + "\n             ".join(EDK2_AARCH64)
    )


# ── 直接启动：把根打成 initramfs ──────────────────────────────────────────

def _make_initramfs(root_dir: Path, out: Path) -> Path:
    if out.is_file() and not os.environ.get("PANSU_VM_FORCE"):
        log("initramfs", f"复用 {out.relative_to(ROOT)}（想重打就删了它）")
        return out
    log("initramfs", f"从 {root_dir.relative_to(ROOT)}/ 打一份")
    script = (
        "set -eu\n"
        "apk add --no-cache cpio >/dev/null 2>&1\n"
        "cd /root\n"
        "[ -x /root/init ] || { echo '  ✗ 根里没有可执行的 /init' >&2; exit 1; }\n"
        # ★ pipefail：没有它，cpio 没找到也照样产出文件（退出码取自 gzip）。
        "set -o pipefail\n"
        "find . -print0 | cpio --null -o -H newc --quiet | gzip -9 > /out/$OUT_NAME\n"
        # ★ 光靠 pipefail 不够：管道都成功也可能打出个空包，所以量大小。
        "SZ=$(stat -c %s /out/$OUT_NAME)\n"
        f"[ \"$SZ\" -ge {INITRAMFS_MIN_BYTES} ] || {{ echo \"  ✗ initramfs 只有 $SZ 字节，是空的\" >&2; exit 1; }}\n"
        "echo \"  initramfs: $SZ 字节\"\n"
    )
    rc, _ = run_streaming([
        "docker", "run", "--rm",
        "--platform", f"linux/{arch}",
        "-v", f"{root_dir}:/root:ro",
        "-v", f"{out.parent}:/out",
        "-e", f"OUT_NAME={out.name}",
        TOOL_IMAGE, "sh", "-c", script,
    ])
    if rc != 0 or not out.is_file():
        die(f"打 initramfs 失败（期望产物 {out.relative_to(ROOT)}）")
    return out


# ── 造 QEMU 命令 ──────────────────────────────────────────────────────────

def _qemu_cmd(arch: str, kernel: Path | None, initrd: Path | None, img: Path | None,
              mem: str, accel: bool, firmware: str | None,
              serial_file: Path | None) -> list[str]:
    qemu = f"qemu-system-{arch}"
    if shutil.which(qemu) is None:
        die(f"没有 {qemu}（装一下 qemu）")

    cmd = [qemu]
    if arch == "x86_64":
        cmd += ["-machine", "q35"]          # SeaBIOS 是 QEMU 的默认固件 = TX 同构
    else:
        cmd += ["-machine", "virt"]
        fw = _find_firmware(firmware, arch)
        if fw:
            cmd += ["-bios", fw]

    if accel:
        pick = _accel_name(arch)
        # -cpu host 只有真的在加速时才对：纯模拟下它会暴露宿主的 CPU 特性，
        # 客户机可能因此跑不起来。
        cmd += ["-cpu", "host", "-accel", pick] if pick else ["-cpu", "max"]
    else:
        cmd += ["-cpu", "max"]

    cmd += ["-m", mem, "-no-reboot"]

    if img is not None:
        cmd += ["-drive", f"file={img},if=virtio,format=raw"]
    else:
        assert kernel is not None and initrd is not None
        cmd += ["-kernel", str(kernel), "-initrd", str(initrd)]
        # 直启时内核自己就是"根"，用 rdinit 指定第一个用户空间程序。
        cmd += ["-append", f"console=tty0 console={CONSOLE[arch]},115200 rdinit=/init"]

    if serial_file is not None:
        # 给脚本和 CI 用：串口写文件、不要图形，谁也不用盯着看。
        cmd += ["-display", "none", "-serial", f"file:{serial_file}"]
    else:
        cmd += ["-nographic"]
    return cmd


# ── 入口 ──────────────────────────────────────────────────────────────────

def run(name: str, arch_requested: str | None = None, img: str | None = None,
        mem: str = "1G", mode: str = "auto", accel: bool | None = None,
        firmware: str | None = None, serial_file: str | None = None,
        timeout: int | None = None, expect: str | None = None) -> int:
    v = flavor.load(name)
    arch = resolve_arch(arch_requested)
    # ★ --expect 和 --serial-file 是一对：没人把串口写下来，它就只能一路等到
    #   超时，然后看起来像"起不来"。那种失败最费时间，所以在门口就拦住。
    if expect and not serial_file:
        die("--expect 要配合 --serial-file：否则串口没写到任何地方，"
            "它只能一路等到超时（看起来像起不来）")
    work = VAR_DIR / name
    root_dir = work / f"rootfs-{arch}"
    img_path = Path(img) if img else (work / f"{name}-{arch}.img")
    zst = img_path.with_suffix(img_path.suffix + ".zst")

    # 只有 .zst 就先解出来——手边只有发布附件时最自然的用法。
    if not img_path.is_file() and zst.is_file():
        log("解压", f"{zst.name} → {img_path.name}")
        rc, _ = run_streaming([
            "docker", "run", "--rm", "-v", f"{img_path.parent}:/out",
            TOOL_IMAGE, "sh", "-c",
            f"apk add --no-cache zstd >/dev/null 2>&1 && zstd -d -f /out/{zst.name} -o /out/{img_path.name}",
        ])
        if rc != 0 or not img_path.is_file():
            die(f"解压 {zst.name} 失败")

    if mode == "auto":
        mode = "disk" if img_path.is_file() else "direct"
    if mode == "disk" and not img_path.is_file():
        die(
            f"没有 {img_path.relative_to(ROOT)}。\n"
            f"       先建：pantsu img {name} --arch {arch}"
        )

    use_accel = (arch == _host_arch()) if accel is None else accel
    firmware_path = _find_firmware(firmware, arch)

    log("风味", f"{name}  [{v['_kind']}]")
    log("架构", f"{arch}（宿主 {_host_arch()}）")
    log("方式", {"disk": f"磁盘镜像 {img_path.name}", "direct": "内核 + initramfs 直启"}[mode])
    # ★ 必须传 arch。这里原来写的是 `_accel_name()`——签名要 guest_arch，
    #   所以每次走到这一行都 TypeError：
    #
    #       TypeError: _accel_name() missing 1 required positional argument: 'guest_arch'
    #
    #   而它【只在真启动的时候】才走到，于是：
    #     · 骨架的单元测试不覆盖这条路径
    #     · 前几轮 CI 从没跑到这里（盘都没建出来过）
    #
    #   一跑 QEMU 就现形——这正好说明"启动一次"这个验收步骤的价值。
    if use_accel and _accel_name(arch) is None:
        print("        ⚠ 这台机器上没有 /dev/kvm（或不是 macOS）→ 退回 TCG 模拟，会慢很多")
    elif not use_accel:
        print("        ⚠ 客户机和宿主不同架构，硬件加速用不了 → 走 TCG 模拟，会慢很多")
    print()

    serial_out = Path(serial_file) if serial_file else None
    if mode == "disk":
        cmd = _qemu_cmd(arch, None, None, img_path, mem, use_accel, firmware_path, serial_out)
    else:
        kernel = root_dir / "boot" / "vmlinuz"
        if not kernel.is_file():
            die(
                f"{kernel.relative_to(ROOT)} 不存在（直启要有内核）。\n"
                f"       或者用磁盘镜像：先 pantsu img {name}"
            )
        initrd = _make_initramfs(root_dir, work / f"initramfs-{arch}.cpio.gz")
        cmd = _qemu_cmd(arch, kernel, initrd, None, mem, use_accel, firmware_path, serial_out)

    print("  " + " ".join(shlex.quote(c) for c in cmd))
    print()

    if timeout is not None:
        # 脚本 / CI 模式：跑到看见约定的一句话就收工，或者跑到超时为止。
        #
        # ★ 不是"睡够 N 秒再看一眼"：那会让每次 CI 都白等满超时，也会让
        #   "起来了但慢"和"根本没起来"看起来一样。等的是【启动成功这件事
        #   本身留下的痕迹】——那是这个 workflow 里唯一真正的验收。
        proc = subprocess.Popen(cmd)
        deadline = time.time() + timeout
        text, seen = "", False
        try:
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if serial_out and serial_out.is_file():
                    text = serial_out.read_text(encoding="utf-8", errors="replace")
                    if "Kernel panic" in text:
                        break
                    if expect and expect in text:
                        seen = True
                        break
                time.sleep(1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        if serial_out and serial_out.is_file():
            text = serial_out.read_text(encoding="utf-8", errors="replace")
            log("串口", f"{serial_out}（{len(text)} 字节）")

        if expect:
            if seen:
                print()
                log("启动", f"看到「{expect}」——它起来了 ✓")
                return 0
            print()
            print(text[-1500:])
            print()
            why = "内核 panic 了" if "Kernel panic" in text else f"等了 {timeout} 秒也没看到「{expect}」"
            die(f"没起来：{why}（完整串口在 {serial_out}）")

        if timeout is not None and not expect:
            print(text[-2000:])
        return 0

    if serial_out:
        print(f"  串口写到 {serial_out}，起完自己看。Ctrl-C 结束。")
        proc = subprocess.Popen(cmd)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return 0

    print(f"  启动 {name}（{arch}）—— 退出 QEMU 按 Ctrl-A 然后 X")
    print()
    # exec：把终端整个交给 QEMU，Ctrl-A X 的语义才和平时一样。
    os.execvp(cmd[0], cmd)
    return 0          # 到不了这里
