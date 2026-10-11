#!/bin/sh
# 在 QEMU 里启动 YojigenOS
#
# 用法：
#     scripts/run-vm.sh                     # aarch64，Apple Silicon 上是硬件加速的
#     ARCH=x86_64 scripts/run-vm.sh         # x86_64（模拟，慢很多）
#     MEM=2G scripts/run-vm.sh              # 给多点内存
#
# 它做四件事：
#
#     1. 用 skirt 把 rootfs 拼出来（包列表取自 variants/base.toml，加一个 /init）
#     2. 把 rootfs 打成 initramfs（cpio.gz）
#     3. 找回内核镜像
#     4. 一条 QEMU 命令启动
#
# 【为什么用 initramfs 而不是磁盘镜像】
#
# 内核把 initramfs 解开到内存里的 rootfs，然后 exec /init——不需要分区表、
# 不需要文件系统驱动、不需要引导器。这是「能开机」最短的一条路，而磁盘镜像
# 和 ISO 是另一种交付形式（决策记录 0007 把它们分开），需求不一样，以后再说。
#
# 【为什么 cpio 那步要进容器】
#
# 做 initramfs 要用 GNU cpio 的 newc 格式。macOS 自带的是 BSD cpio，参数和
# 行为都不一样。既然这台机器上一定有 Docker（skirt 就靠它），那就用它做——
# 比在宿主机上装一套 GNU 工具可靠。

set -eu

cd "$(dirname "$0")/.."

ARCH=${ARCH:-$(uname -m)}
[ "$ARCH" = "arm64" ] && ARCH=aarch64
MEM=${MEM:-1G}
FLAVOR=${FLAVOR:-base}

case "$ARCH" in
    aarch64) QEMU=qemu-system-aarch64; MACHINE=virt;    CONSOLE=ttyAMA0; CPU=host ;;
    x86_64)  QEMU=qemu-system-x86_64;  MACHINE=q35;     CONSOLE=ttyS0;   CPU=host ;;
    *) echo "不认识的架构: $ARCH" >&2; exit 1 ;;
esac

# 路径只该有一处：问驱动，不在这里抄一遍。
# （2026-10-11 把产物集中到 var/ 时，这里抄的那份没跟着改，冒烟测试当场报"缺产物"。）
eval "$(./skirt/skirt paths --shell)"

# --no-prepare：跳过拼 rootfs 和打 initramfs，直接用上次留下的。
#
# 它们的产物在 $OUTPUT_DIR/$ARCH/.vm/ 里，都在宿主上（不是 Docker 里的），
# 所以只要没换过包，复用是安全的——重复跑本来也会得到同一份字节。
# 从零准备大约要两分钟，跳过就只剩 QEMU 启动的那一秒。
PREPARE=1
for a in "$@"; do
    case "$a" in
        --no-prepare) PREPARE=0 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $a" >&2; exit 1 ;;
    esac
done

RUN=$OUTPUT_DIR/$ARCH/.vm
mkdir -p "$RUN"


INITRAMFS="$RUN/initramfs.cpio.gz"

if [ "$PREPARE" = 1 ]; then

# ---------------------------------------------------------------- 1. 包列表
#
# 从 variants/$FLAVOR.toml 读，而不是在这里再抄一份——两张列表一定会漂移，
# 而漂移的表现是「VM 里的系统比镜像里少一个包」这种很难查的现象。
echo "[1/4] 读风味 $FLAVOR 的包列表"
PACKAGES=$(python3 - "$FLAVOR" <<'PY'
import sys, tomllib, pathlib
v = tomllib.loads(pathlib.Path(f"variants/{sys.argv[1]}.toml").read_text())
print(" ".join(v["variant"]["packages"]))
PY
)

# /init 是启动的必需品，但它不属于任何一个「用户态风味」——容器版不需要它。
# 所以在这里加，而不是塞进 base.toml。
PACKAGES="$PACKAGES yojigenos-init"

