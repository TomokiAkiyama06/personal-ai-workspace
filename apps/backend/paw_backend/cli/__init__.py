"""Server-local management commands: ``python -m paw_backend.cli``.

These run on the server, with the database credentials from the environment.
They are not part of the HTTP API and cannot be reached from a browser or from
``apps/cli``, which only talks to the public HTTP API.

``owner-setup`` / ``owner-recover`` (``owner``, PAW-021) and
``audit-retention-run`` / ``audit-retention-check`` (``retention``, Issue #117);
``dispatch`` picks the module from the first argument.
"""
