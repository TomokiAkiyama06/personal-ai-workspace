"""The Admin's policy of the CLI agents' models over HTTP (issue #341,
Decision 0103, 2 A, 3 A, 5 A, 7 A and 9 A): real PostgreSQL.

* ``GET`` shows the runtime configuration's model while no policy is stored,
  and the verified candidates;
* ``PUT`` stores verified models only (422 ``model_not_verified``), exactly
  one default per kind (422 ``validation_error``), keeps the kinds it does
  not name, and writes the history and its ``admin.agent_models.update``
  audit event (no model name in it);
* both need ``admin.config.manage``; the change needs the Passkey Step-up
  (a refusal is audited); an audit event that cannot be written stores
  nothing (one transaction);
* a change of a kind's default model starts that connection's probe once
  (trigger ``policy``), and a change that keeps the default starts none.
"""

from unittest.mock import patch

from paw_backend.agent_models import AgentModelPolicyStore, AgentModelService
from paw_backend.connections import ConnectionKind

from .agent_models_support import TEST_CODEX_MODEL, verified_pairs_of
from .auth_http_support import HttpTestCase, error_code, requires_postgres
from .test_connections_probe_api import HeldProbe, ProbeHttpCase

PATH = "/api/v1/admin/agent-models"
CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE
# No Codex pair is verified in the code yet: the tests run on a test pair
# (``agent_models_support``); ``ShippedPairsTest`` runs on the shipped ones.
MODEL = TEST_CODEX_MODEL
SOL = {"model": MODEL, "default": True, "description": "Coding"}


class AgentModelsCase(HttpTestCase):
    configured = {CODEX: MODEL, CLAUDE: "claude-opus-5-5"}

    shipped_pairs = False

    def setUp(self) -> None:
        super().setUp()
        if not self.shipped_pairs:
            self.enterContext(verified_pairs_of())
        with self.engine.begin() as connection:
            connection.exec_driver_sql("TRUNCATE agent_model_policies")
            connection.exec_driver_sql("TRUNCATE agent_model_policy_changes")
        self.use_service(self.configured)
        self.admin_id = self.make_user("admin-one", role="admin")
        self.user_id = self.make_user("alice")
        self.admin = self.login_token("admin-one")
        self.user = self.login_token("alice")

    def use_service(self, configured) -> None:
        self.app.state.agent_models = AgentModelService(
            AgentModelPolicyStore(self.database),
            self.app.state.authorizer,
            configured=configured,
        )

    def put(self, *policies, token=None):
        return self.call(
            "PUT",
            PATH,
            token=token or self.admin,
            json={"policies": list(policies)},
        )

    def kinds(self, response) -> dict:
        self.assertEqual(response.status_code, 200, response.text)
        return {item["kind"]: item for item in response.json()["kinds"]}

    def stored(self) -> list:
        return self.rows(
            "SELECT kind, model, is_default, description, updated_by"
            " FROM agent_model_policies ORDER BY kind, model"
        )


@requires_postgres
class ReadTest(AgentModelsCase):
    def test_without_a_policy_the_configured_model(self):
        kinds = self.kinds(self.call("GET", PATH, token=self.admin))
        self.assertEqual(
            kinds["codex"],
            {
                "kind": "codex",
                "source": "configuration",
                "models": [{"model": MODEL, "default": True, "description": None}],
                "default_model": MODEL,
                "configured_model": MODEL,
                "candidates": [{"model": MODEL, "versions": ["0.156.1"]}],
                "updated_at": None,
            },
        )
        self.assertEqual(
            kinds["claude"]["candidates"],
            [{"model": "claude-opus-5-5", "versions": ["2.1.301"]}],
        )
        self.use_service({})
        nothing = self.kinds(self.call("GET", PATH, token=self.admin))
        self.assertEqual(
            (nothing["codex"]["source"], nothing["codex"]["models"]), ("none", [])
        )


