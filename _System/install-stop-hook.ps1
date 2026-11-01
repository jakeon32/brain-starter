# install-stop-hook.ps1 - set up the brain distill + health-check hooks on a Windows device.
#
# Windows counterpart of install-stop-hook.sh. Idempotent. Per-device (run once
# after cloning brain, and again any time _System/hooks/*.py changes). Does:
#   1. Copies every _System/hooks/*.py -> %USERPROFILE%\.claude\hooks\
#      (hash-compare, only touches files that actually changed).
#   2. Generates the windowless hook launcher (brain-hook-launch.py) with the
#      brain path + claude bin baked in, plus brain-watchdog.cmd for schtasks.
#      The launcher pins a python interpreter carrying the ML stack (sqlite_vec,
#      sentence-transformers) — bare `python` can resolve to an unrelated
#      venv per-shell (2026-06-11 postmortem: this exact drift silently broke
#      distillation for 3+ days). Auto-detects Python 3.10; override with
#      -Python or $env:BRAIN_PYTHON if your ML-stack interpreter lives elsewhere.
#   3. Backs up settings.json, then patches the SessionStart (brain-health) and
#      SessionEnd (distill-session, brain-sync) entries to EXEC-FORM pythonw.exe
#      calls through that launcher, so no console window flashes on Windows.
#
# Note: the hook event is SessionEnd, not Stop — Claude Code's `Stop` hook does
# not fire on /exit or user interrupt (see
# Decisions/2026-05-18-stop-hook-to-sessionend-migration.md). An older version
# of this installer patched "Stop", which a fresh device would never have
# fired.
#
# Usage:
#   pwsh/powershell -File _System\install-stop-hook.ps1 [-Brain DIR] [-Python PATH] [-DryRun]
#
#   -Brain DIR    brain vault path (default: the repo this script lives in)
#   -Python PATH  python interpreter with sqlite_vec + sentence-transformers
#                 installed (default: auto-detect Python 3.10, falls back to
#                 `python` on PATH with a warning)
#   -DryRun       print what would happen, change nothing

[CmdletBinding()]
param(
    [string]$Brain = "",
    [string]$Python = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir "..")).Path
if ([string]::IsNullOrWhiteSpace($Brain)) { $Brain = $RepoRoot }
$Brain = (Resolve-Path $Brain).Path

$HooksSrcDir = Join-Path $ScriptDir "hooks"
$HookDir     = Join-Path $env:USERPROFILE ".claude\hooks"
$Settings    = Join-Path $env:USERPROFILE ".claude\settings.json"

# Every pipeline script that must exist device-local for distill / health /
# search / consolidate / catchup to function — not just the hook entry points.
# 2026-09-01: 하드코딩 목록을 버리고 «정본 폴더를 그대로 읽는다».
# 목록 방식은 두 번 샜다 — 훅을 새로 만든 사람이 설치기까지 고쳐야 하는 구조였고, 매번 잊었다.
# 이제 _System/hooks/*.py 는 «있으면 배포된다» (tests/ 와 __pycache__ 는 하위 폴더라 자동 제외).
$HookFiles = @(Get-ChildItem -LiteralPath $HooksSrcDir -Filter *.py -File | Sort-Object Name | ForEach-Object { $_.Name })

function Say($m) { Write-Host $m }

Say "brain      : $Brain"
Say "hooks src  : $HooksSrcDir"
Say "hooks dst  : $HookDir"
Say "settings   : $Settings"
Say ""

if (-not (Test-Path -LiteralPath $Brain)) { throw "brain dir missing: $Brain" }

