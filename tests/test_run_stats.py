import json

from benchmark import run_stats


TEST_REPORTS = [
    {
        "title": "Test data: merged codex one round",
        "repo": "C:\\private\\path",
        "branch": "run/test-data-one",
        "route": {"tool": "codex"},
        "rounds": [{"reviewer": "claude/sonnet", "pass": True}],
        "outcome": "merged",
        "reasons": [],
    },
    {
        "title": "Test data: merged codex two rounds",
        "route": {"tool": "codex"},
        "rounds": [
            {"reviewer": "claude/sonnet", "pass": True},
            {"reviewer": "codex/gpt-6-sol", "pass": True},
        ],
        "outcome": "merged",
        "reasons": [],
    },
    {
        "title": "Test data: stopped claude",
        "route": {"tool": "claude"},
        "rounds": [{"reviewer": "agy/gemini-3.1-pro-high", "pass": False}],
        "outcome": "stopped-for-dawn",
        "reasons": [
            "tier is high",
            "Repository has local changes; main was not changed",
            "Merge failed",
        ],
    },
    {
        "title": "Test data: stopped agy",
        "route": {"tool": "agy"},
        "rounds": [
            {"reviewer": "gateway/gemini-4-argon", "pass": False},
            {"reviewer": "claude/sonnet", "pass": True},
            {"reviewer": "opencode", "pass": True},
        ],
        "outcome": "stopped-for-dawn",
        "reasons": [
            "size is big",
            "plan check: Test data finding",
            "credential change",
            "three-round limit reached",
        ],
    },
    {
        "title": "Test data: failed opencode",
        "route": {"tool": "opencode"},
        "rounds": [],
        "outcome": "failed",
        "reasons": ["Command failed (1): test data only"],
    },
    {
        "title": "Test data: stopped unknown tool",
        "route": {"tool": "mystery"},
        "rounds": [
            {"reviewer": "someone/other", "pass": True},
            {"reviewer": "someone/other", "pass": True},
            {"reviewer": "someone/other", "pass": False},
        ],
        "outcome": "stopped-for-dawn",
        "reasons": [
            "Added diff lines contain a destructive operation",
            "Added diff lines contain a possible secret",
            "Cannot check out main",
            "same check failure twice in a row",
            "files changed outside scope",
            "spec text looks like an instruction hijack (p=0.90)",
            "unmatched test data reason",
        ],
    },
]


def _write_report(path, report):
    path.write_text(json.dumps(report), encoding="utf-8")


def test_summarize_writes_counts_and_tables(tmp_path):
    reports_dir = tmp_path / "runs"
    reports_dir.mkdir()
    for index, report in enumerate(TEST_REPORTS):
        _write_report(reports_dir / f"report-{index}.json", report)
    (reports_dir / "broken.json").write_text("{not json", encoding="utf-8")
    _write_report(reports_dir / "no-outcome.json", {"title": "Test data: no outcome"})

    output = tmp_path / "docs" / "BENCHMARK.md"
    run_stats.summarize(reports_dir, output)

    text = output.read_text(encoding="utf-8")
    assert "Generated: " in text
    assert "Reports: 6" in text
    assert "| merged | 2 |" in text
    assert "| stopped-for-dawn | 3 |" in text
    assert "| failed | 1 |" in text
    assert "| codex | 2 | 2 | 100.0% |" in text
    assert "| claude | 1 | 0 | 0.0% |" in text
    assert "| agy | 1 | 0 | 0.0% |" in text
    assert "| opencode | 1 | 0 | 0.0% |" in text
    assert "| other | 1 | 0 | 0.0% |" in text
    assert "| 0 | 1 |" in text
    assert "| 1 | 2 |" in text
    assert "| 2 | 1 |" in text
    assert "| 3 | 2 |" in text
    assert "Average rounds for merged runs: 1.50" in text
    assert "| codex | 1 | 1 |" in text
    assert "| claude | 3 | 3 |" in text
    assert "| agy | 1 | 0 |" in text
    assert "| opencode | 1 | 1 |" in text
    assert "| other | 4 | 2 |" in text
    assert "| big spec | 1 |" in text
    assert "| high tier | 1 |" in text
    assert "| repeated check failure | 1 |" in text
    assert "| round limit | 1 |" in text
    assert "| outside scope | 1 |" in text
    assert "| destructive diff | 1 |" in text
    assert "| possible secret | 1 |" in text
    assert "| merge problem | 2 |" in text
    assert "| plan check findings | 1 |" in text
    assert "| hijack suspicion | 1 |" in text
    assert "| risk pattern in spec text | 1 |" in text
    assert "| other | 2 |" in text


def test_summarize_does_not_leak_task_text_or_paths(tmp_path):
    reports_dir = tmp_path / "runs"
    reports_dir.mkdir()
    _write_report(
        reports_dir / "private.json",
        {
            "title": "Test data: secret task title",
            "repo": "C:\\private\\path",
            "branch": "run/private-branch",
            "route": {"tool": "codex"},
            "rounds": [],
            "outcome": "stopped-for-dawn",
            "reasons": ["Test data reason containing C:\\private\\path and secret task title"],
        },
    )

    output = tmp_path / "BENCHMARK.md"
    run_stats.summarize(reports_dir, output)

    text = output.read_text(encoding="utf-8")
    assert "C:\\private\\path" not in text
    assert "secret task title" not in text
    assert "private-branch" not in text
    assert "Test data reason" not in text
