#!/usr/bin/env python3
"""flavor.py —— 风味定义（variants/*.toml）的读取与校验。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_flavor.py            # 单独跑也行

【为什么值得测】`variants/*.toml` 是"这个镜像里有什么"的**唯一出处**，而读它
的地方不止一处（rootfs / img / vm，还有 CI）。文件坏了要在【读的那一刻】报
出来，而不是等装完一个系统才发现里面少了一半东西。

★ 校验的价值全在**坏输入**上，所以下面大半的用例是"喂一个坏文件，看它报不报"。
  好输入那边（仓库里真实的三个风味）由第一条守着——它同时是"校验还活着"的
  证明：2026-10-12 之前这段校验躺在一个叫 variants.py.待改 的死文件里，
  没有任何地方 import 它。

这份文件从仓库根的 tests/test_pantsu.py 搬来（2026-10-15）：那边是仓库级测试，
而它测的东西全在 pantsu 里。搬过来之后补上了坏输入那一半。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import 安静, 会报错的混入  # noqa: E402

from pantsulib import flavor  # noqa: E402

#: 一个最小的、合法的风味定义。坏用例都从它改一个字，这样"哪一处坏了"是明确的。
好的 = """\
[variant]
name        = "试验"
kind        = "container"
description = "测试用"
packages    = ["filesystem", "glibc"]
"""


@contextlib.contextmanager
def 风味目录(文件们: dict[str, str]):
    """建一个临时 variants/，把 flavor.VARIANTS_DIR 指过去。

    ★ 改模块常量，而不是往真实的 variants/ 里写坏文件：**测试不许弄脏真实输入**。
      一个坏文件留在原地，后面所有测试和 CI 都会跟着坏——而那种坏法很难查，
      因为它看起来像"风味真的出问题了"。
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for 名, 内容 in 文件们.items():
            (d / 名).write_text(内容, encoding="utf-8")
        with mock.patch.object(flavor, "VARIANTS_DIR", d):
            yield d


class Test真实的三个风味(unittest.TestCase):

    def test_仓库里每个风味都读得进来(self):
        """这条最值钱：CI 一跑就知道有没有风味文件坏了。"""
        names = flavor.names()
        self.assertTrue(names, "variants/ 里一个风味都没有？")
        for n in names:
            with self.subTest(flavor=n):
                v = flavor.load(n)
                self.assertEqual(v["name"], n)
                self.assertTrue(v["packages"])
                self.assertIn(v["_kind"], flavor.KINDS)

    def test_names_来自目录_排好序(self):
        """`pantsu flavors` 的输出顺序由它决定——少一个名字，那个风味就"不见了"。"""
        got = flavor.names()
        self.assertEqual(got, sorted(got))
        self.assertEqual(got, sorted(p.stem for p in flavor.VARIANTS_DIR.glob("*.toml")))

    def test_加载会带上_kind_和_path(self):
        """调用方靠这两个下划线字段干活（img 挑 disk、repo 报错要指文件）。"""
        v = flavor.load("base")
        self.assertEqual(v["_kind"], "container")
        self.assertTrue(v["_path"].is_file())
        self.assertEqual(v["_path"].name, "base.toml")

    def test_minimal_是磁盘风味(self):
        v = flavor.load("minimal")
        self.assertEqual(v["_kind"], "disk")
        # 启动链上的三样必须在——它们少了系统就起不来，而"少了"这件事
        # 在装根那一步是看不出来的（装根不执行被装的二进制）。
        for p in ("yojigen-os-init", "linux", "util-linux"):
            self.assertIn(p, v["packages"])


