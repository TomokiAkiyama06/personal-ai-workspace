"""Passkeys (WebAuthn) and Passkey Step-up (PAW-023; Decision 0025).

Modules (see ``apps/backend/README.md`` "Passkey / Step-up" and Decision 0025):

* ``models``: the ``user_passkeys`` and ``passkey_challenges`` tables (revision
  ``0023``);
* ``config``: ``PasskeyConfig``, the relying party and the allowed origins, built
  from the settings (``None`` when the feature is not configured);
* ``types``: the strictly validated shape of what a browser sends;
* ``ceremony``: the ONLY module that imports the WebAuthn library (``webauthn``,
  py_webauthn): builds the options and verifies a registration / an assertion;
* ``store``: ``PasskeyRegistry``, every SQL statement on the two tables, and
  ``PasskeyEnrollment`` (what ``AuthService`` asks) plus the credential invalidator;
* ``service``: ``PasskeyService`` (register, list, revoke, begin an authentication)
  and ``PasskeyStepUpVerifier`` (the ``StepUpVerifier`` of ``AuthService``);
* ``approvals``: ``PasskeyApprovalStepUp``, the Tool Broker's strong-approval
  ``StepUpVerifier``.

Nothing is imported here: importing the package must stay free of side effects
(the models are imported by Alembic's ``env.py``).
"""
