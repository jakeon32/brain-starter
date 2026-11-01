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

if sys.platform == "win32":
    # The warnings below contain em-dashes, and a legacy console codepage (cp949
    # here) raises UnicodeEncodeError on the first one — which killed the watchdog
    # while it was printing the alarm it exists to raise (2026-07-22).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# A session transcript untouched this long is treated as ended, not in-progress
# -- UNLESS a live claude process still owns it (see live_transcripts). Idle is
# not ended: 2026-08-12 a session left open across two days sat untouched for
# hours at a time, was counted as "finished", and produced five consecutive
# hourly false alarms. A warning that is always on is a warning nobody reads.
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
# 2026-08-10: 이 경로가 Windows 하드코딩이라 macOS 에서는 canary가 **구조적으로**
# 존재할 수 없었다. 아래 trace 판정이 distill.log 한쪽 다리로만 서 있었다는 뜻이다
# (모듈 docstring이 "둘 다 필요하다"고 적어둔 바로 그 판정). 이제 모든 OS에서
# 같은 home 기준 경로를 쓴다 — 두 설치기가 이 경로로 canary 를 등록한다.
CANARY = Path(os.environ.get(
    "BRAIN_CANARY",
    str(Path.home() / ".claude" / "logs" / "sessionend-canary.log"),
))


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
                # Exec form ({"command": "<pythonw>", "args": [launcher, script]})
                # keeps the script name in args, not command. Reading command alone
                # made this check report a healthy install as "hook is gone"
                # (2026-07-22; the installer's own idempotency check reads both).
                parts = [str(h.get("command", ""))]
                parts += [str(a) for a in (h.get("args") or [])]
                cmds.append((event, " ".join(parts)))

    for name, event in (("brain-health", "SessionStart"), ("distill-session", "SessionEnd")):
        hit = [c for e, c in cmds if e == event and name in c]
        if not hit:
            warn.append(f"{event} 훅에 {name} 등록이 사라짐 — 파이프라인이 아예 안 돈다")
            continue
        for c in hit:
            # The two checks below are about the old `cmd //c` launcher only. Exec
            # form has no shell in between, so neither failure mode can occur --
            # and testing for them there would only produce false alarms.
            if "cmd" not in c:
                continue
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


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# 세션 프로세스가 아닌 claude 바이너리 용도들 — 이것들은 트랜스크립트를 갖지 않는다.
_NOT_A_SESSION = ("--chrome-native-host", "mcp-server", "--chrome-devtools")


def _claude_procs() -> list[tuple[str, str]] | None:
    """살아 있는 claude 프로세스의 (pid, cmdline). 못 구하면 None."""
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'claude%'\" | "
                  "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            sep = "\t"
        else:
            proc = subprocess.run(["ps", "-eo", "pid=,command="],
                                  capture_output=True, text=True, timeout=20)
            sep = None
        if proc.returncode != 0:
            return None
    except Exception:
        return None

    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(sep) if sep else line.partition(" ")
        cmd = cmd.strip()
        if not pid.strip().isdigit() or not cmd:
            continue
        head = cmd.split()[0]
        looks_like_cli = (
            Path(head).name in ("claude", "claude.exe")
            or "/claude/versions/" in head or "\\claude\\versions\\" in head
        )
        if not looks_like_cli or any(x in cmd for x in _NOT_A_SESSION):
            continue
        out.append((pid.strip(), cmd))
    return out


def _proc_cwd(pid: str) -> Path | None:
    """POSIX만. Windows는 프로세스 cwd를 싸게 얻을 방법이 없어 None."""
    if os.name == "nt":
        return None
    linux = Path("/proc") / pid / "cwd"
    try:
        if linux.exists():
            return Path(os.readlink(linux))
    except OSError:
        pass
    try:
        proc = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                              capture_output=True, text=True, timeout=15)
        for line in proc.stdout.splitlines():
            if line.startswith("n"):
                return Path(line[1:])
    except Exception:
        pass
    return None


