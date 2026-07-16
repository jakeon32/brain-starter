---
title: brain-health — pipeline self-monitoring + auto-catalogs
type: spec
status: confirmed
updated: 2026-07-16
---

# brain-health — the pipeline watches itself

> **개요 (한국어):** 2026-06-08~11 증류 파이프라인이 3일 넘게 무증상으로 죽어 있던 사고의 재발 방지 장치. 세션 시작 시 읽기 전용 헬스체크가 돌고, 이상이 있을 때만 경고를 세션 컨텍스트에 주입한다. 카탈로그(`_catalog.md`)는 손으로 쓰지 않고 자동 생성한다.

## Why

The distill pipeline is fail-silent by design (a broken hook must never block
the user). The cost: when it breaks systemically — as on 2026-06-08 when the
claude CLI moved and the pinned binary path died — nothing surfaces it.
Three days of sessions silently fell out of long-term memory and the search
index froze. `brain-health` makes breakage visible at the *next session
start*, which is the earliest moment anyone is listening.

## Components (device-local: `~/.claude/hooks/`, source: `_System/hooks/`)

> **Sync obligation — both directions:**
> - **device → source:** fix a hook in device-local, then copy it to `_System/hooks/`
>   and commit — otherwise the tracked source falls behind and a new device clones an
>   old hook. (2026-06-24: the 6/19 resume-bloat check lived only in the device copy;
>   the tracked source sat 7 days stale until this audit caught it.)
> - **source → device:** after any `_System/hooks/*.py` change lands — your own commit
>   **or a `git pull` carrying another device's work** — re-run `install-stop-hook.ps1`.
>   Nothing does this for you. (2026-07-15: this device ran 7/6-era hooks for 9 days
>   and was missing `brain-sync.py`/`brain-decay.py` outright, because the 7/12–13
>   commits were never installed. See §2026-07-15 postmortem.)
>
> Quick drift check:
> `for f in _System/hooks/*.py; do diff -q "$f" ~/.claude/hooks/$(basename $f) >/dev/null || echo "DRIFT: $f"; done`

### brain-health.py — SessionStart hook

Registered in `~/.claude/settings.json` → `hooks.SessionStart`. Read-only,
no LLM, target < 1s. **Prints nothing when healthy** (hook stdout becomes
session context; silence keeps healthy sessions clean). Checks:

| Check | Warns when |
|---|---|
| distill failures | failures in last 72h **and** the latest log event is a failure (old failures already followed by successes are history, not noise) |
| search index staleness | newest brain `.md` is > 24h newer than `brain-index.sqlite` |
| consolidate receipt | newest `_System/audit/consolidate-*.md` is > 9 days old (weekly schedule regressed — task runs only while logged in), **or** (added 2026-07-03) no receipt has *ever* been written and the journal already has ≥15 files (weekly schedule was never set up on this device — the installer prints the setup command but does not run it) |
| Memory staleness | `Memory/*.md` untouched > 60 days (reference facts drifting) |
| session-resume bloat | `Memory/session-resume.md` > 55KB or > 18 `##` sections — the one-section-per-project overwrite rule (recording-protocol §세션 연속성) has regressed to append. Added 2026-06-12 after the file silently grew to 132KB/42 sections and broke session resume. **Caps raised from 35KB/12 on 2026-07-15**: with 13 genuinely active projects the old caps fired every session on a healthy file, and an always-on warning trains you to skim past the block that carries the real ones. |
| _index.md catalog drift | a tracked `_index.md` carries > 5 hand-written list-item `.md` links — file listings are `brain-catalog.py`'s job, not hand-maintenance. Added 2026-06-24 after `Knowledge/_index.md` silently regrew 21 hand-written links despite the catalog automation (git diff only surfaces this at commit time; runtime sessions rarely commit the skeleton). The `_catalog.md`/`_index.md` pointers are excluded so curated prose links don't false-positive. **2026-06-30: the regrowth was NOT hand-written — root cause was `consolidate.py`'s `index_add()` auto-append (now removed). See postmortem below.** |

