---
title: brain-consolidate — LLM-wiki Ingest/Lint contract
type: spec
status: confirmed
updated: 2026-08-06
---

# brain-consolidate (Ingest + Lint automation)

> 개요(한국어): 저널(Decisions/Mistakes/Patterns)을 주제별 위키(`Knowledge/`)로 수렴하는 디바이스 인프라. 2026-07-19부터 하이브리드 — 평시 소량은 주간 잡이 무인 처리하고, 백로그는 LLM 호출 없이 대기열만 만들어 다음 세션의 claude가 직접 통합한다(한 기기에서 무인 `--auto`가 claude 자식 프로세스를 폭증시켜 구독 한도를 태운 사건이 계기). 품질 룰은 [`consolidation-quality.md`](consolidation-quality.md).

## Tool

`~/.claude/hooks/brain-consolidate.py` — installed device-local (same boundary as `brain-search.py`; the index/derivable artifacts are not synced), but tracked source of truth is `_System/hooks/brain-consolidate.py` (since 2026-07-03) — installed by `install-stop-hook.sh`/`.ps1` along with the rest of the pipeline.

## Modes

- **`detect`** (default, read-only) — cluster the journal layer by title-vector (chunk_idx 0) cosine via union-find; report groups ≥ min-size.
- **`--ingest N [--apply]`** — consolidate cluster N into one `Knowledge/` page. Dry-run unless `--apply`.
- **`--weekly`** — the scheduled entry point (hybrid, 2026-07-19). Clusters ≤ `UNATTENDED_MAX_CLUSTERS` (env `BRAIN_CONSOLIDATE_UNATTENDED_MAX`, default 12) → runs `--auto` unattended here and now (normal weekly load, a handful of bounded calls). More → **zero LLM calls**; writes the queue for in-session processing (backlog is exactly when unattended spawning runs away).
- **`--queue`** — cluster only, LLM 0 calls; write `_System/audit/consolidate-queue.json`. `brain-health` surfaces pending clusters at SessionStart; the live session merges them itself (§Session processing).
- **`--auto [--dry-run]`** — UNATTENDED fallback, capped at `AUTO_MAX_CALLS` (env `BRAIN_CONSOLIDATE_MAX_CALLS`, default 80) claude calls per run; on budget exhaustion remaining clusters are held and carried over. Every cluster → quality-gated merge. `single_topic` fail, or big+low-cohesion, → split finer and retry. Other quality fail → hold (logged). Writes a receipt to `_System/audit/consolidate-YYYY-WNN.md`.

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
- **fail-open on missing fragments** (2026-07-19) — a fragment path that no longer resolves is logged and skipped, never raised. A `FileNotFoundError` used to kill the whole run; stale paths are normal because dedupe/decay move files between queue generation and processing.

### Diagnosing a mass failure

> **개요 (한국어):** `rc=1` + stderr 빈 값으로 전량 실패하면 **세션 한도 소모**를 먼저 의심한다. 환경(SSL·네트워크) 탓으로 넘겨짚지 말 것.

650 consolidate calls once failed with `rc=1` and an empty stderr. It was misdiagnosed as an
SSL-blocked environment; the real cause was the **Discord plugin booting inside every `claude -p`
child and exhausting the gateway session limit**. Both look identical from the log. The
discriminator is a **single isolated call** — run one by hand with the plugin surface removed. Check
session-limit exhaustion before blaming the environment.

## Session processing (2026-07-19)

When `--weekly`/`--queue` leaves a pending queue, `brain-health` prints
"통합 대기 N클러스터/M조각" at SessionStart. The live session's claude then
processes it directly — no `claude -p` children, no extra system-prompt cost,
ambiguous merges can be asked about on the spot. Contract per cluster:

1. Read `_System/audit/consolidate-queue.json`; skip fragments that no longer
   exist (they move/archive between queue generation and processing).
2. Read the fragments; merge only genuinely single-topic groups into one
   `Knowledge/<slug>.md` page (conventions: frontmatter `title/type/status/
   updated`, kebab-case ASCII slug). Mixed groups: split by topic or leave —
   quality rules in [`consolidation-quality.md`](consolidation-quality.md).
3. Move merged fragments to `<category>/archived/<year>/` (archive-not-delete).
4. Process incrementally — 2-3 clusters per session is fine; the queue and
   next weekly run pick up the rest.
5. When done (or partially done): rewrite the queue file minus the processed
   clusters (or delete it if empty), and write/update the weekly receipt
   `_System/audit/consolidate-YYYY-WNN.md` (통합/보류 counts + slugs) so the
   receipt-age health check stays meaningful. Then best-effort:
   `brain-decay.py --apply` and `brain-sync.py --push` (same chaining as
   `--auto`; search reindexes itself on next query).

## Schedule

Weekly `--weekly`, per device — **not set up by the installer** (creating an
unattended scheduled task is a bigger step than copying files/patching hooks,
so it's left explicit). One-time setup:

- Windows: `schtasks /create /tn brain-consolidate-weekly /sc weekly /d SUN /st 14:00 /tr "'<python>' '<hooks-dir>\brain-consolidate.py' --weekly"` (the installer prints this with your actual paths filled in). An existing task made for `--auto`: `schtasks /change /tn brain-consolidate-weekly /tr "..."` with the new argument.
- Mac/Linux: cron/launchd entry, e.g. `0 14 * * SUN python3 ~/.claude/hooks/brain-consolidate.py --weekly` (the installer prints a ready-to-use crontab line). Daytime beats 04:00 — a sleeping/locked machine fails claude calls (2026-07-19: every 04:08 call exited rc=1); launchd/schtasks-StartWhenAvailable run a missed slot at next wake, which is exactly when the user is present.

If this is skipped, journal content (Decisions/Mistakes/Patterns) still
accumulates fine — only the convergence into `Knowledge/` wiki pages doesn't
happen. `brain-health.py`'s SessionStart check (added 2026-07-03) warns once
the journal has ≥15 files and no consolidate receipt has ever been written,
so a skipped schedule doesn't go unnoticed forever.

## Env knobs

`BRAIN_CONSOLIDATE_MODEL` · `_AUTO_COS` · `_HARD_COS` · `_TRUST_SIZE` · `_AUTO_DEPTH` · `_AUTO_STEP` · `_MAX_CALLS` (run-level claude-call cap, default 80) · `_UNATTENDED_MAX` (`--weekly` hybrid threshold, default 12 clusters).

## Decay chaining (P2, 2026-07-12)

`--auto` invokes `brain-decay.py --apply` after writing its receipt —
consolidation first (clusterable journals converge into Knowledge), decay
second (stale singles archive out of the search view). Contract:
[`decay.md`](decay.md).

## Related

[`search.md`](search.md) · [`decay.md`](decay.md) · [`consolidation-quality.md`](consolidation-quality.md) · [`stop-hook.md`](stop-hook.md).
