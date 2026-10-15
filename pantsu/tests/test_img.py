#!/usr/bin/env python3
"""img.py —— 建盘那一步里【不起容器】的那些逻辑。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_img.py               # 单独跑也行

【为什么值得测】建盘是"建得出来、挂不上"最贵的一类错误发生的现场：

  · `--size` 太小 → 一路走到容器里，失败在"文件系统比分区大 / parted 拒绝"
    这些看不懂的地方。**而这件事宿主上就能算清楚**（根有多大是知道的），
    所以那一段预检值得守住
  · `_mib` 读不懂就猜 → 猜出来的数字会一路用到底
  · 风味 kind 不对（容器风味拿去建盘）→ 一进门就该说清楚

真正建一张盘要 docker --privileged + 会下 grub 的网，那部分见文件末尾的缺口。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import 安静, 会报错的混入  # noqa: E402

from pantsulib import img  # noqa: E402


def 假根(var_dir: Path, arch: str, 内核: bool = True, 根有多大: int = 0):
    """在 var/<风味>/rootfs-<架构>/ 里摆一个"装好的根"（只有名字对，内容是假的）。"""
    根 = var_dir / "minimal" / f"rootfs-{arch}"
    (根 / "boot").mkdir(parents=True, exist_ok=True)
    (根 / "init").write_text("#!/bin/sh\n")
    if 内核:
        (根 / "boot" / "vmlinuz").write_bytes("假装这是内核".encode())
    if 根有多大:
        # 稀疏文件：st_size 是真的，磁盘上不占地方
        with (根 / "大文件").open("wb") as f:
            f.truncate(根有多大)
    return 根


class Test空间换算(会报错的混入, unittest.TestCase):

    def test_读得懂三种写法(self):
        for 写法, want in (("1G", 1024), ("2g", 2048), ("1024M", 1024),
                           ("600m", 600), ("768", 768), (" 1G ", 1024)):
            with self.subTest(size=写法):
                self.assertEqual(img._mib(写法), want)

    def test_读不懂就报错_别猜(self):
        """猜出来的数字会一路用到底：盘建出来了，大小不是你要的，而且没人知道。

        12.5M 也算读不懂——MiB 是整数，小数部分去哪儿了是个没说清的决定。
        """
        for 写法 in ("1T", "abc", "12.5M", "", "G"):
            with self.subTest(size=写法):
                msg = self.应当报错(img._mib, 写法)
                self.assertIn(写法, msg)      # 把看不懂的那个值原样报出来

    def test_按文件的实际长度算_不按占的块(self):
        """`du` 读的是块数，在 APFS 和 ext4 上不是一回事。

        而这里要回答的是"装进去要占多少"——那是文件长度，不是块。
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            with (d / "一个_MiB").open("wb") as f:
                f.truncate(1 << 20)
            (d / "子目录").mkdir()
            with (d / "子目录" / "一个_KiB").open("wb") as f:
                f.truncate(1 << 10)
            got = img._tree_mib(d)
        self.assertAlmostEqual(got, 1 + 1024 / 1048576, places=6)


