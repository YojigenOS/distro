#!/bin/bash
# 验证一个组装出来的交付镜像。
#
# 用法：
#     ./scripts/verify.sh yojigenos/base:edge
#     ./scripts/verify.sh yojigenos/base:edge --flavor base
#
# ── 为什么需要它 ──────────────────────────────────────────────────────────
#
# assemble.sh 的产物是【交付物】。而交付物只有【跑起来】才知道对不对。
#
# 实测过这件事：base 第一次组装出来时，"装成功、tar 成、import 成"三样都对，
# 但 `docker run <镜像>` 直接报 "no command specified"——因为 docker import
# 不像 Dockerfile 那样有默认 CMD。而那三个"成功"一个都没提这件事。提示里还
# 写着 `docker run … /usr/bin/bash`，而 base 的 /bin/sh 换成 dash 之后就不带
# bash 了。同样是"看起来都成功"。
#
# 所以验证必须在【镜像里】跑命令，而不是检查宿主上的文件。
#
# ── 分两层 ────────────────────────────────────────────────────────────────
#
#   这一层（宿主）    镜像的元数据：有没有默认 CMD、起不起得来
#   里面那层          scripts/verify-in-image.sh —— 通过 stdin 喂进去跑
#
# ★ 里面那层用 POSIX sh 写，因为它要拿去给被验证的镜像跑——而那个镜像里
#   可能只有 dash。用 bash 写就成了"要求被验证的东西里先有 bash"。

set -uo pipefail

cd "$(dirname "$0")/.."

IMG="${1:-}"
FLAVOR=""
shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --flavor) FLAVOR="$2"; shift 2 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

[ -n "$IMG" ] || { echo "用法: $0 <镜像> [--flavor <名>]" >&2; exit 1; }

FAIL=0
ok()  { printf '  ✓ %s\n' "$1"; }
bad() { printf '  ✗ %s\n' "$1"; FAIL=$((FAIL + 1)); }

echo
echo "  验证 $IMG${FLAVOR:+（风味 ${FLAVOR}）}"

# ── ① 元数据：默认 CMD ────────────────────────────────────────────────────
CMD=$(docker image inspect "$IMG" --format '{{.Config.Cmd}}' 2>/dev/null)
if [ -z "$CMD" ] || [ "$CMD" = "[]" ]; then
    bad "没有默认 CMD —— 裸跑 \`docker run <镜像>\` 会报 no command specified"
else
    ok "默认 CMD: $CMD"
fi

# 风味标签和 --flavor 对不对得上。对不上说明传错了镜像。
LABEL=$(docker image inspect "$IMG" --format '{{index .Config.Labels "org.yojigenos.flavor"}}' 2>/dev/null)
if [ -n "$FLAVOR" ]; then
    if [ "$LABEL" = "$FLAVOR" ]; then
        ok "风味标签: $LABEL"
    else
        bad "风味标签是 '$LABEL'，但期望 '$FLAVOR' —— 传错镜像了？"
    fi
elif [ -n "$LABEL" ]; then
    ok "风味标签: $LABEL"
fi

# ── ② 起得来吗 ────────────────────────────────────────────────────────────
if docker run --rm --entrypoint /bin/sh "$IMG" -c 'exit 0' >/dev/null 2>&1; then
    ok "镜像起得来"
else
    bad "镜像起不来"
    echo
    echo "  失败 $FAIL 项，后面的检查没有意义。"
    exit 1
fi

# ── ③ 在镜像里跑那套检查 ──────────────────────────────────────────────────
#
# ★ 不给命令，直接把脚本从 stdin 喂进去。
#   镜像的 CMD 是 /bin/sh，而 sh 没有参数时从 stdin 读脚本——所以这一条命令
#   同时验证了「默认 CMD 是对的」和「镜像里的用户态能用」。
echo
if docker run --rm -i "$IMG" < scripts/verify-in-image.sh; then
    IN_RC=0
else
    IN_RC=$?
fi

echo
if [ "$FAIL" = "0" ] && [ "$IN_RC" = "0" ]; then
    echo "  ✓ $IMG 验证通过"
    exit 0
fi
echo "  ✗ 验证没通过（镜像元数据 $FAIL 项，镜像内部退出码 ${IN_RC}）"
exit 1
