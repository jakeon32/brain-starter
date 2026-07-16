---
title: Knowledge — reusable how-tos
type: moc
status: stub
updated: 2026-07-16
---

> **개요 (한국어):** 재사용 가능한 how-to. Claude가 판단해서 쓰며, 의미 검색의 주 대상이다.

> **What lands here:** A reusable how-to — a protocol, a tool's quirks, a piece of external-service usage worth not re-discovering next time.
> **Who writes it:** Claude's judgment, during any session.
> **When:** Whenever something reusable is learned — not on a schedule.

This folder is empty on purpose. Your first session fills it.

This is the primary target of the search index (see [`_System/search.md`](../_System/search.md)) — when you ask Claude to recall something, this is mostly what it searches. Unlike `Decisions/`, `Mistakes/`, and `Patterns/`, nothing writes here automatically; Claude decides in the moment that a fact is durable and reusable enough to deserve a page, written with full frontmatter per [`_System/conventions.md`](../_System/conventions.md). A `Knowledge/` file can opt into cross-device sync via `scope: shared` frontmatter if you run more than one device — see [`_System/structure.md`](../_System/structure.md) for that mechanism. This `_index.md` itself is excluded from the search index; only what you add beneath it gets indexed.
