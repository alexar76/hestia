# Воркшоп оператора — деплой агента на HESTIA

**Языки:** [EN](workshop.md) · [RU](workshop.ru.md) · [ES](workshop.es.md) · [FR](workshop.fr.md) · [ZH](workshop.zh.md)

**Это не курс.** Нет Colab, нет сертификата, нет портала Академии. Девяносто минут у живой консоли: **`/ui/` на хосте, который вы поднимаете**.

Термины — [`localization-glossary.md`](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md). Имена продуктов (`HESTIA`, `Hub`, `THEMIS`, `USDC`, `Base`) и env-переменные остаются латиницей. В прозе: **хост (HESTIA)** и **агент** — не «очаг» и не «тенант».

Продовая касса (кто чеканит `402`, куда идёт USDC) — [`hestia-hub-market-rail.ru.md`](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.ru.md). Эта страница — стол оператора для того рельса.

---

## Что вы делаете

Вы стартуете изолированный процесс агента **на машинах под вашим контролем**. Это **деплой**. Это не **листинг** Hub.

Покупатели ищут в **каталоге**. Каталог меняется только после **announce** (стук в Hub) и crawl. Стук — наблюдение, не выдача доверия.

| Роль | Что это | Деньги |
|---|---|---|
| **Хост (HESTIA)** | Runtime. Консоль `/ui/` говорит с **этим** процессом. | Не касса при `HESTIA_PAYMENTS_ENABLED=0`. |
| **Hub** | Каталог + касса листинга HESTIA. | Чеканит `402`, `payTo` = продавец. |
| **Продавец** | Кошелёк в `payout_address`. | Получает USDC. |

Factory, закончив пайплайн, **никуда не деплоит**. Пустой **roster** (список хоста) значит: здесь ничего не хостится, не «рынок пуст».

---

## Запрещено

- **Не** вставлять токен деплоя на [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/), если вы не оператор этого хоста. Эталонный хост — **только смотреть**.
- **Не** включать «Также анонсировать в Hub» на `https://modelmarket.dev` с ноутбука. `127.0.0.1` не проходит публичный HTTPS у Hub и всё равно засоряет карантин.
- **Не** платить USDC. Увидеть `402` достаточно.
- **Не** вводить placeholder `HESTIA_DEPLOY_TOKEN`. Вставьте **значение**, которое вы `export`-нули до старта **этого** процесса.

Развернуть может не каждый. Токен — замок на запись. Публичный roster токена не требует.

---

## Два экрана

Держите оба. Они **обязаны расходиться**, пока доверенный crawl не проиндексирует агента.

**1 — Этот хост** (сюда пишете)

- Консоль: `http://127.0.0.1:9480/ui/`
- API списка: `GET /v1/hearth` (путь остаётся `/v1/hearth`; в прозе — хост)
- После деплоя: `{HESTIA_PUBLIC_BASE}/t/{slug}` — по умолчанию `http://127.0.0.1:9480/t/demo-echo`

**2 — Рынок** (только смотреть)

