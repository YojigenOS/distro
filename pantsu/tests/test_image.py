#!/usr/bin/env python3
"""image.py —— 容器交付物那一步（装根 → 打 tar → docker import）。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_image.py             # 单独跑也行

【为什么值得测】镜像是**别人会拉的东西**，而它的两个部分——标签和 CMD——
都不是"看得见"的：docker import 少了 CMD，`docker run` 报 no command
specified；标签少一个，外面的人看不出这个镜像从哪来。两样都要等到用的时候
才发现（决策记录 0003 的"验在哪儿"说的就是这件事）。

装根本身（root.build）不在这里测：它要 docker + 真仓库，那部分在
`pantsu/tests/test_root.py`（输入那一层）和 CI 的 workflow（端到端）里。
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from support import 会报错的混入  # noqa: E402

from pantsulib import common, image, verify  # noqa: E402


class Test标签和命令(unittest.TestCase):

    def _参数(self, revision="a" * 40, **kw):
        默认 = dict(name="base", arch="aarch64", tag="edge",
                  built="2026-10-15T00:00:00Z", revision=revision)
        默认.update(kw)
        return image.import_args(**默认)

    def test_每一条_c_指令都成对(self):
        args = self._参数()
        self.assertEqual(len(args) % 2, 0)
        for i in range(0, len(args), 2):
            with self.subTest(i=i):
                self.assertEqual(args[i], "-c")
                self.assertTrue(args[i + 1].strip(), "空的 -c 指令")

    def test_有默认_CMD(self):
        """★ docker import 不像 Dockerfile 那样有默认值。不给的话
        `docker run <镜像>` 直接报 no command specified——而那正是最自然的用法。"""
        self.assertIn('CMD ["/bin/sh"]', self._参数())

    def test_路径和_语言环境(self):
        指令 = self._参数()
        self.assertIn("ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                      指令)
        self.assertIn("ENV LANG=C.UTF-8", 指令)

    def test_清单里的标签一个不少(self):
        """个数从 `common.LABELS` 读——这里不写死。

        （原来写死 9，并且还问了一遍 `image.OWN_LABELS` 那份影子清单：清单里
        加一个标签，这条测试照样是绿的。见 `common.LABELS` 那段。）
        """
        标签 = [a for a in self._参数() if a.startswith("LABEL ")]
        self.assertEqual(len(标签), len(common.LABELS))
        for k in common.LABELS:
            with self.subTest(label=k):
                self.assertTrue(any(t.startswith(f"LABEL {k}=") for t in 标签))

    def test_拿不到_commit_就不写那个标签(self):
        """★ 这不是"少一个"：一个说"我不知道"的标签比一个写着 "unknown" 的
        标签有用——后者看起来像"这个 commit 叫 unknown"。

        ★ 少的那一个是 `revision` —— 清单里唯一允许缺的。个数从清单推出来，
        不写死（原来写死的 8 是"九个减一个"，清单一变它就静静地对不上了）。
        """
        标签 = [a for a in self._参数(revision="") if a.startswith("LABEL ")]
        self.assertEqual(len(标签), len(common.LABELS) - 1)
        self.assertFalse([t for t in 标签 if "revision" in t])

    def test_title_的值要带引号(self):
        """`docker import -c "LABEL …"` 会【按空格切】。

        不加引号报的是 `Syntax error - can't find = in "OS"`——只提到 OS，
        看不出是 title 那一行出的问题。
        """
        标题 = [t for t in self._参数() if "image.title" in t][0]
        self.assertEqual(标题, 'LABEL org.opencontainers.image.title="Yojigen OS base"')

    def test_风味和架构写进我们自己的命名空间(self):
        """命名空间是反写域名，所以根必须是我们的域名（原来的 org.yojigenos
        声明的是一个不存在的域名——合法不等于对）。"""
        指令 = self._参数(name="minimal", arch="x86_64")
        self.assertIn("LABEL cn.yojigen.os.flavor=minimal", 指令)
        self.assertIn("LABEL cn.yojigen.os.arch=x86_64", 指令)
        self.assertIn("LABEL cn.yojigen.os.built=2026-10-15T00:00:00Z", 指令)

    def test_created_和_built_是同一个时刻(self):
        """0003 说它们是一回事：标准组已经有 created，built 只是自己人看着顺。"""
        指令 = self._参数()
        created = [t for t in 指令 if "image.created" in t][0]
        built = [t for t in 指令 if "cn.yojigen.os.built" in t][0]
        self.assertEqual(created.split("=", 1)[1], built.split("=", 1)[1])


class Test_revision(unittest.TestCase):

    def test_在_git_仓库里拿得到_commit(self):
        """跑的就是这个仓库本身——它必须是绿的，否则 revision 是假的。"""
        rev = image._revision()
        self.assertRegex(rev, r"^[0-9a-f]{40}$")

    def test_没有_git_就不写(self):
        with mock.patch.object(image.shutil, "which", lambda _: None):
            self.assertEqual(image._revision(), "")

    def test_git_命令失败也不写(self):
        """不 checkout 到 git 仓库里（比如下载的 tarball）时也要能组装。"""
        class 结果:
            returncode = 128
            stdout = ""
        with mock.patch.object(image.shutil, "which", lambda _: "/usr/bin/git"), \
             mock.patch.object(image.subprocess, "run", lambda *a, **k: 结果()):
            self.assertEqual(image._revision(), "")


class Test组装(会报错的混入, unittest.TestCase):

    def _跑(self, flavor="base", 导入的码=0, stderr="", **kw):
        """跑一遍 assemble()：docker 和装根全换成假的。返回 (镜像引用, 记录)。"""
        记录: dict = {}

        def 假的_build(name, arch, repo, **bkw):
            记录["build"] = dict(name=name, arch=arch, repo=repo, **bkw)
            return Path("/tmp/var/base/base-aarch64.tar.gz")

        def 假的_run(cmd, **_kw):
            记录["run"] = cmd

        def 假的_subprocess_run(cmd, **_kw):
            记录["import"] = cmd
            return type("结果", (), {"returncode": 导入的码, "stdout": "sha256:…",
                                 "stderr": stderr})()

        # stdout 和 stderr 都收：`die()` 往 stderr 说，而失败的话都在那儿
        out = io.StringIO()
        with mock.patch.object(image.root, "build", 假的_build), \
             mock.patch.object(image, "run", 假的_run), \
             mock.patch.object(image.subprocess, "run", 假的_subprocess_run), \
             mock.patch.object(image, "_revision", lambda: "b" * 40), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                ref = image.assemble(flavor, "aarch64", "http://仓库", **kw)
            except SystemExit as e:
                ref = f"[退出码 {e.code}]"
        return ref, 记录, out.getvalue()

    def test_装根那一步就是_rootfs_那一份(self):
        """★ 这块逻辑没有第二个出处：assemble 调 root.build()，
        而不是又写一遍 apk add --root。"""
        _, 记录, _ = self._跑()
        self.assertEqual(记录["build"]["name"], "base")
        self.assertEqual(记录["build"]["arch"], "aarch64")
        self.assertEqual(记录["build"]["repo"], "http://仓库")

    def test_不暴露_skip_missing(self):
        """`--skip-missing` 是给"先看看剩下的部分对不对"用的（rootfs 那边），
        而这条路的产物是【交付物】——故意不完整的镜像不该从这里出去。"""
        _, 记录, _ = self._跑()
        self.assertNotIn("skip_missing", 记录["build"])

    def test_导出的镜像名和_tag(self):
        ref, 记录, _ = self._跑(tag="2026.10.15")
        self.assertEqual(ref, "yojigenos/base:2026.10.15")
        self.assertEqual(记录["import"][-1], "yojigenos/base:2026.10.15")
        self.assertEqual(记录["import"][-2], "/tmp/var/base/base-aarch64.tar.gz")

    def test_可以换镜像名(self):
        ref, _, _ = self._跑(image_name="yojigenos/试验")
        self.assertEqual(ref, "yojigenos/试验:edge")

    def test_不给_tag_就是_edge(self):
        ref, _, _ = self._跑(tag="")
        self.assertEqual(ref, "yojigenos/base:edge")

    def test_不给_platform(self):
        """★ docker import 出来的架构由【本机 daemon】决定——跨架构打标签
        会让它在别的机器上 exec format error。"""
        _, 记录, _ = self._跑()
        self.assertNotIn("--platform", 记录["import"])

    def test_导入失败要说清_docker_说了什么(self):
        """shell 版那句 `> /dev/null` 把成功时的 sha256 丢了（对的），
        但失败时也必须让人看见 docker 的原话——否则只剩一句"失败"。"""
        ref, _, 文本 = self._跑(导入的码=1, stderr="invalid reference format")
        self.assertEqual(ref, "[退出码 1]")
        self.assertIn("invalid reference format", 文本)

    def test_报告里给的三条命令都对得上这个镜像(self):
        """给人抄的命令必须能跑：从前那句教人用 /usr/bin/bash，而 base 不带 bash。"""
        ref, _, 文本 = self._跑()
        for line in 文本.splitlines():
            if "docker run" in line:
                with self.subTest(line=line.strip()):
                    self.assertIn(ref, line)
        self.assertNotIn("bash", 文本)
        self.assertTrue(re.search(r"docker run -it --rm .*# 进去看看", 文本))



class Test标签的写和查不许分叉(unittest.TestCase):
    """★ 这是这次真正的修法。

    2026-10-15 之前：`image.py` 写九个标签、`verify.py` 查七个，**两张清单各写一份**。
    漏掉的那两个（`image.url`、`cn.yojigen.os.built`）可以悄悄消失而没人知道 ——
    而 `verify.py` 自己的注释里当时就写着「一个"计划了但没写进去"的标签，和"写了但
    写错"的标签一样糟：外面的人看不到区别」。

    现在清单只有一份（`common.LABELS`），所以分叉在结构上不可能。这两条测试守的就是
    那个结构：**两边拿到的是同一个对象**，以及**生成出来的 LABEL 恰好覆盖它**。
    """

    def test_写和查用的是同一张清单(self):
        self.assertIs(image.LABELS, verify.LABELS)
        self.assertIs(image.LABELS, common.LABELS)

    def test_生成出来的_LABEL_恰好覆盖那张清单(self):
        """不多不少。

        少了 = 有标签没人验（原来就是这样：写九个、查七个）；
        多了 = 有标签不在清单里 —— 那它是什么、该不该在，就没人知道了。
        """
        args = image.import_args(name="base", arch="aarch64", tag="edge",
                                 built="2026-01-01T00:00:00Z", revision="a" * 40)
        写了 = sorted(a[len("LABEL "):].split("=", 1)[0]
                     for a in args if a.startswith("LABEL "))
        self.assertEqual(写了, sorted(common.LABELS))

    def test_值里不能有空字符串(self):
        """清单里每一个都真的填了值。

        `LABEL k=` 那种在 docker 里是合法的空值，而 verify 只查「非空」，
        所以它会当成缺了。这种错要在【写】的那一头拦住。
        """
        args = image.import_args(name="base", arch="aarch64", tag="edge",
                                 built="2026-01-01T00:00:00Z", revision="a" * 40)
        for a in args:
            if not a.startswith("LABEL "):
                continue
            with self.subTest(label=a):
                k, _, v = a[len("LABEL "):].partition("=")
                self.assertTrue(v.strip(), f"标签 {k} 的值是空的")


if __name__ == "__main__":
    unittest.main()
