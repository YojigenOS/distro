"""验证一个组装出来的交付镜像 —— 在镜像里真跑一遍，不是看文件对不对。

    pantsu verify yojigenos/base:edge
    pantsu verify yojigenos/base:edge --flavor base

## 为什么是【一个】子命令，而不是两个

`verify-in-image.sh` 从来不是给人敲的：它通过 stdin 喂给**被验证的那个镜像**，
而那个镜像里可能只有 dash。它没有"命令行用法"这回事——所以不该长成子命令
（长成子命令会让人以为能在宿主上跑它，而它在宿主上毫无意义）。它现在是这个
文件里的 `IN_IMAGE_SCRIPT`：一段 payload，**一份，两处用**（这里，以及
`rootfs --probe`）。

（2026-10-15 从 scripts/verify.sh + scripts/verify-in-image.sh 收进来的。
收的理由和别的脚本一样：它们是主流程，而主流程该在 pantsu 里。）

## 为什么需要它

组装出来的东西是【交付物】，而交付物只有【跑起来】才知道对不对。

实测过这件事：base 第一次组装出来时，"装成功、tar 成、import 成"三样都对，
但 `docker run <镜像>` 直接报 "no command specified"——因为 docker import 不像
Dockerfile 那样有默认 CMD。而那三个"成功"一个都没提这件事。提示里还写着
`docker run … /usr/bin/bash`，而 base 的 /bin/sh 换成 dash 之后就不带 bash 了。
同样是"看起来都成功"。

**所以验证必须在【镜像里】跑命令，而不是检查宿主上的文件。**

## 分两层

    这一层（宿主）    镜像的元数据：有没有默认 CMD、风味标签对不对、起不起得来
    里面那层          IN_IMAGE_SCRIPT —— 通过 stdin 喂进去跑

★ 里面那层用 POSIX sh 写，因为它要拿去给【被验证的镜像】跑，而那个镜像里可能
  只有 dash。用 bash 写就成了"要求被验证的东西里先有 bash"——那是循环论证。
  同理：那段脚本不能 import 任何东西、不能假设有 python、不能用 bash 的扩展。
"""

from __future__ import annotations

import subprocess
import sys

from .common import die, run_streaming

#: 镜像里跑的那段检查。**POSIX sh**，通过 stdin 喂给镜像。
#:
#: ★ 它不是自己执行的：镜像的默认 CMD 是 /bin/sh，而 sh 没有参数时从 stdin
#:   读脚本。所以这条命令同时验证了两件事：镜像能起来、而且默认 CMD 是对的。
#:
#: ★ 退出码 = 失败的项数，0 表示全过。
IN_IMAGE_SCRIPT = """
FAIL=0

echo "──── ① /bin/sh 是谁 ────"
SH=$(readlink /bin/sh)
echo "    /bin/sh → ${SH}"
case "$SH" in
    dash|busybox|ash)
        # 这几类是"只实现 POSIX"，正是我们要的：写 #!/bin/sh 的脚本
        # 在我们这儿和在 Debian 上行为一致。
        ;;
    bash)
        echo "    ⚠ /bin/sh 指向 bash：写 #!/bin/sh 的脚本会偷偷用上 bash 扩展，"
        echo "      换到 Debian 就炸。不算失败，但要知道。"
        ;;
    *)
        echo "    ⚠ /bin/sh 指向 ${SH}，不认识它"
        ;;
esac

echo
echo "──── ② 常用命令都在吗 ────"
MISS=""
N=0
for c in ls cat cp mv rm mkdir rmdir ln chmod chown touch echo printf test tr \\
         sed grep awk find xargs sort uniq head tail wc cut paste \\
         diff cmp tar gzip gunzip xz unxz dd df du env id whoami; do
    N=$((N + 1))
    command -v "$c" >/dev/null 2>&1 || MISS="$MISS $c"
done
if [ -n "$MISS" ]; then
    echo "    ✗ 缺:$MISS"
    FAIL=$((FAIL + 1))
else
    echo "    ✓ $N 个全在（/usr/bin 下共 $(ls /usr/bin | wc -l) 个命令）"
fi

echo
echo "──── ③ 中文（核心承诺）────"
if locale -a 2>/dev/null | grep -q '^zh_CN'; then
    echo "    ✓ zh_CN locale 在"
else
    echo "    ✗ 没有 zh_CN locale"
    FAIL=$((FAIL + 1))
fi
printf "    输出中文:     "; echo "你好，世界 · 简体 · 繁體"
printf "    sed 替换中文: "; echo "我爱北京" | sed 's/北京/上海/'
S=$(echo "我爱北京" | sed 's/北京/上海/')
[ "$S" = "我爱上海" ] || { echo "    ✗ sed 处理中文不对"; FAIL=$((FAIL + 1)); }

echo
echo "──── ④ 动态链接 ────"
BAD=""
for b in /bin/sh /usr/bin/ls /usr/bin/sed /usr/bin/grep /usr/bin/awk; do
    [ -e "$b" ] || continue
    n=$(ldd "$b" 2>/dev/null | grep -c 'not found')
    [ "$n" != "0" ] && BAD="$BAD $b"
done
if [ -z "$BAD" ]; then
    echo "    ✓ 关键二进制的库都解析得了"
else
    echo "    ✗ 有缺失的库:$BAD"
    FAIL=$((FAIL + 1))
fi

exit "$FAIL"
"""

