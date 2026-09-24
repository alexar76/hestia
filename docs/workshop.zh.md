# 运营者工坊 — 在 HESTIA 上部署智能体

**语言：** [EN](workshop.md) · [RU](workshop.ru.md) · [ES](workshop.es.md) · [FR](workshop.fr.md) · [ZH](workshop.zh.md)

**这不是一门课。** 没有 Colab、没有证书、没有学院门户。九十分钟对着线上控制台：**你启动的那台主机上的 `/ui/`**。

术语见 [`localization-glossary.md`](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md)。产品名（`HESTIA`、`Hub`、`THEMIS`、`USDC`、`Base`）和环境变量保持拉丁文。正文写 **主机 (HESTIA)** 和 **智能体**。

生产收银台（谁签发 `402`、USDC 去向）见 [`hestia-hub-market-rail.zh.md`](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.zh.md)。本页是该轨道的运营者工位。

---

## 你在做什么

你在**自己控制的机器**上启动一个隔离的智能体进程。这是 **部署**。这不是 Hub 的 **上架条目**。

买家在 **目录** 里搜索。目录只在 **announce**（向 Hub 宣布 / 敲门）和 crawl 之后才会变。敲门是观测，不授予信任。

| 角色 | 是什么 | 钱 |
|---|---|---|
| **主机 (HESTIA)** | 运行时。`/ui/` 控制台只跟**这个**进程说话。 | `HESTIA_PAYMENTS_ENABLED=0` 时不当收银台。 |
| **Hub** | HESTIA 上架条目的目录 + 收银台。 | 签发 `402`，`payTo` = 卖家。 |
| **卖家** | `payout_address` 里的钱包。 | 收到 USDC。 |

Factory 跑完流水线 **不是** 部署。空 **名册** 表示**这里**没有托管任何东西，不是「市场是空的」。

---

## 禁止

- **不要**把部署令牌贴进 [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)，除非你运营那台主机。参考主机只许 **看**。
- **不要**在笔记本上勾选「同时向 Hub 宣布」并指向 `https://modelmarket.dev`。`127.0.0.1` 过不了 Hub 的公网 HTTPS 检查，仍会弄脏隔离区。
- **不要**支付 USDC。看到 `402` 就够。
- **不要**输入占位符 `HESTIA_DEPLOY_TOKEN`。粘贴你在启动**本**进程前 `export` 的 **值**。

不是人人都能部署。令牌锁住写入。公开名册不需要令牌。

---

## 两块屏幕

一直开着。在受信任的 crawl 编入智能体之前，它们 **必须不一致**。

**1 — 这台主机**（在这里写）

- 控制台：`http://127.0.0.1:9480/ui/`
- 名册 API：`GET /v1/hearth`（路径仍是 `/v1/hearth`；正文称主机）
- 部署后：`{HESTIA_PUBLIC_BASE}/t/{slug}` — 默认 `http://127.0.0.1:9480/t/demo-echo`

**2 — 市场**（只读）

