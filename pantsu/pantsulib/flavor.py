"""风味：一份「这个镜像里有什么」的声明。

决策记录 0007 定下了模型，这里实现其中**现在就能做的那一半**：

    风味     = 一组包 + 一个用途        用什么
    交付形式 = 容器 / ISO / 磁盘 / 内核   怎么给

`kind` 说这个风味想要哪种形式。四种名字都是合法的，但**能产出交付物的只有
容器**——写 `disk` 不会被当成写错，只会在真去产出的时候报"还没实现"。

这个文件是从 scripts/variants.py.待改 搬过来的。搬的理由是它当时是【死代码】：
它校验得最全（文件名和 name 必须一致、packages 不能为空、kind 要在白名单里），
但没有任何地方 import 它；而真正在用的两处（assemble.sh 和 run-vm.sh）各自
内联了一段没有校验的读 toml 代码。同一个东西三个出处，只有死的那个是对的。

（2026-10-15：那两个脚本也收进来了——assemble → pantsulib/image.py，
run-vm → pantsulib/vm.py。这条"同一个东西几个出处"的账到这里才平。）
"""

from __future__ import annotations

import tomllib

from .common import VARIANTS_DIR, die, log

#: 0007 里定的四种交付形式。
KINDS = ("container", "iso", "disk", "kernel")

#: 现在【真能产出交付物】的形式。
#:
#: 2026-10-12：`disk` 加进来了（pantsulib/img.py）。iso / kernel 还是没有。
PRODUCIBLE = ("container", "disk")


def names() -> list[str]:
    if not VARIANTS_DIR.is_dir():
        return []
    return sorted(p.stem for p in VARIANTS_DIR.glob("*.toml"))


def load(name: str, _seen: tuple[str, ...] = ()) -> dict:
    """读一个风味定义，顺带校验。

    ★ 2026-10-22 加了 `extends`：一个风味可以在另一个风味的基础上【加】东西。

      成立的理由是 `server`：它就是 `minimal` 加上"sshd 自启"（硬化那一层还没
      做）。没有 `extends` 的话，那 46 行包列表要复制一份 —— 而复制的那份会漂移，
      漂移的症状是"server 少了 minimal 后来加的那个包"，很难在事后看出来。

      ★ 规则：`packages` 和 `services` 都是【父在前、自己在后】，去重保序；
        `kind` 没写就跟着父。而环路当场报错（不然是无限递归）。
    """
    if name in _seen:
        die(f"风味 extends 成环了：{' → '.join(_seen + (name,))}")

    path = VARIANTS_DIR / f"{name}.toml"
    if not path.is_file():
        known = names()
        hint = f"       现有的：{' '.join(known)}" if known else "       还没有任何风味定义"
        die(f"找不到风味 {name}（{path.relative_to(VARIANTS_DIR.parent)}）\n{hint}")

    with path.open("rb") as f:
        data = tomllib.load(f)

    if "variant" not in data:
        die(f"{path}: 缺少 [variant] 段")
    v = data["variant"]

    parent_name = v.get("extends")
    if "name" not in v:
        die(f"{path}: [variant] 缺少 name")
    # ★ 有 extends 时 packages 可以不写（那就等于"父的那些，一个不加"）；
    #   没有 extends 就必须写 —— 一个空风味没有意义。
    if "packages" not in v and not parent_name:
        die(f"{path}: [variant] 缺少 packages（没有 extends 时它是必填的）")

    # 文件名和 name 必须一致。不一致时「按名字找文件」会有两个答案，而症状是
    # 两个风味的内容互相串——这种错很难在事后看出来。
    if v["name"] != name:
        die(f"{path}: [variant] name 是 {v['name']!r}，和文件名 {name!r} 对不上")

    # ★ 说清是【哪一种】不对：packages = "glibc" 是合法的 toml，而字符串是
    #   可迭代的——' '.join("glibc") 会变成 "g l i b c"，一串单字母的包名。
    #   原来这里一律报"不能为空"，而它明明不空，人会往错的方向查。
    if "packages" in v and not isinstance(v["packages"], list):
        die(f"{path}: packages 必须是一个非空的列表"
            f"（现在拿到的是 {type(v['packages']).__name__}）")
    # ★ 没有 extends 时，空清单等于"装出来一个空根"—— 而那要等到起不来才发现。
    if not v.get("packages") and not parent_name:
        die(f"{path}: packages 必须是一个非空的列表"
            f"（空清单装出来的根是空的）")
        # ★ 说清是【哪一种】不对：packages = "glibc" 是合法的 toml，而字符串
        #   是可迭代的——' '.join("glibc") 会变成 "g l i b c"，一串单字母的
        #   包名。原来这里一律报"不能为空"，而它明明不空，人会往错的方向查。
        die(f"{path}: packages 必须是一个非空的列表"
            f"（现在拿到的是 {type(v['packages']).__name__}）")

    kind = v.get("kind", "container")
    if kind not in KINDS:
        die(
            f"{path}: kind = {kind!r} 不是 0007 里的四种形式之一\n"
            f"       合法值：{' '.join(KINDS)}"
        )

    v["_kind"] = kind

    # ── services：这个风味【启用】哪些服务（决策记录 0024）──────────────────
    #
    # 「装 openssh」和「开机起 sshd」是两件事，而后者是风味的决定：
    # 同一个 openssh 包，minimal 要它起来、base（容器）不要。
    #
    # ★ 所以 `services` 里写的是【服务名】，它对应
    #   `/usr/lib/yojigen-os/services/<名字>` 那份定义（由某个包提供）。
    #   而"这个风味里到底有没有那份定义"只能在组装时才知道 —— 见 root.py 里那段
    #   校验（**声明了却没人提供 → 当场失败**，不猜）。
    services = v.get("services", [])
    if not isinstance(services, list):
        die(f"{path}: services 必须是列表，现在拿到的是 {type(services).__name__}")
    for x in services:
        if not isinstance(x, str) or not x.strip():
            die(f"{path}: services 里有一项不是非空字符串: {x!r}")
        if x != x.strip() or "/" in x or " " in x:
            # ★ 这些形状都会让"启用了但永远不起"，而症状出现在开机之后。
            die(f"{path}: 服务名 {x!r} 不合法（不能有空格或斜杠，且要原样写）")
    v["services"] = services

    # ── 合并父风味（extends）────────────────────────────────────────────────
    if parent_name:
        if not isinstance(parent_name, str) or parent_name == name:
            die(f"{path}: extends = {parent_name!r} 不合法（要么是风味名，要么不写）")
        parent = load(parent_name, _seen + (name,))
        # 父在前、自己在后；去重保序（重复的包名 apk 也能装，但列表读起来会乱）
        见 = set()
        合并 = []
        for x in list(parent["packages"]) + list(v.get("packages") or []):
            if x not in 见:
                见.add(x)
                合并.append(x)
        v["packages"] = 合并
        v["services"] = [x for x in dict.fromkeys(
            list(parent["services"]) + list(v["services"]))]
        v["_extends"] = parent_name
        # kind 没写就跟着父（`server` 是 disk，因为 minimal 是 disk）
        if "kind" not in v and "_kind" in parent:
            v["_kind"] = parent["_kind"]
    v["_path"] = path
    return v


