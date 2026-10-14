"""End-to-end router accuracy (the real assess_task with Laya) on labeled task sets.

Usage: python omni-router/laya/eval_router.py omni-router/laya/eval_tasks.json omni-router/laya/eval_holdout.json ...
Add --misses to list wrong routes. Run on every set before changing route_policy.py or questions.json.
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tier_router import assess_task


def score(path, show_misses):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    tool_ok = tier_ok = both = 0
    for row in rows:
        decision = assess_task(row["task"])
        tool, tier = decision.tool.value, decision.tier.value
        tool_ok += tool == row["tool"]
        tier_ok += tier == row["tier"]
        both += tool == row["tool"] and tier == row["tier"]
        if show_misses and (tool, tier) != (row["tool"], row["tier"]):
            print(f"  want {row['tool']}/{row['tier']} got {tool}/{tier} :: {row['task'][:65]}")
    n = len(rows)
    print(f"{Path(path).name}: tool {tool_ok / n:.0%}  tier {tier_ok / n:.0%}  both {both / n:.0%}  (n={n})")


if __name__ == "__main__":
    paths = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    for path in paths:
        score(path, "--misses" in sys.argv)
