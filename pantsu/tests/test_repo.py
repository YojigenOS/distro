#!/usr/bin/env python3
"""repo.py —— 把镜像放进仓库那一步里【不联网、不传东西】的那些逻辑。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_repo.py              # 单独跑也行

【为什么值得测】"发布"是唯一一个**做错了别人会拿到坏东西**的动作：

  · 交付物必须不可变（0013）：文件名里带日期，latest.txt 只写一行文件名
  · 干跑要真的只是干跑（没凭据 / 没加 --go 就不许传）
  · 推文件那一组参数（S3 分段上传、逐变量 -e）——它们在 packages 的
    `skirt deploy`（`packages/skirt/skirtlib/deploy.py`；2026-10-15 之前那个
    文件叫 `packages/scripts/deploy.sh`）里已经踩过一遍，这里验的是
    "同一个仓库用同一套参数"
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import importlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent          # distro/pantsu
sys.path.insert(0, str(ROOT))

from support import 安静, 会报错的混入  # noqa: E402

from pantsulib import repo  # noqa: E402

#: packages 那边推同一个仓库的那份实现。★ 它在【另一个仓库】里：本地开发时
#: 是 workspace 布局下的 `../../packages/skirt/skirtlib/deploy.py`，而 CI 只
#: checkout 这一个仓库 —— 那里没有它（见下面那条测试为什么 skip）。
PACKAGES_DEPLOY = ROOT.parent.parent / "packages" / "skirt" / "skirtlib" / "deploy.py"


def _加载_skirt_的_deploy():
    """把 packages 的 `skirtlib.deploy` 当模块加载进来。

    ★ 用 import 而不是"读源码抽字符串"：要的是【它真拼出来什么】，而不是
      "那个文件里出现过哪些名字"。
    ★ `packages/skirt`（skirtlib 的父目录）只是临时挂到 sys.path 上，加载完
      就摘掉——不让它之外的顶层名字渗进这个进程。
    """
    package_root = str(PACKAGES_DEPLOY.parent.parent)
    要摘 = package_root not in sys.path
    if 要摘:
        sys.path.insert(0, package_root)
    try:
        return importlib.import_module("skirtlib.deploy")
    finally:
        if 要摘:
            sys.path.remove(package_root)


@contextlib.contextmanager
def 干净的环境(**加进去):
    """一份没有凭据的环境变量。

    ★ 测试不该被开发者本机 shell 里的东西左右：有没有 S3_AK 会让同一条测试
      走到两条完全不同的分支上。
    """
    with mock.patch.dict(os.environ) as env:
        for k in list(env):
            if k.startswith("RCLONE_") or k in ("S3_AK", "S3_SK", "S3_BASE_URL",
                                                "S3_BUCKET", "PANSU_DATE"):
                env.pop(k)
        env.update(加进去)
        yield env


class Test命名(unittest.TestCase):

    def test_交付物文件名带日期(self):
        """★ 交付物不可变（0013）：同名覆盖会让"上次那张盘"静默消失。

        所以时间进文件名，而"最新的那张"由 latest.txt 指。

        ★ 2026-10-16：这断言原来是 `== "20261015"`，只查了【天】——
          而下面那个 class 里补的是"同一天两次会怎样"。那条才是真的。
        """
        with 干净的环境(PANSU_DATE="20261015"):
            v = repo._stamp()
            self.assertTrue(v.startswith("20261015T"), v)
            self.assertRegex(v, r"^20261015T\d{4}$", v)

    def test_传了完整时间戳就原样用(self):
        """CI / 调试时可以钉死到分钟，那样产物名是可复现的。"""
        with 干净的环境(PANSU_DATE="20261015T0330"):
            self.assertEqual(repo._stamp(), "20261015T0330")

    def test_不给日期就用今天(self):
        """没给 PANSU_DATE 就用本机时钟，粒度到【分钟】。"""
        with 干净的环境():
            v = repo._stamp()
            self.assertTrue(v.startswith(datetime.date.today().strftime("%Y%m%d")), v)
            self.assertRegex(v, r"^\d{8}T\d{4}$", v)

    def test_公开_URL_的形状(self):
        name = "minimal-x86_64-20261012.img.zst"
        self.assertEqual(repo.public_url("minimal", "x86_64", name),
                         f"{repo.PUBLIC_BASE}/img/minimal/x86_64/{name}")

    def test_校验值算得对(self):
        """sha256 算错了不会报错，只会让"校验"变成一句空话。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "空文件"
            f.write_bytes(b"")
            self.assertEqual(repo._sha256(f),
                             "e3b0c44298fc1c149afbf4c8996fb924"
                             "27ae41e4649b934ca495991b7852b855")
            f.write_bytes(b"abc")
            self.assertEqual(repo._sha256(f),
                             "ba7816bf8f01cfea414140de5dae2223"
                             "b00361a396177a9cb410ff61f20015ad")
            # 大文件走的是分块读，结果必须和一次读出来的一样
            f.write_bytes(b"abc" * 400_000)
            self.assertEqual(repo._sha256(f),
                             hashlib.sha256(b"abc" * 400_000).hexdigest())