# ---------------------------------------------------------------- 2. rootfs
echo "[2/4] 拼 rootfs"
ROOTFS="$RUN/rootfs.tar.gz"
./skirt/skirt rootfs --arch "$ARCH" --output "$ROOTFS" $PACKAGES

# ---------------------------------------------------------------- 3. initramfs
echo "[3/4] 打成 initramfs"
# 挂【目录】而不是单个文件。挂文件在 Docker Desktop / OrbStack 上有个已知的
# 坑：守护进程会把那个路径当成目录去 mkdir，然后报
#     mkdir .../rootfs.tar.gz: file exists
# 挂父目录没这个问题，而且 initramfs 要写回来，一次挂载两个方向都解决。
# cpio 从我们自己的产物里挂进去，而不是要求镜像里有它。
#
# 理由：run-vm.sh 是【开发脚本】，它不该逼着 base-devel 镜像带一个只有打包
# initramfs 才用到的工具。而 cpio 我们刚建出来了，挂一下就有。
# （也已经加进 variants/base-devel.toml——将来的镜像会自带，那时这几行可以删。）
CPIO=$(ls -d "$OUTPUT_DIR/$ARCH"/cpio-*/payload 2>/dev/null | head -1 || true)
if [ -z "$CPIO" ]; then
    echo "  ✗ 没找到 cpio。先建它：./skirt/skirt build cpio --arch $ARCH" >&2
    exit 1
fi
docker run --rm \
    -v "$RUN:/vm" \
    -v "$CPIO:/cpio:ro" \
    yojigenos/base-devel:"$ARCH" bash -c '
        set -e
        mkdir -p /root && tar -xzf /vm/rootfs.tar.gz -C /root
        # 内核要的核心前提：/init 存在且可执行。找不到时给一句人话，
        # 而不是等到 QEMU 里报 "Unable to open initial console"。
        if [ ! -x /root/init ]; then
            echo "  ✗ rootfs 里没有可执行的 /init —— yojigenos-init 没进包列表？" >&2
            exit 1
        fi
        cd /root
        # set -o pipefail：没有它，`find | cpio | gzip` 里 cpio 没找到也照样
        # 产出文件（退出码取自 gzip）。实测踩过：cpio 不存在，于是打出一个
        # 4 KB 的空 initramfs，QEMU 立刻退出，而且没有任何错误信息。
        export PATH=/cpio/usr/bin:$PATH
        set -o pipefail
        find . -print0 | cpio --null -o -H newc --quiet | gzip -9 > /vm/initramfs.cpio.gz

        # 光靠 pipefail 还不够：就算管道都成功，也可能打出个空包。直接量大小。
        SZ=$(stat -c %s /vm/initramfs.cpio.gz 2>/dev/null || echo 0)
        if [ "$SZ" -lt 100000 ]; then
            echo "  ✗ initramfs 只有 $SZ 字节 —— 这是空的。rootfs 没进去？" >&2
            exit 1
        fi
        echo "    initramfs: $(du -h /vm/initramfs.cpio.gz | cut -f1)"
    '

fi   # PREPARE

# ---------------------------------------------------------------- 4. 内核
echo "[4/4] 找内核"
KERNEL=$(ls -d "$OUTPUT_DIR/$ARCH"/linux-*/payload/boot/vmlinuz 2>/dev/null | head -1 || true)
if [ -z "$KERNEL" ]; then
    echo "  ✗ 没找到内核镜像。" >&2
    echo "    先建它：./skirt/skirt build linux --arch $ARCH" >&2
    exit 1
fi
echo "    内核: $KERNEL"

# ---------------------------------------------------------------- 启动
echo
echo "  启动 YojigenOS（${ARCH}）—— 退出 QEMU 按 Ctrl-A 然后 X"
echo
exec "$QEMU" \
    -machine "$MACHINE" \
    -cpu "$CPU" \
    -accel hvf \
    -m "$MEM" \
    -kernel "$KERNEL" \
    -initrd "$INITRAMFS" \
    -append "console=$CONSOLE rdinit=/init" \
    -nographic
