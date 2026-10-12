#!/bin/sh
# 组装一个交付物：从 apk 仓库装出一个根，打成 tar，导入成容器镜像
#
# 用法：
#     ./scripts/assemble.sh base
#     ./scripts/assemble.sh base --repo http://localhost:18080
#     ./scripts/assemble.sh base --repo https://repo.os.yojigen.cn --tag edge
#
# ── 组装是什么 ────────────────────────────────────────────────────────────
#
# 就三步，而且中间那步【不是我们自己实现的】：
#
#     ① 读风味        variants/<名>.toml → 一个包名列表
#     ② 装            apk add --root <根> --repository <URL> <那些包>
#                     ★ apk 自己做：依赖解析 · 冲突检测 · 下载 · 校验 · 解包 · 记账
#     ③ 变成交付物    tar → docker import
#
# 【为什么用 Alpine 容器】
#
# 因为这台机器上没有 apk。而且 Alpine 的 apk 是 3.0，和我们打的包同代（v3）。
# 它是【借来的工具】——和 bootstrap/ 里那个 Debian 同一类。
#
# 注意：借的是【工具】，不是【包】。Alpine 自己的包是 musl 链接的，一个都用不了
# （决策记录 0006 那句话）。
#
# 【为什么装完还要写 /etc/apk/repositories】
#
# 那样镜像里的 apk 才知道去哪装东西。base 是运行时，用户不一定用它装软件，
# 但把仓库地址写进去是零成本的，而且让镜像【自己说得清它是从哪来的】。

set -eu

cd "$(dirname "$0")/.."

FLAVOR=""
# ★ 默认指向宿主上的临时服务。注意容器里的 localhost 是【容器自己】——
#   要访问宿主得用 host.docker.internal（Docker Desktop 提供，Linux 上靠
#   下面那个 --add-host 参数补）。
REPO="http://host.docker.internal:18080"
ARCH=aarch64
TAG=""
NAME=""

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --arch) ARCH="$2"; shift 2 ;;
        --tag)  TAG="$2";  shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 1 ;;
        *) FLAVOR="$1"; shift ;;
    esac
done

[ -n "$FLAVOR" ] || { echo "用法: $0 <风味> [--repo URL] [--tag TAG]" >&2; exit 1; }

SPEC="variants/$FLAVOR.toml"
[ -f "$SPEC" ] || { echo "找不到 $SPEC" >&2; exit 1; }

[ "$(uname -m)" = "arm64" ] && [ "$ARCH" = "aarch64" ] || true
TAG="${TAG:-edge}"
NAME="${NAME:-yojigenos/$FLAVOR}"
OUT="$PWD/var/$FLAVOR"
mkdir -p "$OUT"

# ── ① 读风味 ──────────────────────────────────────────────────────────────
#
# 这是 variants/*.toml 唯一的消费者。而它是【唯一出处】——那张包列表只此一份。
PKGS=$(python3 - "$SPEC" <<'PY'
import sys, tomllib, pathlib
v = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["variant"]
print(" ".join(v["packages"]))
PY
)
[ -n "$PKGS" ] || { echo "$SPEC 里没有包" >&2; exit 1; }

COUNT=$(printf '%s\n' "$PKGS" | wc -w | tr -d ' ')
echo
echo "  风味:    ${FLAVOR}（$COUNT 个包）"
echo "  仓库:    $REPO"
echo "  架构:    $ARCH"
echo "  输出:    var/$FLAVOR/$FLAVOR-$ARCH.tar.gz"
echo "  镜像:    $NAME:$TAG"
echo

