"""容器交付物：从 apk 仓库装出一个根，打成 tar，导入成容器镜像。

    pantsu assemble base
    pantsu assemble base --repo http://host.docker.internal:18080 --tag edge
    pantsu assemble base --image yojigenos/试验 --tag 2026.10.15

## 它做的两件事

    ① 装根 + 打 tar     → root.build()（就是 `pantsu rootfs` 那一步，同一份实现）
    ② 导入成容器镜像     → docker import + 标签

第①步原来整段写在 `scripts/assemble.sh` 里（2026-10-15 收进这里）。搬走的
理由和别处一样：它和"建一张磁盘镜像"的前半段**逐字相同**——同一个东西两个
出处，只有一个会被改。

## 它只认一个仓库 URL

不 checkout 配方，不读 `packages/var/output/`。最要命的那条理由见 README：
那会让"发布的包"和"用来组装的包"可以是不同的东西，而且没人会发现。

## 和 shell 版不一样的两处默认值（都是故意的）

    --repo    默认 https://repo.os.yojigen.cn（shell 版默认本地那个 18080）
              理由：`rootfs` / `img` 都默认线上仓库，而 CI 一直显式传。
              本地调试照旧：`--repo http://host.docker.internal:18080`
    --arch    默认宿主架构（shell 版写死 aarch64）
              理由：调用方和 `rootfs` / `img` 一样，不该在这里有例外
"""

from __future__ import annotations

import shutil
import subprocess
import time

from . import flavor, root
from .common import LABELS
from .common import DEFAULT_REPO, ROOT, die, log, resolve_arch, run

#: 标准的环境变量。`docker import` 不像 Dockerfile 那样有默认值，而少了
#: PATH 的镜像跑什么都要写全路径。
DEFAULT_ENV = (
    "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "ENV LANG=C.UTF-8",
    # ★ CMD 必须显式给。不给的话 `docker run <镜像>` 直接报
    #   "no command specified"——而那正是最自然的用法（进一个 shell 看看）。
    #
    #   /bin/sh 是基础镜像的惯例（alpine、debian:slim 都是）。我们的 /bin/sh
    #   是 dash，所以进去就是个 POSIX shell。
    'CMD ["/bin/sh"]',
)

# ★ 我们自己的三个标签（`cn.yojigen.*`）【没有】单独一份清单 —— 清单只有
#   `common.LABELS` 一份。这里曾经有一份 `OWN_LABELS`，而它唯一的读者是测试：
#   生产代码从来不问它。第三份抄写没有好处，只会分叉（这一族已经分叉过一次，
#   见 common.LABELS 那段）。
#
# ★ 命名空间用【反写域名】，所以根必须是【我们真的拥有的域名】。
#
#   原来是 `org.yojigenos.…`——那声明的是 yojigenos.org，而那个域名不是我们的。
#   真的是 yojigen.cn，所以根是 `cn.yojigen` ✓。
#
#   这一层比"连字符"重要：`org.yojigenos` 和 `org.yojigen-os` 都合法、都能用，
#   但两个都指向一个不存在的域名。**合法不等于对。**
#   （名字这一层由 `pantsu/tests/test_verify.py` 的"用的是我们真拥有的域名"守着。）


def _revision() -> str:
    """这份镜像是从哪个 commit 建的。

    ★ 拿不到就【不写这个标签】，而不是写个占位符。一个说"我不知道"的标签比
      一个写着 "unknown" 的标签有用——后者看起来像"这个 commit 叫 unknown"。
    """
    if shutil.which("git") is None:
        return ""
    proc = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def import_args(name: str, arch: str, tag: str, built: str, revision: str) -> list[str]:
    """`docker import` 的 `-c` 参数——顺序和内容都是决策记录 0003 定的。"""
    args: list[str] = []
    for instruction in DEFAULT_ENV:
        args += ["-c", instruction]

    # ★ 值里有空格就必须带引号：`docker import -c "LABEL …"` 会【按空格切】。
    #   不加报的是 `Syntax error - can't find = in "OS"`——只提到 OS，
    #   看不出是 title 那一行出的问题。
    #: ★ 值这么填。而【有哪些标签】是 LABELS 那份清单说了算 —— 它和 verify 共用，
    #:   所以"写了但没验"在结构上不可能发生（那正是它之前发生过的事：
    #:   image 写九个、verify 查七个，而漏掉的那两个可以悄悄消失而没人知道）。
    values = {
        "org.opencontainers.image.title": f'"Yojigen OS {name}"',
        "org.opencontainers.image.version": tag,
        "org.opencontainers.image.created": built,
        "org.opencontainers.image.source": "https://github.com/YojigenOS/distro",
        "org.opencontainers.image.url": "https://os.yojigen.cn",
        "cn.yojigen.os.flavor": name,
        "cn.yojigen.os.arch": arch,
        "cn.yojigen.os.built": built,
        "org.opencontainers.image.revision": revision,
    }
    for key in LABELS:
        v = values.get(key)
        if not v:
            # revision 在拿不到 commit 时是空的（见上）。其余的空了就是 bug。
            if key == "org.opencontainers.image.revision":
                continue
            die(f"标签 {key} 没有值 —— LABELS 里加了它而 values 里没填")
        args += ["-c", f"LABEL {key}={v}"]
    return args


