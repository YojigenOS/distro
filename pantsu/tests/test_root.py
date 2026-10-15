#!/usr/bin/env python3
"""root.py —— 装根那一步里【不碰 docker、不碰网络】的那些逻辑。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_root.py              # 单独跑也行

【为什么值得测】root.py 是整个 pantsu 里最"重"的一段：它在一个借来的容器里
跑 apk 装出一个根，再打成 tar。真正跑一次要 docker + 一个能拉到的仓库，所以
下面验的是**它交给容器的那些输入**：

  · `MISSING_RE` —— 从 apk 的报错里认出【哪些包不在仓库里】，好给人一句人话。
    它认错了不会报错，只会把 apk 的原始输出原样倒出来（或者认出错的包名）
  · 容器脚本里的**确定性四件套**（0005）：少一个，同一个输入会建出不同的产物
  · 公钥是**先放进去再装**的——顺序反了第一个包就验不了签
  · `BUILD_DATE` 从固定的 `TAR_MTIME` 推出来，不是"现在"

最后一条特别值得钉住：它和"忘了写 --pax-option"是同一种病——**看得见的地方
都对，只有产物的哈希不一样**。
"""

from __future__ import annotations

import contextlib
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

from pantsulib import common, flavor, root  # noqa: E402


class Test缺包识别(unittest.TestCase):
    """"仓库里没有这个包"这件事，apk 是这么说的——而这里是唯一听得懂它的地方。"""

    #: 真实形状（apk 3.0）。缩进和括号都是它自己打的。
    真实输出 = """\
fetch https://repo.os.yojigen.cn/aarch64/APKINDEX.tar.gz
(1/12) Installing filesystem (1-1)
ERROR: unable to select packages:
  linux (no such package):
    required by: world[linux]
  yojigen-os-init (no such package):
    required by: world[yojigen-os-init]
"""

    def test_认得出缺的是哪几个包(self):
        self.assertEqual(root.MISSING_RE.findall(self.真实输出),
                         ["linux", "yojigen-os-init"])

    def test_包名后面不带冒号(self):
        """认出来的名字要能直接拿去从包列表里删（build() 就是这么用的）。

        多一个冒号，删不掉的那个包会留在列表里，重试时再失败一次——
        而报错看起来和第一次一模一样。
        """
        for name in root.MISSING_RE.findall(self.真实输出):
            self.assertFalse(name.endswith(":"), f"{name!r} 带着冒号")

    def test_不会把别的话认成包名(self):
        """`required by: world[linux]` 里也有 linux，但它不是一条缺包记录。

        认错了会把一个装得上的包从列表里删掉，然后重试"成功"——而那个根里
        少了一个包，没人会知道。
        """
        self.assertNotIn("world", root.MISSING_RE.findall(self.真实输出))
        self.assertNotIn("ERROR:", root.MISSING_RE.findall(self.真实输出))
        # 一行里位于句中的 (no such package)：apk 的输出里没有这种形状，
        # 而正则要求它紧跟在行首那个词后面——所以不该匹配
        self.assertEqual(root.MISSING_RE.findall("ERROR: linux (no such package)\n"), [])


