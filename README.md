# Andy Router

A small router and runner for coding agents. It reads a task, picks which agent should do it (Codex, Claude or Gemini through `agy`) and how much reasoning it needs, builds the change on its own git branch, has a different model family review it, and merges only when it is safe.

This is the agent part of my personal assistant system, Andy. I use it every day on my own repos. The version here is a copy of the routing and run code with my private integrations (Notion reporting, vault sync, my labelled task sets) removed.

## What It Does

1. **Route.** `tier_router.assess_task` reads the task text and returns a tool, a tier (low, mid, high), a model and the reasons. Word rules decide first. For vague tasks it asks Laya, a small scoring model run through Node, and Gemini Flash makes the final call on tool and tier unless the text looks like a hijack. If Laya or Gemini are not available the word rules still decide.
2. **Branch.** `andy_run.py` creates a git worktree next to the repo on a new branch `run/<slug>`. It never builds on main.
3. **Build.** The chosen CLI runs in the worktree with a fixed rule: work only here, do not push or merge, keep the code simple.
4. **Check.** It commits the result itself, then runs the spec's check command (usually the test suite) and keeps the last 20 lines.
5. **Review.** A reviewer from a different model family reads the spec, the diff and the check result, and answers in JSON: `{"pass": true, "findings": [{"severity": "high|low", "text": "...", "fix": "..."}]}`. Invalid JSON counts as a failed review and falls through to the next reviewer.
6. **Two-way loop.** If the round fails, the findings go back to the builder with the instruction to answer each one with AGREE or DISAGREE and a reason, then fix only what it agrees with. Up to three rounds. If the same check failure shows up twice in a row, it stops early and asks a second model for a root-cause diagnosis.
7. **Decide.** A small, passing, low-risk run is merged into main, the check runs again on main, and only then is it pushed. Anything else stops for a human.

## When It Stops Instead of Merging

The run ends as `stopped-for-dawn` (exit code 3) when any of these is true:

- the spec says `size: big`, or the routed tier is high
- the spec text matches a risk pattern: credentials, destructive operations, production, database migration, security bypass, costly API use
- the added diff lines contain a destructive call (`rmtree`, `git push --force`, `DROP TABLE` and similar) or something that looks like a secret
- the spec text looks like an instruction hijack (Laya's hijack score is 0.8 or higher)
- the three-round limit is reached, or the same check failure repeats
- files changed outside the spec's `scope`
- the repo has local changes, the merge conflicts, or the post-merge check fails
- no reviewer CLI is available

A big spec also gets a plan review before anything is built. Exit code 0 means merged and 1 means failed. Every run writes a JSON report to `~/.andy_sync/runs/` with the route and reasons, each round's findings, the builder's AGREE or DISAGREE answers, the check results and the commit ids.

## Spec Format

```
---
title: Fix greeting
repo: ../my-repo
check: python -m pytest -q
size: small
---
Make greet() return "Hello, <name>!" and add a test.
```

Optional header fields are `tool` (codex, claude, agy, opencode), `tier` (low, mid, high), `effort` and `scope` (comma-separated path prefixes the builder may change). If a spec names both tool and tier, routing is skipped. See `examples/example_spec.md`.

## Run It

Needs Python 3.10 or newer, git, and at least the `codex` CLI. `claude` and `agy` are used as builders or reviewers when installed.

```
python andy_run.py examples/example_spec.md --dry-run   # shows the route and the commands, changes nothing
python andy_run.py examples/example_spec.md             # the real run
```

Laya is optional. To turn it on, run `npm install` inside `laya/` (it uses `@receptron/laya`).

## Switches

- **OpenCode builder.** `tool: opencode` in a spec header runs a free model. Routing never picks it by itself. Set `ANDY_NO_OPENCODE_REPOS` to a list of paths it must not build in.
- **Builder mode.** A small file forces every build onto one model family until a set time, and reviewers skip the family being saved.
- **Save-Claude.** A small file holds a time. Until then Claude is kept for chat and planning, so builds go to Codex and reviews to Gemini.
- **Tool cooldown.** A builder that hits a spend cap or usage limit is benched for five hours and the run falls back to another tool.

The files live in `~/.andy_sync`, or in the folder named by `ANDY_STATE_DIR`. Details are in `docs/FEATURES.md`, and what was left out of this copy is in `docs/NOT_INCLUDED.md`.

## Results

These numbers come from my own run reports, 166 runs in October 2026, counted by `benchmark/run_stats.py`. Full tables are in `docs/BENCHMARK.md`.

| Outcome | Runs |
| --- | --- |
| Merged | 50 |
| Stopped for a human | 99 |
| Failed | 17 |

Merge rate by builder: Codex 36.8% of 95 runs, Claude 28.3% of 46, OpenCode 14.3% of 14. Agy was used for 11 runs and none merged. These rates mix spec sizes, and big or high-tier specs always stop for a human, so they say more about how I use the router than about how good each tool is. Regenerate the tables from your own reports with `python benchmark/run_stats.py`.

## Tests

```
pip install -r requirements.txt
python -m pytest tests -q
```

The 200+ tests mock every CLI, git call and network call, so they run offline. They cover the routing rules, reviewer selection (never the builder's own family), each stop condition, a failed check overriding a passing review, bad reviewer JSON, the round limit, the AGREE or DISAGREE instruction, model fallback when a Codex model is at capacity, and `--dry-run` changing nothing.

## Files

| File | Job |
| --- | --- |
| `andy_run.py` | The run: branch, build, check, review, loop, merge or stop, report |
| `tier_router.py` | Turns a task into a tool, tier and model with reasons, and flags risky wording |
| `route_policy.py` | Word signals combined with Laya's answers; the measured policy |
| `llm_router.py` | Asks Gemini Flash for a tool and tier using labelled examples |
| `laya_router.py`, `laya/` | Calls Laya through Node and validates its output |
| `secret_scan.py` | Flags lines that look like keys or tokens in a diff |
| `tool_cooldown.py` | Benches a builder tool after a spend-cap or usage-limit failure |
| `run_tree.py` | Kills the whole process tree when a call times out |
| `benchmark/run_stats.py` | Turns the run reports into the aggregate tables in `docs/BENCHMARK.md` |
| `laya/eval_router.py` | Scores the router on a labelled task set |

## Notes

- The accuracy numbers in the code comments (for example 92% against 7% for the old rules on 25 sealed tasks) were measured on my own labelled task sets. `laya/eval_tasks.json` here is a trimmed copy of the training examples. The holdout sets are not included, so those numbers cannot be reproduced from this repo.
- The outcome name `stopped-for-dawn` is left as it is in my reports. It means a human needs to look.
- Model names in `tier_router.py` are the ones I had access to when I wrote it. Change them to yours.
