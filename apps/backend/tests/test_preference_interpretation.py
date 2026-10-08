"""The free-text answer [その他...] and its structured preview (PAW-044).

No database: the rule interpreter, the model's contract, and the structure's own
validation (what a client sends back is validated the same way).
"""

import json
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from paw_backend.memory.preferences.interpretation import (
    InterpretedScope,
    InterpreterOutputError,
    RuleInterpreter,
    Strength,
    StructuredPreference,
    parse_interpreter_output,
)
from paw_backend.memory.preferences.rules import RiskLevel, TargetScope
from paw_backend.memory.versioning import InputProblem, InvalidMemoryInputError

CANDIDATE = "PR は subscription review を通してから出す"


def answer(**fields):
    document = {
        "scope": "user",
        "apply_to": None,
        "rule": "use tabs",
        "exceptions": [],
        "strength": "default",
        "risk_level": "low",
        "expires_at": None,
    }
    document.update(fields)
    return json.dumps(document)


class RuleInterpreterTest(unittest.TestCase):
    def interpret(self, text, candidate=CANDIDATE, recommended=TargetScope.USER):
        return RuleInterpreter().interpret_text(text, candidate, recommended)

    def test_the_requirements_example(self):
        found = self.interpret(
            "開発系のProjectだけ適用して。ただしmainへのMergeは毎回確認して"
        )
        self.assertIs(found.scope, InterpretedScope.PROJECT_GROUP)
        self.assertEqual(found.apply_to, "開発系のProject")
        self.assertEqual(found.exceptions, ("mainへのMergeは毎回確認して",))
        self.assertEqual(found.rule, CANDIDATE)
        # project_group has no entity: it is the person's own memory with a condition.
        self.assertIs(found.target, TargetScope.USER)
        self.assertIs(found.risk_level(), RiskLevel.HIGH)
        self.assertEqual(
            found.content(),
            f"{CANDIDATE}\n適用対象: 開発系のProject"
            "\n例外: mainへのMergeは毎回確認して",
        )

    def test_scope_phrases(self):
        self.assertIs(self.interpret("このRepoだけで").scope, InterpretedScope.REPO)
        self.assertIs(
            self.interpret("このプロジェクトでお願い").scope, InterpretedScope.PROJECT
        )
        self.assertIs(self.interpret("すべてのProjectで").scope, InterpretedScope.USER)

    def test_no_scope_phrase_keeps_the_recommendation(self):
        found = self.interpret("そうして", recommended=TargetScope.PROJECT)
        self.assertIs(found.scope, InterpretedScope.PROJECT)

    def test_a_mandatory_rule_is_high_risk(self):
        found = self.interpret("必ずそうして")
        self.assertIs(found.strength, Strength.REQUIRED)
        self.assertIs(found.risk_level(), RiskLevel.HIGH)
        self.assertIn("強さ: 必須", found.content())

    def test_without_a_candidate_the_text_is_the_rule(self):
        found = self.interpret("インデントはtabにして", candidate=None)
        self.assertEqual(found.rule, "インデントはtabにして")
        self.assertIs(found.risk_level(), RiskLevel.LOW)


class ModelOutputTest(unittest.TestCase):
    def test_a_valid_answer(self):
        preference, risk = parse_interpreter_output(
            answer(
                scope="project_group",
                apply_to="development projects",
                exceptions=["confirm merges to main"],
                strength="default",
                risk_level="high",
                expires_at="2026-11-01T00:00:00+09:00",
            )
        )
        self.assertIs(preference.scope, InterpretedScope.PROJECT_GROUP)
        self.assertEqual(preference.exceptions, ("confirm merges to main",))
        self.assertEqual(preference.expires_at, datetime(2026, 10, 31, 15, tzinfo=UTC))
        self.assertIs(risk, RiskLevel.HIGH)

    def test_any_violation_drops_the_whole_answer(self):
        bad = [
            "not json",
            json.dumps([]),
            answer(extra=1)[:-1] + ', "extra": 1}',
            answer(scope="shared"),
            answer(rule="   "),
            answer(exceptions="x"),
            answer(strength="maybe"),
            answer(risk_level="none"),
            answer(scope="project_group"),  # needs apply_to
            answer(apply_to="x"),  # only for project_group
            answer(expires_at="next week"),
            answer(expires_at="2026-11-01T00:00:00"),  # no offset
            answer(rule="a\x00b"),
            None,
            "x" * 30_000,
        ]
        for raw in bad:
            with (
                self.subTest(raw=str(raw)[:40]),
                self.assertRaises(InterpreterOutputError),
            ):
                parse_interpreter_output(raw)

    def test_the_model_never_names_an_id(self):
        with self.assertRaises(InterpreterOutputError):
            parse_interpreter_output(
                answer(scope="project")[:-1] + ', "project_id": "x"}'
            )


class StructureTest(unittest.TestCase):
    def test_ids_belong_to_their_scope(self):
        project, repo = uuid4(), uuid4()
        StructuredPreference(
            InterpretedScope.REPO, "r", project_id=project, repo_id=repo
        )
        with self.assertRaises(InvalidMemoryInputError) as caught:
            StructuredPreference(InterpretedScope.USER, "r", project_id=project)
        self.assertEqual(caught.exception.problem, InputProblem.NOT_ALLOWED)
        with self.assertRaises(InvalidMemoryInputError):
            StructuredPreference(InterpretedScope.PROJECT, "r", repo_id=repo)

    def test_limits(self):
        with self.assertRaises(InvalidMemoryInputError):
            StructuredPreference(InterpretedScope.USER, "r", exceptions=("e",) * 11)
        with self.assertRaises(InvalidMemoryInputError):
            StructuredPreference(InterpretedScope.USER, "r" * 2001)

    def test_risk_includes_the_other_texts(self):
        preference = StructuredPreference(InterpretedScope.USER, "use tabs")
        self.assertIs(preference.risk_level(), RiskLevel.LOW)
        self.assertIs(preference.risk_level("delete_branch"), RiskLevel.HIGH)


if __name__ == "__main__":
    unittest.main()
