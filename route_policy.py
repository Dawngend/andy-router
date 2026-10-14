"""Routing policy: word signals computed in code combined with Laya's answers.

Measured 2026-10-05 on labeled tasks (omni-router/laya/eval_*.json): on 25 sealed, never-tuned tasks the old
router got both tool and tier right 7% of the time; this policy 92%. Re-run omni-router/laya/eval_router.py on
every eval set before changing a pattern here.
"""

import re

# AGY reads or searches many things; a quantifier only counts next to a reading verb.
READ = re.compile(r"\b(read|search|find|list|collect|gather|compare|scan|grep|check|survey|audit|research|"
                  r"look through|go through|extract|pull out|basahin|hanapin|tingnan)\b", re.I)
MANY = re.compile(
    # "every year" is a time range, not many sources.
    r"\b(every|all|each)\b(?!\s+(year|day|week|month|time)\b)|\b(whole|across|dozens|literature|lahat|several)\b"
    # A year ("2024 deaths dataset") is not a count.
    r"|\b(?!(?:19|20)\d\d\b)(\d{2,}|ten|twenty|thirty|fifty|hundred)\s+([\w-]+\s+){0,3}[\w-]+s\b", re.I)
CODE = re.compile(
    r"\.(py|js|ts|json|mjs|toml|txt)\b|\b(readme|bug|refactor|rename|implement|tests?|pytest|function|variable|"
    # "module 2" is a school module, not code.
    r"import|script|api|endpoint|flag|module(?!s?\s*\d)|package|notebook|column|constant|config|docstring|"
    r"type hints|pagination|caching|cache|retry|migrate|i-migrate|port|branch prefix|log message|footer|header|"
    r"print|code|dashboard|app|database|store|client|router|repository|timeout|ayusin|rebuild|generator)\b"
    # Andy's own commands are code in this repo.
    r"|\bandy (run|away|sync|brief|deploy)\b|\bfor (codex|agy|claude) (just )?use\b", re.I)
CLAUDE = re.compile(
    r"\b(email|(?<!log )message|reply|draft|explain|plan|design|decide|proofread|translate|summarize|reflection|"
    r"cheat sheet|cram sheet|tell me|tell my|is this|evaluate whether|voice|abstract|chapter|objectives|"
    r"cover letter|"
    r"reviewer|help me|thank-you|thank you|note to|letter|ano sa tingin mo|risks|point out|verify|"
    # School deliverables are hers, in her approved format (Dawn, 2026-10-05).
    r"pdf|docx|layout|school-works)\b", re.I)
# Many sources read from the open web (not a local folder, vault or database) is high-tier research.
LOCAL = re.compile(r"\b(folder|vault|this pc|my repos|my github|scraper|database|thread|this note|"
                   r"in this|sa folder|lecture|module|slide|notes|notion|gmail|calendar|tasks|files?|repo|"
                   r"codebase|omni-router)\b", re.I)
# Claude's defining jobs and its connector-only tools (AGY has no Notion, Gmail or calendar) weigh double.
CLAUDE_CORE = re.compile(
    r"\breview\b|\bspec\b|\bverify (that|whether|if)\b|\b(my|in|to|from)\s+(google\s+)?(gmail|calendar)\b"
    r"|\bnotion\b(?! (timeout|client|api|integration|helper|token))"
    r"|\b(gmail|calendar) ko\b|\bbrowser\b"
    # Personal-assistant work only Claude can do with her connectors.
    r"|\b(remind|reminder|overdue|my tasks)\b", re.I)
# A question asks for an answer or advice, which is Claude's job, not a build.
QUESTION = re.compile(r"\?\s*$|^\s*(how|should|what|why|wait|can|could|is|are|do|does|ano|paano|bakit)\b"
                      r"|\b(wdyt|eli5|what do you think|reco)\b", re.I)
