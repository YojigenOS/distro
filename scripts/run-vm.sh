#!/bin/sh
# 在 QEMU 里启动 Yojigen OS —— 现在只是 pantsu vm 的一个薄壳
#
# 用法：
#     scripts/run-vm.sh                     # 宿主架构，内核 + initramfs 直启
#     ARCH=x86_64 scripts/run-vm.sh         # x86_64（跨架构 → 走 TCG，慢很多）
#     MEM=2G scripts/run-vm.sh              # 给多点内存
#     FLAVOR=minimal scripts/run-vm.sh      # 换风味
#
# 想走磁盘镜像那条路（和 TX 同构：SeaBIOS + virtio-blk + ttyS0）：
#     ./pantsu/pantsu img minimal && ./pantsu/pantsu vm minimal
#
# 想在脚本里断言"能不能启动"：
#     ./pantsu/pantsu vm minimal --serial-file /tmp/boot.log --timeout 25
#
# ── 为什么只剩一个壳 ─────────────────────────────────────────────────────
#
# 原来这 159 行做的四件事（拼 rootfs、打 initramfs、找内核、起 QEMU）现在都在
# pantsu/pantsulib/vm.py 里。搬走的一个实际好处：它不再向 packages 要 cpio 的
# 产物了——那是读 var/output/，这个仓库的规矩是不许。cpio 由容器里的 apk 现装，
# 和建盘那边一样：借工具，不借包。

set -eu
cd "$(dirname "$0")/.."

FLAVOR=${FLAVOR:-base}
ARCH=${ARCH:-}
MEM=${MEM:-1G}

ARGS="vm ${FLAVOR} --mem ${MEM} --mode direct"
if [ -n "${ARCH}" ]; then
    ARGS="${ARGS} --arch ${ARCH}"
fi

for a in "$@"; do
    case "$a" in
        --no-prepare) ;;   # 兼容老参数：initramfs 现在会自动复用，不用它了
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) ARGS="${ARGS} ${a}" ;;
    esac
done

# shellcheck disable=SC2086
exec ./pantsu/pantsu ${ARGS}