- 目录：[modelmarket.dev](https://modelmarket.dev)
- 搜索：`GET https://modelmarket.dev/ai-market/v2/search`
- Monitor：[monitor.modelmarket.dev](https://monitor.modelmarket.dev/) — 节点 `hestia`，**Knocking** 轨道
- 参考名册：[hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)

智能体 URL **不是** capability JSON 里的字段。它拼成 `{HESTIA_PUBLIC_BASE}/t/{slug}`，成功部署后出现在按钮下方的 JSON（`public_url`、`invoke_url`）和名册卡片上。空闲的 **就绪。** 表示你还没部署。

---

## 90 分钟

### 0–10 · 三个角色

见上表。一笔付款填不满两座收银台：生产里 **只有一座收银台：Hub**。细节见 market rail 文档。

### 10–25 · 启动 **你的** 主机

在 `hestia/` 树下：

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

打开 `http://127.0.0.1:9480/ui/`。把 **同一个值** 贴进 **部署令牌**。环境变量里令牌为空 ⇒ 任何写入都是 401，即使表单填满。这是故意的默认。

**可选已准入 handler** 留空（密封 stub）。不要勾选 **同时向 Hub 宣布**。

### 25–40 · 部署 `demo-echo`

slug 保持 `demo-echo`，用现成的 capability JSON。点 **部署智能体**。

通过条件：

- 按钮下的 `<pre>` 是带 `"ok": true`、`public_url`、`invoke_url` 的 JSON；
- 名册卡片显示 `demo-echo`、URL、`/health`、`announced=false`；
- `GET http://127.0.0.1:9480/t/demo-echo/health` 活着。

`/ui/` 表单 **不发送** `payout_address`。该字段在 `POST /v1/tenants`（见 [`examples/deploy-echo.json`](examples/deploy-echo.json)）。本工坊用不到：直付卖家上架条目不在此槽。

### 40–55 · 在主机上调用（invoke）（没有 `402`）

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"host"}'
```

返回 `result`、`provider_pubkey` 和 Ed25519 `signature`。没有 HTTP `402`。主机不当收银台。

AST 准入 **不是** 沙箱。stub **不是** 虚拟机。对本工坊如实说明；`HESTIA_RUNTIME=docker` 是之后的运营选择。

### 55–70 · 看线上收银台（不要写）

**不要**在参考主机上部署。对比：

```bash
# Hub 目录 — 未付款 invoke → 402，payTo = 卖家
curl -sS -D - https://modelmarket.dev/ai-market/v2/invoke \
  -H 'content-type: application/json' \
  -d '{"capability_id":"json.canonical@v1","product_id":"hestia-agents","input":{"document":{}}}' \
  | head -n 40

# 直连主机 — 到达 handler，不是 402
curl -sS -D - https://hestia.modelmarket.dev/t/json-canonical/invoke \
  -H 'content-type: application/json' \
  -d '{"document":{}}' \
  | head -n 40
```

同一 capability，两扇门。Hub 点名卖家。主机跑进程。

### 70–80 · Announce 是敲门

默认关闭。托管 ≠ 上架条目。

```bash
export HESTIA_HUB_URL=https://modelmarket.dev
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/announce \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

笔记本不要对生产做这一步。若你敲的是自己运营的 **公网 HTTPS** 主机：

- Hub 把 peer 记为 `pending`、`trusted: false`（隔离）；
- Monitor 显示 **Knocking**；
- 搜索和付费 invoke 仍关闭，直到 assay 为 `pass` **且** judge 令牌 auto-admit，或运营者 Approve；
- 已在 `AIMARKET_SELLS_FOR` 里的 peer（参考主机）会在 **下一次 crawl** 收进新智能体（`AIMARKET_AUTO_CRAWL`，默认 1 小时）。这仍然不是「部署 ⇒ 目录」。

`HESTIA_AUTO_ANNOUNCE=1` 仅在设置了 `HESTIA_HUB_URL` 时敲门。仍然不授予信任。

### 80–90 · Stop。空名册 ≠ 空市场

在 `/ui/` 看过名册卡片后：

```bash
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/stop \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

刷新名册。若接了 THEMIS（`HESTIA_THEMIS_URL`）且它宕机，部署 fail-closed，不是跳过。

---

## 清单

- [ ] 表单里的令牌是本进程的 **值**，不是占位符，也不是参考主机。
- [ ] `demo-echo` 在 **你的** 名册上，带 `public_url`。
- [ ] 主机上的 invoke 返回带签名的结果，不是 `402`。
- [ ] 线上 Hub 对未付款的 `json.canonical@v1` 返回 `payTo` = 卖家的 `402`（只读）。
- [ ] 你 **没有** 向 `modelmarket.dev` 宣布笔记本。
- [ ] 能说明：部署 ≠ 上架条目；announce ≠ 信任；空名册 ≠ 空目录。

---

## 相关

- 你正在用的控制台 — `/ui/`（这台主机）· 只读 [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)
- 指南 — [user-guide.zh.md](user-guide.zh.md)
- Market rail — [docs/hestia-hub-market-rail.zh.md](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.zh.md)
- 联邦敲门 — [join-the-federation.zh.md](https://github.com/alexar76/aicom/blob/main/docs/join-the-federation.zh.md)
- 用例 — [USE-CASES.zh.md](USE-CASES.zh.md)
