#!/usr/bin/env python3
"""
Stop-hook: distill Claude Code session into Obsidian vault long-term memory.

Pipeline:
  stdin (Stop hook payload JSON)
    -> read transcript JSONL
    -> filter to user/assistant text turns
    -> call `claude -p --bare --output-format text --json-schema {...}` for distillation
    -> write Decisions/Mistakes/Patterns notes to vault

Design notes:
- Runs in background-friendly mode: bounded timeout, fail-silent, all errors logged.
- Uses `--bare` to skip SessionStart hook + plugins + CLAUDE.md so the distill call
  itself does NOT trigger the parent's session-init machinery (and avoids recursive
  distillation when this inner session also stops).
- Skips trivial sessions (turns < MIN_TURNS) to keep noise low.
- All output paths configurable via env vars; sensible defaults for a vault at ~/brain.
"""
import json
import sys
import os
import shutil
import subprocess
import re
from pathlib import Path
from datetime import datetime, timezone

# ---- Config ----------------------------------------------------------------
VAULT_ROOT = Path(os.environ.get(
    "CLAUDE_DISTILL_VAULT",
    str(Path.home() / "brain"),
))
LOG_PATH = Path(os.environ.get(
    "CLAUDE_DISTILL_LOG",
    str(Path.home() / ".claude" / "logs" / "distill.log"),
))
def _resolve_claude_bin() -> str:
    """Resolve the claude binary defensively. A pinned absolute path can go
    stale when the CLI is reinstalled (the npm->native migration moved it once
    already, silently killing distillation for days); fall back to PATH, then
    known install locations, so the pipeline survives the next move too."""
    pinned = os.environ.get("CLAUDE_DISTILL_CLAUDE_BIN")
    if pinned and (os.sep not in pinned or Path(pinned).exists()):
        return pinned
    found = shutil.which("claude") or shutil.which("claude.cmd")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
        Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules"
            / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return pinned or "claude"


CLAUDE_BIN = _resolve_claude_bin()
DISTILL_MODEL = os.environ.get("CLAUDE_DISTILL_MODEL", "claude-sonnet-4-6")
MIN_TURNS = int(os.environ.get("CLAUDE_DISTILL_MIN_TURNS", "6"))
# Importance gate (P1, 2026-07-12): items scored at or below this are dropped
# instead of written. Scoring rubric lives in DISTILL_PROMPT_TEMPLATE.
MIN_IMPORTANCE = int(os.environ.get("CLAUDE_DISTILL_MIN_IMPORTANCE", "2"))
MAX_TURNS_INCLUDED = int(os.environ.get("CLAUDE_DISTILL_MAX_TURNS", "120"))
MAX_CHARS_PER_TURN = int(os.environ.get("CLAUDE_DISTILL_MAX_CHARS_PER_TURN", "2000"))
TIMEOUT_SEC = int(os.environ.get("CLAUDE_DISTILL_TIMEOUT", "180"))


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def safe_str(x) -> str:
    if isinstance(x, str):
        return x
    try:
        return json.dumps(x, ensure_ascii=False)
    except Exception:
        return str(x)


def extract_text(message: dict) -> str:
    """Pull plain text out of a transcript message's `message.content`."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", "?")
                parts.append(f"[tool_use:{name}]")
            elif btype == "tool_result":
                # keep short result snippet only
                raw = safe_str(block.get("content", ""))[:300]
                parts.append(f"[tool_result] {raw}")
        return "\n".join(p for p in parts if p)
    return safe_str(content)


def load_turns(transcript_path: Path):
    turns = []
    with transcript_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            msg = d.get("message") or {}
            role = msg.get("role", t)
            text = extract_text(msg).strip()
            if not text:
                continue
            ts = d.get("timestamp", "")
            turns.append({
                "role": role,
                "text": text[:MAX_CHARS_PER_TURN],
                "ts": ts,
            })
    return turns


DISTILL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "skip": {"type": "boolean"},
        "topic": {"type": "string", "description": "Short topic label for the whole session, kebab-case"},
        "decisions": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "summary", "importance"],
                "properties": {
                    "title": {"type": "string", "description": "kebab-case, <=60 chars"},
                    "summary": {"type": "string", "description": "2-6 sentences, what was decided and WHY"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "mistakes": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "summary", "importance"],
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string", "description": "what went wrong, root cause, fix applied"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "patterns": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "summary", "importance"],
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string", "description": "reusable approach generalizable to future sessions"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    "required": ["skip", "decisions", "mistakes", "patterns"],
}


DISTILL_PROMPT_TEMPLATE = """You are a memory distillation agent for a Claude Code user's Obsidian vault long-term memory.

