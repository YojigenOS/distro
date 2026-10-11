#!/bin/sh
# 双击就能在终端窗口里启动 YojigenOS
#
# macOS 会把 .command 文件在 Terminal 里打开并执行——所以这个文件双击可用，
# 也是给「我想亲眼看看自己的系统」准备的最短路径。
#
# 为什么是 --no-prepare：rootfs 和 initramfs 已经留在 output/<arch>/.vm/ 里了，
# 从零准备要两分钟，而这里只想尽快看到内核起来。
# 换了包之后要重做一次准备，去掉那个开关跑 scripts/run-vm.sh 就行。

cd "$(dirname "$0")/.."

cat <<'BANNER'

  ┌────────────────────────────────────────────────────────────┐
  │                                                            │
  │   YojigenOS —— 在 QEMU 里启动                              │
  │                                                            │
  │   · 内核是 7.2.9，用我们自己的 gcc 16.2.0 编的             │
  │   · rootfs 里的每个包都是从源码一步步建出来的              │
  │   · 启动大约 1 秒（Apple Silicon 上走 hvf 硬件加速）       │
  │                                                            │
  │   看到 sh-5.3# 就是进系统了，可以随便敲命令。              │
  │                                                            │
  │   退出：按 Ctrl-A 然后按 X                                 │
  │                                                            │
  └────────────────────────────────────────────────────────────┘

  试试这些：

      uname -a                看内核版本（注意 gcc 那一段）
      cat /etc/os-release     看系统身份
      ls /                    看根目录
      echo "从零建出来的" | sed 's/零/源码/'
      seq 1 10 | gawk '{s+=$1} END{print "和 =", s}'
      head -3 /proc/meminfo   看内存

BANNER

sleep 2
exec ./scripts/run-vm.sh --no-prepare