# Resolve claude binary (Windows npm shim is claude.cmd)
# Prefer the native claude.exe over the claude.cmd shim. The .cmd shim runs
# through cmd.exe (8191-char command-line cap) AND breaks when invoked with a
# piped stdin from a Python subprocess on Windows ("system cannot find the
# file specified"). The .exe is a native binary with neither limitation, so
# distill-session.py can pass the long transcript prompt via stdin reliably.
$ClaudeBin = "claude"
$cmdInfo = Get-Command claude -ErrorAction SilentlyContinue
if ($null -eq $cmdInfo) { $cmdInfo = Get-Command claude.cmd -ErrorAction SilentlyContinue }
if ($null -ne $cmdInfo -and $cmdInfo.Source) {
    $ClaudeBin = $cmdInfo.Source
    # npm shim only: upgrade the shim to the underlying exe if present.
    # NOTE (2026-07-18): under PowerShell, `Get-Command claude` resolves to the
    # .ps1 shim, not .cmd. A .ps1 cannot be exec'd by CreateProcess, so pinning
    # it made every SessionEnd distill die with OSError 193 ("not a valid Win32
    # application") for days. Match every npm shim flavour, not just .cmd.
    if ($ClaudeBin -match '\.(cmd|ps1|bat)$' -or [IO.Path]::GetExtension($ClaudeBin) -eq '') {
        $npmDir = Split-Path -Parent $cmdInfo.Source
        $exe = Join-Path $npmDir "node_modules\@anthropic-ai\claude-code\bin\claude.exe"
        if (Test-Path -LiteralPath $exe) { $ClaudeBin = (Resolve-Path -LiteralPath $exe).Path }
    }
    if ($ClaudeBin -match '\.(ps1|bat)$') {
        Say "WARN: claude bin resolved to a shim that cannot be exec'd ($ClaudeBin) and no claude.exe was found; distillation will fail."
    }
}
Say "claude bin : $ClaudeBin"

# Resolve a python interpreter carrying the ML stack (sqlite_vec,
# sentence-transformers). brain-health.py / brain-reindex.py / brain-search.py
# / brain-consolidate.py all need it; bare `python` is not reliable per-shell.
function Resolve-BrainPython {
    param([string]$Override)
    if ($Override) { return $Override }
    if ($env:BRAIN_PYTHON) { return $env:BRAIN_PYTHON }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        "C:\Python310\python.exe",
        (Join-Path ${env:ProgramFiles} "Python310\python.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $resolved = & py -3.10 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) { return $resolved.Trim() }
        } catch {}
    }
    $bare = Get-Command python -ErrorAction SilentlyContinue
    if ($bare) {
        Say "WARNING: no Python 3.10 install auto-detected; falling back to '$($bare.Source)'."
        Say "         If it lacks sqlite_vec/sentence-transformers, re-run with -Python <path>."
        return $bare.Source
    }
    throw "no python interpreter found on PATH; re-run with -Python <path-to-python.exe>"
}
$PythonBin = Resolve-BrainPython -Override $Python
Say "python bin : $PythonBin"

# Console-less twin used by the hook/schtasks launchers so SessionEnd/SessionStart
# and scheduled tasks don't flash a python console window on Windows. pythonw.exe
# ships next to python.exe in every CPython install; fall back if it's somehow absent.
$PythonWBin = $PythonBin -replace '(?i)python\.exe$', 'pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonWBin)) { $PythonWBin = $PythonBin }
Say "pythonw bin: $PythonWBin"