Side effect: spawns **detached** `distill-catchup.py` (recover missed
sessions — deferred recovery, zero loss as long as a next session ever
starts), `brain-reindex.py` (incremental self-heal — re-indexes any note
whose own reindex died, e.g. a concurrent-spawn lock collision; catchup
cannot catch this because it sees the session as already distilled), and
`brain-catalog.py` (refresh catalogs). Opt out:
`BRAIN_HEALTH_NO_CATCHUP=1`. Recursion guard: exits silently when
`CLAUDE_DISTILL_CHILD` is set (distill's own `claude -p` child sessions).

### brain-watchdog.py — the out-of-band witness (Task Scheduler, not a hook)

Added 2026-07-15, after the SessionStart hook turned out to have been dead for
weeks with nothing noticing. **Deliberately not a hook** — everything above
assumes health runs, and a watchdog inside the session cannot report its own
absence. Registered per device, one-time (the installer prints the command but
does not run it, same as weekly consolidate):

```
schtasks /create /tn brain-watchdog /sc daily /st 09:00 /tr "%USERPROFILE%\.claude\hooks\brain-watchdog.cmd" /f
```

> `/sc onlogon` would be the natural trigger but needs admin; `/sc daily` runs
> under the plain user account. Like consolidate, it only fires while logged on
> — acceptable here, because a machine that is off is also not running sessions.

| Check | Warns when |
|---|---|
| hook registration | `settings.json` lost its `brain-health`/`distill-session` entry, or a command regressed to single-slash `cmd /c` (the literal 2026-07-15 bug), **or the `//c` path arrives unquoted** (the same-day sequel — sh eats the backslashes, cmd gets `C:UsersUSER...`, exits 0, fails just as silently). Inline commands like the canary (`cmd //c "echo ..."`) are not flagged |
| sessions ran, hooks left no trace | a **finished** session (transcript untouched > 30 min) is newer than `max(canary, distill.log)` + 2h. Not "no distills lately" — going days without a session is normal and a warning that cries wolf gets ignored |
| device hooks vs tracked source | any `_System/hooks/*.py` differs from `~/.claude/hooks/` |

Notifies by **Windows toast** (WinRT via powershell): no token, no network, so
the alarm has fewer ways to die than what it watches. Silent when healthy.
Appends one line to `~/.claude/logs/watchdog.log` every run, healthy or not.

> **Why the trace check needs both canary and distill.log** (learned while
> testing it, 2026-07-15): distill spawns `claude -p` children whose *own*
> SessionEnd fires the canary — all 14 canary lines that afternoon were
> children, not user sessions. So the canary alone can look healthy while
> distill is dead. distill.log alone is no better: it is silent for children by
> design (`CLAUDE_DISTILL_CHILD` guard). Together they answer both halves.

### brain-catalog.py — auto-generated `_catalog.md` per folder

Hand-maintained `_index.md` listings drift: the 2026-06-11 audit found the
Knowledge catalog ~70% accurate (7 ghost entries, 5 orphaned files), and
Sessions/Decisions/Mistakes/Patterns had no catalog at all. Listings are now
generated, not curated:

- Writes `{folder}/_catalog.md` for Knowledge / Sessions / Decisions /
  Mistakes / Patterns / Memory / Agents. Frontmatter scan only; fast.
- `_catalog.md` is auto-ignored by the existing `.gitignore` patterns
  (`Knowledge/**` etc.) — device listings never leak into the shared skeleton.
- Tracked `_index.md` files keep **curated prose only** plus a pointer to
  `_catalog.md`. Do not hand-write file listings in `_index.md` again.
- Regenerated: after each successful distill (spawned by
  `distill-session.py`), at session start (spawned by `brain-health.py`),
  or manually. Skips writing when content is unchanged (no mtime churn).

## Failure-mode coverage map

| Failure | Caught by |
|---|---|
| claude binary moved/renamed | self-resolution in distill-session.py; if that fails too → health warning at next session start |
| Stop/SessionEnd hook not firing | catchup at session start (spawned by health hook) |
| **SessionEnd hook firing but killed mid-flight** ("Hook cancelled") | **catchup — but only since 2026-07-16.** Before that its done-set was built from a regex that also matched the *start* line, so a cancelled distill retired the session permanently. 14 sessions lost. See §2026-07-16 sequel. |
| catchup itself broken | health warning (failures stay latest event in log) |
| index stale (reindex spawn died) | session-start incremental reindex (self-heal, ≤1 session); 24h staleness warning as backstop |
| concurrent reindex collision (two sessions end together) | `busy_timeout=120s` in `open_db` — loser waits for the lock instead of dying silently |
| weekly consolidate missed (not logged in) | health receipt check |
| catalog drift in generated `_catalog.md` | not possible — generated |
| catalog drift via hand-written `_index.md` listing | `_index.md` link-burst check at session start (added 2026-06-24 after a 21-link regression in `Knowledge/_index.md`) |
| **the health hook itself never runs** (broken registration: shell quoting, bad path, wrong event) | **`brain-watchdog.py`** (Task Scheduler, daily) — checks the registration literally (`cmd //c` present, single-slash form absent) *and* the general form: a finished session newer than every SessionEnd trace. Nothing inside a session can catch this; see §2026-07-15 postmortem. |
| device-local hooks older than the tracked source | **`brain-watchdog.py`** — diffs `_System/hooks/*.py` against `~/.claude/hooks/`. A `git pull` pushes nothing to the device, and re-running the installer is a manual step nobody was reminded of (2026-07-15: 9 days stale, `brain-sync.py`/`brain-decay.py` missing outright). |
| **`scope: shared` file never leaves the device** | **nothing, until 2026-07-16** — `brain-sync.py --push` is now a SessionEnd hook. Before that only the weekly consolidate called it, so a shared page written on Monday sat local until Sunday, and an untracked one sat local forever. See §2026-07-16 postmortem. |
| **the sync push is killed mid-`git push`** | **the absent receipt** — `cmd_push` writes `pushed: yes`/`pushed: NO` on every terminal path, so *no receipt at all* means the process died before deciding. Nothing reads that silence automatically yet; it caught the missed push only because a human asked whether the work was on the remote. Now mitigated at the source: `--push` detaches (§2026-07-16 sequel → second victim). |

Known residual gaps: a failure on the *last* session before a long break is
only recovered when the next session starts. (The old note here — *"`processed_ids`
treats `failed` as done, so systemic failures need a manual recovery pass"* — was
fixed on 2026-07-16: explicit failures are retryable now, bounded by `STALE_DAYS`.
That sentence knew the function conflated outcomes and stopped one case short of
the real defect; see §2026-07-16 sequel.) **The watchdog itself is the bottom
turtle** — nothing watches it. That is deliberate (another watcher would just
move the question) and bounded instead by keeping it dumb: no LLM, no network,
no token, and one line appended to `~/.claude/logs/watchdog.log` on every run
so "did it run at all?" is answerable without trusting it. If you audit this
pipeline by hand, that log's freshness is the first thing to look at.

## Device notes (home, 2026-06-11)

- Interpreter: `C:\...\Python310\python.exe` pinned in the SessionStart
  command and `distill-session.cmd` — the only install with `sqlite_vec` +
  `sentence-transformers`. Bare `python` resolves to a hermes venv that
  cannot run reindex.
- Weekly consolidate: Task Scheduler `brain-consolidate-weekly`, Sundays
  04:00, **runs only while logged on** — the receipt check exists precisely
  because this can silently skip.

## Index write contract (all devices)

`_brain_lib.open_db` **must** open the index with a busy timeout longer than
the slowest single reindex — currently `sqlite3.connect(..., timeout=120)` +
`PRAGMA busy_timeout=120000`. Reindex holds the sqlite write lock for the
entire embed+insert pass (10–34s); when two sessions end close together each
spawns a **detached** reindex, so without a long busy timeout the second hits
sqlite's 5s default, raises `database is locked`, and — being detached with
`stderr=DEVNULL` — dies leaving no trace. This silently dropped 14 notes on
2026-06-18 (the notes existed on disk but were unsearchable until the next
manual reindex). Prevention = this timeout; recovery = the session-start
incremental reindex spawned by `brain-health.py`. Keep both; do not lower the
timeout. The hook files are device-local, so each device must carry this —
hence the contract lives here in the tracked skeleton.

## 2026-06-30 postmortem — auto-generated meta-files treated as runtime notes

> **개요 (한국어):** `_index.md`가 반복적으로 오염되고 주간 consolidate가 조용히 크래시(W26 영수증 누락)한 사고. 표면 진단("손으로 쓴 링크 회귀")은 **오진**이었고, 진짜 원인은 자동생성 메타파일(`_index.md`·`_catalog.md`)을 일반 런타임 노트로 취급한 코드 버그 2개였다.

**Symptom:** repeated `_index.md` drift warnings (6/24, 6/30) and a silent
`brain-consolidate.py` crash (`FileExistsError` on a `_catalog.md` rename) that
skipped the W26 receipt.

**Misdiagnosis:** the catalog-drift check assumed the regrown links were
*hand-written*, so each audit merely reverted them — and they returned. They
were never hand-written.

**Two root-cause bugs (same theme):**
- **A — `consolidate.py write_page → index_add()`** appended every new
  consolidated note as a link into the tracked, shared `_index.md`. The old
  Karpathy "update index" pattern, left stale after the 2026-06-11 switch to
  auto-generated `_catalog.md`. Per-device note links leaked into the shared
  skeleton. Fix: removed the call + the `index_add` function.
- **B — `_brain_lib.iter_markdown_files` did not exclude `_catalog.md`.** The
  auto-generated catalogs were indexed (search noise) and became consolidation
  cluster members — `write_page` then tried to `rename` a `_catalog.md` into
  `archived/`, corrupting the merged note (`brain-vault-runtime-catalog.md`,
  deleted) and crashing on a name clash with a 6/12 ghost. Fix: exclude
  `_catalog.md` in `iter_markdown_files`.

**Cleanup:** deleted 3 ghost `archived/2026/_catalog.md` (6/12) + the corrupt
merged note; ran `--full` reindex (652→620 files) to purge `_catalog` rows.

**Gap closed (2026-07-03):** `_System/hooks/` now tracks all 8 pipeline
scripts — `_brain_lib.py`, `brain-consolidate.py`, `brain-reindex.py`,
`brain-search.py`, `distill-catchup.py` joined `brain-catalog.py`,
`brain-health.py`, `distill-session.py`. Two more bugs surfaced and were fixed
while closing this:
- **Env-var name split**: `_brain_lib.py` (search/reindex/consolidate) read
  `BRAIN_VAULT` while the rest of the pipeline used `CLAUDE_DISTILL_VAULT` —
  brain-health's spawn set only the latter, so `_brain_lib`'s hardcoded
  `~/brain` default was silently doing the real work, masked by
  coincidence on the one device where that path happened to be correct. Now
  unified: `BRAIN_VAULT` (explicit override) → `CLAUDE_DISTILL_VAULT`
  (standard) → `~/brain` (home-relative fallback, not a device-specific
  absolute path).
- **Installer patched the wrong hook event**: `install-stop-hook.sh/ps1`
  still targeted `settings.json`'s `Stop` bucket, but the live setup had
  already moved to `SessionEnd` (`Stop` never fires on `/exit` or interrupt).
  A fresh device running the old installer would have
  installed a hook event that never fires. Fixed — installer now patches
  `SessionEnd` (distill) and `SessionStart` (health), and also generates a
  `brain-health.cmd`/env-wrapped launcher so `-py` gets `CLAUDE_DISTILL_VAULT`
  explicitly rather than relying on the script's own fallback. See
  [`stop-hook.md`](stop-hook.md) §Reproducible setup.

## 2026-07-15 postmortem — the watchdog itself was fail-silent for weeks

> **개요 (한국어):** 2026-06-08 사고("파이프라인이 무증상으로 죽음")의 재발 방지 장치인 `brain-health` **자신이 똑같은 방식으로 죽어 있었다**. 원인은 `settings.json`의 훅 등록 한 줄 — `cmd /c`의 `/c`가 셸의 경로 변환에 걸려 스위치가 사라지면서, cmd가 대화형으로 떠 배너만 찍고 **exit 0**으로 끝났다. Claude Code는 이걸 "hook success"로 보고했다. 파이썬은 몇 주간 단 한 번도 실행되지 않았다.

**Symptom:** none. That is the whole point — sessions started normally, and the
hook was reported as succeeding. What actually appeared in the session context
was `Microsoft Windows [Version ...]` + a `<VAULT>>` prompt with the
hook's own stdin JSON echoed into it: an interactive cmd.exe banner, mistaken
for benign noise.

**Root cause:** `"command": "cmd /c C:\...\brain-health.cmd"` in
`~/.claude/settings.json`. Claude Code runs hook commands through its bundled
sh (Git Bash/MSYS) on Windows. MSYS argument conversion rewrites a lone `/c`
into a Windows path (`C:\Program Files\Git\c`), so cmd.exe never sees the
switch, starts **interactively**, prints its banner, and exits 0. The fix is
`cmd //c` — the MSYS escape that survives as `/c`.

**Why nothing caught it — three compounding reasons:**
1. **The watchdog is a single point of failure.** `brain-health.py` is both the
   only warner *and* the spawner of catchup/reindex/catalog. Its death removes
   the recovery path and the alarm at once. Every row of the coverage map above
   silently assumed "health runs".
2. **exit 0 = success.** A hook that never ran its payload is
   indistinguishable, to the harness, from one that ran and found nothing.
   Silence-when-healthy (a deliberate design choice) makes the two identical.
3. **catchup masked the damage.** Distillation still happened — just late, via
   catchup — whenever anything happened to invoke health manually. Notes kept
   appearing in `Decisions/`/`Mistakes/`/`Patterns/`, so the vault looked alive.

**Evidence (how it was confirmed):** `distill.log` from 2026-06-25 to 07-15 has
`distilling session` counts **exactly equal** to `catchup: recovering` counts —
i.e. **zero** SessionEnd-driven distills across three weeks. The SessionEnd
canary (`C:\tmp\sessionend-canary.log`, same `cmd /c` pattern) had **never been
created**. Running the registered command by hand reproduced the banner
verbatim.

**Fixed:** `cmd //c` in `settings.json` (SessionStart health, SessionEnd
distill, SessionEnd canary) **and** in `install-stop-hook.ps1`, which was
generating the broken form and would have reproduced it on every new device.
Verified by running each registered command as-registered: health prints its
warning block, distill reaches Python (`skip: no transcript_path`), canary
writes its line.

**Second finding, same audit — device hooks 9 days behind the tracked source.**
`~/.claude/hooks/` was still 7/6-era: `brain-sync.py` and `brain-decay.py` were
**absent entirely** and health/search/consolidate/distill were stale, because
nobody re-ran the installer after the 7/12–13 commits. The sync obligation at
the top of this file only covers device→source (fixing a hook locally); the
reverse (source→device, after `git pull` or another device's work) had no
trigger at all. Resolved by re-running `install-stop-hook.ps1` (10/10 files now
identical). **This is now a coverage-map row, still open.**

**Lesson:** a fail-silent design needs an out-of-band witness. Anything that
lives inside the session-start hook cannot report its own absence — and "the
hook reported success" is not evidence the hook did anything. When auditing
this pipeline, do not ask "did health warn?"; ask **"is there a direct
SessionEnd distill in the log today, and is the canary growing?"**

**Closed the same day:** `brain-watchdog.py` (§ above) now runs from Task
Scheduler and checks exactly that, plus the drift row. Verified by fault
injection rather than by looking at it — 7/7: it warns on a re-introduced
single-slash `cmd /c`, on a missing registration, on finished-sessions-without
-trace, on hook drift, and stays quiet on the healthy states. Then run through
`schtasks /run` to confirm the scheduled path itself works (Last Result 0, log
line appended) — because "registered" and "actually runs" are different claims,
which is the whole lesson of this postmortem.

**Also confirmed the repair, live:** the canary recorded 14 SessionEnd firings
between 12:10 and 14:06 on 2026-07-15, the first since the registration broke.

## 2026-07-15 sequel — the repair was itself half-dead, for four hours

> **개요 (한국어):** 슬래시를 고친 뒤 "복구 완료"라고 선언했지만, 훅은 **여전히 죽어 있었다**. `cmd //c` 뒤의 경로에 따옴표가 없으면 sh가 백슬래시를 먹어 `C:UsersUSER...`가 되고 — 또 exit 0. 검증할 때 손으로 따옴표를 씌워 테스트했기 때문에 못 봤다. **등록된 문자열을 읽어서 검증해야 한다.** 사용자가 installer를 고쳐 지적해준 뒤에야 발견.

Two bugs stacked, and fixing the first hid the second:

1. `cmd /c` → `/c` eaten by MSYS path conversion (the original).
2. `cmd //c C:\path\x.cmd` → **sh eats the backslashes** because the path is
   unquoted; cmd.exe is handed `C:UsersUSER.claudehooksx.cmd`, says "not a
   command", and exits 0. Identical silence. The canary survived only because
   its command happens to be quoted (`cmd //c "echo ... >> ..."`).

**Why the verification missed it:** every by-hand check wrapped the path in
quotes (`cmd //c 'C:\...\x.cmd'`) that the actual registration did not have. So
the test passed while the registered form failed. This is the postmortem's own
lesson — "registered ≠ runs" — reintroduced one layer down: *what you typed ≠
what is registered*. Correct form:

```sh
CMD=$(python -c "...print the command out of settings.json...")
before=$(wc -l < ~/.claude/logs/distill.log)
echo '{}' | eval "$CMD"                 # eval reproduces sh's own parsing
after=$(wc -l < ~/.claude/logs/distill.log)   # 0 new lines = the hook is dead
```

Judge by the **log the payload writes**, never by exit code and never by
silence — a hook that printed a banner and died exits 0 too.

**Third bug, found while fixing the second:** Windows PowerShell 5.1 strips
embedded double quotes when passing arguments to a native exe, so the
installer's `& $py $patch ... "cmd //c `"$HealthCmd`""` silently delivered the
*unquoted* form. Quoting must be composed on the receiving side — the patch
script now takes the bare launcher path and builds `'cmd //c "%s"'` itself.

**Fixed + verified:** all three registrations re-read out of `settings.json`
and run through `eval` — health silent (healthy), distill reaching Python
(`skip: no transcript_path`, +1 log line), canary +1 line. `brain-watchdog`
gained an unquoted-path check; fault-injection now 11/11.

### Sequel, same day — the repair's own installer re-broke it (quotes this time)

> **개요 (한국어):** 위 `cmd //c` 수정은 옳았지만 절반이었다. `install-stop-hook.ps1`은
> 따옴표까지 붙여 쓰려 했는데, **Windows PowerShell 5.1이 네이티브 exe 인자의 안쪽
> 따옴표를 조용히 제거**한다. 그래서 따옴표 없는 경로가 settings.json에 박혔고, sh가
> 백슬래시를 먹어 cmd.exe는 `C:UsersUSER...`를 받았다. 드리프트를 닫으려고 13:23에
> 설치 스크립트를 재실행한 것이 바로 두 훅을 다시 깨뜨린 행위였다.

**Root cause:** `& $PythonBin $tmp $Settings "SessionStart" "brain-health" "cmd //c `"$HealthCmd`""`.
The intent was right; PS 5.1 strips the embedded quotes when handing the argument
to a native exe, so python received — and wrote — `cmd //c C:\...\brain-health.cmd`.
sh then ate the backslashes, cmd.exe got `C:UsersUSER.claudehooksbrain-health.cmd`
→ "not an internal or external command", exit 1.

**The good news is the failure mode inverted.** `/c` failed **silently** (exit 0,
fake success, dead for weeks). Unquoted `//c` fails **loudly** (exit 1, surfaced
in the session on the next start). The visible error was the audit.

**Fixed:** quotes restored in `settings.json` (both hooks), and the composition
moved out of PowerShell into the python patch script (`command = 'cmd //c "%s"' %
launcher`) — PS 5.1 cannot strip what it never handles. Re-running the installer
now reproduces the working string instead of the broken one.

**Verified, not assumed:** PS 5.1.26100 reproduced live — passing `"cmd //c `"$P`""`
yields `cmd //c C:\...` (quotes gone), passing `$P` alone and composing in python
yields the quoted form. End-to-end proof used the log, not the exit code: running
the registered command as-registered appended a fresh `catchup: scan` line to
`distill.log` (14:17:31), which only happens if `brain-health.py` actually ran.

**Coverage gap this leaves open:** `brain-watchdog.py`'s registration check only
rejects the single-slash form (`(?<!/)/c(?=\s)`) — an **unquoted** `//c` path
passes it. Only the general check (a finished session newer than every SessionEnd
trace) would have caught this, a day late. A quoted-path assertion belongs in
`check_hook_registration`.

**Lesson (extends the one above):** a postmortem's own fix needs the same witness
as the thing it fixed. "The installer writes the right string" is a claim about a
shell you did not test — here PowerShell, one layer *above* the sh/cmd boundary
that caused the original bug. Every layer between the source and the registered
string is another place quoting dies. Assert on the **file the installer
produced**, never on the source that produces it.

## 2026-07-16 postmortem — the shared layer only ever received

**Symptom.** Six of fourteen `scope: shared` files existed on one device only.
Three were untracked, three were tracked-but-uncommitted. No warning, ever.

**Why it stayed invisible.** `Knowledge/**` is gitignored, so a shared page that
was never `git add -f`'d does not appear in `git status` at all. The three
uncommitted ones did show up, but as three unremarkable `M` lines among the noise
of a runtime vault. Nothing connected "this file says `scope: shared`" to "this
file is not actually shared" — the frontmatter tag was a statement of intent that
nothing verified.

**Root cause.** `brain-health.py` pulls at SessionStart. Push was designed to
happen at SessionEnd — *"if this session touched any `scope: shared` file, commit
and push right away"* — and never wired. `brain-sync.py` had a working `--push` mode
the whole time; its only caller was the weekly consolidate. So the layer received
automatically and sent manually, and manual meant "whoever remembers".

**What it cost.** One `Knowledge/` note documenting internal infrastructure was
promoted to `scope: shared` on 2026-07-16 with the explicit reasoning *"기기
로컬에 두면 네 번째가 난다"* — written to stop a deployment rule from being broken
a fourth time, after three device-crossing repeats. It was pushed by hand that
morning; the edits made after that push stayed local. **The rule against
device-local rules was itself device-local.**

**Fix.** SessionEnd → `brain-sync.py --push`, registered by both installers
(Windows via a generated `brain-sync-push.cmd`, same launcher pattern as health
and distill). Backlog force-added and pushed in one commit; the wiring landed in a follow-up commit the same day.

**The lesson that generalises.** Every other silent failure in this file is a
hook that stopped running. This one is a hook that was never started — the design
said what should happen, everyone read the design, and no one checked whether the
code did it. `grep` for the caller before believing a documented behaviour exists:
this took one command (`grep -rn "brain-sync" _System/hooks/`) and the answer was
right there.

## 2026-07-16 sequel — the safety net counted "started" as "finished"

> **개요:** 증류 훅이 취소되면 로그에 시작 줄만 남는데, 이를 따라잡아야 할
> catchup이 그 시작 줄을 "이미 처리함"으로 읽어 해당 세션을 영구히 건너뛰었다.
> 세션 14개가 노트 0개로 유실. 안전망이 자기가 지켜야 할 것을 지웠다.

**Symptom.** The user saw `SessionEnd hook [cmd //c distill-session.cmd] failed:
Hook cancelled` on exit. That message was the *visible* half; it is also the
harmless half.

**Evidence.** `distill.log` records a start line per attempt and an outcome line
per completion. Of 267 attempts, **20 logged a start and no outcome** — the
process died before it could log even its own timeout (`distill-session.py` logs
every failure path, so silence means it was killed from outside, not that it
failed). Fourteen of those sessions had **never** produced a note. Three were
from the previous 24h of brain work; four were old enough that the transcript is
gone and the loss is permanent.

**Root cause — not the cancellation.** `distill-catchup.py` exists precisely so a
cancelled hook costs nothing: its docstring promises *"as long as a next session
ever starts, the previous one is recovered."* It decided what to skip with

```python
set(re.findall(r"session ([0-9a-f]{8})", txt))   # "already touched"
```

and that substring matches the **start** lines too — `distilling session X`,
`catchup: recovering session X`. So a session whose only trace was "I began" got
retired as done, forever. `8101f80b` is the clean specimen: one start line, zero
notes, permanently skipped. **The net dropped exactly the sessions it existed to
catch** — a session that completed normally was never at risk in the first place.

**Why nothing caught it.** The coverage map's row for this is "Stop/SessionEnd
hook not firing → caught by catchup", which was true for *not firing* and false
for *firing and being killed*. Worse, the residual-gaps note already said
`processed_ids` treats `failed` as done — so the function's habit of conflating
outcomes was known and written down, but only for the explicit-failure case that
at least leaves a log line. Nobody looked at what else that regex swallowed.
The check that would have found it is one line: does the done-set contain a
session that never wrote anything?

**Fix.** `OUTCOME_RE` whitelists terminal outcomes only (wrote / marked skip /
below MIN_TURNS). Start lines no longer retire a session, and explicit failures
became retryable (bounded by `STALE_DAYS`, so a permanently-failing session
cannot loop). Verified against the real log: done-set 252→233, 19 freed for
retry, and **0 of the 194 sessions that wrote notes were lost from the set**. The
fixed scan then found the 3 recoverable sessions on its own and distilled them
(+15 notes); a re-scan reports `missing=0`.

**What it cost.** The three recovered sessions carried the previous day's own
lessons — including the postmortem directly above this one. **They nearly
vanished to the bug they describe.**

**Second fix — remove the exposure instead of diagnosing it.** *Why* the hook got
cancelled was never established, and deliberately so: the default `command` hook
timeout is 600s, well above distill's own 180s ceiling, so timeout was not the
trigger; the likeliest candidate was the CLI tearing its process tree down at
exit (fits the clustering — 11 SessionEnd fires between 11:45 and 11:54). Rather
than chase it, `distill-session.py` now reads the payload, **re-execs itself
detached, and returns**. Measured: the hook goes from ~24s (p90 36s) of blocking
to **0.08s**, and the LLM round-trip finishes out-of-band —

```
12:24:45  session cafe1234: detached worker spawned; hook returning   <- hook ends here
12:24:45  distilling session cafe1234 (8 turns, 1818 chars)           <- child continues
12:24:53  session cafe1234 marked skip by distiller                   <- parent long gone
```

Whatever the teardown was, there is now nothing in-flight for it to kill, and
session exit no longer waits on an LLM. Detaching is fail-open (a failed spawn
runs inline) and the machine dying mid-work is still covered by catchup. Note the
ordering: this was only safe to do **after** the done-set fix — a detached worker
that dies leaves the same start-line-only trace, so on the old code this
"improvement" would have widened the very hole it sits on.

**The lesson that generalises.** A safety net that decides "handled" from the
same log it is auditing must key on **completion**, not on **attempt**. If the
evidence for "done" is something written *before* the work, every interruption
becomes an invisible success. And when a doc promises a guarantee — *"the
previous one is recovered"* — test the guarantee against the failure it names,
not against the happy path.

### Same day, second victim — the sync push died the same way

Fixing distill's detach did not fix the hook event; it fixed *one hook on it*.
`brain-sync-push.cmd` is also a SessionEnd hook, and a network `git push` is
also slow. It died in exactly the same teardown, on the day it was wired:

```
11:45:02.22  SessionEnd fires
11:45:02     distilling session 56a65e83  -> no outcome, ever      (victim 1)
11:45:03     a commit lands               -> push never ran        (victim 2)
```

That commit carried `Knowledge/absolute-layout-verification.md` — a `scope: shared`
file. **The commit sat local-only for a day**, which is precisely the failure the
SessionEnd wiring was added to prevent the day before (§2026-07-16 postmortem,
"the shared layer only ever received"). The fix received a fix and still only
half-worked.

The tell was the **absent receipt**: `cmd_push` writes one on every terminal
path, `pushed: yes` or `pushed: NO`. The 11:30 run left `pushed: yes`; the 11:45
run left nothing at all. No receipt ≠ push failed — it means *the process never
reached the decision*. An audit trail that is written only at the end tells you
about deaths by its silence; that silence is a signal, not a gap.

**Fix.** `brain-sync.py --push` now detaches the same way distill does (returns
in 0.08s). `--pull` deliberately does not — SessionStart needs its result before
the session reads anything. `--dry-run` stays in-process so the caller sees the
report.

**The lesson.** When you find a hook event that kills slow work, **enumerate
every hook on that event** before calling it fixed. The mechanism was found on
distill, distill was fixed, and the pipeline was reported healthy — while an
identical victim sat two lines below it in the same `settings.json`. The user
asking "is this on the remote?" is what surfaced it; nothing in the pipeline
did.