@requires_postgres
class UpdateTest(AgentModelsCase):
    def test_a_policy_is_stored_with_its_history_and_audit(self):
        self.fake_passkey_step_up(self.admin)
        kinds = self.kinds(self.put({"kind": "codex", "models": [SOL]}))
        self.assertEqual(kinds["codex"]["source"], "policy")
        self.assertEqual(kinds["codex"]["models"], [SOL])
        self.assertIsNotNone(kinds["codex"]["updated_at"])
        # The kind it did not name keeps the configuration's model.
        self.assertEqual(kinds["claude"]["source"], "configuration")
        self.assertEqual(
            self.stored(), [("codex", MODEL, True, "Coding", self.admin_id)]
        )
        (change,) = self.rows(
            "SELECT change_id, kind, model, is_default, changed_by"
            " FROM agent_model_policy_changes"
        )
        self.assertEqual(change[1:], ("codex", MODEL, True, self.admin_id))
        (event,) = [
            r for r in self.audit_rows() if r.action == "admin.agent_models.update"
        ]
        self.assertEqual(
            (event.decision, event.reason, event.actor_id, event.correlation_id),
            ("allow", "succeeded", self.admin_id, change.change_id),
        )
        self.assertNotIn(MODEL, str(tuple(event)))
        listed = self.kinds(self.call("GET", PATH, token=self.admin))
        self.assertEqual(listed["codex"]["models"], [SOL])

    def test_a_model_outside_the_verified_ones_is_refused(self):
        self.fake_passkey_step_up(self.admin)
        for model in ("gpt-6-sol", "gpt-6-astra", "gpt-5.4", "claude-opus-5-5"):
            with self.subTest(model):
                response = self.put(
                    {"kind": "codex", "models": [{"model": model, "default": True}]}
                )
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (422, "model_not_verified"),
                )
        alias = self.put(
            {"kind": "claude", "models": [{"model": "opus", "default": True}]}
        )
        self.assertEqual(error_code(alias), "model_not_verified")
        self.assertEqual(self.stored(), [])

    def test_exactly_one_default_per_kind(self):
        self.fake_passkey_step_up(self.admin)
        for name, body in {
            "no default": [{"kind": "codex", "models": [{"model": MODEL}]}],
            "twice": [{"kind": "codex", "models": [SOL, SOL]}],
            "no model": [{"kind": "codex", "models": []}],
            "a kind twice": [{"kind": "codex", "models": [SOL]},
                             {"kind": "codex", "models": [SOL]}],
            "an unknown kind": [{"kind": "gemini", "models": [SOL]}],
            "an unknown field": [{"kind": "codex", "models": [{**SOL, "x": 1}]}],
        }.items():  # fmt: skip
            with self.subTest(name):
                response = self.put(*body)
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (422, "validation_error"),
                )
        self.assertEqual(self.stored(), [])

    def test_admin_config_manage_and_the_step_up(self):
        self.fake_passkey_step_up(self.user)
        for response in (
            self.call("GET", PATH, token=self.user),
            self.put({"kind": "codex", "models": [SOL]}, token=self.user),
        ):
            self.assertEqual(
                (response.status_code, error_code(response)), (403, "forbidden")
            )
        self.assertEqual(self.call("GET", PATH).status_code, 401)
        refused = self.put({"kind": "codex", "models": [SOL]})
        self.assertEqual(
            (refused.status_code, error_code(refused)), (403, "step_up_required")
        )
        self.assertEqual(self.stored(), [])
        summary = self.audit_summary()
        self.assertEqual(
            summary[("admin.agent_models.update", "deny", "step_up_required")], 1
        )

    def test_an_audit_that_cannot_be_written_stores_nothing(self):
        self.fake_passkey_step_up(self.admin)
        with patch(
            "paw_backend.agent_models.store.AUDIT_INSERT_SQL",
            "INSERT INTO audit_events_missing VALUES (%(id)s)",
        ):
            response = self.put({"kind": "codex", "models": [SOL]})
        self.assertEqual(
            (response.status_code, error_code(response)), (503, "service_unavailable")
        )
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.rows("SELECT 1 FROM agent_model_policy_changes"), [])

    def test_a_second_change_replaces_the_kinds_models(self):
        self.fake_passkey_step_up(self.admin)
        self.kinds(self.put({"kind": "codex", "models": [SOL]}))
        kinds = self.kinds(
            self.put(
                {"kind": "codex", "models": [{"model": MODEL, "default": True}]},
                {"kind": "claude",
                 "models": [{"model": "claude-opus-5-5", "default": True}]},
            )
        )  # fmt: skip
        self.assertEqual(kinds["codex"]["models"][0]["description"], None)
        self.assertEqual(
            [row[:3] for row in self.stored()],
            [("claude", "claude-opus-5-5", True), ("codex", MODEL, True)],
        )
        changes = self.rows("SELECT DISTINCT change_id FROM agent_model_policy_changes")
        self.assertEqual(len(changes), 2)


