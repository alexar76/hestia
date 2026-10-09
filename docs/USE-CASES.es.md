# Casos de uso

🌐 [English](USE-CASES.md) · [Русский](USE-CASES.ru.md) · **Español** · [Français](USE-CASES.fr.md) · [中文](USE-CASES.zh.md)

## Alojar un vendedor que debe seguir encendido

Un agente de clima, río o documentos cuya URL los compradores pegan 24/7. Despliégalo en Hestia, no en un tmux olvidado.

## Darle a `create-aimarket-agent` una URL real

El CLI andamia `capability.json` y un handler. Hestia es la dirección que faltaba: proceso aislado + `/t/{slug}/invoke`.

## Mantener honesto al Hub

Listar en el Hub solo cuando la URL del hogar está viva. El anuncio es explícito para que un deploy roto no cree un SKU de pago.

## Admisión opcional antes de arrancar

`HESTIA_THEMIS_URL` hacia THEMIS: un `reject` nunca llega a running.

## Para qué no usar Hestia

| Quieres | Usa |
|---|---|
| Ver todos los productos de Factory | Búsqueda del Hub |
| Autopublicar cuando CI está verde | Política de Hub/THEMIS, no el hogar |
| Red team de la federación en vivo | MOMUS |
| Admitir una fila de catálogo | THEMIS |
| Consumir como comprador | ARGUS |
| Componer un grafo de capacidades | HEPHAESTUS |
