# 1Panel 在我们系统上装上了，而且重启会自己起来

**2026-10-22。** 目标是一句命令：

```bash
bash -c "$(curl -sSL https://resource.fit2cloud.com/1panel/package/v2/quick_start.sh)"
```

它现在能跑完，装上，并且**重启之后自己起来**。

## 它要什么（一路撞出来的清单）

| 它要的 | 我们原来有没有 | 补法 |
|---|---|---|
| `curl` / `tar` / `sha256sum` / `grep` / `awk` | ✅ 有 | — |
| `/etc/init.d/`（没有 systemctl 时它走这条路） | ✗ **没有** | `filesystem` 建 `init.d` + `rc2.d`..`rc5.d` |
| `rc2.d/S*` 能被开机执行 | ✗ | `/init` 起完 `/etc/services/` 之后再扫 `.rc2.d/S*` |
| `/var/lock/subsys/` | ✗ | `filesystem` 建 |
| `echo_success` / `echo_failure` | ✗ | `yojigen-os-init` 装一个 multi-call 脚本 |
| `service <名字> <动作>` 这个调用形状 | ✗ 我们把顺序写反了 | `service` 两种顺序都认 |
| `service` 认得 `/etc/init.d/` 里的服务 | ✗ 只认我们自己的 | 存在就原样委托，返回码也原样传 |
| **`/bin/sh` 能解析 bash 数组** | ✗ 是 dash | **风味选**：server 用 `bash-binsh` |

★ 其中两条是**我们自己的假阴性**：面板明明在跑、端口在监听，而安装器判"启动
失败"——因为它调 `service 1panel-core status`，而我们答"没这个服务"（那时
`service` 还不认 `/etc/init.d`），接着又把参数顺序搞反了。

## 最后那个坎：`/bin/sh` 指向谁

1Panel 的 `/etc/init.d/1panel-core` 在 `#!/bin/sh` 里写了 bash 数组：

```sh
local rc_dirs=( /etc/rc*.d … )
```

dash 不认 → `Syntax error: "(" unexpected` → 服务**装了但重启后起不来**。

### 而解法不是"把全系统的 /bin/sh 换成 bash"

`dash` 配方里那条理由仍然成立：**只要 /bin/sh 是 bash，大家就会用扩展**，
那是个可移植性陷阱。所以：

```
base / devel / minimal    仍然 dash     （纪律）
server                    bash          （要跑别人写的安装脚本，那些大多假设 bash）
```

★ 而这是 **风味** 该决定的事 —— 和决策记录 0024（服务由风味启用）同一个思路。
实现上是 `variants/server.toml` 里：

```toml
exclude  = ["dash-binsh"]     # 从父风味（minimal）的清单里去掉
packages = ["bash-binsh"]     # 换成这个（provides /bin/sh，priority 90）
```

## 端到端验过（TX 真机）

```
$ bash -c "$(curl -sSL …/quick_start.sh)" -- --non-interactive --install-docker n
  (10/64) Installing bash (5.3-r3)
  Starting 1panel-agent:   [ OK ]
  ========感谢您的耐心等待，安装已完成========
  外部地址： http://<公网IP>:35044/<入口>
  面板用户： …    面板密码： …

$ curl http://127.0.0.1:35044/<入口>      →  HTTP 200
$ 1pctl user-info                          →  面板地址 / 用户 正常

$ reboot
$ cat /run/yojigen-os/services.log
  docker ok
  sshd ok
  panel-agent ok (sysv)
  panel-core  ok (sysv)
$ pgrep -a 1panel                          →  agent + core 都在
$ curl http://127.0.0.1:35044/<入口>      →  HTTP 200
```

★ 最后那一段才是"能正常工作"的判据 —— 不是"装完了"，而是**重启之后还在**。

## 而"我从外面打不开"是另一件事

同一个公网 IP：**22 端口通，35044 超时**。而机器上：

