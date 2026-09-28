"""The pure Markdown renderer of the Memory Projection (PAW-045).

Diff-friendly and deterministic: the same memories give the same bytes in any
order and at any time; one file per memory named by its id; each audience in its
own directory; credentials never reach a file.
"""

import json
import random
import re
import unittest
from datetime import timedelta
from uuid import uuid4

from paw_backend.memory.projection import (
    FORMAT_VERSION,
    INDEX_FILE,
    ProjectionRenderError,
    directory_for,
    render_index,
    render_memory,
    render_projection,
)
from paw_backend.tools.credentials import MAX_TEXT_CHARS

from .projection_support import T0, memory

# What a YAML reader rejects (the complement of YAML's printable set, as PyYAML's
# reader checks it) or reads as a line break inside a quoted scalar.
_YAML_NOT_PRINTABLE = re.compile(
    "[^\x09\x0a\x0d\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)
_YAML_LINE_BREAKS = re.compile("[\x85\u2028\u2029]")


def assert_reads_as_yaml(test: unittest.TestCase, block: str) -> None:
    """Each line of ``block`` is ``key: <JSON>`` a YAML reader takes unchanged."""
    test.assertIsNone(_YAML_NOT_PRINTABLE.search(block))
    test.assertIsNone(_YAML_LINE_BREAKS.search(block))
    test.assertNotIn("\ufeff", block)  # a byte order mark
    for line in block.split("\n"):
        test.assertRegex(line, r"^[a-z_]+: [\[\"0-9tfn-]")


def front_matter(data: bytes) -> dict[str, object]:
    text = data.decode("utf-8")
    assert text.startswith("---\n")
    block = text[4 : text.index("\n---\n")]
    values = {}
    for line in block.split("\n"):
        key, _, value = line.partition(": ")
        values[key] = json.loads(value)
    return values


class DirectoryTest(unittest.TestCase):
    def test_each_scope_has_its_own_directory_named_by_id(self):
        user, project, group, repo = uuid4(), uuid4(), uuid4(), uuid4()
        cases = [
            (memory(owner_user_id=user), ("users", str(user))),
            (memory(scope="project", project_id=project), ("projects", str(project))),
            (
                memory(scope="project_group", project_group_id=group),
                ("project-groups", str(group)),
            ),
            (memory(scope="repo", repo_id=repo), ("repos", str(repo))),
            (memory(scope="shared"), ("shared",)),
        ]
        for value, expected in cases:
            with self.subTest(scope=value.scope):
                self.assertEqual(directory_for(value), expected)

    def test_a_scope_without_its_id_is_refused(self):
        with self.assertRaises(ProjectionRenderError):
            directory_for(memory(owner_user_id=None))
        with self.assertRaises(ProjectionRenderError):
            directory_for(memory(scope="unknown"))

    def test_no_text_of_a_memory_can_choose_its_directory(self):
        evil = memory(title="../../etc/passwd", memory_type="../x", content="/root")
        plan = render_projection([evil])
        (key,) = plan.directories
        self.assertEqual(key, ("users", str(evil.owner_user_id)))
        self.assertEqual(
            set(plan.directories[key]),
            {INDEX_FILE, f"{evil.memory_id}.md"},
        )


class MemoryFileTest(unittest.TestCase):
    def test_front_matter_has_the_fixed_keys_in_order(self):
        value = memory(
            version_number=3,
            freshness_policy="revalidate",
            verified_at=T0,
            revalidate_after=timedelta(days=90),
            revalidate_triggers=("model_changed", "member_changed"),
            stale_since=T0 + timedelta(days=91),
            importance=80,
            pinned=True,
        )
        data, redactions = render_memory(value)
        self.assertEqual(redactions, 0)
        fields = front_matter(data)
        self.assertEqual(
            list(fields),
            [
                "projection_format",
                "memory_id",
                "version",
                "scope",
                "owner_user_id",
                "title",
                "memory_type",
                "status",
                "confirmation_state",
                "importance",
                "pinned",
                "freshness_policy",
                "verified_at",
                "revalidate_after_seconds",
                "revalidate_triggers",
                "stale_since",
                "version_created_at",
            ],
        )
        self.assertEqual(fields["projection_format"], FORMAT_VERSION)
        self.assertEqual(fields["memory_id"], str(value.memory_id))
        self.assertEqual(fields["version"], 3)
        self.assertEqual(fields["owner_user_id"], str(value.owner_user_id))
        self.assertEqual(fields["pinned"], True)
        self.assertEqual(fields["verified_at"], "2026-09-01T12:00:00Z")
        self.assertEqual(fields["revalidate_after_seconds"], 90 * 86400)
        # Sorted, so the order the database returned them in does not matter.
        self.assertEqual(
            fields["revalidate_triggers"], ["member_changed", "model_changed"]
        )

    def test_the_heading_and_the_body_follow_the_front_matter(self):
        text = render_memory(memory(title="Tabs", content="Line 1\nLine 2"))[0]
        text = text.decode()
        self.assertIn("\n---\n<!-- Generated from PostgreSQL", text)
        self.assertTrue(text.endswith("\n\n# Tabs\n\nLine 1\nLine 2\n"))

    def test_line_ends_are_lf_and_the_file_ends_with_one_newline(self):
        data = render_memory(memory(content="a\r\nb\rc\n\n\n"))[0]
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"a\nb\nc\n"))
        self.assertFalse(data.endswith(b"\n\n"))

    def test_quotes_and_line_breaks_in_a_title_cannot_break_the_block(self):
        title = 'He said: "yes"\n---\nstatus: deprecated'
        data = render_memory(memory(title=title))[0]
        fields = front_matter(data)
        self.assertEqual(fields["title"], title)
        self.assertEqual(fields["status"], "active")
        self.assertIn(b'# He said: "yes" --- status: deprecated\n', data)

    def test_the_front_matter_reads_as_yaml_with_control_characters(self):
        # JSON leaves DEL and the C1 controls unescaped (ensure_ascii=False); YAML
        # rejects them or, for NEL / U+2028 / U+2029, reads a line break.
        for character in ("\x7f", "\x80", "\x85", "\x9f", "\u2028", "\u2029"):
            title = f"a{character}b 日本語"
            data = render_memory(memory(title=title, branch=f"x{character}"))[0]
            text = data.decode("utf-8")
            block = text[4 : text.index("\n---\n")]
            with self.subTest(character=hex(ord(character))):
                assert_reads_as_yaml(self, block)
                fields = front_matter(data)
                self.assertEqual(fields["title"], title)
                self.assertEqual(fields["branch"], f"x{character}")
                self.assertNotIn(character, block)
                self.assertIn("日本語", block)  # still readable

    def test_the_whole_front_matter_reads_as_yaml(self):
        value = memory(
            title='He said: "yes" # no comment\n- item',
            revalidate_after=timedelta(days=1),
            revalidate_triggers=("model_changed",),
            pinned=True,
        )
        data = render_memory(value)[0]
        text = data.decode("utf-8")
        assert_reads_as_yaml(self, text[4 : text.index("\n---\n")])
        self.assertEqual(front_matter(data)["title"], value.title)

    def test_a_text_too_long_to_scan_is_marked_truncated_not_redacted(self):
        content = "x" * (MAX_TEXT_CHARS + 10)
        data, redactions = render_memory(memory(content=content))
        fields = front_matter(data)
        self.assertEqual(redactions, 0)
        self.assertNotIn("redactions", fields)
        self.assertIs(fields["truncated"], True)
        self.assertIn(b"[TRUNCATED]", data)
        # A text that fits is not marked.
        self.assertNotIn("truncated", front_matter(render_memory(memory())[0]))

    def test_truncations_are_counted_apart_from_redactions(self):
        token = "ghp_" + "a1B2" * 9
        long = token + "\n" + "x" * MAX_TEXT_CHARS
        plan = render_projection([memory(content=long), memory(content=token)])
        self.assertEqual((plan.redactions, plan.truncations), (2, 1))

    def test_unicode_is_kept_readable(self):
        data = render_memory(memory(title="日本語のメモ", content="内容"))[0]
        self.assertIn("日本語のメモ".encode(), data)

    def test_nothing_depends_on_the_time_of_the_run(self):
        value = memory()
        self.assertEqual(render_memory(value), render_memory(value))

    def test_scope_ids_of_other_scopes_are_not_written(self):
        value = memory(scope="project")
        fields = front_matter(render_memory(value)[0])
        self.assertIn("project_id", fields)
        self.assertNotIn("owner_user_id", fields)
        shared = front_matter(render_memory(memory(scope="shared"))[0])
        self.assertNotIn("owner_user_id", shared)
        self.assertNotIn("project_id", shared)

    def test_credentials_are_redacted_and_counted(self):
        token = "ghp_" + "a1B2" * 9
        data, redactions = render_memory(
            memory(title=f"token {token}", content=f"password = hunter22\n{token}")
        )
        self.assertNotIn(token.encode(), data)
        self.assertNotIn(b"hunter22", data)
        self.assertIn(b"[REDACTED]", data)
        self.assertEqual(redactions, 3)
        self.assertEqual(front_matter(data)["redactions"], 3)


