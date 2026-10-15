#!/usr/bin/env python3
"""repo.py —— 把镜像放进仓库那一步里【不联网、不传东西】的那些逻辑。

跑法:
    python3 -m unittest discover -s pantsu/tests
    python3 pantsu/tests/test_repo.py              # 单独跑也行

【为什么值得测】"发布"是唯一一个**做错了别人会拿到坏东西**的动作：

  · 交付物必须不可变（0005）：文件名里带日期，latest.txt 只写一行文件名
  · 干跑要真的只是干跑（没凭据 / 没加 --go 就不许传）
  · 推文件那一组参数（S3 分段上传、逐变量 -e）——它们在 packages 的
    deploy.sh 里已经踩过一遍，这里验的是"同一个仓库用同一套参数"
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
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

from pantsulib import repo  # noqa: E402


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
        """★ 交付物不可变（0005）：同名覆盖会让"上次那张盘"静默消失。

        所以日期进文件名，而"最新的那张"由 latest.txt 指。
        """
        with 干净的环境(PANSU_DATE="20261015"):
            self.assertEqual(repo._stamp(), "20261015")

    def test_不给日期就用今天(self):
        with 干净的环境():
            self.assertEqual(repo._stamp(),
                             datetime.date.today().strftime("%Y%m%d"))

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
        实测撞过（照抄了 deploy.sh 的思路，漏了这一步）。
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

    def test_变量名单是给同一个仓库用的(self):
        """这一份和 packages/scripts/deploy.sh 里那份是同一套，两处必须一起改。"""
        for name in repo.RCLONE_VARS:
            self.assertTrue(name.startswith("RCLONE_CONFIG_YOJIGEN_"), name)
        self.assertIn("RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY", repo.RCLONE_VARS)


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


if __name__ == "__main__":
    unittest.main()
