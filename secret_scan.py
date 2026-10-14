"""Flag lines that look like API keys, tokens or private keys."""

import re

SECRET_PATTERNS = (
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}", r"\bghp_[A-Za-z0-9]{30,}",
    r"\bgithub_pat_[A-Za-z0-9_]{30,}", r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[bp]-[A-Za-z0-9-]{20,}", r"\bntn_[A-Za-z0-9]{30,}",
    r"\bsecret_[A-Za-z0-9]{30,}", r"-----BEGIN .*PRIVATE KEY-----",
)
JSON_SECRET_PATTERN = r'"(?:api_?key|token|secret|password)"\s*[:=]\s*"[^"]{12,}"'


def scan_secret_content(content: str) -> int | None:
    """Return the 1-based number of the first suspicious line, or None."""
    for line_number, line in enumerate(content.splitlines(), 1):
        if any(re.search(pattern, line) for pattern in SECRET_PATTERNS) or re.search(
            JSON_SECRET_PATTERN, line, re.IGNORECASE
        ):
            return line_number
    return None