def live_transcripts() -> set[Path] | None:
    """지금 열려 있는 세션의 트랜스크립트 경로. 판별 실패는 None.

    **왜 파일만으로는 안 되는가** (2026-08-12 실측): 트랜스크립트는 턴마다 열고
    닫혀서 `lsof`에 잡히지 않고, 마지막 레코드 타입도 종료 여부를 말해주지 않는다
    (같은 날 표본: 종료된 세션이 `last-prompt`·`file-history-snapshot`, 살아 있는
    세션이 `user`). 남는 신호는 프로세스뿐이다.

    두 경로로 잡는다 —
      1. `--resume <uuid>` 는 커맨드라인에 세션 ID가 그대로 있다. 정확하다.
      2. 새로 연 세션은 ID가 안 드러난다(실측: `claude --channels …`). 그래서
         프로세스의 cwd → 프로젝트 폴더로 환산하고, **그 폴더에서 가장 최근
         트랜스크립트**를 그 세션의 것으로 본다.

    None을 돌려주면 호출부는 억제하지 않는다 — 오탐 하나보다 미탐 하나가 비싸다.
    """
    procs = _claude_procs()
    if procs is None:
        return None
    live: set[Path] = set()
    for pid, cmd in procs:
        for uuid in _UUID_RE.findall(cmd):
            for hit in PROJECTS.glob(f"*/{uuid}.jsonl"):
                live.add(hit)
        cwd = _proc_cwd(pid)
        if cwd is None:
            continue
        folder = PROJECTS / str(cwd).replace("/", "-").replace("\\", "-")
        if not folder.is_dir():
            continue
        newest = max(folder.glob("*.jsonl"),
                     key=lambda f: f.stat().st_mtime, default=None)
        if newest:
            live.add(newest)
    return live


def check_sessions_vs_hook_trace(warn: list, stats: dict) -> None:
    if not PROJECTS.exists():
        return
    now = time.time()
    live = live_transcripts()
    stats["live"] = len(live) if live is not None else -1
    newest_finished = 0.0
    for p in PROJECTS.glob("*/*.jsonl"):
        if live and p in live:
            continue  # 유휴일 뿐 살아 있다 — 아직 SessionEnd가 올 차례가 아니다
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


def _notify_mac(lines: list) -> bool:
    """macOS 알림 센터. 2026-08-10: 이 감시자는 Windows 토스트만 쏠 줄 알아서
    macOS 에서는 `toast=FAILED`로 조용히 끝났다 — 08-09에 다섯 시간 연속 경고가
    로그에만 쌓이고 사람에게는 한 번도 닿지 않았다. **감시자가 알리지 못하면
    감시하지 않는 것과 같다.**"""
    body = "\n".join(f"- {w}" for w in lines)[:400].replace('"', "'").replace("\\", "")
    script = (f'display notification "{body}" '
              f'with title "brain 파이프라인 이상" sound name "Basso"')
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=30)
        return p.returncode == 0
    except Exception:
        return False


def toast(lines: list) -> bool:
    if sys.platform == "darwin":
        return _notify_mac(lines)
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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.returncode == 0
    except Exception:
        return False


def main() -> None:
    brain = resolve_brain()
    warn: list = []
    stats: dict = {}
    try:
        check_hook_registration(warn)
        check_sessions_vs_hook_trace(warn, stats)
        check_hook_drift(warn, brain)
    except Exception as e:  # a broken check must not silence the whole watchdog
        warn.append(f"watchdog 점검 중 예외: {e!r}")

    # 살아 있다고 판정해 제외한 세션 수를 남긴다 — 억제가 맞았는지 사후에
    # 따질 수 있어야 한다. -1은 "프로세스를 못 읽어 억제하지 않았다".
    live = stats.get("live")
    suffix = "" if live in (0, None) else f" [live={live}]"

    if not warn:
        log("ok" + suffix)
        return

    shown = toast(warn)
    log(f"WARN({len(warn)}) toast={'shown' if shown else 'FAILED'}{suffix} :: " + " | ".join(warn))
    # stdout matters only when run by hand; Task Scheduler discards it.
    print("[brain-watchdog] 경고:")
    for w in warn:
        print(" -", w)
    if not shown:
        print("(토스트 표시 실패 — 알림 경로 자체가 죽었다)")


if __name__ == "__main__":
    main()