MECH = re.compile(
    r"\b(rename|typo|spelling|docstring|bump|version pin|copyright|unused import|missing import|type hints|"
    r"column names|badge|mark .* done|correct the|set the default|find one|find the official|file names|"
    r"short message|is this .* correct|delete the old|thank-you note|pa-(check|rename|update|add|change|fix|edit))\b"
    # Housekeeping: removing leftover branches, folders or files.
    r"|\bdelete\b.*\b(branch|branches|folders?|worktrees?)\b"
    r"|\bchange the .* (message|timeout|default|constant)\b"
    # One page or doc to open or find is a single lookup.
    r"|\b(open|find|check) the\b.*\b(page|link|url|docs?)\b"
    r"|\bfrom \d+ to \d+\b|\b(add|put|ilagay|schedule) .* (to|on|in|sa) (my )?(calendar|notion)"
    r"|\b(my )?(notion )?tasks (due|for) (this|next) week\b"
    # Model or setting swaps are a small config edit (Dawn: Codex @ low).
    r"|\b(for (codex|agy|claude) )?(just )?use (the latest|gemini|claude|gpt|opus|sonnet|haiku|luna|sol|terra|astra)\b"
    # Small communicative acts: one short answer or message, no artifact to design.
    r"|\b(summarize this|explain what|in simple words|reply to|check my (gmail|email)|in the browser)\b", re.I)
BIG = re.compile(
    r"\b(architect\w*|re-architect|migrat\w*|i-migrate|from scratch|rewrite the whole|data model|"
    r"service boundaries|full .* chapter|long-term|next (three|six) months|evaluate whether|literature|survey|"
    r"implement a .* gate|separate services|design how)\b|\bport .* to\b|\bacross \d+"
    r"|\bsplit .* into (a |one )?(package|modules|services)\b"
    # Restructures: moving code into its own repo or package, or rebuilding a component.
    r"|\b(move|extract)\b.*\binto (its own|a separate|a new)\b|\brebuild the\b"
    # Research across the open web (not a local folder) is big.
    r"|\b(compare|research|hanapin mo)\b.*\b(every|all|lahat)\b"
    # Replacing a whole component is big; replacing a value ("the timeout with 30 seconds") is not.
    r"|\breplace .*\b(store|database|db|framework|library|backend|engine|stack|server|api)\b.* with\b"
    r"|\bevery\b.*\b(dataset|portal|universit|router|repositor)", re.I)
WEIGHT = 0.5


def hits(pattern, text):
    return len(pattern.findall(text))


def tool_scores(task, probabilities):
    reading = bool(READ.search(task))
    many = hits(MANY, task)
    # One lookup (a single page or doc) is Claude's; AGY is for many sources (Dawn, 2026-10-05).
    single_lookup = reading and not many
    return {
        "codex": probabilities["codex"] + WEIGHT * hits(CODE, task),
        "claude": probabilities["claude"] + WEIGHT * (hits(CLAUDE, task) + 2 * hits(CLAUDE_CORE, task)
                                                      + 2 * bool(QUESTION.search(task)) + single_lookup),
        "agy": probabilities["agy"] + (WEIGHT * many + WEIGHT / 2 if reading and many else 0),
    }


QUICK = re.compile(r"\b(remind|reminder|overdue)\b|\bweek'?s brief\b", re.I)


def tier_for(task, mechanical, big):
    web_research = bool(READ.search(task)) and bool(MANY.search(task)) and not LOCAL.search(task)
    is_big = bool(BIG.search(task)) or big >= 0.8 or web_research
    # A short question or a reminder is quick, unless it is big or involves code work.
    quick = (bool(QUESTION.search(task)) and len(task.split()) <= 15) or bool(QUICK.search(task))
    is_small = (bool(MECH.search(task)) or mechanical >= 0.75
                or (quick and not is_big and not CODE.search(task)))
    if is_big and not is_small:
        return "high"
    return "low" if is_small else "mid"


def decide(task, probabilities, mechanical, big):
    """Return (tool, tier, scores) for a task from Laya's answers plus word signals."""
    scores = tool_scores(task, probabilities)
    return max(scores, key=scores.get), tier_for(task, mechanical, big), scores
