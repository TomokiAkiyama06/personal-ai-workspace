"""Usage and quotas over HTTP (issue #187, Decision 0069, Proposed): real PostgreSQL.

The sums themselves are tested on the service (``test_connections_report``); here
the routes: their guards, the authorization of the service behind them, the
Passkey Step-up of a quota change, the answers' shape and the errors.
"""

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .auth_http_support import HttpTestCase, error_code, requires_postgres

TOKYO = ZoneInfo("Asia/Tokyo")
QUOTA = "/api/v1/users/{}/quotas/codex/tasks/month"


class UsageHttpCase(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.engine.begin() as connection:
            connection.execute(
                text("TRUNCATE connection_usage, connection_quotas, tasks CASCADE")
            )
        self.owner_id = self.make_user("owner-one", role="owner")
        self.admin_id = self.make_user("admin-one", role="admin")
        self.user_id = self.make_user("alice")
        self.other_id = self.make_user("bob")
        self.owner = self.login_token("owner-one")
        self.admin = self.login_token("admin-one")
        self.user = self.login_token("alice")
        self.other = self.login_token("bob")

    # -- data -------------------------------------------------------------------------

    def task(self, user_id: uuid.UUID) -> uuid.UUID:
        task_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " VALUES (:id, :project, :user, 'Task', CAST('{}' AS jsonb),"
                    " 'running', 1, 0, 1, now(), now())"
                ),
                {"id": task_id, "project": uuid.uuid4(), "user": user_id},
            )
        return task_id

    def usage(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        kind: str = "codex",
        purpose: str = "coding",
        tokens: int = 10,
        ago: timedelta = timedelta(minutes=1),
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_usage (id, user_id, task_id, project_id,"
                    " kind, model, purpose, status, input_tokens, output_tokens,"
                    " started_at, finished_at, duration_ms) VALUES (:id, :user,"
                    " :task, (SELECT project_id FROM tasks WHERE id = :task), :kind,"
                    " 'secret-model-name', :purpose, 'succeeded', :tokens, 1,"
                    " now() - :ago, now() - :ago, 0)"
                ),
                {
                    "id": uuid.uuid4(),
                    "user": user_id,
                    "task": task_id,
                    "kind": kind,
                    "purpose": purpose,
                    "tokens": tokens,
                    "ago": ago,
                },
            )

    def local(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        tokens: int = 0,
        seconds: int = 0,
        placement: str = "local_gpu",
        calls: int = 1,
        ago: timedelta = timedelta(minutes=1),
    ) -> None:
        """A call of a local model (``local_usage``, Decision 0077)."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO local_usage (user_id, task_id, placement, calls,"
                    " tokens, seconds, started_at) VALUES (:user, :task, :placement,"
                    " :calls, :tokens, :seconds, now() - :ago)"
                ),
                {
                    "user": user_id,
                    "task": task_id,
                    "placement": placement,
                    "calls": calls,
                    "tokens": tokens,
                    "seconds": seconds,
                    "ago": ago,
                },
            )

    def escalation(
        self, task_id: uuid.UUID | None, ago: timedelta = timedelta(minutes=1)
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_incidents (kind, task_id, occurred_at)"
                    " VALUES ('escalation', :task, now() - :ago)"
                ),
                {"task": task_id, "ago": ago},
            )

    def quota(self, user_id: uuid.UUID, limit: int | None) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connection_quotas (user_id, kind, metric, period,"
                    " limit_value, created_at, updated_at) VALUES (:user, 'codex',"
                    " 'tasks', 'month', :limit, now(), now())"
                ),
                {"user": user_id, "limit": limit},
            )

    def quota_rows(self, user_id: uuid.UUID) -> list:
        return self.rows(
            "SELECT kind, metric, period, limit_value FROM connection_quotas"
            " WHERE user_id = :u",
            u=user_id,
        )

    def today(self) -> str:
        now = self.scalar("SELECT now()")
        return now.astimezone(TOKYO).date().isoformat()


@requires_postgres
class UsageRoutesTest(UsageHttpCase):
    def test_ones_own_usage(self):
        task = self.task(self.user_id)
        self.usage(self.user_id, task, tokens=10)
        self.usage(self.user_id, task, kind="claude", purpose="review", tokens=4)
        self.usage(self.other_id, self.task(self.other_id))
        self.quota(self.user_id, 20)

        response = self.call("GET", "/api/v1/usage", token=self.user)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["scope"], body["range"]), ("self", "last14"))
        self.assertEqual(body["tasks"], 1)
        self.assertEqual(body["previous_tasks"], 0)
        # No local call and no escalation: zero, recorded (Decision 0077).
        self.assertEqual(body["tokens"], {"local": 0, "external": 16})
        self.assertEqual(body["gpu_seconds"], 0)
        self.assertEqual(body["escalations"], {"failed": 0, "loop_detected": 0})
        self.assertEqual(len(body["daily"]), 14)
        self.assertEqual(
            body["daily"][-1],
            {"date": self.today(), "local": 0, "codex": 1, "claude": 1},
        )
        self.assertEqual(
            body["agents"],
            [
                {"agent": "codex", "tasks": 1, "tokens": 11},
                {"agent": "claude", "tasks": 1, "tokens": 5},
            ],
        )
        self.assertEqual(
            body["purposes"],
            [
                {"purpose": "coding", "tasks": 1, "tokens": 11},
                {"purpose": "review", "tasks": 1, "tokens": 5},
            ],
        )
        (quota,) = body["quotas"]
        self.assertEqual(
            (quota["kind"], quota["metric"], quota["period"], quota["limit"]),
            ("codex", "tasks", "month", 20),
        )
        self.assertEqual(quota["used"], 1)
        self.assertEqual(body["users"], [])
        self.assertNotIn("secret-model-name", response.text)

    def test_the_local_calls_gpu_time_and_escalations(self):
        # Decision 0077: alice's task ran on Codex and locally, another only
        # locally (on the CPU: no GPU time), and one escalated twice; bob's is not
        # hers, nor is an escalation without a task (before revision 0189).
        both, local_only = self.task(self.user_id), self.task(self.user_id)
        self.usage(self.user_id, both, tokens=10)
        self.local(self.user_id, both, tokens=100, seconds=90)
        self.local(self.user_id, both, seconds=5, calls=0)  # the late time
        self.local(
            self.user_id, local_only, tokens=7, seconds=30, placement="local_cpu"
        )
        self.escalation(both)
        self.escalation(both)
        bob = self.task(self.other_id)
        self.local(self.other_id, bob, tokens=1000, seconds=1000)
        self.escalation(bob)
        self.escalation(None)
        # Before the period: not counted.
        self.local(self.user_id, both, tokens=1, seconds=1, ago=timedelta(days=20))
        self.escalation(local_only, ago=timedelta(days=20))

        response = self.call("GET", "/api/v1/usage", token=self.user)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["tasks"], 2)  # one task with Codex and local is one
        self.assertEqual(body["tokens"], {"local": 107, "external": 11})
        self.assertEqual(body["gpu_seconds"], 95)
        self.assertEqual(body["escalations"], {"failed": 0, "loop_detected": 1})
        self.assertEqual(
            body["daily"][-1],
            {"date": self.today(), "local": 2, "codex": 1, "claude": 0},
        )
        self.assertEqual(
            body["agents"],
            [
                {"agent": "local", "tasks": 2, "tokens": 107},
                {"agent": "codex", "tasks": 1, "tokens": 11},
            ],
        )
        # The categories are the shared connections' only.
        self.assertEqual(
            body["purposes"], [{"purpose": "coding", "tasks": 1, "tokens": 11}]
        )

        workspace = self.call(
            "GET", "/api/v1/usage?scope=workspace", token=self.admin
        ).json()

        self.assertEqual(workspace["tasks"], 3)
        self.assertEqual(workspace["tokens"], {"local": 1107, "external": 11})
        self.assertEqual(workspace["gpu_seconds"], 1095)
        # alice's task, bob's and the one without a task.
        self.assertEqual(workspace["escalations"], {"failed": 0, "loop_detected": 3})
        self.assertEqual(
            [
                (user["login_name"], user["tasks"], user["tokens"])
                for user in workspace["users"]
            ],
            [
                ("owner-one", 0, 0),
                ("admin-one", 0, 0),
                ("alice", 2, 118),
                ("bob", 1, 1000),
            ],
        )

    def test_the_workspace_needs_admin_usage_view(self):
        self.usage(self.user_id, self.task(self.user_id))
        self.usage(self.other_id, self.task(self.other_id), tokens=5)

        refused = self.call("GET", "/api/v1/usage?scope=workspace", token=self.user)
        response = self.call(
            "GET", "/api/v1/usage?scope=workspace&range=month", token=self.admin
        )

        self.assertEqual((refused.status_code, error_code(refused)), (403, "forbidden"))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["scope"], body["range"]), ("workspace", "month"))
        self.assertEqual(body["tasks"], 2)
        self.assertEqual(
            [(user["login_name"], user["tasks"]) for user in body["users"]],
            [("owner-one", 0), ("admin-one", 0), ("alice", 1), ("bob", 1)],
        )
        self.assertEqual(body["users"][3]["tokens"], 6)
        self.assertEqual(body["users"][0]["quotas"], [])

    def test_an_owner_reads_the_workspace_too(self):
        response = self.call(
            "GET", "/api/v1/usage?scope=workspace&range=last30", token=self.owner
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["daily"]), 30)

    def test_the_query_is_validated(self):
        for query in ("range=last7", "scope=everyone", "range="):
            response = self.call("GET", f"/api/v1/usage?{query}", token=self.admin)
            self.assertEqual(
                (response.status_code, error_code(response)),
                (422, "validation_error"),
                query,
            )

    def test_nobody_signed_in_gets_nothing(self):
        for path in (
            "/api/v1/usage",
            "/api/v1/quotas/me",
            "/api/v1/admin/users",
            f"/api/v1/users/{self.user_id}/quotas",
        ):
            response = self.call("GET", path)
            self.assertEqual(
                (response.status_code, error_code(response)),
                (401, "unauthorized"),
                path,
            )

    def test_my_quotas(self):
        self.quota(self.user_id, None)

        response = self.call("GET", "/api/v1/quotas/me", token=self.user)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["user_id"], str(self.user_id))
        self.assertEqual(body["quotas"][0]["limit"], "unlimited")

    def test_no_quota_is_an_empty_list(self):
        response = self.call("GET", "/api/v1/quotas/me", token=self.user)

        self.assertEqual(response.json()["quotas"], [])

    def test_another_users_quotas_need_admin_usage_view(self):
        self.quota(self.other_id, 3)
        path = f"/api/v1/users/{self.other_id}/quotas"

        refused = self.call("GET", path, token=self.user)
        allowed = self.call("GET", path, token=self.admin)
        own = self.call("GET", f"/api/v1/users/{self.user_id}/quotas", token=self.user)

        self.assertEqual((refused.status_code, error_code(refused)), (403, "forbidden"))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["quotas"][0]["limit"], 3)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["quotas"], [])

    def test_an_unknown_user_is_not_found_only_for_who_may_look(self):
        path = f"/api/v1/users/{uuid.uuid4()}/quotas"

        for_admin = self.call("GET", path, token=self.admin)
        for_user = self.call("GET", path, token=self.user)

        self.assertEqual(
            (for_admin.status_code, error_code(for_admin)), (404, "not_found")
        )
        self.assertEqual(
            (for_user.status_code, error_code(for_user)), (403, "forbidden")
        )

    def test_the_user_list(self):
        self.make_user("gone", status="deleted")
        self.make_user("leaving", status="pending_deletion")

        refused = self.call("GET", "/api/v1/admin/users", token=self.user)
        response = self.call("GET", "/api/v1/admin/users", token=self.admin)

        self.assertEqual((refused.status_code, error_code(refused)), (403, "forbidden"))
        self.assertEqual(response.status_code, 200, response.text)
        users = response.json()["users"]
        self.assertEqual(
            [
                (user["login_name"], user["system_role"], user["status"])
                for user in users
            ],
            [
                ("owner-one", "owner", "active"),
                ("admin-one", "admin", "active"),
                ("alice", "user", "active"),
                ("bob", "user", "active"),
                ("leaving", "user", "pending_deletion"),
            ],
        )
        self.assertEqual(set(users[0]), {
            "user_id", "login_name", "system_role", "status", "created_at",
        })  # fmt: skip


@requires_postgres
class QuotaChangeTest(UsageHttpCase):
    def test_set_and_remove_a_quota_with_a_step_up(self):
        self.fake_passkey_step_up(self.admin)
        path = QUOTA.format(self.user_id)

        response = self.call("PUT", path, token=self.admin, json={"limit": 30})

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            (body["user_id"], body["kind"], body["metric"], body["period"]),
            (str(self.user_id), "codex", "tasks", "month"),
        )
        self.assertEqual(body["limit"], 30)
        self.assertEqual(
            self.quota_rows(self.user_id), [("codex", "tasks", "month", 30)]
        )

        unlimited = self.call(
            "PUT", path, token=self.admin, json={"limit": "unlimited"}
        )
        self.assertEqual(unlimited.json()["limit"], "unlimited")
        self.assertEqual(self.quota_rows(self.user_id)[0][3], None)

        removed = self.call("DELETE", path, token=self.admin)
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(self.quota_rows(self.user_id), [])
        again = self.call("DELETE", path, token=self.admin)
        self.assertEqual((again.status_code, error_code(again)), (404, "not_found"))
        summary = self.audit_summary()
        self.assertEqual(summary[("connection.quota.set", "allow", "succeeded")], 2)
        self.assertEqual(summary[("connection.quota.remove", "allow", "succeeded")], 1)

    def test_a_change_needs_a_recent_passkey_step_up(self):
        path = QUOTA.format(self.user_id)

        put = self.call("PUT", path, token=self.admin, json={"limit": 1})
        delete = self.call("DELETE", path, token=self.admin)

        self.assertEqual((put.status_code, error_code(put)), (403, "step_up_required"))
        self.assertEqual(
            (delete.status_code, error_code(delete)), (403, "step_up_required")
        )
        self.assertEqual(self.quota_rows(self.user_id), [])
        summary = self.audit_summary()
        self.assertEqual(
            summary[("connection.quota.set", "deny", "step_up_required")], 1
        )
        self.assertEqual(
            summary[("connection.quota.remove", "deny", "step_up_required")], 1
        )

    def test_an_old_step_up_is_not_enough(self):
        self.fake_passkey_step_up(self.admin)
        self.clock.advance(minutes=31)  # the policy's window is 30 minutes

        response = self.call(
            "PUT", QUOTA.format(self.user_id), token=self.admin, json={"limit": 1}
        )

        self.assertEqual(
            (response.status_code, error_code(response)), (403, "step_up_required")
        )

    def test_a_password_step_up_is_not_enough(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE auth_sessions SET stepup_at = :at, stepup_method ="
                    " 'password' WHERE user_id = :u"
                ),
                {"at": self.clock.now, "u": self.admin_id},
            )

        response = self.call(
            "PUT", QUOTA.format(self.user_id), token=self.admin, json={"limit": 1}
        )

        self.assertEqual(
            (response.status_code, error_code(response)),
            (403, "step_up_method_insufficient"),
        )

    def test_a_user_may_not_change_a_quota(self):
        self.fake_passkey_step_up(self.user)

        own = self.call(
            "PUT", QUOTA.format(self.user_id), token=self.user, json={"limit": 99}
        )
        other = self.call("DELETE", QUOTA.format(self.other_id), token=self.user)

        self.assertEqual((own.status_code, error_code(own)), (403, "forbidden"))
        self.assertEqual((other.status_code, error_code(other)), (403, "forbidden"))
        self.assertEqual(self.quota_rows(self.user_id), [])

    def test_only_the_owner_changes_the_owners_quota(self):
        self.fake_passkey_step_up(self.admin)
        self.fake_passkey_step_up(self.owner)
        path = QUOTA.format(self.owner_id)

        by_admin = self.call("PUT", path, token=self.admin, json={"limit": 1})
        by_owner = self.call("PUT", path, token=self.owner, json={"limit": 1})

        self.assertEqual(
            (by_admin.status_code, error_code(by_admin)), (403, "forbidden")
        )
        self.assertEqual(by_owner.status_code, 200, by_owner.text)

    def test_an_unknown_or_deleted_user_is_not_found(self):
        self.fake_passkey_step_up(self.admin)
        deleted = self.make_user("gone", status="deleted")

        for user_id in (uuid.uuid4(), deleted):
            response = self.call(
                "PUT", QUOTA.format(user_id), token=self.admin, json={"limit": 1}
            )
            self.assertEqual(
                (response.status_code, error_code(response)), (404, "not_found")
            )

    def test_the_request_is_validated(self):
        self.fake_passkey_step_up(self.admin)
        path = QUOTA.format(self.user_id)

        for body in (
            {"limit": -1},
            {"limit": 10**13},
            {"limit": "Unlimited"},
            {"limit": 1.5},
            {"limit": "5"},
            {"limit": True},
            {},
            {"limit": 1, "extra": 1},
        ):
            response = self.call("PUT", path, token=self.admin, json=body)
            self.assertEqual(
                (response.status_code, error_code(response)),
                (422, "validation_error"),
                body,
            )
        for bad in (
            f"/api/v1/users/{self.user_id}/quotas/gemini/tasks/month",
            f"/api/v1/users/{self.user_id}/quotas/codex/gpu_seconds/month",
            f"/api/v1/users/{self.user_id}/quotas/codex/tasks/year",
            "/api/v1/users/not-a-uuid/quotas/codex/tasks/month",
        ):
            response = self.call("PUT", bad, token=self.admin, json={"limit": 1})
            self.assertEqual(response.status_code, 422, bad)
        self.assertEqual(self.quota_rows(self.user_id), [])

    def test_a_limit_of_zero_is_a_limit(self):
        self.fake_passkey_step_up(self.admin)

        response = self.call(
            "PUT", QUOTA.format(self.user_id), token=self.admin, json={"limit": 0}
        )

        self.assertEqual(response.json()["limit"], 0)

    def test_a_cross_origin_change_is_refused(self):
        self.fake_passkey_step_up(self.admin)

        response = self.call(
            "PUT",
            QUOTA.format(self.user_id),
            token=self.admin,
            origin="https://evil.example",
            json={"limit": 1},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.quota_rows(self.user_id), [])
