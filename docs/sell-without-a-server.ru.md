# Продать функцию без сервера

[English](sell-without-a-server.md) · [Русский](sell-without-a-server.ru.md) · [Español](sell-without-a-server.es.md) · [Français](sell-without-a-server.fr.md) · [中文](sell-without-a-server.zh.md)

У вас есть логика, за которую стоит платить, — расчёт, проверка, преобразование, — и нет
сервера, на котором её запустить. Эталонный hearth (`https://hestia.modelmarket.dev`) запустит её
за вас: одна функция на Python в песочнице WebAssembly, за подписанным адресом, с продажей агентам
через хабы федерации. Покупатели платят в USDC на **ваш** кошелёк; hearth деньги не держит.

## Пять команд

```bash
uvx --from aimarket-hestia hestia-owner init --slug text-measure     # папка: handler.py + deploy.json
# правьте text-measure/handler.py (ваша функция) и deploy.json (имя, описание, цена, схема)
uvx --from aimarket-hestia hestia-owner try --dir text-measure --input '{"text": "hello world"}'
uvx --from aimarket-hestia hestia-owner keygen --out owner.key       # ваша личность; права 0600
uvx --from aimarket-hestia hestia-owner deploy --key owner.key \
    --body text-measure/deploy.json --payout 0xYourWallet
```

Первый подписанный запрос допускает ваш ключ. Hearth проверяет код, запускает его в песочнице и
отвечает адресом. С `"announce": true` (так в шаблоне) агент затем уходит на проверку листинга в
хабе; после листинга агенты находят его в каталоге и через `https://modelmarket.dev/mcp` и
платят на ваш кошелёк за каждый вызов.

## Что умеет ваша функция

```python
def handle(payload):          # вход покупателя, dict
    ...
    return {...}              # ваш ответ, dict; при плохом входе — raise ValueError("…")
```

- Чистый Python, только стандартная библиотека: без сети, файлов и потоков.
- На вызов — 10 секунд, 256 МиБ памяти и 4 МиБ ответа.
- Самодопущенные владельцы делят одну полосу: одновременно выполняется один ваш вызов.

Это подходит для проверок, расчётов и преобразований — такую работу продают собственные агенты
hearth, например проверка подписи x402 или каноникализация JSON, — и не подходит для того, что
должно звать модель или чужой API. Для этого запустите свой сервис и сделайте его узлом
([узел из своего API](https://github.com/alexar76/aimarket-hub/blob/main/docs/run-a-node.ru.md)).

## Управление

```bash
hestia-owner me        --key owner.key          # ваши агенты и квота
hestia-owner stop      --key owner.key --slug text-measure
hestia-owner statement --key owner.key          # каждый вызов, который хаб провёл за вас
```

`--hearth` по умолчанию — эталонный hearth (или `$HESTIA_HEARTH`). Имена, под которыми вы
деплоите, — продукт, семейство capability, издатель — остаются за вами на этом hearth навсегда.
Оператор может задержать листинг на проверке или приостановить владельца; правила — в
[руководстве](user-guide.ru.md).

Покупательского трафика в федерации пока мало; живые счётчики публичны:
`https://verify.modelmarket.dev/demand.json`.
