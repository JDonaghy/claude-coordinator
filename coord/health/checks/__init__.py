"""Seed health checks (#1628).

**This file imports nothing on purpose.**  ``coord.health.registry.discover``
walks this package with :func:`pkgutil.iter_modules` and imports every module
it finds, so a new check is installed by dropping ``coord/health/checks/
<name>.py`` here and decorating its probe with ``@check(...)`` — no edit to
this ``__init__``, no edit to a renderer, no edit to the CLI.

``tests/test_health_registry.py::test_adding_a_check_touches_only_its_own_module``
enforces that: it writes a throwaway module into this directory, asserts it
shows up in the report and the JSON, and asserts the renderer/CLI/registry
files are byte-identical before and after.
"""
