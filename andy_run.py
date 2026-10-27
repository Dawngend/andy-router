"""Build, check and review a spec in its own worktree."""

import argparse
from dataclasses import asdict, replace
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tokenize

import tool_cooldown

from secret_scan import scan_secret_content
from laya_router import ask_laya
from tier_router import Tier, Tool, _HIGH_RISK_PATTERNS, _is_within_dir, assess_task, select_model


REPLY_RULE = "First answer each finding with AGREE or DISAGREE and one reason, then fix only what you agree with."
BUILD_RULES = "Work only in this worktree. Do not push or merge. Keep code simple."
CODEX_FALLBACK = ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-terra")


def state_dir():
    return Path(os.environ["ANDY_STATE_DIR"]) if "ANDY_STATE_DIR" in os.environ else Path.home() / ".andy_sync"


def read_spec(path):
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("Spec needs a --- header block")
    header = {}
    for line in lines[1:lines.index("---", 1)]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or key.strip() in header:
            raise ValueError("Invalid or duplicate spec header field")
        header[key.strip()] = value.strip().strip("\"'")
    for key in ("title", "repo", "check"):
        if not header.get(key):
            raise ValueError(f"Spec requires {key}")
    header.setdefault("size", "small")
    if header["size"] not in {"small", "big"}:
        raise ValueError("Spec size must be small or big")
    if header.get("tool", "codex") not in {"codex", "claude", "agy", "opencode"} or header.get("tier", "mid") not in {"low", "mid", "high"}:
        raise ValueError("Spec tool must be codex, claude, agy or opencode and tier low, mid or high")
    if "effort" in header and header["effort"] not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Spec effort must be low, medium, high, xhigh or max")
    if "scope" in header and (not header["scope"] or any(
        not prefix.strip() or prefix.strip().startswith("/") or "\\" in prefix
        or ".." in prefix.split("/")
        for prefix in header["scope"].split(",")
    )):
        raise ValueError("Spec scope must contain repo-relative path prefixes with forward slashes")
    repo = Path(header["repo"]).expanduser()
    header["repo"] = (path.parent / repo).resolve()
    if header.get("tool") == "opencode" and any(
        _is_within_dir(header["repo"], Path(repo))
        for repo in os.environ.get("ANDY_NO_OPENCODE_REPOS", "").split(os.pathsep) if repo
    ):
        raise ValueError("opencode is blocked for this repo; use codex or claude")
    return header, text


AGY_PROMPT_LIMIT = 24000  # agy takes the prompt as an argument; Windows caps a command line near 32k


class NoReviewer(RuntimeError):
    """Every reviewer CLI failed (for example a logged-out Claude CLI)."""


def effort_for(tool, effort):
    if tool in (Tool.AGY, Tool.OPENCODE):
        return None
    return "xhigh" if tool is Tool.CODEX and effort == "max" else effort


def agent_command(tool, model, worktree, *, review=False, which=shutil.which, prompt="", effort=None):
    executable = which(tool.value)
    if not executable:
        raise ValueError(f"CLI not found: {tool.value}")
    if tool is Tool.CODEX:
        command = [executable, "exec", "-m", model, "--sandbox",
                   "read-only" if review else "workspace-write", "-C", str(worktree)]
        if effort:
            command.extend(["-c", f'model_reasoning_effort="{effort_for(tool, effort)}"'])
        return [*command, "-"]
    if tool is Tool.AGY:
        # agy ignores stdin, but it is a real .exe, so an argument is not mangled by cmd.exe.
        # Never truncate: a half prompt could drop the diff or the JSON instructions and still pass.
        if len(prompt) > AGY_PROMPT_LIMIT:
            raise ValueError(f"prompt too long for agy ({len(prompt)} characters)")
        return [executable, "--model", model, "-p", prompt]
    if tool is Tool.OPENCODE:
        # opencode is a .cmd shim; cmd.exe mangles a long multi-line argument, so the prompt
        # travels as a file instead, written outside the worktree sandbox.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(prompt)
            prompt_path = handle.name
        return [executable, "run", "Follow the attached task file exactly. Work only in this directory.",
                "-m", model, "--dir", str(worktree), "--auto", "-f", prompt_path]
    command = [executable, "-p", "--model", model]
    if effort:
        command.extend(["--effort", effort_for(tool, effort)])
    if not review:
        # Print mode cannot ask for approval, so a building Claude needs edits pre-approved.
        command.extend(["--permission-mode", "acceptEdits"])
    return command


