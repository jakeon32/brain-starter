---
title: knowledge model — know-how, cautions, records, resume points
type: spec
status: confirmed
updated: 2026-10-09
---

# Knowledge model

> 개요 (한국어): brain 에 남기는 것을 네 가지로 나눈다 — **노하우**(다시 쓸 방법·원리·판단 기준), **주의사항**(단편적 금지·함정 — 노하우의 한 절에 붙는 짧은 줄이지 독립 지식이 아니다), **기록**(한 일·진행 상태 — 지식이 아니므로 brain 밖의 업무 도구에 둔다), **재개점**(다음 세션 시작 위치 → resume). 프로젝트 노하우는 프로젝트마다 **노하우 북 한 장**(`Knowledge/Projects/<slug>.md`)에서 자란다. 이 기능은 선택 사항이다 — `Knowledge/Projects/` 가 없으면 관련 코드는 아무것도 하지 않는다.

## Why this exists

An audit of a long-running vault (2026-10-09, ~1,100 Knowledge pages) found:

- **Knowledge had become a pile of fragments.** 86% of pages were produced by unattended consolidation, mostly one incident → one page (median 1.9 KB). A «principle / how to apply» template dressed single-incident cautions up as know-how. 10–15% were duplicates, some contradicting each other.
- **No project axis.** No page carried a project field. A session working on a project had no way to load "what we know about this project" — only keyword luck.
- **The only place know-how actually grew** was a project-local canonical doc: one document, current methods only, no dated sections, referenced from the project's `CLAUDE.md`. That shape is the model.

## The four kinds

| Kind | Definition | Where it lives | Shape |
|---|---|---|---|
| **노하우 (know-how)** | Reusable method, principle or judgment criterion — what / why / how / when. Could guide future work on its own. | Project: `Knowledge/Projects/<slug>.md` (the know-how book). Cross-project: a `Knowledge/` topic page. | Grows in place: new facts update the relevant section of the existing document. No new page per incident. |
| **주의사항 (caution)** | A fragmentary prohibition, trap, or one-off lesson ("X breaks Y"). | A short bullet under «주의» at the end of the know-how section it belongs to. | One line + the reason in a few words. **Never a standalone page.** Obsolete ones are deleted, not kept. |
| **기록 (record)** | What was done, status, progress, dated narrative. | Your task tracker / work log — outside this vault. | Not knowledge. Keep out of `Knowledge/`. |
| **재개점 (resume point)** | Where the next session starts: folder, latest version, open identifiers. | `Memory/resume/<project>.md`, indexed by `Memory/session-resume.md`. | Entry points only; points to the project's know-how book. |

Test for a candidate: *would a session starting this kind of work next month be worse off without it?* If yes and it says how → know-how. If it only says "don't" → caution, attached to the know-how it guards. If it says what happened → record.

## Project know-how book

`Knowledge/Projects/<slug>.md`, frontmatter:

```yaml
title: <project> — 노하우 북
type: knowhow
project: <slug>
folders:            # working folders of this project
  - ~/work/<project-folder>
keywords:           # words that occur ONLY in this project (no shared terms)
  - <unique term>
updated: YYYY-MM-DD
```

Body: one line on what the project is and where its status and resume point live, then sections by area of work. Each section: current methods and criteria; «주의» bullets at its end; pending-but-undecided items marked «미확정» in one line. Final sections: source/tool locations, related knowledge links. Target 8–15 KB; when it grows past that, split by area into `Knowledge/Projects/<slug>/<area>.md` with the book as the index — never by date.

Project-local canonical docs stay where they are; the book links to them rather than copying.

## How know-how gets in

1. **In session (primary).** When something reusable is learned while working on a project, the session edits the book's relevant section on the spot (or adds a «주의» line).
2. **Inbox (safety net).** `brain-consolidate.py --weekly` first runs `route_to_projects` (also `--route-projects [--dry-run]`): live journal fragments whose `cwd` falls under a book's `folders` — or whose text contains one of its `keywords` — are appended to `Knowledge/Projects/<slug>.inbox.md` (Mistakes → caution candidates, Patterns/Decisions → know-how candidates) and archived. They no longer become standalone `Knowledge/` pages. The project's next session folds the inbox into the book and deletes the handled entries. Unattended LLM edits to the book itself are deliberately not done — that is how a vault turns into fragments.
3. Fragments with no project keep the cross-project path ([`consolidate.md`](consolidate.md)), which prefers updating an existing page.

## How know-how gets out

- `brain-recall-inject.py` (UserPromptSubmit): if the session's `cwd` falls under a book's `folders`, or the prompt contains one of its `keywords`, the book is injected first, once per session.
- **Why keywords, not just folders:** when several projects are worked on from the same folder, the folder cannot tell them apart. Keywords must be unique to the project — a shared term misroutes, so when in doubt leave it out; an unrouted fragment just stays on the general path. System text (task notifications, channel wrappers, file paths) is stripped before term extraction; a document already injected in the session is not repeated.
- The project's resume file and project `CLAUDE.md` point to the book.

## Related

[`consolidate.md`](consolidate.md) · [`decay.md`](decay.md) (recall = actual reads) · [`health.md`](health.md) (recall wiring check) · [`conventions.md`](conventions.md)
