# Руководство пользователя

🌐 [English](user-guide.md) · **Русский** · [Español](user-guide.es.md) · [Français](user-guide.fr.md) · [中文](user-guide.zh.md)

## Чем вы управляете

**Очаг**: машины под вашим контролем, на которых крутятся изолированные провайдеры AIMarket. Консоль `/ui/` говорит с **этим** процессом. Это не поиск Hub.

Агенты появляются в roster только после успешного `POST /v1/tenants` (или `hestia.host.deploy@v1`) **на этом хосте**.

## Локально

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Откройте http://127.0.0.1:9480/ui/ — вставьте токен, задеплойте demo echo, откройте `/t/demo-echo/health`.

## Вызов тенанта

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"hearth"}'
```

## Свой handler

Только Python после AST-допуска. Нужна `handle(payload) -> dict`. Закрытый набор stdlib. Нет `os`, сети и файлов. AST-допуск — **не** песочница; stub — **не** ВМ.

## Закреплённый образ

```bash
export HESTIA_RUNTIME=docker
export HESTIA_ALLOW_IMAGE_DIGESTS=sha256:<64 hex>
```

Digest уже должен быть на **том же Docker**, который у вас уже есть. Hestia не делает `docker build`. Это процесс **на хосте**, не сокет в compose-коробке (сокет в коробке = шелл на хост). Host `docker.sock` запрещён без `HESTIA_ALLOW_HOST_DOCKER=1`.

## Announce в Hub

По умолчанию выключен. Нужен `HESTIA_HUB_URL` и отдельный `POST /v1/tenants/{slug}/announce` (или `HESTIA_AUTO_ANNOUNCE=1`). Хостинг ≠ размещение в каталоге.

## Self-host и флот AICOM

Вы запускаете Hestia — тенанты на **вашей** машине. Эталонный очаг AICOM — на хосте за `hestia.modelmarket.dev`. Factory, закончив пайплайн, никуда сам не деплоит.

## Флот (эталонный очаг)

На хосте, который будет отдавать `hestia.modelmarket.dev` — A-запись должна указывать **сюда**. Хост не выдумывать. Пустой токен запрещает любую запись.

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export HESTIA_PUBLIC_BASE=https://hestia.modelmarket.dev
sudo ./scripts/deploy_hestia.sh
```

Compose остаётся stub (без `docker.sock`). nginx — TLS-край перед `127.0.0.1:9480`.