def stdin_for(tool, prompt):
    # OpenCode reads the prompt from its -f file, so it gets closed stdin instead of the parent's.
    if tool is Tool.OPENCODE:
        return ""
    return None if tool is Tool.AGY else prompt


_SWITCH_WARNED = set()


def read_switch(path):
    """Read a switch file; an undecodable (for example UTF-16) file counts as off."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        if path not in _SWITCH_WARNED:
            _SWITCH_WARNED.add(path)
            print(f"Ignoring {path}: switch file is not UTF-8 text")
        return None


def save_claude(now=None):
    """Dawn's switch for a low weekly Claude limit: Claude becomes last resort until the limit resets.

    Claude writes the weekly reset time (ISO 8601) into the file when it turns the switch on; after that
    time the switch counts as off by itself. An empty file means on until removed.
    """
    path = state_dir() / "save_claude"
    if not path.exists():
        return False
    text = read_switch(path)
    if text is None:
        return False
    lines = text.splitlines()
    text = lines[0].strip().lower() if lines else ""
    if not text:
        return True
    try:
        resets_at = datetime.fromisoformat(text.replace("z", "+00:00"))
    except ValueError:
        return True
    return (now or datetime.now(resets_at.tzinfo)) < resets_at


MODE_TOOLS = {"claude": Tool.CLAUDE, "codex": Tool.CODEX, "gemini": Tool.AGY}


def builder_mode(now=None):
    """Dawn's switch to force one builder family for a stretch of time (`andy mode`).

    Line 1 names the family: claude, codex or gemini (gemini means the agy tool). An optional
    line 2 is an ISO reset time; after it the mode counts as off, same as save_claude. A missing
    file, empty file, or an unknown word on line 1 means off (None).
    """
    path = state_dir() / "builder_mode"
    if not path.exists():
        return None
    text = read_switch(path)
    if text is None:
        return None
    lines = text.splitlines()
    tool = MODE_TOOLS.get(lines[0].strip().lower() if lines else "")
    if tool is None:
        return None
    if len(lines) > 1 and lines[1].strip():
        try:
            resets_at = datetime.fromisoformat(lines[1].strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        current = now or datetime.now(resets_at.tzinfo)
        if resets_at.tzinfo is not None and current.tzinfo is None:
            current = current.astimezone()
        elif resets_at.tzinfo is None and current.tzinfo is not None:
            current = current.astimezone().replace(tzinfo=None)
        if current >= resets_at:
            return None
    return tool


def saved_family(mode):
    # The family Dawn is saving by forcing the other one to build: codex mode saves Claude;
    # claude or gemini mode saves Codex.
    return Tool.CLAUDE if mode is Tool.CODEX else Tool.CODEX


def reviewers_for(route, *, plan_check=False):
    # With save-Claude on, a separate read-only Codex run follows AGY for Codex builds.
    agy = (Tool.AGY, "gemini-3.8-flash-high")
    claude_model = "opus" if route.tier is Tier.HIGH else "sonnet"
    if route.tool is Tool.CODEX:
        reviewers = [(Tool.CLAUDE, claude_model), agy]
        if save_claude():
            reviewers = [agy, (Tool.CODEX, "gpt-6-sol"), reviewers[0]]
    elif route.tool is Tool.CLAUDE:
        reviewers = ([(Tool.CODEX, "gpt-6-sol"), agy] if plan_check
                     else [agy, (Tool.CODEX, "gpt-6-sol")])
    elif route.tool is Tool.OPENCODE:
        # Never Codex first, never OpenCode reviewing itself.
        reviewers = [agy, (Tool.CLAUDE, claude_model)]
    else:
        reviewers = [(Tool.CODEX, "gpt-6-sol"), (Tool.CLAUDE, claude_model)]
    if save_claude():
        reviewers.sort(key=lambda reviewer: reviewer[0] is Tool.CLAUDE)
    try:
        cooldowns = tool_cooldown.load_cooldowns(
            state_dir() / "tool_cooldown.json", datetime.now())
    except Exception as exc:
        print(f"tool cooldown file unreadable, ignoring cooldowns: {exc}")
        cooldowns = {}
    filtered = [reviewer for reviewer in reviewers if reviewer[0].value not in cooldowns]
    reviewers = filtered or reviewers
    mode = builder_mode()
    if mode is not None:
        filtered = [reviewer for reviewer in reviewers if reviewer[0] is not saved_family(mode)]
        reviewers = filtered or reviewers
    return reviewers


def reviewer_for(route):
    return reviewers_for(route)[0]


def call(command, cwd, runner, *, required=True, shell=False, input=None):
    result = runner(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    check=False, shell=shell, input=input)
    if required and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command[0] if isinstance(command, list) else command}\n"
                           f"{result.stderr or result.stdout or ''}")
    return result


def call_agent(tool, model, worktree, prompt, runner, which, *, review=False, required=True, effort=None):
    models = (CODEX_FALLBACK[CODEX_FALLBACK.index(model):]
              if tool is Tool.CODEX and model in CODEX_FALLBACK else (model,))
    for used_model in models:
        command = agent_command(tool, used_model, worktree, review=review, which=which, prompt=prompt,
                                effort=effort)
        try:
            result = call(command, worktree, runner, required=False, input=stdin_for(tool, prompt))
        finally:
            if tool is Tool.OPENCODE:
                Path(command[command.index("-f") + 1]).unlink(missing_ok=True)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 or "Selected model is at capacity" not in output:
            break
    if required and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {tool.value}/{used_model}\n"
                           f"{result.stderr or result.stdout or ''}")
    return result, used_model


def cooldown_fallback(route, cooldowns, *, exclude=None):
    if route.tool.value not in cooldowns:
        return route
    capped_tool = route.tool
    for fallback in (Tool.CLAUDE, Tool.AGY):
        if fallback is not capped_tool and fallback.value not in cooldowns and fallback is not exclude:
            return replace(route, tool=fallback, model=select_model(fallback, route.tier).model,
                           reasons=(*route.reasons,
                                    f"{capped_tool.value} in cooldown (spend cap/usage limit) -> {fallback.value}"))
    return route


def git(args, cwd, runner, *, required=True):
    return call(["git", *args], cwd, runner, required=required)


def scope_violations(scope, branch, worktree, runner):
    changed = git(["diff", "--name-only", f"main...{branch}"], worktree, runner).stdout.splitlines()
    prefixes = [prefix.strip() for prefix in scope.split(",")]
    return [name for name in changed if not any(name.startswith(prefix) for prefix in prefixes)]


def check_command(command, cwd, runner):
    result = call(command, cwd, runner, required=False, shell=True)
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return {"pass": result.returncode == 0, "exit_code": result.returncode,
            "tail": "\n".join(output.splitlines()[-20:])}


def same_failed_check(previous, current):
    if previous["pass"] or current["pass"]:
        return False

    def without_noise(tail):
        tail = re.sub(r"\bin \d+(?:\.\d+)?s\b", "in <time>s", tail)
        return re.sub(r"0x[0-9a-fA-F]+", "<address>", tail)

    return without_noise(previous["tail"]) == without_noise(current["tail"])


def diagnose_stuck(route, worktree, spec, diff, previous, current, runner, which):
    tool, model = ((Tool.CLAUDE, "opus") if route.tool in {Tool.CODEX, Tool.AGY} and not save_claude()
                   else (Tool.CODEX, "gpt-6-sol"))
    mode = builder_mode()
    if mode is not None and tool is saved_family(mode):
        tool, model = (Tool.CODEX, "gpt-6-sol") if tool is Tool.CLAUDE else (Tool.CLAUDE, "opus")
    prompt = (f"SPEC:\n{spec}\nDIFF:\n{diff}\nPREVIOUS CHECK TAIL:\n{previous}\n"
              f"LATEST CHECK TAIL:\n{current}\n"
              "In at most 10 lines, say the root cause and the one change that would fix it. Plain text, no edits.")
    try:
        result, _ = call_agent(tool, model, worktree, prompt, runner, which,
                               review=True, required=False, effort="xhigh" if tool is Tool.CODEX else None)
        if result.returncode:
            return f"unavailable: {result.stderr or result.stdout or f'exit {result.returncode}'}".strip()
        return result.stdout.strip() or "unavailable: empty diagnosis"
    except Exception as exc:
        return f"unavailable: {exc}"


def strip_fences(text):
    # Claude often wraps a JSON-only answer in ```json fences.
    match = re.search(r"```(?:json)?\s*(.*?)```", text or "", re.S)
    return match.group(1) if match else (text or "")


def parse_review(result):
    try:
        review = json.loads(strip_fences(result.stdout))
        if not isinstance(review, dict) or type(review.get("pass")) is not bool:
            raise ValueError("Expected a boolean pass")
        findings = review.get("findings")
        if not isinstance(findings, list) or any(
            not isinstance(item, dict) or item.get("severity") not in {"high", "low"}
            or not isinstance(item.get("text"), str) or not isinstance(item.get("fix"), str)
            for item in findings
        ):
            raise ValueError("Invalid findings")
        if result.returncode:
            raise ValueError(f"Reviewer exited {result.returncode}: {result.stderr or ''}")
        return review
    except (ValueError, TypeError) as exc:
        return {"pass": False, "findings": [{"severity": "high", "text": f"Invalid review: {exc}",
                                            "fix": "Return the required JSON review."}]}


def review_prompt(text, diff, check):
    return (f"Review this task. Do not edit files, push or merge.\nSPEC:\n{text}\nDIFF:\n{diff}\n"
            f"CHECK:\n{json.dumps(check)}\nAnswer with JSON only: "
            '{"pass": true, "findings": [{"severity": "high|low", "text": "...", "fix": "..."}]}')


def plan_check_prompt(text):
    return (f"SPEC:\n{text}\n"
            "Review this spec before anyone builds it. Look for missing requirements, contradictions, "
            "risky or destructive steps, missing tests, and anything that would break the repo's rules. "
            "Do not edit files.\nAnswer with JSON only: "
            '{"pass": true, "findings": [{"severity": "high|low", "text": "...", "fix": "..."}]}')


def run_review(route, worktree, prompt, runner, which, effort=None, *, plan_check=False):
    failures = []
    for tool, model in reviewers_for(route, plan_check=plan_check):
        try:
            result, used_model = call_agent(tool, model, worktree, prompt, runner, which,
                                            review=True, required=False, effort=effort)
        except ValueError as exc:
            failures.append(f"{tool.value}: {exc}")
            continue
        review = parse_review(result)
        # Only a valid JSON review counts; junk output falls through to the next reviewer.
        if not review["findings"] or not review["findings"][0]["text"].startswith("Invalid review"):
            return f"{tool.value}/{used_model}", review
        failures.append(f"{tool.value}/{used_model}: exit {result.returncode}: "
                        f"{((result.stdout or '') + (result.stderr or '')).strip()[:200]}")
    raise NoReviewer("No reviewer available: " + " | ".join(failures))


def build_round(number, prompt, text, header, route, worktree, branch, runner, which, *,
                clock=datetime.now, cooldown_path=None):
    try:
        built, builder_model = call_agent(route.tool, route.model, worktree, prompt, runner, which,
                                          effort=header.get("effort"))
    except RuntimeError as exc:
        if cooldown_path is not None and tool_cooldown.is_cap_failure(str(exc)):
            tool_cooldown.record_cooldown(cooldown_path, route.tool.value, clock())
        raise
    git(["add", "-A"], worktree, runner)
    # A builder may have made no edits (or committed them itself).
    staged = git(["diff", "--cached", "--quiet"], worktree, runner, required=False)
    if staged.returncode == 1:
        git(["commit", "-m", f"andy run {branch[4:]}: round {number}"], worktree, runner)
    elif staged.returncode != 0:
        raise RuntimeError("Could not inspect staged changes")
    commit = git(["rev-parse", "HEAD"], worktree, runner).stdout.strip()
    check = check_command(header["check"], worktree, runner)
    diff = git(["diff", f"main...{branch}"], worktree, runner).stdout
    scope_result = ({"scope_violations": scope_violations(header["scope"], branch, worktree, runner)}
                    if "scope" in header else {})
    try:
        reviewer, review = run_review(route, worktree, review_prompt(text, diff, check), runner, which,
                                      effort=header.get("effort"))
    except NoReviewer as exc:
        # Keep the build and check that did happen in the report.
        exc.round = {"round": number, "commit": commit, "check": check,
                     "builder_model": builder_model, "reviewer": None,
                     "review": None, "builder_answers": built.stdout or "", "pass": False,
                     **scope_result}
        raise
    passed = check["pass"] and review["pass"] and not scope_result.get("scope_violations") and not any(
        finding["severity"] == "high" for finding in review["findings"]
    )
    return {"round": number, "commit": commit, "check": check, "builder_model": builder_model,
            "reviewer": reviewer, "review": review,
            "builder_answers": built.stdout or "", "pass": passed, **scope_result}, diff


def free_slug(base, repo, runner):
    for number in range(1, 21):
        slug = base if number == 1 else f"{base}-{number}"
        exists = git(["show-ref", "--verify", "--quiet", f"refs/heads/run/{slug}"], repo, runner, required=False)
        if exists.returncode not in (0, 1):
            raise RuntimeError("Could not check for existing branch")
        if exists.returncode == 1 and not (repo.parent / f"wt-{slug}").exists():
            return slug
    raise ValueError(f"No free branch name for {base}")


NEGATED = re.compile(r"\b(never|not|no|don't|do not|without|nothing)\b(\s+[\w-]+){0,3}\s*$", re.I)


def risky_match(pattern, text):
    # A safety sentence ("rows are never removed") is not a destructive request.
    return any(not NEGATED.search(text[max(0, m.start() - 40):m.start()]) for m in pattern.finditer(text))


DESTRUCTIVE_CALL = re.compile(
    r"\b(?:shutil\s*\.\s*rmtree|os\s*\.\s*(?:remove|unlink)|"
    r"Path\s*\.\s*unlink|Path\s*\([^()\n]*\)\s*\.\s*unlink|"
    r"[A-Za-z_]\w*\s*\.\s*unlink)\s*\(|"
    r"\brm\s+-rf\b|\bRemove\s*-\s*Item\b[^\n;]*?\s+-Recurse\b|"
    r"\bgit\s+push\b[^\n;]*?(?:--force\b|-f\b)|"
    r"\bgit\s+reset\s+--hard\b|\bgit\s+branch\s+-D\b|"
    r"\bDROP\s+TABLE\b|\bTRUNCATE(?:\s+TABLE)?\s+[A-Za-z_]\w*|"
    r"\bDELETE\s+FROM\b",
    re.I,
)


def destructive_diff(diff):
    for file_diff in re.split(r"(?m)^diff --git .*$", diff):
        added = "\n".join(line[1:] for line in file_diff.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        added = re.sub(r"(?P<quote>'''|\"\"\").*?(?P=quote)", "", added, flags=re.S)
        added = re.sub(r"/\*.*?\*/", "", added, flags=re.S)
        added = re.sub(r"(?:^|[ \t])--(?:[ \t]|$)[^\n]*", "", added, flags=re.M)
        for line in added.splitlines():
            try:
                tokens = tokenize.generate_tokens(io.StringIO(line.lstrip() + "\n").readline)
                code = tokenize.untokenize(
                    (token.type, " " if token.type in (tokenize.STRING, tokenize.COMMENT) else token.string)
                    for token in tokens
                )
            except (tokenize.TokenError, IndentationError):
                continue
            if DESTRUCTIVE_CALL.search(code):
                return True
    return False


def stop_reasons(header, route, text, diff, rounds, *, stuck=False):
    reasons = []
    if header["size"] == "big":
        reasons.append("size is big")
    if route.tier is Tier.HIGH:
        reasons.append("tier is high")
    reasons.extend(reason for pattern, reason in _HIGH_RISK_PATTERNS if risky_match(pattern, text))
    if destructive_diff(diff):
        reasons.append("Added diff lines contain a destructive operation")
    added = "\n".join(line[1:] for line in diff.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    if scan_secret_content(added) is not None:
        reasons.append("Added diff lines contain a possible secret")
    if stuck:
        reasons.append("same check failure twice in a row")
    elif len(rounds) == 3 and not rounds[-1]["pass"]:
        reasons.append("three-round limit reached")
    if rounds and rounds[-1].get("scope_violations"):
        reasons.append("files changed outside scope: " + ", ".join(rounds[-1]["scope_violations"]))
    return reasons


def merge_result(header, branch, report, runner):
    repo = header["repo"]
    if git(["status", "--porcelain", "--untracked-files=no", "--ignore-submodules=dirty"], repo, runner).stdout.strip():
        report["reasons"] = ["Repository has local changes; main was not changed"]
        return "stopped-for-dawn"
    switched = git(["checkout", "main"], repo, runner, required=False)
    if switched.returncode:
        report["reasons"] = [f"Cannot check out main: {switched.stderr}"]
        return "stopped-for-dawn"
    pulled = git(["pull", "--rebase", "--autostash", "origin", "main"], repo, runner, required=False)
    if pulled.returncode:
        report["reasons"] = [f"Pull failed before merge: {pulled.stderr or pulled.stdout}"]
        return "stopped-for-dawn"
    report["main_before"] = git(["rev-parse", "HEAD"], repo, runner).stdout.strip()
    merged = git(["merge", "--no-edit", branch], repo, runner, required=False)
    if merged.returncode:
        report["reasons"] = [f"Merge failed or has conflicts: {merged.stderr or merged.stdout}"]
        return "stopped-for-dawn"
    report["main_after"] = git(["rev-parse", "HEAD"], repo, runner).stdout.strip()
    report["post_merge_check"] = check_command(header["check"], repo, runner)
    if not report["post_merge_check"]["pass"]:
        report["reasons"] = ["Post-merge check failed; main is committed but was not pushed"]
        return "stopped-for-dawn"
    git(["push", "origin", "main"], repo, runner)
    return "merged"


def finish(report, runs_dir, notion):
    path = runs_dir / f"{report['slug']}.json"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    try:
        if notion is not False:
            report["notion_page_id"] = notion(report)
    except Exception as exc:
        report["notion_error"] = str(exc)
        print(f"Notion report failed: {exc}")
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"andy run: {report['outcome']} after {len(report['rounds'])} round(s). Report: {path}")
    for reason in report.get("reasons", []):
        print(reason)
    return {"merged": 0, "stopped-for-dawn": 3, "failed": 1}[report["outcome"]]


def main(argv=None, *, runner=None, which=None, clock=None, runs_dir=None, route_task=None, notion=None,
         cooldown_path=None):
    parser = argparse.ArgumentParser(prog="andy run")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    runner = runner or subprocess.run
    which = which or shutil.which
    clock = clock or datetime.now
    route_task = route_task or assess_task
    if notion is None:
        notion = False  # optional hook: a callable that reports the run elsewhere and returns an id
    runs_dir = Path(runs_dir) if runs_dir is not None else state_dir() / "runs"
    cooldown_path = (Path(cooldown_path) if cooldown_path is not None
                     else state_dir() / "tool_cooldown.json")
    report = None
    try:
        header, text = read_spec(args.spec.resolve())
        slug = (re.sub(r"[^a-z0-9]+", "-", header["title"].lower()).strip("-") or "task")
        slug += "-" + clock().strftime("%Y%m%d")
        branch = f"run/{slug}"
        repo = header["repo"]
        worktree = repo.parent / f"wt-{slug}"
        # Preview must not start Laya or populate its model cache. A spec that names both tool and tier
        # skips routing (the spec writer already knows); the hijack check below still runs.
        pinned = bool(header.get("tool") and header.get("tier"))
        route = route_task(text, use_laya=not (args.dry_run or pinned))
        if header.get("tool") or header.get("tier"):
            tool = Tool(header.get("tool", route.tool.value))
            tier = Tier(header.get("tier", route.tier.value))
            route = replace(route, tool=tool, tier=tier, model=select_model(tool, tier).model,
                            reasons=(*route.reasons, f"spec header: {tool.value} @ {tier.value}"))
        if not header.get("tool") and route.tool is Tool.CLAUDE and save_claude():
            route = replace(route, tool=Tool.CODEX, model=select_model(Tool.CODEX, route.tier).model)
            print("save-Claude on: builder claude -> codex")
        cooldowns = tool_cooldown.load_cooldowns(cooldown_path, clock())
        route = cooldown_fallback(route, cooldowns)
        mode = builder_mode(clock())
        if mode is not None and route.tool is not Tool.OPENCODE:
            route = replace(route, tool=mode, model=select_model(mode, route.tier).model,
                            reasons=(*route.reasons, f"builder mode: {mode.value}"))
            print(f"Builder mode: {mode.value}")
            route = cooldown_fallback(route, cooldowns, exclude=saved_family(mode))
        requested_effort = header.get("effort")
        chosen_effort = effort_for(route.tool, requested_effort)
        if requested_effort == "max" and (route.tool is Tool.CODEX or
                                            any(tool is Tool.CODEX for tool, _ in reviewers_for(route))):
            print("Effort: max -> xhigh for Codex")
        suffix = f", effort {chosen_effort}" if chosen_effort else ""
        print(f"Route: {route.tool.value} @ {route.tier.value} ({route.model}{suffix})")
        print(f"Reason: {'; '.join(route.reasons)}")
        if "scope" in header and args.dry_run:
            print(f"Scope: {header['scope']}")
        if args.dry_run:
            tool, model = reviewer_for(route)
            builder_command = agent_command(route.tool, route.model, worktree, which=which,
                                            effort=requested_effort)
            review_command = agent_command(tool, model, worktree, review=True, which=which,
                                           prompt="<review prompt>", effort=requested_effort)
            print(f"Plan check: {'yes' if header['size'] == 'big' else 'no'}")
            print(f"Worktree: {worktree}")
            print("Builder: " + subprocess.list2cmdline(builder_command))
            print("Reviewer: " + subprocess.list2cmdline(review_command))
            for command in (builder_command, review_command):
                if "-f" in command:
                    Path(command[command.index("-f") + 1]).unlink(missing_ok=True)
            print("Fallback reviewer: " + " ".join(f"{t.value}/{m}" for t, m in reviewers_for(route)[1:]))
            print("Dry run: rules-only route; nothing changed.")
            return 0
        report = {"slug": slug, "title": header["title"], "repo": str(repo), "branch": branch,
                  "worktree": str(worktree), "route": {**asdict(route), "effort": chosen_effort},
                  "rounds": [], "outcome": "failed"}
        hijack_decision = ask_laya(text)
        if hijack_decision is None:
            report["hijack_check"] = "unavailable"
        else:
            report["hijack_check"] = hijack_decision.hijack
            if hijack_decision.hijack is not None and hijack_decision.hijack >= 0.8:
                report["outcome"] = "stopped-for-dawn"
                report["reasons"] = [f"spec text looks like an instruction hijack (p={hijack_decision.hijack:.2f})"]
                return finish(report, runs_dir, notion)
        plan_notes = []
        if header["size"] == "big":
            try:
                reviewer, review = run_review(route, repo, plan_check_prompt(text), runner, which,
                                              effort=header.get("effort"), plan_check=True)
                report["plan_check"] = {"reviewer": reviewer, "review": review}
                findings = review["findings"]
                high_findings = [item for item in findings if item["severity"] == "high"]
                if high_findings:
                    report["outcome"] = "stopped-for-dawn"
                    report["reasons"] = [f"plan check: {item['text']}" for item in high_findings]
                    report.update(branch=None, worktree=None)
                    return finish(report, runs_dir, notion)
                plan_notes = findings
            except NoReviewer:
                report["plan_check"] = "unavailable"
        # A re-queued task must not be blocked by its earlier branch: take the next free -2, -3 suffix.
        slug = free_slug(slug, repo, runner)
        branch, worktree = f"run/{slug}", repo.parent / f"wt-{slug}"
        print(f"Worktree: {worktree}")
        scope_rule = f"\nOnly change files under: {header['scope']}" if "scope" in header else ""
        prompt = f"{text}\n\n{BUILD_RULES}\nWorktree: {worktree}{scope_rule}"
        if plan_notes:
            prompt += "\nPlan review notes:\n" + "\n".join(
                f"- {item['text']} Fix: {item['fix']}" for item in plan_notes)
        report.update(slug=slug, branch=branch, worktree=str(worktree))
        git(["worktree", "add", "-b", branch, str(worktree), "main"], repo, runner)
        diff = ""
        stuck = False
        for number in range(1, 4):
            result, diff = build_round(number, prompt, text, header, route, worktree, branch, runner, which,
                                       clock=clock, cooldown_path=cooldown_path)
            report["rounds"].append(result)
            if result["pass"]:
                break
            if number >= 2 and same_failed_check(report["rounds"][-2]["check"], result["check"]):
                stuck = True
                report["stuck_diagnosis"] = diagnose_stuck(
                    route, worktree, text, diff, report["rounds"][-2]["check"]["tail"],
                    result["check"]["tail"], runner, which)
                break
            prompt = (f"{text}\n\n{BUILD_RULES}\nWorktree: {worktree}{scope_rule}\n{REPLY_RULE}\n"
                      f"Findings: {json.dumps(result['review'])}\nCheck: {json.dumps(result['check'])}")
            if result.get("scope_violations"):
                prompt += "\nRevert files changed outside scope: " + ", ".join(result["scope_violations"])
        report["reasons"] = stop_reasons(header, route, text, diff, report["rounds"], stuck=stuck)
        if report["reasons"]:
            report["outcome"] = "stopped-for-dawn"
        else:
            report["outcome"] = merge_result(header, branch, report, runner)
    except NoReviewer as exc:
        # The build may be fine; only the review could not happen, so Dawn decides.
        print(f"andy run stopped: {exc}")
        if getattr(exc, "round", None):
            report["rounds"].append(exc.round)
        report["outcome"] = "stopped-for-dawn"
        report["reasons"] = [str(exc)]
        if report["rounds"] and report["rounds"][-1].get("scope_violations"):
            report["reasons"].append("files changed outside scope: " +
                                     ", ".join(report["rounds"][-1]["scope_violations"]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"andy run failed: {exc}")
        if report is None:
            return 1
        report["outcome"] = "failed"
        report["reasons"] = [str(exc)]
    try:
        return finish(report, runs_dir, notion)
    except OSError as exc:
        print(f"Could not write run report: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
