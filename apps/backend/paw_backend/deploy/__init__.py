"""Update / Rollback of the Personal AI Workspace, the database side (PAW-068,
Issue #54, Decision 0079).

The release tool (``apps/backend/deploy/release/paw_release.py``, standard
library only) switches between versioned releases; it runs the server-local
commands of ``paw_backend.cli.deploy`` of a release for what needs the
database:

* :mod:`.maintenance`: an update's maintenance (no task starts, the running ones
  are held and drain to a checkpoint, resumed at the end);
* :mod:`.restore_points`: the database restore point before a migration (a
  ``pg_dump`` outside the Recovery Repository, verified by restoring it into a
  scratch database) and the restore of one on rollback;
* :mod:`.audit`: their rows in ``audit_events``.

``deploy-status`` / ``deploy-precheck`` (``paw_backend.cli.deploy``) report the
read-only facts the release tool compares before and after an update.
"""