```
面板监听        0.0.0.0:35044 ✓
本机 curl       HTTP 200 ✓
iptables        INPUT policy ACCEPT，没有拦 35044 的规则 ✓
```

所以是**云厂商的安全组**（要放行 TCP 35044）。那件事得在控制台做，不是系统里。

## 还没做的

- `server` 的**硬化**那一层（ssh 配置、只认公钥、禁 root 密码…）—— 描述里写着"还没做"
- `chrony` / `cronie` 的服务定义（现在 `services` 里只有 `sshd`）
- 1Panel 的 `docker` 管理（它自己的容器功能）没在真机上验过

---

# 附：它报的那些错，一条条按源码对

2026-10-22 下午。看完 1Panel 的源码（`1Panel-dev/1Panel`）之后，报错的来源都
有确定答案了 —— 不再靠猜。

## ① `$LOCAL_IP` —— **不是我们的问题**

`core/cmd/server/cmd/user-info.go`：

```go
address := getSettingByKey(agentDB, "SystemIP")
domain  := getSettingByKey(db, "BindDomain")
if len(domain) != 0 { address = domain }
...
if address == "" { address = "$LOCAL_IP" }
```

而 `SystemIP` 是迁移时**故意建成空值**的（`agent/init/migration/.../init.go:144`），
由 UI 写入。所以任何发行版上一装完 `1pctl user-info` 都会打 `$LOCAL_IP` ——
在面板设置里填一次"系统 IP"就正常。**跟我们的系统无关。**

## ② `ip6tables ... (legacy): Table does not exist` —— 我们的漏

上一轮把 `iptables` 三个名字改指 nft 了，**漏了 `ip6tables` 那三个**。
现在六个名字全指 `xtables-nft-multi`，实测 `ip6tables v1.8.12 (nf_tables)` ✓

## ③ 防火墙页 —— 缺 `nft`

1Panel v2 的防火墙走 `nftables_helper`（源码里 `nft` 38 处引用）。所以补了：

| 包 | 版本 | 为什么 |
|---|---|---|
| `nftables` | 1.1.6 | 1Panel 防火墙页的判据 |
| `libnftnl` | 1.2.8 → **1.3.1** | nftables 的 configure 硬要 `>= 1.3.1` |

★ 而 nftables 的 configure 里**嵌了一段 bash 循环**（`for ((i = 56; …))`），
顶上是 `#! /bin/sh` —— 我们的 /bin/sh 是 dash，于是
`config.status: Bad for loop variable`。为此给驱动加了
`[build] configure_shell = "/bin/bash"`（走 `CONFIG_SHELL` 这个标准口子）。

## ④ `Docker Compose command not found` —— 我自己卸掉的

前面验 `post-install` 时 `apk del docker docker-buildx docker-compose`，只装回了
`docker`。装回来就没了。

## ⑤ 而现在还缺的（源码里数出来的，都用 `cmd.Which` 判在不在）

| 缺的 | 1Panel 哪里用 | 影响 |
|---|---|---|
| `zip` / `unzip` | 文件管理、应用安装包 | 压缩/解压相关操作报错 |
| `wget` | 部分下载路径 | 少数场景 |
| `git` | 应用源码拉取 | 一部分应用装不上 |
| `systemctl` / `hostnamectl` / `timedatectl` | 服务页、主机名页、时间页 | **这几页在我们这儿没有对等物**（我们没有 systemd） |
| `supervisord` | 老版本的进程守护 | 视应用而定 |

★ 前四个是**真该补的包**（像 nftables 一样写配方就行）。
★ 后两个是**取舍**：要么给它们写一层"读同样的内核接口"的兼容程序（像我们给
  `service` 做的那样），要么认下"这几页不支持"。这个得定。

## 现在的状态

```
nft v1.1.6 · iptables (nf_tables) · ip6tables (nf_tables) · docker 29.9.2
面板 HTTP 200 · agent + core 两个进程都在
重启面板之后【没有新的 ERROR】（最后一条是 08:57 的 ip6tables，那是修复前）
```
