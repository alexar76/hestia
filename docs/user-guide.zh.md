# 用户指南

🌐 [English](user-guide.md) · [Русский](user-guide.ru.md) · [Español](user-guide.es.md) · [Français](user-guide.fr.md) · **中文**

## 你在运营什么

一座**炉灶**：由你控制的机器，跑隔离的 AIMarket 提供方。`/ui/` 控制台只跟**这个**进程说话。它不是 Hub 搜索。

智能体只有在本机 `POST /v1/tenants`（或 `hestia.host.deploy@v1`）成功后才会出现在名册上。

## 本地

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

打开 http://127.0.0.1:9480/ui/ — 粘贴令牌，部署 demo echo，访问 `/t/demo-echo/health`。

## 调用租户

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"hearth"}'
```

## 自定义 handler

仅 Python，且须通过 AST 准入。必须定义 `handle(payload) -> dict`。封闭的 stdlib 集合。没有 `os`、没有网络、没有文件。

## 钉死的镜像

```bash
export HESTIA_RUNTIME=docker
export HESTIA_ALLOW_IMAGE_DIGESTS=sha256:<64 hex>
```

digest 必须已经在你现有的 **同一个 Docker** 上。Hestia 不会替你 `docker build`。这是**宿主机进程**，不要把 `docker.sock` 挂进 compose 盒子（那就是对宿主机的 shell）。主机 `docker.sock` 默认拒绝，除非 `HESTIA_ALLOW_HOST_DOCKER=1`。

## 向 Hub 宣布

默认关闭。需要 `HESTIA_HUB_URL` 以及单独的 `POST /v1/tenants/{slug}/announce`（或 `HESTIA_AUTO_ANNOUNCE=1`）。托管 ≠ 上架。

## 自托管 vs AICOM 机队

你运行 Hestia — 租户在**你的**机器上。AICOM 参考炉灶在 `hestia.modelmarket.dev` 后面。Factory 跑完流水线不会自动部署到任何地方。

## 机队（参考炉灶）

在将要服务 `hestia.modelmarket.dev` 的主机上 — A 记录必须指向**这里**。不要臆造主机。空令牌拒绝一切写入。

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export HESTIA_PUBLIC_BASE=https://hestia.modelmarket.dev
sudo ./scripts/deploy_hestia.sh
```

Compose 仍是 stub（没有 `docker.sock`）。nginx 是 `127.0.0.1:9480` 前面的 TLS 边缘。
