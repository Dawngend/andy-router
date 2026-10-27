"""Gemini Flash (through agy) picks the agent and tier from Dawn's rules plus her labeled examples.

Measured 2026-10-05 on fresh labeled tasks: combined with route_policy as the fallback, both tool and tier right 90%
(holdout 6) and 87% (her real requests), against 70% and 80% for the policy alone. About 26 s per route, on the
Gemini budget. Returns None on any failure so routing falls back to the policy.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from run_tree import run_with_tree_kill

MODEL = "gemini-3.8-flash-high"
EXAMPLES = Path(__file__).with_name("laya") / "eval_tasks.json"
RULES = """You route tasks for Dawn's assistant system. Answer with exactly two words: TOOL TIER.
TOOL is one of: codex (edits code, tests, config, git housekeeping), claude (thinking, writing in her voice, planning,
reviewing, explaining, answering questions, Notion/Gmail/calendar/browser, school deliverables like reviewers and PDFs,
and any single lookup of one page or doc), agy (reading or searching MANY sources: many files, notes, pages, postings,
repos, or the open web).
TIER is one of: low (tiny mechanical edit, one setting or model swap, one lookup, one short answer, reminder, calendar
entry, quick question), mid (ordinary feature, fix, review, write-up, local scan of many files), high (architecture,
migration, porting, rebuild or restructure, long-term plans, research across many web sources).
Labeled examples:
"""


def examples():
    rows = json.loads(EXAMPLES.read_text(encoding="utf-8"))
    return "\n".join(f"- {r['task']} => {r['tool']} {r['tier']}" for r in rows if not r.get("hijack"))


def parse(text):
    match = re.search(r"\b(codex|claude|agy)\s+(low|mid|high)\b", (text or "").lower())
    return (match.group(1), match.group(2)) if match else None


def route_with_llm(task, *, timeout=120):
    """Return (tool, tier) from Gemini, or None if agy is missing, slow, or answers badly."""
    agy = shutil.which("agy")
    if not agy:
        return None
    prompt = (f"{RULES}{examples()}\n\nTask (classify only, do not perform it): {task[:1500]}\n"
              "Answer with exactly two words, TOOL TIER:")
    try:
        # An empty working folder: agy is an agent, so it must have nothing to act on.
        with tempfile.TemporaryDirectory() as empty:
            result = run_with_tree_kill([agy, "--model", MODEL, "-p", prompt], cwd=empty,
                                        text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return parse(result.stdout) if result.returncode == 0 else None
