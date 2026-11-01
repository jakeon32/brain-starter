#!/usr/bin/env python3
"""
Session-start catch-up: deterministically recover any session that the
SessionEnd/Stop distill hook failed to capture.

Why this exists:
  On Windows CLI 2.1.140 the SessionEnd/Stop command hooks fire unreliably
  (observed 0/5). Relying on auto-fire => sessions silently drop out of the
  long-term brain. This script is the *primary* defense line: run at the
  very start of every session (runbook_behavior STEP 0B) it scans every
  transcript, diffs against distill.log's processed-id set, and feeds each
  missing 6+turn session into distill-session.py via an OS-level stdin
  redirect (subprocess stdin=fileobj — NOT a shell pipe, which is the
  failure mode documented in brain note distill-manual-recovery-via-file-redirect).

Guarantees: as long as a *next* session ever starts, the previous one is
recovered. Hook liveness is irrelevant. (If no next session starts, work
has ended, so it does not matter.)

That guarantee rests entirely on processed_ids() keying off *completion*
rather than *attempt*. It did not, until 2026-07-16: the done-set regex also
matched the "distilling session X" start line, so a hook killed mid-flight
("Hook cancelled" at SessionEnd) retired its session permanently and this
script skipped exactly the sessions it exists to catch — 14 of them, zero
notes each. If you touch OUTCOME_RE, the invariant to preserve is: a session
id enters the done-set only after work finished, never when it began.
See _System/health.md §2026-07-16 sequel.

Safety:
  - Newest transcript = the live current session -> excluded (or pass its
    id as argv to exclude explicitly).
  - <6 line transcripts are trivial/empty -> skipped silently (distill
    would skip them anyway; avoids log spam).
  - Sessions older than STALE_DAYS are marked stale in distill.log once and
    never retried (prevents first-run flood over full history).
  - MAX_PER_RUN caps work per invocation.
  - distill-session.py uses `claude -p --bare` so no recursive SessionStart.

Concurrency (2026-08-07): this loop *looks* sequential -- subprocess.run blocks
-- but distill-session.py detaches itself the moment it is invoked (see
_respawn_detached there: it exists because the SessionEnd hook's process tree is
torn down mid-LLM-call). So run() returned in ~0.2s and the loop fired all
MAX_PER_RUN workers at once; each worker's `claude` grandchildren allocate their
own consoles, and a backlog of 8 sessions flashed 8 cmd windows across the
user's screen. Fix, in two parts:
  1. This script self-detaches ONCE into a single background runner, so session
     start still returns immediately (the pre-existing latency contract).
  2. The runner sets CLAUDE_DISTILL_DETACHED=1 on each child, which makes
     distill-session.py skip its own detach and run inline -- restoring the
     blocking semantics this loop was written to rely on. One worker, one
     console, at a time.
The hook's own detach is untouched; it still needs it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

HOOK_DIR = Path(__file__).parent
DISTILL = HOOK_DIR / "distill-session.py"
LOG = Path(os.environ.get(
    "CLAUDE_DISTILL_LOG",
    str(Path.home() / ".claude" / "logs" / "distill.log"),
))
PROJECTS = Path.home() / ".claude" / "projects"
TMP = Path(os.environ.get("TEMP", os.environ.get("TMP", "C:/tmp")))
MAX_PER_RUN = int(os.environ.get("CLAUDE_CATCHUP_MAX", "8"))
STALE_DAYS = int(os.environ.get("CLAUDE_CATCHUP_STALE_DAYS", "3"))
# A transcript touched within this window may be a *live* session in another
# project/window. Skip it this run; once it goes quiet it is recovered on a
# later run (deferred recovery still => zero loss).
ACTIVE_QUIET_SEC = int(os.environ.get("CLAUDE_CATCHUP_ACTIVE_QUIET_SEC", "900"))
MIN_LINES = 6  # cheap prefilter; distill-session.py does the real turn check
# Home-relative fallback (not a device-specific absolute path) — only used if
# the caller (brain-health.py's spawn) somehow didn't already set the env var.
DEFAULT_VAULT = str(Path.home() / "brain")
# 2026-06-11: npm path died after native-CLI migration; distill-session.py
# now self-resolves via PATH when a pinned path goes stale. Mirror that here
# instead of hardcoding one device's username-specific install path.
# 2026-07-18: prefer the native .exe. shutil.which() returns claude.CMD on
# Windows, and the .cmd shim fails with rc=1 "the system cannot find the file
# specified" whenever stdin is a pipe -- which is how the worker feeds the
# transcript. See _upgrade_shim() in distill-session.py for the full postmortem.
def _default_claude_bin() -> str:
    found = shutil.which("claude") or shutil.which("claude.cmd")
    if found and os.name == "nt" and Path(found).suffix.lower() != ".exe":
        exe = (Path(found).parent / "node_modules" / "@anthropic-ai"
               / "claude-code" / "bin" / "claude.exe")
        if exe.exists():
            return str(exe)
    return found or "claude"


DEFAULT_CLAUDE_BIN = _default_claude_bin()


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] catchup: {msg}\n")


# Only a *terminal outcome* retires a session. Start lines ("distilling
# session X", "catchup: recovering session X") must NOT count: a hook that is
# cancelled mid-flight leaves its start line as the sole trace, and treating
# that as done drops the session out of this safety net permanently -- which
# is exactly what happened to 14 sessions before 2026-07-16 (see
# _System/health.md). Explicit failures stay retryable; STALE_DAYS caps them.
OUTCOME_RE = re.compile(
    r"session ([0-9a-f]{8}): wrote "                          # notes written
    r"|session ([0-9a-f]{8}) marked skip"                     # trivial per distiller
    r"|skip: only \d+ turns \(<\d+\) session ([0-9a-f]{8})"   # below MIN_TURNS
)


def processed_ids() -> set:
    """8-char session ids distill.log has *finished* with (wrote / judged
    trivial / too few turns). Deliberately excludes in-flight start lines --
    see OUTCOME_RE above."""
    if not LOG.exists():
        return set()
    txt = LOG.read_text(encoding="utf-8", errors="replace")
    return {sid for m in OUTCOME_RE.finditer(txt) for sid in m.groups() if sid}


def _spawn_runner() -> bool:
    """Re-launch this script detached as the single sequential runner.

    Returns True if it took over. Fail-open: any problem here means we just do
    the recovery inline in this process (slower for the caller, still correct).
    """
    try:
        env = dict(os.environ)
        env["CLAUDE_CATCHUP_RUNNER"] = "1"
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=env,
            **kwargs,
        )
        log("sequential runner spawned; returning")
        return True
    except Exception as e:  # noqa: BLE001 - fail-open by design
        log(f"runner spawn failed ({e!r}); recovering inline")
        return False


def main() -> int:
    dry = "--dry" in sys.argv[1:]
    # Current live session: env is authoritative (CLI 2.1.140 exports it).
    # argv override is allowed for manual use / testing.
    cur = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            cur = a
            break
    cur8 = cur[:8]

    done = processed_ids()
    now = datetime.now().timestamp()
    stale_cut = now - STALE_DAYS * 86400
    active_cut = now - ACTIVE_QUIET_SEC

    candidates = []
    n_done = n_stale = n_active = n_trivial = n_self = 0
    for p in PROJECTS.glob("*/*.jsonl"):
        sid8 = p.stem[:8]
        if cur8 and sid8 == cur8:
            n_self += 1
            continue
        if sid8 in done:
            n_done += 1
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt < stale_cut:
            n_stale += 1  # pre-distill history; silently ignore (no log spam)
            continue
        if mt > active_cut:
            n_active += 1  # may be a live session elsewhere; defer to a later run
            continue
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                nlines = sum(1 for _ in f)
        except OSError:
            continue
        if nlines < MIN_LINES:
            n_trivial += 1
            continue
        candidates.append((mt, p, sid8))

    candidates.sort(reverse=True)  # newest missing first
    summary = (
        f"scan: missing={len(candidates)} "
        f"(done={n_done} stale={n_stale} active={n_active} "
        f"trivial={n_trivial} self={n_self})"
    )

    if dry:
        print(f"[dry] {summary}")
        for _, p, sid8 in candidates[:MAX_PER_RUN]:
            print(f"[dry] WOULD RECOVER  {sid8}  {p.parent.name}/{p.name}")
        if len(candidates) > MAX_PER_RUN:
            print(f"[dry] (+{len(candidates) - MAX_PER_RUN} more, capped at {MAX_PER_RUN})")
        return 0

    if not candidates:
        log(summary)
        return 0
    log(summary)

    # Hand the whole queue to one detached runner and return, so the caller
    # (session start) is not blocked for MAX_PER_RUN x ~30s. The runner does
    # the recovery strictly one at a time -- see the Concurrency note up top.
    if not os.environ.get("CLAUDE_CATCHUP_RUNNER") and _spawn_runner():
        return 0

    env = dict(os.environ)
    env.setdefault("CLAUDE_DISTILL_VAULT", DEFAULT_VAULT)
    env.setdefault("CLAUDE_DISTILL_CLAUDE_BIN", DEFAULT_CLAUDE_BIN)
    # Suppress the child's own detach so subprocess.run below actually waits.
    env["CLAUDE_DISTILL_DETACHED"] = "1"
    # ...and drop any inherited hand-off path: with DETACHED=1 the child calls
    # _cleanup_payload_file(), which unlinks whatever CLAUDE_DISTILL_PAYLOAD_FILE
    # points at. Ours is still open as the child's stdin, so the parent deletes
    # it after the run instead.
    env.pop("CLAUDE_DISTILL_PAYLOAD_FILE", None)

    n = 0
    for _, p, sid8 in candidates[:MAX_PER_RUN]:
        sid = p.stem
        payload = {
            "session_id": sid,
            "transcript_path": str(p),
            "cwd": p.parent.name,  # informational frontmatter only
            "stop_hook_active": False,
        }
        pf = TMP / f"distill-catchup-{sid8}.json"
        pf.write_text(json.dumps(payload), encoding="utf-8")
        log(f"recovering session {sid8} ({p.name})")
        try:
            with pf.open("rb") as stdin_f:
                subprocess.run(
                    [sys.executable, str(DISTILL)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdin=stdin_f,
                    env=env,
                    timeout=300,
                )
        except subprocess.TimeoutExpired:
            log(f"recover timeout session {sid8}")
        except Exception as e:  # noqa: BLE001 - fail-silent by design
            log(f"recover error session {sid8}: {e!r}")
        finally:
            try:
                pf.unlink()  # child can't: it is still open as the child's stdin
            except OSError:
                pass
        n += 1

    log(f"catchup done, processed {n} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
