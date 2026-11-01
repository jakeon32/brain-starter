---
title: brain-decay — Ebbinghaus forgetting sweep contract
type: spec
status: confirmed
updated: 2026-09-28
---

# brain-decay (forgetting / archive lifecycle)

> 개요(한국어): 저널(Decisions/Mistakes/Patterns)에 망각곡선을 적용하는 주간 스윕. 회상 안 된 저널은 보존율 R이 떨어져 `archived/`로 이동(검색 인덱스 제외 — **삭제 아님**). Knowledge는 semantic 계층이라 감쇠 면제. 근거: MemoryBank(arXiv 2305.10250) 공식 + Zep의 "삭제 대신 무효화" 철학.

## Model

```
R = exp(-t / (S × SCALE_DAYS))
  t = days since last_recalled (fallback: creation date)
  S = strength — starts 1 at distill; brain-search bumps +1 per recall
```

- `R < THRESHOLD` → move to `<category>/archived/<year>/` (already excluded
  from `brain-search` via `_brain_lib.iter_markdown_files`). Reversible.
- Archived + `EXPIRY_DAYS` with no recall → listed in
  `_System/audit/expiry-queue.md` for **human review**. No auto-delete, ever.
- Never-recalled journal lifetime ≈ `-ln(THRESHOLD) × SCALE_DAYS` ≈ 36 days
  at defaults — long enough for several weekly consolidate passes to converge
  clusterable journals into `Knowledge/` first.

## Recall write-back (brain-recall-read, since 2026-09-28)

A recall is an **actual read**, not a search hit. `brain-recall-read.py`
(PostToolUse, matcher `Read|Bash`) sees every `.md` path inside the vault that
Claude opens with Read or names in a Bash command (`sed`/`cat`/`head` reads are
as common as Read), and counts each file **once per session**:

- journals: `strength += 1`, `recall_count += 1`, `last_recalled = today`
  (frontmatter; missing fields inserted on first read)
- every category: a row in `reads` of `~/.claude/data/brain-usage.sqlite`
  (Knowledge frontmatter is never rewritten — `scope: shared` sync conflicts)

What no longer counts:

| Event | Before 2026-09-28 | Now |
|---|---|---|
| `brain-search` returns a hit | recall +1 | exposure only (`usage` table) |
| distill near-duplicate hit | recall +1, strength +1 | strength +1 only |
| file read by Claude | not counted | recall +1 (once per session) |

Why: counting exposure made the recall rate unmeasurable — 68/766 journals
"recalled" could not be told apart from 68 journals that merely ranked in a
top-k. This is the baseline for measuring auto-injection
(`brain-recall-inject.py`; readouts: `_System/recall-ratio.py`, `_System/recall-replay.py`). Expect recall counts
to grow more slowly from here; decay archives slightly more as a result, which
is reversible. Test: `_System/hooks/tests/test_brain_recall_read.py`.

## Scheduling

`brain-consolidate.py --auto` invokes `brain-decay.py --apply` at the end of
each weekly run — consolidation first, decay second, by design. No separate
schedule needed. Manual: `python3 ~/.claude/hooks/brain-decay.py` (preview) /
`--apply`.

## Env knobs

| Var | Default | Meaning |
|---|---|---|
| `BRAIN_DECAY_SCALE_DAYS` | `30` | Decay time-scale per unit strength |
| `BRAIN_DECAY_THRESHOLD` | `0.3` | Archive below this retention |
| `BRAIN_DECAY_MIN_AGE_DAYS` | `21` | Never touch journals younger than this |
| `BRAIN_DECAY_EXPIRY_DAYS` | `180` | Archived idle days before review queue |

## Receipts

`_System/audit/decay-YYYY-WNN.md` per apply run; `expiry-queue.md` rewritten
each run (idempotent review list).

## Related

[`consolidate.md`](consolidate.md) · [`search.md`](search.md) ·
[`stop-hook.md`](stop-hook.md) (importance gate + frontmatter seeds)
