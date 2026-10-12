#!/bin/sh
# 在【交付镜像里面】跑的检查。
#
# ★ 这个脚本不是自己执行的 —— 它通过 stdin 喂给镜像：
#
#     docker run --rm -i <镜像> < scripts/verify-in-image.sh
#
#   镜像的默认 CMD 是 /bin/sh，而 sh 没有参数时从 stdin 读脚本。
#   所以这一条命令同时验证了两件事：镜像能起来，而且默认 CMD 是对的。
#
# 退出码 = 失败的项数。0 表示全过。
#
# ★ 为什么用 POSIX sh 写而不是 bash：
#   它就是拿去给【被验证的镜像】跑的，而那个镜像里可能只有 dash。
#   用 bash 写就变成"要求被验证的东西里先有 bash"——那是循环论证。

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
for c in ls cat cp mv rm mkdir rmdir ln chmod chown touch echo printf test tr \
         sed grep awk find xargs sort uniq head tail wc cut paste \
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
