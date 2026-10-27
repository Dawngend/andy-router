from pathlib import Path
import sys
import socket
import subprocess

import pytest


ROUTER = Path(__file__).resolve().parents[1]
if str(ROUTER) not in sys.path:
    sys.path.insert(0, str(ROUTER))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDY_STATE_DIR", str(tmp_path / ".andy_sync"))
    monkeypatch.delenv("ANDY_NO_OPENCODE_REPOS", raising=False)


@pytest.fixture(autouse=True)
def offline_commands(monkeypatch):
    original = subprocess.run

    def run(command, *args, **kwargs):
        executable = Path(command[0]).stem.lower() if isinstance(command, (list, tuple)) else ""
        if executable == "node":
            raise FileNotFoundError("Laya is mocked in offline tests")
        if executable in {"codex", "claude", "agy", "opencode", "gcloud", "ssh", "scp"}:
            raise AssertionError(f"Unmocked external CLI: {executable}")
        return original(command, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("Network calls must be mocked")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(socket, "create_connection", no_network)


@pytest.fixture(autouse=True)
def gemini_router_off(monkeypatch):
    # The router's Gemini call is off by default; tests that need it patch tier_router.llm_router themselves.
    import types

    import tier_router

    monkeypatch.setattr(tier_router, "llm_router", types.SimpleNamespace(route_with_llm=lambda prompt: None))
