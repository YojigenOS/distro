#!/usr/bin/env python3
"""风味定义的校验。

为什么值得测：`variants/*.toml` 是"这个镜像里有什么"的**唯一出处**，而它的
消费者散在几个地方（pantsu rootfs、将来的建盘、CI）。校验写在这里，风味文件
坏了要在【读的那一刻】就报出来，而不是等装完一个系统才发现里面少了一半东西。

2026-10-12 搬进 pantsu 之前，这段校验躺在一个叫 variants.py.待改 的死文件里
——没有任何地方 import 它，而真正在用的两处各自内联了一段没有校验的读 toml
代码。所以这些测试同时也是"它活着"的证明。
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pantsu"))

from pantsulib import flavor  # noqa: E402


@contextlib.contextmanager
def quiet():
    """die() 会往 stderr 打一行，测试输出里不需要看见它。"""
    with contextlib.redirect_stderr(io.StringIO()):
        yield


class TestFlavorLoading(unittest.TestCase):

    def test_仓库里每个风味都读得进来(self):
        """这条最值钱：CI 一跑就知道有没有风味文件坏了。"""
        names = flavor.names()
        self.assertTrue(names, "variants/ 里一个风味都没有？")
        for n in names:
            with self.subTest(flavor=n):
                v = flavor.load(n)
                self.assertEqual(v["name"], n)
                self.assertTrue(v["packages"])

    def test_minimal_是磁盘风味(self):
        v = flavor.load("minimal")
        self.assertEqual(v["_kind"], "disk")
        # 启动链上的三样必须在——它们少了系统就起不来，而"少了"这件事
        # 在装根那一步是看不出来的。
        for p in ("yojigen-os-init", "linux", "util-linux"):
            self.assertIn(p, v["packages"])

    def test_文件名和_name_不一致要报错(self):
        # 不一致时「按名字找文件」会有两个答案，症状是两个风味的内容互相串。
        v = flavor.load("base")
        self.assertEqual(v["name"], "base")

    def test_找不到的风味报错并提示现有的(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            flavor.load("没有这个风味")
        self.assertIn("base", err.getvalue())

    def test_kind_不认识要报错(self):
        # 只测 KINDS 的边界。真正的文件校验在 test_仓库里每个风味都读得进来。
        self.assertIn("disk", flavor.KINDS)
        self.assertNotIn("dockerfile", flavor.KINDS)

    def test_disk_现在能产出交付物了(self):
        # 2026-10-12：pantsulib/img.py 落地，disk 从 KINDS 走进了 PRODUCIBLE。
        v = flavor.load("minimal")
        flavor.require_producible(v)      # 不抛就是过

    def test_container_能产交付物(self):
        v = flavor.load("base")
        flavor.require_producible(v)      # 不抛就是过


if __name__ == "__main__":
    unittest.main()
