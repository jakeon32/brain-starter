---
title: brain-consolidate — LLM-wiki Ingest/Lint contract
type: spec
status: confirmed
updated: 2026-10-29
---

# brain-consolidate (Ingest + Lint automation)

> 개요(한국어): 저널(Decisions/Mistakes/Patterns)을 주제별 위키(`Knowledge/`)로 수렴하는 디바이스 인프라. 2026-07-19부터 하이브리드 — 평시 소량은 주간 잡이 무인 처리하고, 백로그는 호출 상한(80회) 안에서 소화한 뒤 나머지만 대기열로 넘긴다(2026-09-27 변경 — 원래는 LLM 0회였으나 대기열이 줄지 않고 불기만 했다). 품질 룰은 [`consolidation-quality.md`](consolidation-quality.md).

## Tool

`~/.claude/hooks/brain-consolidate.py` — installed device-local (same boundary as `brain-search.py`; the index/derivable artifacts are not synced), but tracked source of truth is `_System/hooks/brain-consolidate.py` (since 2026-07-03) — installed by `install-stop-hook.sh`/`.ps1` along with the rest of the pipeline.

## Modes

- **`detect`** (default, read-only) — cluster the journal layer by title-vector (chunk_idx 0) cosine via union-find; report groups ≥ min-size.
- **`--ingest N [--apply]`** — consolidate cluster N into one `Knowledge/` page. Dry-run unless `--apply`.
- **`--weekly`** — the scheduled entry point (hybrid, 2026-07-19). Clusters ≤ `UNATTENDED_MAX_CLUSTERS` (env `BRAIN_CONSOLIDATE_UNATTENDED_MAX`, default 12) → runs `--auto` unattended here and now (normal weekly load, a handful of bounded calls). More (backlog) → refines the clusters the same way `--queue` does, drains them with `--auto` inside `AUTO_MAX_CALLS`, then writes the remainder to the queue. **Changed 2026-09-27**: this path used to make zero LLM calls (backlog is when unattended spawning ran away on 2026-07-19), but the run-level call cap already bounds that — and with zero calls every week's 21–29 new clusters tripped the threshold, the unattended path never ran after W33, and the queue grew 345 → 392 clusters because in-session processing (2–3 per session) cannot keep up.
- **`--queue`** — cluster only, LLM 0 calls; write `_System/audit/consolidate-queue.json`. `brain-health` surfaces pending clusters at SessionStart; the live session merges them itself (§Session processing). Since 2026-08-10 the queue is **size-capped** before it is written (§Chaining cap).
- **`--lonely [--dry-run]`** — fragments that no cluster can ever reach (2026-09-01). Three stages: **triage** (worth promoting at all? 25 briefs per call, verdicts persisted to `_System/audit/promotion-verdicts.json` so a fragment is never judged twice) → **theme assignment** (one call: the LLM names narrow topics from titles+reasons; the project fence is deliberately *off* here — see §Why lonely needs its own path) → **narrow merge** through the same quality gate. A theme that fails the gate puts its fragments on a `LONELY_RETRY_DAYS` (default 45) cooldown, which is what stops the weekly job from re-burning budget on the same failure. Receipt: `_System/audit/lonely-<date>.md`. `--weekly` chains into this after the unattended path, within the remaining budget.
- **`--auto [--dry-run]`** — UNATTENDED fallback, capped at `AUTO_MAX_CALLS` (env `BRAIN_CONSOLIDATE_MAX_CALLS`, default 80) claude calls per run; on budget exhaustion remaining clusters are held and carried over. Every cluster → quality-gated merge. `single_topic` fail, or big+low-cohesion, → split finer and retry. Other quality fail → hold (logged). Writes a receipt to `_System/audit/consolidate-YYYY-WNN.md`.

## Project routing first (2026-10-09)

`--weekly` now starts with `route_to_projects` (manual: `--route-projects [--dry-run]`): live fragments that belong to a project with a know-how book (`Knowledge/Projects/<slug>.md`, matched by frontmatter `folders:` or unique `keywords:`) are appended to `<slug>.inbox.md` and archived, instead of becoming standalone Knowledge pages. The project session folds the inbox into the book. Contract and rationale: [`knowledge-model.md`](knowledge-model.md).

## Pipeline (Karpathy *Ingest*)

```
cluster → nearest existing Knowledge page (title-vector cos ≥ 0.68, not scope:shared)
  → claude -p 통합(품질룰 프롬프트, 후보 페이지 본문 동봉) → action new|update
  → 6-rule self-assess + cohesion floor(객관 보강) + collapse guard(update만)
  → pass: write/update Knowledge page + archive 조각 + reindex
  → single_topic fail / big+incoherent: 세분화 후 재시도
  → other fail: hold (조각은 저널에 남고 다음 라운드 재시도)
```

## Update existing pages (2026-09-28)

Before 2026-09-28 every merge created a page. A topic that came back weeks later
got a second page (4 Cloudflare-tunnel pages, 6 hero-section pages — 08-28
structure audit), and a new page whose slug matched an old one overwrote it
without a trace. Roadmap P3; borrowed from OpenViking (`memory_diff`), Hindsight
(observations refined, not replaced), Letta (fix at the source).

- `nearest_page()` takes the centroid of the fragments' title vectors and finds
  the closest Knowledge page. At cos ≥ `UPDATE_COS` (env
  `BRAIN_CONSOLIDATE_UPDATE_COS`, default 0.68) its body goes into the prompt
  and the LLM picks `action: update` or `new`. 0.68 is from the 09-28 queue:
  every pair ≥ 0.72 and most at 0.67–0.68 were the same topic.
- Never offered as a target: `scope: shared` pages (they propagate to other
  devices — a person edits those), pages over 30,000 chars, `_*.md`.