class Test坏文件(会报错的混入, unittest.TestCase):

    def test_文件名和_name_不一致要报错(self):
        """★ 不一致时「按名字找文件」会有两个答案，症状是两个风味的内容互相串。

        这种错很难在事后看出来：装出来的根看着正常，只是里面是【别人】的包。

        文件名叫「另一个名字」、里面写的是「试验」——不一致的正是这一对。
        """
        with 风味目录({"另一个名字.toml": 好的}):
            msg = self.应当报错(flavor.load, "另一个名字")
        self.assertIn("试验", msg)
        self.assertIn("另一个名字", msg)
        self.assertIn("对不上", msg)

    def test_缺_variant_段要报错(self):
        with 风味目录({"试验.toml": 'packages = ["glibc"]\n'}):
            msg = self.应当报错(flavor.load, "试验")
        self.assertIn("[variant]", msg)

    def test_缺_name_要报错(self):
        with 风味目录({"试验.toml": '[variant]\npackages = ["glibc"]\n'}):
            msg = self.应当报错(flavor.load, "试验")
        self.assertIn("name", msg)

    def test_缺_packages_要报错(self):
        with 风味目录({"试验.toml": '[variant]\nname = "试验"\n'}):
            msg = self.应当报错(flavor.load, "试验")
        self.assertIn("packages", msg)

    def test_packages_空的要报错(self):
        """空清单装出来的根是空的，而那要等到起不来才发现。"""
        with 风味目录({"试验.toml": '[variant]\nname = "试验"\npackages = []\n'}):
            msg = self.应当报错(flavor.load, "试验")
        self.assertIn("非空", msg)

    def test_packages_写成字符串要报错_而且要报对原因(self):
        """`packages = "glibc"` 是 toml 里合法的写法，而它会被当成包列表用。

        字符串是【可迭代】的，`' '.join("glibc")` 会变成 "g l i b c"——
        一串单字母的包名，然后 apk 会报"没有这些包"。

        ★ 所以这里必须挡住，而且报的得是**真原因**：原来那句是"packages
          不能为空"——可它明明不空，是类型不对。一个说错原因的报错会把
          人往错的方向查。
        """
        with 风味目录({"试验.toml": '[variant]\nname = "试验"\npackages = "glibc"\n'}):
            msg = self.应当报错(flavor.load, "试验")
        self.assertIn("非空", msg)          # 要的是一个非空的列表
        self.assertIn("str", msg)           # 现在拿到的是字符串（不是"空"）

    def test_kind_不认识要报错(self):
        """kind 拼错（比如 dockerfile）不能默认成 container 就往下走。"""
        with 风味目录({"试验.toml":
                    '[variant]\nname = "试验"\nkind = "dockerfile"\npackages = ["glibc"]\n'}):
            msg = self.应当报错(flavor.load, "试验")
        self.assertIn("dockerfile", msg)
        for k in flavor.KINDS:            # 报错要告诉人合法值是什么
            self.assertIn(k, msg)

    def test_kind_不写就是_container(self):
        """0007 说 kind 默认是容器——这是绝大多数风味的情形，值得钉住。"""
        with 风味目录({"试验.toml": 好的}):
            self.assertEqual(flavor.load("试验")["_kind"], "container")

    def test_找不到的风味要报错_并提示现有的(self):
        """写错名字时最需要的是"现在有哪些"。"""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            flavor.load("没有这个风味")
        self.assertIn("没有这个风味", err.getvalue())
        self.assertIn("base", err.getvalue())

    def test_一个风味都没有时也要说清楚(self):
        with 风味目录({}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                flavor.load("base")
            self.assertIn("还没有任何风味定义", err.getvalue())


class Test产出资格(会报错的混入, unittest.TestCase):

    def test_names_在没有目录时是空的(self):
        """目录都可能不在（比如只装了 pantsu、没有 variants/）。不该炸。"""
        with mock.patch.object(flavor, "VARIANTS_DIR", Path("/没有这个目录")):
            self.assertEqual(flavor.names(), [])

    def test_现在能产出的放行(self):
        for name in flavor.PRODUCIBLE:
            with self.subTest(kind=name):
                with 风味目录({"试验.toml":
                            f'[variant]\nname = "试验"\nkind = "{name}"\npackages = ["glibc"]\n'}):
                    flavor.require_producible(flavor.load("试验"))   # 不抛就是过

    def test_还没实现的拦住_而且名单不能过时(self):
        """★ 这条守的是【话说得要真】。

        报错里会列"0007 里哪几种还没实现"。2026-10-12 之前那句话写死了
        "iso / disk / kernel 都还没有实现"——而 disk 那天已经能建出来了
        （pantsulib/img.py）。一句写在报错里的假话比不说更糟：它恰好是人
        卡住的时候【最信】的那句话。

          所以：名单从数据算出来，而这里验它算得对。
        """
        还没实现 = [k for k in flavor.KINDS if k not in flavor.PRODUCIBLE]
        self.assertTrue(还没实现, "四种形式全实现了？那这个函数该删掉了")

        for kind in 还没实现:
            with self.subTest(kind=kind):
                with 风味目录({"试验.toml":
                            f'[variant]\nname = "试验"\nkind = "{kind}"\npackages = ["glibc"]\n'}):
                    msg = self.应当报错(flavor.require_producible, flavor.load("试验"))

                self.assertIn(kind, msg)                       # 缺的是哪一种，说清楚
                for done in flavor.PRODUCIBLE:
                    self.assertIn(done, msg)                   # 能产出的也说清楚（给条出路）

                名单 = [ln for ln in msg.splitlines() if "都还没有实现" in ln]
                self.assertEqual(len(名单), 1, f"报错里该有一行说「还没实现」的是哪些：{msg}")
                for done in flavor.PRODUCIBLE:
                    self.assertNotIn(done, 名单[0],
                                     f"{done} 已经能产出了，不该出现在「还没实现」的名单里")


class Test展示(unittest.TestCase):

    def test_show_打出包清单(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            flavor.show("base")
        text = out.getvalue()
        v = flavor.load("base")
        self.assertIn(f"包数: {len(v['packages'])}", text)
        for p in v["packages"]:
            self.assertIn(f"· {p}", text)

    def test_listing_每个风味都真读一遍(self):
        """`pantsu flavors` 是"这个仓库里有什么"的第一入口——它必须真读文件。

        只列文件名（不读内容）的写法在文件坏了的时候照样"成功"，而那是这个
        命令最该抓住的事。
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            flavor.listing()
        text = out.getvalue()
        names = flavor.names()
        for n in names:
            self.assertIn(n, text)
        self.assertIn(f"{len(names)} 个风味", text)

    def test_listing_碰到坏文件就报错(self):
        with 风味目录({"试验.toml": '[variant]\nname = "对不上"\npackages = ["glibc"]\n'}):
            with 安静():
                with self.assertRaises(SystemExit) as cm:
                    flavor.listing()
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()


class Test_extends(unittest.TestCase):
    """一个风味可以【在另一个的基础上加东西】（2026-10-22 加）。

    ★ 起因是 `server`：它就是 `minimal` 加上"sshd 自启"。没有 `extends` 的话，
      minimal 那 46 行包列表要复制一份 —— 而复制的那份会漂移，漂移的症状是
      "server 少了 minimal 后来加的那个包"，很难在事后看出来。
    """

    def _造(self, tmp: Path, 名: str, 内容: str):
        (tmp / f"{名}.toml").write_text(内容, encoding="utf-8")

    def _load(self, tmp: Path, 名: str):
        with mock.patch.object(flavor, "VARIANTS_DIR", tmp), 安静():
            return flavor.load(名)

    def test_父的包接在前面_自己在后面(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._造(tmp, "父", '[variant]\nname = "父"\npackages = ["a", "b"]\n')
            self._造(tmp, "子", '[variant]\nname = "子"\nextends = "父"\npackages = ["c"]\n')
            v = self._load(tmp, "子")
        self.assertEqual(v["packages"], ["a", "b", "c"])

    def test_重复的包只留一次(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._造(tmp, "父", '[variant]\nname = "父"\npackages = ["a", "b"]\n')
            self._造(tmp, "子", '[variant]\nname = "子"\nextends = "父"\npackages = ["b", "c"]\n')
            v = self._load(tmp, "子")
        self.assertEqual(v["packages"], ["a", "b", "c"], "父在前、保序、去重")

    def test_kind_没写就跟着父(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._造(tmp, "父", '[variant]\nname = "父"\nkind = "disk"\npackages = ["a"]\n')
            self._造(tmp, "子", '[variant]\nname = "子"\nextends = "父"\n')
            v = self._load(tmp, "子")
        self.assertEqual(v["_kind"], "disk")

    def test_services_也合并(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._造(tmp, "父", '[variant]\nname = "父"\npackages = ["a"]\nservices = ["x"]\n')
            self._造(tmp, "子", '[variant]\nname = "子"\nextends = "父"\nservices = ["y"]\n')
            v = self._load(tmp, "子")
        self.assertEqual(v["services"], ["x", "y"])

    def test_成环要报错(self):
        """★ 不报错就是无限递归 —— 而那会以一个看不懂的 RecursionError 出现。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._造(tmp, "甲", '[variant]\nname = "甲"\nextends = "乙"\n')
            self._造(tmp, "乙", '[variant]\nname = "乙"\nextends = "甲"\n')
            with self.assertRaises(SystemExit):
                self._load(tmp, "甲")

    def test_有_extends_就可以不写_packages(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._造(tmp, "父", '[variant]\nname = "父"\npackages = ["a"]\n')
            self._造(tmp, "子", '[variant]\nname = "子"\nextends = "父"\n')
            v = self._load(tmp, "子")
        self.assertEqual(v["packages"], ["a"])


class Test_services_字段(unittest.TestCase):
    """`services` 是这个风味【启用】哪些服务（决策记录 0024）。"""

    def _造(self, tmp: Path):
        (tmp / "试.toml").write_text(
            '[variant]\nname = "试"\npackages = ["a"]\nservices = ["sshd"]\n',
            encoding="utf-8")

    def test_正形(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._造(tmp)
            with mock.patch.object(flavor, "VARIANTS_DIR", tmp), 安静():
                v = flavor.load("试")
        self.assertEqual(v["services"], ["sshd"])

    def test_不写就是空(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "试.toml").write_text('[variant]\nname = "试"\npackages = ["a"]\n',
                                        encoding="utf-8")
            with mock.patch.object(flavor, "VARIANTS_DIR", tmp), 安静():
                v = flavor.load("试")
        self.assertEqual(v["services"], [])

    def test_名字里有空格要报错(self):
        """★ `"sshd "` 多一个空格会让"启用了但永远不起"，而症状在开机之后。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "试.toml").write_text(
                '[variant]\nname = "试"\npackages = ["a"]\nservices = ["sshd "]\n',
                encoding="utf-8")
            with mock.patch.object(flavor, "VARIANTS_DIR", tmp), 安静(), \
                 self.assertRaises(SystemExit):
                flavor.load("试")

    def test_仓库里的_server_是从_minimal_来的(self):
        """真的那份文件 —— 这条防的是"server.toml 写坏了而没人发现"。"""
        with 安静():
            v = flavor.load("server")
            m = flavor.load("minimal")
        self.assertEqual(v["_extends"], "minimal")
        self.assertEqual(v["packages"], m["packages"], "server 现在一个包都不加")
        self.assertIn("sshd", v["services"])
        self.assertEqual(v["_kind"], "disk", "kind 跟着 minimal")
