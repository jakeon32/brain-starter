---
title: brain-consolidate — LLM-wiki Ingest/Lint contract
type: spec
status: confirmed
updated: 2026-07-12
---

# brain-consolidate (Ingest + Lint automation)

> 개요(한국어): 저널(Decisions/Mistakes/Patterns)을 주제별 위키(`Knowledge/`)로 수렴하는 디바이스 인프라. `brain-search`와 동일하게 brain 밖(`~/.claude/hooks/`)에 구현하고 brain은 컨트랙트만 둔다. 품질 룰은 [`consolidation-quality.md`](consolidation-quality.md).

## Tool

`~/.claude/hooks/brain-consolidate.py` — installed device-local (same boundary as `brain-search.py`; the index/derivable artifacts are not synced), but tracked source of truth is `_System/hooks/brain-consolidate.py` (since 2026-07-03) — installed by `install-stop-hook.sh`/`.ps1` along with the rest of the pipeline.

## Modes

- **`detect`** (default, read-only) — cluster the journal layer by title-vector (chunk_idx 0) cosine via union-find; report groups ≥ min-size.
- **`--ingest N [--apply]`** — consolidate cluster N into one `Knowledge/` page. Dry-run unless `--apply`.
- **`--auto [--dry-run]`** — UNATTENDED. Every cluster → quality-gated merge. `single_topic` fail, or big+low-cohesion, → split finer and retry. Other quality fail → hold (logged). Writes a receipt to `_System/audit/consolidate-YYYY-WNN.md`.

## Pipeline (Karpathy *Ingest*)

```
cluster → claude -p 통합(품질룰 프롬프트) → 6-rule self-assess
  + cohesion floor(객관 보강)
  → pass: write Knowledge page + index_add(_index 카탈로그) + archive 조각 + reindex
  → single_topic fail / big+incoherent: 세분화 후 재시도
  → other fail: hold (조각은 저널에 남고 다음 라운드 재시도)
```

## Safety

- **archive-not-delete** — consolidated fragments move to `<category>/archived/<year>/`, excluded from brain-search (`_brain_lib.iter_markdown_files`). Rollback = move back + delete page.
- **cohesion floor** (`AUTO_HARD_COS=0.35`, `AUTO_TRUST_SIZE=3`) overrides the LLM's lenient self-assessment for large incoherent groups (it once rated a 21-file blob single_topic=true).
- **quality gate** ([`consolidation-quality.md`](consolidation-quality.md)) holds bad merges instead of writing them.

## Schedule

Weekly `--auto`, per device — **not set up by the installer** (creating an
unattended scheduled task is a bigger step than copying files/patching hooks,
so it's left explicit). One-time setup:

- Windows: `schtasks /create /tn brain-consolidate-weekly /sc weekly /d SUN /st 04:00 /tr "'<python>' '<hooks-dir>\brain-consolidate.py' --auto"` (the installer prints this with your actual paths filled in)
- Mac/Linux: cron entry, e.g. `0 4 * * SUN python3 ~/.claude/hooks/brain-consolidate.py --auto` (the installer prints a ready-to-use crontab line)

If this is skipped, journal content (Decisions/Mistakes/Patterns) still
accumulates fine — only the convergence into `Knowledge/` wiki pages doesn't
happen. `brain-health.py`'s SessionStart check (added 2026-07-03) warns once
the journal has ≥15 files and no consolidate receipt has ever been written,
so a skipped schedule doesn't go unnoticed forever.

## Env knobs

`BRAIN_CONSOLIDATE_MODEL` · `_AUTO_COS` · `_HARD_COS` · `_TRUST_SIZE` · `_AUTO_DEPTH` · `_AUTO_STEP`.

## Decay chaining (P2, 2026-07-12)

`--auto` invokes `brain-decay.py --apply` after writing its receipt —
consolidation first (clusterable journals converge into Knowledge), decay
second (stale singles archive out of the search view). Contract:
[`decay.md`](decay.md).

## Related

[`search.md`](search.md) · [`decay.md`](decay.md) · [`consolidation-quality.md`](consolidation-quality.md) · [`stop-hook.md`](stop-hook.md) · design [`docs/plans/2026-06-04-llm-wiki-convergence-design.md`](../docs/plans/2026-06-04-llm-wiki-convergence-design.md).
