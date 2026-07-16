#!/usr/bin/env python3
"""
brain-watchdog: the out-of-band witness for the distill/health pipeline.

Runs from Task Scheduler (at logon), NEVER from a Claude session. That is the
entire point. Every row of _System/health.md's failure-mode coverage map
assumes brain-health.py runs at session start -- and on 2026-07-15 it came out
that the hook registration itself had been broken for weeks (`cmd /c` losing
its switch to MSYS path conversion; cmd.exe started interactively, printed a
banner, exited 0, and Claude Code dutifully reported "hook success"). A
watchdog that lives inside the thing it watches cannot report its own absence.
This one lives outside the session entirely.

Checks (all read-only, no LLM, target < 2s):

  1. hook registration sane   -- settings.json still uses `cmd //c`. The single
     -slash form is the exact 2026-07-15 bug; catch it the day it reappears
     instead of three weeks later.
  2. sessions ran but hooks left no trace -- the general form of that failure,
     and the one that survives whatever the next broken-registration looks
     like. A *finished* session newer than every SessionEnd trace means the
     hooks never fired. Deliberately NOT "no distills lately": going a few days
     without a session is normal and would train you to ignore this.

     Trace = max(canary mtime, distill.log mtime), and it needs both:
       - canary alone is not enough. distill spawns `claude -p` children, whose
         own SessionEnd fires the canary too (verified 2026-07-15: all 14 canary
         lines that day were children, not user sessions). So if distill broke
         but the canary registration survived, the canary would keep growing
         from... nothing, actually — no distill means no children — but the
         reverse hole is real: the canary keeps growing off user sessions while
         distill is dead, and this check would call that healthy.
       - distill.log alone is not enough either: it stays silent for children by
         design (CLAUDE_DISTILL_CHILD recursion guard), and silent when distill
         itself is the thing that never ran.
     Together they answer "did *anything* on SessionEnd run, and did distill
     specifically leave a mark" — distill-session.py logs on every exit path it
     has (skip/trivial/failed/wrote), so a live distill always moves that mtime.
  3. device hooks match the tracked source -- `_System/hooks/*.py` landing via
     git pull pushes nothing to `~/.claude/hooks/`; re-running the installer is
     a manual step nobody is reminded of (2026-07-15: 9 days stale, brain-sync
     .py and brain-decay.py missing outright).

Notifies by Windows toast (WinRT via powershell) -- no token, no network, so
the alarm has fewer ways to die than the thing it is watching. Silent when
healthy, exactly like brain-health.

Always appends one line to ~/.claude/logs/watchdog.log, healthy or not, so
"did the watchdog itself run?" is answerable without trusting the watchdog.
"""
from __future__ import annotations

import datetime
import filecmp
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# A session transcript untouched this long is treated as ended, not in-progress.
SETTLE_S = 30 * 60
# Allowance between a session ending and the canary line landing.
GRACE_S = 2 * 3600

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
PROJECTS = CLAUDE_DIR / "projects"
HOOKS_LIVE = CLAUDE_DIR / "hooks"
LOG = CLAUDE_DIR / "logs" / "watchdog.log"
DISTILL_LOG = CLAUDE_DIR / "logs" / "distill.log"
CANARY = Path(r"C:\tmp\sessionend-canary.log")