class Test凭据(会报错的混入, unittest.TestCase):

    凭据 = dict(S3_AK="ak", S3_SK="sk", S3_BASE_URL="https://s3.example",
              S3_BUCKET="yojigen")

    def test_有本机_rclone_就用本机的(self):
        with 干净的环境(**self.凭据), mock.patch.object(repo.shutil, "which",
                                                lambda _: "/usr/local/bin/rclone"):
            cmd, env = repo._creds()
        self.assertEqual(cmd, "rclone")
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_TYPE"], "s3")
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID"], "ak")
        # RustFS / MinIO 都是路径式：不开这个，rclone 会去连 bucket.host
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE"], "true")

    def test_没有_rclone_就起容器_而且变量要逐个传进去(self):
        """★ `subprocess.Popen(env=env)` 改的是 **docker 客户端**的环境，而
        `docker run` 默认【不会】把客户端的变量带进容器。

        少了逐个 -e，容器里的 rclone 什么配置都没有，报的是
        "didn't find section in config file"——一句和真因看不出关系的话。
        实测撞过（照抄了 `skirt deploy` 的思路，漏了这一步）。
        """
        with 干净的环境(**self.凭据), mock.patch.object(repo.shutil, "which",
                                                lambda _: None):
            cmd, env = repo._creds()
        self.assertTrue(cmd.startswith("docker run --rm -i "), cmd)
        self.assertTrue(cmd.endswith(" rclone/rclone"), cmd)
        选项 = cmd.split("rclone/rclone")[0]        # 镜像名【之前】才是 docker 的参数
        for name in repo.RCLONE_VARS:
            with self.subTest(var=name):
                self.assertIn(f" -e {name}", 选项)
                self.assertIn(name, env)

    def test_没有凭据就什么都不设(self):
        """干跑要真的只是干跑：没凭据时环境里不该冒出一堆半截配置。"""
        with 干净的环境(), mock.patch.object(repo.shutil, "which", lambda _: "rclone"):
            _, env = repo._creds()
        self.assertFalse([k for k in env if k.startswith("RCLONE_CONFIG_YOJIGEN_")])

    def test_变量名单和_skirt_deploy_是同一套(self):
        """两个仓库往【同一个仓库】推文件，所以 rclone 那组变量必须一样。

        ★ 这一条真的去比：把 `packages/skirt/skirtlib/deploy.py` 加载进来，
        问它 `rclone_env()` 拼出哪些变量，再和这边的 `repo.RCLONE_VARS` 对齐。
        一边加了变量、另一边没加，症状是"某个字段静默地没生效"，比直接报错难查
        ——所以它值得一条会红的测试。

        ★ 原来这条只查了"名字都以 `RCLONE_CONFIG_YOJIGEN_` 开头"和
        "`SECRET_ACCESS_KEY` 在里面"——那两句是【假装查了】：另一边怎么变、
        这边少多少个，它都不会红。而它当时指的那个文件（`packages/scripts/
        deploy.sh`）2026-10-15 就搬成了 `packages/skirt/skirtlib/deploy.py`。

        ★ 那个文件在另一个仓库里，CI 只 checkout distro —— 读不到就 skip，
        并且说清是【因为不存在】，不是"比过了，一样"。
        """
        if not PACKAGES_DEPLOY.is_file():
            self.skipTest(f"读不到 {PACKAGES_DEPLOY}（CI 只 checkout distro 这一个仓库）"
                          "——跨仓库那半比对没法做")
        deploy = _加载_skirt_的_deploy()
        那边的名单 = set(deploy.rclone_env(
            {"S3_AK": "ak", "S3_SK": "sk", "S3_BASE_URL": "https://s3.example"}))
        self.assertTrue(那边的名单,
                        "deploy.rclone_env() 什么都没拼出来——它要的那三个 S3_* 变量名"
                        "是不是变了？(RCLONE_VARS=" + repr(getattr(deploy, "RCLONE_VARS", None)) + ")")
        self.assertEqual(set(repo.RCLONE_VARS), 那边的名单,
                         "两边的 rclone 变量名单不一样 —— 同一个仓库，两处必须一起改")

    def test_分段上传参数和_skirt_deploy_也是同一套(self):
        """★ 变量之外，那组【共享的 S3 参数】也要一样。

        为什么值得单独一条：`repo.py` 里自己写着——

            「这是【第三次】在 repo.py 里补 `skirt deploy` 已经有的东西了：
              ① 逐个 -e 传环境变量 ② 把源目录挂进容器 ③ 这一组分段上传参数。
              ……每次都是撞上才知道——**它们的形状是"那边能跑"，所以没人会去比。**」

        而"没人会去比"就是这一条要解决的事。

        ★ 而【不同的那些是有意不同的】，所以不要拿整个 argv 去比：
          `deploy` 推 `.apk`，用 `--checksum`（按内容比，apk 是内容寻址的）；
          `repo` 推镜像，用 `--s3-no-check-bucket` / `--stats-one-line`。
          比的是两边都声明为"共享"的那一组。
        """
        if not PACKAGES_DEPLOY.is_file():
            self.skipTest(f"读不到 {PACKAGES_DEPLOY}（CI 只 checkout distro 这一个仓库）"
                          "——跨仓库那半比对没法做")
        deploy = _加载_skirt_的_deploy()
        那边 = getattr(deploy, "SHARED_S3_ARGS", None)
        self.assertIsNotNone(那边, "packages 那边的 deploy.py 里没有 SHARED_S3_ARGS —— "
                                   "这一组共享参数的名字变了？")
        self.assertEqual(tuple(repo.SHARED_S3_ARGS), tuple(那边),
                         "两边共享的分段上传参数不一样 —— 同一个仓库，两处必须一起改。\n"
                         "  distro  : " + repr(repo.SHARED_S3_ARGS) + "\n"
                         "  packages: " + repr(那边))


