#!/usr/bin/env python3
"""Guards the one invariant distill-catchup.py's promise rests on:

    a session id enters the done-set only after work FINISHED, never when it began.

Why this file exists (2026-07-16, _System/health.md §2026-07-16 sequel):
processed_ids() matched the bare substring "session <id>", which also appears in
the start lines ("distilling session X", "catchup: recovering session X"). A hook
killed mid-flight — the "Hook cancelled" seen at SessionEnd — leaves only its
start line, so the session was retired as done forever and this safety net
skipped precisely the sessions it exists to catch. 14 sessions distilled zero
notes; 4 were unrecoverable by the time it was noticed.

Run (no framework, no deps — the vault has no test runner):
    python _System/hooks/tests/test_distill_catchup.py
Exit 0 = pass. Run it after ANY edit to OUTCOME_RE.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "distill-catchup.py"

# Every log-line shape that carries a session id, taken from the real
# distill.log (2026-07-16 census of 267 attempts). Start lines first —
# those are the ones that must NOT retire a session.
LOG_FIXTURE = """\
[2026-07-16T11:54:10+09:00] distilling session aaaaaaaa (506 turns, 20348 chars)
[2026-07-16T09:09:38+09:00] catchup: recovering session bbbbbbbb (bbbbbbbb-1111-2222-3333-444444444444.jsonl)
[2026-07-16T11:45:16+09:00] distilling session cccccccc (72 turns, 16236 chars)
[2026-07-16T11:45:40+09:00] session cccccccc: wrote 4 note(s): ['x.md']
[2026-07-16T11:45:40+09:00] session cccccccc: brain-reindex.py spawned in background
[2026-07-16T11:45:40+09:00] session cccccccc: brain-catalog.py spawned in background
[2026-07-15T11:54:54+09:00] distilling session dddddddd (10 turns, 900 chars)
[2026-07-15T11:54:54+09:00] session dddddddd marked skip by distiller
[2026-07-15T13:00:00+09:00] skip: only 3 turns (<6) session eeeeeeee
[2026-07-15T14:00:00+09:00] distilling session ffffffff (400 turns, 9000 chars)
[2026-07-15T14:00:30+09:00] distillation failed for session ffffffff
[2026-07-16T11:46:07+09:00] skip: transcript missing at C:\\x\\projects\\p\\99999999-dead-beef-0000-000000000000.jsonl
"""

CASES = [
    # (session id, should_be_done, why)
    ("aaaaaaaa", False, "cancelled: only a 'distilling' start line -> MUST be retried"),
    ("bbbbbbbb", False, "catchup logged 'recovering' then died -> MUST be retried"),
    ("cccccccc", True,  "wrote notes -> done"),
    ("dddddddd", True,  "distiller judged it trivial -> done"),
    ("eeeeeeee", True,  "below MIN_TURNS -> done"),
    ("ffffffff", False, "explicit failure -> retryable (STALE_DAYS bounds it)"),
    ("99999999", False, "transcript gone; glob won't offer it, so must not pollute done"),
]


def main() -> int:
    if not HOOK.exists():
        print(f"FAIL: cannot find {HOOK}")
        return 1

    spec = importlib.util.spec_from_file_location("catchup", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["catchup"] = mod
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "distill.log"
        log_path.write_text(LOG_FIXTURE, encoding="utf-8")
        mod.LOG = log_path
        done = mod.processed_ids()

    failures = []
    for sid, want, why in CASES:
        got = sid in done
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {sid}  done={got!r:5} want={want!r:5}  {why}")
        if not ok:
            failures.append(sid)

    print()
    if failures:
        print(f"FAILED {len(failures)}/{len(CASES)}: {failures}")
        print("A start line must never retire a session. See health.md §2026-07-16 sequel.")
        return 1
    print(f"ALL {len(CASES)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
