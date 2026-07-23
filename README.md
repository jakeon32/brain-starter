# brain-starter

> **개요 (한국어):** Claude Code(및 Codex) 세션이 끝날 때마다 그 세션의 결정·실수·패턴이 자동으로 쌓이는 개인 장기기억 저장소의 뼈대. 왜 이렇게 생겼는지는 아래 brain-guide 링크에 있고, 실제로 쓰는 법은 이 문서에 있다.

brain-starter is the skeleton of a personal long-term memory vault for Claude Code (and Codex). Every time a session ends, a hook reads that session's transcript and writes out what's worth keeping — decisions made, mistakes and how they were fixed, patterns worth repeating — into plain Markdown files in this repo. Come back days or months later, in the same project or a different one, and Claude can search for what it already knows instead of re-learning it from scratch.

> **Language:** this README and the `_System/` contracts are in English, but the pipeline's own runtime output — the session-start health warnings and the watchdog alarm — is written in Korean. Everything runs the same either way; [`_System/health.md`](_System/health.md) describes what each check looks for, in English.

## Why it's shaped this way

The design rationale — why per-device instead of synced across machines, why a distill hook instead of one giant memory file, why search instead of re-reading old transcripts — lives in a separate guide, not here:

**https://jakeon32.github.io/brain-guide/**

This README stays a quick-start and reference. Read the guide for the why; don't expect a copy of it here — a copy just drifts from its source.

## Quick start (5 minutes)

```sh
git clone https://github.com/jakeon32/brain-starter ~/brain
cd ~/brain
./_System/install-stop-hook.sh          # macOS/Linux
# or: powershell -File _System\install-stop-hook.ps1   # Windows
pip install sqlite-vec sentence-transformers
./_System/install-stop-hook.sh          # ★ run it again — see below
```

(On Windows, the line you re-run is the `powershell -File` one.)

### Why the installer runs twice

The installer only builds the search index if the ML stack (`sqlite-vec`, `sentence-transformers`) is already importable *at the moment it runs*. On a first run, before those packages exist, it prints a warning and skips the index build — silently, by design, since it doesn't install packages for you. Everything else it does still succeeds: the pipeline scripts get copied, the hooks get wired into `settings.json`, the interpreter gets pinned. So the device looks fully installed after one run. Only running the installer again, after `pip install` has given the interpreter the packages, actually builds the index. Skip that second run and you have a device that passes every other check while search quietly returns nothing.

### Prove it worked — with one search

```sh
python ~/.claude/hooks/brain-search.py "<something you've actually discussed with Claude>"
```

This is the only check that proves the index exists and has content in it. Every other post-install check — the scripts are present, the hooks are registered, the interpreter is pinned, a session ends and a new file lands under `Decisions/`/`Mistakes/`/`Patterns/` — passes on a device whose index never got built. Search returning nothing looks exactly like "you have no notes on that yet" unless you already know otherwise, so run it once against a topic you know is in there.

## Two things the installer doesn't set up

The installer prints two recurring jobs at the very end of its own output instead of registering them for you — neither `cron` nor `schtasks` can be re-run idempotently the way copying a file can, so doing it automatically would just duplicate the job on every re-install:

- **Weekly consolidate** — folds the growing journal (`Decisions`/`Mistakes`/`Patterns`) into reusable `Knowledge/` pages. Skip it and `brain-health` will eventually nag you once the journal is big enough to be worth folding.
- **Watchdog** — the one job that would notice if the rest of the pipeline silently stopped running. Nothing else nags you about missing this one, because noticing is exactly what it's for.

Copy the exact command from the installer's own output rather than from anywhere else — it bakes in your real paths.

## Requirements

- Python 3.10+ (some hook scripts use `X | None`-style type hints that only run on 3.10 or newer)
- git
- Claude Code CLI — distillation shells out to `claude -p`
- Optional, for search: `sqlite-vec` + `sentence-transformers` (pulls in a ~420MB embedding model on first use, cached after that)

## Structure at a glance

Seven rooms, all empty right after clone — that's the correct starting state, not a setup step you missed:

| Folder | What lands there | Written by |
|---|---|---|
| `Memory/` | Stable facts about you, your projects, your devices | Claude's judgment |
| `Decisions/` | Lasting choices and the reasoning behind them | SessionEnd hook, automatically |
| `Mistakes/` | Errors, diagnosed (symptom → cause → fix) | SessionEnd hook, automatically |
| `Patterns/` | Reusable approaches worth repeating | SessionEnd hook, automatically |
| `Knowledge/` | Reusable how-tos — the main search target | Claude's judgment |
| `Sessions/` | Narrative write-ups, longer than a distilled note | By hand |
| `Agents/` | Operating notes for agents/subagents you use often | By hand or Claude's judgment |

`_System/` holds the contract these rooms run on — the installer scripts, the hook source, and the specs describing what each script actually does. It's tracked and shared by every clone; the seven rooms above are per-device runtime and stay out of git except for each folder's own `_index.md`. Full breakdown: [`_System/structure.md`](_System/structure.md).

## License

MIT — see [LICENSE](LICENSE).
