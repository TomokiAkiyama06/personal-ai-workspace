"""Login, sessions and passwords (PAW-022).

Modules (see ``apps/backend/README.md`` "Login / Session / Password Policy" and
Decision 0015):

* ``passwords``: the password policy and Argon2id hashing;
* ``sessions``, ``tokens``: server-side sessions (only the hash of the id is
  stored) and the keys of the throttles and the audit trail;
* ``throttle``: progressive login backoff and the token endpoint's rate limits;
* ``service``: ``AuthService``, the behaviour behind every authentication route;
* ``auth_policy``, ``state``: the Owner-controlled Passkey policy and the Step-up /
  Passkey seams PAW-023 plugs into;
* ``principals``: the session cookie as the ``PrincipalProvider`` of ``authz``;
* ``csrf``: the Origin check of state-changing requests;
* ``wiring``: builds the services and installs them on the application.

Nothing is imported here: importing the package must stay free of side effects
(the models are imported by Alembic's ``env.py``).
"""
