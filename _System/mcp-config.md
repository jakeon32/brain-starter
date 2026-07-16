---
title: MCP — not used for brain
type: spec
status: confirmed
updated: 2026-05-15
---

# MCP — not used for brain

> **개요 (한국어):** brain은 MCP 서버를 등록하지 않는다. Claude는 표준 파일 도구(Read/Write/Bash) 로 brain을 직접 다룬다. Obsidian MCP는 사용자 본인이 Obsidian 앱으로 brain을 열고 싶을 때만 (그것도 선택).

## Decision

The brain vault is operated by Claude through **direct filesystem access** — the same Read / Write / Bash tools Claude uses for any other folder. We do not register an MCP server for it.

## Why no MCP

- The Stop-hook (the only "automation" wired to the vault) writes via Python's `pathlib`, not through any MCP server. So MCP wouldn't gate distillation.
- Triage (one-time per-device walkthrough of a pre-existing personal notes vault) is hand-driven via Read/Write — no benefit from MCP.
- Adding an MCP layer is more moving parts to install per device, more failure modes, and no operational gain for this use case.
- Obsidian-specific features (`[[wiki-link]]` resolution, frontmatter indexing, search) come from running Obsidian on the vault. The user does that ~monthly for audits, not from inside a Claude session.

## What this means in practice

When Claude needs to read/write brain content:

```
Read ~/brain/Memory/profile.md
Write ~/brain/Knowledge/<area>/<slug>.md
Bash: ls ~/brain/Decisions/ | head
```

Same for that other notes vault (read-only in spirit, except during the one-time triage step):

```
Read ~/<other-notes-vault>/Memory/profile.md
```

No `obsidian-brain` or `obsidian-notes` MCP server needed.

## If you (the user) want Obsidian-aware MCP later

This is independent of the brain plan. If you decide you want Claude to use Obsidian-style operations (e.g. for `[[link]]` traversal in tools that support it), you can register one then — it's additive and doesn't change anything in this repo. Reasonable choices:

- `@bitbonsai/mcpvault` — npx, vault-rooted
- `@notionhq/notion-mcp-server` style equivalents for Obsidian

Add to `~/.claude.json` `mcpServers`, restart Claude Code, done. None of brain's `_System/*` contracts depend on it.

## What about other MCP servers already registered

A `claude mcp list` snapshot from a real device showed several domain-specific
MCP servers already in use for unrelated work — a couple of note/knowledge
tools, some creative/media integrations, a couple of chat-app channel plugins.
No Obsidian server was among them. That's the normal case: brain not needing
MCP doesn't mean nothing else on the device does, and adding one for brain
later is additive regardless of what else is already registered.
