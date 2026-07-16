#!/usr/bin/env bash
# install-stop-hook.sh — set up the brain distill + health-check hooks on this device.
#
# Idempotent. Safe to re-run. Per-device (run once after cloning brain, and
# again any time _System/hooks/*.py changes). Does:
#   1. Copies every _System/hooks/*.py -> ~/.claude/hooks/ (only files that changed).
#   2. Patches ~/.claude/settings.json so BOTH hooks run with
#      CLAUDE_DISTILL_VAULT pointed at this brain (env-wrapped commands):
#        - SessionEnd  -> distill-session.py
#        - SessionStart -> brain-health.py
#   3. Backs up settings.json before touching it; verifies at the end.
#
# Usage:
#   ./_System/install-stop-hook.sh [--brain DIR] [--dry-run]
#
#   --brain DIR   brain vault path (default: the repo this script lives in)
#   --dry-run     print what would happen, change nothing
#
# Note: the hook event is SessionEnd, not Stop — Claude Code's `Stop` hook does
# not fire on /exit or user interrupt (see
# Decisions/2026-05-18-stop-hook-to-sessionend-migration.md). An older version
# of this installer patched "Stop", which a fresh device would never have fired.
#
# Why this exists: the hooks themselves are device-local (~/.claude/hooks/),
# not synced. This installer + the tracked _System/hooks/*.py scripts make a
# fresh device reproducible from a single `git clone` + one command.

set -euo pipefail

DRY_RUN=0
BRAIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --brain) BRAIN="$2"; shift 2 ;;
    --dry-run|-n) DRY_RUN=1; shift ;;
    --help|-h) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 64 ;;
  esac
done

# Resolve repo root = dir containing this script's parent (_System/..)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
[[ -z "$BRAIN" ]] && BRAIN="$REPO_ROOT"

HOOKS_SRC_DIR="$SCRIPT_DIR/hooks"
HOOK_DST_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"

# Every pipeline script that must exist device-local for distill / health /
# search / consolidate / catchup to function — not just the hook entry points.
HOOK_FILES=(
  distill-session.py
  brain-health.py
  brain-catalog.py
  brain-reindex.py
  brain-search.py
  brain-consolidate.py
  brain-decay.py
  brain-sync.py
  brain-watchdog.py
  distill-catchup.py
  _brain_lib.py
)

say() { printf '%s\n' "$*"; }
run() { if [[ "$DRY_RUN" == 1 ]]; then say "[dry-run] $*"; else eval "$*"; fi; }

say "brain     : $BRAIN"
say "hooks src : $HOOKS_SRC_DIR"
say "hooks dst : $HOOK_DST_DIR"
say "settings  : $SETTINGS"
say ""

[[ -d "$BRAIN" ]] || { echo "ERROR: brain dir missing: $BRAIN" >&2; exit 2; }

# Check (not install) the ML stack brain-search/reindex/consolidate need.
# These run detached with stderr suppressed (brain-health.py's spawn), so a
# missing import fails completely silently — search/consolidate would never
# work on a fresh device and nothing would ever say why. Surface it loudly,
# once, at install time instead.
ML_PYTHON=""
PY_HINT="python3"   # what to print in copy-paste instructions below
if python3 -c "import sqlite_vec, sentence_transformers" 2>/dev/null; then
  ML_PYTHON="$(command -v python3)"
  PY_HINT="$ML_PYTHON"
  say "ML stack   : OK ($ML_PYTHON)"
else
  say ""
  say "WARNING: python3 is missing sqlite_vec and/or sentence_transformers."
  say "         brain-search / brain-reindex / brain-consolidate will silently fail"
  say "         (they run detached with no visible error) until you run:"
  say "           pip3 install sqlite-vec sentence-transformers"
  say "         Distillation (Decisions/Mistakes/Patterns) does not need this — only search/consolidate do."
  say "         Re-run this installer afterwards to pin the interpreter and build the index."
  say ""
fi

# 1. Install every pipeline script (hash-compare, only touch what changed)
run "mkdir -p \"$HOOK_DST_DIR\""
for name in "${HOOK_FILES[@]}"; do
  src="$HOOKS_SRC_DIR/$name"
  dst="$HOOK_DST_DIR/$name"
  if [[ ! -f "$src" ]]; then
    say "WARNING: source missing, skipped: $src"
    continue
  fi
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    say "$name already up to date."
  else
    run "cp \"$src\" \"$dst\""
    run "chmod +x \"$dst\""
    say "$name installed/updated."
  fi
done

# 2. Patch settings.json (backup first), via python3 for safe JSON handling
if [[ -f "$SETTINGS" ]]; then
  BK="$SETTINGS.pre-stop-hook-install-$(date +%Y%m%d-%H%M%S)"
  run "cp \"$SETTINGS\" \"$BK\""
  say "settings backup: $BK"
fi

DISTILL_DST="$HOOK_DST_DIR/distill-session.py"
HEALTH_DST="$HOOK_DST_DIR/brain-health.py"
SYNC_DST="$HOOK_DST_DIR/brain-sync.py"
DISTILL_CMD="env CLAUDE_DISTILL_VAULT=$BRAIN $DISTILL_DST"
HEALTH_CMD="env CLAUDE_DISTILL_VAULT=$BRAIN $HEALTH_DST"
SYNC_CMD="env CLAUDE_DISTILL_VAULT=$BRAIN $SYNC_DST --push"

