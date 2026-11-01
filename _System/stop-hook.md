---
title: Distillation pipeline — install contract and current reality
type: spec
status: confirmed
updated: 2026-11-01
---

# The distillation pipeline — what runs, and how to install it

> **개요 (한국어):** 세션 종료 시 자동 증류하는 파이프라인(훅 11개)의 실제 동작과,
> **새 기기 설치의 정본**. 설치는 `_System/install-stop-hook.ps1`/`.sh` 한 번이면 된다.
> 이벤트는 `SessionEnd`다 — `Stop`이 아니다(§Not `Stop`). 파일명이 `stop-hook`인 건
> 역사적 잔재다.

## What exists today

Two entries are wired globally in `~/.claude/settings.json` — both installed by
`install-stop-hook.ps1`/`.sh`, both env-wrapped so they point at *this* brain:

```jsonc
"hooks": {
  "SessionEnd":   [ { "matcher": "", "hooks": [{ "type": "command", "command": "…/distill-session" }] } ],
  "SessionStart": [ { "matcher": "", "hooks": [{ "type": "command", "command": "…/brain-health"    }] } ]
}
```

`SessionEnd` distills the session; `SessionStart` self-checks the pipeline. On
Windows both run in **exec form** — `"command": "…\pythonw.exe", "args":
["…\brain-hook-launch.py", "<script>.py"]` — so Claude Code spawns `pythonw.exe`
directly with no Git Bash/cmd shell and no flashing console window. The generated
`brain-hook-launch.py` bakes `CLAUDE_DISTILL_VAULT` (and the claude bin) in, then
runs the target script in-process. (2026-07-20; replaced the old
`cmd //c "*.cmd"` launchers, which flashed a cmd.exe window on every session
start/end.) The launcher being windowless is necessary but **not sufficient**:
`pythonw.exe` has no console, so any `git`/`schtasks` child the hook scripts spawn
gets a *new* console allocated and flashes its own window — a push alone is
add+diff+commit+push, i.e. several. Every such blocking call therefore passes
`creationflags=CREATE_NO_WINDOW` (`run_git` in `brain-sync.py`, the schtasks query
in `brain-health.py`); the detached background spawns already did. Authoritative
source for behaviour:
`~/.claude/hooks/distill-session.py` (tracked at `_System/hooks/`).

**The SessionEnd hook does not wait for the distillation.** Since 2026-07-16 the
script reads its payload, re-execs itself detached, and returns in ~0.08s; the
`claude -p` round-trip (~24s) finishes out-of-band. So an empty `distill.log`
right after exit is expected — the worker is still running. Do not "fix" a
launcher to block on it: blocking is what let session teardown kill distills
mid-flight ("Hook cancelled"), and the recovery net was silently dropping those
sessions. See [health.md](health.md) §2026-07-16 sequel.

> This section described a `Stop` hook — with a hard-coded `/Users/<you>/…`
> path — until 2026-07-16, long after the migration to `SessionEnd`. It is the
> likeliest origin of the stale `Stop` claim that then spread into other docs.

## What it actually writes (read this carefully)

The script writes to **three** category folders directly under the vault root, NOT under a `Distilled/` wrapper:

```
<VAULT>/Decisions/YYYY-MM-DD-<slug>.md
<VAULT>/Mistakes/YYYY-MM-DD-<slug>.md
<VAULT>/Patterns/YYYY-MM-DD-<slug>.md
```

There is **no `Sessions/` auto-write** in the current script. Any `Sessions/` folder content you find was written manually or by an older script — see [`../Sessions/_index.md`](../Sessions/_index.md).

The brain layout matches: `Decisions/`, `Mistakes/`, `Patterns/`, `Sessions/` all live at brain root.

## Per-file behavior

For each session that has ≥ `MIN_TURNS` (default 6) turns, the hook:

