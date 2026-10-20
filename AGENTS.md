# YojigenOS distro

**建系统。** 从 `packages` 建出来的软件仓库里装出一个根，再打包成交付物
（容器 / rootfs / 磁盘镜像）。

    packages 仓库    建包     配方 → .apk → 一个软件仓库
    distro 仓库      建系统   ★ 按风味从那个仓库装 → rootfs / 镜像

**这个仓库里没有编译器。** `rootfs` 是"从仓库装包"，`img` 是"把根变成一张盘"
—— 都是组装和打包，可以在本地跑。而**编译**只在 `packages` 那边，它的规则
（所有构建都在云端 CI）见 `packages/docs/约定/构建在哪里跑.md`。

## 干活之前

```bash
cd distro
./pantsu/pantsu flavors                 # 列出所有风味（顺带校验每一个）
./pantsu/pantsu rootfs minimal --probe  # 装出一个根，并在容器里验一遍
python3 -m unittest discover -s tests
```

## 目录

| 目录 | 是什么 |
|---|---|
| `variants/<风味>.toml` | **风味定义**：这个交付物里要有什么包 |
| `pantsu/` | 驱动（`pantsu` 入口 + `pantsulib/`） |
| `keys/` | 仓库签名公钥（`*.rsa.pub`）—— 装包时验签用 |
| `scripts/` | 零散的 shell |
| `tests/` | 单元测试（跑得很快，不装包） |
| `docs/` | 见下 |

## 那条最要紧的分界

**镜像名只出现在 `variants/<风味>.toml`。** `packages` 那边不该知道它叫什么
—— 那是这个项目为"同一个东西两个出处"吃过的亏之一。

同理：**风味是"目标"，不是"现状"**。`variants/minimal.toml` 里列的是这个交付
物**该有**什么，而 `pantsu rootfs` 会告诉你现在卡在哪一档。别把那张列表当成
"已经能装出来"的证明。

## `docs/` 的三类文件

| 文件 | 规矩 |
|---|---|
| `决策记录/NNNN-*.md` | **编号是全项目共用的**（和 `packages/docs/决策记录/` 连着编）。★ **只能追加，不能改写** —— 改了就等于篡改历史。要推翻一条，写新的一条并指明它取代了谁 |
| `待办.md` | 现在卡在哪、为什么、试过什么不通 |
| `记录-*.md` | **里程碑**。一次真的做成了什么：日期、命令、输出、而它证明了什么 |

★ 第三类值得单独说：它记的是**实测**，不是计划。已经有三份 ——
`第一个能用的交付物` · `第一次装到真机` · `第一次从仓库装出一台系统`。

## CI

`.github/workflows/` 三个：

    test.yml     push 时跑单元测试
    image.yml    建镜像
    release.yml  发布（`-f deploy=true` 才会推到 repo.os.yojigen.cn）

★ **查 CI 历史有个坑**：`gh api .../runs?workflow_id=<文件名>` 的
`workflow_id` **会被静默忽略**，于是你拿到的是【所有】workflow 的 runs，而它
看起来完全正常。见 `packages/docs/约定/查-CI-历史.md`。

## 这个项目反复在修的那一类

**"一个该说话的地方没说话。"** 落到这个仓库，它具体是这几种：

* 报了一个包名而那是**子包**（`glibc-locales` 不是配方名）—— 位置错，报错
  信息却指向"没有这个配方"
* 交付物的**列表是目标**，而读数被当成现状
* `pantsu rootfs` 装成功了，而那个系统里的程序**跑不起来**（缺少的 `NEEDED`）
  —— 所以"装上了"和"装对了"是两件事，而后者要量（`ldd -r`）

★ 所以：**每一条结论都要能自证** —— 给命令、给输出、给日期。而那正是
`记录-*.md` 的格式。