Your job: extract from this session ONLY items that will be reusable across future sessions. Drop anything trivial, session-specific, or repeated-question noise.

Strict rules:
- decisions = lasting choices (architecture, tool selection, structural direction) with WHY.
- mistakes = errors that occurred AND were diagnosed/fixed. Include root cause + fix.
- patterns = generalizable approaches/snippets/workflows applicable beyond this one session.
- If the session was trivial (greetings, single status check, no real work), set skip=true with all arrays empty.
- importance: rate each item 1-10. 1 = mundane fact only valid for this exact
  project/session (e.g. a one-off path, a trivial rename); 5 = useful within
  this project across sessions; 10 = changes behavior in ALL future sessions
  (a standing user rule, a recurring failure mode, a cross-project workflow).
  Items rated 2 or below are discarded by the pipeline - do not inflate scores
  to save an item; a discarded mundane item is the correct outcome.
- titles MUST be kebab-case English, max ~60 chars.
- summaries: Korean OR English, whichever the session used. 2-6 sentences. Cite WHY, not just WHAT.
- DO NOT include: file paths, personal credentials, raw tool output, ephemeral debug details.
- Maximum 3 items per category. Quality > quantity. Empty array is fine if nothing qualifies.

Session metadata:
- session_id: {session_id}
- cwd: {cwd}
- date: {date}
- turn_count: {turn_count}

Session transcript (compressed; role + text per turn):
---BEGIN TRANSCRIPT---
{transcript}
---END TRANSCRIPT---

