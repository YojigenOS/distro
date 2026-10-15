#!/usr/bin/env python3
"""vm.py —— 起 QEMU 那一步里【真的不启虚拟机】的那些逻辑。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_vm.py                # 单独跑也行

【为什么值得测】这个文件的历史上有两个 bug，都是**只在真启动那一瞬间**才现形的：

  · `_accel_name()` 少传了一个参数（`TypeError`）——骨架的单元测试不覆盖，
    CI 前几轮也跑不到（盘都没建出来过），一跑 QEMU 就炸
  · QEMU 命令行里**压根没有网卡**——于是"镜像起来有没有 IP"永远验不出来，
    而 CI 一路绿

两条的形状一样：**"看起来成功了"，因为那条路没被走到。** 所以下面验的是
"交给 QEMU 的命令行长什么样"，那是宿主上就能算清楚的事。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import 安静, 会报错的混入  # noqa: E402

from pantsulib import vm  # noqa: E402


def 造命令(固件候选=(), **kw) -> list[str]:
    """跑 `_qemu_cmd`，把"有没有 qemu / 有没有 edk2"这两件事固定下来。

    ★ 不这样做的话，这些测试会失败在"这台机器没装 qemu"或"找不到 aarch64
      固件"——而那两条路和"命令行拼得对不对"无关。
    """
    默认 = dict(arch="x86_64", kernel=None, initrd=None, img=Path("/tmp/盘.img"),
              mem="1G", accel=False, firmware=None, serial_file=None)
    默认.update(kw)
    with mock.patch.object(vm.shutil, "which", lambda _: "/usr/bin/qemu"), \
         mock.patch.object(vm, "EDK2_AARCH64", list(固件候选)):
        return vm._qemu_cmd(**默认)


class Test加速器(unittest.TestCase):

    def _哪个(self, guest: str, host: str, platform: str):
        with mock.patch("platform.machine", return_value=host), \
             mock.patch.object(vm.sys, "platform", platform):
            return vm._accel_name(guest)

    def test_macOS_同架构走_hvf(self):
        # guest 是【归一之后】的名字（arm64 → aarch64 在 resolve_arch 里做过了），
        # 而宿主那边读的是 platform.machine() 的原始值。
        for guest, machine in (("aarch64", "arm64"), ("x86_64", "x86_64")):
            with self.subTest(guest=guest, machine=machine):
                self.assertEqual(self._哪个(guest, machine, "darwin"), "hvf")

    def test_跨架构退回纯模拟(self):
        """★ Apple Silicon 的 HVF 只能虚拟化 arm64 客户机。

        在 arm64 Mac 上启 x86_64 镜像、加 `-accel hvf` 会直接起不来——
        而"先本地验一遍再拿去 DD"正是靠这条路。
        """
        self.assertIsNone(self._哪个("x86_64", "arm64", "darwin"))      # x86 客户机在 arm 宿主上
        self.assertIsNone(self._哪个("aarch64", "x86_64", "darwin"))    # 反过来

    def test_linux_是_kvm_不是写死的_hvf(self):
        """写死 hvf 会让 CI（Linux）那一步直接失败——而那是镜像唯一的自动断言。"""
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch.object(vm.os, "access", return_value=True):
            self.assertEqual(self._哪个("x86_64", "x86_64", "linux"), "kvm")

    def test_linux_没有_kvm_就退回纯模拟(self):
        with mock.patch("pathlib.Path.exists", return_value=False):
            self.assertIsNone(self._哪个("x86_64", "x86_64", "linux"))

    def test_kvm_存在但没权限_也退回纯模拟(self):
        """容器里常见：/dev/kvm 在，但当前用户打不开。"""
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch.object(vm.os, "access", return_value=False):
            self.assertIsNone(self._哪个("x86_64", "x86_64", "linux"))


class Test固件(会报错的混入, unittest.TestCase):

    def test_x86_64_不用给固件(self):
        """x86_64 用 QEMU 自带的 SeaBIOS——和 TX 那台同构，什么都不用给。"""
        self.assertIsNone(vm._find_firmware(None, "x86_64"))
        self.assertIsNone(vm._find_firmware("/随便/指一个", "x86_64"))

    def test_aarch64_指的文件不存在就报错(self):
        msg = self.应当报错(vm._find_firmware, "/没有这个文件.fd", "aarch64")
        self.assertIn("/没有这个文件.fd", msg)

    def test_aarch64_指了就用它(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp) / "QEMU_EFI.fd"
            fw.write_bytes("固件".encode())
            self.assertEqual(vm._find_firmware(str(fw), "aarch64"), str(fw))

    def test_aarch64_没指就去候选列表里找(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp) / "edk2-aarch64-code.fd"
            fw.write_bytes("固件".encode())
            with mock.patch.object(vm, "EDK2_AARCH64", ["/不存在的.fd", str(fw)]):
                self.assertEqual(vm._find_firmware(None, "aarch64"), str(fw))

    def test_aarch64_都找不到要说试过哪些(self):
        with mock.patch.object(vm, "EDK2_AARCH64", ["/甲.fd", "/乙.fd"]):
            msg = self.应当报错(vm._find_firmware, None, "aarch64")
        self.assertIn("/甲.fd", msg)
        self.assertIn("/乙.fd", msg)
        self.assertIn("--firmware", msg)


class TestQEMU_命令行(会报错的混入, unittest.TestCase):

    def test_x86_64_用_q35_和_SeaBIOS(self):
        """q35 + 默认固件 = 和 TX 那台（BIOS + virtio-blk + ttyS0）同构。

        所以本地这一跑能提前回答"DD 上去能不能起"。
        """
        cmd = 造命令(arch="x86_64")
        self.assertEqual(cmd[0], "qemu-system-x86_64")
        self.assertIn("q35", cmd)
        self.assertNotIn("-bios", cmd)

    def test_aarch64_用_virt_和_edk2(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp) / "QEMU_EFI.fd"
            fw.write_bytes("固件".encode())
            cmd = 造命令(固件候选=[str(fw)], arch="aarch64", img=Path("/tmp/盘.img"))
            self.assertIn("virt", cmd)
            self.assertEqual(cmd[cmd.index("-bios") + 1], str(fw))

    def test_给虚拟机一块网卡(self):
        """★ 没有这行，测试用的虚拟机【根本没有网络设备】。

        实测就是这么漏的：镜子里没装 DHCP 客户端，而 CI 一路绿——因为 QEMU 里
        压根没网。有了 user-mode 网络，客户机会真的跑一遍 dhcpcd。
        """
        cmd = 造命令()
        self.assertIn("user,id=n0", cmd)
        self.assertIn("virtio-net-pci,netdev=n0", cmd)

    def test_启盘走_virtio_blk(self):
        cmd = 造命令(img=Path("/tmp/minimal-x86_64.img"))
        self.assertIn("file=/tmp/minimal-x86_64.img,if=virtio,format=raw", cmd)
        self.assertNotIn("-kernel", cmd)
        self.assertNotIn("-initrd", cmd)

    def test_直启给内核和_initrd_和_cmdline(self):
        cmd = 造命令(arch="x86_64", img=None,
                   kernel=Path("/tmp/vmlinuz"), initrd=Path("/tmp/initramfs.cpio.gz"))
        self.assertEqual(cmd[cmd.index("-kernel") + 1], "/tmp/vmlinuz")
        self.assertEqual(cmd[cmd.index("-initrd") + 1], "/tmp/initramfs.cpio.gz")
        self.assertNotIn("-drive", cmd)
        追加 = cmd[cmd.index("-append") + 1]
        # 直启时内核自己就是"根"，用 rdinit 指定第一个用户空间程序
        self.assertTrue(追加.endswith("rdinit=/init"), 追加)
        # 串口写在最后——它才会成为 /dev/console
        self.assertLess(追加.index("console=tty0"), 追加.index("console=ttyS0"))
        self.assertIn("console=ttyS0,115200", 追加)

    def test_aarch64_的串口是_ttyAMA0(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp) / "QEMU_EFI.fd"
            fw.write_bytes("固件".encode())
            cmd = 造命令(固件候选=[str(fw)], arch="aarch64", img=None,
                       kernel=Path("/tmp/vmlinuz"),
                       initrd=Path("/tmp/initramfs.cpio.gz"))
        追加 = cmd[cmd.index("-append") + 1]
        self.assertIn("console=ttyAMA0,115200", 追加)

    def test_串口写文件时不要图形(self):
        """给脚本和 CI 用：谁也不用盯着看。"""
        cmd = 造命令(serial_file=Path("/tmp/boot.log"))
        self.assertEqual(cmd[cmd.index("-display") + 1], "none")
        self.assertEqual(cmd[cmd.index("-serial") + 1], "file:/tmp/boot.log")
        self.assertNotIn("-nographic", cmd)

    def test_不给串口文件就开到当前终端(self):
        cmd = 造命令()
        self.assertIn("-nographic", cmd)
        self.assertNotIn("-display", cmd)

    def test_加速时用_cpu_host_纯模拟用_cpu_max(self):
        """★ 纯模拟下 `-cpu host` 会把宿主 CPU 的特性暴露给客户机，可能起不来。"""
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch.object(vm.os, "access", return_value=True), \
             mock.patch("platform.machine", return_value="x86_64"), \
             mock.patch.object(vm.sys, "platform", "linux"):
            cmd = 造命令(accel=True)
        self.assertEqual(cmd[cmd.index("-cpu") + 1], "host")
        self.assertEqual(cmd[cmd.index("-accel") + 1], "kvm")

        cmd = 造命令(accel=False)
        self.assertEqual(cmd[cmd.index("-cpu") + 1], "max")
        self.assertNotIn("-accel", cmd)

    def test_没有_qemu_就说装一下(self):
        with mock.patch.object(vm.shutil, "which", lambda _: None):
            msg = self.应当报错(vm._qemu_cmd, "x86_64", None, None, Path("/tmp/盘.img"),
                            "1G", False, None, None)
        self.assertIn("qemu", msg)


class Test启动前的护栏(会报错的混入, unittest.TestCase):

    def test_expect_没有_serial_file_就拦住(self):
        """★ 这一对是必须的：没人把串口写下来，它只能一路等到超时——
        然后看起来像"起不来"。**那种失败最费时间**，所以在门口拦住。"""
        msg = self.应当报错(vm.run, "minimal", expect=["欢迎来到"])
        self.assertIn("--serial-file", msg)

    def test_expect_给了_serial_file_就放行到下一步(self):
        """放行之后它就去找根里的内核了——这里验的是"没被那道护栏拦住"。

        （固件那一步换掉：这台机器上有没有 edk2 和这条测试无关。）
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(vm, "VAR_DIR", Path(tmp)), \
                 mock.patch.object(vm, "ROOT", Path(tmp)), \
                 mock.patch.object(vm, "_find_firmware", lambda *a: None), \
                 安静():
                msg = self.应当报错(vm.run, "minimal", serial_file="/tmp/boot.log",
                                 expect=["欢迎来到"])
        self.assertIn("vmlinuz", msg)      # 走到"根里没有内核"那一步了
        self.assertIn("pantsu img", msg)   # 并给了另一条路