- Каталог: [modelmarket.dev](https://modelmarket.dev)
- Поиск: `GET https://modelmarket.dev/ai-market/v2/search`
- Monitor: [monitor.modelmarket.dev](https://monitor.modelmarket.dev/) — нода `hestia`, рельс **Knocking**
- Эталонный roster: [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)

URL агента **не** поле в capability JSON. Он собирается как `{HESTIA_PUBLIC_BASE}/t/{slug}` и появляется после успешного деплоя в JSON под кнопками (`public_url`, `invoke_url`) и на карточке roster. Idle **Готово.** значит, деплоя ещё не было.

---

## 90 минут

### 0–10 · Три роли

Таблица выше. Один платёж не закрывает две кассы — в production **ровно одна касса**: Hub. Подробности — документ market rail.

### 10–25 · Поднять **свой** хост

Из дерева `hestia/`:

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Откройте `http://127.0.0.1:9480/ui/`. Вставьте **то же значение** в **Токен деплоя**. Пустой токен в env ⇒ любая запись 401, даже с заполненной формой. Так задумано.

**Опциональный handler** оставьте пустым (sealed stub). Галку **Также анонсировать в Hub** не ставьте.

### 25–40 · Деплой `demo-echo`

Slug `demo-echo`, canned capability JSON. **Развернуть агента**.

Проход:

- `<pre>` под кнопками — JSON с `"ok": true`, `public_url`, `invoke_url`;
- карточка roster: `demo-echo`, URL, `/health`, `announced=false`;
- `GET http://127.0.0.1:9480/t/demo-echo/health` живой.

Форма `/ui/` **не** шлёт `payout_address`. Поле есть на `POST /v1/tenants` (см. [`examples/deploy-echo.json`](examples/deploy-echo.json)). Для этого воркшопа не нужно — seller-direct листинг вне слота.

### 40–55 · Вызов (invoke) на хосте (без `402`)

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"host"}'
```

В ответе `result`, `provider_pubkey` и Ed25519 `signature`. HTTP `402` нет. Хост не касса.

AST-допуск — **не** песочница. Stub — **не** ВМ. Для воркшопа это честно; `HESTIA_RUNTIME=docker` — следующий выбор оператора.

### 55–70 · Живая касса (не писать)

**Не** деплойте на эталонный хост. Сравните:

```bash
# Каталог Hub — неоплаченный invoke → 402, payTo = продавец
curl -sS -D - https://modelmarket.dev/ai-market/v2/invoke \
  -H 'content-type: application/json' \
  -d '{"capability_id":"json.canonical@v1","product_id":"hestia-agents","input":{"document":{}}}' \
  | head -n 40

# Прямой хост — доходит до handler, не 402
curl -sS -D - https://hestia.modelmarket.dev/t/json-canonical/invoke \
  -H 'content-type: application/json' \
  -d '{"document":{}}' \
  | head -n 40
```

Одна capability, две двери. Hub называет продавца. Хост крутит процесс.

### 70–80 · Announce — стук

По умолчанию выключен. Хостинг ≠ листинг.

```bash
export HESTIA_HUB_URL=https://modelmarket.dev
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/announce \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

С ноутбука в production это пропускайте. Если стучитесь с **публичного HTTPS**-хоста, которым вы управляете:

- Hub пишет пира как `pending`, `trusted: false` (карантин);
- Monitor показывает **Knocking**;
- поиск и платный invoke закрыты, пока assay = `pass` **и** judge token не сделает auto-admit, либо оператор не нажмёт Approve;
- пир уже в `AIMARKET_SELLS_FOR` (эталонный хост) подхватит нового агента на **следующем crawl** (`AIMARKET_AUTO_CRAWL`, по умолчанию 1 ч). Это всё ещё не «деплой ⇒ каталог».

`HESTIA_AUTO_ANNOUNCE=1` стучит только при заданном `HESTIA_HUB_URL`. Доверия всё равно не даёт.

### 80–90 · Stop. Пустой roster ≠ пустой рынок

Запомните карточку roster в `/ui/`, затем:

```bash
curl -X POST http://127.0.0.1:9480/v1/tenants/demo-echo/stop \
  -H "Authorization: Bearer $HESTIA_DEPLOY_TOKEN"
```

Обновите roster. Если THEMIS включён (`HESTIA_THEMIS_URL`) и лежит — деплой fail-closed, не skip.

---

## Чеклист

- [ ] В форме — **значение** токена этого процесса, не placeholder и не эталонный хост.
- [ ] `demo-echo` в **вашем** roster с `public_url`.
- [ ] Invoke на хосте отдаёт подписанный результат, не `402`.
- [ ] Живой Hub на неоплаченном `json.canonical@v1` отдаёт `402` с `payTo` = продавец (только смотреть).
- [ ] Вы **не** анонсировали ноутбук в `modelmarket.dev`.
- [ ] Можете объяснить: деплой ≠ листинг; announce ≠ доверие; пустой roster ≠ пустой каталог.

---

## Связанное

- Консоль, которой вы управляете — `/ui/` (этот хост) · только смотреть [hestia.modelmarket.dev/ui/](https://hestia.modelmarket.dev/ui/)
- Руководство — [user-guide.ru.md](user-guide.ru.md)
- Market rail — [docs/hestia-hub-market-rail.ru.md](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.ru.md)
- Стук в федерацию — [join-the-federation.ru.md](https://github.com/alexar76/aicom/blob/main/docs/join-the-federation.ru.md)
- Сценарии — [USE-CASES.ru.md](USE-CASES.ru.md)
