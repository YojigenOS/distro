"""把镜像放进仓库（repo.os.yojigen.cn）。

仓库里现在住两类东西，各按各的约定：

    <桶>/<架构>/…                apk 仓库 —— apk 自己的约定
                                 （客户端会去取 <url>/<架构>/APKINDEX.tar.gz）
    <桶>/img/<风味>/<架构>/…      系统镜像 —— 我们定的约定

## 镜像的约定

    img/<风味>/<架构>/<风味>-<架构>-<日期>.img.zst     ← 交付物，不可变
    img/<风味>/<架构>/<同名>.sha256                    ← 它的校验值
    img/<风味>/<架构>/latest.txt                       ← 一行文件名

于是有两条 URL：

    https://repo.os.yojigen.cn/img/minimal/x86_64/minimal-x86_64-20261012.img.zst
    https://repo.os.yojigen.cn/img/minimal/x86_64/latest.txt

**为什么是 latest.txt 而不是覆盖一个 latest.img**：交付物必须不可变（0005），
而 reinstall 那条路需要一个【不用改脚本的固定 URL】。一行文本同时满足两件事：

    IMG=$(curl -s https://repo.os.yojigen.cn/img/minimal/x86_64/latest.txt)
    reinstall.sh dd --img https://repo.os.yojigen.cn/img/minimal/x86_64/$IMG

## 推送用 rclone，环境变量和 packages/scripts/deploy.sh 是同一套

    S3_AK · S3_SK · S3_BASE_URL · S3_BUCKET

本地不用装 rclone——没有就起 `rclone/rclone` 容器（版本也统一了）。

**只 copy，绝不 sync。** 仓库是多来源共用的：apk 在 `<架构>/` 下、镜像在
`img/` 下，将来还会有别的东西。sync 会把别人放的东西删掉，而这类事故在
"发布"这个动作里是最贵的（删掉的是别人已经引用的东西）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

from .common import ROOT, die, log, run_streaming

#: 对外可访问的基址。apk 客户端和 reinstall 都用它拼 URL。
PUBLIC_BASE = os.environ.get("PANSU_PUBLIC_BASE", "https://repo.os.yojigen.cn")

#: 交付物文件名里的日期。CI 里用构建日期，本地默认今天。
def _stamp() -> str:
    return os.environ.get("PANSU_DATE") or date.today().strftime("%Y%m%d")


def public_url(flavor: str, arch: str, name: str) -> str:
    return f"{PUBLIC_BASE}/img/{flavor}/{arch}/{name}"


# 要传进 rclone 容器的变量名单。
#
# ★ 和 packages/scripts/deploy.sh 里那份【是同一套】。两处必须一起改——
#   一边加了变量另一边没加，症状是"某个字段静默地没生效"，比直接报错难查。
RCLONE_VARS = (
    "RCLONE_CONFIG_YOJIGEN_TYPE",
    "RCLONE_CONFIG_YOJIGEN_PROVIDER",
    "RCLONE_CONFIG_YOJIGEN_ENDPOINT",
    "RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID",
    "RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY",
    "RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE",
    "RCLONE_CONFIG_YOJIGEN_REGION",
)


def _creds() -> tuple[str, dict[str, str]]:
    ak, sk = os.environ.get("S3_AK"), os.environ.get("S3_SK")
    base, bucket = os.environ.get("S3_BASE_URL"), os.environ.get("S3_BUCKET")
    env = dict(os.environ)
    if ak and sk and base:
        env.update({
            "RCLONE_CONFIG_YOJIGEN_TYPE": "s3",
            "RCLONE_CONFIG_YOJIGEN_PROVIDER": "Other",
            "RCLONE_CONFIG_YOJIGEN_ENDPOINT": base,
            "RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID": ak,
            "RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY": sk,
            # RustFS / MinIO 都是路径式。不开这个，rclone 会去连 bucket.host。
            "RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE": "true",
            "RCLONE_CONFIG_YOJIGEN_REGION": "us-east-1",
        })
    # 有本机 rclone 就用本机的，没有就起容器（本地是 mac，装它要 brew；
    # CI 上装它又是一条命令。放进容器里，这件事就只有一个实现）。
    if shutil.which("rclone"):
        return "rclone", env

    # ★★ 用容器时必须【逐个 -e 把变量传进去】。
    #
    #   `subprocess.Popen(env=env)` 改的是 **docker 客户端**的环境，而
    #   `docker run` 默认【不会】把客户端的变量带进容器。少了这一步，容器里的
    #   rclone 什么配置都没有，报的是：
    #
    #       CRITICAL: Failed to create file system for "yojigen:***/img/…":
    #           didn't find section in config file ("yojigen")
    #
    #   ——一句和真正原因（变量没传进去）看不出关系的话。
    #
    #   `-e VAR`（不带 =值）的语义是"把这个变量从客户端的环境里取出来传进去"，
    #   正是这里要的。packages/scripts/deploy.sh 里那段是同一个写法，
    #   而它是对的——**我照抄了思路，漏了这一步。**
    cmd = "docker run --rm -i"
    for name in sorted(RCLONE_VARS):
        cmd += f" -e {name}"
    return cmd + " rclone/rclone", env


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def publish(zst: Path, flavor: str, arch: str, go: bool = False) -> str:
    if not zst.is_file():
        die(f"找不到镜像 {zst}。先建：pantsu img {flavor} --arch {arch}")

    name = f"{flavor}-{arch}-{_stamp()}.img.zst"
    # 没凭据时用 ${S3_BUCKET} 这个字面量占位——干跑打出来的是"到时候会是什么
    # 样子"，而不是一个假的具体值。
    bucket = os.environ.get("S3_BUCKET") or "${S3_BUCKET}"
    dest = f"yojigen:{bucket}/img/{flavor}/{arch}"

    log("镜像", f"{zst.name}  ({zst.stat().st_size / 1048576:.1f} MB)")
    log("目标", f"img/{flavor}/{arch}/{name}")
    log("URL", public_url(flavor, arch, name))

    # 先在临时目录里摆好要传的三个文件，再一次 copy 上去——一个 rclone 调用，
    # 三件东西要么都在要么都不在，不会留下"镜像传上去了但 sha256 还没上"的
    # 中间状态。
    stage = Path(tempfile.mkdtemp(prefix="pantsu-img-"))
    try:
        target = stage / name
        try:
            os.link(zst, target)          # 同一个盘：硬链接，不占空间
        except OSError:
            shutil.copy2(zst, target)     # 跨盘就老实拷
        (stage / f"{name}.sha256").write_text(
            f"{_sha256(zst)}  {name}\n", encoding="utf-8")
        # latest.txt 里只放文件名：这样"最新的那张"永远是一个可解析的事实，
        # 而不是靠列目录猜。
        (stage / "latest.txt").write_text(name + "\n", encoding="utf-8")

        print()
        for f in sorted(stage.iterdir()):
            log("  备好", f"{f.name}  ({f.stat().st_size} 字节)")

        rclone, env = _creds()
        if "S3_BUCKET" not in env or not env.get("S3_BUCKET"):
            print()
            print("  ⚠ 没有 S3_BUCKET / S3_AK / S3_SK / S3_BASE_URL")
            print("    干跑到此为止：文件都备好了，凭据齐了才会真的传。")
            print(f"    传的时候是这个命令（目标 {dest}/）：")
            print(f"      {rclone} copy <staging> {dest}/")
            return public_url(flavor, arch, name)

        if not go:
            print()
            print("  干跑（加 --go 才真的传）。要传的是上面那三个文件。")
            return public_url(flavor, arch, name)

        print()
        log("上传", f"{rclone}  →  {dest}/")

        # ★ 用容器时，暂存目录必须【挂进去】，源路径也要换成容器里的那个。
        #
        #   容器里看不到宿主的 /tmp：
        #
        #       ERROR : Local file system at /tmp/pantsu-img-1cgv4o7j:
        #           error reading source root directory: directory not found
        #
        #   ★ 这和上面逐个 -e 传变量是同一件事的两半：**起容器时，凡是要被
        #     容器用到的东西——环境变量、路径——都得显式交给它。**
        #     `deploy.sh` 那边挂的是固定的 $PWD/var/repo，所以那半它天然没
        #     这个问题；rclone 用宿主的 /tmp 就得动态挂。
        src = str(stage)
        rclone_cmd = rclone.split()
        if rclone_cmd[:1] == ["docker"]:
            # ★ 插在【镜像名之前】。
            #
            #   `rclone` 这个字符串是 "docker run … rclone/rclone"，直接 append
            #   会把 -v 放到镜像名后面——那时它已经是【传给 rclone 的参数】了，
            #   docker 根本不认，于是挂载静默地没生效，报的还是 directory not found。
            #
            #   ★ 我上一版就是这么写的，而且【把拼好的命令打印出来了】——
            #     打印了但没看顺序。打印一个东西和检查一个东西不是一回事。
            i = rclone_cmd.index("rclone/rclone")
            rclone_cmd[i:i] = ["-v", f"{stage}:/staging"]
            src = "/staging"

        cmd = (rclone_cmd + ["copy", src, dest + "/",
                             "--s3-no-check-bucket", "--stats-one-line"])
        rc, _ = run_streaming(cmd, env=env)
        if rc != 0:
            die(f"上传失败（退出码 {rc}）")
        print()
        log("完成", public_url(flavor, arch, name))
        print(f"      DD 的时候可以用稳定入口：")
        print(f"      {public_url(flavor, arch, 'latest.txt')}")
        return public_url(flavor, arch, name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