#: ★ OCI 标准组 + 我们自己那两个。这组是【工具真的会读】的（docker inspect、
#:   镜像扫描器、供应链工具），所以它值得被验证——一个"计划了但没写进去"的标签，
#:   和"写了但写错"的标签一样糟：外面的人看不到区别。
#:
#:   验的是【存在且非空】，不是具体值——值每次都不一样（时间、commit）。
#:   见决策记录 0003「二、补上 OCI 标准组」。
LABELS = (
    "org.opencontainers.image.title",
    "org.opencontainers.image.version",
    "org.opencontainers.image.created",
    "org.opencontainers.image.source",
    "org.opencontainers.image.revision",
    "cn.yojigen.os.flavor",
    "cn.yojigen.os.arch",
)


def _inspect(image: str, template: str) -> str:
    """问 docker 一个字段。

    拿不到（镜像不存在、标签没写）就返回空串——和 shell 里那句
    `2>/dev/null` 一样：把"没有"和"空"都变成同一个可判断的答案。
    """
    proc = subprocess.run(
        ["docker", "image", "inspect", image, "--format", template],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def verify(image: str, flavor: str | None = None) -> None:
    print()
    print(f"  验证 {image}" + (f"（风味 {flavor}）" if flavor else ""))

    # 先问一句它在不在。原来（shell 版）不查这个也能跑：CMD 是空的、七个标签
    # 全"缺"、最后起不来——四行报错都在说同一件事，而且都没说【真原因】。
    if not _inspect(image, "{{.Id}}"):
        die(f"没有这个镜像：{image}\n"
            f"       （先组装：pantsu assemble <风味>；或者 docker images 看看它在不在）")

    fails = 0

    def ok(msg: str) -> None:
        print(f"  ✓ {msg}")

    def bad(msg: str) -> None:
        nonlocal fails
        fails += 1
        print(f"  ✗ {msg}")

    # ── ① 元数据：默认 CMD ────────────────────────────────────────────────
    cmd = _inspect(image, "{{.Config.Cmd}}")
    if not cmd or cmd == "[]":
        bad("没有默认 CMD —— 裸跑 `docker run <镜像>` 会报 no command specified")
    else:
        ok(f"默认 CMD: {cmd}")

    # 风味标签和 --flavor 对不对得上。对不上说明传错了镜像。
    label = _inspect(image, '{{index .Config.Labels "cn.yojigen.os.flavor"}}')
    if flavor:
        if label == flavor:
            ok(f"风味标签: {label}")
        else:
            bad(f"风味标签是 '{label}'，但期望 '{flavor}' —— 传错镜像了？")
    elif label:
        ok(f"风味标签: {label}")

    missing = [k for k in LABELS if not _inspect(image, f'{{{{index .Config.Labels "{k}"}}}}')]
    if not missing:
        ok(f"OCI 标准标签: {len(LABELS)} 个都在")
    else:
        bad("缺这些标签: " + " ".join(missing))
        print("      （镜像标签是给工具读的，缺了外面看不出这个镜像从哪来）",
              file=sys.stderr)

    # ── ② 起得来吗 ────────────────────────────────────────────────────────
    if subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image, "-c", "exit 0"],
        capture_output=True, text=True,
    ).returncode == 0:
        ok("镜像起得来")
    else:
        bad("镜像起不来")
        print()
        print(f"  失败 {fails} 项，后面的检查没有意义。")
        sys.exit(1)

    # ── ③ 在镜像里跑那套检查 ──────────────────────────────────────────────
    #
    # ★ 不给命令，直接把脚本从 stdin 喂进去。
    #   镜像的 CMD 是 /bin/sh，而 sh 没有参数时从 stdin 读脚本——所以这一条命令
    #   同时验证了「默认 CMD 是对的」和「镜像里的用户态能用」。
    print()
    rc, _ = run_streaming(["docker", "run", "--rm", "-i", image],
                          stdin_text=IN_IMAGE_SCRIPT, indent="")

    print()
    if fails == 0 and rc == 0:
        print(f"  ✓ {image} 验证通过")
        return
    die(f"验证没通过（镜像元数据 {fails} 项，镜像内部退出码 {rc}）")


