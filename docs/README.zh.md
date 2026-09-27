# HESTIA

<p align="center">
  <a href="../README.md">English</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh.md"><b>中文</b></a>
</p>

**HESTIA**（Ἑστία）— AIMarket 能力提供方的**炉灶**。

隔离托管运行时 · 不是 Hub 目录 · 不是任务板 · 不是 Factory。

**能力：** `hestia.host.deploy@v1` · `hestia.host.status@v1` · `hestia.hearth.list@v1` · `hestia.host.stop@v1` ·
**端口：** `9480` ·
**落地页：** [alexar76.github.io/hestia](https://alexar76.github.io/hestia/) ·
**参考炉灶（DNS 就绪时）：** [hestia.modelmarket.dev](https://hestia.modelmarket.dev)

## 直接回答

**这不是一块 Factory 智能体会自动出现的公告板。**

必须有人把**签名包部署到这座炉灶**。在此之前名册是空的——空表示「这里没有托管任何东西」，不是「市场是空的」。

**它们托管在哪里：** **Hestia 运营者的机器上**。

| 模式 | 谁在跑进程 |
|---|---|
| 参考炉灶 | AICOM 机队（`hestia.modelmarket.dev`） |
| 自托管 | **你的**服务器、笔记本或 compose |
| 不是 | 作者的笔记本、Hub、Factory、THEMIS 或 ARGUS |

Hub 仍是目录。THEMIS（可选）仍可在启动前拒绝。宣布是一次显式敲门，不是授予信任。

## 分层（不要混为一谈）

| 节点 | 问题 | 层 |
|---|---|---|
| **Factory** / `create-aimarket-agent` | 如何搭一个提供方？ | 磁盘上的源码。还没有人在听。 |
| **THEMIS** | 能否进入 Hub 目录？ | 发布准入 |
| **HESTIA** | 卖方进程实际跑在哪？ | 运营者机器上的托管运行时 |
| **Hub** | 买方能发现并付款的是什么？ | 目录 + 结算 |
| **ARGUS** | 如何消费？ | 买方 / 桌面 |

炉灶 URL **不是**目录行。

## 隔离

默认是**密封的 loopback 子进程**（`HESTIA_RUNTIME=stub`；无 cgroups；AST 不是沙箱）。**Hestia 盒子不能驱动宿主机。租户也不能。** Compose 是一颗普通卫星（没有 docker CLI、没有 `docker.sock`、没有 privileged）。Docker runtime 是**宿主机进程**（`python -m hestia` + `HESTIA_ALLOW_HOST_DOCKER=1`）。只启动 `HESTIA_ALLOW_IMAGE_DIGESTS` 的 `sha256:` digest。每个租户使用独立的 `Internal` bridge 网络（默认名 `hestia-tenants-{slug}`），关闭 IPv6，子网是从 `HESTIA_TENANT_SUBNET_POOL` 分配的 /29；租户之间没有路由。该网络的网关就是引擎宿主机：请在该宿主机上运行 `sudo scripts/tenant-host-firewall.sh apply`，否则租户可以连到宿主机上监听所有地址的服务。任何没有 TLS 的 TCP 引擎都会被拒绝（包括 `tcp://127.0.0.1`）。启动时 Hestia 逐个核验 image 租户，把仍在旧共享网络上的租户迁到独立网络；无法核验的租户标记为 `quarantined`（不对外提供，可用 `POST /v1/admin/reconcile` 重试）。`stub` 模式的 hearth 从不调用 docker。`--cap-drop ALL`、`--read-only`、uid `65532`。

## 快速开始

```bash
cd hestia
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . pytest -q
uv run --project . python -m hestia
# 控制台：http://127.0.0.1:9480/ui/
```

空的 `HESTIA_DEPLOY_TOKEN` 拒绝所有写入。

指南：[user-guide.zh.md](user-guide.zh.md) · 工坊：[workshop.zh.md](workshop.zh.md) · 用例：[USE-CASES.zh.md](USE-CASES.zh.md) · 架构：[ARCHITECTURE.md](ARCHITECTURE.md)（英文）。

产品名 `HESTIA` 保持拉丁文。许可证 MIT。
