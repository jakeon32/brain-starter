# CLAUDE.md

> **개요 (한국어):** 이 저장소는 개인 장기기억 볼트의 뼈대다. `_System/`은 골격(트래킹), 나머지 7개 방은 기기별 런타임 메모리(원칙적으로 언트래킹)다. 아래 "하지 말 것" 3가지가 이 저장소에서 가장 자주 되돌아오는 실수들이다.

## What this repository is

This repo plays two roles at once:

1. **A meta-system definition** — `_System/`, `_index.md`, `README.md`, `CLAUDE.md`, and every folder's `_index.md`. Tracked. This is what a fresh `git clone` gives a new device.
2. **Per-device runtime memory** — the actual content of `Memory/`, `Decisions/`, `Mistakes/`, `Patterns/`, `Sessions/`, `Knowledge/`, `Agents/`. Not tracked (with one narrow opt-in exception below). Each device fills these independently as Claude operates on it; nothing here is shared or synced by default.

`.gitignore` enforces the split: every runtime folder is ignored except its own `_index.md`, which stays tracked so a fresh clone shows the empty structure instead of nothing at all.

The one exception: a `Knowledge/*.md` or `Memory/*.md` file whose frontmatter carries `scope: shared` does get git-tracked and can propagate to other devices — a deliberate, per-file opt-in, not a default. See [`_System/structure.md`](_System/structure.md) for the exact mechanism.

## Do not

- **Do not commit anything that should be per-device runtime.** `Memory/*.md`, `Decisions/*.md`, `Mistakes/*.md`, `Patterns/*.md`, `Sessions/*.md`, `Knowledge/*.md`, `Agents/*.md`, and `_System/audit/**` are runtime content, not skeleton. `.gitignore` already excludes them; if you're about to `git add` something under one of these folders, check whether it's actually meant to be the shared skeleton (it almost never is) before overriding the ignore.
- **Do not revert the SessionEnd hook to wait for the distillation to finish.** It used to call `claude -p` inline and block for ~24s (p90 36s) at session exit; session teardown killed a chunk of those calls mid-flight and silently lost that session's memory. The hook now hands off to a detached worker and returns in well under a second — see [`_System/stop-hook.md`](_System/stop-hook.md). `_System/hooks/tests/test_distill_detach.py` exists specifically to fail loudly if this regresses.
- **Do not hand-run `brain-reindex.py` after writing a note "to keep search current."** `brain-search.py` already refreshes the index before every query on its own. The old contract — "whoever writes a note remembers to reindex" — was retired because it broke the same day it was relied on: someone edited a doc, searched without reindexing by hand, and got a stale answer back. See [`_System/search.md`](_System/search.md). If a note is ever missing from search, the bug is in the refresh chain, not in a forgotten manual step.

## Source of truth

`_System/` is the contract. When anything else — a filename, an assumption carried over from a previous session, this file's own summary — disagrees with a doc under `_System/`, `_System/` wins; read the actual doc rather than trust the name or a paraphrase.

Two places this specifically bites:

- `_System/stop-hook.md` describes the pipeline, but the hook event it wires up is `SessionEnd`, not `Stop` — `stop-hook` is a legacy filename from before the migration. Claude Code's actual `Stop` hook does not fire on `/exit` or an interrupt, so a device wired to `Stop` looks installed and never distills anything.
- The installer (`_System/install-stop-hook.sh` / `.ps1`) is idempotent. Running it a second time — e.g. right after `pip install`-ing the search dependencies — is expected, not redundant. See `README.md`'s Quick start.

## A few real commands

```sh
# search the vault
python ~/.claude/hooks/brain-search.py "natural-language query"

# (re)install or repair the pipeline on this device
./_System/install-stop-hook.sh --dry-run         # preview only, changes nothing
./_System/install-stop-hook.sh                   # macOS/Linux
powershell -File _System\install-stop-hook.ps1   # Windows

# hook-behavior regression tests (stdlib only, no test framework installed)
python _System/hooks/tests/test_distill_detach.py
python _System/hooks/tests/test_distill_catchup.py

# confirm this repo hasn't picked up anything private (also runs in CI)
python tools/check-purity.py
```

## Structure

Full folder-by-folder spec, including the tracked/ignored table: [`_System/structure.md`](_System/structure.md). File naming, frontmatter, and language rules: [`_System/conventions.md`](_System/conventions.md).
