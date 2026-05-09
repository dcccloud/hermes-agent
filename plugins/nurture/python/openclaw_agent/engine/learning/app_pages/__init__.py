"""Per-app page state graphs for state-aware recipe generation.

Seed definitions live in ``<app>.seed.json`` files (this directory).
Runtime code should use ``AppGraph`` via ``get_app_graph(app)`` from
``operation_engine.learning.app_graph``, which hot-reloads JSON.
"""