class Test容器脚本的不变量(会报错的混入, unittest.TestCase):
    """★ 这段脚本是【在容器里】跑的，宿主上跑不了它（没有 apk、没有 root）。

    所以这里验的是它的**不变量**：那几条一旦丢了，症状都是"看起来成功了"。
    """

    def test_tar_的确定性四件套都在(self):
        """packages 的决策记录 0005：排序 + 固定属主 + 固定时间戳 + 掐掉 PAX 头。

        ★ `--pax-option` 那一条是搬家时补上的：原来 assemble.sh 那份没有它，
          而同一个仓库里的 rootfs.py.待改 有。0005 要的东西不该只做一半——
          少它不会报错，只会让 tar 的哈希在两次相同的输入下不一样。
        """
        for flag in ("--sort=name", "--numeric-owner", "--owner=0", "--group=0",
                     '--mtime="$TAR_MTIME"',
                     "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime"):
            with self.subTest(flag=flag):
                self.assertIn(flag, root.CONTAINER_SCRIPT)

    def test_真的验签_不是_allow_untrusted(self):
        """信任从哪来是一个要回答的问题，而答案是"交付物自己的构建输入"。

        ★ 只在**真会执行的那些行**上查（注释里提到 --allow-untrusted 是解释
          "为什么不用它"，那不是它的用法）。
        """
        命令行 = [ln for ln in root.CONTAINER_SCRIPT.splitlines()
               if not ln.lstrip().startswith("#")]
        self.assertIn("cp /keys/*.rsa.pub", root.CONTAINER_SCRIPT)
        self.assertIn("apk add --keys-dir /newroot/etc/apk/keys", root.CONTAINER_SCRIPT)
        self.assertNotIn("--allow-untrusted", "\n".join(命令行))

    def test_公钥先放进去_再装东西(self):
        """顺序是必须的：apk 装第一个包时就要验签，而验签要公钥。

        不能等 filesystem 包（它自己也得验签）带进来——那是鸡生蛋。
        顺序写反的表现是第一个包就 no such key。
        """
        放钥匙 = root.CONTAINER_SCRIPT.index("cp /keys/*.rsa.pub")
        装东西 = root.CONTAINER_SCRIPT.index("apk add --keys-dir")
        self.assertLess(放钥匙, 装东西)

    def test_仓库地址和_interactive_写进交付物自己(self):
        """交付物【自己说得清它是从哪来的】——零成本，而且以后 apk 用得上。"""
        self.assertIn("/newroot/etc/apk/repositories", root.CONTAINER_SCRIPT)
        self.assertIn("interactive yes", root.CONTAINER_SCRIPT)
        self.assertIn("/newroot/etc/apk/config", root.CONTAINER_SCRIPT)

    def test_组成清单用的是固定日期(self):
        """清单里写"生成于 $BUILD_DATE"——而 $BUILD_DATE 从 TAR_MTIME 推。

        写"现在"的话，同一份输入建出两个不同的根：0005 的东西在这里又破一次。
        """
        行 = root.CONTAINER_SCRIPT.splitlines()
        写入 = next(i for i, ln in enumerate(行)
                  if ln.lstrip().startswith("} >") and "yojigen-os-manifest" in ln)
        这一段 = "\n".join(行[max(0, 写入 - 10):写入])
        self.assertIn("$BUILD_DATE", 这一段)
        self.assertNotIn("$(date", 这一段)      # 不能用"现在"

    def test_装完把属主还给宿主(self):
        """否则宿主用户删不掉 var/<风味>/rootfs-<架构>/（容器里写的是 root 的）。

        macOS 上 Docker Desktop 会把它虚拟化成宿主用户，所以本地看不出来——
        CI 上会。**只在一条路上看得见的问题，就是要在另一条路上写下来。**
        """
        self.assertIn('chown -R "$HOST_UID:$HOST_GID" /newroot', root.CONTAINER_SCRIPT)

    def test_用_GNU_tar_不用_BusyBox_的(self):
        """Alpine 自带的 tar 是 BusyBox 的：不认 --sort=name / --mtime / --pax-option。

        不装 GNU tar 的话，上面那几条"确定性四件套"会**静默失效**（或者 tar
        直接报非法参数），而产物看起来是正常的。
        """
        self.assertIn("apk add --no-cache tar", root.CONTAINER_SCRIPT)


