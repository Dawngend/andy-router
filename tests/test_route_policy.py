import pytest

from route_policy import decide

EVEN = {"codex": 0.34, "claude": 0.33, "agy": 0.33}


@pytest.mark.parametrize("task,tool", [
    ("Rewrite the whole router so every agent runs in parallel worktrees", "codex"),
    ("Read all 40 job postings in this folder and list the remote ones", "agy"),
    ("Rename the Notion task about LinkedIn post 4", "claude"),
    ("Change the default Notion timeout from 30 to 20 seconds", "codex"),
    ("Review my andy deploy spec and point out risks", "claude"),
])
def test_word_signals_settle_a_coin_flip(task, tool):
    assert decide(task, EVEN, 0.1, 0.1)[0] == tool


@pytest.mark.parametrize("task,mechanical,big,tier", [
    ("Fix the spelling of 'recieve' in the log message", 0.1, 0.1, "low"),
    ("Migrate the API from SQLite to Postgres", 0.1, 0.1, "high"),
    ("Add a --verbose flag to andy sync, with a test", 0.1, 0.1, "mid"),
    ("Add a --verbose flag to andy sync, with a test", 0.1, 0.9, "high"),
    ("Add a --verbose flag to andy sync, with a test", 0.9, 0.1, "low"),
    ("Replace the timeout with 30 seconds in the config", 0.1, 0.1, "mid"),
    ("Replace the SQLite job store with a vector database", 0.1, 0.1, "high"),
])
def test_tier_ladder(task, mechanical, big, tier):
    assert decide(task, EVEN, mechanical, big)[1] == tier
