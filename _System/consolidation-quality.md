---
title: consolidation quality rules (LLM-wiki Lint gate)
type: spec
status: confirmed
updated: 2026-06-04
---

# Consolidation quality rules

> 개요(한국어): `brain-consolidate`가 저널(Decisions/Mistakes/Patterns)을 위키 페이지로 통합할 때 통과해야 하는 6개 품질 룰. **사람이 큐레이트하는 schema** — 통합도 점검도 자동(claude 자가평가)이다. 미달 시: single_topic은 세분화 재시도, 나머지는 보류(audit 로그).

The consolidating LLM self-assesses these six rules after each merge (the
`quality` object in the structured output). `brain-consolidate --auto` gates on
them — this is the Karpathy *Lint* step folded into Ingest, so consolidation is
unattended yet quality-bound.

| Rule | Pass condition | On fail |
|---|---|---|
| **complete** | 원본 조각의 핵심 사실·노하우가 손실 없이 통합됨 | hold |
| **coherent** | 통합본 안에 자기모순 없음 | hold |
| **current** | 모순은 최신 조각 우선으로 정리, 폐기된 주장 명시 | hold |
| **single_topic** | 한 주제로 응집됨 (여러 주제 잡탕이 아님) | **split finer & retry** |
| **format_ok** | frontmatter·제목구조·한국어 톤·재사용 how-to 형식 | hold |
| **lean** | 커밋해시·파일경로·스택트레이스 등 형상관리 덤프 없음 | hold |

## Gate behavior

- **Pass** (six true) → write the page to `Knowledge/`, move source fragments
  to `<category>/archived/<year>/`.
- **single_topic fail** → raise the cosine threshold by a step and re-cluster
  *within* this group, then retry each sub-cluster (up to a max depth). This is
  how big mixed clusters (e.g. the 21-file brain-ops blob) get split into
  cohesive sub-topics automatically — no human triage.
- **other fail** → hold: fragments stay in the journal, logged in the receipt,
  retried next run.
- **Receipt** every run → `_System/audit/consolidate-YYYY-WNN.md` (per-device
  runtime; the human MAY skim it but is not required to act).

## Human role

Curate THIS file (the rules). Everything downstream — clustering, merging,
self-checking, splitting, archiving — is automatic. To change a rule, edit this
doc and the matching `INGEST_SCHEMA` / `build_ingest_prompt` in
`~/.claude/hooks/brain-consolidate.py`.