def require_producible(v: dict) -> None:
    """产出交付物之前问一句：这种形式实现了吗。

    校验放在【产出】那一步，不放在读取那一步：`rootfs` 只需要一张包列表，
    它对 kind = disk 的风味照样有用（先在容器里装出来看看，不建盘）。
    """
    kind = v["_kind"]
    if kind not in PRODUCIBLE:
        # ★ 名单从数据里算，不写死。写死过一次：原来那句是"iso / disk / kernel
        #   都还没有实现"，而 disk 2026-10-12 就能建出来了（img.py）——
        #   报错里那句话正好是人卡住的时候最信的一句。
        还没实现 = " / ".join(k for k in KINDS if k not in PRODUCIBLE)
        die(
            f"风味 {v['name']} 的 kind 是 {kind!r}，还没有实现。\n"
            f"       现在能产出交付物的只有：{' '.join(PRODUCIBLE)}\n"
            f"       0007 里的 {还没实现} 都还没有实现"
        )


def show(name: str) -> None:
    v = load(name)
    log("风味", f"{v['name']}  [{v['_kind']}]  {v.get('description', '')}")
    pkgs = v["packages"]
    print(f"       文件: {v['_path'].relative_to(VARIANTS_DIR.parent)}")
    print(f"       包数: {len(pkgs)}")
    for p in pkgs:
        print(f"         · {p}")


def listing() -> None:
    """列出所有风味。每个都真读一遍——文件坏了要在这里就报出来。"""
    all_names = names()
    if not all_names:
        print(f"（{VARIANTS_DIR} 里还没有风味定义）")
        return
    worst = max(len(n) for n in all_names)
    for n in all_names:
        v = load(n)
        mark = " " if v["_kind"] in PRODUCIBLE else "·"
        print(f"  {mark} {v['name']:<{worst}}  {v['_kind']:<10} "
              f"{len(v['packages']):>2} 个包   {v.get('description', '')}")
    print()
    print(f"  （{len(all_names)} 个风味；· 表示这种形式还没有实现）")