class Test打_initramfs(unittest.TestCase):

    def test_有现成的就复用_不重打(self):
        """从零准备要两分钟，而 `pantsu vm` 是最常跑的——复用是默认行为。"""
        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "rootfs-apple"
            root_dir.mkdir()
            out = Path(tmp) / "initramfs-apple.cpio.gz"
            out.write_bytes(b"x" * 200_000)

            def 不该被调用(*a, **k):
                raise AssertionError("有现成的还去容器里重打了一次")

            with mock.patch.object(vm, "ROOT", Path(tmp)), \
                 mock.patch.object(vm, "run_streaming", 不该被调用), \
                 mock.patch.dict(os.environ) as env, \
                 安静():
                env.pop("PANSU_VM_FORCE", None)     # 有它就会重打，和这条测试无关
                got = vm._make_initramfs(root_dir, out, "aarch64")
            self.assertEqual(got, out)

    def test_重打时用容器里的_cpio_并且量大小(self):
        """★ 两个曾经咬过人的地方，都在这条命令里留着防线：

            find | cpio | gzip 里 cpio 没找到也照样"成功"（退出码取自 gzip）
              → 开 pipefail，并且打完【量一下大小】，太小就报错
        """
        记录: list[list[str]] = []

        def 假的_run_streaming(cmd, **_kw):
            记录.append(cmd)
            return (1, "")

        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "rootfs-apple"
            root_dir.mkdir()
            out = Path(tmp) / "initramfs-apple.cpio.gz"

            with mock.patch.object(vm, "ROOT", Path(tmp)), \
                 mock.patch.object(vm, "run_streaming", 假的_run_streaming), \
                 安静():
                with self.assertRaises(SystemExit) as cm:
                    vm._make_initramfs(root_dir, out, "aarch64")
        self.assertEqual(cm.exception.code, 1)

        cmd = 记录[0]
        self.assertEqual(cmd[0], "docker")
        self.assertIn(f"linux/aarch64", cmd)                # 容器按目标架构跑
        self.assertIn(f"{root_dir}:/root:ro", cmd)          # 根是只读挂进去的
        self.assertIn(f"OUT_NAME={out.name}", cmd)
        脚本 = cmd[-1]
        self.assertIn("set -o pipefail", 脚本)
        self.assertIn("apk add --no-cache cpio", 脚本)
        self.assertIn(str(vm.INITRAMFS_MIN_BYTES), 脚本)     # 太小就当空包

    def test_根里没有_init_就不打(self):
        """没有 /init 的根打出来的 initramfs 起不到任何作用——在容器里就拦住。"""
        记录: list[list[str]] = []

        def 假的_run_streaming(cmd, **_kw):
            记录.append(cmd)
            return (1, "")

        with tempfile.TemporaryDirectory() as tmp:
            root_dir = Path(tmp) / "rootfs-apple"
            root_dir.mkdir()
            out = Path(tmp) / "initramfs-apple.cpio.gz"
            with mock.patch.object(vm, "ROOT", Path(tmp)), \
                 mock.patch.object(vm, "run_streaming", 假的_run_streaming), \
                 安静():
                with self.assertRaises(SystemExit):
                    vm._make_initramfs(root_dir, out, "aarch64")
        self.assertIn("[ -x /root/init ]", 记录[0][-1])


class Test没测到的(unittest.TestCase):
    """★ 缺口，不是覆盖。"""

    @unittest.skipUnless(os.environ.get("PANSU_INTEGRATION"),
                         "要真的启一台 QEMU（还要有内核/镜像）：本机想跑就 "
                         "PANSU_INTEGRATION=1（Apple Silicon 上跨架构会慢到分钟级）")
    def test_把它跑起来(self):
        """端到端：真启动，等它打印出约定的那句话。

        单元测试验的是"命令行拼得对"；"它到底起不起来"只有真跑一次才知道——
        而那正是 image.yml 里那个 `--expect` 断言的全部意义。
        """
        rc = vm.run("minimal", timeout=25, serial_file="/tmp/pantsu-test-boot.log",
                    expect=["欢迎来到 Yojigen OS"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
