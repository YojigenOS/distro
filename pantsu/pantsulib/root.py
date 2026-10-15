"""从 apk 仓库装出一个根，并打成 tar（容器交付物的原料 / 建盘的输入）。

这是 pantsu 的核心：**装一个系统，靠的是包管理器，不是叠文件。**

## 为什么合并必须在容器里做

macOS 的 APFS 默认大小写不敏感，在宿主机上合并两棵文件树会**静默**吞掉只差
大小写的文件——产出一个看起来正常、实际上缺了东西的根。这种错误没有任何
报错，只有等到运行时才暴露。所以解包和打包都发生在 Linux 容器里。

## 这里原来不是这么做的

搬过来的 scripts/rootfs.py.待改 走的是另一条路：把 `var/output/<架构>/<包>/payload/`
一层层叠起来。distro/README 已经把它判掉了，理由有三条，最要命的是第三条：

    它让【发布的包】和【用来组装的包】可以是不同的东西，而且没人会发现

现在这条路是 apk：依赖解析、冲突检测、下载、校验、解包、记账，全由它做。
顺带它还更严格——按声明的依赖装，所以"配方少声明了一个库"会当场炸，
而叠 payload 什么都看不出来（见 docs/待办.md 里 gawk 少 libmpfr 那次）。

## 语言筛选为什么没了

原来拼装时会删掉除 zh_CN / zh_TW 之外的 .mo（62 种语言、29.6 MB → 2 MB）。
那个需求现在由 `glibc-locales` 子包满足：**要不要翻译，变成风味列不列它**。
所以"拼装期筛选"这一整段逻辑没有存在的理由了。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from . import flavor, verify
from .common import (
    DEFAULT_REPO, KEYS_DIR, LOCAL_REPO, PROBE_IMAGE, ROOT, TAR_MTIME,
    TOOL_IMAGE, VAR_DIR, die, log, resolve_arch, run, run_streaming, warn,
)

#: apk 说"仓库里没有这个包"时的样子：
#:
#:     ERROR: unable to select packages:
#:       linux (no such package):
#:         required by: world[linux]
MISSING_RE = re.compile(r"^\s*(\S+) \(no such package\)", re.M)


# ── 容器里跑的脚本 ────────────────────────────────────────────────────────
#
# 值全部通过 -e 传进去，不在 Python 里拼字符串——省掉一整套转义问题。
CONTAINER_SCRIPT = """
set -eu

# Alpine 自带的 tar 是 BusyBox 的：不认 --sort=name / --mtime / --pax-option，
# 而可复现构建（packages 的决策记录 0005）全靠那几个参数。
apk add --no-cache tar >/dev/null 2>&1

# 清空目标根。清在这里而不是宿主上：绑定挂载里写出来的东西属主是容器里的
# root，宿主的普通用户删不掉（macOS 上 Docker Desktop 会把它虚拟化成宿主用户，
# 所以本地看不出来，CI 上会）。挂载点本身不能删，所以只清里面的东西。
find /newroot -mindepth 1 -maxdepth 1 -exec rm -rf {} +

