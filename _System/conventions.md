---
title: brain — Conventions
type: spec
status: confirmed
updated: 2026-07-13
---

# brain — Conventions

> **개요 (한국어):** 파일명·frontmatter·날짜·언어 규칙. 모든 새 파일은 이 규칙을 따른다.

## File naming

- **kebab-case** for everything: `session-resume.md`, not `SessionResume.md` or `session_resume.md`
- **Distilled is the only exception**: prefix with date — `YYYY-MM-DD-<slug>.md` (e.g. `2026-05-15-mcp-restart.md`). The Stop-hook generates these.
- No spaces in filenames. Ever.
- ASCII only in filenames; Korean and English freely in content.

## Frontmatter

Every `.md` file under `Memory/`, `Knowledge/`, `Agents/`, `_System/` (and `docs/`) MUST have YAML frontmatter:

```yaml
---
title: <human-readable title>
type: <see types below>
status: <stub | draft | confirmed>
updated: YYYY-MM-DD
---
```

`Distilled/` files are append-only and skip frontmatter — the date is in the filename.

### `type:` values

| Value | Use for |
|---|---|
| `moc` | A `_index.md` file (Map of Contents) |
| `note` | Most ordinary notes |
| `spec` | Specifications and contracts (`_System/*`) |
| `plan` | Planning documents |
| `runbook` | Step-by-step operational procedures |

### `status:` values

| Value | Meaning |
|---|---|
| `stub` | Placeholder, structure only |
| `draft` | In progress, content present but not finalized |
| `confirmed` | Reviewed and considered authoritative |

### Optional fields — `Knowledge/` and `Memory/` only (2026-07-13)

Everything below is **optional**. None of the ~380 existing `Knowledge/`+`Memory/` files need retrofitting — add these only when they're actually useful for the file at hand. Adopted from auditing a team-scale sibling system with the same raw→refined promotion pattern.

```yaml
scope: shared                    # opt-in — see below
confidence: confirmed            # or: needs-verification
supersedes: <filename>
valid_from: YYYY-MM-DD
valid_to: YYYY-MM-DD
```

| Field | Meaning | Use it when |
|---|---|---|
| `scope: shared` | This file is git-tracked and synced across devices (default without this field: local-only, per-device, exactly as before). **Only meaningful in `Knowledge/*.md` and `Memory/*.md`** — journals (`Decisions/Mistakes/Patterns`) and `Sessions/` never take this field, they stay per-device unconditionally. | A note is durable and device-independent — a policy, design principle, or reusable standard, not a device-bound how-to or a raw session fact. Promotion is always a deliberate act (by Claude or the user), never a silent default. |
| `confidence: confirmed \| needs-verification` | Whether a factual/numeric claim has been verified or is still tentative. | Facts and numbers, especially `scope: shared` ones another device might cite without the context to double-check them. |
| `supersedes: <filename>` | This file explicitly replaces another (kept, never deleted — matches the vault-wide "archive, don't delete" rule already used by `decay.md`/`consolidate.md`). | A whole document is being restructured/replaced by a new one — not for ordinary in-place edits, which git history already tracks. |
| `valid_from` / `valid_to` | The period a fact was true. Omit `valid_to` while still current. | Time-bound facts (a policy, a price, an org structure) that will predictably go stale — mainly relevant for `scope: shared` content another device might read long after it was written. |

## Setting up brain on a new device (2026-07-14)

> **개요 (한국어):** 새 기기의 brain은 **`git clone`만**으로 만든다. 기존 기기의 brain 폴더를 통째로 복사하면 그 기기 전용 파일(로컬 경로·개인 토큰·그 기기 도구)이 딸려가 새 기기가 남의 사실을 자기 정본으로 읽게 된다.

**`git clone` only. Never copy an existing device's brain folder.**

A clone gives exactly the right thing: the framework (`_System/`, `docs/`, hooks, `_index.md` skeletons) plus the opt-in `scope: shared` files. Everything else — `Memory/*`, unshared `Knowledge/*`, journals, `Sessions/` — is per-device by design and must be written fresh by the new device as it operates.

Copying the folder instead defeats that boundary: device-local files (local paths, personal API tokens, that machine's tool inventory, that machine's Notion workspace) land on the new device, which then reads another machine's facts as its own authoritative memory. This is not stale content that a later edit can fix — the origin device cannot repair it, because those files are not git-synced. It has to be cleaned up by hand on the polluted device.

*(This happened once: the office PC's brain was seeded by copying the home PC's folder, and the office Claude spent sessions treating the home PC's Notion token, DB ids, and tool paths as its own. Diagnosed 2026-07-14.)*

After cloning, the new device fills in its own `Memory/device-<name>.md` first — assets, paths, which Notion workspace it talks to, installed tooling.

## Dates

ISO format only: `YYYY-MM-DD` (e.g. `2026-05-15`). Never `2026/05/15` or `May 15, 2026` in metadata.

## Language

- **Frontmatter values**: English
- **Body**: English by default for `_System/` and `docs/` (so token-efficient and stable for Claude). The user may translate if they want to read.
- **Each meta-system doc** opens with one Korean line summary (`> **개요 (한국어):** ...`) so the user can skim without translating.
- **Memory / Knowledge / Distilled / Agents content** can be in whatever language fits the conversation that produced it. No rule.

## Wiki links

- Use `[[file-name]]` for cross-vault references inside the brain
- Use Markdown links `[text](path/to/file.md)` when you want custom link text
- Avoid linking from brain to your other notes vault — they are independent vaults

## Required behaviors when writing a new file

1. Pick the right top-level folder per [`structure.md`](structure.md)
2. Add full frontmatter
3. Update the `updated:` date when you change the file
4. If you add a new important file, link it from the relevant `_index.md`

## Required behaviors when editing an existing file

1. Bump `updated:`
2. If you change scope, update `status:` (e.g. `stub` → `draft`)
3. Don't rename without updating links pointing at it
