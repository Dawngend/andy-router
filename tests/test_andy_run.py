from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import andy_run
from laya_router import LayaDecision
from tier_router import Tier, Tool, assess_task


def test_call_captures_utf8_child_output(tmp_path):
    command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write('\u201d'.encode('utf-8'))"]
    result = andy_run.call(command, tmp_path, subprocess.run)
    assert result.stdout == "\u201d"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    spec = tmp_path / "task.md"
    spec.write_text(f"---\ntitle: Fix greeting\nrepo: {repo}\ncheck: python -m pytest\n---\nFix greeting.\n", encoding="utf-8")
    state = {"diff": "+hello", "changed_files": [], "reviews": [], "checks": [], "check_outputs": [],
             "branch_exists": False, "fail": None, "staged": 1, "dirty": "", "capacity": {}}
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        code, output = 0, ""
        if isinstance(command, str):
            code = state["checks"].pop(0) if state["checks"] else 0
            output = (state["check_outputs"].pop(0) if state["check_outputs"]
                      else "\n".join(str(i) for i in range(25)))
        elif command[0] == "git":
            if command[1] == "show-ref":
                # branch_exists marks only the first-choice name as taken.
                code = 0 if state["branch_exists"] and command[-1].endswith("fix-greeting-20261005") else 1
            elif command[1:3] == ["diff", "--cached"]:
                code = state["staged"]
            elif command[1:3] == ["diff", "--name-only"]:
                output = state["changed_files"].pop(0) if state["changed_files"] else ""
            elif command[1] == "diff":
                output = state["diff"]
            elif command[1] == "rev-parse":
                output = "abc123\n"
            elif command[1] == "status":
                output = state["dirty"]
            if command[1] == state["fail"]:
                code = 1
        elif Path(command[0]).stem == "codex" and state["capacity"].get(command[3], 0):
            state["capacity"][command[3]] -= 1
            return subprocess.CompletedProcess(command, 1, "", "Selected model is at capacity")
        elif "In at most 10 lines" in (kwargs.get("input") or "") + " ".join(command):
            if state.get("diagnosis_failure"):
                return subprocess.CompletedProcess(command, 1, "", "diagnosis unavailable")
            output = "Fix the shared root cause."
        elif "Answer with JSON only" in (kwargs.get("input") or "") + " ".join(command):
            if Path(command[0]).stem in state.get("reviewer_down", ()):
                return subprocess.CompletedProcess(command, 1, "Failed to authenticate", "")
            output = state["reviews"].pop(0) if state["reviews"] else '{"pass": true, "findings": []}'
        else:
            output = "AGREE: greeting needs correction."
            if state["fail"] == "builder":
                code = 1
        return subprocess.CompletedProcess(command, code, output, "failure" if code else "")

    route = Mock(return_value=assess_task("fix greeting", use_laya=False))
    notion = Mock(return_value="notion-page")
    monkeypatch.setattr(andy_run.subprocess, "run", run)
    monkeypatch.setattr(andy_run.shutil, "which", lambda tool: None if tool in state.get("missing_tools", ())
                        else f"C:/Program Files/{tool}.cmd")
    monkeypatch.setattr(andy_run, "assess_task", route)
    monkeypatch.setattr(andy_run, "ask_laya", Mock(return_value=None))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("ANDY_STATE_DIR", str(tmp_path / "home/.andy_sync"))
    options = {"clock": lambda: datetime(2026, 10, 5), "notion": notion}

    def execute(*extra):
        return andy_run.main([str(spec), *extra], **options)

    def report():
        runs = sorted((tmp_path / "home/.andy_sync/runs").glob("fix-greeting-20261005*.json"))
        return json.loads(runs[-1].read_text())

    return state, calls, route, notion, spec, execute, report


def git_calls(calls, name):
    return [(command, kwargs) for command, kwargs in calls
            if isinstance(command, list) and command[:2] == ["git", name]]


@pytest.mark.parametrize("pinned,expected", [("claude", "agy"), ("opencode", "opencode")])
def test_builder_mode_overrides_header_except_opencode(harness, pinned, expected, monkeypatch, tmp_path):
    state, calls, route, notion, spec, execute, report = harness
    monkeypatch.setattr(andy_run.tempfile, "tempdir", str(tmp_path))
    switch = andy_run.state_dir() / "builder_mode"
    switch.parent.mkdir(parents=True)
    switch.write_text("gemini")
    spec.write_text(spec.read_text().replace(
        "check: python -m pytest\n", f"check: python -m pytest\ntool: {pinned}\n"))
    assert execute() == 0
    assert report()["route"]["tool"] == expected


def test_main_reads_cooldown_before_building(harness):
    import tool_cooldown

    state, calls, route, notion, spec, execute, report = harness
    tool_cooldown.record_cooldown(andy_run.state_dir() / "tool_cooldown.json", "codex", datetime(2026, 10, 5))
    assert execute() == 0
    assert report()["route"]["tool"] == "claude"


def test_hijack_stops_before_build(harness, monkeypatch):
    state, calls, route, notion, spec, execute, report = harness
    ask = Mock(return_value=LayaDecision("codex", 0.9, 1, 0.8, 0.85))
    monkeypatch.setattr(andy_run, "ask_laya", ask)
    assert execute() == 3
    ask.assert_called_once_with(spec.read_text())
    assert report()["outcome"] == "stopped-for-dawn"
    assert report()["rounds"] == []
    assert report()["reasons"] == ["spec text looks like an instruction hijack (p=0.85)"]
    assert calls == []