class IndexTest(unittest.TestCase):
    def test_rows_are_sorted_by_status_type_title_and_id(self):
        a = memory(title="b", memory_type="note")
        b = memory(title="A", memory_type="note")
        c = memory(title="z", memory_type="decision")
        d = memory(title="a", status="deprecated", memory_type="decision")
        key = ("users", "x")
        lines = render_index(key, [a, b, c, d]).decode().splitlines()
        rows = [line for line in lines if line.startswith("| ") and "---" not in line]
        titles = [row.split(" | ")[2] for row in rows[1:]]
        self.assertEqual(titles, ["z", "A", "b", "a"])
        self.assertEqual(lines[0], "# Memory index: users/x")

    def test_cells_are_escaped_and_stale_is_marked(self):
        value = memory(title="a | b\nc \\ d", stale_since=T0)
        text = render_index(("users", "x"), [value]).decode()
        self.assertIn("| active (stale) | preference | a \\| b c \\\\ d | 1 |", text)
        self.assertIn(f"[{value.memory_id}.md]({value.memory_id}.md)", text)

    def test_index_titles_are_redacted(self):
        token = "ghp_" + "a1B2" * 9
        text = render_index(("shared",), [memory(scope="shared", title=token)])
        self.assertNotIn(token.encode(), text)


