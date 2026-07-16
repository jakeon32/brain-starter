---
title: Mistakes — errors, diagnosed
type: moc
status: stub
updated: 2026-07-16
---

> **개요 (한국어):** 실제로 발생하고 원인까지 진단된 오류(증상→원인→수정)를 SessionEnd 증류 훅이 자동으로 기록한다.

> **What lands here:** An error that actually occurred during a session, with its symptom, root cause, and fix — not a hypothetical or a style nitpick.
> **Who writes it:** The SessionEnd distill hook (`distill-session.py`).
> **When:** Automatically, when a session ends — no manual step.

This folder is empty on purpose. Your first session fills it.

Only mistakes that were diagnosed to a real cause land here, one file per session that had any. Keeping this separate from `Decisions/` and `Patterns/` matters because the same failure tends to resurface across unrelated sessions weeks apart — isolating it here makes "have I hit this before" searchable without noise from ordinary decisions. See [`_System/stop-hook.md`](../_System/stop-hook.md) for what qualifies and how the extraction works.