def test_unavailable_hijack_check_continues(harness):
    state, calls, route, notion, spec, execute, report = harness
    assert execute() == 0
    assert report()["hijack_check"] == "unavailable"
    assert report()["rounds"]


def test_notion_false_skips_report(harness):
    state, calls, route, notion, spec, execute, report = harness
    assert andy_run.main([str(spec)], notion=False, clock=lambda: datetime(2026, 10, 5)) == 0
    notion.assert_not_called()
    assert "notion_page_id" not in report()


def test_questions_without_hijack_report_null(harness, monkeypatch):
    state, calls, route, notion, spec, execute, report = harness
    monkeypatch.setattr(andy_run, "ask_laya", Mock(return_value=LayaDecision("codex", 0.9, 1)))
    assert execute() == 0
    assert report()["hijack_check"] is None


def test_small_pass_merges_checks_then_pushes_and_reports(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    assert execute() == 0
    saved = report()
    assert saved["outcome"] == "merged"
    assert saved["route"]["reasons"]
    assert saved["rounds"][0]["commit"] == "abc123"
    assert saved["main_before"] == saved["main_after"] == "abc123"
    assert saved["rounds"][0]["check"]["tail"].splitlines() == [str(i) for i in range(5, 25)]
    assert git_calls(calls, "worktree")[0][0] == [
        "git", "worktree", "add", "-b", "run/fix-greeting-20261005",
        str(spec.parent / "wt-fix-greeting-20261005"), "main"]
    assert git_calls(calls, "add")[0][0] == ["git", "add", "-A"]
    assert git_calls(calls, "add")[0][1]["cwd"] == spec.parent / "wt-fix-greeting-20261005"
    assert git_calls(calls, "merge")[0][0] == ["git", "merge", "--no-edit", "run/fix-greeting-20261005"]
    pull = git_calls(calls, "pull")[0]
    assert pull[0] == ["git", "pull", "--rebase", "--autostash", "origin", "main"]
    assert calls.index(git_calls(calls, "checkout")[0]) < calls.index(pull)
    assert calls.index(pull) < calls.index(git_calls(calls, "merge")[0])
    assert calls[-1][0] == ["git", "push", "origin", "main"]
    assert sum(isinstance(command, str) for command, _ in calls) == 2
    assert notion.call_count == 1
    route.assert_called_once_with(spec.read_text(), use_laya=True)
    out = capsys.readouterr().out
    assert "Route: codex @ mid (gpt-6-sol)" in out and "Reason:" in out


def test_small_spec_skips_plan_check(harness):
    state, calls, route, notion, spec, execute, report = harness
    assert execute() == 0
    assert "plan_check" not in report()
    assert sum("Review this spec before anyone builds it" in (kw.get("input") or "")
               for _, kw in calls) == 0


def test_big_plan_high_finding_stops_before_worktree(harness):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("title:", "size: big\ntitle:"))
    state["reviews"] = [json.dumps({"pass": False, "findings": [
        {"severity": "high", "text": "missing rollback", "fix": "add rollback"},
        {"severity": "high", "text": "unsafe deletion", "fix": "remove deletion"},
        {"severity": "low", "text": "add an example", "fix": "show an example"}]})]

    assert execute() == 3
    saved = report()
    assert saved["outcome"] == "stopped-for-dawn"
    assert saved["reasons"] == ["plan check: missing rollback", "plan check: unsafe deletion"]
    assert saved["plan_check"]["reviewer"] == "claude/sonnet"
    assert saved["plan_check"]["review"]["findings"][0]["severity"] == "high"
    assert saved["rounds"] == []
    assert saved["branch"] is None and saved["worktree"] is None
    assert not git_calls(calls, "worktree")
    assert not git_calls(calls, "show-ref")
    assert not any(Path(cmd[0]).stem == "codex" for cmd, _ in calls if isinstance(cmd, list))
    plan_call = next(kw for cmd, kw in calls if isinstance(cmd, list)
                     and Path(cmd[0]).stem == "claude")
    assert plan_call["cwd"] == spec.parent / "repo with spaces"
    assert spec.read_text() in plan_call["input"]


def test_big_plan_low_findings_reach_first_builder(harness):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("title:", "size: big\ntitle:"))
    state["reviews"] = [json.dumps({"pass": True, "findings": [
        {"severity": "low", "text": "cover empty input", "fix": "add a test"}]})]

    assert execute() == 3  # Big builds still wait for Dawn at merge.
    assert report()["plan_check"]["review"]["findings"][0]["text"] == "cover empty input"
    builder = next(kw["input"] for cmd, kw in calls if isinstance(cmd, list)
                   and Path(cmd[0]).stem == "codex")
    assert "Plan review notes:\n- cover empty input Fix: add a test" in builder
    assert calls.index(git_calls(calls, "worktree")[0]) > next(
        i for i, (cmd, _) in enumerate(calls) if isinstance(cmd, list) and Path(cmd[0]).stem == "claude")


def test_big_plan_unavailable_continues(harness):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("title:", "size: big\ntitle:"))
    state["reviewer_down"] = {"claude", "agy"}

    assert execute() == 3
    assert report()["plan_check"] == "unavailable"
    assert report()["rounds"]
    assert git_calls(calls, "worktree")


@pytest.mark.parametrize("size,expected", [("small", "no"), ("big", "yes")])
def test_dry_run_prints_plan_check_without_review(harness, capsys, size, expected):
    state, calls, route, notion, spec, execute, report = harness
    if size == "big":
        spec.write_text(spec.read_text().replace("title:", "size: big\ntitle:"))
    assert execute("--dry-run") == 0
    assert f"Plan check: {expected}" in capsys.readouterr().out
    assert calls == []


