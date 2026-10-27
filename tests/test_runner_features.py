from dataclasses import replace
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import andy_run
import run_tree
import tool_cooldown
from tier_router import Tier, Tool, assess_task, select_model


NOW = datetime(2026, 10, 27, 12)


def route(tool):
    return replace(assess_task("fix greeting", use_laya=False), tool=tool)


def test_opencode_prompt_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(andy_run.tempfile, "tempdir", str(tmp_path))
    model = select_model(Tool.OPENCODE, Tier.HIGH).model
    assert model == "opencode/deepseek-v4.1-flash-free"
    paths = []

    def runner(command, **kwargs):
        path = Path(command[command.index("-f") + 1])
        paths.append(path)
        assert path.read_text(encoding="utf-8") == "Build\nthis task"
        assert kwargs["input"] == ""
        assert "--auto" in command and "--effort" not in command
        raise RuntimeError("usage limit")

    with pytest.raises(RuntimeError, match="usage limit"):
        andy_run.call_agent(Tool.OPENCODE, model, tmp_path, "Build\nthis task", runner,
                            lambda tool: tool, effort="high")
    assert not paths[0].exists()
    assert [tool for tool, _ in andy_run.reviewers_for(route(Tool.OPENCODE))] == [Tool.AGY, Tool.CLAUDE]


def test_opencode_blocked_roots_include_children_not_siblings(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    monkeypatch.setenv("ANDY_NO_OPENCODE_REPOS", os.pathsep.join([str(blocked), str(tmp_path / "other")]))
    spec = tmp_path / "task.md"
    for repo, denied in [(blocked, True), (blocked / "child", True), (tmp_path / "blocked-copy", False)]:
        spec.write_text(f"---\ntitle: Task\nrepo: {repo}\ncheck: echo ok\ntool: opencode\n---\nBuild")
        if denied:
            with pytest.raises(ValueError, match="blocked"):
                andy_run.read_spec(spec)
        else:
            assert andy_run.read_spec(spec)[0]["tool"] == "opencode"


@pytest.mark.parametrize("family,tool,saved", [("codex", Tool.CODEX, Tool.CLAUDE),
                                              ("claude", Tool.CLAUDE, Tool.CODEX),
                                              ("gemini", Tool.AGY, Tool.CODEX)])
def test_builder_mode_expiry_review_and_diagnosis(tmp_path, monkeypatch, family, tool, saved):
    state = andy_run.state_dir()
    state.mkdir()
    switch = state / "builder_mode"
    switch.write_text(f"{family}\n{(NOW + timedelta(hours=1)).isoformat()}")
    assert andy_run.builder_mode(NOW) is tool
    assert andy_run.builder_mode(NOW + timedelta(hours=1)) is None
    switch.write_text(family)
    assert all(reviewer is not saved for reviewer, _ in andy_run.reviewers_for(route(Tool.CODEX)))
    agent = Mock(return_value=(SimpleNamespace(returncode=0, stdout="Diagnosis"), "model"))
    monkeypatch.setattr(andy_run, "call_agent", agent)
    assert andy_run.diagnose_stuck(route(Tool.CODEX), tmp_path, "spec", "diff", "old", "new", None, None) == "Diagnosis"
    assert agent.call_args.args[0] is not saved


def test_save_claude_first_line_bom_and_bad_encoding(tmp_path, capsys):
    state = andy_run.state_dir()
    state.mkdir()
    switch = state / "save_claude"
    assert not andy_run.save_claude(NOW)
    switch.write_text("\ufeff2026-10-27T13:00:00\nnotes", encoding="utf-8")
    assert andy_run.save_claude(NOW)
    assert not andy_run.save_claude(NOW + timedelta(hours=1))
    switch.write_text("")
    assert andy_run.save_claude(NOW)
    switch.write_text("enabled", encoding="utf-16")
    assert not andy_run.save_claude(NOW)
    assert not andy_run.save_claude(NOW)
    assert capsys.readouterr().out.count("not UTF-8") == 1


def test_cooldown_records_builder_failure_and_expires(tmp_path):
    path = andy_run.state_dir() / "tool_cooldown.json"
    runner = Mock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="Usage limit"))
    with pytest.raises(RuntimeError, match="Usage limit"):
        andy_run.build_round(1, "prompt", "spec", {}, route(Tool.CODEX), tmp_path, "branch",
                             runner, lambda tool: tool, clock=lambda: NOW, cooldown_path=path)
    active = tool_cooldown.load_cooldowns(path, NOW)
    assert active == {"codex": NOW + timedelta(hours=5)}
    assert andy_run.cooldown_fallback(route(Tool.CODEX), active).tool is Tool.CLAUDE
    assert andy_run.cooldown_fallback(route(Tool.CODEX), active, exclude=Tool.CLAUDE).tool is Tool.AGY
    assert tool_cooldown.load_cooldowns(path, NOW + timedelta(hours=5)) == {}
    assert path.read_text().strip() == "{}"


def test_private_claude_review_order():
    assert andy_run.reviewers_for(route(Tool.CLAUDE))[0][0] is Tool.AGY
    assert andy_run.reviewers_for(route(Tool.CLAUDE), plan_check=True)[0][0] is Tool.CODEX


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_timeout_kills_and_drains_process(monkeypatch, platform):
    process = Mock(pid=123, returncode=1)
    process.__enter__ = Mock(return_value=process)
    process.__exit__ = Mock(return_value=False)
    process.communicate.side_effect = [subprocess.TimeoutExpired("agent", 2), ("", "")]
    popen = Mock(return_value=process)
    taskkill = Mock()
    monkeypatch.setattr(run_tree, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(run_tree.subprocess, "Popen", popen)
    monkeypatch.setattr(run_tree.subprocess, "run", taskkill)
    with pytest.raises(subprocess.TimeoutExpired):
        run_tree.run_with_tree_kill(["agent"], timeout=2, input="task", text=True)
    if platform == "nt":
        taskkill.assert_called_once_with(["taskkill", "/T", "/F", "/PID", "123"], capture_output=True, check=False)
    else:
        process.kill.assert_called_once()
    assert process.communicate.call_count == 2
