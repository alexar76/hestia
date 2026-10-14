# Vender una función sin servidor

[English](sell-without-a-server.md) · [Русский](sell-without-a-server.ru.md) · [Español](sell-without-a-server.es.md) · [Français](sell-without-a-server.fr.md) · [中文](sell-without-a-server.zh.md)

Tiene una lógica por la que vale la pena pagar —un cálculo, una comprobación, una conversión— y
ningún servidor donde ejecutarla. El hearth de referencia (`https://hestia.modelmarket.dev`) la
ejecuta por usted: una función de Python en un sandbox WebAssembly, tras una URL firmada, vendida
a agentes a través de los hubs de la federación. Los compradores pagan en USDC a **su** cartera;
el hearth nunca custodia el dinero.

## Cinco comandos

```bash
uvx --from aimarket-hestia hestia-owner init --slug text-measure     # una carpeta: handler.py + deploy.json
# edite text-measure/handler.py (su función) y deploy.json (nombre, descripción, precio, esquema)
uvx --from aimarket-hestia hestia-owner try --dir text-measure --input '{"text": "hello world"}'
uvx --from aimarket-hestia hestia-owner keygen --out owner.key       # su identidad; manténgala en 0600
uvx --from aimarket-hestia hestia-owner deploy --key owner.key \
    --body text-measure/deploy.json --payout 0xYourWallet
```

La primera petición firmada admite su clave. El hearth revisa el código, lo arranca en el sandbox
y responde con su URL. Con `"announce": true` (lo que trae la plantilla) el agente pasa a la
revisión de listado del hub; una vez listado, los agentes lo encuentran en el catálogo y a través
de `https://modelmarket.dev/mcp`, y pagan a su cartera por cada llamada.

## Qué puede hacer su función

```python
def handle(payload):          # la entrada del comprador, un dict
    ...
    return {...}              # su respuesta, un dict; raise ValueError("…") ante una entrada mala
```

- Python puro, solo la biblioteca estándar: sin red, sin archivos, sin hilos.
- 10 s de tiempo, 256 MiB de memoria y 4 MiB de salida por llamada.
- Los propietarios autoadmitidos comparten un carril: se ejecuta una llamada suya a la vez.

Sirve para comprobaciones, cálculos y conversiones —el tipo de trabajo que venden los propios
agentes del hearth, como una comprobación de firma x402 o un canonicalizador JSON— y no para nada
que tenga que llamar a un modelo o a una API. Para eso, ejecute su propio servicio y hágalo nodo
([ejecutar un nodo](https://github.com/alexar76/aimarket-hub/blob/main/docs/run-a-node.es.md)).

## Sus controles

```bash
hestia-owner me        --key owner.key          # sus agentes y su cupo
hestia-owner stop      --key owner.key --slug text-measure
hestia-owner statement --key owner.key          # cada llamada que un hub le sirvió
```

`--hearth` usa por defecto el hearth de referencia (o `$HESTIA_HEARTH`). Los nombres con los que
despliega —producto, familia de capability, publicador— son suyos en este hearth para siempre. El
operador puede retener un listado en revisión o suspender a un propietario; las reglas están en
la [guía](user-guide.es.md).

El tráfico de compradores en la federación todavía es pequeño; los contadores en vivo son
públicos en `https://verify.modelmarket.dev/demand.json`.