def resolve_brain() -> Path:
    """Same precedence as _brain_lib/brain-health: explicit override, then the
    standard var, then a home-relative fallback (never a device-specific path)."""
    for var in ("BRAIN_VAULT", "CLAUDE_DISTILL_VAULT"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    return HOME / "brain"


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass  # a watchdog must never raise on its way out


def check_hook_registration(warn: list) -> None:
    if not SETTINGS.exists():
        warn.append("settings.json 자체가 없음 — 훅이 등록될 곳이 없다")
        return
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception as e:
        warn.append(f"settings.json 파싱 실패 ({e.__class__.__name__}) — 훅 등록 상태 확인 불가")
        return

    hooks = data.get("hooks") or {}
    cmds = []
    for event in ("SessionStart", "SessionEnd"):
        for block in hooks.get(event) or []:
            for h in block.get("hooks") or []:
                cmds.append((event, str(h.get("command", ""))))

    for name, event in (("brain-health", "SessionStart"), ("distill-session", "SessionEnd")):
        hit = [c for e, c in cmds if e == event and name in c]
        if not hit:
            warn.append(f"{event} 훅에 {name} 등록이 사라짐 — 파이프라인이 아예 안 돈다")
            continue
        for c in hit:
            # `cmd //c` is required: Claude Code runs hooks through MSYS sh,
            # which rewrites a lone /c into a Windows path and drops the switch.
            if re.search(r"(?<!/)/c(?=\s)", c):
                warn.append(
                    f"{name} 훅이 'cmd /c'로 되돌아감 (슬래시 1개) — 2026-07-15과 똑같은 고장. "
                    f"cmd //c 로 고칠 것"
                )
                continue
            # ...and the argument must reach sh quoted. Unquoted, sh eats the
            # backslashes and cmd.exe is handed `C:UsersUSER...` -- then exits 0,
            # so this fails exactly as silently as the single-slash form did.
            # (The 2026-07-15 sequel: the first repair fixed the slash and left
            # this one in, and hand-testing missed it because the tester wrapped
            # the path in quotes that the real registration does not have.)
            m = re.search(r"cmd\s+//c\s+(\S.*)$", c)
            if m:
                arg = m.group(1).strip()
                if not arg.startswith(('"', "'")) and "\\" in arg:
                    warn.append(
                        f"{name} 훅 경로가 따옴표 없이 등록됨 — sh가 백슬래시를 먹어 "
                        f'cmd가 못 찾는다. cmd //c "<경로>" 형태로 고칠 것'
                    )


def check_sessions_vs_hook_trace(warn: list) -> None:
    if not PROJECTS.exists():
        return
    now = time.time()
    newest_finished = 0.0
    for p in PROJECTS.glob("*/*.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if now - m > SETTLE_S and m > newest_finished:
            newest_finished = m
    if not newest_finished:
        return  # no finished session yet -- nothing to conclude

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime if p.exists() else 0.0
        except OSError:
            return 0.0

    canary_m, distill_m = mtime(CANARY), mtime(DISTILL_LOG)
    trace_m = max(canary_m, distill_m)  # see module docstring: neither alone suffices
    if newest_finished > trace_m + GRACE_S:
        fmt = lambda t: (
            datetime.datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "한 번도 없음"
        )
        warn.append(
            f"세션은 끝났는데(마지막 {fmt(newest_finished)}) SessionEnd 훅 흔적이 없음 "
            f"(canary: {fmt(canary_m)} / distill.log: {fmt(distill_m)}) — 증류가 안 돌고 있다"
        )


def check_hook_drift(warn: list, brain: Path) -> None:
    src = brain / "_System" / "hooks"
    if not src.is_dir():
        return
    drift = []
    for f in sorted(src.glob("*.py")):
        live = HOOKS_LIVE / f.name
        if not live.exists() or not filecmp.cmp(f, live, shallow=False):
            drift.append(f.name)
    if drift:
        warn.append(
            f"이 기기 훅이 정본과 다름: {', '.join(drift)} "
            f"— brain에서 install-stop-hook.ps1 재실행 필요"
        )


def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&apos;")
    )


def toast(lines: list) -> bool:
    body = esc("\n".join(f"- {w}" for w in lines))[:900]
    xml = (
        '<toast scenario="reminder"><visual><binding template="ToastGeneric">'
        "<text>brain 파이프라인 이상</text>"
        f"<text>{body}</text>"
        "</binding></visual></toast>"
    )
    app_id = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml(@'
{xml}
'@)
$t = New-Object Windows.UI.Notifications.ToastNotification $x
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($t)
"""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        return p.returncode == 0
    except Exception:
        return False


def main() -> None:
    brain = resolve_brain()
    warn: list = []
    try:
        check_hook_registration(warn)
        check_sessions_vs_hook_trace(warn)
        check_hook_drift(warn, brain)
    except Exception as e:  # a broken check must not silence the whole watchdog
        warn.append(f"watchdog 점검 중 예외: {e!r}")

    if not warn:
        log("ok")
        return

    shown = toast(warn)
    log(f"WARN({len(warn)}) toast={'shown' if shown else 'FAILED'} :: " + " | ".join(warn))
    # stdout matters only when run by hand; Task Scheduler discards it.
    print("[brain-watchdog] 경고:")
    for w in warn:
        print(" -", w)
    if not shown:
        print("(토스트 표시 실패 — 알림 경로 자체가 죽었다)")


if __name__ == "__main__":
    main()