class Test发布(会报错的混入, unittest.TestCase):

    def test_没有镜像就报错_并说先建(self):
        msg = self.应当报错(repo.publish, Path("/没有这个.img.zst"), "minimal", "x86_64")
        self.assertIn("pantsu img", msg)

    def test_没凭据就干跑_绝不传东西(self):
        """★ "干跑到此为止"必须是字面意思：一个字节都不该出网。

        所以这里把 run_streaming 换成"被调用就是失败"。
        """
        def 不该被调用(*a, **k):
            raise AssertionError("没有凭据还去传了")

        with tempfile.TemporaryDirectory() as tmp:
            zst = Path(tmp) / "minimal-x86_64.img.zst"
            zst.write_bytes("假装这是镜像".encode())
            out = io.StringIO()
            with 干净的环境(), mock.patch.object(repo, "run_streaming", 不该被调用), \
                 contextlib.redirect_stdout(out):
                url = repo.publish(zst, "minimal", "x86_64")
        self.assertTrue(url.endswith(".img.zst"), url)
        self.assertIn("minimal-x86_64-", url)
        self.assertIn("干跑", out.getvalue())

    def test_有凭据但没加_go_也还是干跑(self):
        def 不该被调用(*a, **k):
            raise AssertionError("没加 --go 还去传了")

        with tempfile.TemporaryDirectory() as tmp:
            zst = Path(tmp) / "minimal-x86_64.img.zst"
            zst.write_bytes("假装这是镜像".encode())
            out = io.StringIO()
            with 干净的环境(S3_AK="ak", S3_SK="sk", S3_BASE_URL="https://s3.example",
                        S3_BUCKET="yojigen"), \
                 mock.patch.object(repo, "run_streaming", 不该被调用), \
                 contextlib.redirect_stdout(out):
                repo.publish(zst, "minimal", "x86_64", go=False)
        self.assertIn("--go", out.getvalue())

    def test_干跑不留下垃圾_也不动原件(self):
        """暂存目录用完就删——留一堆 pantsu-img-* 在 /tmp 里会越攒越多。"""
        with tempfile.TemporaryDirectory() as tmp:
            zst = Path(tmp) / "minimal-x86_64.img.zst"
            zst.write_bytes("假装这是镜像".encode())
            之前 = set(Path(tempfile.gettempdir()).glob("pantsu-img-*"))
            with 干净的环境(), contextlib.redirect_stdout(io.StringIO()):
                repo.publish(zst, "minimal", "x86_64")
            之后 = set(Path(tempfile.gettempdir()).glob("pantsu-img-*"))
            self.assertTrue(zst.is_file())      # 原件不能被动过
        self.assertEqual(之后 - 之前, set(), "暂存目录没删掉")


