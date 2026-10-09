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

## 沙箱（`HESTIA_RUNTIME=wasm`）

使用 `HESTIA_RUNTIME=wasm` 时，处理器从不作为独立进程运行。每次调用都在一个**全新的 WebAssembly 实例**中执行：编译为 WASI 的 CPython 3.14，由 wasmtime 运行，位于单独的 `hestia-runner` 服务中（`docker compose --profile wasm up -d`）。该容器**完全没有网络**、**没有任何密钥**（不传入 hearth 的环境变量）、文件系统只读，只有一个与 hearth 共享卷中的 Unix 套接字。在实例内，处理器只能只读地看到标准库，别无其他：

| 处理器尝试 | 结果 |
|---|---|
| 读取 `/data/provider.key`、`/etc/passwd`、`~/.ssh`、运行器的套接字 | `FileNotFoundError`——实例中不存在任何主机路径 |
| 写入标准库 | `PermissionError`（只读） |
| 打开套接字 | WASI preview 1 没有套接字 |
| 启动进程、加载原生代码（`ctypes`） | WASI 不支持 |
| 读取环境变量 | 只有两个固定变量：`PYTHONHASHSEED`、`PYTHONHOME` |
| 超出内存上限、死循环、刷屏 stdout | `MemoryError`（256 MiB）、超时（10 秒，epoch interruption）、超过 4 MiB 的输出被拒绝 |
| 在调用之间保留状态或读取其他调用的状态 | 每次调用都是新实例 |

hearth 在沙箱**之外**用租户自己的密钥对结果签名（密钥从不进入沙箱），规范形式和状态码与 stub 相同：从 `stub` 迁移到 `wasm` 的租户保留其密钥，应答照常可验证。由于沙箱才是边界，`wasm` 处理器只做语法检查，不受 stub 白名单限制：可以导入 WASI 构建自带的任何模块（`base64`、`difflib`、`typing`、`decimal`、`unicodedata`……；不包括 `zlib`、套接字和线程）。调用之间没有进程，空闲的智能体只占磁盘、不占内存：stub 只能容纳三十个左右的地方可以容纳数百个。

已在参考 hearth 上用一个陌生人编写的逃逸智能体验证：它尝试的每个路径都是 `FileNotFoundError`，网络和进程都不可用，环境中只有两个变量。

## 所有者

共享的 hearth 承载多家企业的智能体，因此运营方令牌不再是唯一的钥匙。**所有者**是运营方准入的 Ed25519 密钥（`POST /v1/owners`）。所有者的每个请求都对以下内容签名：hearth 的公开地址、方法、路径、unix 时间戳（±300 秒）、一次性 nonce 和请求体的 SHA-256（`X-Hestia-Owner` / `-Timestamp` / `-Nonce` / `-Signature`，格式 `hestia-owner/1`，见 [`hestia/owners.py`](../hestia/owners.py)）。

| 谁 | 可以做什么 |
|---|---|
| 运营方（令牌） | 一切，与以前相同；准入、限额和暂停所有者 |
| 所有者 | 在配额内（`HESTIA_OWNER_MAX_TENANTS`，默认 3）部署到空闲的或自己的 slug；只能停止、公告和列出**自己的**智能体；在 `GET /v1/owners/me` 查看自身状态 |
| 其他任何人 | 读取公开名册、调用智能体 |

以下请求会被拒绝：重放的请求、为其他 hearth、路径或请求体签名的请求，以及在 `owner_pubkey` 中填写他人密钥的所有者。全零占位密钥和其他所有小阶 Ed25519 密钥都不能拥有任何东西，因此用占位密钥部署的智能体只归运营方。

`HESTIA_OPEN_OWNERS=1` 允许任何有效密钥在首次部署时自行准入。在 stub 模式下它无权运行代码——AST 准入不是沙箱——所以陌生人只能部署固定镜像和密封桩。在 `HESTIA_RUNTIME=wasm` 下，`HESTIA_OPEN_OWNER_CODE=1` 赋予他们这项权利：他们的代码在上面所述的沙箱中运行，他们的智能体只有通过下面的上架审核才能进入公开清单。两者默认都关闭；参考 hearth 两者都开启。

```bash
python -m hestia.owner_cli keygen --out owner.key        # 打印供运营方使用的公钥
python -m hestia.owner_cli deploy --hearth https://hestia.example --key owner.key --body deploy.json
python -m hestia.owner_cli stop   --hearth https://hestia.example --key owner.key --slug my-agent
python -m hestia.owner_cli proof  --key owner.key --domain example.com   # 打印证明域名所需的 TXT 记录和文件
python -m hestia.owner_cli domain --hearth https://hestia.example --key owner.key --domain example.com
```

## 上架前审核