class Test公钥(会报错的混入, unittest.TestCase):
    """`_keys()` 报错时用的是**相对仓库的**路径，所以换 KEYS_DIR 时要连着 ROOT 一起换。

    （这是它现在的一个前提：KEYS_DIR 在仓库里。真实运行时成立。）
    """

    def test_按名字排序返回所有公钥(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "b.rsa.pub").write_text("b")
            (d / "a.rsa.pub").write_text("a")
            (d / "README.md").write_text("不是公钥")     # 真实目录里就有这个文件
            with mock.patch.object(root, "KEYS_DIR", d):
                got = root._keys()
        self.assertEqual([p.name for p in got], ["a.rsa.pub", "b.rsa.pub"])

    def test_目录不在要报错_并说要先跑什么(self):
        with mock.patch.object(root, "KEYS_DIR", Path("/没有这个目录")), \
             mock.patch.object(root, "ROOT", Path("/")):
            msg = self.应当报错(root._keys)
        self.assertIn("keys.sh", msg)      # 该去哪儿生成
        self.assertIn("没有这个目录", msg)

    def test_一个公钥都没有要报错(self):
        """空目录比缺目录更隐蔽：目录在、看着正常，装到第一个包才炸。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(root, "KEYS_DIR", Path(tmp)), \
                 mock.patch.object(root, "ROOT", Path("/")):
                msg = self.应当报错(root._keys)
        self.assertIn("rsa.pub", msg)

    def test_真实的_keys_目录拿得出公钥(self):
        """仓库里那份是真的要烤进交付物的——它得非空。"""
        keys = root._keys()
        self.assertTrue(keys, "keys/ 里一个 *.rsa.pub 都没有")


class Test探仓库(会报错的混入, unittest.TestCase):
    """`_check_repo` 是装根的第一道门：仓库拿不到的话，apk 的报错要绕一圈才说清楚。

    ★ 这里把 curl 换成假的：真去连一次的话，测试就依赖网络和线上仓库——
      "测试通过"会变成"这个时刻网通"。探的逻辑（探哪个 URL、失败说什么）
      和网络连通性无关，那就不该被它左右。
    """

    def _探(self, repo: str, arch: str, rc: int = 0) -> tuple[list[str], str]:
        """跑一次 _check_repo，返回 (交给 curl 的命令, 失败时的报错文本)。"""
        seen: list[list[str]] = []

        class 结果:
            returncode = rc

        def 假的_run(cmd, **_kw):
            seen.append(cmd)
            return 结果()

        err = ""
        with mock.patch.object(root, "subprocess", mock.MagicMock(run=假的_run)):
            if rc == 0:
                root._check_repo(repo, arch)
            else:
                err = self.应当报错(root._check_repo, repo, arch)
        return seen[0], err

    def test_探的是_APKINDEX(self):
        cmd, _ = self._探("https://repo.os.yojigen.cn", "aarch64")
        self.assertEqual(cmd[0], "curl")
        self.assertEqual(cmd[-1], "https://repo.os.yojigen.cn/aarch64/APKINDEX.tar.gz")
        # 探测要快，而且不能把索引下下来——只要一个 HTTP 码
        self.assertIn("-o", cmd)
        self.assertIn(os.devnull, cmd)
        self.assertIn("--max-time", cmd)

    def test_把_host_docker_internal_换回_localhost(self):
        """★ 这个检查跑在【宿主】上，而 host.docker.internal 只在容器里有意义。

        不换的话本地调试永远"仓库拿不到"——而同一个地址在容器里是通的，
        看起来像"仓库坏了"。
        """
        cmd, _ = self._探(common.LOCAL_REPO, "x86_64")
        self.assertEqual(cmd[-1], "http://localhost:18080/x86_64/APKINDEX.tar.gz")

    def test_拿不到就说清楚_并给一条本地的路(self):
        _, msg = self._探(common.DEFAULT_REPO, "aarch64", rc=1)
        self.assertIn("APKINDEX.tar.gz", msg)
        self.assertIn("http.server 18080", msg)      # 本地起一个
        self.assertIn(common.LOCAL_REPO, msg)        # 然后用它

    def test_x86_64_额外提示一句_它可能只是还没发布(self):
        """x86_64 的仓库到现在还空着——这时候"仓库拿不到"最常见的真因不是网络。"""
        _, msg = self._探(common.DEFAULT_REPO, "x86_64", rc=1)
        self.assertIn("x86_64", msg)
        self.assertIn("release", msg)
        # 这条提示不该出现在 aarch64 上（那边仓库是有的）
        _, msg = self._探(common.DEFAULT_REPO, "aarch64", rc=1)
        self.assertNotIn("release workflow", msg)


class Test交给容器的输入(unittest.TestCase):

    def _装(self, pkgs, repo="http://host.docker.internal:18080", name="base",
            arch="aarch64"):
        """跑 _install，返回它交给 docker 的那条命令。"""
        seen: dict[str, list[str]] = {}

        def 假的_run_streaming(cmd, **_kw):
            seen["cmd"] = cmd
            return (0, "")

        with mock.patch.object(root, "run_streaming", 假的_run_streaming):
            rc, _ = root._install(list(pkgs), repo, Path("/tmp/根"), Path("/tmp/出"),
                                  f"{name}-{arch}.tar.gz", name, arch)
        self.assertEqual(rc, 0)
        return seen["cmd"]

    def test_三个挂载_一个都不少(self):
        cmd = self._装(["a"])
        for 挂载 in ("/tmp/出:/out", "/tmp/根:/newroot", f"{common.KEYS_DIR}:/keys:ro"):
            with self.subTest(挂载=挂载):
                self.assertIn(挂载, cmd)
        # 公钥是只读挂进去的——容器没有任何理由改它
        self.assertIn(f"{common.KEYS_DIR}:/keys:ro", cmd)

    def test_包列表和仓库走环境变量_不在字符串里拼(self):
        """省掉一整套转义问题：包名、仓库 URL 里出现引号或空格都不会串味。"""
        cmd = self._装(["filesystem", "glibc", "yojigen-os-init"])
        self.assertIn("PKGS=filesystem glibc yojigen-os-init", cmd)
        self.assertIn("REPO=http://host.docker.internal:18080", cmd)
        self.assertIn("TAR_NAME=base-aarch64.tar.gz", cmd)
        # 脚本本身是原样传的（不是拼出来的）
        self.assertEqual(cmd[-4:], ["alpine:latest", "sh", "-c", root.CONTAINER_SCRIPT])

    def test_清单要的四样都传了(self):
        cmd = self._装(["a"], name="minimal", arch="x86_64")
        for 项 in ("FLAVOR=minimal", "ARCH=x86_64"):
            with self.subTest(项=项):
                self.assertIn(项, cmd)

    def test_BUILD_DATE_从_TAR_MTIME_推出来_不是现在(self):
        """★ 同一份输入必须建出同一个根（0005）。

        写 `now()` 的话，两个小时的 CI 里同一个风味会产出两个不同的根——
        而两边的"成功"看不出区别。
        """
        cmd = self._装(["a"])
        self.assertIn("BUILD_DATE=2026-01-01 00:00 UTC", cmd)

        # 两次调用必须是同一个值（用"现在"的话，跨秒就会不一样）
        self.assertEqual(
            [c for c in self._装(["a"]) if c.startswith("BUILD_DATE=")],
            [c for c in self._装(["a"]) if c.startswith("BUILD_DATE=")])

    def test_不给_platform(self):
        """★ 组装【不执行】被装的二进制：apk --root 只是解包。

        所以 aarch64 的宿主上装 x86_64 的包是安全的——前提是**我们打的包里
        没有 install script**（apk 会 chroot 进去执行它，那就得把 qemu 拉进来）。
        这个前提写在这里，是因为哪天它不成立了，症状会是"装到某个包就卡死"。
        """
        cmd = self._装(["a"], arch="x86_64")
        self.assertNotIn("--platform", cmd)

    def test_属主用宿主自己的_不写死(self):
        """CI 上 uid 是 1001，开发者本机是别的——写死会让另一侧删不掉产物。"""
        cmd = self._装(["a"])
        self.assertIn(f"HOST_UID={os.getuid()}", cmd)
        self.assertIn(f"HOST_GID={os.getgid()}", cmd)

    def test_容器连宿主要走_add_host(self):
        """容器里的 localhost 是容器自己——本地仓库那条路全靠这一行。"""
        cmd = self._装(["a"])
        self.assertIn("--add-host=host.docker.internal:host-gateway", cmd)


class Test装根的流程(会报错的混入, unittest.TestCase):
    """`build()` 里除了"起容器"之外的那些（探仓库、公钥、缺包重试）。

    这一格验的是**卡住的时候人看到什么**：装根失败最费时间的不是失败本身，
    而是那句报错没告诉你下一步该干什么。
    """

    def _跑(self, 第一次的输出: str, skip_missing: bool = False) -> dict:
        """跑 build()：docker / curl / 公钥全换掉，返回调用记录。"""
        out = io.StringIO()
        calls: dict = {"pkgs": []}

        def 假的_install(pkgs, repo, root_dir, out_dir, tar_name, name, arch):
            calls["pkgs"].append(list(pkgs))
            if len(calls["pkgs"]) == 1:
                return (1, 第一次的输出)
            (out_dir / tar_name).write_bytes("假装这是 tar".encode())  # 第二次当它装成了
            return (0, "装好了\n")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(root, "VAR_DIR", Path(tmp)), \
             mock.patch.object(root, "ROOT", Path(tmp)), \
             mock.patch.object(root, "_check_repo", lambda *_: None), \
             mock.patch.object(root, "_keys", lambda: [Path("k.rsa.pub")]), \
             mock.patch.object(root, "_install", 假的_install), \
             安静(), contextlib.redirect_stdout(out):
            if skip_missing:
                calls["tar"] = root.build("base", "aarch64", "http://仓库.example",
                                          skip_missing=True)
            else:
                calls["错误"] = self.应当报错(root.build, "base", "aarch64",
                                        "http://仓库.example")
        calls["输出"] = out.getvalue()
        return calls

    def test_缺包时_skip_missing_真的重试_并且去掉那些包(self):
        """★ 重试要验的是**第二次拿到的是什么**：缺的包必须【真的从列表里去掉】。

        去掉的写错了（比如去掉了一个不存在的包名）不会报错，第二次还是失败，
        而报错和第一次一模一样——看着像 apk 的毛病。
        """
        r = self._跑("ERROR: unable to select packages:\n"
                  "  gawk (no such package):\n"
                  "    required by: world[gawk]\n", skip_missing=True)

        全部 = list(flavor.load("base")["packages"])
        self.assertIn("gawk", 全部, "这条测试用的缺包得是 base 里真有的")
        self.assertEqual(r["pkgs"][0], 全部)
        self.assertEqual(r["pkgs"][1], [p for p in 全部 if p != "gawk"])
        self.assertIn("gawk", r["输出"])        # 大声报出来
        # 返回的是 tar 那条路径（临时目录一出 with 就没了，所以只验名字）
        self.assertEqual(r["tar"].name, "base-aarch64.tar.gz")

    def test_缺包时_不跳过就说清楚两种可能(self):
        """apk 说"没有这个包"时，真因只有两种：没发布，或者还没配方。

        只说"装根失败"的话，人得自己去猜是哪一种——而这两种的下一步
        完全不同（一个去跑 release workflow，一个去写配方）。
        """
        r = self._跑("ERROR: unable to select packages:\n"
                  "  gawk (no such package):\n"
                  "    required by: world[gawk]\n")
        self.assertIn("gawk", r["错误"])
        self.assertIn("release", r["错误"])          # 包建了没发布
        self.assertIn("配方", r["错误"])             # 压根还没写
        self.assertIn("--skip-missing", r["错误"])   # 过渡期怎么先跑通
        self.assertEqual(len(r["pkgs"]), 1, "不跳过就不该重试")

    def test_不是缺包的失败_不瞎猜(self):
        """别的失败（网络断了、磁盘满了）要说"完整输出见上面"，而不是
        把两种可能硬套上去——套错了会把人引到 release workflow 上去。"""
        r = self._跑("fetch bad: connection refused\n")
        self.assertIn("完整输出见上面", r["错误"])
        self.assertNotIn("release", r["错误"])


class Test没测到的(unittest.TestCase):
    """★ 这一格是【缺口】，不是覆盖。

    写在这里是为了让"没测到什么"是一个摆明的事实，而不是一个没人提起的空白。
    """

    @unittest.skipUnless(os.environ.get("PANSU_INTEGRATION"),
                         "要 docker 在跑 + 真拉一个 apk 仓库："
                         "本机想跑就 PANSU_INTEGRATION=1"
                         "（它会往 var/ 里写真实的产物，所以默认不跑）")
    def test_装根到打tar(self):
        """端到端的那一半：在容器里 apk add --root → GNU tar 打包。

        单元测试里验的是"交给容器的输入对不对"；"容器里到底装出了什么"
        只有真跑一次才知道（那也正是缺包报错和 --skip-missing 存在的理由）。
        """
        tar = root.build("base", "aarch64", common.DEFAULT_REPO)
        self.assertTrue(tar.is_file())


if __name__ == "__main__":
    unittest.main()