# Check (not install) the ML stack brain-search/reindex/consolidate need.
# These run detached with stderr suppressed (brain-health.py's spawn), so a
# missing import fails completely silently — search/consolidate would never
# work on a fresh device and nothing would ever say why. Surface it loudly,
# once, at install time instead.
$mlProbe = 1
try { & $PythonBin -c "import sqlite_vec, sentence_transformers" 2>$null; $mlProbe = $LASTEXITCODE } catch { $mlProbe = 1 }
if ($mlProbe -ne 0) {
    Say ""
    Say "WARNING: '$PythonBin' is missing sqlite_vec and/or sentence_transformers."
    Say "         brain-search / brain-reindex / brain-consolidate will silently fail"
    Say "         (they run detached with no visible error) until you run:"
    Say "           & `"$PythonBin`" -m pip install sqlite-vec sentence-transformers"
    Say "         Distillation (Decisions/Mistakes/Patterns) does not need this — only search/consolidate do."
    Say "         Re-run this installer afterwards to pin the interpreter and build the index."
}
Say ""

# 1. hooks dir
if (-not (Test-Path -LiteralPath $HookDir)) {
    if ($DryRun) { Say "[dry-run] mkdir $HookDir" } else { New-Item -ItemType Directory -Path $HookDir -Force | Out-Null }
}

# 2. copy every pipeline script, only if changed
foreach ($name in $HookFiles) {
    $src = Join-Path $HooksSrcDir $name
    if (-not (Test-Path -LiteralPath $src)) {
        Say "WARNING: source missing, skipped: $src"
        continue
    }
    $dst = Join-Path $HookDir $name
    $needCopy = $true
    if (Test-Path -LiteralPath $dst) {
        $a = (Get-FileHash -LiteralPath $src).Hash
        $b = (Get-FileHash -LiteralPath $dst).Hash
        if ($a -eq $b) { $needCopy = $false }
    }
    if ($needCopy) {
        if ($DryRun) { Say "[dry-run] copy $name -> $dst" }
        else { Copy-Item -LiteralPath $src -Destination $dst -Force; Say "$name installed/updated." }
    } else { Say "$name already up to date." }
}

# 3. (re)generate the launchers (brain + interpreter + claude bin baked in)
#
# Session hooks (SessionStart/SessionEnd) run WINDOWLESS: Claude Code spawns
# pythonw.exe in EXEC FORM (a settings.json hook with an "args" array) pointing at
# the shared launcher below. Exec form takes NO shell (no Git Bash/cmd), and
# pythonw.exe is a GUI-subsystem binary, so nothing flashes a conhost window on
# Windows. The launcher bakes the per-device env the pipeline scripts read, then
# runs the requested script in-process (runpy) so its stdin/stdout/exit reach
# Claude Code exactly as a direct `pythonw.exe <script>` call would.
#   Superseded the old `cmd //c "*.cmd"` launchers, which flashed a cmd.exe window
#   on every SessionStart/SessionEnd (2026-07-20). Exec form + args is documented
#   in Claude Code's hooks reference; matcher/behaviour unchanged.
$LaunchPy    = Join-Path $HookDir "brain-hook-launch.py"
$WatchdogCmd = Join-Path $HookDir "brain-watchdog.cmd"
$HiddenVbs   = Join-Path $HookDir "run-hidden.vbs"
$CanaryCmd   = Join-Path $HookDir "sessionend-canary.cmd"
$CanaryLog   = Join-Path $env:USERPROFILE ".claude\logs\sessionend-canary.log"   # brain-watchdog.py reads the same default (override both with BRAIN_CANARY)

$hookLauncher = @"
# Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
# Windowless hook launcher (see install-stop-hook.ps1 step 3 for the why).
# Usage: pythonw.exe brain-hook-launch.py <script.py> [script args...]
import os, sys, runpy
os.environ.setdefault("CLAUDE_DISTILL_VAULT", r"$Brain")
os.environ.setdefault("CLAUDE_DISTILL_CLAUDE_BIN", r"$ClaudeBin")
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)  # so the target's `import _brain_lib` resolves
if len(sys.argv) < 2:
    sys.stderr.write("brain-hook-launch.py: missing target script name\n")
    raise SystemExit(2)
_target = os.path.join(_here, sys.argv[1])
sys.argv = [_target] + sys.argv[2:]
runpy.run_path(_target, run_name="__main__")
"@

# Watchdog runs from Task Scheduler at logon, not from a session (see step 6), so
# it stays a .cmd: schtasks needs an env-setting wrapper and shows no window there.
$watchdogLauncher = @"
@echo off
rem Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
set "CLAUDE_DISTILL_VAULT=$Brain"
"$PythonWBin" "%~dp0brain-watchdog.py"
"@

# SessionEnd canary — the witness that says "the hook fired at all", independent of
# everything else. Deliberately uses NO python: the canary exists to survive a
# distill failure and testify, so sharing an interpreter with the thing it watches
# would make it fail in correlation (2026-07-15: a pythonw path problem took out
# every SessionEnd hook at once; nothing was left to notice).
#
# Windows shape is wscript -> run-hidden.vbs -> canary.cmd (the mac installer writes
# a plain shell line instead; same contract, different OS). Three candidates were
# weighed on 2026-08-13:
#   (a) pythonw exec-form  windowless, but re-couples the witness to python
#   (b) bare cmd one-liner python-free, but flashes a console window on every exit
#                          (the exact regression removed on 2026-07-20)
#   (c) wscript + VBS      python-free AND windowless — the only one satisfying both
# (c) had been running by hand on one device since 2026-08-10: 15 real
# SessionEnd lines across 3 days, no window reported. That is the evidence for
# baking it in, not a fresh guess.
# NOTE: body variables must NOT differ from the path variables by case alone —
# PowerShell variable names are case-INSENSITIVE, so `$hiddenVbs` and `$HiddenVbs`
# are the same variable. Naming the body `$hiddenVbs` silently overwrote the path
# and the dry-run showed the whole VBS source being passed to wscript as a filename.
$hiddenVbsBody = @"
' Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
' run-hidden.vbs - windowless exec wrapper.
' Usage: wscript.exe run-hidden.vbs "<command>" ["<arg>" ...]
' Why: console apps flash a window when launched from schtasks or a session hook.
'      wscript is a GUI-subsystem host, so Run(..., 0, False) shows nothing.
If WScript.Arguments.Count < 1 Then WScript.Quit 1
Dim sh, cmd, i
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    If i > 0 Then cmd = cmd & " "
    cmd = cmd & """" & WScript.Arguments(i) & """"
