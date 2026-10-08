"""``/api/v1/memory/preferences/*`` through the application (issue #38, PAW-044).

Real sessions on a real PostgreSQL. The rules of the flow are tested on the service
(``test_preference_service.py``, ``test_preference_flow.py``); this is the HTTP
layer: authentication, the answers' shape, the error codes, the model interpreter
from the application's state.
"""

import json
import uuid

from sqlalchemy import text

from .auth_http_support import T0, HttpTestCase, error_code, requires_postgres

API = "/api/v1/memory/preferences"
# REQUIREMENTS.md's example of a free-text answer.
EXAMPLE = "開発系のProjectだけ適用して。ただしmainへのMergeは毎回確認して"


class Model:
    """A model interpreter for the application's state."""

    async def interpret(self, text, candidate):
        return json.dumps(
            {
                "scope": "user",
                "apply_to": None,
                "rule": f"{candidate} (model)",
                "exceptions": [],
                "strength": "default",
                "risk_level": "low",
                "expires_at": None,
            }
        )


@requires_postgres
class PreferenceHttpTest(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE memories, conversations CASCADE"))
            connection.execute(text("TRUNCATE projects CASCADE"))
        self.alice = self.make_user("alice")
        self.bob = self.make_user("bob")
        self.token = self.login_token("alice")

    # -- seeding (SQL) ---------------------------------------------------------------

    def execute(self, sql: str, **params):
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params)

    def seed_candidate(self, key: str, content: str, owner=None) -> uuid.UUID:
        owner = owner or self.alice
        memory_id = self.execute(
            "INSERT INTO memories (created_at) VALUES (:t) RETURNING id", t=T0
        ).scalar_one()
        self.execute(
            "INSERT INTO memory_versions (memory_id, version_number, scope,"
            " owner_user_id, memory_type, title, content, status, confirmation_state,"
            " freshness_policy, actor_type, created_at) VALUES (:m, 1, 'user', :o,"
            " 'worker_candidate', :k, :c, 'active', 'inferred', 'permanent', 'system',"
            " :t)",
            m=memory_id,
            o=owner,
            k=key,
            c=content,
            t=T0,
        )
        self.execute(
            "INSERT INTO memory_consolidation_keys (owner_user_id, key, memory_id,"
            " applied_conversation_id, applied_event_sequence, applied_recorded_at)"
            " VALUES (:o, :k, :m, gen_random_uuid(), 0, :t)",
            o=owner,
            k=key,
            m=memory_id,
            t=T0,
        )
        return memory_id

    def seed_held(self, key: str, content: str) -> uuid.UUID:
        conversation = self.execute(
            "INSERT INTO conversations (owner_user_id) VALUES (:o) RETURNING id",
            o=self.alice,
        ).scalar_one()
        message = self.execute(
            "INSERT INTO messages (conversation_id, turn_id, event_sequence, role,"
            " content) VALUES (:c, gen_random_uuid(), 0, 'user', 'x') RETURNING id",
            c=conversation,
        ).scalar_one()
        outcome = {
            "contract": "memory-worker-output-v1",
            "items": [
                {
                    "index": 0,
                    "result": "held_high_risk",
                    "key": key,
                    "candidate": {
                        "scope": "user",
                        "state": "inferred",
                        "content": content,
                        "supersedes": None,
                        "conflicts_with": [],
                    },
                }
            ],
        }
        return self.execute(
            "INSERT INTO memory_journal_entries (conversation_id, message_id, turn_id,"
            " event_sequence, owner_user_id, state, consolidated_at, outcome)"
            " SELECT :c, :m, turn_id, 0, :o, 'consolidated', now(),"
            " CAST(:outcome AS jsonb) FROM messages WHERE id = :m RETURNING id",
            c=conversation,
            m=message,
            o=self.alice,
            outcome=json.dumps(outcome),
        ).scalar_one()

    def get(self, path: str, **options):
        return self.call("GET", f"{API}{path}", token=self.token, **options)

    def post(self, path: str, body: dict, **options):
        options.setdefault("token", self.token)
        return self.call("POST", f"{API}{path}", json=body, **options)

    def versions(self, memory_id) -> list:
        return self.rows(
            "SELECT version_number, status, confirmation_state, scope"
            " FROM memory_versions WHERE memory_id = :m ORDER BY version_number",
            m=memory_id,
        )

    # -- tests -------------------------------------------------------------------------

    def test_every_route_needs_a_session(self):
        memory_id = self.seed_candidate("k", "use tabs")
        entry = self.seed_held("h", "merge it")
        for method, path, body in (
            ("GET", "/candidates", None),
            ("POST", f"/memories/{memory_id}/confirm", {"expected_version": 1}),
            ("POST", f"/memories/{memory_id}/reject", {"expected_version": 1}),
            (
                "POST",
                f"/memories/{memory_id}/interpret",
                {"expected_version": 1, "text": "x"},
            ),
            ("POST", f"/held/{entry}/0/confirm", {"scope": "user"}),
            ("POST", f"/held/{entry}/0/reject", {}),
            ("POST", f"/held/{entry}/0/interpret", {"text": "x"}),
        ):
            response = self.call(method, f"{API}{path}", json=body)
            self.assertEqual(response.status_code, 401, (method, path))
        self.assertEqual(len(self.versions(memory_id)), 1)

    def test_the_candidates(self):
        memory_id = self.seed_candidate("indent_style", "use tabs")
        entry = self.seed_held("merge_policy", "merge it")
        self.seed_candidate("theirs", "x", owner=self.bob)

        response = self.get("/candidates")

        self.assertEqual(response.status_code, 200, response.text)
        found = {c["key"]: c for c in response.json()["candidates"]}
        self.assertEqual(set(found), {"indent_style", "merge_policy"})
        memory = found["indent_style"]
        self.assertEqual(
            (memory["kind"], memory["memory_id"], memory["version_number"]),
            ("memory", str(memory_id), 1),
        )
        self.assertEqual(memory["confirmation_state"], "inferred")
        self.assertEqual(memory["recommendation"]["scope"], "user")
        self.assertEqual(
            memory["options"],
            [
                {
                    "scope": "user",
                    "project_id": None,
                    "repo_id": None,
                    "recommended": True,
                }
            ],
        )
        self.assertEqual(memory["evidence"]["frequency"], 0)
        self.assertFalse(memory["ready"])
        held = found["merge_policy"]
        self.assertEqual(
            (held["kind"], held["entry_id"], held["item_index"], held["held_reason"]),
            ("held", str(entry), 0, "held_high_risk"),
        )
        self.assertEqual(held["evidence"]["risk_level"], "high")

    def test_confirm_reject_and_their_errors(self):
        memory_id = self.seed_candidate("k", "use tabs")
        path = f"/memories/{memory_id}/confirm"

        both = self.post(
            path,
            {
                "expected_version": 1,
                "scope": "user",
                "preference": {"scope": "user", "rule": "x"},
            },
        )
        self.assertEqual(
            (both.status_code, error_code(both)), (422, "validation_error")
        )
        missing = self.post(path, {"expected_version": 1, "scope": "project"})
        self.assertEqual(missing.status_code, 422)
        stale = self.post(path, {"expected_version": 2, "scope": "user"})
        self.assertEqual(
            (stale.status_code, error_code(stale)), (409, "memory_version_conflict")
        )
        project = uuid.uuid4()
        forbidden = self.post(
            path,
            {"expected_version": 1, "scope": "project", "project_id": str(project)},
        )
        self.assertEqual(
            (forbidden.status_code, error_code(forbidden)), (403, "forbidden")
        )

        response = self.post(path, {"expected_version": 1, "scope": "user"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            (body["version_number"], body["confirmation_state"], body["actor_name"]),
            (2, "confirmed", "alice"),
        )
        again = self.post(path, {"expected_version": 2, "scope": "user"})
        self.assertEqual(
            (again.status_code, error_code(again)),
            (409, "preference_candidate_changed"),
        )

        other = self.seed_candidate("k2", "use spaces")
        rejected = self.post(f"/memories/{other}/reject", {"expected_version": 1})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["version"]["confirmation_state"], "rejected")
        self.assertEqual(
            [tuple(row) for row in self.versions(other)],
            [
                (1, "superseded", "inferred", "user"),
                (2, "deprecated", "rejected", "user"),
            ],
        )

    def test_a_high_risk_candidate_needs_the_acknowledgement(self):
        entry = self.seed_held("merge_policy", "merge it")
        refused = self.post(f"/held/{entry}/0/confirm", {"scope": "user"})
        self.assertEqual(
            (refused.status_code, error_code(refused)),
            (409, "preference_high_risk_unacknowledged"),
        )
        self.assertEqual(self.rows("SELECT id FROM memory_versions"), [])
        response = self.post(
            f"/held/{entry}/0/confirm",
            {"scope": "user", "acknowledge_high_risk": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["content"], "merge it")

    def test_reject_a_held_candidate(self):
        entry = self.seed_held("h", "merge it")
        response = self.post(f"/held/{entry}/0/reject", {})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"version": None})
        self.assertEqual(self.get("/candidates").json(), {"candidates": []})
        bad_index = self.post(f"/held/{entry}/-1/reject", {})
        self.assertEqual(bad_index.status_code, 422)

    def test_interpret_and_confirm_the_preview(self):
        memory_id = self.seed_candidate("review", "review PRs first")
        response = self.post(
            f"/memories/{memory_id}/interpret",
            {
                "expected_version": 1,
                "text": EXAMPLE,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["interpreted_by"], "rules")
        self.assertEqual(preview["preference"]["scope"], "project_group")
        self.assertEqual(preview["preference"]["apply_to"], "開発系のProject")
        self.assertEqual(
            (preview["risk_level"], preview["requires_acknowledgement"]),
            ("high", True),
        )
        self.assertEqual(len(self.versions(memory_id)), 1)

        confirmed = self.post(
            f"/memories/{memory_id}/confirm",
            {
                "expected_version": 1,
                "preference": preview["preference"],
                "acknowledge_high_risk": True,
            },
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["content"], preview["content"])
        self.assertEqual(confirmed.json()["scope"], "user")

    def test_the_applications_model_interpreter_answers_first(self):
        self.app.state.preference_interpreter = Model()
        memory_id = self.seed_candidate("k", "use tabs")
        response = self.post(
            f"/memories/{memory_id}/interpret", {"expected_version": 1, "text": "x"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["interpreted_by"], "model")
        self.assertEqual(response.json()["preference"]["rule"], "use tabs (model)")
