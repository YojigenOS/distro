#!/usr/bin/env python3
"""common.py —— 路径常量、架构解析、日志、命令执行。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_common.py            # 单独跑也行

【为什么值得测】这个文件里全是**别的文件依赖的前提**，而它们错了不会立刻报错：

  · 路径常量错一层（比如 ROOT 多退一级）→ 报的是"找不到 variants/"，
    看上去像文件丢了，不像常量写错了
  · `resolve_arch` 把架构归一化错了 → 装出来的根是另一个架构的，而这要等到
    运行的时候才发现（装根本身不执行被装的二进制，见 root.py）
  · `TAR_MTIME` 挪一下 → 每个包、每张盘都变了，而 0005 要的是"同样的输入
    建出同样的东西"。**同一个输入建出两个不同的产物，没人会当场发现。**

最后一条尤其值得钉住：它是"能算出来的，不要靠人记得"在时间戳上的形态。
"""

from __future__ import annotations

import ast
import collections
import contextlib
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import 安静, 会报错的混入  # noqa: E402

from pantsulib import common  # noqa: E402


class Test路径常量(unittest.TestCase):

    def test_根目录就是本仓库(self):
        """ROOT 是上三级算出来的。算错一级不会报错，只会在用到时找不到东西。"""
        for name, path in (("ROOT", common.ROOT),
                           ("VARIANTS_DIR", common.VARIANTS_DIR),
                           ("KEYS_DIR", common.KEYS_DIR)):
            self.assertTrue(path.is_dir(), f"{name} 指向的 {path} 不是目录")

        # 仓库的身份：入口脚本、风味定义、README 都在这儿。
        self.assertTrue((common.ROOT / "pantsu" / "pantsu").is_file())
        self.assertTrue((common.ROOT / "README.md").is_file())
        self.assertTrue((common.VARIANTS_DIR / "base.toml").is_file())

    def test_产物目录在仓库里(self):
        """var/ 是产物（.gitignore 里有它）——留在仓库里是为了 ls 一眼分清源码和产物。

        它现在不一定存在（没跑过组装就没有），所以只查"在仓库里"。
        """
        self.assertTrue(common.VAR_DIR.is_relative_to(common.ROOT))
        self.assertEqual(common.VAR_DIR.name, "var")

    def test_常量只定义一次(self):
        """同一个名字定义两遍，后面那份静默生效——改第一处不会报错，只是没用。

        packages 那边的 common.py 真的这样过（一段常量被误粘了三遍，实际生效的
        是最后一份），而当时正要做的恰好是"把产物目录集中到 build/"。
        这类错误查起来很贵、防起来很便宜。
        """
        found = {}
        for f in sorted((ROOT / "pantsulib").glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            defines = collections.Counter()
            for node in tree.body:
                targets = []
                if isinstance(node, ast.Assign):
                    targets = [t for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target]
                for t in targets:
                    if t.id.isupper():
                        defines[t.id] += 1
            dup = {k: v for k, v in defines.items() if v > 1}
            if dup:
                found[f.name] = dup

        self.assertEqual(
            found, {},
            "有常量被定义了多次（后定义的静默覆盖前面的）：\n  "
            + "\n  ".join(f"{f}: {sorted(d)}" for f, d in found.items()))


class Test架构(会报错的混入, unittest.TestCase):

    def test_别名表覆盖两个架构(self):
        """装根只认 aarch64 / x86_64 这两个名字（仓库路径就是 <架构>/）。

        别名表少一个，`--arch amd64` 会被当成"不认识的架构"——而它明明认识。
        """
        self.assertEqual(common.ARCH_ALIASES["arm64"], "aarch64")
        self.assertEqual(common.ARCH_ALIASES["amd64"], "x86_64")
        self.assertEqual(set(common.ARCH_ALIASES.values()), {"aarch64", "x86_64"})

    def test_别名归一(self):
        for given, want in (("arm64", "aarch64"), ("aarch64", "aarch64"),
                            ("x86_64", "x86_64"), ("amd64", "x86_64")):
            with self.subTest(requested=given):
                self.assertEqual(common.resolve_arch(given), want)

    def test_不给就取宿主架构(self):
        for machine, want in (("arm64", "aarch64"), ("aarch64", "aarch64"),
                              ("x86_64", "x86_64")):
            with self.subTest(machine=machine):
                with mock.patch("platform.machine", return_value=machine):
                    self.assertEqual(common.resolve_arch(None), want)

    def test_不认识的架构要报错(self):
        """★ 报错里必须有那个【不认识的】名字。

        只说"不认识的架构"的话，用的人看不出是自己把 riscv64 打错了，
        还是这个工具不认某个明明存在的架构。
        """
        with mock.patch("platform.machine", return_value="x86_64"):
            msg = self.应当报错(common.resolve_arch, "riscv64")
        self.assertIn("riscv64", msg)

    def test_不认识的宿主架构也要报错(self):
        with mock.patch("platform.machine", return_value="mips"):
            msg = self.应当报错(common.resolve_arch, None)
        self.assertIn("mips", msg)

    def test_不拒绝非宿主架构(self):
        """换架构在这一层不是换工具链，是换一串包——真拒了就没法在 Mac 上装 x86_64。"""
        with mock.patch("platform.machine", return_value="arm64"):
            self.assertEqual(common.resolve_arch("x86_64"), "x86_64")


class Test仓库常量(unittest.TestCase):

    def test_仓库_URL_拼得出_APKINDEX_的地址(self):
        """两个仓库之间的契约就是这个 URL（README 里那条）。"""

        def index_url(repo: str, arch: str) -> str:
            # 和 root._check_repo 里拼的是同一种形状
            return f"{repo.replace('host.docker.internal', 'localhost')}/{arch}/APKINDEX.tar.gz"

        self.assertEqual(index_url(common.DEFAULT_REPO, "aarch64"),
                         "https://repo.os.yojigen.cn/aarch64/APKINDEX.tar.gz")
        self.assertEqual(index_url(common.LOCAL_REPO, "x86_64"),
                         "http://localhost:18080/x86_64/APKINDEX.tar.gz")

        # ★ 结尾不能带斜杠：带上就是 https://…//aarch64/…，有些服务对双斜杠
        #   直接 404——一个"看着差不多"的地址。
        for repo in (common.DEFAULT_REPO, common.LOCAL_REPO):
            self.assertFalse(repo.endswith("/"), f"{repo} 结尾多了斜杠")

    def test_本地仓库地址只在容器里有意义(self):
        """容器里的 localhost 是容器自己——所以本地仓库要用 host.docker.internal。

        这条一旦写反（LOCAL_REPO 写成 http://localhost:18080），症状是
        容器里"连不上仓库"，而宿主上 curl 同一个地址是通的。
        """
        self.assertIn("host.docker.internal", common.LOCAL_REPO)

    def test_TAR_MTIME_就是_2026_01_01(self):
        """tar 里所有成员的时间戳（packages 的决策记录 0005）。

        ★ 这个值一变，**每个交付物都变**——包括"同样的输入"。所以它不是
          "随便挑的一个日期"，是一条被钉住的承诺，值得在这里写死。
        """
        self.assertEqual(common.TAR_MTIME, "@1767225600")
        self.assertEqual(
            time.strftime("%Y-%m-%d %H:%M UTC",
                          time.gmtime(int(common.TAR_MTIME.lstrip("@")))).split()[0],
            "2026-01-01")

    def test_风味清单里没有借来的工具镜像(self):
        """alpine 是【借工具】——交付物里不该出现它。

        这条守的是"借的是工具不是包"：TOOL_IMAGE 用来跑 apk/tar/grub，
        它不是我们发的包，也不该被人列进风味。
        """
        self.assertEqual(common.TOOL_IMAGE, "alpine:latest")
        for f in sorted(common.VARIANTS_DIR.glob("*.toml")):
            text = f.read_text(encoding="utf-8")
            # 注释里可以提它（说明"为什么是借的"），只有真被列进 packages 才算错
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                self.assertNotIn("alpine", stripped,
                                 f"{f.name} 的包列表里出现了借来的工具镜像：{stripped}")


class Test输出(unittest.TestCase):

    def test_log_和_warn_往_stdout_说(self):
        """进度和警告是"给人看的"，不是错误——错误走 stderr（见下一条）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            common.log("风味", "base")
            common.warn("这一步是过渡期的")
        self.assertIn("[风味]", out.getvalue())
        self.assertIn("base", out.getvalue())
        self.assertIn("警告", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_die_写_stderr_并退_1(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                common.die("东西坏了")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("东西坏了", err.getvalue())
        self.assertEqual(out.getvalue(), "", "错误信息不该混进 stdout")


class Test命令执行(会报错的混入, unittest.TestCase):

    def test_run_失败即报错并带上退出码(self):
        """★ 退出码要出现在报错里：shell 那边靠它，而"命令失败"三个字
        没法告诉人失败在哪一步。"""
        msg = self.应当报错(common.run, ["/bin/sh", "-c", "exit 3"])
        self.assertIn("3", msg)
        self.assertIn("/bin/sh", msg)

    def test_run_成功就什么都不说(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            common.run(["/bin/sh", "-c", "exit 0"])
        self.assertEqual(out.getvalue(), "")

    def test_run_streaming_一边打一边攒(self):
        """为什么不能直接 capture_output：apk 装包要看得见进度。

        为什么还要攒一份：失败时要能从输出里认出【哪些包不在仓库里】
        （root.MISSING_RE 就是拿去解析这份文本的）。
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc, text = common.run_streaming(
                ["/bin/sh", "-c", "echo 第一行; echo 第二行; exit 0"])
        self.assertEqual(rc, 0)
        self.assertEqual(text, "第一行\n第二行\n")
        # 屏幕上也要有——而且带缩进（它是【子命令】的输出）
        self.assertIn("第一行", out.getvalue())
        self.assertIn("      第一行", out.getvalue())

    def test_run_streaming_把退出码原样返回_不自己退出(self):
        """和 run() 的区别就在这儿：调用方要自己判断（比如跳过缺失的包重试）。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc, text = common.run_streaming(["/bin/sh", "-c", "echo 坏了 >&2; exit 7"])
        self.assertEqual(rc, 7)
        # stderr 被并进 stdout（stderr=STDOUT）——否则调用方会漏掉一半报错
        self.assertIn("坏了", text)
        self.assertIn("坏了", out.getvalue())

    def test_run_streaming_能把脚本喂进_stdin(self):
        """这个是给容器用的：整段脚本从 stdin 喂给 `sh`（root._probe 就是这么干的）。"""
        with 安静():
            rc, text = common.run_streaming(["/bin/sh"], stdin_text="echo 从标准输入来\n")
        self.assertEqual(rc, 0)
        self.assertIn("从标准输入来", text)


if __name__ == "__main__":
    unittest.main()
