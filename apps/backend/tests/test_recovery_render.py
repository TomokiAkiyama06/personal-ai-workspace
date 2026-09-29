"""The pure renderer and format of the Recovery Repository (PAW-047, Decision 0054).

No database, no file system: the same snapshot gives the same bytes; users in
deletion keep only a deletion record; ``session_only`` versions are left out;
credentials in free text are redacted; nothing credential-shaped is written; the
manifest changes only when a file changed; the checksums verify.
"""

import json
import unittest
from datetime import timedelta
from uuid import uuid4

from paw_backend.recovery.format import (
    CHECKSUMS_PATH,
    MANIFEST_NAME,
    MARKER_NAME,
    RECOVERY_FORMAT_VERSION,
    parse_checksums,
    parse_manifest,
    sha256_hex,
)
from paw_backend.recovery.render import render_recovery
from paw_backend.recovery.restore import (
    RecoveryRestoreError,
    RestoreProblem,
    parse_source,
    verify_files,
)

from .recovery_support import T0, snapshot_with, user, version

# Split so the literal never looks like a credential in the source.
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def render(snapshot, memory_files=None, *, now=T0, previous=None):
    return render_recovery(
        snapshot,
        memory_files or {},
        schema_version="0129",
        workspace_version="0.1.0",
        now=now,
        previous_manifest=previous,
    )


def load(data: bytes):
    return json.loads(data.decode("utf-8"))