patch_hook() {
  # $1 = hook_type (SessionEnd|SessionStart), $2 = marker substring, $3 = command
  if [[ "$DRY_RUN" == 1 ]]; then
    say "[dry-run] would ensure settings.json $1 hook command == \"$3\""
    return
  fi
  python3 - "$SETTINGS" "$1" "$2" "$3" <<'PY'
import json, sys, os

settings_path, hook_type, marker, command = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

if os.path.exists(settings_path):
    with open(settings_path, "r", encoding="utf-8") as f:
        data = json.load(f)
else:
    data = {}

hooks = data.setdefault("hooks", {})
bucket = hooks.setdefault(hook_type, [])

# Find an existing block with a matcher == "" (the global one), else add.
block = None
for b in bucket:
    if isinstance(b, dict) and b.get("matcher", "") == "":
        block = b
        break
if block is None:
    block = {"matcher": "", "hooks": []}
    bucket.append(block)

inner = block.setdefault("hooks", [])

# Replace any command matching the marker; keep unrelated hooks intact.
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
PY
}

patch_hook "SessionEnd" "distill-session.py" "$DISTILL_CMD"
patch_hook "SessionStart" "brain-health.py" "$HEALTH_CMD"
# The shared layer syncs both ways or it doesn't sync: brain-health pulls at
# SessionStart, so without this push a device only ever receives. The design
# (docs/plans/2026-07-13-refined-layer-cross-device-sync-design.md §Push) called
# for this from the start; it was never wired, and scope:shared content piled
# up on this device without ever reaching any other device -- including notes
# meant to stop a mistake from repeating.
patch_hook "SessionEnd" "brain-sync.py" "$SYNC_CMD"

# 3. Pin the interpreter that actually has the ML stack, so a bare `python
#    brain-search.py` typed by a human self-heals instead of dying in whatever
#    venv the shell happens to be in (2026-06-11 / 2026-07-06 postmortems).
if [[ -n "$ML_PYTHON" ]]; then
  run "printf '%s\n' \"$ML_PYTHON\" > \"$HOOK_DST_DIR/.brain-python\""
  say "interpreter pinned: $ML_PYTHON"
fi

# 4. Build the search index. Copying brain-reindex.py without ever running it
#    leaves a fresh device at zero rows, where search answers "nothing found" —
#    indistinguishable from "you have no such note" and just as quietly wrong.
if [[ -n "$ML_PYTHON" ]]; then
  if [[ "$DRY_RUN" == 1 ]]; then
    say "[dry-run] would build the search index (brain-reindex.py)"
  else
    say ""
    say "Building search index (first run walks the whole vault — this takes a minute)..."
    if CLAUDE_DISTILL_VAULT="$BRAIN" "$ML_PYTHON" "$HOOK_DST_DIR/brain-reindex.py"; then
      say "index built."
    else
      say "WARNING: index build failed. search will return nothing until you run:"
      say "  CLAUDE_DISTILL_VAULT=$BRAIN $ML_PYTHON $HOOK_DST_DIR/brain-reindex.py"
    fi
  fi
fi

say ""
say "Done. Verify:"
say "  1. Run a real Claude Code session (>= 6 substantive turns), then end it."
say "  2. tail -n 5 ~/.claude/logs/distill.log"
say "  3. ls $BRAIN/Decisions $BRAIN/Mistakes $BRAIN/Patterns"
say "     -> new YYYY-MM-DD-*.md should appear here, under the vault you just installed into — not in whatever other notes vault you keep elsewhere."
say "  4. Start a fresh session and confirm brain-health prints nothing when healthy"
say "     (or a warning block if something's actually broken)."
say "  5. $PY_HINT $HOOK_DST_DIR/brain-search.py \"any topic you have notes on\""
say "     -> should return hits. Nothing at all means the index never built."
say ""
say "Two things this installer does NOT set up — both are cron entries, one-time"
say "per device (cron cannot be re-run idempotently, so they are left to you"
say "rather than silently duplicated). Add with: crontab -e"
say ""
say "1. weekly consolidate (journal -> Knowledge/ wiki convergence):"
say "  0 4 * * 0 CLAUDE_DISTILL_VAULT=$BRAIN $PY_HINT $HOOK_DST_DIR/brain-consolidate.py --auto"
say "   brain-health will remind you once the journal has enough files and this"
say "   was never run (see _System/consolidate.md)."
say ""
say "2. watchdog (the out-of-band witness — nothing else can tell you the hooks"
say "   themselves stopped running; see _System/health.md 2026-07-15 postmortem):"
say "  0 * * * * CLAUDE_DISTILL_VAULT=$BRAIN $PY_HINT $HOOK_DST_DIR/brain-watchdog.py"
say "   Nothing reminds you about this one — by construction it is what would"
say "   have done the reminding. Verify with: crontab -l | grep watchdog"
say ""
say "   Note: macOS may require granting cron Full Disk Access (System Settings"
say "   -> Privacy & Security) before it can read the vault."
say ""
