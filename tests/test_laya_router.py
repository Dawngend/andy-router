import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from laya_router import LayaDecision, ask_laya
from tier_router import Tier, Tool, assess_task


def test_huge_number_from_laya_falls_back_instead_of_crashing() -> None:
    output = '{"tool": "codex", "tool_p": 0.5, "tier_score": 1, "mechanical": 1' + "0" * 400 + "}"
    with patch("laya_router.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output)):
        assert ask_laya("task") is None


def test_ask_laya_parses_decision_and_limits_task() -> None:
    output = json.dumps({"tool": "claude", "tool_p": 0.82, "tier_score": 1.4,
                         "tool_confidence": 0.91, "tier_probabilities": [0.1, 0.8, 0.1], "hijack": 0.3})
    with patch("laya_router.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output)) as run:
        decision = ask_laya("x" * 2000)

    assert decision == LayaDecision("claude", 0.82, 1.4, 0.91, 0.3)
    assert json.loads(run.call_args.kwargs["input"])["task"] == "x" * 1500
    assert run.call_args.kwargs["timeout"] == 60


def test_questions_path_reaches_subprocess(tmp_path: Path) -> None:
    questions = tmp_path / "questions.json"
    output = json.dumps({"tool": "codex", "tool_p": 0.5, "tier_score": 0})
    with patch("laya_router.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output)) as run:
        assert ask_laya("task", questions=questions) is not None
    assert run.call_args.kwargs["env"]["LAYA_QUESTIONS"] == str(questions)


def test_default_questions_are_the_measured_v3_set() -> None:
    # v3 won on labeled data (laya/eval_tasks.json and the unseen laya/eval_holdout.json); re-run
    # laya/eval_laya.py on both before changing these questions.
    questions = json.loads((Path(__file__).parents[1] / "laya/questions.json").read_text(encoding="utf-8"))
    assert set(questions) == {"tool", "tier", "hijack", "mechanical", "big"}
    assert set(questions["tool"]["criteria"]) == {"codex", "claude", "agy"}
    assert all(questions[name]["type"] == "noul" for name in ("hijack", "mechanical", "big"))
    assert not any(ch.isdigit() for level in questions["tier"]["criteria"] for ch in level)


def test_laya_failure_keeps_rule_route() -> None:
    with patch("laya_router.subprocess.run", side_effect=FileNotFoundError):
        decision = assess_task("investigate behavior")

    assert decision.tool is Tool.CODEX
    assert decision.tier is Tier.MID


def test_bad_laya_output_returns_none() -> None:
    with patch("laya_router.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "not json")):
        assert ask_laya("task") is None


def test_laya_applies_to_default_route() -> None:
    with patch("tier_router.ask_laya", return_value=LayaDecision("claude", 0.82, 1.4)) as ask:
        decision = assess_task("investigate behavior")

    ask.assert_called_once_with("investigate behavior")
    assert (decision.tool, decision.tier, decision.model) == (Tool.CLAUDE, Tier.MID, "sonnet")
    assert "laya: claude p=0.82, tier mid" in decision.reasons
    assert "default tier: mid" not in decision.reasons


@pytest.mark.parametrize(
    ("score", "expected"), ((0.2, Tier.LOW), (1.6, Tier.HIGH))
)
def test_laya_score_rounds_to_tier(score: float, expected: Tier) -> None:
    with patch("tier_router.ask_laya", return_value=LayaDecision("agy", 0.6, score)):
        assert assess_task("investigate behavior").tier is expected


def test_low_confidence_is_ignored() -> None:
    with patch("tier_router.ask_laya", return_value=LayaDecision("claude", 0.44, 2)):
        decision = assess_task("investigate behavior")

    assert (decision.tool, decision.tier) == (Tool.CODEX, Tier.MID)
    assert not any(reason.startswith("laya:") for reason in decision.reasons)


def test_half_confidence_is_accepted() -> None:
    with patch("tier_router.ask_laya", return_value=LayaDecision("claude", 0.5, 1)):
        decision = assess_task("investigate behavior")

    assert decision.tool is Tool.CLAUDE


def full_answers(codex=0.2, claude=0.6, agy=0.2, mechanical=0.1, big=0.1) -> LayaDecision:
    return LayaDecision("claude", claude, 1.0, 0.5, 0.0, {"codex": codex, "claude": claude, "agy": agy},
                        mechanical, big)


def test_policy_decides_when_laya_gives_full_answers() -> None:
    with patch("tier_router.ask_laya", return_value=full_answers(big=0.9)):
        decision = assess_task("implement and test the repository")

    # Word signals (implement, test, repository) outweigh Laya's lean toward Claude; big >= 0.8 means high.
    assert (decision.tool, decision.tier) == (Tool.CODEX, Tier.HIGH)
    assert any(reason.startswith("policy: codex") for reason in decision.reasons)


def test_rules_decide_when_laya_is_unavailable() -> None:
    with patch("tier_router.ask_laya", return_value=None):
        decision = assess_task("implement and test the repository")

    assert decision.tool is Tool.CODEX
    assert not any(reason.startswith("policy:") for reason in decision.reasons)


