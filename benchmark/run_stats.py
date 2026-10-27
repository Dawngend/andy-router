"""Turn router run reports into aggregate numbers for the README."""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andy_run import _HIGH_RISK_PATTERNS


FAMILIES = ("codex", "claude", "agy", "opencode", "other")
OUTCOMES = ("merged", "stopped-for-dawn", "failed")
ROUND_BUCKETS = (0, 1, 2, 3)
RISK_LABELS = {reason for _, reason in _HIGH_RISK_PATTERNS}
STOP_CATEGORIES = (
    "big spec",
    "high tier",
    "repeated check failure",
    "round limit",
    "outside scope",
    "destructive diff",
    "possible secret",
    "merge problem",
    "plan check findings",
    "hijack suspicion",
    "risk pattern in spec text",
    "other",
)
STOP_PREFIXES = (
    ("size is big", "big spec"),
    ("tier is high", "high tier"),
    ("same check failure twice", "repeated check failure"),
    ("three-round limit", "round limit"),
    ("files changed outside scope", "outside scope"),
    ("Added diff lines contain a destructive", "destructive diff"),
    ("Added diff lines contain a possible secret", "possible secret"),
    ("Repository has local changes", "merge problem"),
    ("Cannot check out main", "merge problem"),
    ("Pull failed", "merge problem"),
    ("Merge failed", "merge problem"),
    ("Post-merge check failed", "merge problem"),
    ("plan check:", "plan check findings"),
    ("spec text looks like an instruction hijack", "hijack suspicion"),
)


def _load_reports(reports_dir):
    reports = []
    for path in sorted(Path(reports_dir).glob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and "outcome" in report:
            reports.append(report)
    return reports


def _family(value):
    if not isinstance(value, str):
        return "other"
    family = value.split("/", 1)[0].strip().lower()
    return family if family in FAMILIES else "other"


def _rounds(report):
    rounds = report.get("rounds")
    return rounds if isinstance(rounds, list) else []


def _outcome_counts(reports):
    counts = Counter(report.get("outcome") for report in reports)
    return [(outcome, counts.get(outcome, 0)) for outcome in OUTCOMES]


def _tool_stats(reports):
    runs = Counter()
    merged = Counter()
    for report in reports:
        route = report.get("route")
        tool = _family(route.get("tool")) if isinstance(route, dict) else "other"
        runs[tool] += 1
        if report.get("outcome") == "merged":
            merged[tool] += 1
    return [(tool, runs.get(tool, 0), merged.get(tool, 0)) for tool in FAMILIES]


def _round_counts(reports):
    counts = Counter(len(_rounds(report)) for report in reports)
    return [(bucket, counts.get(bucket, 0)) for bucket in ROUND_BUCKETS]


def _average_rounds_for_merged(reports):
    merged = [report for report in reports if report.get("outcome") == "merged"]
    if not merged:
        return 0.0
    return sum(len(_rounds(report)) for report in merged) / len(merged)


def _reviewer_stats(reports):
    reviewed = Counter()
    passed = Counter()
    for report in reports:
        for round_ in _rounds(report):
            if not isinstance(round_, dict):
                continue
            family = _family(round_.get("reviewer"))
            reviewed[family] += 1
            if round_.get("pass"):
                passed[family] += 1
    return [(family, reviewed.get(family, 0), passed.get(family, 0)) for family in FAMILIES]


def _stop_category(reason):
    if not isinstance(reason, str):
        return "other"
    for prefix, category in STOP_PREFIXES:
        if reason.startswith(prefix):
            return category
    if reason in RISK_LABELS:
        return "risk pattern in spec text"
    return "other"


def _stop_counts(reports):
    counts = Counter()
    for report in reports:
        reasons = report.get("reasons")
        if not isinstance(reasons, list):
            continue
        counts.update({_stop_category(reason) for reason in reasons})
    return [(category, counts.get(category, 0)) for category in STOP_CATEGORIES]


def _merge_rate(merged, runs):
    if not runs:
        return "0.0%"
    return f"{100 * merged / runs:.1f}%"


def _table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _render(reports, generated):
    tool_rows = [
        (tool, runs, merged, _merge_rate(merged, runs))
        for tool, runs, merged in _tool_stats(reports)
    ]
    lines = [
        "# Router Benchmark",
        "",
        f"Generated: {generated}",
        "",
        f"Reports: {len(reports)}",
        "",
        "## Outcomes",
        "",
        _table(("Outcome", "Runs"), _outcome_counts(reports)),
        "",
        "## Runs Per Tool",
        "",
        _table(("Tool", "Runs", "Merged", "Merge rate"), tool_rows),
        "",
        "## Review Rounds Per Run",
        "",
        _table(("Rounds", "Runs"), _round_counts(reports)),
        "",
        f"Average rounds for merged runs: {_average_rounds_for_merged(reports):.2f}",
        "",
        "## Reviews Per Reviewer Family",
        "",
        _table(("Reviewer", "Rounds reviewed", "Rounds passed"), _reviewer_stats(reports)),
        "",
        "## Stop Reasons",
        "",
        _table(("Reason", "Runs"), _stop_counts(reports)),
        "",
    ]
    return "\n".join(lines)


def summarize(reports_dir, output_path):
    reports = _load_reports(reports_dir)
    text = _render(reports, date.today().isoformat())
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write aggregate router benchmark stats.")
    parser.add_argument(
        "reports_dir",
        nargs="?",
        type=Path,
        default=Path.home() / ".andy_sync" / "runs",
    )
    args = parser.parse_args(argv)
    summarize(args.reports_dir, Path("docs") / "BENCHMARK.md")


if __name__ == "__main__":
    main()
