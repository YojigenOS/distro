#!/usr/bin/env python3
"""入口本身：`./pantsu/pantsu <子命令>` 这条路。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_cli.py               # 单独跑也行

【为什么值得测】上面那些测试都是 import pantsulib 直接调函数，而**用户和 CI
走的是另一条路**：命令行。argparse 少注册一个子命令、退出码没传出去、参数名
和 workflow 里写的不一样——都只在那条路上现形。

★ 而 CI 只走那条路。所以它是这套测试里唯一"和线上同一根线"的一档——
  只是它跑的是只读的子命令（flavors / flavor），不会动任何产物。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent

#: 六个子命令。**这份名单动过就要跟着改**——它同时是"入口注册了哪些功能"的清单。
子命令 = ("flavors", "flavor", "rootfs", "img", "publish", "vm")


def 跑(*args: str) -> subprocess.CompletedProcess:
    """以脚本方式跑一次 pantsu（和用户敲的是同一条路）。

    cwd 用仓库根：pantsu 里的相对路径（var/、variants/）都是相对它算的。
    """
    return subprocess.run([sys.executable, str(ROOT / "pantsu"), *args],
                          cwd=REPO, capture_output=True, text=True)


class Test入口(unittest.TestCase):

    def test_help_列出所有子命令(self):
        r = 跑("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in 子命令:
            with self.subTest(cmd=name):
                self.assertIn(name, r.stdout)

    def test_不给子命令要说用法并退_2(self):
        """argparse 的约定：用法错误退 2（退 1 是"跑到一半失败了"，
        两件事混在一起会让脚本分不清"我用错了"和"东西坏了"）。"""
        r = 跑()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr.lower())

    def test_不认识的子命令也说用法(self):
        r = 跑("没有这个子命令")
        self.assertEqual(r.returncode, 2)


class Test只读的子命令(unittest.TestCase):
    """这两条是"入口真能干活"的证明——它们不碰网络、不碰 docker、不写任何东西。"""

    def test_flavors_真跑一遍(self):
        r = 跑("flavors")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("base", "minimal", "devel"):
            with self.subTest(flavor=name):
                self.assertIn(name, r.stdout)
        self.assertIn("个风味", r.stdout)

    def test_flavor_打出一个风味的包清单(self):
        r = 跑("flavor", "base")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("包数:", r.stdout)
        self.assertIn("filesystem", r.stdout)

    def test_没有这个风味_退出码是_1(self):
        """★ die() 的退出码要真的走到 shell 那边：CI 靠它判断成败。

        （模块级的测试能看到 SystemExit(1)，但看不到"这个 1 有没有穿过
        python 的退出路径"——那是两条路。）
        """
        r = 跑("flavor", "没有这个风味")
        self.assertEqual(r.returncode, 1)
        self.assertIn("找不到风味", r.stderr)


if __name__ == "__main__":
    unittest.main()
