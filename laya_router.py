"""Optional Laya decision for tasks with no clear rule match."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class LayaDecision:
    tool: str
    tool_p: float
    tier_score: float
    tool_confidence: float | None = None
    hijack: float | None = None
    tool_probabilities: dict | None = None
    mechanical: float | None = None
    big: float | None = None


def ask_laya(task: str, questions=None) -> LayaDecision | None:
    script = Path(__file__).with_name("laya") / "decide.mjs"
    try:
        completed = subprocess.run(
            ["node", str(script)],
            input=json.dumps({"task": task[:1500]}),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "LAYA_QUESTIONS": str(questions)} if questions is not None else None,
        )
        if completed.returncode != 0:
            return None
        result = json.loads(completed.stdout)
        tool = result["tool"]
        probability = result["tool_p"]
        score = result["tier_score"]
        confidence = result.get("tool_confidence")
        hijack = result.get("hijack")
        mechanical = result.get("mechanical")
        big = result.get("big")
        probabilities = result.get("tool_probabilities")
        if probabilities is not None and (
            not isinstance(probabilities, dict) or set(probabilities) != {"codex", "claude", "agy"}
            or any(type(v) not in {int, float} or not math.isfinite(v) for v in probabilities.values())
        ):
            probabilities = None
        if (
            tool not in {"codex", "claude", "agy"}
            or type(probability) not in {int, float}
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
            or type(score) not in {int, float}
            or not math.isfinite(score)
            or any(value is not None and (type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1)
                   for value in (confidence, hijack, mechanical, big))
        ):
            return None
        return LayaDecision(tool, float(probability), float(score),
                            None if confidence is None else float(confidence),
                            None if hijack is None else float(hijack),
                            None if probabilities is None else {k: float(v) for k, v in probabilities.items()},
                            None if mechanical is None else float(mechanical),
                            None if big is None else float(big))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, OverflowError):
        return None