# ── 仓库可达性 ────────────────────────────────────────────────────────────
#
# 先探一下。不然 apk 的报错会绕一圈才说清楚"仓库拿不到"。
# ★ 这个检查跑在【宿主】上，而 REPO 里的 host.docker.internal 只在容器里有意义。
#   所以探测时把它换回 localhost。容器里那个名字由 --add-host 负责解析。
HOST_REPO=$(printf '%s' "$REPO" | sed 's|host\.docker\.internal|localhost|')
if ! curl -fsS -o /dev/null --max-time 15 "$HOST_REPO/$ARCH/APKINDEX.tar.gz"; then
    echo "  拿不到 $HOST_REPO/$ARCH/APKINDEX.tar.gz" >&2
    echo "  本地起一个：cd packages/var/repo && python3 -m http.server 18080" >&2
    exit 1
fi
echo "  索引可读 ✓"

# ── ②③ 装 + 打 tar（都在容器里，一次做完）────────────────────────────────
#
# 【为什么 tar 也在容器里打】
#
# macOS 上是 bsdtar，和 GNU tar 的默认行为不同（owner、mtime、排序）。
# 放进容器里打，这件事就只有一个实现，在哪儿跑都一样——0005 要的确定性。
#
# 【为什么要 --allow-untrusted】
#
# 本地还没签名（repo.sh 支持 --sign-key，密钥还没生成）。发布时签了就不需要它。
echo
echo "  [1/2] 装 + 打 tar（容器里）"
docker run --rm \
    --add-host=host.docker.internal:host-gateway \
    -v "$OUT:/out" \
    alpine:latest sh -c "
        set -e

        # Alpine 自带的 tar 是 BusyBox 的，不认 --sort=name / --mtime。
        # 0005 要的确定性靠 GNU tar 的那几个参数，所以先装它。
        apk add --no-cache tar >/dev/null 2>&1

        mkdir -p /newroot

        # ★ 仓库根是【不带架构】的那层——apk 自己会加 /$ARCH
        apk add --allow-untrusted \\
            --repository $REPO \\
            --root /newroot \\
            --initdb \\
            $PKGS 2>&1 | sed 's/^/      /'

        # 仓库地址写进根里——镜像要说得清自己是从哪来的。
        # ★ 这一步必须在 apk add 之后：它才建出 /etc/apk。
        mkdir -p /newroot/etc/apk
        echo '$REPO' > /newroot/etc/apk/repositories

        echo
        echo '      ── 装出来的根 ──'
        echo \"        包数: \$(ls /newroot/lib/apk/db/installed >/dev/null 2>&1 && apk --root /newroot info 2>/dev/null | wc -l | tr -d ' ')\"
        echo \"        文件: \$(find /newroot/usr -type f 2>/dev/null | wc -l | tr -d ' ') 个\"

        # ★ 确定性：排序 + 固定 owner + 固定时间戳
        tar --sort=name --owner=0 --group=0 --numeric-owner \\
            --mtime=\"@1767225600\" \\
            -C /newroot -czf /out/$FLAVOR-$ARCH.tar.gz .
        echo
        echo \"      tar: \$(stat -c%s /out/$FLAVOR-$ARCH.tar.gz) 字节\"
    "

# ── ④ 导入成 docker 镜像 ─────────────────────────────────────────────────
echo
echo "  [2/2] 导入成镜像 $NAME:$TAG"
docker import \
    -c "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    -c "ENV LANG=C.UTF-8" \
    -c "LABEL org.yojigenos.flavor=$FLAVOR" \
    "$OUT/$FLAVOR-$ARCH.tar.gz" \
    "$NAME:$TAG" > /dev/null

SIZE=$(docker image inspect "$NAME:$TAG" --format '{{.Size}}')
echo "      ✓ $(docker images "$NAME:$TAG" --format '{{.Repository}}:{{.Tag}}  {{.Size}}')"

# ── 报告 ──────────────────────────────────────────────────────────────────
echo
echo "  试着用它："
echo "      docker run --rm $NAME:$TAG /usr/bin/bash -c 'echo 你好，YojigenOS'"
echo "      docker run --rm $NAME:$TAG /usr/bin/gawk 'BEGIN{print 1+1}'"
echo
