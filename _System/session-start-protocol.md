---
title: Session-start protocol
type: spec
status: confirmed
updated: 2026-06-12
---

# Session-start protocol

> **개요 (한국어):** Claude가 새 세션을 시작할 때 brain에서 무엇을 먼저 읽으면 좋은지의 지침. SessionStart hook을 자동으로 깔지는 않고, Claude가 이 문서를 보고 판단한다.

## What runs automatically vs. by judgment

**Since 2026-06-11 a SessionStart hook exists** — but it is a *health check*,
not memory injection: `~/.claude/hooks/brain-health.py` (contract:
[health.md](health.md)) verifies the distill pipeline / search index /
consolidate schedule, prints warnings only on problems, and spawns detached
`distill-catchup.py` + `brain-catalog.py`. It deliberately does NOT inject
memory content.

Memory loading on session start still works through:
1. The **project `CLAUDE.md`** (`~/brain/CLAUDE.md`) which Claude Code loads automatically when working in this repo.
2. The **Claude global memory** under `~/.claude/projects/.../memory/` which the harness loads.
3. **Claude reading this document** (and the files it points at) early in a relevant session.

So the *recall* protocol remains a contract Claude follows by judgment, not a
hook the OS executes. That is intentional — context injection should be
demand-driven, cheap, and judged per session.

## What to read first

When you (Claude) open a fresh session and see `~/brain/` exists, read in this order until you have enough context for the user's actual request:

1. `~/brain/_index.md` — what this vault is and where things live
2. `~/brain/Memory/profile.md` — who the user is (if exists)
3. `~/brain/Memory/session-resume.md` — **index only** (one line per project). Find the row matching the current project (cwd), then read that one file under `Memory/resume/<project>.md`. Don't read the other projects' files. (A single combined file grows past its cap every few weeks, because its size is project count × section size.)
4. `~/brain/Memory/projects.md` — what's currently in flight (if exists)

Stop after step 1 if the user's request has nothing to do with their personal context (e.g. a one-off `ls` or generic question). Reading is cheap, but inflating context costs tokens.

## Resume procedure ("이어서 하자" — added 2026-06-12)

> 한국어 요약: 재개 발화 시 확인 3단계 — 인덱스에서 찾은 resume 파일 실제 Read → 최신성 교차검증 → 재개 선언 후 시작. 진입점 1번 고정, 임의 트랙 선택 금지. 정본은 기록 규칙을 모아둔 런타임 문서 하나다.

When the user says "continue from last session" (이어서 하자 / 지난 세션에 이어서 / 계속하자 / 어디까지 했지), three steps are **mandatory before any work**:

1. **Actually Read** the current project's file — look up the row in `Memory/session-resume.md` (index), then Read `Memory/resume/<project>.md`. Never resume from memory, conversation summary, or brain-search fragments alone.
2. **Cross-check freshness** — if the project shows work newer than the file's `updated` (git log, latest ticket, daily note), the resume is stale: inspect those traces first and tell the user the resume missed a session.
3. **Declare before starting** — one line: "직전 세션 = X / 진입점 1번 = Y → 여기부터 재개". This lets the user catch a wrong resume at second zero.

Resume target is **entry point #1 = the last active track, verbatim**. Entry-point order is the user's priority — no reordering, no skipping. If #1 is blocked on user input, do **not** silently switch to another track; report what input is needed. ("Autonomous mode, don't ask" governs *how* to proceed, not *what* to work on.) Track switches happen only on explicit user request. This section exists because on 2026-06-11 a session resumed into the wrong track by autonomously skipping entry point #1.

## What to read on demand, not eagerly

| When user mentions… | Look here |
|---|---|
| A specific device | `Memory/devices/<device>.md` |
| Their style or quality bar | `Memory/style/` |
| An external service | `Memory/external/` or `Knowledge/external/` or `Knowledge/platforms/` |
| A past decision | `Distilled/Decisions/` (search by date or keyword) |
| A past mistake | `Distilled/Mistakes/` |
| A recurring pattern | `Distilled/Patterns/` or `Knowledge/patterns/` |
| Tools or environment | `Memory/tools.md`, `Knowledge/setup/` |

### Semantic search via `brain-search`

When the user invokes recall vocabulary ("기억나?", "예전에", "지난번", "3주 전", "그때") or mentions a topic that likely has prior context (brand names, project codes, tool names, past decisions), prefer **hybrid semantic search** over filesystem grep:

```sh
python ~/.claude/hooks/brain-search.py "natural-language query"            # top 5
python ~/.claude/hooks/brain-search.py "..." --category Decisions          # bias to authoritative
python ~/.claude/hooks/brain-search.py "..." --json                        # programmatic
```

Returns ranked file paths with snippets. ~12s cold start (per shell), then ~250ms. Use when keyword/date is unknown but topic is known. Contract: [search.md](search.md). For known-filename or known-date queries, direct `Read`/`Grep` is still faster.

## What about a fresh device?

On a freshly-cloned brain, the runtime files (`Memory/profile.md`, etc.) do not exist — only the `_index.md` skeleton. That's expected.

If a pre-existing personal notes vault (from before this system was set up) still has content that hasn't been migrated in, this device hasn't triaged yet. Before proceeding with the user's actual request, suggest a **one-time triage** — walk the user through choosing which past content to bring onto this device. After triage, future sessions follow the normal protocol above.

If that content is empty or already gone, triage is already done (on this or other devices). Treat any absence in `Memory/` as "this fact wasn't worth bringing to this device" and let the file populate naturally as the conversation reveals new facts.

## Write rules during the session

- New stable fact about the user → append to or update the relevant `Memory/` file
- A decision the user explicitly locked in → mention it in conversation, the Stop-hook will distill it (but if the decision is critical, also drop a manual note in `Distilled/Decisions/`)
- A new how-to that worked → `Knowledge/<area>/<slug>.md`

## Write rules at session end

If a Stop-hook is installed, it handles the journal layer (`Decisions/Mistakes/Patterns`). Updating the resume state is **not optional**: on session wrap, overwrite `Memory/resume/<project>.md` (that one file only) and refresh its row in the `Memory/session-resume.md` index; displace the old content to `Sessions/` if it has log value. Keep the file to next-entry-points, re-entry identifiers, and project-specific traps — reusable know-how belongs in `Knowledge/` with only a `[[link]]` here. Canonical rules live in whichever runtime file collects your recording conventions.

If no Stop-hook is installed, you may manually drop a `Distilled/Sessions/YYYY-MM-DD-<slug>.md` before ending. See [`stop-hook.md`](stop-hook.md) for format.
