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
