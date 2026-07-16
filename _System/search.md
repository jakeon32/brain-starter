---
title: brain — Semantic search index (P1)
type: spec
status: confirmed
updated: 2026-07-03
---

# brain — Semantic search index

> **개요 (한국어):** brain 코퍼스에 대한 하이브리드 의미 검색(BM25 + 벡터). 외부 서버·MCP·iii-engine 없이 SQLite 한 파일과 로컬 임베딩 모델로 동작. Markdown은 진실원천 그대로이고 인덱스는 derivable이라 언제든 삭제 후 재빌드 가능. 설계는 agentmemory의 하이브리드 검색 접근을 차용했다.

## What it is

A pair of Python CLIs that maintain a local search index over brain's runtime markdown:

- `~/.claude/hooks/brain-reindex.py` — index builder (incremental + full)
- `~/.claude/hooks/brain-search.py` — hybrid search CLI
- `~/.claude/hooks/_brain_lib.py` — shared library (DB, chunking, embeddings)

Tracked source of truth: `_System/hooks/*.py` (since 2026-07-03; installed
device-local by `install-stop-hook.sh`/`.ps1`, same as the distill/health
hooks — see [stop-hook.md](stop-hook.md) §Reproducible setup).

The index lives at `~/.claude/data/brain-index.sqlite` (per-device, not synced — derivable from the brain vault's Markdown).

## What it indexes

The seven runtime folders:

```
~/brain/{Decisions, Mistakes, Patterns, Sessions, Knowledge, Memory, Agents}/**/*.md
```

Excluded by design:
- `_System/`, `docs/` — tracked specs/planning, low value as recall targets
- `_index.md` files — pure link tables (Obsidian MOCs)
- `.obsidian/`, `.git/` — vault metadata

## How it works

```
brain Markdown file
  -> strip frontmatter
  -> chunk by paragraph (~500 chars, merge tiny tails)
  -> prepend synthetic "title boost chunk" (filename slug + H1 + category)
  -> embed each chunk with sentence-transformers
  -> store in SQLite:
       chunks (canonical text + char offsets)
       chunks_fts (FTS5 BM25 index over chunk text)
       chunks_vec (sqlite-vec cosine over 384-dim embedding)

query
  -> embed query (same model)
  -> top-30 from BM25  (FTS5 bm25() over OR-joined query tokens)
  -> top-30 from vec   (sqlite-vec MATCH ... k=30)
  -> Reciprocal Rank Fusion (k=60)
  -> dedupe per file (best chunk wins)
  -> snippet (body chunk, not title boost) + score + file path
```

## Embedding model

| | |
|---|---|
| Model | `paraphrase-multilingual-MiniLM-L12-v2` |
| Dim | 384 |
| Size | ~420MB (cached at `~/.cache/huggingface/hub/`) |
| Speed | ~70ms/sentence CPU, batch 50 in 0.09s |
| Korean support | Good for specific terms; weaker for abstract Korean→English bridging |
| Override | env `BRAIN_EMBED_MODEL=<name>` then full reindex |

Why this model: balances size, speed, and Korean coverage. Empirically: KO-KO same-topic cosine ~0.6, unrelated ~0.2 (gap 0.4 — good separation). KO-EN cross-lingual on same topic ~0.43 (workable). All-English MiniLM-L6 was tested first and rejected: KO query vs EN body scored 0.07 = pure noise.

## Configuration env vars

| Var | Default | Purpose |
|---|---|---|
| `BRAIN_VAULT` | `$CLAUDE_DISTILL_VAULT` if set, else `~/brain` | brain root (explicit override; normally leave unset and let `CLAUDE_DISTILL_VAULT` — set by the installer's launchers — flow through) |
| `BRAIN_INDEX` | `~/.claude/data/brain-index.sqlite` | index path |
| `BRAIN_EMBED_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | sentence-transformers model name |
| `BRAIN_EMBED_DIM` | `384` | must match model |

If you change `BRAIN_EMBED_MODEL` or `BRAIN_EMBED_DIM`, do a `--full` reindex.

## Reproducible setup (per device, one command)

This is per-device runtime infrastructure, but the scripts themselves are
tracked (`_System/hooks/*.py`) and installed by the same installer as the
distill/health hooks:

```sh
cd ~/brain
./_System/install-stop-hook.sh       # Mac/Linux, add --dry-run to preview
# or: powershell -File _System\install-stop-hook.ps1   # Windows
```

The installer copies `brain-reindex.py` / `brain-search.py` / `_brain_lib.py`
(and the rest of the pipeline) to `~/.claude/hooks/`, and **checks** whether
the resolved python has `sqlite_vec` + `sentence_transformers` importable —
printing a loud warning with the exact `pip install` command if not (it does
not install them for you; these two packages are the only per-device manual
step left):

```sh
pip install sqlite-vec sentence-transformers
# (torch should already be present on this device's Python)
```

**The installer builds the first index itself** — but only if the ML stack was
already importable *when it ran*; otherwise it skips the build (silently, by
design: it warns about the missing packages instead). So the order on a fresh
device is:

1. run the installer
2. `pip install sqlite-vec sentence-transformers`
3. **run the installer again**

The installer's own warning says exactly this ("Re-run this installer afterwards
to pin the interpreter and build the index"). Skipping step 3 leaves a device
where the scripts are copied and the interpreter is pinned — everything *looks*
installed — and the index is empty. Do not hand-run `brain-reindex.py --full` as
a setup step; that was the manual procedure until 2026-07-16 and is now the
installer's job.

Then prove it. This is the only check that can:

```sh
python ~/.claude/hooks/brain-search.py "전 디바이스 독립 brain"
```

Every other post-install check — scripts present, hooks registered, interpreter
pinned, distill writing notes — **passes on a device whose index never built**.
Search returning nothing is indistinguishable from "you have no such note".

Why the dep-check matters: `brain-reindex.py`/`brain-search.py`/
`brain-consolidate.py` are normally spawned **detached** by `brain-health.py`
with stderr suppressed — a missing import fails completely silently, so
without this check a fresh device's search/consolidate layer could stay
broken indefinitely with no visible signal.

## Usage

### Direct CLI (terminal or Bash tool)

```sh
# basic query (top 5)
python ~/.claude/hooks/brain-search.py "Wan 2.2 메모리 이슈"

# more results
python ~/.claude/hooks/brain-search.py "..." -k 10

# restrict to one folder
python ~/.claude/hooks/brain-search.py "..." --category Decisions

# JSON output for programmatic use
python ~/.claude/hooks/brain-search.py "..." --json

# debug: see BM25 + vec ranks per result
python ~/.claude/hooks/brain-search.py "..." --raw-scores
```

### From Claude

Claude should invoke `brain-search.py` via Bash whenever the user mentions:
- past work: "기억나?", "예전에", "지난번", "3주 전", "그때"
- a topic that may have prior context: brand names, project codes, tool names
- a question that would be answered by a specific past decision/mistake/pattern

Default top-K = 5 is enough. Use `--category Decisions` if you want to bias toward authoritative past decisions vs raw session narrative.

### Latency

- Cold start (first call per session): ~12s (sentence-transformers + model load)
- Warm (subsequent calls in same Python process — not common via Bash): ~250ms

The 12s cold-start cost is acceptable for occasional recall queries. For automatic per-message surfacing (P2 plan) we will need a daemon mode or BM25-only fast path.

## Maintenance

### Incremental reindex

Two things keep the index fresh, and **neither is you**:

1. `distill-session.py` spawns a detached incremental reindex after each
   successful distill (since 2026-06-01).
2. **`brain-search.py` refreshes the index before every search** (since
   2026-07-16) — +0.15s, ~1.7% overhead, fail-open. Disable with
   `--no-reindex`.

(2) retired the old contract that *"whoever writes a note must remember to run
reindex"*. That contract was broken the same day it was audited: the auditor
edited docs, searched without reindexing, and the search silently returned the
pre-edit index — the exact failure being reported. **Do not re-add "run reindex
after writing" to any doc or guide.** If a note is missing from search, the bug
is in the refresh chain, not in the human.

Manual run (rarely needed — a refresh chain that is broken, or a bulk import):

```sh
python ~/.claude/hooks/brain-reindex.py             # incremental: new + changed files
```

This is fast (~1s per changed file).

**Freshness is monitored**, not assumed: the auto-reindex chain dies whenever
distill dies (2026-06-08~11: stale claude path → no distill → no reindex →
3-day-stale index, silently). The `brain-health.py` SessionStart hook
([health.md](health.md)) warns when the newest brain `.md` is > 24h newer
than `brain-index.sqlite`.

**Interpreter (this device):** run reindex/search with
`C:\...\Python310\python.exe` — bare `python` may resolve to a venv without
`sqlite_vec`/`sentence-transformers` and die on import.

### Full rebuild

If the model or schema changed, or to clear cruft:

```sh
python ~/.claude/hooks/brain-reindex.py --full
```

~40s for 200 files. The old `brain-index.sqlite` is deleted and rebuilt.

### Single-file reindex (after manual edit)

```sh
python ~/.claude/hooks/brain-reindex.py --file "~/brain/Decisions/some-file.md"
```

### Health check

```sh
python ~/.claude/hooks/brain-reindex.py --stats
```

Verifies that `chunks`, `chunks_fts`, and `chunks_vec` row counts match (any mismatch = corrupted index, do `--full`).

## Validation suite

P1 ship criterion: each of these queries returns the target file in top-3.

| # | Query | Target file | Status |
|---|---|---|---|
| 1 | 이전 세션 누락 복구 catchup 안전망 | `Decisions/2026-05-18-distill-catchup-as-primary-defense-over-hooks.md` | ✓ top-1 |
| 2 | Windows에서 Stop hook 실행 안 되는 문제 | `Decisions/2026-05-15-windows-stop-hook-cmd-launcher-pattern.md` | ✓ top-1 |
| 3 | my-project 홈페이지 색상 팔레트 | `Decisions/2026-05-27-my-project-home-color-palette-warm-cream-cobalt-coral.md` | ⚠ not in top-3; specific terms ("코발트 코랄") give top-1 |
| 4 | LTX-2.3 속도 한계 하드웨어 | `Decisions/2026-05-27-ltx-23-step-speed-is-hardware-limit-not-bug.md` | ✓ top-1 |
| 5 | MEMORY.md를 라우터로 쓰는 이유 deterministic state | `Decisions/2026-05-18-memory-md-active-projects-as-deterministic-state.md` | ✓ top-1 |

4/5 perfect. Q3 fails on generic Korean ("팔레트") that doesn't appear in body text; works when query uses specific terms from the body. This is the multilingual MiniLM-L12 model's known limitation (KO abstract → KO body bridging is weaker than EN-EN). Mitigation: use BGE-m3 or similar (~2GB) if Q3-like failures become frequent — swap the model name in `_brain_lib.py`'s `EMBED_MODEL` and full-rebuild the index.

## Known limitations

- **Cold start 12s** — fine for occasional Bash invocation; not fine for per-message hooks. P2 will need a daemon.
- **No filename in FTS** — search uses chunk text only. Title boost chunk partially compensates by including filename tokens.
- **Korean abstract recall** — see Q3 above.
- **No recency boost** — same-topic notes from 6 months ago and yesterday rank purely by relevance. Acceptable; recency is recoverable from filename date and STEP 0A.
- **Index not in git** — derivable; each device builds its own.

## Per-device independence

Same policy as everything else: each device builds + maintains its own index. Index files are never synced. If a device's index is broken, it doesn't affect other devices.

## See also

- [stop-hook.md](stop-hook.md) — Stop-hook that produces most of the indexed content
- [session-start-protocol.md](session-start-protocol.md) — current STEP 0A/0B protocol (per-project runbook owns the actual sequence)
