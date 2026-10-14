"""Deterministic task routing and state/risk checks for Andy Router."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
from typing import Iterable, Sequence

from laya_router import ask_laya
import llm_router
import route_policy


class Tier(str, Enum):
    """Logical cost/complexity tiers, independent of provider model names."""

    LOW = "low"
    MID = "mid"
    HIGH = "high"


class Tool(str, Enum):
    """Installed assistant CLIs, independent of complexity tiers."""

    AGY = "agy"
    CODEX = "codex"
    CLAUDE = "claude"


class TaskType(str, Enum):
    """Capability families documented in AI_Assistant_Workflow.md."""

    AGENTIC_ENGINEERING = "agentic-engineering"
    REPOSITORY_ENGINEERING = "repository-engineering"
    KNOWLEDGE_WORK = "knowledge-work"
    GENERAL = "general"


@dataclass(frozen=True)
class TaskClassification:
    task_type: TaskType
    scores: tuple[tuple[TaskType, int], ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ModelSelection:
    model: str
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    tier: Tier
    tool: Tool
    model: str
    task_type: TaskType
    complexity_score: int
    reasons: tuple[str, ...]
    high_risk: bool
    risk_reasons: tuple[str, ...]


@dataclass(frozen=True)
class StateCheckResult:
    already_completed: bool
    evidence: tuple[Path, ...] = ()


_LOW_TERMS = {
    "format",
    "rename",
    "typo",
    "comment",
    "summarize",
    "explain",
    "list",
}
_MEDIUM_TERMS = {
    "fix",
    "bug",
    "debug",
    "test",
    "implement",
    "refactor",
    "endpoint",
    "auth",
    "database",
    "api",
}
_HIGH_TERMS = {
    "architecture",
    "architect",
    "migration",
    "distributed",
    "security",
    "production",
    "redesign",
    "multi-agent",
    "multiagent",
}
_TASK_TYPE_TERMS: dict[TaskType, set[str]] = {
    TaskType.AGENTIC_ENGINEERING: {
        "agentic",
        "architecture",
        "architect",
        "codebase",
        "empirical",
        "execute",
        "execution",
        "integration",
        "orchestrate",
        "prototype",
        "synthesis",
        "system",
    },
    TaskType.REPOSITORY_ENGINEERING: {
        "automate",
        "automation",
        "bug",
        "debug",
        "debugging",
        "fix",
        "implement",
        "implementation",
        "lint",
        "pytest",
        "refactor",
        "regression",
        "repository",
        "test",
        "testing",
        "verification",
        "verify",
    },
    TaskType.KNOWLEDGE_WORK: {
        "context",
        "curate",
        "curation",
        "frontmatter",
        "markdown",
        "note",
        "notes",
        "obsidian",
        "organization",
        "organize",
        "vault",
        "wikilink",
        "writing",
    },
}
_TASK_TYPE_PHRASES: dict[TaskType, tuple[re.Pattern[str], ...]] = {
    TaskType.AGENTIC_ENGINEERING: (
        re.compile(r"\bpair[ -]programming\b", re.I),
        re.compile(r"\bsystem\s+architecture\b", re.I),
        re.compile(r"\bempirical\s+verification\b", re.I),
        re.compile(r"\bmulti[ -]step\b", re.I),
        re.compile(r"\bend[ -]to[ -]end\b", re.I),
    ),
    TaskType.REPOSITORY_ENGINEERING: (
        re.compile(r"\brepository\s+implementation\b", re.I),
        re.compile(r"\btechnical\s+verification\b", re.I),
        re.compile(r"\btest\s+suite\b", re.I),
    ),
    TaskType.KNOWLEDGE_WORK: (
        re.compile(r"\blong[ -]form\b", re.I),
        re.compile(r"\bnote\s+organization\b", re.I),
        re.compile(r"\bcontext\s+maintenance\b", re.I),
    ),
}
_TASK_TYPE_ORDER = (
    TaskType.AGENTIC_ENGINEERING,
    TaskType.REPOSITORY_ENGINEERING,
    TaskType.KNOWLEDGE_WORK,
)
_MODEL_LADDERS: dict[Tool, dict[Tier, str]] = {
    Tool.AGY: {
        # Dawn's call (2026-10-04): AGY always runs at high reasoning, since the
        # Gemini budget is separate and otherwise goes unused. 3.1 Pro is gone from
        # `agy models` (checked 2026-10-13), so Flash (High) takes every rung.
        Tier.LOW: "gemini-3.8-flash-high",
        Tier.MID: "gemini-3.8-flash-high",
        Tier.HIGH: "gemini-3.8-flash-high",
    },
    Tool.CODEX: {
        # GPT-6 generation (Dawn, 2026-10-04). Her Codex model list has Astra,
        # Sol and Luna 6 but no Terra 6, so Sol 6 (the workhorse) takes mid.
        Tier.LOW: "gpt-6-luna",
        Tier.MID: "gpt-6-sol",
        Tier.HIGH: "gpt-6-astra",
    },
    Tool.CLAUDE: {
        # Aliases resolve to the latest models: Opus 5.5, Sonnet 5.5, and
        # Haiku 4.5 (the newest Haiku). She has no Fable access, so the old
        # "fable" rung routed routine Claude tasks to a model she cannot call.
        Tier.LOW: "haiku",
        Tier.MID: "sonnet",
        Tier.HIGH: "opus",
    },
}
_HIGH_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(credentials?|passwords?|secrets?|api[_ -]?keys?|private[_ -]?keys?)\b", re.I), "credential change"),
    (re.compile(
        r"\b(delete|drop|truncate|destroy|purge|wipe|remove|erase|shred|clear)\b"
        r"(?:\s+[\w-]+,?){0,4}\s+"
        r"(?:rows?|tables?|databases?|files?|folders?|directories|directory|"
        r"branches?|records?|data|caches?|backups?|history|repos?|worktrees?)\b",
        re.I,
    ), "destructive operation"),
    (re.compile(r"\b(production|prod)\b", re.I), "production change"),
    (re.compile(r"\b(database|schema)\s+migration\b", re.I), "database migration"),
    (
        re.compile(r"\b(bypass|disable)\b(?:\W+\w+){0,3}?\W+(security|permissions?|approval)\b", re.I),
        "security bypass",
    ),
    (re.compile(r"\b(unlimited|large|heavy)\b.{0,24}\b(api|tokens?|budget|spend)\b", re.I), "potentially costly API usage"),
)
_COMPLETION_TERMS = re.compile(
    r"\b(completed|implemented|verified|resolved|fixed|shipped|already done)\b", re.I
)
_NEGATED_COMPLETION = re.compile(
    r"\b(not|never|isn't|isnt|wasn't|wasnt|hasn't|hasnt|haven't|havent|"
    r"not\syet|partially|attempted\sbut\snot|still\sneeds?\sto\sbe)"
    r"\b(?:\W+\w+){0,4}?\W+(completed|implemented|verified|resolved|fixed|shipped|done)\b",
    re.I,
)
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]+", re.I)
_IGNORED_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "is",
    "it",
    "of",
    "on",
    "please",
    "the",
    "this",
    "to",
    "with",
}
_TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml", ".json", ".txt"}
_SKIP_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "98_Source_Mirrors",
    "tests",
}
_TEST_ARTIFACT_PREFIX = ".test-tmp"


def parse_tier(value: str) -> Tier:
    normalized = value.strip().lower()
    legacy_aliases = {
        "luna": Tier.LOW,
        "terra": Tier.MID,
        "sol": Tier.HIGH,
    }
    if normalized in legacy_aliases:
        return legacy_aliases[normalized]
    try:
        return Tier(normalized)
    except ValueError as exc:
        choices = ", ".join(tier.value for tier in Tier)
        raise ValueError(f"Unknown tier {value!r}; expected one of: {choices}") from exc


def classify_task_type(prompt: str) -> TaskClassification:
    """Classify task capability with local rules and no model invocation."""

    words = {word.lower() for word in _WORD.findall(prompt)}
    score_map: dict[TaskType, int] = {}
    reasons: list[str] = []
    for task_type in _TASK_TYPE_ORDER:
        term_hits = sorted(words & _TASK_TYPE_TERMS[task_type])
        phrase_hits = [
            pattern.pattern
            for pattern in _TASK_TYPE_PHRASES[task_type]
            if pattern.search(prompt)
        ]
        score = len(term_hits) + (2 * len(phrase_hits))
        score_map[task_type] = score
        if score:
            evidence = term_hits + phrase_hits
            reasons.append(f"{task_type.value}: {', '.join(evidence)}")

    best_score = max(score_map.values(), default=0)
    if best_score == 0:
        task_type = TaskType.GENERAL
        reasons.append("no capability-specific terms")
    else:
        winners = [
            task_type
            for task_type in _TASK_TYPE_ORDER
            if score_map[task_type] == best_score
        ]
        task_type = winners[0] if len(winners) == 1 else TaskType.GENERAL
        if len(winners) > 1:
            reasons.append(
                "capability tie: " + ", ".join(winner.value for winner in winners)
            )

    return TaskClassification(
        task_type=task_type,
        scores=tuple((task_type, score_map[task_type]) for task_type in _TASK_TYPE_ORDER),
        reasons=tuple(reasons),
    )


def select_tool(classification: TaskClassification, tier: Tier) -> tuple[Tool, str]:
    """Choose by capability first, using complexity for ambiguous/general tasks."""

    direct = {
        TaskType.AGENTIC_ENGINEERING: Tool.AGY,
        TaskType.REPOSITORY_ENGINEERING: Tool.CODEX,
        TaskType.KNOWLEDGE_WORK: Tool.CLAUDE,
    }
    if classification.task_type in direct:
        tool = direct[classification.task_type]
        return tool, f"{classification.task_type.value} capability -> {tool.value}"

    fallback = {
        Tier.LOW: Tool.AGY,
        Tier.MID: Tool.CODEX,
        Tier.HIGH: Tool.CLAUDE,
    }
    tool = fallback[tier]
    return tool, f"ambiguous capability refined by {tier.value} complexity -> {tool.value}"


def select_model(tool: Tool, tier: Tier) -> ModelSelection:
    """Pick the least costly configured rung plausibly sufficient for the tier."""

    model = _MODEL_LADDERS[tool][tier]
    labels = {
        Tier.LOW: "routine complexity uses the cheapest configured model",
        Tier.MID: "implementation complexity warrants the balanced model",
        Tier.HIGH: "architecture complexity warrants the strongest model",
    }
    return ModelSelection(model=model, reason=labels[tier])


def assess_task(
    prompt: str, default_tier: Tier = Tier.MID, *, use_laya: bool = True
) -> RoutingDecision:
    """Combine independent complexity and capability rules into one decision."""

    words = {word.lower() for word in _WORD.findall(prompt)}
    score = 0
    reasons: list[str] = []

    high_hits = sorted(words & _HIGH_TERMS)
    medium_hits = sorted(words & _MEDIUM_TERMS)
    low_hits = sorted(words & _LOW_TERMS)
    if high_hits:
        score += min(8, 6 + 2 * (len(high_hits) - 1))
        reasons.append(f"architecture terms: {', '.join(high_hits)}")
    if medium_hits:
        score += min(5, 2 * len(medium_hits))
        reasons.append(f"implementation terms: {', '.join(medium_hits)}")
    if low_hits and not high_hits and not medium_hits:
        score -= 1
        reasons.append(f"routine terms: {', '.join(low_hits)}")
    if len(prompt) > 500:
        score += 3
        reasons.append("long prompt")
    elif len(prompt) > 200:
        score += 1
        reasons.append("multi-part prompt")

    if score >= 7:
        tier = Tier.HIGH
    elif score >= 2:
        tier = Tier.MID
    elif score <= -1:
        tier = Tier.LOW
    else:
        tier = default_tier
        reasons.append(f"default tier: {default_tier.value}")

    risk_reasons = tuple(
        reason for pattern, reason in _HIGH_RISK_PATTERNS if pattern.search(prompt)
    )
    classification = classify_task_type(prompt)
    tool, tool_reason = select_tool(classification, tier)
    laya = ask_laya(prompt) if use_laya else None
    if laya is not None and laya.tool_probabilities and laya.mechanical is not None and laya.big is not None:
        # Measured policy (route_policy.py): Laya's answers plus word signals beat these rules 92% to 7%
        # on sealed tasks, so it decides whenever Laya runs; the rules above are the fallback.
        name, tier_name, scores = route_policy.decide(prompt, laya.tool_probabilities, laya.mechanical, laya.big)
        tool, tier = Tool(name), Tier(tier_name)
        if f"default tier: {default_tier.value}" in reasons:
            reasons.remove(f"default tier: {default_tier.value}")
        shown = ", ".join(f"{key} {value:.2f}" for key, value in scores.items())
        tool_reason = (f"policy: {tool.value} ({shown}); tier {tier.value} "
                       f"(mechanical {laya.mechanical:.2f}, big {laya.big:.2f})")
        # Gemini reads intent; the policy counts words. Gemini decides unless the text looks like a hijack or
        # Gemini fails; explicit size words in the text still set the tier (measured best combination).
        hijack = laya.hijack if laya.hijack is not None else 0.0
        llm = llm_router.route_with_llm(prompt) if hijack < 0.8 else None
        if llm is not None:
            # Size words only decide in short requests; in a long spec "rename" or "migrate" are details.
            explicit_tier = len(prompt.split()) <= 40 and (route_policy.BIG.search(prompt)
                                                           or route_policy.MECH.search(prompt))
            tool, tier = Tool(llm[0]), (tier if explicit_tier else Tier(llm[1]))
            tool_reason = f"gemini: {tool.value} {llm[1]}; final tier {tier.value} ({tool_reason})"
    elif use_laya and score == 0 and all(value == 0 for _, value in classification.scores):
        # Three choices make 33% a coin flip, so 45% is already a clear lean (Dawn, 2026-10-05).
        if laya is not None and laya.tool_p >= 0.45:
            tool = Tool(laya.tool)
            tier = (Tier.LOW, Tier.MID, Tier.HIGH)[
                max(0, min(2, int(laya.tier_score + 0.5)))
            ]
            reasons.remove(f"default tier: {default_tier.value}")
            tool_reason = f"laya: {tool.value} p={laya.tool_p:.2f}, tier {tier.value}"
    model_selection = select_model(tool, tier)
    reasons.extend(classification.reasons)
    reasons.extend((tool_reason, model_selection.reason))
    return RoutingDecision(
        tier=tier,
        tool=tool,
        model=model_selection.model,
        task_type=classification.task_type,
        complexity_score=score,
        reasons=tuple(reasons),
        high_risk=bool(risk_reasons),
        risk_reasons=risk_reasons,
    )


def build_agent_command(
    route: Tier | Tool,
    prompt: str,
    *,
    model: str | None = None,
    cwd: Path | None = None,
    vault: Path | None = None,
) -> list[str]:
    """Build a shell-free command using verified local CLI argument contracts."""

    tool = _legacy_tool_for_tier(route) if isinstance(route, Tier) else route
    if tool is Tool.AGY:
        command = ["agy", "--print"]
        if model:
            command.extend(["--model", model])
    elif tool is Tool.CODEX:
        command = ["codex", "exec", "--skip-git-repo-check"]
        # Codex now runs vault_write_guard through its PreToolUse lifecycle hook,
        # but OpenAI documents hooks as a guardrail rather than a complete security
        # boundary. Keep the mid tier read-only inside the canonical vault as independent
        # OS-level enforcement; ordinary repositories retain their normal sandbox.
        if cwd is not None and vault is not None and _is_within_dir(cwd.resolve(), vault.resolve()):
            command.extend(["--sandbox", "read-only"])
        if model:
            command.extend(["--model", model])
    else:
        command = ["claude", "--print"]
        if model:
            command.extend(["--model", model])
    command.append(prompt)
    return command


def _legacy_tool_for_tier(tier: Tier) -> Tool:
    """Preserve the original tier-to-tool contract for explicit overrides."""

    return {
        Tier.LOW: Tool.AGY,
        Tier.MID: Tool.CODEX,
        Tier.HIGH: Tool.CLAUDE,
    }[tier]


def check_task_state(prompt: str, roots: Sequence[Path]) -> StateCheckResult:
    """Find explicit completion records matching the task; never infer completion from a file name."""

    task_words = {
        word.lower()
        for word in _WORD.findall(prompt)
        if word.lower() not in _IGNORED_WORDS and len(word) > 2
    }
    if len(task_words) < 2:
        return StateCheckResult(already_completed=False)

    evidence: list[Path] = []
    seen: set[Path] = set()
    for path in _iter_text_files(roots):
        if path in seen:
            continue
        seen.add(path)
        try:
            content = path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        for line in content.splitlines():
            if not _COMPLETION_TERMS.search(line):
                continue
            if _NEGATED_COMPLETION.search(line):
                continue
            line_words = {word.lower() for word in _WORD.findall(line)}
            overlap = len(task_words & line_words) / len(task_words)
            if overlap >= 0.7:
                evidence.append(path)
                break
    return StateCheckResult(already_completed=bool(evidence), evidence=tuple(evidence[:5]))


def _is_within_dir(path: Path, root: Path) -> bool:
    normalized_path = os.path.normcase(str(path))
    normalized_root = os.path.normcase(str(root))
    try:
        Path(normalized_path).relative_to(Path(normalized_root))
    except ValueError:
        return normalized_path == normalized_root
    return True


def _iter_text_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        resolved = root.resolve()
        if resolved.is_file() and resolved.suffix.lower() in _TEXT_SUFFIXES:
            yield resolved
            continue
        if not resolved.is_dir():
            continue
        for path in resolved.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            relative_parts = path.relative_to(resolved).parts
            if any(
                part in _SKIP_PARTS or part.startswith(_TEST_ARTIFACT_PREFIX)
                for part in relative_parts
            ):
                continue
            yield path