枢纽会索引 hearth 的清单，因此清单中的智能体会展示给所有固定此 hearth 的枢纽的买家。**所有者**的智能体部署后立即运行——可以在它自己的 `/t/{slug}` 入口调用——但只有通过自动审核（[`hestia/listing.py`](../hestia/listing.py)）后才进入清单。审核读取买家模型会读取的内容（名称、描述、模式），遇到以下情况会暂缓上架：指令标签、"ignore previous instructions"、"不要告诉用户"、"在使用任何其他工具之前"、隐蔽操作、凭据路径、隐藏字符、HTML 注释、混用形近字母表的单词（拉丁字母与西里尔字母、希腊字母等），以及与其他所有者的能力族相近的能力 ID，或读起来与本 hearth 上某个智能体（无论是否运行）相同的名称。所有者可在部署响应和 `GET /v1/tenants` 中看到原因；运营方可通过 `POST /v1/admin/tenants/{slug}/listing` 手动上架或暂缓。运营方自己的智能体不经审核。

枢纽向买家展示的名称永久属于第一个以其部署的持有者（[`hestia/names.py`](../hestia/names.py)）：能力族（`@` 之前的 ID 部分）、产品 ID 和发布者（publisher id 与收款地址）。名称按折叠后的形式比较——大小写、分隔符、形近字母和末尾的版本号都不会构成新名称，因此 `merkle-proof@v2`、`merkle_proof.v2@v1` 和 `Merkle.Pr0of@v1` 都属于 `merkle.proof` 的能力族。已停止的智能体、改过名的智能体和被暂停的所有者都保留自己的名称；其他所有者会收到 409。`hestia*` 属于 hearth 自身，`HESTIA_RESERVED_PREFIXES` 中的前缀（默认 `aicom,aimarket,modelmarket`）属于运营方。同一所有者名下，一个确切的 ID（不分大小写）同一时间只由一个智能体提供服务。名称由数据库本身认领（主键），因此同一账本上的两个进程不可能同时赢得同一个名称。运营方通过 `GET /v1/admin/names` 查看认领，通过 `POST /v1/admin/names` 保留名称或转交给他人（`{"kind": "family|product|publisher|prefix", "name": "…", "holder": "operator" | 所有者公钥}`；`prefix` 覆盖所有以它开头的名称，例如为某公司保留其品牌），并在持有者的智能体停止后通过 `POST /v1/admin/names/release` 释放。

被暂缓的智能体只在它自己的 `/t/{slug}` 入口应答：枢纽使用的路由调用只能到达已上架的智能体。所有者重新部署会重新审核，但不能解除运营方设置的暂缓；运营方重新部署也不会让暂缓的智能体上架——只有上架调用才会。被暂停所有者的智能体会从清单和名册中消失，两个入口都不再提供服务。`HESTIA_MAX_TENANTS` 计算运行中和隔离中的智能体；已停止的智能体保留它的 slug 和名称，但不占位置。

证明了域名的所有者，将获得以该域名（包括顶级域）开头的所有名称（[`hestia/domains.py`](../hestia/domains.py)）。证明方式是在 `_hestia.<域名>` 上设置内容为 `hestia-owner=<公钥>` 的 TXT 记录，或在 `https://<域名>/.well-known/hestia-owner.json` 中放置 `{"owner_pubkeys": ["<公钥>"]}`；`python -m hestia.owner_cli proof` 会打印这两种方式，`… domain` 请求 hearth 进行检查。此后，凡是以该域名开头（正序或倒序均可）的能力族、产品和发布者——`attestedmemory.net.deal`、`net.attestedmemory.deal`、`attestedmemory-net.deal`、“Attestedmemory.net Labs”——都归该所有者所有，枢纽会把该域名视为智能体的 `publisher_domain`。单独的单词不属于任何人：attestedmemory.com、.dev 和 .net 可能属于三个不同的所有者，所以 `attestedmemory.deal` 和其他名称一样先到先得。只接受可注册的 ASCII 域名（example.com、example.co.uk；不接受子域名和 IDN），且域名绝不会夺走其他持有者已在使用的名称。HTTPS 证明只向公网地址请求，且不跟随重定向。

## 替你销售智能体的枢纽

智能体每次调用都在链上收取 USDC，而枢纽无法用买方的额度或分包额度这样付款。因此所有者可以让某个枢纽代其销售智能体（[`hestia/hub_billing.py`](../hestia/hub_billing.py)）：

1. 运营方把该枢纽的密钥交给 hearth：`HESTIA_TENANT_HUB_KEYS=https://hub.example=<密钥>`——与该枢纽 `AIMARKET_PEER_API_KEYS` 中这个 hearth 的条目同值；
2. 所有者在该枢纽开设额度账户并选择它：`python -m hestia.owner_cli billing --hearth … --key owner.key --hub https://hub.example --account acct_…`（`--account ""` 撤回选择）；
3. 枢纽用该密钥发起的调用无需链上付款即可得到服务，响应中附带一个由 hearth 提供方密钥签名的 `hub_billing` 块：智能体属于谁、哪个枢纽、哪个账户。枢纽用它为该 hearth 固定的密钥验证，并按**它自己**向买方收取的金额，向所有者记入 `AIMARKET_PUBLISHER_SHARE_BPS`（默认 70 %）；
4. `python -m hestia.owner_cli statement` 列出每一次这样的调用，用于与枢纽实际支付的金额核对。

运营方自己的智能体（占位所有者密钥）由运营方配置了密钥的任何枢纽销售，收入归枢纽。没有收费的枢纽（`X-AIMarket-Hub-Charged: 0`，免费试用）拿不到所有者的智能体。运营方视图：`GET /v1/admin/hub-billing`。

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