Next
sh.Run cmd, 0, False
"@

$canaryCmdBody = @"
@echo off
rem Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
rem SessionEnd canary - records only that the hook fired. No python on purpose
rem (see install-stop-hook.ps1 step 3). Window hiding is run-hidden.vbs's job.
if not exist "$(Split-Path -Parent $CanaryLog)" mkdir "$(Split-Path -Parent $CanaryLog)"
echo [%DATE% %TIME%] SessionEnd>> "$CanaryLog"
"@

if ($DryRun) {
    Say "[dry-run] would write launcher: $LaunchPy"
    Say $hookLauncher
    Say "[dry-run] would write launcher: $WatchdogCmd"
    Say $watchdogLauncher
    Say "[dry-run] would write launcher: $HiddenVbs"
    Say "[dry-run] would write launcher: $CanaryCmd  (log -> $CanaryLog)"
} else {
    Set-Content -LiteralPath $LaunchPy -Value $hookLauncher -Encoding ASCII
    Say "launcher written: $LaunchPy"
    Set-Content -LiteralPath $WatchdogCmd -Value $watchdogLauncher -Encoding ASCII
    Say "launcher written: $WatchdogCmd"
    Set-Content -LiteralPath $HiddenVbs -Value $hiddenVbsBody -Encoding ASCII
    Say "launcher written: $HiddenVbs"
    Set-Content -LiteralPath $CanaryCmd -Value $canaryCmdBody -Encoding ASCII
    Say "launcher written: $CanaryCmd"
}

# 4. backup settings.json
if (Test-Path -LiteralPath $Settings) {
    $bk = "$Settings.pre-stop-hook-install-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    if ($DryRun) { Say "[dry-run] backup $Settings -> $bk" }
    else { Copy-Item -LiteralPath $Settings -Destination $bk -Force; Say "settings backup: $bk" }
}

# 5. patch settings.json via python (safe JSON, idempotent). hook_type is the
# actual event name Claude Code fires (SessionEnd for distill, SessionStart
# for health) — NOT "Stop" (see decision note above).
#
# Each hook is written in EXEC FORM: {"type":"command","command":"<pythonw>",
# "args":["<brain-hook-launch.py>","<script.py>",...]}. Claude Code spawns the
# command directly with that argv and NO shell (per its hooks reference), so on
# Windows there is no Git Bash/cmd layer to flash a console window, and pythonw.exe
# (GUI subsystem) shows none either. This replaced the old shell-form
# `cmd //c "*.cmd"` command, whose cmd.exe wrapper flashed on every session
# start/end (2026-07-20). Exec form also sidesteps the 2026-07-15 `//c` MSYS
# quoting trap entirely — no shell means no argument rewriting (see health.md
# §2026-07-15 postmortem).
#
# argv to the patcher: settings, hook_type, marker, command, [args...]
# It matches an existing entry by `marker` in either the command OR any arg, so
# re-runs update in place whether the prior entry was shell-form or exec-form.
#
# NOTE (2026-08-13): the patcher scans EVERY matcher-"" block, not just the first.
# Claude Code accepts several blocks per event, and a hand-registered entry can sit
# in any of them — one device had the SessionEnd canary in a second block. Scanning
# only the first would have found nothing and appended a DUPLICATE that fires the
# canary twice per session. "Idempotent" has to mean idempotent against what is
# actually in the file, not against what this installer would have written.
$pyPatch = @'
import json, sys, os

settings_path, hook_type, marker = sys.argv[1], sys.argv[2], sys.argv[3]
command = sys.argv[4]
args = sys.argv[5:]

if os.path.exists(settings_path):
    with open(settings_path, "r", encoding="utf-8") as f:
        data = json.load(f)
else:
    data = {}

hooks = data.setdefault("hooks", {})
bucket = hooks.setdefault(hook_type, [])