1. Reads the transcript JSONL.
2. Calls `claude -p` with a strict JSON schema, model `claude-sonnet-4-6`, effort `low`, no slash commands, no session persistence.
3. Receives up to 3 items per category (decisions / mistakes / patterns), or `skip: true` if trivial. Each item carries an `importance` score 1-10 (rubric in the prompt: 1 = session-local mundane, 10 = changes all future sessions).
4. **Importance gate (P1, 2026-07-12)**: items scoring ≤ `MIN_IMPORTANCE` (**default 5** — 2 until 2026-08-10, then 4, then 5 on 2026-08-28: at 2 nearly everything passed and 77% of journals were never recalled before they decayed; 5 keeps only items useful beyond the session) are dropped and logged, not written. Missing score fails open to 5. This is the first write-time quality gate against journal volume (~1,500/mo before it).
5. **Near-duplicate gate (2026-08-10)**: the new note's title is embedded and compared against the title vectors of every live journal in the search index. At cosine ≥ `DEDUP_COS` (default 0.82) nothing is written — the matched original gets `strength +1` and `last_recalled = today` instead, so a topic that keeps coming back decays slower rather than piling up copies. Fail-open: without the index or the ML stack the gate is silently off.
6. Writes each surviving item to a new `.md` with frontmatter:

```yaml
---
type: decisions      # or mistakes, patterns
date: YYYY-MM-DD
session: <session-id>
cwd: <working-directory>
auto-distilled: true
importance: 1-10     # write-time prior (Generative Agents poignancy adapted)
strength: 1          # P2 decay seed — brain-search bumps on recall
recall_count: 0
last_recalled: YYYY-MM-DD
tags: [...]
---

# <title>

<2-6 sentence summary>
```

7. On filename collision (same day, same slug) the note is **skipped**, not suffixed (2026-08-03 — the old `-2`, `-3`, … behaviour piled up near-identical files when a running session was summarised again).

Three more rules shape what gets written:

- `<brain-recall>` blocks injected by `brain-recall-inject.py` are stripped from the transcript, so titles the vault already holds are not re-distilled into new journals.
- **Incremental guard** (2026-08-03): when the payload comes from a `Stop` hook (`hook_event_name: Stop`), the session is skipped unless it grew by `MIN_TURN_DELTA` turns since its last distillation (state in `~/.claude/logs/distill-state.json`). `Stop` fires after every reply, so without this a long session re-summarised itself dozens of times. SessionEnd — the only event this skeleton's installer wires — catchup and hand runs always proceed.
- The prompt tells the model to write mistakes and patterns as **the rule to follow next time and when it applies** — not who pointed out what. Quoted remarks in internal notes leak into material other people read later (2026-11-01).

## Recursion guard

The hook sets `CLAUDE_DISTILL_CHILD=1` in the env when invoking `claude -p`, and exits immediately if it sees that var on entry. So the inner distillation call doesn't trigger another distillation.

## Configurable env vars

| Var | Default | Purpose |
|---|---|---|
| `CLAUDE_DISTILL_VAULT` | (none — installer bakes it in) | **Where output goes.** The old built-in fallback pointed into a separate, personal notes vault that no longer held any Claude-related content; the fallback is now `~/brain` |
| `CLAUDE_DISTILL_LOG` | `~/.claude/logs/distill.log` | Hook's own log file |
| `CLAUDE_DISTILL_CLAUDE_BIN` | `claude` | claude CLI binary |
| `CLAUDE_DISTILL_MODEL` | `claude-sonnet-4-6` | Distillation model |
| `CLAUDE_DISTILL_MIN_TURNS` | `6` | Skip if fewer turns |
| `CLAUDE_DISTILL_MIN_IMPORTANCE` | `5` | Drop items scored at or below this |
| `CLAUDE_DISTILL_DEDUP_COS` | `0.82` | Title-cosine at/above which a note is treated as a re-recording (recall bump instead of write) |
| `CLAUDE_DISTILL_MIN_TURN_DELTA` | `50` | `Stop`-hook payloads only: skip unless the session grew this many turns since its last distillation (`CLAUDE_DISTILL_FORCE=1` overrides) |
| `CLAUDE_DISTILL_MAX_TURNS` | `120` | Last N turns included in prompt |
| `CLAUDE_DISTILL_MAX_CHARS_PER_TURN` | `2000` | Truncate per turn |
| `CLAUDE_DISTILL_TIMEOUT` | `180` | Seconds for the distill subprocess |

