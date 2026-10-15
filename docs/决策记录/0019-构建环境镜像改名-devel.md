# 0019 构建环境镜像改名：base-devel → devel

- **日期**：2026-10-15
- **状态**：**已决定**
- **影响范围**：`variants/devel.toml`（原名 `base-devel.toml`）、`yojigenos/devel`
  这个镜像名、`packages` 那边消费它的两处 CI 配置、0003 那张命名表

## 背景

0003 那张命名表里，构建环境叫 `base-devel`：

```
yojigenos/base-devel    base + 工具链与构建工具    ← 自举容器，skirt 用它
```

而 2026-10-15 那天在 `packages` 那边撞到一件事：`bootstrap.yml` 的输入说明里
写着 `yojigenos/devel` —— **而那个镜像不存在**（Docker Hub 上 `devel` 是 401，
`base-devel` 才有 tag）。也就是说：有人在某个时刻决定了要改名，在**一处**写了
新名字，其余地方没跟。而那一处恰好是给人看的说明，所以它会让人 `docker pull`
失败。

**所以这次要做的不是"改个名"，是"把一件只发生在一处的事做完"。**

## 决定

**叫 `devel`。** 变体文件 `variants/base-devel.toml` → `variants/devel.toml`，
`[variant] name` 跟着改，镜像名因此变成 `yojigenos/devel`。

## 为什么

**一、`base-devel` 这个名字在骗人。** 它读起来像"base + devel 两部分"，而
事实上 `variants/devel.toml` 和 `variants/base.toml` 是**两套并列的包列表**，
不是叠加关系（各自都从 `filesystem` 开始列全）。名字暗示的那个结构不存在。

**二、"devel" 短，而且不暗示结构。** 它只说"这是开发者用的那一套"——那正是它的
定位（0011：配方能用什么 ⟺ devel 里有什么）。

★ 而这不是"两个名字都行挑一个顺眼的"。理由是一里那个**名字和事实不符**——
和这一趟在别处修的几件事是同一类。

## 怎么做的（顺序要紧）

镜像名是**发布端**（本仓库）产出的，而 `packages` 是消费端。所以：

```
1. 本仓库：改变体文件名与 name、改 CI 的 flavor 下拉   → 推
2. 本仓库：跑一次 release，发布 yojigenos/devel:edge-<架构>   → 镜像真的存在了
3. packages：把 SKIRT_BUILDENV / docker pull 的四处切到 devel
```

**反过来做的话，`packages` 的 CI 会在一段窗口里拉一个不存在的镜像。**

而旧的 `yojigenos/base-devel` **不删**——它躺在那里不碍事，而删了会让"照着旧文档
做的人"失败。真正的"停用"是等没人引它。

## 顺带

`variants/devel.toml` 和文件名必须对得上，而 `pantsu` 里**已经有**一道守卫
（`flavor.load()`：`[variant] name` 和文件名不一致就 die，报
`name 是 'X'，和文件名 'Y' 对不上`）。

**所以这次改名是一处改错就会当场响的** —— 那道守卫本身就是"客观性"那条规范说的
「配一个能跑的检查」的一个实例：**同一个东西的两处写法不许分叉。**