def test_scope_prompt_and_in_scope_change_passes(harness):
    state, calls, route, notion, spec, execute, report = harness
    scope = "omni-router/andy_run.py, omni-router/tests/"
    spec.write_text(spec.read_text().replace("check:", f"scope: {scope}\ncheck:"))
    state["changed_files"] = ["omni-router/andy_run.py\nomni-router/tests/test_andy_run.py\n"]

    assert execute() == 0
    builder = next(kw["input"] for cmd, kw in calls if isinstance(cmd, list)
                   and Path(cmd[0]).stem == "codex")
    assert f"Only change files under: {scope}" in builder
    assert report()["rounds"][0]["scope_violations"] == []
    assert report()["rounds"][0]["pass"] is True
    assert git_calls(calls, "diff")[-1][0] == ["git", "diff", "--name-only", "main...run/fix-greeting-20261005"]
    assert git_calls(calls, "diff")[-1][1]["cwd"] == spec.parent / "wt-fix-greeting-20261005"


def test_out_of_scope_change_fails_round_and_is_named_in_next_prompt(harness):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check:", "scope: omni-router/tests/\ncheck:"))
    state["changed_files"] = ["other/file.py\n", "omni-router/tests/test_andy_run.py\n"]

    assert execute() == 0
    rounds = report()["rounds"]
    assert rounds[0]["check"]["pass"] and rounds[0]["review"]["pass"]
    assert rounds[0]["scope_violations"] == ["other/file.py"]
    assert rounds[0]["pass"] is False
    assert rounds[1]["scope_violations"] == [] and rounds[1]["pass"] is True
    prompts = [kw["input"] for cmd, kw in calls if isinstance(cmd, list)
               and Path(cmd[0]).stem == "codex"]
    assert "Only change files under: omni-router/tests/" in prompts[1]
    assert "Revert files changed outside scope: other/file.py" in prompts[1]


def test_final_round_scope_violation_stops_run(harness):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check:", "scope: omni-router/tests/\ncheck:"))
    state["changed_files"] = ["other/file.py\n"] * 3

    assert execute() == 3
    saved = report()
    assert len(saved["rounds"]) == 3
    assert all(round_["scope_violations"] == ["other/file.py"] and not round_["pass"]
               for round_ in saved["rounds"])
    assert "files changed outside scope: other/file.py" in saved["reasons"]
    assert not git_calls(calls, "merge")


def test_no_scope_header_keeps_prompt_and_report_unchanged(harness):
    state, calls, route, notion, spec, execute, report = harness
    assert execute() == 0
    builder = next(kw["input"] for cmd, kw in calls if isinstance(cmd, list)
                   and Path(cmd[0]).stem == "codex")
    assert "Only change files under:" not in builder
    assert "scope_violations" not in report()["rounds"][0]
    assert not any(cmd[:3] == ["git", "diff", "--name-only"] for cmd, _ in git_calls(calls, "diff"))


