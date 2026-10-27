import re
import subprocess
from pathlib import Path

from secret_scan import scan_secret_content

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_PATTERNS = (
    r"[A-Za-z]:[\\/](?:Users|Andy)",
    r"[\w.+-]+@[\w-]+\.[\w.-]+",
)


def tracked_text_files():
    names = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for name in names:
        path = ROOT / name
        if path.suffix in {".py", ".md", ".json", ".txt", ".js", ".ts"} and path.is_file():
            yield name, path.read_text(encoding="utf-8", errors="ignore")


def test_no_private_paths_or_emails():
    found = []
    for name, text in tracked_text_files():
        if name == "tests/test_no_private_data.py":
            continue
        for pattern in PRIVATE_PATTERNS:
            for match in re.finditer(pattern, text):
                found.append(f"{name}: {match.group(0)}")
    assert not found, found


def test_no_secrets_in_tracked_files():
    found = [
        name for name, text in tracked_text_files()
        if name != "secret_scan.py" and scan_secret_content(text)  # its own patterns look like keys
    ]
    assert not found, found
