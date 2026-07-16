---
title: Decisions — lasting choices and why
type: moc
status: stub
updated: 2026-07-16
---

> **개요 (한국어):** 세션 종료 시 SessionEnd 증류 훅이 자동으로 기록하는, 오래 가는 선택과 그 이유. 사람이 손으로 쓰지 않는다.

> **What lands here:** A lasting choice made during a session (architecture, tool selection, structural direction) plus the reasoning behind it.
> **Who writes it:** The SessionEnd distill hook (`distill-session.py`).
> **When:** Automatically, when a session ends — no manual step.

This folder is empty on purpose. Your first session fills it.

Each entry is one file, one decision, named `YYYY-MM-DD-<kebab-slug>.md`. The hook reads the session transcript at `SessionEnd`, asks a model to extract up to a handful of durable decisions, and writes them here as plain Markdown with auto-generated frontmatter — you never write to this folder by hand. Keeping decisions separate from `Mistakes/` and `Patterns/` lets you scan "what did we commit to, and why" without wading through everything else that happened in a session. See [`_System/stop-hook.md`](../_System/stop-hook.md) for exactly how the hook decides what counts.
