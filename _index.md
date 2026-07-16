---
title: brain-starter — Top MOC
type: moc
status: stub
updated: 2026-07-16
---

> **개요 (한국어):** 이 저장소는 Claude Code용 개인 장기기억 볼트의 뼈대다. 아래 7개 방은 모두 비어 있는 것이 정상이며, 첫 세션부터 스스로 채워진다.

# brain-starter — Top MOC

This is the entry point into your memory vault. Every room below starts empty — that is by design, not a setup step you missed. The pipeline under [`_System/`](_System/structure.md) fills them as you use Claude Code; nothing here needs to be seeded by hand.

## Rooms

| Folder | What lands there | Who writes it | When |
|---|---|---|---|
| [`Memory/`](Memory/_index.md) | Stable facts about you, your projects, your devices | Claude's judgment | As Claude learns them |
| [`Decisions/`](Decisions/_index.md) | Lasting choices and the reasoning behind them | SessionEnd distill hook | Automatically, at session end |
| [`Mistakes/`](Mistakes/_index.md) | Errors that occurred, diagnosed (symptom → cause → fix) | SessionEnd distill hook | Automatically, at session end |
| [`Patterns/`](Patterns/_index.md) | Reusable approaches worth repeating | SessionEnd distill hook | Automatically, at session end |
| [`Knowledge/`](Knowledge/_index.md) | Reusable how-tos — the main search target | Claude's judgment | When something reusable is learned |
| [`Sessions/`](Sessions/_index.md) | Narrative journal entries, longer than a distilled note | By hand (you or Claude) | Whenever a session deserves a durable write-up |
| [`Agents/`](Agents/_index.md) | Operating notes for agents/subagents you use often | By hand or Claude's judgment | When an agent's usage pattern is worth remembering |

## Why empty is the correct starting state

A seeded example note would sit in the search index next to your real memory, indistinguishable from something Claude actually learned about you. Shipping empty rooms with only an `_index.md` each keeps the skeleton honest: what you see right after `git clone` is exactly the structure, nothing pretending to be content.

## See also

- [`_System/structure.md`](_System/structure.md) — full role and tracked-vs-ignored spec for every folder
- [`_System/conventions.md`](_System/conventions.md) — file naming and frontmatter rules
- [`_System/stop-hook.md`](_System/stop-hook.md) — how the SessionEnd distill hook actually works
