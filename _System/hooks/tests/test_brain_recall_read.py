#!/usr/bin/env python3
"""Guards brain-recall-read.py: only an actual read counts as a recall.

Why this file exists (2026-09-28):
recall used to be bumped when brain-search merely *returned* a hit, so the
recall rate measured exposure. The hook now counts reads from Read and Bash
(Claude reads with sed/cat as often as with Read). Invariants checked here:
  - Read / Bash (absolute, Git Bash /d/..., relative-to-cwd, quoted) all count
  - the same file counts once per session, again in a new session
  - journals get frontmatter write-back, Knowledge does not
  - paths outside the vault and non-.md files are ignored
  - garbage input never raises (exit 0, no output)

Run (no framework): python _System/hooks/tests/test_brain_recall_read.py
Exit 0 = pass.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "brain-recall-read.py"
FM = "---\ntitle: t\nrecall_count: 0\nstrength: 1\nlast_recalled: 2026-01-01\n---\nbody\n"


def run(vault: Path, db: Path, event) -> subprocess.CompletedProcess:
    env = dict(os.environ, CLAUDE_DISTILL_VAULT=str(vault), BRAIN_USAGE_DB=str(db))
    data = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run([sys.executable, str(HOOK)], input=data, env=env,
                          capture_output=True, text=True, encoding="utf-8")


def fm(p: Path, key: str) -> str:
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def reads(db: Path) -> list:
    con = sqlite3.connect(db)
    rows = con.execute("SELECT path, session_id, tool FROM reads ORDER BY rowid").fetchall()
    con.close()
    return rows


def main() -> int:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "brain"
        for d in ("_System", "Decisions", "Knowledge"):
            (vault / d).mkdir(parents=True)
        j = vault / "Decisions" / "2026-09-01-a.md"
        j2 = vault / "Decisions" / "2026 spaced.md"
        k = vault / "Knowledge" / "k.md"
        for p in (j, j2, k):
            p.write_text(FM, encoding="utf-8")
        outside = Path(td) / "other.md"
        outside.write_text(FM, encoding="utf-8")
        db = Path(td) / "usage.sqlite"

        def check(cond, msg):
            if not cond:
                fails.append(msg)

        # 1. Read of a journal -> counted + frontmatter bumped
        r = run(vault, db, {"tool_name": "Read", "session_id": "s1", "cwd": td,
                            "tool_input": {"file_path": str(j)}})
        check(r.returncode == 0 and r.stdout == "", "hook must exit 0 silently")
        check(fm(j, "recall_count") == "1" and fm(j, "strength") == "2",
              f"journal write-back: {fm(j, 'recall_count')}/{fm(j, 'strength')}")
        check(fm(j, "last_recalled") != "2026-01-01", "last_recalled not reset")

        # 2. same file, same session, via Bash -> not counted again
        run(vault, db, {"tool_name": "Bash", "session_id": "s1", "cwd": td,
                        "tool_input": {"command": f"sed -n 1,5p {j.as_posix()}"}})
        check(fm(j, "recall_count") == "1", "same session must count once")

        # 3. new session, relative path from cwd -> counted
        run(vault, db, {"tool_name": "Bash", "session_id": "s2", "cwd": str(vault),
                        "tool_input": {"command": "cat Decisions/2026-09-01-a.md | head"}})
        check(fm(j, "recall_count") == "2", "relative Bash path in new session")

        # 4. quoted path with a space
        run(vault, db, {"tool_name": "Bash", "session_id": "s2", "cwd": td,
                        "tool_input": {"command": f'head -5 "{j2.as_posix()}"'}})
        check(fm(j2, "recall_count") == "1", "quoted path with space")

        # 5. Git Bash /x/... form (Windows only)
        if sys.platform == "win32":
            gb = "/" + k.drive[0].lower() + k.as_posix()[2:]
            run(vault, db, {"tool_name": "Bash", "session_id": "s3", "cwd": td,
                            "tool_input": {"command": f"cat {gb}"}})
            check(any(p == "Knowledge/k.md" and s == "s3" for p, s, _ in reads(db)),
                  "Git Bash path form")

        # 6. Knowledge read: logged, frontmatter untouched
        run(vault, db, {"tool_name": "Read", "session_id": "s4", "cwd": td,
                        "tool_input": {"file_path": str(k)}})
        check(fm(k, "recall_count") == "0", "Knowledge frontmatter must not change")
        check(any(p == "Knowledge/k.md" and s == "s4" for p, s, _ in reads(db)),
              "Knowledge read must be logged")

        # 7. outside vault / non-md / other tools / garbage -> ignored, no crash
        n = len(reads(db))
        run(vault, db, {"tool_name": "Read", "session_id": "s5", "cwd": td,
                        "tool_input": {"file_path": str(outside)}})
        run(vault, db, {"tool_name": "Read", "session_id": "s5", "cwd": td,
                        "tool_input": {"file_path": str(vault / "_System")}})
        run(vault, db, {"tool_name": "Write", "session_id": "s5", "cwd": td,
                        "tool_input": {"file_path": str(j)}})
        cat = vault / "Decisions" / "_catalog.md"
        cat.write_text(FM, encoding="utf-8")
        run(vault, db, {"tool_name": "Read", "session_id": "s5", "cwd": td,
                        "tool_input": {"file_path": str(cat)}})
        check(fm(cat, "recall_count") == "0", "_catalog.md must not be bumped")
        check(len(reads(db)) == n, "outside/non-md/Write/_index files must be ignored")
        for bad in ("", "not json", json.dumps({"tool_name": "Read", "tool_input": "x"})):
            r = run(vault, db, bad)
            check(r.returncode == 0 and r.stdout == "", f"garbage input {bad!r}")

    if fails:
        print("FAIL\n  - " + "\n  - ".join(fails))
        return 1
    print("PASS brain-recall-read (7 groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