class Test建盘前的检查(会报错的混入, unittest.TestCase):

    def test_容器风味产不出盘(self):
        """base 是 kind = container —— 拿它建盘要说清楚，而不是建出一张空的。"""
        msg = self.应当报错(img.build, "base", "aarch64")
        self.assertIn("container", msg)
        self.assertIn('"disk"', msg)

    def test_没有根就直说先装根(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(img, "VAR_DIR", Path(tmp)), \
                 mock.patch.object(img, "ROOT", Path(tmp)):
                msg = self.应当报错(img.build, "minimal", "x86_64")
        self.assertIn("/init", msg)
        self.assertIn("pantsu rootfs", msg)

    def test_没有内核要说清楚两条路(self):
        """少了内核，要么把它列进风味，要么用 --kernel 指一个。"""
        with tempfile.TemporaryDirectory() as tmp:
            假根(Path(tmp), "x86_64", 内核=False)
            with mock.patch.object(img, "VAR_DIR", Path(tmp)), \
                 mock.patch.object(img, "ROOT", Path(tmp)):
                msg = self.应当报错(img.build, "minimal", "x86_64")
        self.assertIn("vmlinuz", msg)
        self.assertIn("--kernel", msg)

    def test_size_小于分区表就报错(self):
        """515 MiB 是 ESP + bios_grub + 两端对齐——比这还小连分区都摆不下。"""
        with tempfile.TemporaryDirectory() as tmp:
            假根(Path(tmp), "x86_64")
            with mock.patch.object(img, "VAR_DIR", Path(tmp)), \
                 mock.patch.object(img, "ROOT", Path(tmp)):
                msg = self.应当报错(img.build, "minimal", "x86_64", size="100M")
        self.assertIn("太小", msg)
        self.assertIn(str(img.LAYOUT_MIB), msg)

    def test_装不下就说要多大(self):
        """★ 这段预检的全部意义：把"容器里那句看不懂的报错"换成宿主上的一句
        人话，而且带上【下次该写多少】。

        没有它，失败发生在"文件系统比分区大"——那要懂 0013 里那几个坑才看得懂。
        """
        with tempfile.TemporaryDirectory() as tmp:
            假根(Path(tmp), "x86_64", 根有多大=100 << 20)      # 100 MiB
            with mock.patch.object(img, "VAR_DIR", Path(tmp)), \
                 mock.patch.object(img, "ROOT", Path(tmp)):
                msg = self.应当报错(img.build, "minimal", "x86_64", size="600M")
        self.assertIn("装不下", msg)
        self.assertIn("--size", msg)

    def test_刚好装得下就不拦(self):
        """反过来也要验：房间够的时候不该报错（预检写严了会让正常的盘建不出来）。

        这一条走到"起容器"那一步才停——所以把 run_streaming 换掉。
        """
        with tempfile.TemporaryDirectory() as tmp:
            假根(Path(tmp), "x86_64")
            with mock.patch.object(img, "VAR_DIR", Path(tmp)), \
                 mock.patch.object(img, "ROOT", Path(tmp)), \
                 mock.patch.object(img, "run_streaming", lambda *a, **k: (1, "")), \
                 安静():
                # 容器那一步故意失败——这里只关心它在预检里【没被拦下】
                msg = self.应当报错(img.build, "minimal", "x86_64", size="1G")
        self.assertIn("建盘失败", msg)


class Test容器脚本的不变量(unittest.TestCase):

    def test_loop_设备有兜底(self):
        """失败的运行会把 loop 设备留在 docker VM 里，攒够几个之后再也拿不到。

        （真实撞过：`device node /dev/loopN is lost`。）
        """
        self.assertIn("trap", img.CONTAINER_SCRIPT)
        self.assertIn("losetup -D", img.CONTAINER_SCRIPT)

    def test_串口写在_cmdline_最后(self):
        """`console=` 写在最后的那个才是 /dev/console——顺序反了串口里一个字都没有，
        看起来就像 /init 没跑。"""
        行 = [ln for ln in img.CONTAINER_SCRIPT.splitlines()
             if ln.strip().startswith("linux /boot/")]
        self.assertEqual(len(行), 1)
        命令行 = 行[0].strip()
        self.assertTrue(命令行.endswith("init=/init"), f"内核命令行结尾不对：{命令行}")
        # 串口那句必须比 tty0 晚——它写在最后才成为 /dev/console
        self.assertLess(命令行.index("console=tty0"), 命令行.index("console=$CONSOLE"))
        self.assertEqual(命令行.count("console="), 2)

    def test_根用_PARTLABEL_不用文件系统_UUID(self):
        """裸内核的 root= 不认文件系统 UUID（源码注释原话：This does not search
        by filesystem UUIDs.）——只认 /dev/…、PARTUUID=、PARTLABEL=。"""
        self.assertIn("root=PARTLABEL=yojigen-os-root", img.CONTAINER_SCRIPT)

    def test_文件系统和分区要能对上_有自检(self):
        """文件系统比分区大 1 MiB 时内核报 bad geometry——那要挂上根才暴露。

        所以容器脚本里有一段静态自检，把它提前到"建完就能看见"。
        """
        self.assertIn("dumpe2fs", img.CONTAINER_SCRIPT)
        self.assertIn("文件系统没有超出分区", img.CONTAINER_SCRIPT)

    def test_关掉两个_GRUB_不认的_ext4_特性(self):
        """metadata_csum_seed 会让老 GRUB 掉 rescue；orphan_file 是另一个著名的坑。"""
        self.assertIn("^metadata_csum_seed,^orphan_file", img.CONTAINER_SCRIPT)

    def test_建完把属主还给宿主(self):
        """容器是 --privileged 以 root 跑的，产物归 root；而 QEMU 是宿主用户跑的。

        实测撞上过：`Could not open '…': Permission denied`——**在容器里以 root
        写、拿到容器外用**的东西都要还属主。
        """
        self.assertIn('chown "$HOST_UID:$HOST_GID" "$IMG" "$IMG.zst"', img.CONTAINER_SCRIPT)


class Test没测到的(unittest.TestCase):
    """★ 缺口，不是覆盖。"""

    @unittest.skipUnless(os.environ.get("PANSU_INTEGRATION"),
                         "要 docker（--privileged：losetup + mount）+ 会下 grub 的网"
                         " + 一个真的装好的根：本机想跑就 PANSU_INTEGRATION=1")
    def test_真建一张盘(self):
        """端到端：分区 → mkfs → 拷根 → 装 GRUB → 自检 → zstd。

        单元测试验的是"交给容器的那些输入"和"预检拦不拦得住"；盘本身能不能
        启动只有 QEMU 里跑一次才知道（image.yml 里那一步就是干这个的）。
        """
        zst = img.build("minimal", "x86_64", img.DEFAULT_REPO)
        self.assertTrue(zst.is_file())


if __name__ == "__main__":
    unittest.main()


class Test容器脚本里的元字符(unittest.TestCase):
    """★ 这条守的是一个【刚刚踩过的坑】（2026-10-15 晚，建盘当场失败）。

    `CONTAINER_SCRIPT` 里有几个【不带引号的 heredoc】（`cat > … <<EOF`）——
    而**不带引号的 heredoc 会把里面的注释也展开**：

        sh: pantsu: not found                      ← 注释里写了 `pantsu vm`，反引号当命令替换
        sh: config_directory: parameter not set    ← 注释里写了 ${config_directory}

    而那份脚本是 `set -eu` 的，所以第二句直接把整个建盘杀掉。

    ★ 规律：**heredoc 外面的注释是注释，里面的注释是代码。**
      要在那里面写美元号、反引号、反斜杠，都得先转义。

    ★ 而为什么值得一条测试：这个坑【本地的单元测试全绿、CI 前面几步也全绿】
      —— 它只在那一步真的跑起来时才炸。而那时它已经花了十几分钟。
    """

    def _heredoc_段(self, 脚本文本: str):
        """把每个 `cat > … <<EOF … EOF` 之间的内容切出来。"""
        import re
        段s = []
        for m in re.finditer(r'<<(?:\'|")?EOF(?:\'|")?\n(.*?)\nEOF', 脚本文本, re.S):
            段s.append(m.group(1))
        return 段s

    def test_heredoc_里的注释不许有裸的元字符(self):
        import re
        from pantsulib import img
        段s = self._heredoc_段(img.CONTAINER_SCRIPT)
        self.assertTrue(段s, "一个 heredoc 都没找到 —— 这条测试的判据过时了")

        # ★ 只扫【注释行】—— 那才是这条测试要说的事。
        #
        #   代码行里的 `$ROOT_UUID` / `$KERNEL_NAME` / `$CONSOLE` 是【要展开】的
        #   （少了它们 grub.cfg 里会留下字面的变量名，机器起不来）。
        #   而我第一版把整段都扫了，于是它把这三位也报了出来 —— 测试太严，
        #   和太松一样没用：它会逼着下一个人把该展开的东西转义掉。
        坏 = []
        for n, 段 in enumerate(段s, 1):
            for i, line in enumerate(段.split("\n"), 1):
                if not line.lstrip().startswith("#"):
                    continue
                去转义 = re.sub(r"\\[\$`\\]", "", line)
                if "$" in 去转义:
                    坏.append(f"第 {n} 段第 {i} 行有裸的美元号: {line.strip()[:70]}")
                if "`" in 去转义:
                    坏.append(f"第 {n} 段第 {i} 行有裸的反引号: {line.strip()[:70]}")
        self.assertFalse(坏, "heredoc 里（包括注释）有会被 shell 展开的写法：\n  "
                             + "\n  ".join(坏))

    def test_而该展开的还在(self):
        """反过来那一头：`$ROOT_UUID` / `$KERNEL_NAME` / `$CONSOLE` 必须能展开。

        少了这句话，上面那条测试可以用"把 heredoc 改成带引号"来作弊 ——
        而那样 grub.cfg 里会留下字面的 `$ROOT_UUID`，机器起不来。
        """
        from pantsulib import img
        段s = self._heredoc_段(img.CONTAINER_SCRIPT)
        合 = "\n".join(段s)
        for 变量 in ("$ROOT_UUID", "$KERNEL_NAME", "$CONSOLE"):
            self.assertIn(变量, 合, f"{变量} 不见了 —— 它是要在 heredoc 里展开的")
            self.assertNotIn("\\" + 变量, 合, f"{变量} 被转义了 —— 那它就展开不了")
