#!/usr/bin/env python3
"""verify.py —— 验交付物的那套检查（宿主那一层 + 喂进镜像的那段 payload）。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_verify.py            # 单独跑也行

【为什么值得测】它是"能不能推出去"的那道门，所以两个方向都要验：

  · **好镜像要被放行** —— 拦严了会把正常的镜像挡在门外，而修法通常是
    "把验证关掉"，那比不验更糟
  · **坏镜像要被拦下、而且要说清哪一样不对** —— 说错了会把人往错的方向查
    （"没有默认 CMD"和"标签缺了"是两种完全不同的坏法）

docker 全换成假的：这几条验的是判据，不是 docker 到底会不会。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent          # distro/pantsu
sys.path.insert(0, str(ROOT))

from support import 安静, 会报错的混入  # noqa: E402

from pantsulib import verify  # noqa: E402


def _仓库根的_bashisms() -> list:
    """那份 bashism 名单住在【仓库根的 tests/test_sh_scripts.py】——唯一出处。

    ★ 为什么这里要跨目录去取，而不是再抄一份：那段 payload 现在住在
      `verify.py` 的字符串里，**没有文件名**，按 shebang 扫描的那些检查看不见
      它 —— 所以要在这里单独扫一遍。而"单独扫一遍"和"单独列一份名单"是两件
      事：抄一份的结果是这边少了四条（`${VAR/a/b}` · `(( ))` · `${a[@]}` ·
      `$'…'`），而 **IN_IMAGE_SCRIPT 要拿去只有 dash 的镜像里跑**，少的这四条
      恰好是那里最危险的。

    ★ 那个文件不是包（`tests/` 下没有 `__init__.py`），所以按【路径】加载，
      不往 sys.path 里塞第二个 `tests` —— 那个名字太通用，容易被别的东西占。
    """
    path = ROOT.parent / "tests" / "test_sh_scripts.py"
    spec = importlib.util.spec_from_file_location("_root_test_sh_scripts", path)
    assert spec and spec.loader, f"加载不了 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BASHISMS


#: (正则, 名字, 为什么) —— 形状跟着仓库根那份走，这里【不】改它。
BASHISMS = _仓库根的_bashisms()


def 假_inspect(标签: dict | None = None, cmd: str = '["/bin/sh"]', 存在: bool = True):
    """造一个假的 `docker image inspect`。

    按 --format 的模板回答——模板长什么样，代码就是怎么问的。
    """
    def 问(image: str, template: str) -> str:
        if template == "{{.Id}}":
            return "sha256:abc" if 存在 else ""
        if template == "{{.Config.Cmd}}":
            return cmd
        m = re.search(r'Labels "([^"]+)"', template)
        assert m, f"不认识的模板：{template}"
        return (标签 or {}).get(m.group(1), "")
    return 问


def 全部标签(**覆盖) -> dict:
    """清单里【每一个】标签都给一个非空值。

    ★ 个数不写死在这里：它跟着 `verify.LABELS` 走（那是唯一出处）。
      原来这位 helper 叫「七个标签」，而那份清单早就是九个了 —— 名字和它
      返回的东西对不上，本身就让人少查两个。见下面 Test标签名单。
    """
    d = {k: "有值" for k in verify.LABELS}
    d.update(覆盖)
    return d


class Test镜像里那段脚本(unittest.TestCase):
    """那段 payload 要被喂进【别的镜像】跑——里面可能只有 dash。"""

    def test_是_POSIX_sh_没有_bash_扩展(self):
        """名单来自仓库根的 `tests/test_sh_scripts.py`（见上面那个 helper）。"""
        found = []
        for i, line in enumerate(verify.IN_IMAGE_SCRIPT.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for pat, name, why in BASHISMS:
                if pat.search(line):
                    found.append(f"{i}: {name}（{why}）—— {line.strip()}")
        self.assertEqual(found, [],
                         "镜像里那段脚本用了 bash 专有写法（被验证的镜像里可能只有 dash）：\n  "
                         + "\n  ".join(found))

    def test_四项检查一样不少(self):
        """这四样是这个交付物【承诺】的东西。少一项，验证就少一分意义。

        ① /bin/sh 是不是 POSIX 的那个（写 #!/bin/sh 的脚本的行为底线）
        ② 常用命令在不在
        ③ 中文（这个项目的核心承诺）
        ④ 动态链接解析得了吗（gawk 少 libmpfr 那次就是这类）
        """
        for 标题 in ("/bin/sh 是谁", "常用命令都在吗", "中文（核心承诺）", "动态链接"):
            with self.subTest(标题=标题):
                self.assertIn(标题, verify.IN_IMAGE_SCRIPT)
        self.assertIn("ldd", verify.IN_IMAGE_SCRIPT)
        self.assertIn("not found", verify.IN_IMAGE_SCRIPT)

    def test_中文那条真的做一次替换(self):
        """"支持中文"不能只是"locale 在"——得真拿中文数据走一遍 sed。

        （locale 装了但 sed 处理多字节出错，是这两件事分开的经典例子。）
        """
        self.assertIn("sed 's/北京/上海/'", verify.IN_IMAGE_SCRIPT)
        self.assertIn('[ "$S" = "我爱上海" ]', verify.IN_IMAGE_SCRIPT)

    def test_退出码是失败项数(self):
        self.assertIn("FAIL=0", verify.IN_IMAGE_SCRIPT)
        self.assertIn('exit "$FAIL"', verify.IN_IMAGE_SCRIPT)


class Test标签名单(unittest.TestCase):

    def test_九个标签就是_0003_说的那组(self):
        """★ 验的是【存在且非空】，不是具体值——时间戳和 commit 每次都不同。

        名单短一个，"计划了但没写进去"的标签就永远没人发现；而它和"写了但写错"
        对外面的人来说一样糟。见决策记录 0003「二」。

        ★ 这条测试原来叫「七个标签」并写死了那七个 —— 而当时 image.py 写的是九个、
        `docs/记录-第一个能用的交付物.md` 说的也是九个。**所以那条测试在守一个错的
        数**：它让"少了两个"看起来是被保证的。2026-10-15 改成九。

        ★ 这九个名字是【故意抄在这里的】，别改成从 `verify.LABELS` 读数：
        这就是那一族测试里唯一一处"把合同打出来"的地方，从 `verify.LABELS`
        读出来再和它自己比就成了同义反复 —— 加一个、删一个都不会红。
        （反过来，问"个数对不对"的那两条在 `test_image.py` 里，那两条从
        `common.LABELS` 读数才是对的。）
        """
        self.assertEqual(set(verify.LABELS), {
            "org.opencontainers.image.title",
            "org.opencontainers.image.version",
            "org.opencontainers.image.created",
            "org.opencontainers.image.source",
            "org.opencontainers.image.url",
            "org.opencontainers.image.revision",
            "cn.yojigen.os.flavor",
            "cn.yojigen.os.arch",
            "cn.yojigen.os.built",
        })

    def test_用的是我们真拥有的域名(self):
        """命名空间是反写域名，所以根必须是我们的域名。

        原来是 `org.yojigenos.…`——那声明的是 yojigenos.org，不是我们的。
        """
        self.assertEqual([k for k in verify.LABELS if k.startswith("cn.yojigen.")],
                         ["cn.yojigen.os.flavor", "cn.yojigen.os.arch",
                          "cn.yojigen.os.built"])


class Test问_docker(会报错的混入, unittest.TestCase):

    def test_拿得到就返回去掉空白的值(self):
        class 结果:
            returncode = 0
            stdout = "  有值\n"
        with mock.patch.object(verify.subprocess, "run", lambda *a, **k: 结果()):
            self.assertEqual(verify._inspect("x", "{{.Id}}"), "有值")

    def test_拿不到就返回空串(self):
        """镜像不存在、标签没写——都是"没有"，而调用方要能一起判断。

        （和 shell 里那句 `2>/dev/null` 同一个意思。）
        """
        class 结果:
            returncode = 1
            stdout = ""
        with mock.patch.object(verify.subprocess, "run", lambda *a, **k: 结果()):
            self.assertEqual(verify._inspect("x", "{{.Id}}"), "")


class Test验证流程(会报错的混入, unittest.TestCase):

    def _跑(self, 标签=None, cmd='["/bin/sh"]', 存在=True, flavor=None,
            起得来=True, 镜像里=(0, ""), 镜像里不该被调用=False):
        """跑一遍 verify()，把 docker 全换成假的。

        返回它打印的东西——**stdout 和 stderr 都要**：`die()` 走 stderr，
        而失败的那些话正好都在那里。
        """
        out = io.StringIO()

        def 假的_run(cmd_list, **_kw):
            return type("结果", (), {"returncode": 0 if 起得来 else 125,
                                 "stdout": "", "stderr": ""})()

        def 假的_run_streaming(cmd_list, **_kw):
            if 镜像里不该被调用:
                raise AssertionError("镜像起不来还往后跑了一步")
            return 镜像里

        with mock.patch.object(verify, "_inspect", 假_inspect(标签, cmd, 存在)), \
             mock.patch.object(verify.subprocess, "run", 假的_run), \
             mock.patch.object(verify, "run_streaming", 假的_run_streaming), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                verify.verify("yojigenos/base:edge", flavor)
            except SystemExit as e:
                out.write(f"\n[退出码 {e.code}]")
        return out.getvalue()

    def test_好镜像放行(self):
        text = self._跑(标签=全部标签())
        self.assertIn("验证通过", text)
        self.assertNotIn("[退出码", text)

    def test_没有这个镜像就直说(self):
        """★ 比 shell 版多做的一件事。

        原来镜像不存在也能"跑完"：CMD 空、九个标签全缺、最后起不来——四行
        报错都在说同一件事，而没一行说【真原因】。
        """
        text = self._跑(存在=False)
        self.assertIn("没有这个镜像", text)
        self.assertIn("[退出码 1]", text)

    def test_没有默认_CMD_要拦下(self):
        """docker import 不像 Dockerfile 那样有默认 CMD——实测撞过。"""
        text = self._跑(标签=全部标签(), cmd="[]")
        self.assertIn("没有默认 CMD", text)
        self.assertIn("no command specified", text)
        self.assertIn("[退出码 1]", text)

    def test_风味标签对不上要说传错镜像了(self):
        """--flavor 的全部意义：它挡住的是"验了另一个镜像"这种错。"""
        text = self._跑(标签=全部标签(**{"cn.yojigen.os.flavor": "minimal"}),
                     flavor="base")
        self.assertIn("传错镜像", text)
        self.assertIn("[退出码 1]", text)

    def test_缺标签要说缺了哪几个(self):
        text = self._跑(标签={k: "有值" for k in verify.LABELS
                          if k != "org.opencontainers.image.revision"})
        self.assertIn("org.opencontainers.image.revision", text)

    def test_起不来就别往后跑(self):
        """后面那些检查要进镜像里跑——起不来就没有"后面"。"""
        text = self._跑(标签=全部标签(), 起得来=False, 镜像里不该被调用=True)
        self.assertIn("镜像起不来", text)
        self.assertIn("没有意义", text)
        self.assertIn("[退出码 1]", text)

    def test_镜像里没过也要退出_1(self):
        """★ 两个数字都要报出来：元数据的问题和镜像里的问题是两处，
        合在一起看才知道该先修哪个。"""
        text = self._跑(标签=全部标签(), 镜像里=(2, ""))
        self.assertIn("镜像内部退出码 2", text)
        self.assertIn("镜像元数据 0 项", text)
        self.assertIn("[退出码 1]", text)

    def test_镜像里那段脚本是原样喂进去的(self):
        """`docker run --rm -i <镜像>` 不带命令：靠镜像自己的 CMD 从 stdin 读。

        所以这一条命令同时验证了「默认 CMD 是对的」和「镜像里的用户态能用」。
        """
        记录 = {}

        def 假的_run_streaming(cmd_list, **kw):
            记录["cmd"] = cmd_list
            记录.update(kw)
            return (0, "")

        with mock.patch.object(verify, "_inspect", 假_inspect(全部标签())), \
             mock.patch.object(verify.subprocess, "run",
                            lambda *a, **k: type("结果", (), {"returncode": 0})()), \
             mock.patch.object(verify, "run_streaming", 假的_run_streaming), \
             安静():
            verify.verify("yojigenos/base:edge")
        self.assertEqual(记录["cmd"], ["docker", "run", "--rm", "-i", "yojigenos/base:edge"])
        self.assertEqual(记录["stdin_text"], verify.IN_IMAGE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