# 2026-08-30: PostToolUse 는 도구 이름으로 걸러야 한다 — matcher 를 환경변수로
# 받는다(미지정이면 종전대로 "").
target_matcher = os.environ.get("HOOK_MATCHER", "")

def matches(h):
    if not (isinstance(h, dict) and h.get("type") == "command"):
        return False
    if marker in str(h.get("command", "")):
        return True
    return any(marker in str(a) for a in (h.get("args") or []))

# 1) replace in place wherever it already lives (any block)
replaced = False
for b in bucket:
    if not isinstance(b, dict):
        continue
    for h in (b.get("hooks") or []):
        if matches(h):
            h["command"] = command
            h["args"] = args
            replaced = True

# 2) only if it exists nowhere, append to the first matcher-"" block
if not replaced:
    block = None
    for b in bucket:
        if isinstance(b, dict) and b.get("matcher", "") == target_matcher:
            block = b
            break
    if block is None:
        block = {"matcher": target_matcher, "hooks": []}
        bucket.append(block)
    block.setdefault("hooks", []).append(
        {"type": "command", "command": command, "args": args})

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")

verb = "updated in place" if replaced else "added"
print(f"settings.json {hook_type} [{marker}] {verb}:")
print("  " + command + " " + " ".join(args))
'@

if ($DryRun) {
    Say "[dry-run] SessionStart brain-health   == `"$PythonWBin`" `"$LaunchPy`" brain-health.py"
    Say "[dry-run] SessionEnd  distill-session == `"$PythonWBin`" `"$LaunchPy`" distill-session.py"
    Say "[dry-run] SessionEnd  brain-sync      == `"$PythonWBin`" `"$LaunchPy`" brain-sync.py --push"
    Say "[dry-run] SessionEnd  canary          == wscript.exe `"$HiddenVbs`" `"$CanaryCmd`""
    Say "[dry-run] PostToolUse brain-recall-read == `"$PythonWBin`" `"$LaunchPy`" brain-recall-read.py  (matcher: Read|Bash)"
    Say "[dry-run] UserPromptSubmit brain-recall-inject == `"$PythonWBin`" `"$LaunchPy`" brain-recall-inject.py"
} else {
    $tmp = [System.IO.Path]::GetTempFileName()
    Set-Content -LiteralPath $tmp -Value $pyPatch -Encoding UTF8
    try {
        & $PythonBin $tmp $Settings "SessionEnd" "distill-session" $PythonWBin $LaunchPy "distill-session.py"
        if ($LASTEXITCODE -ne 0) { throw "settings.json SessionEnd patch failed (python exit $LASTEXITCODE)" }
        & $PythonBin $tmp $Settings "SessionStart" "brain-health" $PythonWBin $LaunchPy "brain-health.py"
        if ($LASTEXITCODE -ne 0) { throw "settings.json SessionStart patch failed (python exit $LASTEXITCODE)" }
        # The shared layer syncs both ways or it doesn't sync: brain-health pulls
        # at SessionStart, so without this push a device only ever receives. The
        # design (docs/plans/2026-07-13-refined-layer-cross-device-sync-design.md
        # §Push) called for this from the start; it was never wired, and by
        # 2026-07-16 six of fourteen scope:shared files existed on this device
        # only — including the one written to stop a rule being broken a 4th time.
        & $PythonBin $tmp $Settings "SessionEnd" "brain-sync" $PythonWBin $LaunchPy "brain-sync.py" "--push"
        if ($LASTEXITCODE -ne 0) { throw "settings.json SessionEnd sync patch failed (python exit $LASTEXITCODE)" }
        # The canary. Marker is the .cmd filename, which is what a hand-registered
        # entry on any device already carries in its args — so this updates those in
        # place instead of stacking a second one.
        & $PythonBin $tmp $Settings "SessionEnd" "sessionend-canary" "wscript.exe" $HiddenVbs $CanaryCmd
        if ($LASTEXITCODE -ne 0) { throw "settings.json SessionEnd canary patch failed (python exit $LASTEXITCODE)" }
        # brain-recall-read (PostToolUse). 2026-09-28 추가 — 회상은 검색 결과 노출이
        # 아니라 실제 읽기로 센다 (_System/decay.md §Recall write-back). 런처를 거쳐
        # vault 환경변수를 받는다. Claude 는 Read 만큼 sed/cat 으로도 읽으므로 Bash 포함.
        $env:HOOK_MATCHER = "Read|Bash"
        try {
            & $PythonBin $tmp $Settings "PostToolUse" "brain-recall-read" $PythonWBin $LaunchPy "brain-recall-read.py"
            if ($LASTEXITCODE -ne 0) { throw "settings.json PostToolUse recall patch failed (python exit $LASTEXITCODE)" }
        } finally {
            Remove-Item Env:HOOK_MATCHER -ErrorAction SilentlyContinue
        }
        # brain-recall-inject (UserPromptSubmit). 2026-09-28 추가 — 프롬프트마다 관련
        # brain 문서의 제목·경로를 알린다. 끄기: 환경변수 BRAIN_RECALL_INJECT=0.
        & $PythonBin $tmp $Settings "UserPromptSubmit" "brain-recall-inject" $PythonWBin $LaunchPy "brain-recall-inject.py"
        if ($LASTEXITCODE -ne 0) { throw "settings.json UserPromptSubmit inject patch failed (python exit $LASTEXITCODE)" }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

# 6. Pin the interpreter that actually has the ML stack, so a bare `python
#    brain-search.py` typed by a human self-heals instead of dying in whatever
#    venv the shell happens to be in (2026-06-11 / 2026-07-06 postmortems).
$MarkerPath = Join-Path $HookDir ".brain-python"
if ($DryRun) {
    Say "[dry-run] would pin interpreter in $MarkerPath -> $PythonBin"
} else {
    Set-Content -LiteralPath $MarkerPath -Value $PythonBin -Encoding ASCII
    Say "interpreter pinned: $PythonBin"
}

# 7. Build the search index. Copying brain-reindex.py without ever running it
#    leaves a fresh device at zero rows, where search answers "nothing found" —
#    indistinguishable from "you have no such note" and just as quietly wrong.
$ReindexPy = Join-Path $HookDir "brain-reindex.py"
if ($DryRun) {
    Say "[dry-run] would build the search index (brain-reindex.py)"
} elseif ($mlProbe -eq 0) {
    Say ""
    Say "Building search index (first run walks the whole vault - this takes a minute)..."
    $env:CLAUDE_DISTILL_VAULT = $Brain
    & $PythonBin $ReindexPy
    if ($LASTEXITCODE -eq 0) {
        Say "index built."
    } else {
        Say "WARNING: index build failed. search will return nothing until you run:"
        Say "  `$env:CLAUDE_DISTILL_VAULT='$Brain'; & '$PythonBin' '$ReindexPy'"
    }
}

Say ""
Say "Done. Verify:"
Say "  1. Run a real Claude Code session (>= 6 substantive turns), then end it."
Say "  2. Get-Content `"$env:USERPROFILE\.claude\logs\distill.log`" -Tail 5"
Say "  3. ls `"$Brain\Decisions`" `"$Brain\Mistakes`" `"$Brain\Patterns`""
Say "     -> new YYYY-MM-DD-*.md should appear here, under the vault you just installed into — not in whatever other notes vault you keep elsewhere."
Say "  4. Start a fresh session and confirm brain-health prints nothing when healthy"
Say "     (or a warning block if something's actually broken)."
Say "  5. & `"$PythonBin`" `"$HookDir\brain-search.py`" `"any topic you have notes on`""
Say "     -> should return hits. Nothing at all means the index never built."
Say ""
Say "Two things this installer does NOT set up — both are Task Scheduler tasks,"
Say "one-time per device (schtasks cannot be re-run idempotently, so they are"
Say "left to you rather than silently duplicated):"
Say ""
Say "1. weekly consolidate (journal -> Knowledge/ wiki convergence):"
Say "  schtasks /create /tn brain-consolidate-weekly /sc weekly /d SUN /st 14:00 /tr `"'$PythonBin' '$HookDir\brain-consolidate.py' --weekly`""
Say "   brain-health will remind you once the journal has enough files and this"
Say "   was never run (see _System/consolidate.md)."
Say ""
Say "2. watchdog (the out-of-band witness — nothing else can tell you the hooks"
Say "   themselves stopped running; see _System/health.md 2026-07-15 postmortem):"
Say "  schtasks /create /tn brain-watchdog /sc onlogon /tr `"cmd /c '$WatchdogCmd'`""
Say "   Nothing reminds you about this one — by construction it is what would"
Say "   have done the reminding. Verify with: schtasks /query /tn brain-watchdog"
