---
title: brain — Structure
type: spec
status: confirmed
updated: 2026-07-13
---

# brain — Structure

> **개요 (한국어):** brain 폴더의 각 디렉터리가 무슨 역할을 하는지 정의한다. 새 디바이스의 Claude가 이 문서 한 장으로 폴더 의미를 파악할 수 있어야 한다.

```
~/brain/
├── README.md, CLAUDE.md, _index.md       — entry points
├── Memory/                                — stable user/project facts (per-device runtime, opt-in shareable — see below)
├── Decisions/                             — Stop-hook auto-output (per-device runtime, always)
├── Mistakes/                              — Stop-hook auto-output (per-device runtime, always)
├── Patterns/                              — Stop-hook auto-output (per-device runtime, always)
├── Sessions/                              — manual session narratives (per-device runtime, always)
├── Knowledge/                             — reusable how-tos (per-device runtime, opt-in shareable — see below)
├── Agents/                                — agent operating guides (per-device runtime, always)
├── _System/                               — meta-system definition (TRACKED)
│   ├── structure.md                       — this file
│   ├── conventions.md                     — file naming, frontmatter
│   ├── stop-hook.md                       — Stop-hook reality + retarget instructions
│   ├── search.md                          — hybrid BM25+vector search index (P1)
│   ├── mcp-config.md                      — why brain doesn't use MCP (direct file access)
│   ├── session-start-protocol.md          — what Claude reads first on session start
│   └── audit/                             — weekly/monthly audit results (per-device runtime)
└── .obsidian/                             — Obsidian vault config (mostly ignored)
```

**Layout note:** `Decisions/`, `Mistakes/`, `Patterns/` live at brain root, not under a `Distilled/` wrapper. This matches the existing Stop-hook script (`~/.claude/hooks/distill-session.py`) which writes directly to `<vault>/<Category>/`. See [`stop-hook.md`](stop-hook.md). `Sessions/` is a parallel sibling for manual narrative entries, not auto-written.

**Work-logging protocol (schema pointer):** if you keep a work log across several tools, the recording rules belong in one runtime file (e.g. `Knowledge/recording-protocol.md`, not tracked) that `_System/` points to and never duplicates — duplicating them is how the rules drift apart across project memories.

**Convergence / archive:** brain is run as an LLM-maintained wiki (Karpathy *LLM Wiki*). Periodic Ingest folds journal fragments (`Decisions/Mistakes/Patterns`) into topic pages under `Knowledge/`; consolidated source fragments move to `<category>/archived/<year>/` (kept for provenance, **excluded from brain-search** via `_brain_lib.iter_markdown_files`). Never deleted.

## Tracked vs ignored — the rule

This repo is shared via `git clone` to bootstrap a new device. Anything that defines *what brain is* must be tracked. Anything that is *Claude's running memory on this specific device* must be ignored — **except** an opt-in subset of `Memory/`+`Knowledge/` (below).

| Path | Tracked? | Why |
|---|---|---|
| `_index.md`, `README.md`, `CLAUDE.md` | ✓ | Entry points — every device must have them |
| `*/​_index.md` | ✓ | Folder MOCs explain what the folder is for |
| `_System/**/*.md` (except `audit/`) | ✓ | Defines conventions and contracts |
| `_System/audit/**` | ✗ | Per-device audit results |
| `Memory/*.md`, `Memory/*/*.md` | ✗ by default, **✓ if `scope: shared`** | Per-device runtime; an explicitly tagged subset propagates (below) |
| `Decisions/*.md`, `Mistakes/*.md`, `Patterns/*.md`, `Sessions/*.md` | ✗, no exception | Per-device runtime (Stop-hook output + manual sessions) — raw/episodic, never shared even opt-in |
| `Knowledge/*.md`, `Knowledge/*/*.md` | ✗ by default, **✓ if `scope: shared`** | Per-device runtime; an explicitly tagged subset propagates (below) |
| `Agents/*.md` | ✗ | Per-device runtime |

The exact patterns are in `.gitignore` at the repo root — plus `_System/hooks/brain-sync.py`, which force-adds any `Knowledge/*.md`/`Memory/*.md` file whose frontmatter has `scope: shared` regardless of the blanket ignore. Frontmatter field: [`conventions.md`](conventions.md) §Optional fields.

**This only benefits a device whose own pipeline is healthy.** `brain-sync.py --pull` runs at SessionStart (via `brain-health.py`) and `--push` runs after weekly `brain-consolidate.py --auto` — a device that never got its weekly schedule set up (see onboarding table in `README.md`) will still *receive* shared content on pull, but won't *contribute* anything back until that's fixed.

## Why this layout

- **Memory / Distilled / Knowledge / Agents** mirror four different lifecycles of Claude's memory: user-facts, session-output, evergreen-skills, agent-ops. Splitting them lets Claude write to the right place without judgment calls each time.
- **`_System/`** holds anything *about* the system rather than memory itself. Conventions, contracts, audits.
- **`_index.md` files** at every level let Obsidian's MOC pattern work without anyone curating link tables manually.
