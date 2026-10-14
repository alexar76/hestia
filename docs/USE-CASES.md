# Use cases

🌐 **English** · [Русский](USE-CASES.ru.md) · [Español](USE-CASES.es.md) · [Français](USE-CASES.fr.md) · [中文](USE-CASES.zh.md)

## Host a seller that must stay on

A weather, river, or document agent whose URL buyers will hit 24/7. You deploy it onto Hestia so the process is not a forgotten tmux on a laptop.

## Give `create-aimarket-agent` an actual URL

The CLI scaffolds `capability.json` and a handler. Hestia is the missing listen address: isolated process + `/t/{slug}/invoke`.

## Keep Hub honest

List on Hub only after the hearth URL is live. Announce is explicit so a broken deploy cannot create a paid SKU.

## Optional admission before start

Point `HESTIA_THEMIS_URL` at THEMIS so a `reject` never becomes a running tenant.

## What not to use Hestia for

| Want | Use instead |
|---|---|
| Browse every Factory product | Hub search |
| Auto-publish when CI goes green | That is a Hub/THEMIS policy, not a hearth |
| Red-team the live federation | MOMUS |
| Admit a catalogue row | THEMIS |
| Consume as a buyer | ARGUS |
| Compose a capability graph | HEPHAESTUS |
