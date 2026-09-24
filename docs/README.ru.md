# HESTIA

<p align="center">
  <a href="../README.md">English</a> ·
  <a href="README.ru.md"><b>Русский</b></a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh.md">中文</a>
</p>

**HESTIA** (Ἑστία) — **очаг** для провайдеров способностей AIMarket.

Изолированный hosted-runtime · не каталог Hub · не доска работ · не Factory.

**Возможности:** `hestia.host.deploy@v1` · `hestia.host.status@v1` · `hestia.hearth.list@v1` · `hestia.host.stop@v1` ·
**Порт:** `9480` ·
**Лендинг:** [alexar76.github.io/hestia](https://alexar76.github.io/hestia/) ·
**Эталонный очаг (когда DNS поднят):** [hestia.modelmarket.dev](https://hestia.modelmarket.dev)

## Прямой ответ

**Это не доска, на которой агенты Factory появляются сами.**

Нужен **явный деплой подписанного бандла на этот очаг**. Пока его нет, roster пуст — и пустой roster значит «здесь ничего не хостится», а не «рынок пуст».

**Где они хостятся:** на **машинах оператора Hestia**.

| Режим | Кто крутит процесс |
|---|---|
| Эталонный очаг | Флот AICOM (`hestia.modelmarket.dev`) |
| Self-host | **Ваш** сервер, ноутбук или compose |
| Не | Ноутбук автора, Hub, Factory, THEMIS или ARGUS |

Hub остаётся каталогом. THEMIS (по желанию) может отказать до старта. Announce — явный стук, не выдача доверия.

## Слои (не схлопывать)

| Узел | Вопрос | Слой |
|---|---|---|
| **Factory** / `create-aimarket-agent` | Собрать провайдера? | Исходники на диске. Пока никто не слушает. |
| **THEMIS** | Пускать в каталог Hub? | Допуск публикации |
| **HESTIA** | Где реально крутится процесс продавца? | Hosted-runtime на машинах оператора |
| **Hub** | Что покупатель находит и оплачивает? | Каталог + расчёт |
| **ARGUS** | Как это потреблять? | Покупатель / desktop |

URL очага (`https://hestia.example/t/weather-bot`) — **не** строка каталога.

## Изоляция

По умолчанию — **замкнутый loopback-подпроцесс** (`HESTIA_RUNTIME=stub`): наш HTTP и Ed25519, опциональный `handle(payload)` только после AST-допуска. Stub **не** ставит cgroups. AST — допуск, не песочница. Production-ledger — Postgres (`HESTIA_DATABASE_URL`); SQLite — dev/test. `HESTIA_REPLICAS>1` отказ: общий ledger ≠ ферма.

**Коробка Hestia не управляет хостом. Тенанты тоже.** Compose — один обычный спутник (нет docker CLI, нет `docker.sock`, нет privileged). Тенанты там — loopback-подпроцессы внутри коробки. Docker-runtime — процесс **на хосте** (`python -m hestia` + `HESTIA_ALLOW_HOST_DOCKER=1`), не сокет в контейнере. Стартует только `sha256:` из `HESTIA_ALLOW_IMAGE_DIGESTS`. У каждого тенанта своя сеть `hestia-tenants-{slug}` с `Internal=true`, без IPv6, на /29 из `HESTIA_TENANT_SUBNET_POOL`; маршрута от тенанта к тенанту нет. Шлюз этой сети — сам хост движка: запустите на нём `sudo scripts/tenant-host-firewall.sh apply`, иначе тенант достаёт сервисы хоста, слушающие на всех адресах. TCP-движок без TLS отклоняется при любом адресе, включая `tcp://127.0.0.1`. При старте Hestia проверяет каждый image-тенант и переносит в отдельные сети тех, кто ещё в старой общей; кого проверить не удалось, получает статус `quarantined` (не обслуживается, повтор через `POST /v1/admin/reconcile`). Hearth в режиме `stub` docker не вызывает никогда. У тенантов нет `docker.sock`, нет `--privileged`, нет host-сети, `--cap-drop ALL`, `--read-only`, uid `65532`.

## Быстрый старт

```bash
cd hestia
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . pytest -q
uv run --project . python -m hestia
# консоль: http://127.0.0.1:9480/ui/
```

Пустой `HESTIA_DEPLOY_TOKEN` отклоняет любую запись. Так задумано.

Полный гид: [user-guide.ru.md](user-guide.ru.md) · воркшоп: [workshop.ru.md](workshop.ru.md) · сценарии: [USE-CASES.ru.md](USE-CASES.ru.md) · архитектура: [ARCHITECTURE.md](ARCHITECTURE.md) (EN).

Имя продукта `HESTIA` остаётся латиницей. Лицензия MIT.
