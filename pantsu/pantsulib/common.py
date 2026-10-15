"""路径常量、日志、错误退出、命令执行——最底层的公共设施。

和 packages 那边的 skirtlib/common.py 是两个东西：那边管构建（配方、源码、
哈希），这边管组装（风味、仓库、交付物）。共用的只有"怎么打印一句话"的品味。
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

# 模块在 pantsu/pantsulib/ 下，仓库根是上三级
ROOT = Path(__file__).resolve().parent.parent.parent
VARIANTS_DIR = ROOT / "variants"
#: 会烤进交付物的签名公钥。见 keys/README.md。
KEYS_DIR = ROOT / "keys"
VAR_DIR = ROOT / "var"

#: 借来的工具镜像。借的是【工具】不是【包】——Alpine 自己的包是 musl 链接的，
#: 一个都用不了。和 packages 那边的 Debian 构建环境同一类。
#:
#: 为什么是 Alpine：这台机器上没有 apk，而 Alpine 的 apk 是 3.0，和我们打的包
#: 同代（v3 格式）。
TOOL_IMAGE = "alpine:latest"

#: 探测用的临时镜像。**不是交付物**——交付物是 tar 和 .img。
PROBE_IMAGE = "yojigenos/probe"

#: 默认仓库。这就是两个仓库之间的契约：distro 只认一个 URL。
DEFAULT_REPO = "https://repo.os.yojigen.cn"

#: 本地调试用的仓库地址（宿主上起一个静态服务）。
#: 注意容器里的 localhost 是【容器自己】，所以这个地址只在容器里有意义，
#: 由 docker 的 --add-host 负责解析。
LOCAL_REPO = "http://host.docker.internal:18080"

#: tar 里所有成员统一的时间戳（2026-01-01T00:00:00Z）。
#:
#: 见 packages 的决策记录 0005。这里目前是个常量，而不是"参与组装的包里最晚
#: 那个日期"——那个值算得出来（原先的 rootfs.py 就是这么做的），但它需要读配方
#: 的日期，而 distro 不读配方。等仓库的索引里带上日期再说。
TAR_MTIME = "@1767225600"

ARCH_ALIASES = {
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}

# --------------------------------------------------------------- 输出

def log(stage: str, msg: str) -> None:
    print(f"\033[1m[{stage}]\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"\033[33m[pantsu] 警告\033[0m {msg}", flush=True)


def die(msg: str) -> None:
    sys.stdout.flush()      # 否则 stdout 里的进度会冒到错误信息后面
    print(f"\033[31mpantsu: 错误\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------- 架构

def resolve_arch(requested: str | None) -> str:
    host = ARCH_ALIASES.get(platform.machine())
    if host is None:
        die(f"不认识的宿主架构: {platform.machine()}")
    if requested is None:
        return host
    want = ARCH_ALIASES.get(requested, requested)
    if want not in ARCH_ALIASES.values():
        die(f"不认识的架构: {requested}")
    # 不拒绝非宿主架构：换架构在这里不是换工具链，是换一串包（和 docker 的
    # --platform 无关——组装是解包，不执行被装的二进制）。
    return want


# --------------------------------------------------------------- 命令执行

def run(cmd: list[str]) -> None:
    """跑一条命令，输出直接透传，失败即 die。"""
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        die(f"命令失败（退出码 {proc.returncode}）: {' '.join(cmd)}")


def run_streaming(cmd: list[str], stdin_text: str | None = None,
                  indent: str = "      ",
                  env: dict | None = None) -> tuple[int, str]:
    """跑一条命令：输出边跑边打出来，同时攒一份给调用方解析。

    为什么不能直接 capture_output：apk 下载和解包要看得见进度。为什么还要
    攒一份：失败时要能从 apk 的报错里认出【哪些包不在仓库里】，好给一句
    人话，而不是把 apk 的原始输出原样倒出来。

    env：整份环境变量（不是增量）。推送镜像要给 rclone 塞
    RCLONE_CONFIG_YOJIGEN_*，又不能污染当前进程。
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    if stdin_text is not None and proc.stdin is not None:
        proc.stdin.write(stdin_text)
        proc.stdin.close()

    chunks: list[str] = []
    assert proc.stdout is not None
    # ★ 用 with 关掉它。不关的话每跑一条命令漏一个文件描述符——pantsu 自己
    #   跑一轮就退了看不出来，但它在【别人的进程里】被当库用时是会攒起来的
    #   （测试里表现为 ResourceWarning）。
    with proc.stdout:
        for line in proc.stdout:
            chunks.append(line)
            print(indent + line, end="", flush=True)
    proc.wait()
    return proc.returncode, "".join(chunks)
