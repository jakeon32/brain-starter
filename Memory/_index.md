---
title: Memory — stable facts
type: moc
status: stub
updated: 2026-07-16
---

> **개요 (한국어):** 사용자·프로젝트에 대한 안정된 사실. Claude가 판단해서 쓴다.

> **What lands here:** Stable facts about you, your projects, your devices, your tools — the kind of thing that stays true for a while.
> **Who writes it:** Claude's judgment, during any session.
> **When:** As Claude learns something worth remembering — not on a schedule.

This folder is empty on purpose. Your first session fills it.

`Memory/` differs from `Knowledge/` in subject, not mechanism: both are Claude's judgment calls, but `Memory/` holds facts about you and your setup (who you are, what you're working on, what this device is), while `Knowledge/` holds how-tos and reusable procedures. On a fresh device, writing a `Memory/device-<name>.md` first — local paths, installed tooling, which accounts this device uses — is the usual starting point (see [`_System/conventions.md`](../_System/conventions.md) §Setting up brain on a new device). A `Memory/` file can opt into cross-device sync via `scope: shared` frontmatter — see [`_System/structure.md`](../_System/structure.md).
