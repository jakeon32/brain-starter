#!/usr/bin/env python3
"""Guards the SessionEnd hook's return-fast invariant:

    the hook hands off and returns; it NEVER blocks on the `claude -p` call.

Why (2026-07-16, _System/health.md §2026-07-16 sequel): distill used to run the
LLM round-trip inline, so the hook blocked ~24s (p90 36s) at session exit. Session
teardown killed 20 of 267 attempts mid-flight — the user saw "SessionEnd hook ...
failed: Hook cancelled" and the session's memory was lost. Detaching removed the
exposure entirely. If a future edit makes the hook wait again, that whole class of
loss comes back, so this test fails loudly instead.

Uses the MIN_TURNS skip path: 3 turns means the worker exits before ever calling
the LLM, so this test costs no tokens and writes no notes. Log and vault are
redirected to a temp dir — the real distill.log is never touched.

Run:  python _System/hooks/tests/test_distill_detach.py
Exit 0 = pass.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "distill-session.py"
BUDGET_SEC = 2.0  # generous: measured 0.08s. Inline would be ~24s.


def main() -> int:
    if not HOOK.exists():
        print(f"FAIL: cannot find {HOOK}")
        return 1

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        transcript = td / "t.jsonl"
        turns = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}},
            {"type": "user", "message": {"role": "user", "content": "bye"}},
        ]
        transcript.write_text(
            "\n".join(json.dumps(t) for t in turns), encoding="utf-8")

        payload = td / "p.json"
        payload.write_text(json.dumps({
            "session_id": "abcdef01-0000-0000-0000-000000000000",
            "transcript_path": str(transcript),
            "cwd": str(td),
            "stop_hook_active": False,
        }), encoding="utf-8")

        log = td / "distill.log"
        env = dict(
            os.environ,
            CLAUDE_DISTILL_LOG=str(log),
            CLAUDE_DISTILL_VAULT=str(td / "vault"),
            PYTHONIOENCODING="utf-8",
        )

        t0 = time.time()
        with payload.open("rb") as f:
            rc = subprocess.run([sys.executable, str(HOOK)], stdin=f, env=env).returncode
        elapsed = time.time() - t0

        # the detached worker needs a moment to log its own outcome
        deadline = time.time() + 30
        text = ""
        while time.time() < deadline:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if "only 3 turns" in text:
                break
            time.sleep(0.5)

    checks = [
        ("hook exits 0", rc == 0),
        (f"hook returns in <{BUDGET_SEC}s (measured {elapsed:.2f}s)", elapsed < BUDGET_SEC),
        ("hook logged the hand-off", "detached worker spawned" in text),
        ("detached worker actually ran (logged its own outcome)", "only 3 turns" in text),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")

    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(checks)}")
        print("The SessionEnd hook must not block on the LLM. See health.md §2026-07-16 sequel.")
        if text:
            print("--- captured log ---")
            print(text)
        return 1
    print(f"ALL {len(checks)} PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
