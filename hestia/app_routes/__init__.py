"""Route groups of the HESTIA app, one module per area.

Each module has one ``register(...)`` that ``hestia.app.build_app`` calls in route order,
passing the app state its routes read as keyword arguments.
"""
