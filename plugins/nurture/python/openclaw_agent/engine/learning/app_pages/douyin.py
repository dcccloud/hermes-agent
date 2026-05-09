"""Douyin (抖音) page state graph — thin proxy over seed JSON.

Seed definitions live in ``douyin.seed.json`` (same directory).
Runtime code should use ``AppGraph`` via ``get_app_graph("douyin")``
which hot-reloads both seed and learned JSON without restart.

This module exists only for backward compatibility.
"""