# ★ 公钥【先】放进目标根，再装东西。
#
#   顺序是必须的：apk 装第一个包时就要验签，而验签要公钥。所以不能等
#   filesystem 包（它自己也得验签）带进来——那就成了鸡生蛋。
#
#   这也正是"信任从哪里开始"的答案：公钥来自交付物自己的构建输入
#   （distro/keys/，在 git 里），不经过网络，不依赖 HTTPS 锚定。
mkdir -p /newroot/etc/apk/keys
cp /keys/*.rsa.pub /newroot/etc/apk/keys/

# 仓库地址也先写进去——那样系统里的 apk 才知道去哪装东西。零成本，而且让
# 交付物【自己说得清它是从哪来的】。
mkdir -p /newroot/etc/apk
printf '%s\\n' "$REPO" > /newroot/etc/apk/repositories

# ★ apk 的行为策略。放这里而不是 apk-tools 包里，判据和上面那条一样：
#
#     仓库地址    = 「装哪些仓库」       —— 发行版的决定
#     interactive = 「升级要不要先问」  —— 也是发行版的决定
#
#   上游默认是 auto（看是不是终端：是就问，不是就直接干）。而"从非终端的
#   连接里一声不吭把库换掉"这件事是实打实撞到的——VNC 控制台那类连接，
#   stdin 不是终端，于是 apk 认为没人看着。
#
#   对一台服务器这个默认不够：升级内核、换库这种事，按回车之前该看一眼。
#
#   ★ 而脚本那条路不会因此卡住：apk 读到 EOF 时【接受】它（源码里
#     `r != EOF` 不算拒绝），接着往下走。下面那处 apk add 另外显式加了
#     </dev/null，双保险。
printf 'interactive yes\n' > /newroot/etc/apk/config

# ★ 不用 --allow-untrusted —— 真的验签。
apk add --keys-dir /newroot/etc/apk/keys \\
    --repository "$REPO" \\
    --root /newroot \\
    --initdb \\
    $PKGS </dev/null

echo
echo "── 装出来的根 ──"
echo "   包数: $(apk --root /newroot info 2>/dev/null | wc -l)"
echo "   文件: $(find /newroot/usr -type f 2>/dev/null | wc -l)"
echo "   大小: $(du -sh /newroot 2>/dev/null | cut -f1)"

# 装完复验：如果上面有错但 apk 继续装了，这里要能看出来
echo
echo "── apk audit ──"
apk --root /newroot audit --system 2>&1 | head -10 || true

# ── 组成清单 ──────────────────────────────────────────────────────────────
#
# 一张盘出了问题，第一个要问的是"它到底是哪些包的哪些版本"。apk 自己的库里
# 有这个事实（/lib/apk/db/installed），但那是给 apk 读的格式。这里把它整理成
# 一份人能读、能 diff、能贴进 issue 的清单——它会跟着根进 tar、进镜像。
#
# ★ 时间戳用固定的 $BUILD_DATE，不用"现在"：否则同一份输入会建出两个不同的根，
#   0005 要的确定性就破了。
#
#   ★ 它从哪儿来：宿主上由 `TAR_MTIME`（`common.py`，@1767225600）算成
#     "2026-01-01 00:00 UTC" 再通过 -e 传进来（见下面 `_install()`）——
#     和 tar 成员那个 `--mtime` 是【同一个值】。这里原来写的是"由
#     SOURCE_DATE_EPOCH 算出来"，那是 packages 那边每个包自己的构建时间戳，
#     distro 这一侧没有这个概念。
#
#   ★ 这一侧管时间的只有两样，别混：`TAR_MTIME` 是【钉死的】，管的是"同一份
#     输入建出同一个根"（0005）；`time.gmtime()` 是【现在】，只用在 image.py
#     那个 `created` 标签上（回答"这个镜像什么时候建的"）。看错一个就会以为
#     分发侧接了上游的 epoch。
#
# ★ 放在【装根】这一步而不是建盘那一步：tar、磁盘镜像两种交付物就都带着它，
#   不用各写一遍、也不会两边不一致。
{
    echo "# Yojigen OS 组成清单"
    echo "# 风味 $FLAVOR · 架构 $ARCH"
    echo "# 仓库 $REPO"
    echo "# 生成 $BUILD_DATE"
    echo "#"
    awk -F: '/^P:/{p=$2} /^V:/{print p "-" $2}' /newroot/lib/apk/db/installed | sort
} > /newroot/etc/yojigen-os-manifest
echo "   清单: $(grep -c '^[^#]' /newroot/etc/yojigen-os-manifest) 个包 → /etc/yojigen-os-manifest"

# ★ 确定性：排序 + 固定属主 + 固定时间戳 + 去掉 PAX 头里的 atime/ctime。
#   一条都不能少，理由见 packages 的决策记录 0005。
#
#   注意 --pax-option 那一条是这次搬家补上的：原来 assemble.sh 那份没有它，
#   而同一个仓库里的 rootfs.py.待改 有。0005 要的东西不该只做一半。
echo
echo "── 打包 ──"
tar -C /newroot -czf "/out/$TAR_NAME" \\
    --sort=name --numeric-owner --owner=0 --group=0 \\
    --mtime="$TAR_MTIME" \\
    --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \\
    .
echo "   $TAR_NAME: $(stat -c%s "/out/$TAR_NAME") 字节"

# 交付物里 tar 成员的属主由上面 --owner/--group 决定，和这一步无关；这一步
# 只是让【宿主】能删掉 var/<风味>/rootfs-<架构>/。
chown -R "$HOST_UID:$HOST_GID" /newroot
"""


# ── 前置检查 ──────────────────────────────────────────────────────────────

def _keys() -> list[Path]:
    if not KEYS_DIR.is_dir():
        die(f"找不到 {KEYS_DIR.relative_to(ROOT)}/。先跑 packages/scripts/keys.sh gen。")
    keys = sorted(KEYS_DIR.glob("*.rsa.pub"))
    if not keys:
        die(f"{KEYS_DIR.relative_to(ROOT)}/ 里没有 *.rsa.pub")
    return keys


def _check_repo(repo: str, arch: str) -> None:
    """先探一下仓库。不然 apk 的报错会绕一圈才说清楚"仓库拿不到"。

    ★ 这个检查跑在【宿主】上，而 REPO 里的 host.docker.internal 只在容器里
      有意义，所以探测时把它换回 localhost。
    """
    url = f"{repo.replace('host.docker.internal', 'localhost')}/{arch}/APKINDEX.tar.gz"
    proc = subprocess.run(
        ["curl", "-fsS", "-o", os.devnull, "--max-time", "15", url],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return

    hint = ""
    if arch == "x86_64":
        hint = ("\n       x86_64 的仓库可能还没发布——packages 那边跑一次 release "
                "workflow 并勾上 deploy。")
    die(
        f"拿不到 {url}{hint}\n"
        f"       本地起一个：cd packages/var/repo && python3 -m http.server 18080\n"
        f"       然后用 --repo {LOCAL_REPO}"
    )


# ── 装 ────────────────────────────────────────────────────────────────────

def _install(pkgs: list[str], repo: str, root_dir: Path, out_dir: Path,
             tar_name: str, name: str, arch: str) -> tuple[int, str]:
    """在容器里装根 + 打 tar。返回 (退出码, 合并后的输出)。"""
    #
    # 没有 --platform：组装【不执行】被装的二进制，apk --root 只是解包，所以
    # 在 aarch64 上装 x86_64 的包是安全的（Tar 由容器自己的 GNU tar 打）。
    #
    # ★ 这条前提是"我们打的包没有 install script"。哪天有包需要在目标根里跑
    #   脚本（apk 会 chroot 进去执行），这里就必须加 --platform 并把 qemu 拉进来。
    cmd = [
        "docker", "run", "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "-v", f"{out_dir}:/out",
        "-v", f"{root_dir}:/newroot",
        "-v", f"{KEYS_DIR}:/keys:ro",
        "-e", f"REPO={repo}",
        "-e", f"PKGS={' '.join(pkgs)}",
        "-e", f"TAR_NAME={tar_name}",
        "-e", f"TAR_MTIME={TAR_MTIME}",
        # 组成清单要的四样：它是什么、给哪个架构、从哪儿装的、什么时候装的。
        # 时间从固定的 TAR_MTIME 推出来——不用"现在"，否则同一份输入建出两个
        # 不同的根（0005）。
        "-e", f"FLAVOR={name}",
        "-e", f"ARCH={arch}",
        "-e", "BUILD_DATE=" + time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime(int(TAR_MTIME.lstrip("@")))),
        "-e", f"HOST_UID={os.getuid()}",
        "-e", f"HOST_GID={os.getgid()}",
        TOOL_IMAGE, "sh", "-c", CONTAINER_SCRIPT,
    ]
    return run_streaming(cmd)


def build(name: str, arch_requested: str | None = None, repo: str = DEFAULT_REPO,
          probe: bool = False, skip_missing: bool = False) -> Path:
    v = flavor.load(name)
    arch = resolve_arch(arch_requested)
    pkgs = list(v["packages"])

    out_dir = VAR_DIR / name
    root_dir = out_dir / f"rootfs-{arch}"
    tar_path = out_dir / f"{name}-{arch}.tar.gz"

    log("风味", f"{name}  [{v['_kind']}]  {len(pkgs)} 个包")
    log("仓库", repo)
    log("架构", arch)
    print(f"       根:  {root_dir.relative_to(ROOT)}/")
    print(f"       tar: {tar_path.relative_to(ROOT)}")
    print()

    _check_repo(repo, arch)
    keys = _keys()
    log("公钥", f"{len(keys)} 个（会烤进交付物的 /etc/apk/keys/）")

    out_dir.mkdir(parents=True, exist_ok=True)
    root_dir.mkdir(parents=True, exist_ok=True)

    print()
    log("装根", f"{len(pkgs)} 个包，从 {repo}")
    rc, out = _install(pkgs, repo, root_dir, out_dir, tar_path.name, name, arch)

    skipped: list[str] = []
    if rc != 0:
        missing = MISSING_RE.findall(out)
        if missing and skip_missing:
            print()
            warn(f"仓库里没有：{' '.join(missing)}")
            warn("按 --skip-missing 跳过它们。这个根是【故意不完整】的，")
            warn("不能当交付物——它能回答的只有「剩下的部分装得对不对」。")
            skipped = missing
            pkgs = [p for p in pkgs if p not in missing]
            print()
            log("重试", f"{len(pkgs)} 个包")
            rc, out = _install(pkgs, repo, root_dir, out_dir, tar_path.name, name, arch)

        if rc != 0:
            lines = ["装根失败（退出码 %d）" % rc]
            if missing:
                lines.append(f"       apk 说仓库里没有这些包：{' '.join(missing)}")
                lines += [
                    "       两种可能：",
                    "         · 包建出来了但没打包发布 —— packages 那边跑一次 release workflow（勾 deploy）",
                    "         · 还没有配方 —— 那要先在 packages 里写",
                    "       过渡期想先跑通别的：加 --skip-missing。",
                ]
            else:
                lines.append("       完整输出见上面。")
            die("\n".join(lines))

    if not tar_path.is_file():
        die(f"装完了，但没找到 {tar_path.relative_to(ROOT)}")

    size = tar_path.stat().st_size
    print()
    log("完成", f"{tar_path.relative_to(ROOT)}  ({size / 1048576:.1f} MB)")
    log("根", f"{root_dir.relative_to(ROOT)}/  —— 给后面的步骤当输入（建盘、起 QEMU）")
    if skipped:
        warn(f"这个 tar 里【没有】：{' '.join(skipped)}")

    if probe:
        _probe(name, arch, tar_path)

    return tar_path


# ── 探测 ──────────────────────────────────────────────────────────────────

def _probe(name: str, arch: str, tar_path: Path) -> None:
    """把它导成一个临时镜像，跑一遍容器里那套检查（`verify.IN_IMAGE_SCRIPT`）。

    为什么用这个法子：**"装成功、tar 成"都不能说明里面的东西能跑**。而
    那段脚本已经在检查那件真正要命的事——关键二进制的动态链接解析得了吗
    （`ldd` 里有没有 'not found'）。gawk 少 libmpfr 那次，就是这类检查才抓得到的。

    ★ 复用同一份，不另写一份：`pantsu verify` 喂给 `docker run` 的 stdin 也是
      它。一份 payload，两处用。
    """
    tag = f"{PROBE_IMAGE}:{name}-{arch}"
    print()
    log("探测", f"导入成临时镜像 {tag}（不是交付物，不会推）")
    # 和容器交付物同一个形状：有 CMD、有 ENV。这样探测的结论才算数。
    run(["docker", "import",
           "--platform", f"linux/{arch}",
         "-c", "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
         "-c", "ENV LANG=C.UTF-8",
         "-c", 'CMD ["/bin/sh"]',
         str(tar_path), tag])

    log("探测", "在镜像里跑那套检查（和 pantsu verify 同一份）")
    # --platform：被探测的根可能是【另一个架构】（比如在 Mac 上验 x86_64 的根）。
    # 不写的话 docker 会按宿主架构给它打标签，运行时报 exec format error。
    rc, _ = run_streaming(["docker", "run", "--rm", "-i",
                             "--platform", f"linux/{arch}", tag, "/bin/sh"],
                          stdin_text=verify.IN_IMAGE_SCRIPT)
    if rc != 0:
        die(
            f"探测没过（退出码 {rc}）——这个根不能当交付物。\n"
            f"       进去自己看：docker run -it --rm {tag}"
        )
    log("探测", f"通过（{tag}）")
