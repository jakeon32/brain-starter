#!/usr/bin/env python3
"""Fail the build if anything personal or company-specific leaked into this repo.

Why this exists: brain-starter is the public, generic skeleton of a system whose
private original carries a company's internal server addresses, Notion database
ids and personal paths. A public repo cannot be un-published — clones and caches
outlive a force-push — so this scanner is the only defence that runs *before*
the mistake becomes permanent. It runs in CI on every push, which also makes it
a guard against a future contributor pasting something in without thinking.

What lives where, and why: this file ships publicly, so it cannot itself
contain the real denylist of proper nouns (a company name, a person's name, a
private vault's folder name...) it exists to keep out — that was the original
bug here, not a missing feature. PATTERNS below holds only generic *shape*
patterns (a Windows user-profile path, an RFC1918 IP, a UUID/Slack-id-shaped
token) that name no one. The actual proper-noun denylist is optional, local,
and gitignored — see load_denylist(). A fresh public clone has no such file
and the scanner still runs fine on shape patterns alone.

Usage:
    python tools/check-purity.py              # scan every tracked file
    python tools/check-purity.py --paths a b  # scan specific paths
Exit 0 = clean, 1 = contaminated (details on stderr).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Each pattern is deliberately narrow: a scanner that cries wolf gets ignored,
# and an ignored scanner is worse than none. Every pattern here matches a
# *class* of thing, never a specific real entity — that is what makes it safe
# to publish this file. Naming an actual company, person, or vault belongs in
# the local denylist (load_denylist(), below), not here.
PATTERNS: dict[str, re.Pattern] = {
    # Any Windows user-profile path, any D:-drive path, any macOS /Users/
    # home directory path. These match ANY such path, which is the point --
    # they flag the *shape* of a personal filesystem path without naming a
    # person or a machine. Case-insensitive because drive-letter and segment
    # casing is not guaranteed to survive a paste. `\b` before C:/D: keeps
    # this from firing on an unrelated word merely ending in "c:"/"d:" (e.g.
    # a regex source line whose prose ends "...recalled:" then more regex).
    "personal-path": re.compile(
        r"(?i)\bC:[\\/]Users[\\/][^\\/\s]+"
        r"|\bD:[\\/][^\\/\s]+"
        r"|/Users/[^/\s]+"
    ),
    # RFC1918 ranges only. 127.0.0.1 is fine — it is in the docs legitimately.
    "internal-ip": re.compile(r"\b(?:192\.168|10\.(?:\d{1,3})|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    # Notion page/database ids (uuid-ish hex, matched case-insensitively via
    # an explicit a-fA-F class since Notion's export UI can show these upper-
    # or lower-case) and Slack channel ids (Slack channel ids start with a
    # literal uppercase "C" followed by 8+ uppercase alphanumerics — not just
    # a "C0..." prefix — kept case-sensitive on purpose so this branch doesn't
    # fire on ordinary lowercase words starting with c).
    "vault-id": re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        r"|\bC[A-Z0-9]{8,}\b"
    ),
}

# Optional, gitignored, per-clone wordlist of the actual sensitive proper
# nouns this project's real (private) use touches — a company name, a
# person's name, a private vault's folder name. See
# tools/denylist.local.txt.example for the format and how to make your own
# copy. Absent by default: a fresh public clone has no such file.
DENYLIST_PATH = Path(__file__).resolve().parent / "denylist.local.txt"


def load_denylist(path: Path = DENYLIST_PATH) -> re.Pattern | None:
    """Compile the local wordlist into one case-insensitive pattern, or None.

    Returns None — not an error, not a crash — when the file is missing.
    That is the normal state for anyone who cloned the public repo and has
    not made their own copy of tools/denylist.local.txt.example yet.
    """
    if not path.is_file():
        return None
    terms = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not terms:
        return None
    return re.compile("(?i)" + "|".join(re.escape(t) for t in terms))


def active_patterns(denylist_path: Path = DENYLIST_PATH) -> dict[str, re.Pattern]:
    """The shape PATTERNS, plus a 'denylist' category if a local wordlist exists."""
    patterns = dict(PATTERNS)
    denylist = load_denylist(denylist_path)
    if denylist is not None:
        patterns["denylist"] = denylist
    return patterns


# path -> categories exempted there, or ["*"] for the whole file.
# Every entry needs a reason. An allowlist without reasons becomes a place to
# hide things.
ALLOW: dict[str, list[str]] = {
    # This scanner's own source necessarily quotes the literal regex text
    # that defines the /Users/ path shape it looks for -- "/Users/" followed
    # immediately by the character-class syntax "[^" happens to satisfy that
    # same shape (>=1 non-slash, non-space character after "/Users/"). A
    # scanner inevitably contains a fragment of what it blocks; that is why
    # this narrow, single-category exemption exists instead of rewording the
    # regex into something fragile. (personal-path only -- everything else in
    # this file is generic shape code and prose, and is expected to scan clean.)
    "tools/check-purity.py": ["personal-path"],
    # Its DIRTY test fixtures are deliberately shaped like real personal
    # paths / internal IPs / vault ids so the test can prove each pattern
    # still fires -- see the file's own docstring. Every fixture is
    # synthetic (fake IPs, all-zero UUIDs, invented paths); none is real.
    "tools/tests/test_check_purity.py": ["personal-path", "internal-ip", "vault-id"],
    # A worked example in prose, already anonymised to a generic `<you>`
    # placeholder rather than a real username -- not a leaked path.
    "_System/stop-hook.md": ["personal-path"],
    # Hook tests use synthetic session ids as fixtures (deadbeef/abcdef01/...).
    # They are not real ids — they are shaped like them on purpose.
    "_System/hooks/tests/test_distill_catchup.py": ["vault-id"],
    "_System/hooks/tests/test_distill_detach.py": ["vault-id"],
    # brain-watchdog.py's toast-notification code quotes the fixed, publicly
    # documented Windows AppUserModelID for the PowerShell Start Menu
    # shortcut. It is UUID-shaped but identical on every Windows install —
    # not a vault/personal/company identifier.
    "_System/hooks/brain-watchdog.py": ["vault-id"],
    # The copyright line is the one place a real name belongs by design (MIT
    # requires attribution) — not a leak. The standard MIT disclaimer
    # paragraph is also in all-caps verbatim, which trips the Slack-channel-id
    # shape check on ordinary long all-caps words in license boilerplate
    # (e.g. copyright, connection) — false positives on legal text, not ids.
    "LICENSE": ["vault-id"],
    # The one hit here is the real, public GitHub username in this repo's own
    # clone URL and companion-guide link — the repo's real address, not
    # private data about anyone. Only relevant when a maintainer's own local
    # denylist.local.txt happens to contain a term that is a substring of it.
    "README.md": ["denylist"],
}

SKIP_DIRS = (".git/",)


def scan_text(text: str, patterns: dict[str, re.Pattern] | None = None) -> list[tuple[str, str]]:
    """Return [(category, matched_text), ...] for one blob of text."""
    if patterns is None:
        patterns = active_patterns()
    hits: list[tuple[str, str]] = []
    for cat, rx in patterns.items():
        for m in rx.finditer(text):
            hits.append((cat, m.group(0)))
    return hits


def scan_file(
    path: Path,
    allow: list[str] | None = None,
    patterns: dict[str, re.Pattern] | None = None,
) -> list[tuple[int, str, str]]:
    """Return [(lineno, category, matched_text), ...] for a file."""
    allow = allow or []
    if "*" in allow:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if patterns is None:
        patterns = active_patterns()
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for cat, matched in scan_text(line, patterns):
            if cat in allow:
                continue
            out.append((i, cat, matched))
    return out


def tracked_files() -> list[str]:
    r = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=False)
    return [f for f in r.stdout.split("\n") if f and not f.startswith(SKIP_DIRS)]


def main(argv: list[str]) -> int:
    if "--paths" in argv:
        files = argv[argv.index("--paths") + 1:]
    else:
        files = tracked_files()

    patterns = active_patterns()
    total = 0
    for f in files:
        p = Path(f)
        if not p.is_file():
            continue
        for lineno, cat, matched in scan_file(p, ALLOW.get(f.replace("\\", "/"), []), patterns):
            print(f"{f}:{lineno}: [{cat}] {matched!r}", file=sys.stderr)
            total += 1

    if total:
        print(f"\ncheck-purity: {total} contamination(s) in {len(files)} file(s).", file=sys.stderr)
        print("This repo is public. Fix before committing.", file=sys.stderr)
        return 1
    print(f"check-purity: clean ({len(files)} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
