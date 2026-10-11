"""根文件系统拼装：把若干包的 payload 合成一个完整系统，打成 tar。

合并与打包都在容器里做，理由同 docs/决策记录/0002：macOS 的 APFS 默认
大小写不敏感，在宿主机上合并会**静默**吞掉只差大小写的文件——产出一个
看起来正常、实际上缺了东西的 rootfs。这种错误没有任何报错，只有等到
运行时才暴露。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .common import BUILDENV_IMAGE, BUILD_DIR, OUTPUT_DIR, die, log
from .deps import load_host_provides, dep_closure, dep_payload, source_date_epoch
from .spec import load_spec, recipe_of

# 拼装时保留哪些语言的 .mo 消息文件。
#
# 一个完整的 base 带着 62 种语言、29.6 MB 的翻译，而我们要的是中文。
# 只留 zh_CN / zh_TW 之后这部分降到 2 MB——省 94%。
#
# 为什么在【拼装】时筛，而不是像 Debian 那样给每个包拆 -lang 子包：包里
# 什么都还在，只是拼出来的系统不带。所以这是可逆的，以后想放进去改一行
# 就行，也不用给 23 个配方挨个加规则。
#
# 英文不需要 .mo——gettext 取不到翻译时会回落到程序里写死的原文。
KEEP_LOCALES = ("zh_CN", "zh_TW")


def resolve_set(packages: list[str], arch: str) -> list[str]:
    """把点名的包展开成完整集合：依赖在前，被依赖者在前。

    两点与「构建一个包」不同：

      - **只跟 runtime 依赖**。rootfs 是最终系统，不该包含编译器、make
        这类只在构建时需要的东西。
      - **不看 bootstrap/provides.txt**。那份列表说的是「构建环境提供什么」，
        和「最终系统里应该有什么」是两回事。glibc 在基线的意思是构建时
        不必处理它，不是说成品系统里不需要 C 库。

    同名包只出现一次。顺序很重要——后面的包会覆盖前面同路径的文件，
    所以必须先铺依赖、再铺依赖它的人。
    """
    order: list[str] = []
    seen: set[str] = set()

    for name in packages:
        recipe = recipe_of(name)
        if recipe is None:
            die(f"不认识的包: {name}")
        spec = load_spec(recipe)
        # 点名的可能是子包（glibc-dev）。dep_closure 返回的是【依赖】，不含
        # 自身，所以要把主包和子包都补上；子包依赖主包，顺序也就对了。
        for dep in dep_closure(spec, arch, set(), kinds=("runtime",)) + [recipe, name]:
            if dep not in seen:
                seen.add(dep)
                order.append(dep)
    return order


def assemble(packages: list[str], arch: str, output: Path | None = None) -> tuple[Path, list[str]]:
    """拼装并打包，返回 (tar 路径, 参与拼装的包列表)。"""
    order = resolve_set(packages, arch)

    rels: list[str] = []
    for name in order:
        _payload, rel = dep_payload(name, arch)
        rels.append(rel)

    out = output or (OUTPUT_DIR / arch / f"yojigenos-rootfs-{arch}.tar.gz")
    out.parent.mkdir(parents=True, exist_ok=True)

    # rootfs 的时间戳：参与拼装的包里最晚的那个。
    #
    # 它是一堆包的合成物，所以没有一个「自己的」时间戳可谈；取最大值既
    # 稳定（同样的输入算出同样的值），又满足时间戳不该晚于任何输入的直觉。
    # 必须传真正的 host_provides，不能传空集。
    #
    # source_date_epoch 跟的是【构建】依赖链（构建环境会改变产物字节），而
    # 那条链里有真实的自举环：glibc 的构建依赖 make，make 的运行时依赖 glibc。
    # 这个环平时由 bootstrap/provides.txt 切开——它声明「这一环由宿主提供，不用
    # 自己解析」。传空集就等于把切口堵上，dep_closure 会直接报「依赖成环」。
    #
    # 而且这不只是崩不崩的问题：传空集就算侥幸不环，算出来的值也会和各包
    # 自己构建时用的不一样——同一个包的 SOURCE_DATE_EPOCH 在两处必须一致，
    # 否则重复构建产不出相同字节。
    host_provides = load_host_provides()
    # order 里可能有子包名，它们继承母配方的 date——所以先落回配方。
    epoch = max(source_date_epoch(load_spec(recipe_of(n) or n), arch, host_provides)
                for n in order)

    # 语言筛选：只留下 KEEP_LOCALES 里列的那些。
    #
    # 这是拼装期的事，不是构建期——包里 62 种语言一个不少，只是拼出来的
    # 系统不带。所以改这个列表不需要重编任何东西。
    keep_names = " ".join(f"! -name {loc}" for loc in KEEP_LOCALES)
    prune = (
        "if [ -d /rootfs/usr/share/locale ]; then\n"
        "  loc_before=$(du -sk /rootfs/usr/share/locale | cut -f1)\n"
        "  find /rootfs/usr/share/locale -mindepth 1 -maxdepth 1 -type d \\\n"
        f"    {keep_names} -exec rm -rf {{}} +\n"
        "  loc_after=$(du -sk /rootfs/usr/share/locale | cut -f1)\n"
        f'  echo "[语言] 保留 {" ".join(KEEP_LOCALES)}'
        '，$(( loc_before - loc_after )) KB 的翻译没有装进去"\n'
        "fi\n"
    )

    script = (
        "set -euo pipefail\n"
        "mkdir -p /rootfs\n"
        + "".join(f'cp -a "/pkgs/{rel}/payload/." /rootfs/\n' for rel in rels)
        # payload 是在 macOS 的绑定挂载上生成的，属主被虚拟化成了宿主用户
        # （实测 501:20）。成品系统的文件必须是 root:root。
        + "chown -R 0:0 /rootfs\n"
        + prune
        # tar 参数按可复现构建的标准来（见决策记录 0005）：
        #   --sort=name    文件顺序固定——否则取决于文件系统的返回顺序
        #   --mtime        所有成员的时间戳统一，不再各自带着构建时刻
        #   --owner/--group 属主记成 0:0
        #   --pax-option   去掉 PAX 头里的 atime/ctime；它们每次构建都不同
        #
        # 压缩写成 --use-compress-program="gzip -9n" 而不是 GZIP 环境变量：
        # gzip 已把那个变量标记为弃用（会打警告）。
        #
        # -n 本身是防御性的。gzip 头里记的是「输入文件的 mtime 和文件名」，
        # 而从管道读时两者都不存在（tar -z 就是管道），所以实测加不加它结果
        # 都一样。写出来是为了不依赖那个隐含行为。
        + "tar -C /rootfs -cf /out/rootfs.tar.gz \\\n"
        '    --use-compress-program="gzip -9n" \\\n'
        "    --sort=name --numeric-owner --owner=0 --group=0 \\\n"
        f"    --mtime=@{epoch} \\\n"
        "    --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \\\n"
        "    .\n"
    )

    proc = subprocess.run([
        "docker", "run", "--rm",
        "-v", f"{OUTPUT_DIR / arch}:/pkgs:ro",
        "-v", f"{BUILD_DIR}:/out",
        f"{BUILDENV_IMAGE}:{arch}",
        "bash", "-c", script,
    ])
    if proc.returncode != 0:
        die(f"拼装失败（退出码 {proc.returncode}）")

    staging = BUILD_DIR / "rootfs.tar.gz"
    if not staging.is_file():
        die("拼装结束但没找到 rootfs.tar.gz")
    out.unlink(missing_ok=True)
    shutil.move(str(staging), out)
    return out, order