def test_dry_run_prints_scope_when_set(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check:", "scope: omni-router/tests/\ncheck:"))
    assert execute("--dry-run") == 0
    assert "Scope: omni-router/tests/" in capsys.readouterr().out
    assert calls == []


@pytest.mark.parametrize("tool,tier,reviewer,model", [
    (Tool.CODEX, Tier.MID, "claude", "sonnet"),
    (Tool.CODEX, Tier.HIGH, "claude", "opus"),
    (Tool.CLAUDE, Tier.MID, "codex", "gpt-6-sol"),
])
def test_reviewer_family_and_cli_contract(harness, tool, tier, reviewer, model):
    state, calls, route, notion, spec, execute, report = harness
    state["missing_tools"] = {"agy"}
    route.return_value = replace(route.return_value, tool=tool, tier=tier)
    spec.write_text(spec.read_text() + '\nSay "hello" and \'welcome\'.\n')
    assert execute() == (3 if tier is Tier.HIGH else 0)
    agents = [(cmd, kw) for cmd, kw in calls if isinstance(cmd, list) and cmd[0] != "git"]
    builder, review = agents
    assert builder[0][0].endswith(f"/{tool.value}.cmd")
    assert review[0][0].endswith(f"/{reviewer}.cmd")
    assert model in review[0]
    assert spec.read_text() in builder[1]["input"]
    assert "Do not push or merge" in builder[1]["input"]
    assert "Answer with JSON only" in review[1]["input"]
    for command, kwargs in agents:
        assert kwargs["input"] not in command
        assert all("\n" not in arg and "Fix greeting" not in arg for arg in command)
        assert command[-1] == "-" if command[0].endswith("codex.cmd") else command[1] == "-p"
    assert builder[1]["cwd"] == review[1]["cwd"] == spec.parent / "wt-fix-greeting-20261005"
    if tool is Tool.CODEX:
        assert builder[0][1:6] == ["exec", "-m", route.return_value.model, "--sandbox", "workspace-write"]
    if reviewer == "codex":
        assert "read-only" in review[0]


@pytest.mark.parametrize("condition", ["big", "high", "delete", "production", "credentials", "spec-risk", "dirty", "checkout", "pull", "secret"])
def test_stop_conditions_do_not_merge(harness, condition):
    state, calls, route, notion, spec, execute, report = harness
    if condition == "big":
        spec.write_text(spec.read_text().replace("title:", "size: big\ntitle:"))
    elif condition == "high":
        route.return_value = replace(route.return_value, tier=Tier.HIGH)
    elif condition == "spec-risk":
        spec.write_text(spec.read_text() + "Remove old rows.")
    elif condition == "delete":
        spec.write_text(spec.read_text() + "Delete old files.")
    elif condition == "dirty":
        state["dirty"] = " M unrelated.md"
    elif condition in {"checkout", "pull"}:
        state["fail"] = condition
    elif condition == "secret":
        state["diff"] = "+KEY=ghp_" + "a" * 30
    else:
        spec.write_text(spec.read_text() + condition)
    assert execute() == 3
    assert report()["outcome"] == "stopped-for-dawn"
    assert not git_calls(calls, "merge") and not git_calls(calls, "push")


@pytest.mark.parametrize("output", ["not json", '{"pass": "true", "findings": []}'])
def test_invalid_reviews_from_every_reviewer_stop_after_one_round(harness, output):
    state, calls, route, notion, spec, execute, report = harness
    state["reviews"] = [output] * 6
    assert execute() == 3
    data = report()
    assert len(data["rounds"]) == 1 and "No reviewer available" in data["reasons"][0]
    assert not git_calls(calls, "merge") and not git_calls(calls, "push")


def test_invalid_claude_review_falls_back_to_agy(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["reviews"] = ["not json", '{"pass": true, "findings": []}']
    assert execute() == 0
    assert report()["rounds"][0]["reviewer"] == "agy/gemini-3.8-flash-high"


@pytest.mark.parametrize("failure", ["check", "high", "false"])
def test_round_failures_exhaust_three_rounds(harness, failure):
    state, calls, route, notion, spec, execute, report = harness
    if failure == "check":
        state["checks"] = [1, 1, 1]
        state["check_outputs"] = ["first error", "second error", "third error"]
    else:
        output = {"false": '{"pass": false, "findings": []}',
                  "high": '{"pass": true, "findings": [{"severity": "high", "text": "bug", "fix": "fix bug"}]}'}[failure]
        state["reviews"] = [output] * 3
    assert execute() == 3
    assert len(report()["rounds"]) == 3
    assert all(not r["pass"] for r in report()["rounds"])
    assert not git_calls(calls, "merge") and not git_calls(calls, "push")
    assert len(git_calls(calls, "commit")) == 3


@pytest.mark.parametrize("tails", [
    ("FAILED test_fix in 3.21s", "FAILED test_fix in 4.85s"),
    ("FAILED test_fix in 3.21s at 0x7f3a10", "FAILED test_fix in 4.85s at 0x7f3b20"),
    ("FAILED test_fix in 3.21s", "FAILED test_fix in 3.21s"),
])
def test_same_check_failure_stops_after_round_two(harness, tails):
    state, calls, route, notion, spec, execute, report = harness
    state["checks"] = [1, 1, 1]
    state["check_outputs"] = list(tails)

    assert execute() == 3
    saved = report()
    assert saved["outcome"] == "stopped-for-dawn"
    assert saved["reasons"] == ["same check failure twice in a row"]
    assert len(saved["rounds"]) == 2
    assert saved["stuck_diagnosis"] == "Fix the shared root cause."
    diagnosis = [(command, kwargs) for command, kwargs in calls if isinstance(command, list)
                 and "In at most 10 lines" in (kwargs.get("input") or "")]
    assert len(diagnosis) == 1
    assert spec.read_text() in diagnosis[0][1]["input"]
    assert state["diff"] in diagnosis[0][1]["input"]
    assert all(tail in diagnosis[0][1]["input"] for tail in tails)
    assert not git_calls(calls, "merge") and not git_calls(calls, "push")


@pytest.mark.parametrize("builder,expected_tool,expected_model", [
    (Tool.CODEX, "claude", "opus"),
    (Tool.AGY, "claude", "opus"),
    (Tool.CLAUDE, "codex", "gpt-6-sol"),
])
def test_stuck_diagnosis_model(harness, builder, expected_tool, expected_model):
    state, calls, route, notion, spec, execute, report = harness
    route.return_value = replace(route.return_value, tool=builder)
    state["checks"] = [1, 1]

    assert execute() == 3
    diagnosis = [command for command, kwargs in calls if isinstance(command, list)
                 and "In at most 10 lines" in (kwargs.get("input") or "")]
    assert len(diagnosis) == 1
    assert Path(diagnosis[0][0]).stem == expected_tool
    assert expected_model in diagnosis[0]
    if builder is Tool.CLAUDE:
        assert "read-only" in diagnosis[0]
        assert 'model_reasoning_effort="xhigh"' in diagnosis[0]
    else:
        assert "--permission-mode" not in diagnosis[0]


def test_save_claude_uses_codex_for_stuck_diagnosis(harness):
    state, calls, route, notion, spec, execute, report = harness
    switch = Path.home() / ".andy_sync" / "save_claude"
    switch.parent.mkdir(parents=True)
    switch.touch()
    state["checks"] = [1, 1]

    assert execute() == 3
    diagnosis = [command for command, kwargs in calls if isinstance(command, list)
                 and "In at most 10 lines" in (kwargs.get("input") or "")]
    assert len(diagnosis) == 1
    assert Path(diagnosis[0][0]).stem == "codex"
    assert "gpt-6-sol" in diagnosis[0]


def test_failed_stuck_diagnosis_does_not_fail_run(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["checks"] = [1, 1]
    state["diagnosis_failure"] = True

    assert execute() == 3
    saved = report()
    assert saved["outcome"] == "stopped-for-dawn"
    assert saved["reasons"] == ["same check failure twice in a row"]
    assert saved["stuck_diagnosis"] == "unavailable: diagnosis unavailable"
    assert len(saved["rounds"]) == 2
    assert not git_calls(calls, "merge")


def test_findings_and_answers_preserved_and_second_round_passes(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["reviews"] = ['{"pass": false, "findings": [{"severity": "high", "text": "bad greeting", "fix": "say hello"}]}']
    assert execute() == 0
    prompts = [kw["input"] for cmd, kw in calls if isinstance(cmd, list) and cmd[0].endswith("codex.cmd")]
    assert andy_run.REPLY_RULE in prompts[1] and "bad greeting" in prompts[1] and "say hello" in prompts[1]
    assert report()["rounds"][1]["builder_answers"].startswith("AGREE:")


def test_pass_on_third_round_merges(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["checks"] = [1, 1, 0]
    state["check_outputs"] = ["first error", "second error", "all clear"]
    assert execute() == 0
    assert report()["rounds"][-1]["pass"]
    assert len(report()["rounds"]) == 3
    assert report()["reasons"] == []
    assert git_calls(calls, "merge") and git_calls(calls, "push")


def test_ordinary_code_and_removed_or_context_secrets_do_not_stop(harness):
    state, calls, route, notion, spec, execute, report = harness
    secret = "ghp_" + "a" * 30
    state["diff"] = f"+++ b/{secret}\n-{secret}\n {secret}\n+items.clear()\n+remove_old()\n+prod = 1"
    assert execute() == 0
    assert git_calls(calls, "merge")


def test_status_ignores_untracked_and_dirty_submodule_contents(harness, monkeypatch):
    state, calls, route, notion, spec, execute, report = harness
    runner = andy_run.subprocess.run

    def run(command, **kwargs):
        if isinstance(command, list) and command[:2] == ["git", "status"]:
            state["dirty"] = ""
            if "--untracked-files=no" not in command:
                state["dirty"] += "?? scratch.md\n"
            if "--ignore-submodules=dirty" not in command:
                state["dirty"] += " m 98_Source_Mirrors/profile\n"
        return runner(command, **kwargs)

    monkeypatch.setattr(andy_run.subprocess, "run", run)
    assert execute() == 0
    assert git_calls(calls, "merge")


@pytest.mark.parametrize("condition", ["merge", "post-check"])
def test_merge_or_post_merge_failure_stops_without_push_or_abort(harness, condition):
    state, calls, route, notion, spec, execute, report = harness
    if condition == "merge":
        state["fail"] = "merge"
    else:
        state["checks"] = [0, 1]
    assert execute() == 3
    assert git_calls(calls, "merge")
    assert not git_calls(calls, "push")
    assert all("--abort" not in command for command, _ in calls)


@pytest.mark.parametrize("failure", ["builder", "commit", "worktree", "push"])
def test_execution_errors_are_reported(harness, failure):
    state, calls, route, notion, spec, execute, report = harness
    state["fail"] = failure
    assert execute() == 1
    assert report()["outcome"] == "failed"


def test_existing_branch_gets_the_next_free_suffix(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["branch_exists"] = True
    assert execute() == 0
    worktree_add = git_calls(calls, "worktree")[0][0]
    assert "run/fix-greeting-20261005-2" in worktree_add
    assert report()["branch"] == "run/fix-greeting-20261005-2"


def test_dry_run_has_no_processes_writes_or_notion(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    before = sorted(spec.parent.rglob("*"))
    assert execute("--dry-run") == 0
    assert not calls
    assert sorted(spec.parent.rglob("*")) == before
    notion.assert_not_called()
    assert route.call_args.kwargs == {"use_laya": False}
    out = capsys.readouterr().out
    assert all(word in out for word in ("Route:", "Worktree:", "Builder:", "Reviewer:"))


def test_notion_failure_does_not_fail_run(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    notion.side_effect = RuntimeError("offline")
    assert execute() == 0
    assert report()["notion_error"] == "offline"
    assert "Notion report failed: offline" in capsys.readouterr().out


def test_no_edits_skips_commit(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["staged"] = 0
    assert execute() == 0
    assert not git_calls(calls, "commit")


@pytest.mark.parametrize("text,risky", [
    ("Notion rows are only renamed, never removed.", False),
    ("Do not delete anything in the vault.", False),
    ("Delete the old rows from the database.", True),
    ("Never remove rows. Then delete the cache folder.", True),
])
def test_negated_safety_sentences_are_not_risky(text, risky):
    pattern = next(p for p, reason in andy_run._HIGH_RISK_PATTERNS if reason == "destructive operation")
    assert andy_run.risky_match(pattern, text) is risky


@pytest.mark.parametrize("text", [
    "delete the old rows", "remove the cache folder", "drop the table",
    "purge backups", "wipe the worktree", "clear the database records",
])
def test_spec_destructive_data_instructions_are_risky(text):
    pattern = next(p for p, reason in andy_run._HIGH_RISK_PATTERNS if reason == "destructive operation")
    assert andy_run.risky_match(pattern, text)


@pytest.mark.parametrize("text", [
    "delete the old, unused rows",
    "delete the old,\nunused rows",
    "remove the\ncache folder",
])
def test_spec_wrapped_destructive_instructions_are_risky(text):
    pattern = next(p for p, reason in andy_run._HIGH_RISK_PATTERNS if reason == "destructive operation")
    assert andy_run.risky_match(pattern, text)


@pytest.mark.parametrize("text", [
    "can silently drop a batch", "kills the reader thread",
    "remove the duplicate page break", "clear wording",
    "Decode UTF-8 input without dropping malformed characters.",
    "andy read should remove duplicate page breaks from extracted text.",
    "The reader thread kills itself on failure, then clears the warning.",
    "Do not silently drop a batch while decoding UTF-8 slides.",
])
def test_spec_prose_without_destructive_data_instruction_is_safe(text):
    pattern = next(p for p, reason in andy_run._HIGH_RISK_PATTERNS if reason == "destructive operation")
    assert not andy_run.risky_match(pattern, text)


@pytest.mark.parametrize("line", [
    "shutil.rmtree(path)", "os.remove(path)", "os.unlink(path)",
    "Path.unlink(path)", "Path('old').unlink()", "path.unlink()", "rm -rf old-dir",
    "Remove-Item -Recurse old-dir", "Remove-Item -Path old -Recurse",
    "git push --force origin main", "git push origin main --force",
    "git push -f origin main", "git reset --hard HEAD",
    "git branch -D old", "DROP TABLE old", "TRUNCATE old",
    "DELETE FROM old",
])
def test_added_destructive_calls_stop_for_dawn(line):
    assert andy_run.destructive_diff("diff --git a/x b/x\n+++ b/x\n+" + line)


@pytest.mark.parametrize("line", [
    "# shutil.rmtree(path)", 'message = "os.remove(path)"',
    '"""DROP TABLE old"""', "# rm -rf old-dir",
    'note = "Remove-Item -Recurse old-dir"',
    "# git push --force origin main", "# DELETE FROM old",
    "-- DROP TABLE old", "/* TRUNCATE old */", "truncate = True",
    "can silently drop a batch", "remove the duplicate page break",
    "clear wording",
])
def test_added_comments_strings_and_prose_are_safe(line):
    assert not andy_run.destructive_diff("diff --git a/x b/x\n+++ b/x\n+" + line)


def test_destructive_call_after_unrelated_indentation_is_detected():
    diff = "diff --git a/x b/x\n+++ b/x\n+        value = 1\n+    value = 2\n+os.remove(path)"
    assert andy_run.destructive_diff(diff)


def test_destructive_command_after_unclosed_quote_is_detected():
    diff = 'diff --git a/readme b/readme\n+++ b/readme\n+"""unfinished\n' \
           'diff --git a/script b/script\n+++ b/script\n+rm -rf old-dir'
    assert andy_run.destructive_diff(diff)


def test_quotes_in_separate_files_cannot_hide_destructive_command():
    diff = ('diff --git a/readme b/readme\n+++ b/readme\n+"""unfinished\n'
            'diff --git a/script b/script\n+++ b/script\n+rm -rf old-dir\n'
            'diff --git a/other b/other\n+++ b/other\n+"""')
    assert andy_run.destructive_diff(diff)


def test_multiline_docstring_does_not_trigger_destructive_diff():
    diff = 'diff --git a/x b/x\n+++ b/x\n+"""\n+rm -rf old-dir\n+DROP TABLE old\n+"""'
    assert not andy_run.destructive_diff(diff)


def test_added_destructive_call_is_a_stop_reason():
    route = assess_task("Fix greeting", use_laya=False)
    reasons = andy_run.stop_reasons(
        {"size": "small"}, route, "Fix greeting", "+shutil.rmtree(path)", []
    )
    assert "Added diff lines contain a destructive operation" in reasons


def test_spec_header_pins_tool_and_tier_and_skips_routing(harness):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check: python -m pytest\n", "check: python -m pytest\ntool: codex\ntier: high\n"))
    assert execute() == 3  # tier high stops for Dawn before merging
    route.assert_called_once_with(spec.read_text(), use_laya=False)
    data = report()
    assert (data["route"]["tool"], data["route"]["tier"], data["route"]["model"]) == ("codex", "high", "gpt-6-astra")


@pytest.mark.parametrize("builder,reviewer", [
    (Tool.CODEX, Tool.CLAUDE),
    (Tool.CLAUDE, Tool.CODEX),
])
def test_effort_reaches_builder_and_reviewer(harness, capsys, builder, reviewer):
    state, calls, route, notion, spec, execute, report = harness
    state["missing_tools"] = {"agy"}
    spec.write_text(spec.read_text().replace("check: python -m pytest\n",
                                             f"check: python -m pytest\ntool: {builder.value}\ntier: mid\neffort: high\n"))
    assert execute() == 0
    agents = [command for command, _ in calls if isinstance(command, list) and command[0] != "git"]
    assert [Path(command[0]).stem for command in agents] == [builder.value, reviewer.value]
    for command, tool in zip(agents, (builder, reviewer)):
        if tool is Tool.CODEX:
            assert ["-c", 'model_reasoning_effort="high"'] == command[-3:-1]
        else:
            assert command[command.index("--effort") + 1] == "high"
    assert report()["route"]["effort"] == "high"
    out = capsys.readouterr().out
    assert f"Route: {builder.value} @ mid (" in out
    assert ", effort high)" in out


@pytest.mark.parametrize("builder", [Tool.CODEX, Tool.CLAUDE])
def test_no_effort_header_keeps_cli_defaults(harness, builder):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check: python -m pytest\n",
                                             f"check: python -m pytest\ntool: {builder.value}\ntier: mid\n"))
    assert execute() == 0
    assert report()["route"]["effort"] is None
    agents = [command for command, _ in calls if isinstance(command, list) and command[0] != "git"]
    assert all("--effort" not in command and "-c" not in command for command in agents)


def test_agy_ignores_effort_but_reviewer_receives_it(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check: python -m pytest\n",
                                             "check: python -m pytest\ntool: agy\ntier: mid\neffort: high\n"))
    assert execute() == 0
    agents = [command for command, _ in calls if isinstance(command, list) and command[0] != "git"]
    assert [Path(command[0]).stem for command in agents] == ["agy", "codex"]
    assert "--effort" not in agents[0] and "-c" not in agents[0]
    assert 'model_reasoning_effort="high"' in agents[1]
    assert report()["route"]["effort"] is None
    assert "Route: agy @ mid (gemini-3.8-flash-high)" in capsys.readouterr().out


def test_max_maps_to_xhigh_for_codex(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check: python -m pytest\n",
                                             "check: python -m pytest\neffort: max\n"))
    assert execute() == 0
    builder = next(command for command, _ in calls if isinstance(command, list)
                   and Path(command[0]).stem == "codex")
    assert 'model_reasoning_effort="xhigh"' in builder
    assert report()["route"]["effort"] == "xhigh"
    out = capsys.readouterr().out
    assert "Effort: max -> xhigh for Codex" in out
    assert "Route: codex @ mid (gpt-6-sol, effort xhigh)" in out


def test_max_maps_for_codex_reviewer_but_claude_keeps_max(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    state["missing_tools"] = {"agy"}
    spec.write_text(spec.read_text().replace("check: python -m pytest\n",
                                             "check: python -m pytest\ntool: claude\ntier: mid\neffort: max\n"))
    assert execute() == 0
    agents = [command for command, _ in calls if isinstance(command, list) and command[0] != "git"]
    assert agents[0][agents[0].index("--effort") + 1] == "max"
    assert 'model_reasoning_effort="xhigh"' in agents[1]
    assert report()["route"]["effort"] == "max"
    out = capsys.readouterr().out
    assert out.count("Effort: max -> xhigh for Codex") == 1
    assert "Route: claude @ mid (sonnet, effort max)" in out


def test_invalid_effort_has_no_side_effects(harness, capsys):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(spec.read_text().replace("check: python -m pytest\n",
                                             "check: python -m pytest\neffort: extreme\n"))
    assert execute() == 1
    assert "Spec effort must be low, medium, high, xhigh or max" in capsys.readouterr().out
    assert not calls
    notion.assert_not_called()


@pytest.mark.parametrize("text", ["no header", "---\ntitle: Missing fields\n---", "---\ntitle: X\nrepo: X\ncheck: X\nsize: medium\n---",
                                  "---\ntitle: X\nrepo: X\ncheck: X\ntool: gemini\n---"])
def test_invalid_spec_has_no_side_effects(harness, text):
    state, calls, route, notion, spec, execute, report = harness
    spec.write_text(text)
    assert execute() == 1
    assert not calls
    notion.assert_not_called()


@pytest.mark.parametrize("enabled,pinned,expected", [
    (True, False, Tool.CODEX),
    (True, True, Tool.CLAUDE),
    (False, False, Tool.CLAUDE),
])
@pytest.mark.parametrize("tier", list(Tier))
def test_save_claude_builder(harness, monkeypatch, capsys, enabled, pinned, expected, tier):
    state, calls, route, notion, spec, execute, report = harness
    monkeypatch.setattr(andy_run, "save_claude", lambda: enabled)
    route.return_value = replace(route.return_value, tool=Tool.CLAUDE, tier=tier,
                                 model=andy_run.select_model(Tool.CLAUDE, tier).model)
    if pinned:
        spec.write_text(spec.read_text().replace("check: python -m pytest\n",
                                                "check: python -m pytest\ntool: claude\n"))

    assert execute() == (3 if tier is Tier.HIGH else 0)
    model = andy_run.select_model(expected, tier).model
    data = report()
    assert (data["route"]["tool"], data["route"]["tier"], data["route"]["model"]) == (
        expected.value, tier.value, model)
    builder = next(command for command, _ in calls
                   if isinstance(command, list) and command[0] != "git")
    assert Path(builder[0]).stem == expected.value
    assert model in builder
    assert ("save-Claude on: builder claude -> codex" in capsys.readouterr().out) is (
        enabled and not pinned)


def test_save_claude_switch_puts_claude_reviewers_last(tmp_path, monkeypatch):
    monkeypatch.setattr(andy_run.Path, "home", lambda: tmp_path)
    route = SimpleNamespace(tool=Tool.CODEX, tier=Tier.MID)
    assert andy_run.reviewers_for(route)[0][0] is Tool.CLAUDE
    (tmp_path / ".andy_sync").mkdir()
    (tmp_path / ".andy_sync" / "save_claude").touch()
    assert andy_run.reviewers_for(route) == [
        (Tool.AGY, "gemini-3.8-flash-high"),
        (Tool.CODEX, "gpt-6-sol"),
        (Tool.CLAUDE, "sonnet"),
    ]


def test_save_claude_codex_fallback_runs_separately_read_only(harness):
    state, calls, route, notion, spec, execute, report = harness
    switch = Path.home() / ".andy_sync" / "save_claude"
    switch.parent.mkdir(parents=True)
    switch.touch()
    state["reviewer_down"] = {"agy"}

    assert execute() == 0
    assert report()["rounds"][0]["reviewer"] == "codex/gpt-6-sol"
    agents = [(command, kwargs) for command, kwargs in calls
              if isinstance(command, list) and command[0] != "git"]
    assert [Path(command[0]).stem for command, _ in agents] == ["codex", "agy", "codex"]
    assert "workspace-write" in agents[0][0]
    assert "read-only" in agents[2][0]
    assert agents[0][1]["input"] != agents[2][1]["input"]


def test_codex_builder_retries_capacity_and_records_used_model(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["capacity"] = {"gpt-6-sol": 1, "gpt-6-astra": 1}

    assert execute() == 0
    builders = [command[3] for command, _ in calls if isinstance(command, list)
                and Path(command[0]).stem == "codex"]
    assert builders == ["gpt-6-sol", "gpt-6-astra", "gpt-5.6-terra"]
    assert report()["route"]["model"] == "gpt-6-sol"
    assert report()["rounds"][0]["builder_model"] == "gpt-5.6-terra"


def test_codex_reviewer_retries_capacity_and_records_used_model(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["missing_tools"] = {"agy"}
    route.return_value = replace(route.return_value, tool=Tool.CLAUDE)
    state["capacity"] = {"gpt-6-sol": 1, "gpt-6-astra": 1}

    assert execute() == 0
    reviews = [command for command, _ in calls if isinstance(command, list)
               and Path(command[0]).stem == "codex"]
    assert [command[3] for command in reviews] == ["gpt-6-sol", "gpt-6-astra", "gpt-5.6-terra"]
    assert all("read-only" in command for command in reviews)
    assert report()["rounds"][0]["reviewer"] == "codex/gpt-5.6-terra"


def test_codex_capacity_retry_starts_at_selected_model_and_stops_on_other_errors():
    calls = []

    def runner(command, **kwargs):
        calls.append(command[3])
        message = "Selected model is at capacity" if command[3] == "gpt-6-luna" else "Authentication failed"
        return subprocess.CompletedProcess(command, 1, "", message)

    result, model = andy_run.call_agent(Tool.CODEX, "gpt-6-luna", Path("wt"), "task", runner,
                                        lambda _: "codex.cmd", required=False)
    assert (result.returncode, model) == (1, "gpt-6-sol")
    assert calls == ["gpt-6-luna", "gpt-6-sol"]


def test_codex_capacity_retry_ends_after_terra():
    calls = []

    def runner(command, **kwargs):
        calls.append(command[3])
        return subprocess.CompletedProcess(command, 1, "Selected model is at capacity", "")

    result, model = andy_run.call_agent(Tool.CODEX, "gpt-6-luna", Path("wt"), "task", runner,
                                        lambda _: "codex.cmd", required=False)
    assert result.returncode == 1
    assert model == "gpt-5.6-terra"
    assert calls == list(andy_run.CODEX_FALLBACK)


def test_save_claude_uses_claude_only_after_agy_and_codex_fail(harness):
    state, calls, route, notion, spec, execute, report = harness
    switch = Path.home() / ".andy_sync" / "save_claude"
    switch.parent.mkdir(parents=True)
    switch.touch()
    state["reviewer_down"] = {"agy", "codex"}

    assert execute() == 0
    assert report()["rounds"][0]["reviewer"] == "claude/sonnet"
    agents = [Path(command[0]).stem for command, _ in calls
              if isinstance(command, list) and command[0] != "git"]
    assert agents == ["codex", "agy", "codex", "claude"]


def test_save_claude_switch_turns_itself_off_after_the_reset(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(andy_run.Path, "home", lambda: tmp_path)
    (tmp_path / ".andy_sync").mkdir()
    (tmp_path / ".andy_sync" / "save_claude").write_text("2026-10-06T02:59:59Z")
    assert andy_run.save_claude(datetime(2026, 10, 5, 12, tzinfo=timezone.utc)) is True
    assert andy_run.save_claude(datetime(2026, 10, 6, 3, tzinfo=timezone.utc)) is False


def test_fenced_json_review_is_parsed():
    fenced = '```json\n{"pass": true, "findings": []}\n```'
    review = andy_run.parse_review(subprocess.CompletedProcess([], 0, fenced, ""))
    assert review == {"pass": True, "findings": []}


def test_claude_builder_preapproves_edits_but_reviewer_does_not():
    which = lambda name: f"C:/bin/{name}.cmd"
    builder = andy_run.agent_command(Tool.CLAUDE, "sonnet", Path("wt"), which=which)
    reviewer = andy_run.agent_command(Tool.CLAUDE, "sonnet", Path("wt"), review=True, which=which)
    assert builder[-2:] == ["--permission-mode", "acceptEdits"]
    assert "--permission-mode" not in reviewer


def test_agy_gets_prompt_as_argument_and_refuses_to_truncate():
    which = lambda name: f"C:/bin/{name}.exe"
    command = andy_run.agent_command(Tool.AGY, "gemini-3.8-flash-high", Path("wt"), which=which, prompt="review me")
    assert command[1:] == ["--model", "gemini-3.8-flash-high", "-p", "review me"]
    with pytest.raises(ValueError, match="too long"):
        andy_run.agent_command(Tool.AGY, "gemini-3.8-flash-high", Path("wt"), which=which, prompt="x" * 30000)
    assert andy_run.stdin_for(Tool.AGY, "prompt") is None
    assert andy_run.stdin_for(Tool.CODEX, "prompt") == "prompt"


def test_logged_out_claude_reviewer_falls_back_to_agy(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["reviewer_down"] = {"claude"}
    assert execute() == 0
    assert report()["rounds"][0]["reviewer"] == "agy/gemini-3.8-flash-high"


def test_no_reviewer_available_stops_for_dawn_after_one_round(harness):
    state, calls, route, notion, spec, execute, report = harness
    state["reviewer_down"] = {"claude", "agy"}
    assert execute() == 3
    data = report()
    assert data["outcome"] == "stopped-for-dawn"
    assert "No reviewer available" in data["reasons"][0]
    assert len(data["rounds"]) == 1 and data["rounds"][0]["commit"] == "abc123"
    assert data["rounds"][0]["check"]["pass"] is True
    assert not git_calls(calls, "merge")
    assert len([c for c, _ in calls if isinstance(c, list) and Path(c[0]).stem == "codex"]) == 1
