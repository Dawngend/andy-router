from pathlib import Path

import pytest

from tier_router import (
    TaskType,
    Tier,
    Tool,
    assess_task,
    build_agent_command,
    check_task_state,
    classify_task_type,
    parse_tier,
    select_model,
)


def test_fix_auth_bug_routes_to_mid() -> None:
    decision = assess_task("fix this auth bug")

    assert decision.tier is Tier.MID
    assert not decision.high_risk


def test_architecture_and_production_routes_to_high_and_risk_gate() -> None:
    decision = assess_task("redesign the production distributed architecture")

    assert decision.tier is Tier.HIGH
    assert decision.high_risk
    assert "production change" in decision.risk_reasons


def test_routine_formatting_routes_to_low() -> None:
    assert assess_task("format this comment").tier is Tier.LOW


@pytest.mark.parametrize(
    ("prompt", "expected_tier"),
    (
        ("format this file", Tier.LOW),
        ("refactor the auth endpoint", Tier.MID),
        ("design the system architecture", Tier.MID),
        ("refactor the architecture", Tier.HIGH),
        ("redesign the architecture", Tier.HIGH),
        ("plan a security migration for production", Tier.HIGH),
    ),
)
def test_complexity_probe_table_covers_tier_threshold_blast_radius(
    prompt: str, expected_tier: Tier
) -> None:
    assert assess_task(prompt).tier is expected_tier


def test_default_tier_is_used_for_ambiguous_prompt() -> None:
    assert assess_task("investigate behavior", Tier.LOW).tier is Tier.LOW


def test_task_type_classifier_uses_documented_capability_terms() -> None:
    assert (
        classify_task_type("pair programming for system architecture").task_type
        is TaskType.AGENTIC_ENGINEERING
    )
    assert (
        classify_task_type("implement and test this repository bug fix").task_type
        is TaskType.REPOSITORY_ENGINEERING
    )
    assert (
        classify_task_type("curate Obsidian notes and maintain frontmatter").task_type
        is TaskType.KNOWLEDGE_WORK
    )


def test_task_type_classifier_is_independent_of_prompt_length() -> None:
    prompt = "curate Obsidian notes " + ("background " * 100)

    assert classify_task_type(prompt).task_type is TaskType.KNOWLEDGE_WORK


def test_capability_selects_tool_before_complexity_selects_model() -> None:
    routine_note = assess_task("format this Obsidian note")
    complex_repository = assess_task(
        "implement test debug refactor auth database api production security"
    )

    assert routine_note.tool is Tool.CLAUDE
    assert routine_note.model == "haiku"
    assert complex_repository.tool is Tool.CODEX
    assert complex_repository.model == "gpt-6-astra"


def test_model_selection_escalates_only_with_complexity_tier() -> None:
    assert select_model(Tool.AGY, Tier.LOW).model == "gemini-3.8-flash-high"
    assert select_model(Tool.AGY, Tier.MID).model == "gemini-3.8-flash-high"
    assert select_model(Tool.AGY, Tier.HIGH).model == "gemini-3.8-flash-high"
    assert select_model(Tool.CODEX, Tier.LOW).model == "gpt-6-luna"
    assert select_model(Tool.CODEX, Tier.MID).model == "gpt-6-sol"
    assert select_model(Tool.CODEX, Tier.HIGH).model == "gpt-6-astra"
    assert select_model(Tool.CLAUDE, Tier.HIGH).model == "opus"


def test_commands_use_verified_noninteractive_contracts() -> None:
    assert build_agent_command(Tier.LOW, "task") == ["agy", "--print", "task"]
    assert build_agent_command(Tier.MID, "task") == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "task",
    ]
    assert build_agent_command(Tier.HIGH, "task", model="explicit-model") == [
        "claude",
        "--print",
        "--model",
        "explicit-model",
        "task",
    ]


def test_parse_tier_rejects_unknown_value() -> None:
    try:
        parse_tier("guess")
    except ValueError as exc:
        assert "Unknown tier" in str(exc)
    else:
        raise AssertionError("parse_tier should reject an unknown value")


@pytest.mark.parametrize(
    ("legacy_name", "expected"),
    (("luna", Tier.LOW), ("terra", Tier.MID), ("sol", Tier.HIGH)),
)
def test_parse_tier_accepts_legacy_aliases_without_emitting_them(
    legacy_name: str, expected: Tier
) -> None:
    tier = parse_tier(legacy_name)

    assert tier is expected
    assert tier.value not in {"luna", "terra", "sol"}


def test_state_check_requires_explicit_completion_evidence(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "Completed: implemented auth token refresh bug fix.\n", encoding="utf-8"
    )

    result = check_task_state("implement auth token refresh bug", (tmp_path,))

    assert result.already_completed
    assert result.evidence == (tmp_path / "notes.md",)


def test_state_check_does_not_infer_from_filename(tmp_path: Path) -> None:
    (tmp_path / "auth_bug.md").write_text("Work remains pending.\n", encoding="utf-8")

    assert not check_task_state("fix auth bug", (tmp_path,)).already_completed


def test_state_check_ignores_test_fixture_text(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_auth.py").write_text(
        'fixture = "Completed: fixed this auth bug"\n', encoding="utf-8"
    )

    assert not check_task_state("fix this auth bug", (tmp_path,)).already_completed


def test_state_check_ignores_project_local_pytest_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / ".test-tmp-router" / "case"
    artifact.mkdir(parents=True)
    (artifact / "notes.md").write_text(
        "Completed: fixed this auth bug.\n", encoding="utf-8"
    )

    assert not check_task_state("fix this auth bug", (tmp_path,)).already_completed


def test_state_check_ignores_negated_completion_language(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "Not yet completed: implement auth token refresh bug fix.\n", encoding="utf-8"
    )

    assert not check_task_state("implement auth token refresh bug", (tmp_path,)).already_completed


def test_high_risk_flags_remove_and_disable_with_intervening_words() -> None:
    decision = assess_task("remove all rows from the staging table")
    assert decision.high_risk
    assert "destructive operation" in decision.risk_reasons

    decision = assess_task("disable the security checks for this run")
    assert decision.high_risk
    assert "security bypass" in decision.risk_reasons


def test_mid_command_is_read_only_when_cwd_is_inside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    command = build_agent_command(Tier.MID, "task", cwd=vault, vault=vault)

    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "read-only"


def test_mid_command_is_unrestricted_outside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    other = tmp_path / "some-repo"
    other.mkdir()
    command = build_agent_command(Tier.MID, "task", cwd=other, vault=vault)

    assert "--sandbox" not in command