class RenderTest(unittest.TestCase):
    def test_same_snapshot_same_bytes_whatever_the_order(self) -> None:
        memory_id = uuid4()
        users = [user(login_name="alice"), user(login_name="bob")]
        versions = [version(memory_id), version(memory_id, version_number=2)]
        first = render(
            snapshot_with(
                users=users,
                memories=[{"id": memory_id, "created_at": T0}],
                versions=versions,
            )
        )
        second = render(
            snapshot_with(
                users=list(reversed(users)),
                memories=[{"id": memory_id, "created_at": T0}],
                versions=list(reversed(versions)),
            )
        )
        self.assertEqual(first.files, second.files)
        record = load(first.files[f"memory-records/{memory_id}.json"])
        self.assertEqual([1, 2], [v["version_number"] for v in record["versions"]])

    def test_layout_manifest_and_checksums(self) -> None:
        owner = user(system_role="owner", passkey_required=True, login_name="own")
        plan = render(snapshot_with(users=[owner]), {"shared/INDEX.md": b"# x\n"})
        self.assertIn(MARKER_NAME, plan.files)
        self.assertIn(f"users/{owner['id']}.json", plan.files)
        self.assertEqual(b"# x\n", plan.files["memory/shared/INDEX.md"])
        manifest = parse_manifest(plan.files[MANIFEST_NAME])
        self.assertEqual(RECOVERY_FORMAT_VERSION, manifest.recovery_format_version)
        self.assertEqual("0129", manifest.workspace_schema_version)
        self.assertEqual(T0, manifest.generated_at)
        self.assertEqual(
            sha256_hex(plan.files[CHECKSUMS_PATH]), manifest.checksums_sha256
        )
        listed = parse_checksums(plan.files[CHECKSUMS_PATH])
        self.assertEqual(set(plan.files) - {MANIFEST_NAME, CHECKSUMS_PATH}, set(listed))
        self.assertEqual(manifest, verify_files(plan.files))

    def test_every_file_is_deterministic_json_with_a_final_newline(self) -> None:
        plan = render(snapshot_with(users=[user()]))
        for path, data in plan.files.items():
            if path.endswith(".json"):
                self.assertTrue(data.endswith(b"\n"), path)
                self.assertFalse(data.endswith(b"\n\n"), path)
                value = load(data)
                self.assertEqual(
                    data,
                    (
                        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
                        + "\n"
                    ).encode(),
                )

    def test_manifest_is_kept_when_nothing_changed(self) -> None:
        snapshot = snapshot_with(users=[user()])
        first = render(snapshot)
        later = render(
            snapshot, now=T0 + timedelta(hours=1), previous=first.files[MANIFEST_NAME]
        )
        self.assertEqual(first.files, later.files)
        changed = render(
            snapshot_with(users=[user()]),
            now=T0 + timedelta(hours=2),
            previous=first.files[MANIFEST_NAME],
        )
        self.assertNotEqual(first.files[MANIFEST_NAME], changed.files[MANIFEST_NAME])
        self.assertEqual(
            T0 + timedelta(hours=2),
            parse_manifest(changed.files[MANIFEST_NAME]).generated_at,
        )

    def test_a_user_in_deletion_keeps_only_a_deletion_record(self) -> None:
        for status in ("pending_deletion", "deleted"):
            with self.subTest(status=status):
                gone = user(status=status, login_name="gone")
                kept = user(login_name="kept")
                project_id = uuid4()
                private = uuid4()
                shared = uuid4()
                plan = render(
                    snapshot_with(
                        users=[gone, kept],
                        quotas=[
                            {
                                "user_id": gone["id"],
                                "kind": "codex",
                                "metric": "requests",
                                "period": "day",
                                "limit_value": 5,
                                "created_at": T0,
                                "updated_at": T0,
                            }
                        ],
                        projects=[
                            {
                                "id": project_id,
                                "name": "P",
                                "description": None,
                                "status": "active",
                                "created_by": gone["id"],
                                "created_at": T0,
                                "updated_at": T0,
                                "deletion_started_at": None,
                                "deletion_scheduled_at": None,
                                "deleted_at": None,
                            }
                        ],
                        members=[
                            {
                                "project_id": project_id,
                                "user_id": uid,
                                "role": "contributor",
                                "status": "active",
                                "invited_at": T0,
                                "invite_expires_at": None,
                                "joined_at": T0,
                            }
                            for uid in (gone["id"], kept["id"])
                        ],
                        memories=[
                            {"id": private, "created_at": T0},
                            {"id": shared, "created_at": T0},
                        ],
                        versions=[
                            version(
                                private,
                                scope="user",
                                owner_user_id=gone["id"],
                                content="their private note",
                            ),
                            version(shared, actor_user_id=gone["id"]),
                        ],
                    ),
                    {
                        f"users/{gone['id']}/{private}.md": b"private\n",
                        f"users/{gone['id']}/INDEX.md": b"index\n",
                        f"users/{kept['id']}/INDEX.md": b"index\n",
                    },
                )
                self.assertEqual(
                    {"id": str(gone["id"]), "status": status},
                    load(plan.files[f"deletions/users/{gone['id']}.json"]),
                )
                self.assertNotIn(f"users/{gone['id']}.json", plan.files)
                self.assertNotIn(f"memory-records/{private}.json", plan.files)
                self.assertIn(f"memory-records/{shared}.json", plan.files)
                self.assertFalse(
                    [
                        p
                        for p in plan.files
                        if p.startswith(f"memory/users/{gone['id']}")
                    ]
                )
                self.assertIn(f"memory/users/{kept['id']}/INDEX.md", plan.files)
                members = load(plan.files[f"projects/{project_id}.json"])["members"]
                self.assertEqual([str(kept["id"])], [m["user_id"] for m in members])
                everything = b"".join(plan.files.values())
                self.assertNotIn(b"gone", everything)
                self.assertNotIn(b"their private note", everything)
                self.assertEqual(1, plan.counts["deletions"])
                self.assertEqual(1, plan.counts["users"])

    def test_session_only_versions_and_their_relations_are_left_out(self) -> None:
        memory_id = uuid4()
        kept = version(memory_id)
        session = version(memory_id, version_number=2, freshness_policy="session_only")
        plan = render(
            snapshot_with(
                memories=[{"id": memory_id, "created_at": T0}],
                versions=[kept, session],
                relations=[
                    {
                        "id": uuid4(),
                        "from_version_id": session["id"],
                        "to_version_id": kept["id"],
                        "relation_type": "supersedes",
                        "reason": None,
                        "created_at": T0,
                    }
                ],
            )
        )
        record = load(plan.files[f"memory-records/{memory_id}.json"])
        self.assertEqual([str(kept["id"])], [v["id"] for v in record["versions"]])
        self.assertEqual([], record["relations"])

    def test_credentials_in_free_text_are_redacted(self) -> None:
        memory_id = uuid4()
        task_id = uuid4()
        plan = render(
            snapshot_with(
                memories=[{"id": memory_id, "created_at": T0}],
                versions=[
                    version(
                        memory_id,
                        title=f"token {SECRET}",
                        content=f"use {SECRET} here",
                        attributes={"note": f"x {SECRET}", "api_key": "plain"},
                    )
                ],
                tasks=[
                    {
                        "id": task_id,
                        "project_id": uuid4(),
                        "created_by": uuid4(),
                        "title": f"deploy with {SECRET}",
                        "state": "completed",
                        "wait_reason": None,
                        "attempt": 1,
                        "retry_count": 0,
                        "created_at": T0,
                        "updated_at": T0,
                    }
                ],
            )
        )
        everything = b"".join(plan.files.values())
        self.assertNotIn(SECRET.encode(), everything)
        self.assertNotIn(b'"plain"', everything)
        record = load(plan.files[f"memory-records/{memory_id}.json"])
        self.assertGreaterEqual(record["versions"][0]["redactions"], 3)
        self.assertGreaterEqual(plan.redactions, 4)

    def test_no_credential_column_can_reach_a_file(self) -> None:
        plan = render(
            snapshot_with(
                users=[user()],
                connections=[
                    {
                        "kind": "codex",
                        "status": "connected",
                        "enabled": True,
                        "created_at": T0,
                        "updated_at": T0,
                    }
                ],
            )
        )
        for path, data in plan.files.items():
            if not path.endswith(".json"):
                continue
            text = data.decode()
            for word in ("hash", "secret", "passkey_public", "token", "password"):
                self.assertNotIn(f'"{word}', text, path)

    def test_restore_reads_back_what_render_wrote(self) -> None:
        owner = user(system_role="owner", passkey_required=True, login_name="own")
        memory_id = uuid4()
        first = version(memory_id, revalidate_after=timedelta(days=1, seconds=1))
        plan = render(
            snapshot_with(
                users=[owner],
                memories=[{"id": memory_id, "created_at": T0}],
                versions=[first],
                sources=[
                    {
                        "id": uuid4(),
                        "memory_version_id": first["id"],
                        "source_type": "conversation",
                        "source_ref": None,
                        "source_deleted_at": None,
                        "created_at": T0,
                    },
                    {
                        "id": uuid4(),
                        "memory_version_id": first["id"],
                        "source_type": "task",
                        "source_ref": "task:1",
                        "source_deleted_at": None,
                        "created_at": T0,
                    },
                ],
            )
        )
        manifest = verify_files(plan.files)
        data = parse_source(plan.files, "c" * 40, manifest)
        self.assertEqual([owner["id"]], [row["id"] for row in data.users])
        self.assertEqual(
            timedelta(days=1, seconds=1), data.versions[0]["revalidate_after"]
        )
        self.assertEqual(T0, data.versions[0]["created_at"])
        self.assertEqual(1, data.skipped_conversation_sources)
        self.assertEqual(["task"], [row["source_type"] for row in data.sources])


class VerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = dict(render(snapshot_with(users=[user()])).files)

    def refused(self, files) -> RestoreProblem:
        with self.assertRaises(RecoveryRestoreError) as caught:
            verify_files(files)
        return caught.exception.problem

    def test_a_changed_file_is_refused(self) -> None:
        path = next(p for p in self.files if p.startswith("users/"))
        self.files[path] = self.files[path].replace(b"alice", b"mallory")
        self.assertIs(RestoreProblem.CHECKSUM_MISMATCH, self.refused(self.files))

    def test_an_unlisted_or_missing_file_is_refused(self) -> None:
        extra = dict(self.files)
        extra[f"users/{uuid4()}.json"] = b"{}\n"
        self.assertIs(RestoreProblem.UNLISTED_FILE, self.refused(extra))
        missing = dict(self.files)
        del missing[next(p for p in missing if p.startswith("users/"))]
        self.assertIs(RestoreProblem.MISSING_FILE, self.refused(missing))

    def test_an_unknown_format_or_no_manifest_is_refused(self) -> None:
        later = dict(self.files)
        later[MANIFEST_NAME] = b'{"recovery_format_version": 99}\n'
        self.assertIs(RestoreProblem.FORMAT_UNSUPPORTED, self.refused(later))
        none = dict(self.files)
        del none[MANIFEST_NAME]
        self.assertIs(RestoreProblem.MANIFEST_MISSING, self.refused(none))
        broken = dict(self.files)
        broken[MANIFEST_NAME] = b"not json"
        self.assertIs(RestoreProblem.MANIFEST_INVALID, self.refused(broken))

    def test_personal_data_of_a_user_in_deletion_is_refused(self) -> None:
        gone = user(status="deleted")
        memory_id = uuid4()
        plan = render(snapshot_with(users=[gone]))
        files = dict(plan.files)
        # A hand-made source that carries the deleted user's private memory.
        bad = render(
            snapshot_with(
                users=[user(id=gone["id"])],
                memories=[{"id": memory_id, "created_at": T0}],
                versions=[version(memory_id, scope="user", owner_user_id=gone["id"])],
            )
        ).files
        files[f"memory-records/{memory_id}.json"] = bad[
            f"memory-records/{memory_id}.json"
        ]
        with self.assertRaises(RecoveryRestoreError) as caught:
            parse_source(files, "c" * 40, verify_files(plan.files))
        self.assertIs(RestoreProblem.DELETED_USER_DATA, caught.exception.problem)


if __name__ == "__main__":
    unittest.main()