class Test没测到的(unittest.TestCase):
    """★ 缺口，不是覆盖。"""

    @unittest.skip("它会往【真仓库】里写东西——而那里住的是别人会拉的东西。"
                   "这种事不该由本地随手触发（连 latest.txt 都会被改掉）")
    def test_真的推一次(self):
        """端到端：rclone（或它的容器）把镜像 + sha256 + latest.txt 传上去。

        单元测试验的是"干跑不传、参数齐不齐"；"传上去了没有、URL 打不打得开"
        只有真推一次才知道。★ 所以这条【故意留空】：它要在一次真正的发布里
        才算数（image.yml 的 --go 那一步就是它，而且带着真产物）。
        """





class Test交付物不可变(会报错的混入, unittest.TestCase):
    """★★ 这一组守的是一件【2026-10-16 真的发生过】的事。

    那天同一天建了两次 minimal/x86_64：

        04:15  建盘 → ce652348… → 发布
        04:26  把它 DD 进真机 TX          ← TX 现在跑的就是这张
        04:30  又建一次 → ac3c6a2c… → **把 ce652348… 覆盖了**

    结果是 **TX 上跑的那张盘，线上没有了** —— 而它上面的 sha256 拿去对账会对不上。
    没有任何地方说过这件事。

    ★ 而当时那条测试（`test_交付物文件名带日期`）**只查了日期的格式**：
      它验的是"日期进了文件名"，没验"同一天两次会怎样"。
      那条 docstring 写对了，而它守的东西只有一半。
    """

    def _假镜像(self, tmp):
        f = Path(tmp) / "minimal-x86_64.img.zst"
        f.write_bytes(b"\x28\xb5\x2f\xfd" + b"x" * 64)
        return f

    凭据 = dict(S3_AK="ak", S3_SK="sk", S3_BASE_URL="https://s3.example",
              S3_BUCKET="yojigen")

    def test_同一天的两个时刻给不同的名字(self):
        """(a) 那一半：粒度从【天】改成【天+时:分】—— 让撞名要撞到同一分钟。"""
        import datetime as _dt
        with 干净的环境(PANSU_DATE="20261016"):
            with mock.patch.object(repo, "datetime") as dt:
                dt.now.return_value = _dt.datetime(2026, 10, 16, 4, 30)
                甲 = repo._stamp()
                dt.now.return_value = _dt.datetime(2026, 10, 16, 4, 55)
                乙 = repo._stamp()
        self.assertNotEqual(甲, 乙,
                            "同一天的两个时刻给出了同一个名字 —— 那第二次会盖掉第一次")

    def test_线上同名就停下(self):
        """(b) 那一半：不覆盖 —— 除非 --force。"""
        with tempfile.TemporaryDirectory() as tmp:
            zst = self._假镜像(tmp)
            with 干净的环境(**self.凭据):
                名 = f"minimal-x86_64-{repo._stamp()}.img.zst"
                name_hint = 名
                with mock.patch.object(repo, "_线上已有", return_value={名}):
                    话 = self.应当报错(repo.publish, zst, "minimal", "x86_64", go=True)
        self.assertIn("不覆盖", 话)
        self.assertIn("--force", 话, "得告诉人怎么办")
        self.assertIn(name_hint, 话)

    def test_force_才放行(self):
        """而 --force 是【有意的】：给了就走。"""
        with tempfile.TemporaryDirectory() as tmp:
            zst = self._假镜像(tmp)
            with 干净的环境(**self.凭据):
                名 = f"minimal-x86_64-{repo._stamp()}.img.zst"
                with mock.patch.object(repo, "_线上已有", return_value={名}):
                    with mock.patch.object(repo, "run_streaming", return_value=(0, "")) as rs:
                        repo.publish(zst, "minimal", "x86_64", go=True, force=True)
        self.assertTrue(rs.called, "--force 之后应该真的往下传了")

    def test_查不到线上就不拦(self):
        """没有凭据 / 网络不通 —— 那时拦下来的是网络，不是覆盖。"""
        self.assertEqual(repo._线上已有("不存在的命令-xyz", {}, "yojigen:b/x"), set())


if __name__ == "__main__":
    unittest.main()