- **Collapse guard** — an update whose body is shorter than `COLLAPSE_RATIO`
  (env `BRAIN_CONSOLIDATE_COLLAPSE`, default 0.6) of the old body fails the
  quality gate (`complete=false`) and is held (ACE context collapse).
- **Previous version** — `write_page` copies an existing page to
  `Knowledge/archived/<year>/<slug>.<YYYYMMDD-HHMMSS>.md` before writing.
  Knowledge/ is outside git; this copy is the only rollback.
- An update keeps the page's frontmatter (tags, scope, sources) and its
  `## 변경 이력`, bumps `updated:`, and appends one `Update` line.
- A **new** page whose slug exists gets `-2`, `-3`, … instead of overwriting.
- The prompt also tells the model to turn relative dates into absolute ones.

Test: `_System/hooks/tests/test_consolidate_update.py`.

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

## Why lonely needs its own path (2026-09-01)

> 개요(한국어): 짝이 없는 조각은 클러스터 경로가 **영원히** 처리하지 못한다. 묶을 상대가 없다는 것이 클러스터링의 전제를 어기기 때문이다. 그래서 전용 경로를 만들었다.

Measured 2026-08-28: 468 of 852 backlog fragments (55%) had no pair, so the
clustering path could not touch them no matter how often it ran. That is why
the weekly job was paused. 2026-09-01 ran all 209 promotable ones by hand and
the result set the design:

- **A bucket is not a topic.** Grouping the lonely ones by `(project × kind)`
  failed 8 of 9 groups on `single_topic`. Project is the right fence for
  *clustering* (2026-08-28, mixed-domain 78% → 0%) and the wrong one here —
  these fragments are lonely precisely because no neighbour shares their
  vocabulary, so the fence only collects unrelated things into one drawer.
- **Let the LLM name the topic.** Titles + one-line triage reasons are enough;
  bodies are not needed and would not fit. One call themed 200 fragments.
- **Narrow beats broad.** The same fragments merged at 57% under 28 broad
  themes and 69% under 26 narrow ones. The prompt therefore bans domain names
  (「보안」) in favour of one-story topics (「XSS·프록시 입력 검증」).
- **Record the failure.** 160 of 209 merged into 39 pages; the 49 that failed
  twice are left in the journal on a cooldown rather than retried weekly.

## Chaining cap (2026-08-10)

> **개요 (한국어):** `cluster()`는 단일연결 union-find라 A~B, B~C가 각각 임계를 넘으면 **A와 C가 남남이어도 한 덩어리**가 된다. 조각이 쌓일수록 이 체이닝으로 거대 블롭이 생기고, 그 블롭은 단일 주제가 아니라서 **통합이 영원히 불가능**한 채 대기열을 채운다.

Observed twice: 923 fragments (07-31) and 333 (08-09) — the latter had **zero
common slug tokens** and top tags `-·workflow·image·tts`, i.e. a mixed blob,
while the other 46 clusters held 165 fragments between them. Re-clustering at a
higher threshold cleared it once; it came back as soon as the journal refilled.
**Raising the threshold treats the symptom.**

`do_queue` now runs `refine_for_queue()` before writing:

```
cluster > QUEUE_MAX_SIZE ─► re-cluster it at thr + AUTO_COS_STEP (up to AUTO_MAX_DEPTH)
   still one lump?  ─► raise again, spending a depth level
   split?           ─► recurse per sub-cluster; sub-clusters of 1 drop to singleton
   never splits     ─► NOT queued. recorded under the queue's `oversized` key
```

An oversized lump is **not a merge target — it is a decay target**; its
fragments stay in the journal and `brain-decay` archives them on the normal
forgetting curve. Recording them under `oversized` keeps that visible instead of
silently dropping them.

Measured on 174 live journals (2026-08-10), same input before/after:

| threshold | biggest cluster before | after | excluded |
|---|---|---|---|
| 0.60 (default) | 15 | 7 | 0 |
| 0.45 | 169 | 6 | 0 |
| 0.35 | 174 | 4 | 1 lump / 24 frags |

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
   - A fragment with no real partner (the cluster was two unrelated topics)
     gets `consolidate: standalone` in its frontmatter. `is_standalone()` drops
     it from both the cluster and lonely paths, so it is not re-held every week.
     Why (2026-10-29): the same mixed clusters were held W41, W42 and W43 —
     pairs cannot be split finer, so each run spent a claude call for the same
     `single_topic=false`. The fragment stays in the journal and in search.
   - Before writing a page, check whether an existing Knowledge page already
     holds the fragment; then archive the fragment without a new page
     (2026-10-29: 6 of 34 queued fragments were already absorbed).
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

`BRAIN_CONSOLIDATE_MODEL` · `_AUTO_COS` · `_HARD_COS` · `_TRUST_SIZE` · `_AUTO_DEPTH` · `_AUTO_STEP` · `_MAX_CALLS` (run-level claude-call cap, default 80) · `_UNATTENDED_MAX` (`--weekly` hybrid threshold, default 12 clusters) · `_QUEUE_MAX_SIZE` (largest cluster allowed into the queue, default 12 — §Chaining cap) · `_LONELY_MAX` (calls allowed to the `--lonely` path in one run, default 30) · `_LONELY_RETRY_DAYS` (cooldown after a lonely merge fails, default 45).

## Decay chaining (P2, 2026-07-12)

`--auto` invokes `brain-decay.py --apply` after writing its receipt —
consolidation first (clusterable journals converge into Knowledge), decay
second (stale singles archive out of the search view). Contract:
[`decay.md`](decay.md).

## Related

[`search.md`](search.md) · [`decay.md`](decay.md) · [`consolidation-quality.md`](consolidation-quality.md) · [`stop-hook.md`](stop-hook.md).
