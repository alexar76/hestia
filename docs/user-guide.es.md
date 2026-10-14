# Guía de usuario

🌐 [English](user-guide.md) · [Русский](user-guide.ru.md) · **Español** · [Français](user-guide.fr.md) · [中文](user-guide.zh.md)

**Taller de operador (90 min, no es un curso):** [workshop.es.md](workshop.es.md) · [EN](workshop.md) · [RU](workshop.ru.md) · [FR](workshop.fr.md) · [ZH](workshop.zh.md)

## Qué estás operando

Un **hogar**: máquinas bajo tu control que ejecutan proveedores AIMarket aislados. La consola `/ui/` habla con **este** proceso. No es la búsqueda del Hub.

Los agentes aparecen en el roster solo tras un `POST /v1/tenants` (o `hestia.host.deploy@v1`) correcto **en este host**.

## Local

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
uv sync --extra dev --project .
uv run --project . python -m hestia
```

Abre http://127.0.0.1:9480/ui/ — pega el token, despliega el eco de demo, entra a `/t/demo-echo/health`.

## Invocar un inquilino

```bash
curl -sS http://127.0.0.1:9480/t/demo-echo/invoke \
  -H 'content-type: application/json' \
  -d '{"hello":"hearth"}'
```

## Handler propio

Solo Python. Debe definir `handle(payload) -> dict`.

- `HESTIA_RUNTIME=wasm` (el hearth de referencia): el handler corre en el sandbox WebAssembly, una instancia nueva de CPython para WASI por llamada, en un contenedor sin red ni secretos. Puede importar cualquier módulo de la build WASI (`base64`, `difflib`, `typing`, `decimal`…; no `zlib`, sockets ni hilos). No ve archivos del host, red, procesos ni el entorno; memoria (256 MiB), tiempo (10 s) y salida (4 MiB) limitados. Los propietarios auto-admitidos pueden desplegar código aquí con `HESTIA_OPEN_OWNER_CODE=1`.
- `HESTIA_RUNTIME=stub`: admisión AST con un conjunto cerrado de stdlib; sin `os`, red ni archivos. La admisión AST **no** es un sandbox.

## Imagen fijada

```bash
export HESTIA_RUNTIME=docker
export HESTIA_ALLOW_IMAGE_DIGESTS=sha256:<64 hex>
```

El digest ya debe estar en el **mismo Docker** que ya usas. Hestia no ejecuta `docker build`. Es un proceso **en el host**, no un socket en la caja compose (eso sería un shell al host). El `docker.sock` del host se rechaza salvo `HESTIA_ALLOW_HOST_DOCKER=1`.

## Anunciar al Hub

Desactivado por defecto. Hace falta `HESTIA_HUB_URL` y un `POST /v1/tenants/{slug}/announce` (o `HESTIA_AUTO_ANNOUNCE=1`). Alojar ≠ listar.

## Autoalojado frente a la flota AICOM

Tú ejecutas Hestia — los inquilinos viven en **tu** máquina. El hogar de referencia AICOM vive detrás de `hestia.modelmarket.dev`. Factory no despliega al terminar un pipeline.

## Flota (hogar de referencia)

En el host que servirá `hestia.modelmarket.dev` — el registro A debe apuntar **aquí**. No inventes la caja. Un token vacío rechaza toda escritura.

```bash
export HESTIA_DEPLOY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export HESTIA_PUBLIC_BASE=https://hestia.modelmarket.dev
sudo ./scripts/deploy_hestia.sh
```

Desde un portátil, `./scripts/deploy_hestia.sh --remote <host-ssh>` sincroniza la porción de Hestia y ejecuta el mismo script allí.

La configuración vive en `hestia/.env` del host: el entorno solo la rellena en la primera ejecución, y un valor que difiere del archivo se rechaza. Cada ejecución respalda el registro (`pg_dump`) y etiqueta las imágenes en marcha para poder volver atrás, arranca también el ejecutor del sandbox cuando `HESTIA_RUNTIME=wasm` y espera a que ambos estén sanos. Rechaza un host al que no apunta el registro A; `--new-host` es para un primer despliegue antes de mover el DNS. La caja de compose nunca recibe `docker.sock`. nginx es el borde TLS delante de `127.0.0.1:9480`.
