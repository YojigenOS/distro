# YojigenOS distro

**建系统。** 把一堆软件包组装成能用的交付物：容器镜像、rootfs、ISO。

```
packages 仓库    建包      配方 → .apk 包 → 发布到一个软件仓库
distro 仓库      建系统    ★ 按 manifest 从那个仓库装 → 容器 / rootfs / ISO
```

## 为什么是两个仓库

这两件事的**演化驱动完全不同**：

```
建包     跟【上游软件】走    新版本 · 新构建系统 · 新补丁 · 上游的 bug
建系统   跟【交付形式】走    新镜像格式 · 新引导方式 · 新平台 · ISO
```

而更实际的理由是：**建系统不该依赖建包**。

它需要的只是【包的产物】——而那个产物有个干净的格式：

```
<name>-<ver>-<rel>/
  payload/      文件树
  meta.json     身份 + 依赖 + 日期 + 文件清单
```

所以两个仓库之间的契约**不是 git，也不是文件系统**，而是**一个 apk 软件仓库**：

```
packages  →  .apk 包 + APKINDEX  →  软件仓库（URL）
distro    →  读 manifest + 读仓库 URL  →  下载 → 装进一个根  →  交付物
```

**这是关键**：distro 不 checkout 配方，不读 `var/output/`，不 import `skirtlib`。
它只认一个仓库 URL。

## 为什么不能读本地目录

三条，第三条最要命：

```
① 它让 distro 依赖 packages 的【内部布局】
   目录一改，distro 就坏——2026-10-11 那次重构当场验证过这类 bug

② 它绕过了包管理器
   而包管理器正是「装一个系统」的正确机制

③ ★ 它让【发布的包】和【用来组装的包】可以是不同的东西
   组装用本地产物、用户拿发布的包——两者可能不一致，而且没人会发现
```

第三条是把这件事从「更优雅」变成「必须做」的理由。

## 现在的状态

**容器那一格通了；磁盘那一格还没开始。**

```
✓ variants/           三个风味：base / base-devel / minimal（0007 说的"两格都占"）
✓ pantsu/             本仓库自己的驱动。skirt 建包，pantsu 建系统
✓ apk 软件仓库         https://repo.os.yojigen.cn（aarch64 线上 15 个包）
✓ 组装（容器）         scripts/assemble.sh —— 现在只做 docker import，装根交给 pantsu
✓ docs/决策记录/       8 篇属于本仓库的（编号不重编，跳号表示在 packages）
✗ scripts/run-vm.sh    还在调 `./skirt/skirt paths`——那是 packages 的东西（该收进 pantsu vm）
✗ 磁盘交付物（.img）    0013 定的那条路，一张自带一切的磁盘镜像。没有它，系统装不到服务器上
✗ manifest/            还不存在：这个系统用了哪些包的哪些版本
✗ 交付形式/            还不存在：容器 / rootfs / ISO 各自的定义
✗ docs/约定/系统约定.md  还不存在
```

**而最根本的那条已经补上了**：apk 软件仓库存在了，这个仓库因此有了合法的输入。
pantsu 只认那一个 URL——不 checkout 配方、不读 `var/output/`、不 import `skirtlib`。

下一步的顺序：

```
① packages 侧：把已建好的包【打包 + 发布】
   util-linux · yojigenos-init · linux 躺在 var/output/ 里，线上仓库还没有它们
② packages 侧：grub · openssh 两份新配方（grub 的 i386-pc 要 gcc 开 multilib）
③ distro 侧：pantsu img —— 建盘（混合 GPT + 文件系统 + 引导器 + grub.cfg）
④ distro 侧：pantsu vm / verify-img —— 在 QEMU 里起来，把"能启动"变成 CI 的断言
```

**验收的原则没变**：装出来的东西要能和另一条路算出来的对上。
2026-10-12 用了一次——pantsu 接管装根之后，产出的 tar 和旧 `assemble.sh` 的
**逐项一致**（1444 项，模式/属主/大小全同），只少了 PAX 头里那几个不确定的
时间戳记录。一样就说明换了机制、结果没变。

## 目录

```
variants/              风味：哪些包、什么形态
pantsu/                本仓库的驱动（skirt 的兄弟：那边建包，这边建系统）
scripts/               薄壳：组装、验证、跑 VM
keys/                  会烤进交付物的签名公钥
docs/
├── 约定/              系统约定（待写）
├── 指南/              怎么把系统跑起来（待写）
├── 决策记录/           8 篇，编号是全项目的（见那里的 README）
└── 待办.md             （待写）
```

## 相关

- **决策记录的正本**分布在两个仓库里，按「它主要影响什么」分。
  包侧的在 `packages/docs/决策记录/`。
- **2026-10-11 之前**，建包和建系统都在 `packages` 一个仓库里。
