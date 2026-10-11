"""Server-local management commands: ``python -m paw_backend.cli``.

These run on the server, with the database credentials from the environment.
They are not part of the HTTP API and cannot be reached from a browser or from
``apps/cli``, which only talks to the public HTTP API.

``owner-setup`` / ``owner-recover`` (``owner``, PAW-021),
``audit-retention-run`` / ``audit-retention-check`` (``retention``, Issue #117)
``memory-projection-run`` / ``memory-projection-check``
(``memory_projection``, PAW-045) and ``recovery-backup-run`` /
``recovery-backup-check`` / ``recovery-restore`` (``recovery``, PAW-047) and
``deploy-*`` (``deploy``, PAW-068);
``dispatch`` picks the module from the first
argument.
"""