Return ONLY the JSON matching the provided schema.
"""


def build_transcript_block(turns) -> str:
    # keep last N turns; older turns rarely yield distillable insight
    selected = turns[-MAX_TURNS_INCLUDED:]
    lines = []
    for t in selected:
        role = t["role"].upper()[:1]  # U / A
        lines.append(f"[{role}] {t['text']}")
    return "\n\n".join(lines)


def call_claude_distill(prompt: str) -> dict | None:
    # NOTE: not using --bare because user auths via OAuth (no ANTHROPIC_API_KEY).
    # Recursion guard: set CLAUDE_DISTILL_CHILD so the child Stop-hook invocation
    # of this same script exits immediately.
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--model", DISTILL_MODEL,
        "--output-format", "json",
        "--json-schema", json.dumps(DISTILL_SCHEMA),
        "--effort", "low",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    # Prompt goes via stdin, NOT argv. On Windows the claude.cmd shim runs
    # through cmd.exe, whose command line is capped at ~8191 chars; a full
    # transcript prompt (10k+ chars) overflows it ("The command line is too
    # long."). stdin has no such cap and `claude -p` reads the prompt from
    # stdin when no positional prompt is given. Works cross-platform.
    env = os.environ.copy()
    env["CLAUDE_DISTILL_CHILD"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SEC,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log(f"claude CLI timeout after {TIMEOUT_SEC}s")
        return None
    except FileNotFoundError:
        log(f"claude binary not found: {CLAUDE_BIN}")
        return None
    except Exception as e:
        log(f"claude CLI exception: {e!r}")
        return None

    if proc.returncode != 0:
        log(f"claude CLI rc={proc.returncode}; stderr={proc.stderr[:500]!r}")
        return None

    out = (proc.stdout or "").strip()
    if not out:
        log("claude CLI returned empty stdout")
        return None
    try:
        wrapper = json.loads(out)
    except Exception as e:
        log(f"wrapper JSON parse failed: {e!r}; head={out[:400]!r}")
        return None

    # --output-format json wraps the schema-validated result in `structured_output`.
    structured = wrapper.get("structured_output")
    if isinstance(structured, dict):
        return structured

    # fallback: try to extract JSON from `result` field (markdown-fenced)
    result_text = wrapper.get("result", "")
    if isinstance(result_text, str):
        cleaned = re.sub(r"^```(?:json)?\s*", "", result_text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except Exception as e:
            log(f"fallback result parse failed: {e!r}; head={cleaned[:400]!r}")
    log(f"no structured_output in wrapper; keys={list(wrapper.keys())}")
    return None


SAFE_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


def slugify(s: str, fallback: str = "untitled") -> str:
    s = (s or "").strip().lower()
    s = s.replace(" ", "-").replace("_", "-")
    s = SAFE_SLUG_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:60] or fallback


def write_notes(distilled: dict, session_id: str, cwd: str) -> list[str]:
    date = datetime.now().strftime("%Y-%m-%d")
    written: list[str] = []
    dropped = 0
    for category in ("decisions", "mistakes", "patterns"):
        items = distilled.get(category) or []
        if not items:
            continue
        folder = VAULT_ROOT / category.capitalize()
        folder.mkdir(parents=True, exist_ok=True)
        for item in items:
            title = (item.get("title") or "").strip()
            summary = (item.get("summary") or "").strip()
            if not title or not summary:
                continue
            # P1 importance gate: low-value items are dropped, not written.
            # Missing/invalid score defaults to 5 (fail-open: never lose an
            # item because the model omitted the field).
            try:
                importance = int(item.get("importance", 5))
            except (TypeError, ValueError):
                importance = 5
            importance = max(1, min(10, importance))
            if importance <= MIN_IMPORTANCE:
                dropped += 1
                log(f"session {session_id[:8]}: dropped low-importance "
                    f"({importance}) {category[:-1]} '{title[:50]}'")
                continue
            slug = slugify(title)
            fname = f"{date}-{slug}.md"
            fpath = folder / fname
            # avoid overwriting same-day duplicates
            i = 2
            while fpath.exists():
                fpath = folder / f"{date}-{slug}-{i}.md"
                i += 1
            tags = item.get("tags") or []
            tags_yaml = "\n".join(f"  - {t}" for t in tags) if tags else ""
            # strength/recall_count/last_recalled seed the P2 decay model
            # (R = exp(-t/S*k)); brain-search bumps them on recall.
            yaml_block = (
                "---\n"
                f"type: {category}\n"
                f"date: {date}\n"
                f"session: {session_id}\n"
                f"cwd: {cwd}\n"
                "auto-distilled: true\n"
                f"importance: {importance}\n"
                "strength: 1\n"
                "recall_count: 0\n"
                f"last_recalled: {date}\n"
                + (f"tags:\n{tags_yaml}\n" if tags_yaml else "")
                + "---\n\n"
            )
            body = f"# {title}\n\n{summary}\n"
            fpath.write_text(yaml_block + body, encoding="utf-8")
            written.append(str(fpath.relative_to(VAULT_ROOT.parent.parent)))
    if dropped:
        log(f"session {session_id[:8]}: importance gate dropped {dropped} item(s)")
    return written


def _cleanup_payload_file() -> None:
    """Remove the hand-off file once the detached worker has read stdin."""
    pf = os.environ.get("CLAUDE_DISTILL_PAYLOAD_FILE")
    if not pf:
        return
    try:
        Path(pf).unlink()
    except OSError:
        pass


def _respawn_detached(raw: str, session_id: str) -> bool:
    """Hand the work to a detached child; return True if it took over.

    Why this exists (2026-07-16, health.md §2026-07-16 sequel): this script is a
    SessionEnd hook that makes a `claude -p` call — a ~24s LLM round-trip (p90
    36s). Whatever tears the hook's process tree down at session exit kills that
    mid-flight: the user sees "SessionEnd hook ... failed: Hook cancelled" and
    distill.log is left holding only a start line. 20 of 267 attempts died that
    way. Rather than guess at the teardown's exact trigger (the default command
    hook timeout is 600s, so it is not a timeout), remove the exposure: detach,
    and the hook returns in ~0.2s with nothing left to cancel. It also stops
    session exit from blocking on an LLM call.

    Safety: if the machine dies before the worker finishes, distill-catchup.py
    recovers the session at the next session start — a net that only actually
    works because a start line no longer retires a session (same postmortem).
    Fail-open: any problem detaching means we do the work inline instead.
    """
    try:
        tmp_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
        pf = tmp_dir / f"distill-payload-{session_id[:8]}-{os.getpid()}.json"
        pf.write_text(raw, encoding="utf-8")
        env = os.environ.copy()
        env["CLAUDE_DISTILL_DETACHED"] = "1"
        env["CLAUDE_DISTILL_PAYLOAD_FILE"] = str(pf)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        with pf.open("rb") as stdin_f:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                stdin=stdin_f,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=env,
                **kwargs,
            )
        log(f"session {session_id[:8]}: detached worker spawned; hook returning")
        return True
    except Exception as e:  # noqa: BLE001 - fail-open by design
        log(f"session {session_id[:8]}: detach failed ({e!r}); running inline")
        return False


def main() -> None:
    # Recursion guard: distillation invokes `claude -p` whose own Stop event would
    # re-enter this script. CLAUDE_DISTILL_CHILD is set by the parent call.
    if os.environ.get("CLAUDE_DISTILL_CHILD"):
        return

    # 1. read the hook payload. This must happen before any re-exec: stdin is the
    # hook runner's pipe and does not survive one.
    try:
        raw = sys.stdin.read()
    except Exception as e:
        log(f"stdin read failed: {e!r}")
        return

    # 1b. get out of the hook's way (see _respawn_detached).
    if not os.environ.get("CLAUDE_DISTILL_DETACHED"):
        try:
            sid_probe = (json.loads(raw).get("session_id") or "unknown")
        except Exception:
            sid_probe = "unknown"
        if _respawn_detached(raw, sid_probe):
            return
        # fall through: inline is worse than nothing at all

    _cleanup_payload_file()

    try:
        payload = json.loads(raw)
    except Exception as e:
        log(f"stdin parse failed: {e!r}")
        return

    session_id = payload.get("session_id") or "unknown"
    transcript_path_raw = payload.get("transcript_path")
    cwd = payload.get("cwd") or ""
    stop_hook_active = payload.get("stop_hook_active", False)

    if stop_hook_active:
        log(f"skip: stop_hook_active=true (session {session_id[:8]})")
        return
    if not transcript_path_raw:
        log(f"skip: no transcript_path in payload (session {session_id[:8]})")
        return
    transcript_path = Path(transcript_path_raw)
    if not transcript_path.exists():
        log(f"skip: transcript missing at {transcript_path}")
        return

    # 2. load + filter turns
    turns = load_turns(transcript_path)
    if len(turns) < MIN_TURNS:
        log(f"skip: only {len(turns)} turns (<{MIN_TURNS}) session {session_id[:8]}")
        return

    # 3. build prompt
    transcript_block = build_transcript_block(turns)
    prompt = DISTILL_PROMPT_TEMPLATE.format(
        session_id=session_id,
        cwd=cwd,
        date=datetime.now().strftime("%Y-%m-%d"),
        turn_count=len(turns),
        transcript=transcript_block,
    )

    # 4. call claude
    log(f"distilling session {session_id[:8]} ({len(turns)} turns, {len(prompt)} chars)")
    distilled = call_claude_distill(prompt)
    if distilled is None:
        log(f"distillation failed for session {session_id[:8]}")
        return
    if distilled.get("skip"):
        log(f"session {session_id[:8]} marked skip by distiller")
        return

    # 5. write notes
    written = write_notes(distilled, session_id, cwd)
    log(f"session {session_id[:8]}: wrote {len(written)} note(s): {written}")

    # 6. fire-and-forget reindex of brain-search if at least one note was written.
    # Freshness contract: _System/search.md. Failures here MUST
    # NOT affect distillation — wrap in try/except, no wait.
    if written:
        _spawn_brain_reindex(session_id)


def _spawn_brain_reindex(session_id: str) -> None:
    """Detached background reindex + catalog regen. Both live outside distill's
    responsibility but freshness matters for next-session recall. If a script
    is missing or fails, we never block or fail distillation."""
    for script_name in ("brain-reindex.py", "brain-catalog.py"):
        script = Path(__file__).parent / script_name
        if not script.exists():
            continue
        try:
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
            log(f"session {session_id[:8]}: {script_name} spawned in background")
        except Exception as e:
            log(f"session {session_id[:8]}: {script_name} spawn failed: {e!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"fatal: {e!r}")
        # silently exit so we never block the user
