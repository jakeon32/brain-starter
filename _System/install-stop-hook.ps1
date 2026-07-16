# install-stop-hook.ps1 - set up the brain distill + health-check hooks on a Windows device.
#
# Windows counterpart of install-stop-hook.sh. Idempotent. Per-device (run once
# after cloning brain, and again any time _System/hooks/*.py changes). Does:
#   1. Copies every _System/hooks/*.py -> %USERPROFILE%\.claude\hooks\
#      (hash-compare, only touches files that actually changed).
#   2. Generates two launchers with the brain path + interpreter baked in:
#      - distill-session.cmd (SessionEnd hook)
#      - brain-health.cmd    (SessionStart hook)
#      Both pin a python interpreter carrying the ML stack (sqlite_vec,
#      sentence-transformers) — bare `python` can resolve to an unrelated
#      venv per-shell (2026-06-11 postmortem: this exact drift silently broke
#      distillation for 3+ days). Auto-detects Python 3.10; override with
#      -Python or $env:BRAIN_PYTHON if your ML-stack interpreter lives elsewhere.
#   3. Backs up settings.json, then patches BOTH the SessionEnd and
#      SessionStart hook entries to call the matching launcher.
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
$HookFiles = @(
    "distill-session.py",
    "brain-health.py",
    "brain-catalog.py",
    "brain-reindex.py",
    "brain-search.py",
    "brain-consolidate.py",
    "brain-decay.py",
    "brain-sync.py",
    "brain-watchdog.py",
    "distill-catchup.py",
    "_brain_lib.py"
)

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
    # npm shim only: upgrade .cmd shim to the underlying exe if present
    if ($ClaudeBin -like "*.cmd") {
        $npmDir = Split-Path -Parent $cmdInfo.Source
        $exe = Join-Path $npmDir "node_modules\@anthropic-ai\claude-code\bin\claude.exe"
        if (Test-Path -LiteralPath $exe) { $ClaudeBin = (Resolve-Path -LiteralPath $exe).Path }
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
$DistillCmd  = Join-Path $HookDir "distill-session.cmd"
$HealthCmd   = Join-Path $HookDir "brain-health.cmd"
$WatchdogCmd = Join-Path $HookDir "brain-watchdog.cmd"
$SyncCmd     = Join-Path $HookDir "brain-sync-push.cmd"

$distillLauncher = @"
@echo off
rem Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
set "CLAUDE_DISTILL_VAULT=$Brain"
set "CLAUDE_DISTILL_CLAUDE_BIN=$ClaudeBin"
"$PythonBin" "%~dp0distill-session.py"
"@
$healthLauncher = @"
@echo off
rem Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
set "CLAUDE_DISTILL_VAULT=$Brain"
"$PythonBin" "%~dp0brain-health.py"
"@
# Watchdog runs from Task Scheduler at logon, not from a session — see step 6.
$watchdogLauncher = @"
@echo off
rem Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
set "CLAUDE_DISTILL_VAULT=$Brain"
"$PythonBin" "%~dp0brain-watchdog.py"
"@
# Pushes the scope:shared layer at SessionEnd. brain-health already pulls at
# SessionStart, so without this a device only ever receives — see step 5.
$syncLauncher = @"
@echo off
rem Auto-generated by install-stop-hook.ps1 - do not hand-edit; re-run installer.
set "CLAUDE_DISTILL_VAULT=$Brain"
"$PythonBin" "%~dp0brain-sync.py" --push
"@

if ($DryRun) {
    Say "[dry-run] would write launcher: $DistillCmd"
    Say $distillLauncher
    Say "[dry-run] would write launcher: $HealthCmd"
    Say $healthLauncher
    Say "[dry-run] would write launcher: $WatchdogCmd"
    Say $watchdogLauncher
    Say "[dry-run] would write launcher: $SyncCmd"
    Say $syncLauncher
} else {
    Set-Content -LiteralPath $DistillCmd -Value $distillLauncher -Encoding ASCII
    Say "launcher written: $DistillCmd"
    Set-Content -LiteralPath $HealthCmd -Value $healthLauncher -Encoding ASCII
    Say "launcher written: $HealthCmd"
    Set-Content -LiteralPath $WatchdogCmd -Value $watchdogLauncher -Encoding ASCII
    Say "launcher written: $WatchdogCmd"
    Set-Content -LiteralPath $SyncCmd -Value $syncLauncher -Encoding ASCII
    Say "launcher written: $SyncCmd"
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
# The launcher is invoked as `cmd //c` — the doubled slash is required, not a
# typo. Claude Code runs hook commands through its bundled sh (Git Bash/MSYS)
# on Windows, whose argument conversion rewrites a lone `/c` into a Windows
# path (`C:\...`). The switch is then gone, cmd.exe starts interactively,
# prints its banner, exits 0 — and Claude Code reports "hook success" while the
# hook never ran. `//c` is the MSYS escape that survives as `/c`.
# (2026-07-15: this silently disabled distill + health for weeks. See
# health.md §2026-07-15 postmortem.)
#
# The launcher path must reach sh quoted, or sh eats its backslashes and cmd.exe
# gets `C:UsersUSER...`. The quoting is composed in python below, NOT here:
# Windows PowerShell 5.1 strips embedded double quotes when passing arguments to
# a native exe, so `"cmd //c `"$HealthCmd`""` silently arrives unquoted.
$pyPatch = @'
import json, sys, os

settings_path, hook_type, marker, launcher = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
command = 'cmd //c "%s"' % launcher

if os.path.exists(settings_path):
    with open(settings_path, "r", encoding="utf-8") as f:
        data = json.load(f)
else:
    data = {}

hooks = data.setdefault("hooks", {})
bucket = hooks.setdefault(hook_type, [])

block = None
for b in bucket:
    if isinstance(b, dict) and b.get("matcher", "") == "":
        block = b
        break
if block is None:
    block = {"matcher": "", "hooks": []}
    bucket.append(block)

inner = block.setdefault("hooks", [])

replaced = False
for h in inner:
    if isinstance(h, dict) and h.get("type") == "command" \
       and marker in str(h.get("command", "")):
        h["command"] = command
        replaced = True
if not replaced:
    inner.append({"type": "command", "command": command})

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"settings.json {hook_type} hook set to:")
print("  " + command)
'@

if ($DryRun) {
    Say "[dry-run] would set settings.json SessionEnd command == cmd //c `"$DistillCmd`""
    Say "[dry-run] would set settings.json SessionStart command == cmd //c `"$HealthCmd`""
    Say "[dry-run] would set settings.json SessionEnd command == cmd //c `"$SyncCmd`""
} else {
    $tmp = [System.IO.Path]::GetTempFileName()
    Set-Content -LiteralPath $tmp -Value $pyPatch -Encoding UTF8
    try {
        & $PythonBin $tmp $Settings "SessionEnd" "distill-session" $DistillCmd
        if ($LASTEXITCODE -ne 0) { throw "settings.json SessionEnd patch failed (python exit $LASTEXITCODE)" }
        & $PythonBin $tmp $Settings "SessionStart" "brain-health" $HealthCmd
        if ($LASTEXITCODE -ne 0) { throw "settings.json SessionStart patch failed (python exit $LASTEXITCODE)" }
        # The shared layer syncs both ways or it doesn't sync: brain-health pulls
        # at SessionStart, so without this push a device only ever receives. The
        # design (docs/plans/2026-07-13-refined-layer-cross-device-sync-design.md
        # §Push) called for this from the start; it was never wired, and
        # scope:shared content piled up on this device without ever reaching
        # any other device -- including notes meant to stop a mistake from
        # repeating.
        & $PythonBin $tmp $Settings "SessionEnd" "brain-sync" $SyncCmd
        if ($LASTEXITCODE -ne 0) { throw "settings.json SessionEnd sync patch failed (python exit $LASTEXITCODE)" }
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
Say "  schtasks /create /tn brain-consolidate-weekly /sc weekly /d SUN /st 04:00 /tr `"'$PythonBin' '$HookDir\brain-consolidate.py' --auto`""
Say "   brain-health will remind you once the journal has enough files and this"
Say "   was never run (see _System/consolidate.md)."
Say ""
Say "2. watchdog (the out-of-band witness — nothing else can tell you the hooks"
Say "   themselves stopped running; see _System/health.md 2026-07-15 postmortem):"
Say "  schtasks /create /tn brain-watchdog /sc onlogon /tr `"cmd /c '$WatchdogCmd'`""
Say "   Nothing reminds you about this one — by construction it is what would"
Say "   have done the reminding. Verify with: schtasks /query /tn brain-watchdog"
