#!/bin/sh
# 组装一个容器交付物：从 apk 仓库装出一个根，打成 tar，导入成容器镜像
#
# 用法：
#     ./scripts/assemble.sh base
#     ./scripts/assemble.sh base --repo http://localhost:18080
#     ./scripts/assemble.sh base --repo https://repo.os.yojigen.cn --tag edge
#
# ── 它现在只做两件事 ─────────────────────────────────────────────────────
#
#     ① 装根 + 打 tar     → 交给 pantsu（pantsu/pantsu rootfs）
#     ② 导入成容器镜像     → docker import
#
# 第①步原来整段写在这个文件里（在容器里 apk add --root，再 tar）。搬走的理由
# 是它和"建一张磁盘镜像"的前半段**逐字相同**——同一个东西两个出处，只有一个
# 会被改。现在它是 pantsu 里的一份实现，单独跑 `pantsu rootfs <风味>` 也行。
#
# 为什么解包和打包都要在容器里做、为什么公钥必须先放进去、为什么 tar 要那几个
# 参数——那些理由跟着实现走了，在 pantsu/pantsulib/root.py 里，这里不再抄一遍。
#
# ── 它只认一个仓库 URL ───────────────────────────────────────────────────
#
# 不 checkout 配方，不读 packages/var/output/。最要命的那条理由见 README：
# 那会让"发布的包"和"用来组装的包"可以是不同的东西，而且没人会发现。

set -eu

cd "$(dirname "$0")/.."

FLAVOR=""
# ★ 默认指向宿主上的临时服务（本地调试）。注意容器里的 localhost 是【容器
#   自己】——要访问宿主得用 host.docker.internal，pantsu 会加 --add-host。
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
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 1 ;;
        *) FLAVOR="$1"; shift ;;
    esac
done

[ -n "${FLAVOR}" ] || { echo "用法: $0 <风味> [--repo URL] [--tag TAG]" >&2; exit 1; }
[ -f "variants/${FLAVOR}.toml" ] || { echo "找不到 variants/${FLAVOR}.toml" >&2; exit 1; }

TAG="${TAG:-edge}"
NAME="${NAME:-yojigenos/${FLAVOR}}"
OUT="$PWD/var/${FLAVOR}"
TAR="${OUT}/${FLAVOR}-${ARCH}.tar.gz"

# ── ① 装根 + 打 tar ──────────────────────────────────────────────────────
./pantsu/pantsu rootfs "${FLAVOR}" --repo "${REPO}" --arch "${ARCH}"

[ -f "${TAR}" ] || { echo "pantsu 跑完了，但没有 ${TAR}" >&2; exit 1; }

# ── ② 导入成 docker 镜像 ─────────────────────────────────────────────────
echo
echo "  [2/2] 导入成镜像 ${NAME}:${TAG}"
# ★ CMD 必须显式给。docker import 不像 Dockerfile 那样有默认值——不给的话
#   `docker run <镜像>` 直接报 "no command specified"，而那正是最自然的用法
#   （进一个 shell 看看）。
#
#   /bin/sh 是基础镜像的惯例（alpine、debian:slim 都是）。我们的 /bin/sh 是
#   dash，所以进去就是个 POSIX shell。
# ── 标签 ─────────────────────────────────────────────────────────────────
#
# ★ 命名空间用【反写域名】，所以根必须是【我们真的拥有的域名】。
#
#   原来是 `org.yojigenos.…`——那声明的是 yojigenos.org，而那个域名不是我们的。
#   真的是 yojigen.cn，所以根是 `cn.yojigen` ✓。
#
#   这一层比"连字符"重要：`org.yojigenos` 和 `org.yojigen-os` 都合法、都能用，
#   但两个都指向一个不存在的域名。合法不等于对。
#
#   下面是两大组：
#     ① `org.opencontainers.image.*` —— OCI 官方的标准标签。工具【真的会读】它们
#        （docker inspect、镜像扫描器、供应链工具）。写了别人就能知道这个镜像是
#        从哪个 commit、什么时候、由什么建的。
#     ② `cn.yojigen.os.*` —— 我们自己的。标准组没有的地方（风味、架构）才用它。
#
#   ★ 0003 里计划了 recipe-commit / built / packages 三个，实际一直只实现了 flavor
#     一个。这里把 built 补上了（就是标准组的 created），另外两个见下。

BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# revision：这份镜像是从哪个 commit 建的。
#
# ★ 拿不到就【不写这个标签】，而不是写个占位符。一个说"我不知道"的标签比一个
#   写着 "unknown" 的标签有用——后者看起来像"这个 commit 叫 unknown"。
REVISION=""
if command -v git >/dev/null 2>&1; then
    REVISION=$(git -C "$(dirname "$0")/.." rev-parse HEAD 2>/dev/null || true)
fi

LABEL_ARGS=(
    # ★ 值里有空格就必须加引号。`docker import -c "LABEL ..."` 会【按空格切】，
    #   不加引号报的是：
    #       Error response from daemon: Syntax error - can't find = in "OS"
    #   而那句错误只提到 OS，看不出是 title 那一行长什么样。实测过。
    -c "LABEL org.opencontainers.image.title=\"Yojigen OS ${FLAVOR}\""
    -c "LABEL org.opencontainers.image.version=${TAG}"
    -c "LABEL org.opencontainers.image.created=${BUILT}"
    -c "LABEL org.opencontainers.image.source=https://github.com/YojigenOS/distro"
    -c "LABEL org.opencontainers.image.url=https://os.yojigen.cn"
    -c "LABEL cn.yojigen.os.flavor=${FLAVOR}"
    -c "LABEL cn.yojigen.os.arch=${ARCH}"
    -c "LABEL cn.yojigen.os.built=${BUILT}"
)
[ -n "$REVISION" ] && LABEL_ARGS+=(-c "LABEL org.opencontainers.image.revision=${REVISION}")

docker import \
    -c "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    -c "ENV LANG=C.UTF-8" \
    -c "CMD [\"/bin/sh\"]" \
    "${LABEL_ARGS[@]}" \
    "${TAR}" \
    "${NAME}:${TAG}" > /dev/null

echo "  标签:    cn.yojigen.os.flavor=${FLAVOR}  arch=${ARCH}"
echo "           org.opencontainers.image.created=${BUILT}"
[ -n "$REVISION" ] && echo "           org.opencontainers.image.revision=${REVISION:0:12}"

docker images "${NAME}:${TAG}" --format '      ✓ {{.Repository}}:{{.Tag}}  {{.Size}}'

# ── 报告 ─────────────────────────────────────────────────────────────────
echo
echo "  试着用它："
# ★ docker 的语义：`docker run <镜像> <参数...>` 是【替换 CMD】，不是往 CMD
#   后面追加。所以 `docker run <镜像> -c 'echo hi'` 是错的——docker 会把 `-c`
#   当成可执行文件。要跑一条命令得把 shell 写出来。
# ★ base 不带 bash（/bin/sh 是 dash），提示别教人用不存在的东西。
echo "      docker run --rm ${NAME}:${TAG} sh -c 'echo 你好，Yojigen OS'"
echo "      docker run --rm ${NAME}:${TAG} /usr/bin/gawk 'BEGIN{print 1+1}'"
echo "      docker run -it --rm ${NAME}:${TAG}          # 进去看看（默认进 /bin/sh）"
echo
