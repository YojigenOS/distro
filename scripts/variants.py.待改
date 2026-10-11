"""风味：一份「这个镜像里有什么」的声明。

决策记录 0007 定下了模型，这里先实现其中**现在就能做的那一半**：

    风味     = 一组包 + 一个用途        用什么
    交付形式 = 容器 / ISO / 磁盘         怎么给

目前只有「容器」这一种形式能产出，所以 `kind` 只接受 `container`——写
别的会明确报错，而不是悄悄产出一个名不副实的东西。等 `.img` 和 ISO 真有
了实现，再按 0007 里那个 `[variant.formats.*]` 的草案扩。

## 为什么需要它

在那之前，`base-devel` 的包列表是**写在 CI workflow 里的**。加了 `gettext`
之后那份列表就悄悄漏了它——因为同一个东西有两个出处，而只有一个会被改。
风味文件让它只剩一个出处。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .common import VARIANTS_DIR, die, log

#: 现在能产出的交付形式。0007 里还有 iso / disk / kernel，都没实现。
KNOWN_KINDS = ("container",)


def variant_names() -> list[str]:
    if not VARIANTS_DIR.is_dir():
        return []
    return sorted(p.stem for p in VARIANTS_DIR.glob("*.toml"))


def load_variant(name: str) -> dict:
    """读一个风味定义。"""
    path = VARIANTS_DIR / f"{name}.toml"
    if not path.is_file():
        known = variant_names()
        hint = f"       现有的：{' '.join(known)}" if known else "       还没有任何风味定义"
        die(f"找不到风味 {name}（{path.relative_to(VARIANTS_DIR.parent)}）\n{hint}")
    with path.open("rb") as f:
        data = tomllib.load(f)

    if "variant" not in data:
        die(f"{path}: 缺少 [variant] 段")
    v = data["variant"]

    for key in ("name", "packages"):
        if key not in v:
            die(f"{path}: [variant] 缺少 {key}")

    # 文件名和 name 必须一致。不一致时「按名字找文件」会有两个答案，
    # 而症状是两个风味的内容互相串——这种错很难在事后看出来。
    if v["name"] != name:
        die(f"{path}: [variant] name 是 {v['name']}，和文件名 {name} 对不上")

    if not isinstance(v["packages"], list) or not v["packages"]:
        die(f"{path}: packages 不能为空")

    kind = v.get("kind", "container")
    if kind not in KNOWN_KINDS:
        die(
            f"{path}: kind = {kind!r} 还没实现。\n"
            f"       现在只能产出 {' '.join(KNOWN_KINDS)}——"
            f"0007 里的 iso / disk / kernel 都还没有实现"
        )

    v["_kind"] = kind
    v["_path"] = path
    return v


def show(name: str) -> None:
    v = load_variant(name)
    log("风味", f"{v['name']}  [{v['_kind']}]  {v.get('description', '')}")
    log("包含", f"{len(v['packages'])} 个包：{' '.join(v['packages'])}")


def list_variants() -> None:
    names = variant_names()
    if not names:
        print(f"（{VARIANTS_DIR} 里还没有风味定义）")
        return
    for n in names:
        v = load_variant(n)
        print(f"  {v['name']:<14} {v['_kind']:<10} {v.get('description', '')}")