def assemble(name: str, arch_requested: str | None = None, repo: str = DEFAULT_REPO,
             tag: str = "edge", image_name: str | None = None) -> str:
    """装根 → 打 tar → 导入成容器镜像。返回镜像引用（`名:tag`）。"""
    v = flavor.load(name)
    arch = resolve_arch(arch_requested)
    tag = tag or "edge"
    ref = f"{image_name or f'yojigenos/{name}'}:{tag}"

    # ── ① 装根 + 打 tar ──────────────────────────────────────────────────
    # 就是 `pantsu rootfs`。★ 不给它 --skip-missing：那个开关是给"先看看
    # 剩下的部分对不对"用的，而这里产出的东西是【交付物】——故意不完整的
    # 镜像不该从这条路出去。
    tar_path = root.build(name, arch, repo)

    # ── ② 导入成 docker 镜像 ─────────────────────────────────────────────
    #
    # ★ 这一个时间戳就是"现在"——和 tar 里那个钉死的 TAR_MTIME 不一样，而这是
    #   故意的：根要对同一份输入可复现（0005），而这个标签回答的是"这个镜像
    #   什么时候建的"（0003 的 created）。钉死它反而让它变成一句假话。
    built = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    revision = _revision()

    print()
    log("导入", ref)
    if v["_kind"] != "container":
        # 不是错，只是要说清楚：kind 是给【产出交付物】用的声明，
        # 而 assemble 产出的就是容器镜像。
        log("风味", f"{name} 的 kind 是 {v['_kind']}，而这条命令产出的是容器镜像")

    # ★ 不给 --platform：docker import 出来的镜像架构由【本机的 docker daemon】
    #   决定，跨架构打标签会让它在别的机器上 exec format error。要 x86_64 的
    #   镜像就在 x86_64 的机器上 import（CI 就是这么做的，见 release.yml）。
    proc = subprocess.run(
        ["docker", "import", *import_args(name, arch, tag, built, revision),
         str(tar_path), ref],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # shell 版把成功时的 sha256 那行丢掉了（> /dev/null），失败时也能看见
        # stderr——这里两样都留着，但只在失败时把 stderr 端出来。
        die(f"docker import 失败（退出码 {proc.returncode}）\n"
            f"       {proc.stderr.strip()}")

    print(f"  标签:    cn.yojigen.os.flavor={name}  arch={arch}")
    print(f"           org.opencontainers.image.created={built}")
    if revision:
        print(f"           org.opencontainers.image.revision={revision[:12]}")

    run(["docker", "images", ref, "--format",
         "      ✓ {{.Repository}}:{{.Tag}}  {{.Size}}"])

    # ── 报告 ─────────────────────────────────────────────────────────────
    print()
    print("  试着用它：")
    # ★ docker 的语义：`docker run <镜像> <参数...>` 是【替换 CMD】，不是往 CMD
    #   后面追加。所以 `docker run <镜像> -c 'echo hi'` 是错的——docker 会把 `-c`
    #   当成可执行文件。要跑一条命令得把 shell 写出来。
    # ★ 别教人用不存在的东西：base 不带 bash（/bin/sh 是 dash）。
    print(f"      docker run --rm {ref} sh -c 'echo 你好，Yojigen OS'")
    print(f"      docker run --rm {ref} /usr/bin/gawk 'BEGIN{{print 1+1}}'")
    print(f"      docker run -it --rm {ref}          # 进去看看（默认进 /bin/sh）")
    print()
    return ref