## Reproducible setup (per device, one command)

The pipeline itself is device-local (`~/.claude/hooks/`), not synced. To make
a fresh device reproducible, this repo carries the full source and an
installer:

- `_System/hooks/*.py` — tracked source of truth for **every** pipeline
  script. Since 2026-09-01 the installer deploys whatever `*.py` sits in that
  folder instead of a hard-coded list — the list leaked twice, each time a new
  hook was written and the installer was not updated. (Until 2026-07-03 only 3 of these
  were tracked — a fresh clone could not run search/consolidate/catchup at
  all. See [`health.md`](health.md) §Gap closed. This list said "all 8" until
  2026-07-16, omitting decay/sync/**watchdog** — the one that exists to notice
  when the rest die.)
- `_System/install-stop-hook.sh` (Mac/Linux) / `_System/install-stop-hook.ps1`
  (Windows) — idempotent installer

On any device, after `git clone`/`git pull` of brain (the vault root can live
anywhere — this doc assumes `~/brain`, but the exact drive/folder will differ
per device):

```sh
cd <your brain>
./_System/install-stop-hook.sh            # add --dry-run first to preview
```

```powershell
powershell -File _System\install-stop-hook.ps1   # add -DryRun first to preview
```

The installer:
1. Probes for a python carrying the ML stack (`sqlite_vec`, `sentence_transformers`)
   and warns loudly if there is none — search/consolidate fail *silently* without it
2. Copies every `_System/hooks/*.py` → `~/.claude/hooks/` (hash-compare, only touches what changed)
3. Backs up `~/.claude/settings.json`
4. Patches the hook entries to commands pointed at this brain:
   - `SessionEnd` → distill-session (writes Decisions/Mistakes/Patterns)
   - `SessionStart` → brain-health (pipeline self-check; on Windows through the
     generated `brain-hook-launch.py` launcher with `CLAUDE_DISTILL_VAULT` baked in)
   - `SessionEnd` → brain-sync `--push` (the shared layer needs both directions)
   - `SessionEnd` → **the canary**, one line per session end into
     `~/.claude/logs/sessionend-canary.log` with no Python and no vault in the
     path, so it survives the failures it exists to witness. `brain-watchdog`
     needs it as the second leg of "did the hooks fire". macOS/Linux: a plain
     `sh -c 'date … >> …'`. Windows: `wscript.exe run-hidden.vbs
     sessionend-canary.cmd` — a bare `cmd` line would flash a console window at
     every exit, and a `pythonw` entry would re-couple the witness to the
     interpreter it must outlive (2026-08-13).
   - `PostToolUse` (matcher `Read|Bash`) → `brain-recall-read.py` — counts a
     recall only when Claude actually reads a vault `.md` file, once per session
     ([decay.md §Recall write-back](decay.md)).
   - `UserPromptSubmit` → `brain-recall-inject.py` — adds the titles and paths
     (never bodies) of up to 3 relevant notes to each prompt, BM25 only (~0.1 s),
     behind a keyword-rarity gate and a score floor (`BRAIN_RECALL_SUM_MIN`,
     default 12). Turn it off with `BRAIN_RECALL_INJECT=0`.
   (replaces an existing matching command in place — in any block of that event —
   and leaves unrelated hooks alone)
5. Pins that interpreter in `~/.claude/hooks/.brain-python`, so a human typing
   `python brain-search.py` self-heals out of whatever venv their shell is in
   (added 2026-07-16; the hard-coded `_PINNED_PYTHONS` list only ever healed the
   device it was written on)
6. Builds the search index (added 2026-07-16 — it used to copy `brain-reindex.py`
   and never run it, leaving a fresh device at zero rows, where search answers
   "nothing found" indistinguishably from "you have no such note")
7. Prints verification steps

**Still not covered — two scheduled tasks, one-time per device.** The installer
prints both at the end rather than creating them, because neither `schtasks` nor
`cron` can be re-run idempotently and silently duplicating them is worse:
`brain-consolidate-weekly` and `brain-watchdog`. brain-health nags about the
first once the journal is big enough; **nothing nags about the second**, since
by construction the watchdog is what would do the nagging.

**Not `Stop`:** the hook event is `SessionEnd`, not `Stop` — Claude Code's
`Stop` hook does not fire on `/exit` or user interrupt. An earlier installer
version patched `Stop`, which a fresh device would never have fired — fixed.

Re-running is safe (idempotent). Pass `--brain DIR` / `-Brain DIR` to target a
non-default brain path. On Windows, pass `-Python PATH` (or set
`$env:BRAIN_PYTHON`) if the auto-detected interpreter lacks `sqlite_vec` /
`sentence-transformers` (see [`health.md`](health.md) device notes on
interpreter pinning).

### Manual alternative (if you don't want the installer)

Either set `export CLAUDE_DISTILL_VAULT="<your brain>"` in `~/.zshrc`, or
hand-edit the `~/.claude/settings.json` `SessionEnd`/`SessionStart` commands to
the env-wrapped form shown above. The installer just does this for you, with a
backup — plus the interpreter pin and the index build, which the manual route
does not give you.

## Verifying the install

1. Start a Claude Code session that's ≥ 6 substantive turns (or just have a real conversation).
2. End the session.
3. Check `~/.claude/logs/distill.log` for the latest entry — should mention this session id.
4. Check `<your brain>/{Decisions,Mistakes,Patterns}/` for new dated files.
5. Start another fresh session and confirm `brain-health` prints nothing (healthy) or a real warning (see [`health.md`](health.md)).
6. Run `python ~/.claude/hooks/brain-search.py "<a topic you have notes on>"` —
   it must return hits. **Nothing at all means the index never built**, and
   steps 1-5 all pass in that state: they only exercise distillation.

Do **not** verify by checking for a `Stop` hook entry — the event is
`SessionEnd`. A device wired to `Stop` looks installed and never distills.

If files appear under your other notes vault instead, the env var didn't take effect — verify with `echo $CLAUDE_DISTILL_VAULT` in a fresh shell.

## Per-device independence

Every device runs its own hook against its own brain. No coordination, no sync. If a device's hook is broken, only that device misses its distillation — the other devices keep working.

## Known issues to be aware of

- ~~**Naming dedup is collision-suffix only**, not similarity-based.~~ **Fixed 2026-08-10** — see §Per-file behavior step 5 (near-duplicate gate).
- **Encoding artifacts in old exported files** (Korean mojibake in `_log.md`) appear to be from an even older hook iteration. Don't bring those into brain.

## 2026-06-11 outage postmortem + hardening

> **개요 (한국어):** claude CLI가 npm→네이티브 설치로 이동하면서 래퍼에 고정된 바이너리 경로가 죽어 6/8~6/11 증류가 전부 무증상 실패. 17세션을 소급 복구했고, 재발 방지 장치를 넣었다.

What broke: `distill-session.cmd` pinned `CLAUDE_DISTILL_CLAUDE_BIN` to the
npm path; the npm→native CLI migration removed it. Every distill (and every
catchup retry) failed silently for 3+ days; the search index froze with it.
Recovery: 17 sessions re-distilled (61 notes), index rebuilt `--full`.

Hardening now in place:

1. **Binary self-resolution** — `distill-session.py` ignores a stale pinned
   path and falls back to `PATH` → known install locations
   (`~/.local/bin/claude.exe`, npm). A future CLI move degrades to a slower
   lookup, not an outage.
2. **`catchup never retries 'failed' ids`** (by design, to avoid loops) — so
   a systemic failure (binary missing) needs a one-time manual recovery pass:
   feed the failed session ids from `distill.log` back to
   `distill-session.py` via a stdin payload. See the 2026-06-11 session
   notes for a working script shape.
3. **brain-health SessionStart hook** surfaces pipeline failures at the next
   session start instead of never — contract: [`health.md`](health.md).
4. **Interpreter pinning** — the launcher and SessionStart hook call
   `Python310` explicitly: the ML stack for reindex (`sqlite_vec`,
   `sentence-transformers`) lives there, and bare `python` resolves
   differently per shell on this device (hermes venv, 3.14, …).