class ProjectionTest(unittest.TestCase):
    def test_the_same_memories_in_any_order_give_the_same_bytes(self):
        values = [memory(), memory(scope="project"), memory(scope="shared")]
        values += [memory(owner_user_id=values[0].owner_user_id, title="second")]
        first = render_projection(values)
        shuffled = list(values)
        random.Random(7).shuffle(shuffled)
        second = render_projection(shuffled)
        self.assertEqual(first, second)
        self.assertEqual(list(first.directories), sorted(first.directories))

    def test_one_directory_per_audience_with_an_index(self):
        mine = memory()
        also_mine = memory(owner_user_id=mine.owner_user_id)
        theirs = memory()
        project = memory(scope="project")
        plan = render_projection([mine, also_mine, theirs, project])
        self.assertEqual(plan.memories, 4)
        self.assertEqual(
            set(plan.directories),
            {
                ("users", str(mine.owner_user_id)),
                ("users", str(theirs.owner_user_id)),
                ("projects", str(project.project_id)),
            },
        )
        my_files = plan.directories[("users", str(mine.owner_user_id))]
        self.assertEqual(
            set(my_files),
            {INDEX_FILE, f"{mine.memory_id}.md", f"{also_mine.memory_id}.md"},
        )
        # Another user's directory says nothing about mine.
        theirs_files = plan.directories[("users", str(theirs.owner_user_id))]
        for data in theirs_files.values():
            self.assertNotIn(str(mine.memory_id).encode(), data)
            self.assertNotIn(str(mine.owner_user_id).encode(), data)

    def test_a_memory_twice_is_refused(self):
        value = memory()
        with self.assertRaises(ProjectionRenderError):
            render_projection([value, value])

    def test_redactions_are_summed(self):
        token = "ghp_" + "a1B2" * 9
        plan = render_projection([memory(content=token), memory(content=token)])
        self.assertEqual(plan.redactions, 2)


if __name__ == "__main__":
    unittest.main()
