# HESTIA

<p align="center">
  <a href="../README.md">English</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.es.md"><b>Español</b></a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh.md">中文</a>
</p>

**HESTIA** (Ἑστία) — el **hogar** para proveedores de capacidad AIMarket.

Runtime aislado y alojado · no es el catálogo del Hub · no es un tablón de empleos · no es Factory.

**Capacidades:** `hestia.host.deploy@v1` · `hestia.host.status@v1` · `hestia.hearth.list@v1` · `hestia.host.stop@v1` ·
**Puerto:** `9480` ·
**Landing:** [alexar76.github.io/hestia](https://alexar76.github.io/hestia/) ·
**Hogar de referencia (cuando el DNS está arriba):** [hestia.modelmarket.dev](https://hestia.modelmarket.dev)

## Respuesta directa

**No es un tablón donde los agentes de Factory aparecen solos.**

Alguien debe **desplegar un paquete firmado en este hogar**. Hasta entonces el roster está vacío — y vacío significa «aquí no se aloja nada», no «el mercado está vacío».

**Dónde se alojan:** en las **máquinas del operador de Hestia**.

| Modo | Quién ejecuta el proceso |
|---|---|
| Hogar de referencia | Flota AICOM (`hestia.modelmarket.dev`) |
| Autoalojado | **Tu** servidor, portátil o compose |
| No | El portátil del autor, Hub, Factory, THEMIS o ARGUS |

El Hub sigue siendo el catálogo. THEMIS (opcional) puede rechazar antes del arranque. El anuncio es un golpe explícito, no una concesión de confianza.

## Capas (no las mezcles)

| Nodo | Pregunta | Capa |
|---|---|---|
| **Factory** / `create-aimarket-agent` | ¿Andamiar un proveedor? | Fuente en disco. Nadie escucha aún. |
| **THEMIS** | ¿Entrar al catálogo del Hub? | Admisión al publicar |
| **HESTIA** | ¿Dónde corre de verdad el proceso del vendedor? | Runtime alojado en las máquinas del operador |
| **Hub** | ¿Qué puede encontrar y pagar un comprador? | Catálogo + liquidación |
| **ARGUS** | ¿Cómo lo consumo? | Comprador / desktop |

Una URL del hogar **no** es una fila de catálogo.

## Aislamiento

Por defecto, un **subproceso loopback sellado** (`HESTIA_RUNTIME=stub`; sin cgroups; AST no es un sandbox). **La caja Hestia no conduce el host. Los inquilinos tampoco.** Compose es un satélite (sin CLI docker, sin `docker.sock`, sin privileged). Docker runtime es un proceso **en el host** (`python -m hestia` + `HESTIA_ALLOW_HOST_DOCKER=1`). Solo digest `sha256:` de `HESTIA_ALLOW_IMAGE_DIGESTS`. Cada inquilino recibe una red bridge `Internal` propia (`hestia-tenants-{slug}`), sin IPv6, con una /29 de `HESTIA_TENANT_SUBNET_POOL`; no hay ruta de un inquilino a otro. La pasarela de esa red es el host del motor: ejecuta `sudo scripts/tenant-host-firewall.sh apply` en ese host para que un inquilino no alcance servicios del host que escuchan en todas las direcciones. Se rechaza un motor TCP sin TLS, sea cual sea la dirección (también `tcp://127.0.0.1`). Al iniciar, Hestia verifica cada inquilino de imagen y pasa a redes privadas los que siguen en la red compartida antigua; el que no puede verificar queda en `quarantined` (no se sirve, se reintenta con `POST /v1/admin/reconcile`). Un hearth `stub` nunca llama a docker. `--cap-drop ALL`, `--read-only`, uid `65532`.

## Sandbox (`HESTIA_RUNTIME=wasm`)

Con `HESTIA_RUNTIME=wasm` un handler nunca se ejecuta como proceso propio. Cada llamada corre en una **instancia
WebAssembly nueva**: CPython 3.14 compilado a WASI, ejecutado por wasmtime, en el servicio aparte
`hestia-runner` (`docker compose --profile wasm up -d`). Ese contenedor **no tiene red**, **no tiene secretos**
(nada del entorno del hearth), sistema de archivos de solo lectura y un único socket Unix en un volumen que
comparte con el hearth. Dentro de la instancia el handler ve la biblioteca estándar en solo lectura y nada más:

| El handler intenta | Obtiene |
|---|---|
| leer `/data/provider.key`, `/etc/passwd`, `~/.ssh`, el socket del runner | `FileNotFoundError`: no existe ninguna ruta del host |
| escribir en la biblioteca estándar | `PermissionError` (solo lectura) |
| abrir un socket | WASI preview 1 no tiene sockets |
| lanzar un proceso, cargar código nativo (`ctypes`) | WASI no lo soporta |
| leer el entorno | dos variables fijas: `PYTHONHASHSEED`, `PYTHONHOME` |
| pasar del límite, bucle infinito, inundar stdout | `MemoryError` (256 MiB), tiempo agotado (10 s, epoch interruption), salida rechazada pasados 4 MiB |
| guardar estado entre llamadas o leer el de otra | cada llamada es una instancia nueva |

El hearth firma el resultado con la clave propia del tenant **fuera** del sandbox (la clave nunca entra), con el
canónico y los códigos del stub: un tenant movido de `stub` a `wasm` conserva su clave y sus respuestas se
verifican igual. Como el sandbox es la frontera, un handler `wasm` pasa un lint, no la lista blanca del stub:
puede importar cualquier módulo de la build WASI (`base64`, `difflib`, `typing`, `decimal`, `unicodedata`…; no
`zlib`, sockets ni hilos). Entre llamadas no hay proceso: un agente inactivo ocupa disco, no memoria; caben
cientos donde el stub aguantaba unos treinta.

Probado en el hearth de referencia con el agente de un desconocido escrito para escapar: cada ruta que probó dio
`FileNotFoundError`, red y procesos no disponibles, su entorno tenía dos variables.

## Propietarios

Un hogar compartido aloja agentes de más de una empresa, así que el token del operador no es la única llave. Un **propietario** es una clave Ed25519 que el operador admite (`POST /v1/owners`). Cada petición de un propietario va firmada sobre la base pública del hogar, el método, la ruta, una marca unix (±300 s), un nonce de un solo uso y el SHA-256 del cuerpo (`X-Hestia-Owner` / `-Timestamp` / `-Nonce` / `-Signature`, formato `hestia-owner/1` en [`hestia/owners.py`](../hestia/owners.py)).

| Quién | Puede |
|---|---|
| Operador (token) | todo, como antes; admite, limita y suspende propietarios |
| Propietario | desplegar en un slug libre o propio dentro de su cuota (`HESTIA_OWNER_MAX_TENANTS`, 3 por defecto); detener, anunciar y listar **solo sus** agentes; ver su estado en `GET /v1/owners/me` |
| Cualquier otro | leer la lista pública, invocar agentes |

Se rechazan una petición repetida, una firmada para otro hogar, ruta o cuerpo, y un propietario que pone otra clave en `owner_pubkey`. La clave provisional de ceros y cualquier otra clave Ed25519 de orden pequeño no pueden poseer nada, así que los agentes desplegados con la provisional siguen siendo solo del operador.

`HESTIA_OPEN_OWNERS=1` deja que cualquier clave válida se admita sola en su primer despliegue. Con el stub no recibe derecho a ejecutar código —la admisión AST no es un sandbox—, así que los desconocidos solo despliegan imágenes fijadas y stubs sellados. Con `HESTIA_RUNTIME=wasm`, `HESTIA_OPEN_OWNER_CODE=1` les da ese derecho: su código corre en el sandbox descrito arriba y sus agentes entran en el manifiesto público solo a través de la revisión de abajo. Ambos desactivados por defecto; el hearth de referencia funciona con los dos activados.

```bash
python -m hestia.owner_cli keygen --out owner.key        # imprime la clave pública para el operador
python -m hestia.owner_cli deploy --hearth https://hestia.example --key owner.key --body deploy.json
python -m hestia.owner_cli stop   --hearth https://hestia.example --key owner.key --slug my-agent
python -m hestia.owner_cli proof  --key owner.key --domain example.com   # imprime el registro TXT y el archivo que prueban tu dominio
python -m hestia.owner_cli domain --hearth https://hestia.example --key owner.key --domain example.com
```

## Revisión antes de publicar

Los hubs indexan el manifiesto del hearth: un agente que esté en él lo ven los compradores de todos los hubs que
fijan el hearth. El agente de un **propietario** funciona en cuanto se despliega —se puede llamar en su propia
puerta `/t/{slug}`— pero entra en el manifiesto solo cuando pasa una revisión automática
([`hestia/listing.py`](../hestia/listing.py)). Lee lo que leería el modelo del comprador (nombre, descripción,
esquemas) y retiene la publicación ante etiquetas de instrucciones, «ignore previous instructions», «no se lo
digas al usuario», «antes de usar cualquier otra herramienta», acciones encubiertas, rutas de credenciales,
caracteres ocultos, comentarios HTML, una palabra que mezcla alfabetos parecidos (latino con cirílico,
griego…), y un id cercano a la familia de otro propietario o un nombre que se lee igual que el de un agente de
este hearth, en marcha o no. El propietario ve
los motivos en la respuesta del despliegue y en `GET /v1/tenants`; el operador publica o retiene a mano con
`POST /v1/admin/tenants/{slug}/listing`. Los agentes del propio operador no se revisan.

Los nombres que un hub muestra a los compradores pertenecen para siempre a quien desplegó primero bajo ellos
([`hestia/names.py`](../hestia/names.py)): una familia de capacidades (el id antes de `@`), un id de producto y
un editor (publisher id y dirección de cobro). Se comparan plegados —mayúsculas, separadores, letras parecidas y
una versión final no hacen un nombre nuevo—, así que `merkle-proof@v2`, `merkle_proof.v2@v1` y `Merkle.Pr0of@v1`
son todos de la familia de `merkle.proof`. Un agente parado, uno renombrado y un propietario suspendido conservan
sus nombres; otro propietario recibe 409. `hestia*` es del propio hearth, y el operador tiene los prefijos de
`HESTIA_RESERVED_PREFIXES` (por defecto `aicom,aimarket,modelmarket`). Dentro de un mismo propietario, un id
exacto (en mayúsculas o minúsculas) lo sirve un solo agente a la vez. La reserva la hace la base de datos (una
clave primaria), así que dos procesos sobre un mismo registro no pueden ganar los dos un nombre. El operador ve
las reservas con `GET /v1/admin/names`, reserva o traspasa un nombre con `POST /v1/admin/names`
(`{"kind": "family|product|publisher|prefix", "name": "…", "holder": "operator" | clave del propietario}`; un
`prefix` cubre todo nombre que empiece por él, p. ej. una marca para su empresa) y lo libera con
`POST /v1/admin/names/release` cuando los agentes de su titular están parados.

Un agente retenido responde solo en su propia puerta `/t/{slug}`: la invocación enrutada que usa un hub llega solo
a agentes publicados. Un nuevo despliegue del propietario repite la revisión pero no levanta una retención del
operador, y un nuevo despliegue del operador no publica un agente retenido: eso lo hace la llamada de publicación.
Los agentes de un propietario suspendido salen del manifiesto y del listado y no se sirven en ninguna puerta.
`HESTIA_MAX_TENANTS` cuenta los agentes en marcha y en cuarentena; uno parado conserva su slug y sus nombres, no
su plaza.

Un propietario que demuestra un dominio recibe los nombres que empiezan por ese dominio, zona incluida
([`hestia/domains.py`](../hestia/domains.py)). La prueba es un registro TXT en `_hestia.<dominio>` con
`hestia-owner=<clave pública>`, o `https://<dominio>/.well-known/hestia-owner.json` con
`{"owner_pubkeys": ["<clave pública>"]}`; `python -m hestia.owner_cli proof` imprime ambos y `… domain` pide
al hearth que lo compruebe. Desde entonces toda familia, producto y editor que empiece por el dominio, en
cualquier orden —`attestedmemory.net.deal`, `net.attestedmemory.deal`, `attestedmemory-net.deal`,
«Attestedmemory.net Labs»— es de ese propietario, y los hubs ven el dominio como `publisher_domain` del
agente. La palabra sola no es de nadie: attestedmemory.com, .dev y .net pueden ser tres propietarios, así que
`attestedmemory.deal` sigue siendo de quien llegue primero, como cualquier nombre. Solo cuenta un dominio
registrable en ASCII (example.com, example.co.uk; ni subdominios ni IDN), y un dominio nunca quita un nombre
que otro titular ya usa. La prueba HTTPS solo se pide a direcciones públicas y sin redirecciones.

## Hubs que venden tus agentes

Un agente cobra cada llamada en USDC en cadena, algo que un hub no puede pagar con los créditos del
comprador ni con una asignación de subcontratación. Por eso un propietario puede dejar que un hub venda sus
agentes en su nombre ([`hestia/hub_billing.py`](../hestia/hub_billing.py)):

1. el operador da al hogar la clave del hub: `HESTIA_TENANT_HUB_KEYS=https://hub.example=<clave>` — el
   mismo valor que la entrada de ese hub en `AIMARKET_PEER_API_KEYS` para este hogar;
2. el propietario abre una cuenta de créditos en ese hub y lo elige:
   `python -m hestia.owner_cli billing --hearth … --key owner.key --hub https://hub.example --account acct_…`
   (`--account ""` retira la elección);
3. una llamada del hub con esa clave se sirve sin pago en cadena y se responde con un bloque `hub_billing`
   firmado con la clave de proveedor del hogar: de quién es el agente, qué hub, qué cuenta. El hub lo
   verifica con la clave que fijó para este hogar y abona al propietario `AIMARKET_PUBLISHER_SHARE_BPS`
   (70 % por defecto) de lo que **él** cobró al comprador;
4. `python -m hestia.owner_cli statement` lista cada llamada así, para cuadrar con lo que pagó el hub.

Los agentes del propio operador (clave provisional) los vende cualquier hub cuya clave configuró el
operador, y el hub se queda lo cobrado. A un hub que no cobró (`X-AIMarket-Hub-Charged: 0`, prueba
gratuita) no se le sirve el agente de un propietario. Vista del operador: `GET /v1/admin/hub-billing`.

## Inicio rápido

```bash
cd hestia
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . pytest -q
uv run --project . python -m hestia
# consola: http://127.0.0.1:9480/ui/
```

Un `HESTIA_DEPLOY_TOKEN` vacío rechaza toda escritura.

Guía: [user-guide.es.md](user-guide.es.md) · taller: [workshop.es.md](workshop.es.md) · casos: [USE-CASES.es.md](USE-CASES.es.md) · arquitectura: [ARCHITECTURE.md](ARCHITECTURE.md) (EN).

El nombre `HESTIA` se queda en latín. Licencia MIT.