@requires_postgres
class ShippedPairsTest(AgentModelsCase):
    shipped_pairs = True

    def test_gpt_6_sol_is_not_verified_and_codex_has_no_candidate(self):
        # Its recorder smoke on 0.156.1 failed (``agent_models/verified.py``).
        self.fake_passkey_step_up(self.admin)
        response = self.put(
            {"kind": "codex", "models": [{"model": "gpt-6-sol", "default": True}]}
        )
        self.assertEqual(
            (response.status_code, error_code(response)), (422, "model_not_verified")
        )
        kinds = self.kinds(self.call("GET", PATH, token=self.admin))
        self.assertEqual(kinds["codex"]["candidates"], [])
        claude = self.put(
            {
                "kind": "claude",
                "models": [{"model": "claude-opus-5-5", "default": True}],
            }
        )
        self.assertEqual(claude.status_code, 200, claude.text)


@requires_postgres
class ProbeAfterChangeTest(ProbeHttpCase):
    def setUp(self) -> None:
        super().setUp()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("TRUNCATE agent_model_policies")

    def use_models(self, configured) -> None:
        self.app.state.agent_models = AgentModelService(
            AgentModelPolicyStore(self.database),
            self.app.state.authorizer,
            configured=configured,
        )

    def put_claude(self):
        return self.call(
            "PUT",
            PATH,
            token=self.admin,
            json={
                "policies": [
                    {
                        "kind": "claude",
                        "models": [{"model": "claude-opus-5-5", "default": True}],
                    }
                ]
            },
        )

    def test_a_new_default_starts_one_probe(self):
        adapter = HeldProbe()
        self.use_probes(adapter)
        self.attach_claude()
        self.release(adapter)
        # The configuration's model was another one: the default changes.
        self.use_models({CLAUDE: "claude-older-model"})
        adapter.release.clear()

        response = self.put_claude()

        self.assertEqual(response.status_code, 200, response.text)
        triggers = [row.trigger for row in self.probes()]
        self.assertEqual(triggers, ["attach", "policy"])
        self.release(adapter)
        self.assertEqual(self.probes()[-1].state, "finished")
        self.assertEqual(adapter.health_checks, 2)
        self.assertEqual(self.status_of("claude")["last_probe"]["trigger"], "policy")

    def test_the_same_default_starts_none(self):
        adapter = HeldProbe()
        self.use_probes(adapter)
        self.attach_claude()
        self.release(adapter)
        self.use_models({CLAUDE: "claude-opus-5-5"})

        self.assertEqual(self.put_claude().status_code, 200)
        self.assertEqual([row.trigger for row in self.probes()], ["attach"])

    def test_without_a_connection_the_change_stands(self):
        self.use_probes(HeldProbe())
        self.use_models({CLAUDE: "claude-older-model"})
        response = self.put_claude()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.probes(), [])
