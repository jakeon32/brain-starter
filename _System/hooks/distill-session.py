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
# Keep this fallback identical to distill-catchup.py's DEFAULT_VAULT. An earlier
# fallback here named a path that no longer existed, so any time the env var was
# missing the distiller wrote into a dead vault and nothing surfaced the loss.
VAULT_ROOT = Path(os.environ.get(
    "CLAUDE_DISTILL_VAULT",
    str(Path.home() / "brain"),
))
LOG_PATH = Path(os.environ.get(
    "CLAUDE_DISTILL_LOG",
    str(Path.home() / ".claude" / "logs" / "distill.log"),
))
def _upgrade_shim(path: str) -> str | None:
    """Map an npm shim to the native claude.exe beside it; drop it if unusable.

    Windows-only hazard (2026-07-18 postmortem): npm installs four entry points
    next to each other -- `claude` (sh), `claude.cmd`, `claude.ps1`, and the real
    `claude.exe` under node_modules. Only the .exe survives our call pattern:
      - .ps1 / extensionless  -> CreateProcess raises OSError 193 outright.
      - .cmd                  -> runs, but dies with rc=1 "the system cannot find
                                 the file specified" once stdin is a pipe, which
                                 is exactly how the transcript prompt is fed.
    Both failure modes were live simultaneously (SessionEnd pinned the .ps1,
    catchup resolved the .cmd), silently killing every distill for days. Return
    None when a shim cannot be rescued so the caller keeps searching rather than
    committing to a path that is guaranteed to fail at call time.
    """
    if os.name != "nt":
        return path
    ext = Path(path).suffix.lower()
    if ext == ".exe":
        return path
    if ext not in (".cmd", ".bat", ".ps1", ""):
        return path
    exe = Path(path).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if exe.exists():
        return str(exe)
    # A bare name (no separator) is left to PATH resolution at exec time.
    return None if os.sep in path else path


def _resolve_claude_bin() -> str:
    """Resolve the claude binary defensively. A pinned absolute path can go
    stale when the CLI is reinstalled (the npm->native migration moved it once
    already, silently killing distillation for days); fall back to PATH, then
    known install locations, so the pipeline survives the next move too."""
    pinned = os.environ.get("CLAUDE_DISTILL_CLAUDE_BIN")
    if pinned and (os.sep not in pinned or Path(pinned).exists()):
        upgraded = _upgrade_shim(pinned)
        if upgraded:
            return upgraded
    found = shutil.which("claude") or shutil.which("claude.cmd")
    if found:
        upgraded = _upgrade_shim(found)
        if upgraded:
            return upgraded
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
# Incremental guard (2026-08-03): the Stop hook fires after *every* assistant
# reply, and this script re-summarises the whole transcript each time — a 900-turn
# session produced 40 notes in one day, ~5x duplicated per topic. Only re-run once
# the session has grown by this many turns since the last successful distillation.
# The guard applies ONLY to payloads whose hook_event_name is "Stop". SessionEnd,
# distill-catchup and hand runs always proceed — otherwise a session shorter than
# MIN_TURN_DELTA would never be distilled at all on a device that, like this
# skeleton's installer, wires only SessionEnd. CLAUDE_DISTILL_FORCE=1 also bypasses it.
MIN_TURN_DELTA = int(os.environ.get("CLAUDE_DISTILL_MIN_TURN_DELTA", "50"))
FORCE_RUN = os.environ.get("CLAUDE_DISTILL_FORCE", "") not in ("", "0", "false")
STATE_PATH = Path(os.environ.get(
    "CLAUDE_DISTILL_STATE",
    str(Path.home() / ".claude" / "logs" / "distill-state.json"),
))


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # keep the file from growing without bound across sessions
        if len(state) > 200:
            state = dict(sorted(state.items(), key=lambda kv: kv[1], reverse=True)[:100])
        STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except Exception as e:
        log(f"state write failed: {e!r}")
# Importance gate (P1, 2026-07-12): items scored at or below this are dropped
# instead of written. Scoring rubric lives in DISTILL_PROMPT_TEMPLATE.
# 2 → 4 (2026-08-10): 2는 사실상 전량 통과였다. 저널 793개 중 613개(77%)가
# 한 번도 회상되지 않은 채 망각 임계를 넘겼다 — 쓸 때 이미 안 찾을 것이었다는
# 뜻이다. 4 이하를 버리면 rubric상 "5 = 이 프로젝트 안에서 세션을 넘어 쓸모
# 있다" 이상만 남는다. 점수 누락 시 기본값 5는 그대로 통과(fail-open 유지).
# 2026-08-28 임시 상향 (4 → 5, 즉 저장 하한이 5점에서 6점으로).
# 근거: 최근 9주 실측에서 유입 주 190개 대 처리 주 95개로 **주당 95개 적자**였고,
# 처리량은 161→79→39 로 반토막 나는 추세였다. 동시에 저널 851개 중 822개(96.6%)가
# 한 번도 회상되지 않았다 — 거의 쓰이지 않는 것을 하루 27개씩 만들며 그 처리에
# 주간 예산을 태우던 상태다. 5점 구간이 분포의 약 31%라 유입이 주 190 → 약 130 으로
# 내려간다. **임시값이다**: Knowledge 사용 측정이 2026-08-28 에 시작됐으므로
# 2주치(9월 중순)가 쌓이면 "실제로 쓰인 지식이 어느 점수대에서 나왔는가"를 보고
# 근거 있게 재조정한다. 되돌리려면 이 값을 4로 되돌리거나 환경변수로 덮는다.
MIN_IMPORTANCE = int(os.environ.get("CLAUDE_DISTILL_MIN_IMPORTANCE", "5"))
# 근접 중복 임계 (2026-08-10). 제목 벡터 코사인이 이 값 이상이면 새 조각을
# 쓰지 않고 원본을 회상 처리한다. 0.82는 "같은 말을 다르게 쓴 것"과 "옆 주제"가
# 갈리는 지점 — 낮추면 별개 사실이 묻히고, 높이면 재기록이 그대로 통과한다.
DEDUP_COS = float(os.environ.get("CLAUDE_DISTILL_DEDUP_COS", "0.82"))
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


# brain-recall-inject.py가 프롬프트마다 넣는 블록. 이미 brain에 있는 문서의 제목이라
# 증류하면 같은 기억이 새 일지로 다시 저장된다 — 증류 전에 지운다 (2026-09-28).
_RECALL_BLOCK_RE = re.compile(r"<(brain-recall)>.*?</\1>\s*", re.S)


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
            text = _RECALL_BLOCK_RE.sub("", extract_text(msg)).strip()
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
- For mistakes and patterns: do NOT quote who pointed out what (no "the user said/pointed out ...",
  no quoted remarks, no names). Write the rule to follow next time and the situation in which it
  applies. These notes are internal; quoting the incident lets it leak into material other people
  read (2026-11-01 user rule).
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
        # MCP·채널 플러그인 부팅 차단: Discord 플러그인이 부팅되면 distill 1회당
        # 봇 게이트웨이 세션 1회 소모 (24h 1000회 한도 누수, 2026-07-19).
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
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
            # CREATE_NO_WINDOW: Windows에서 자식 claude마다 콘솔 창이 뜨는 것 차단
            # (POSIX엔 속성이 없어 0 = 무동작). 2026-07-19 brain-consolidate.py와 동일 패턴.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


class _DedupIndex:
    """기존 활성 저널의 제목 벡터 — 닮은 노트는 새로 쓰지 않고 회상으로 돌린다.

    같은 날·같은 슬러그는 write_notes가 이미 막지만, **같은 내용을 다른 날
    다른 표현으로** 쓰면 그대로 새 파일이 됐다 (stop-hook.md §Known issues의
    "Naming dedup is collision-suffix only, not similarity-based"). 저널이
    불어난 주된 경로다 — 2026-08-07 통합 때 132조각 대부분이 이미 있는 내용의
    재기록이었다.

    검색 인덱스에 제목 벡터(chunk_idx 0)가 이미 있으니 추가 비용은 새 제목
    임베딩뿐이다. 인덱스나 ML 스택이 없는 기기에서는 조용히 비활성 —
    dedup을 못 하는 것이 기록을 잃는 것보다 낫다 (fail-open).
    """

    def __init__(self):
        self.vecs: dict[str, list[float]] = {}
        self._embed = None
        try:
            import math
            import struct
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from _brain_lib import open_db, embed_one, EMBED_DIM  # noqa
            conn = open_db(create=False)
            rows = conn.execute(
                """SELECT c.file_path, c.id FROM chunks c
                   JOIN files f ON f.path = c.file_path
                   WHERE c.chunk_idx = 0
                     AND f.category IN ('Decisions','Mistakes','Patterns')"""
            ).fetchall()
            for fp, cid in rows:
                if "archived" in fp.replace("\\", "/").split("/"):
                    continue
                r = conn.execute(
                    "SELECT embedding FROM chunks_vec WHERE rowid = ?",
                    (cid,)).fetchone()
                if not (r and r[0]):
                    continue
                v = struct.unpack(f"{EMBED_DIM}f", r[0])
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                self.vecs[fp] = [x / n for x in v]
            self._embed = embed_one
        except Exception as e:
            log(f"dedup: disabled ({e!r})")

    def match(self, title: str):
        """제목이 기존 저널과 DEDUP_COS 이상 닮으면 (경로, 유사도)."""
        if not (self._embed and self.vecs):
            return None
        try:
            import math
            q = self._embed(title)
            n = math.sqrt(sum(x * x for x in q)) or 1.0
            q = [x / n for x in q]
            best, score = None, 0.0
            for fp, v in self.vecs.items():
                s = sum(a * b for a, b in zip(q, v))
                if s > score:
                    best, score = fp, s
            return (best, score) if score >= DEDUP_COS else None
        except Exception as e:
            log(f"dedup: lookup failed ({e!r})")
            return None


def _bump_recall(rel_path: str) -> bool:
    """중복이라 안 쓴 대신, 원본을 회상 처리한다 (strength+1 → 감쇠가 늦어짐).

    brain-search의 write-back과 같은 계약. 새 조각을 만들지 않으면서 "이 주제가
    또 나왔다"는 사실은 남긴다 — 안 그러면 반복되는 주제가 오히려 빨리 잊힌다.
    """
    try:
        # 인덱스의 file_path는 vault 기준 상대경로다 ("Decisions\\2026-…md").
        p = Path(rel_path) if Path(rel_path).is_absolute() \
            else VAULT_ROOT / rel_path
        if not p.exists():
            return False
        txt = p.read_text(encoding="utf-8")
        today = datetime.now().strftime("%Y-%m-%d")
        # recall_count는 올리지 않는다 (2026-09-28) — 같은 주제가 다시 나온 것은
        # "읽혔다"가 아니다. 회상 수는 brain-recall-read.py가 실제 읽기로만 센다.
        # strength/last_recalled는 그대로 올려 반복 주제가 빨리 잊히지 않게 한다.
        for key in ("strength",):
            m = re.search(rf"^({key}:\s*)(\d+)\s*$", txt, re.M)
            if m:
                txt = txt[:m.start()] + f"{m.group(1)}{int(m.group(2)) + 1}" \
                    + txt[m.end():]
        m = re.search(r"^(last_recalled:\s*)('?)[0-9-]+\2\s*$", txt, re.M)
        if m:
            txt = txt[:m.start()] + f"{m.group(1)}{today}" + txt[m.end():]
        p.write_text(txt, encoding="utf-8")
        return True
    except Exception as e:
        log(f"dedup: recall bump failed for {rel_path} ({e!r})")
        return False


def write_notes(distilled: dict, session_id: str, cwd: str) -> list[str]:
    date = datetime.now().strftime("%Y-%m-%d")
    written: list[str] = []
    dropped = 0
    dedup = _DedupIndex()
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
            # Same date + same slug means the running session was summarised
            # again and produced the same note. Suffixing it (-2, -3 …) piled up
            # near-identical files, so skip instead (2026-08-03).
            if fpath.exists():
                dropped += 1
                log(f"session {session_id[:8]}: duplicate note skipped '{fname}'")
                continue
            # Near-duplicate gate (2026-08-10): 같은 주제를 다른 날 다시 쓰면
            # 새 조각 대신 원본을 회상 처리한다.
            hit = dedup.match(title)
            if hit:
                dropped += 1
                bumped = _bump_recall(hit[0])
                log(f"session {session_id[:8]}: near-duplicate of {hit[0]} "
                    f"(cos {hit[1]:.2f}) — skipped '{title[:50]}'"
                    f"{'' if bumped else ' (recall bump failed)'}")
                continue
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
        # bytes, so a surrogate that slipped through can never kill the
        # detach itself (see the stdin note in main()).
        pf.write_bytes(raw.encode("utf-8", errors="replace"))
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
    # stdin is read as bytes and decoded as UTF-8 explicitly. On Windows the
    # text-mode sys.stdin decodes with the locale codec (cp949 here) while the
    # hook runner sends UTF-8, so a Korean cwd arrives as lone surrogates
    # (\udcXX). _respawn_detached then tried to write that string back out as
    # UTF-8 and died with UnicodeEncodeError("surrogates not allowed"), taking
    # the whole detach with it -- 3 failures in the log, all Korean paths
    # (2026-08-12/13/20). Postmortem: _System/health.md 2026-08-20.
    try:
        _buf = getattr(sys.stdin, "buffer", None)
        if _buf is not None:
            raw = _buf.read().decode("utf-8", errors="replace")
        else:
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

    # Incremental guard — a repeated Stop-hook firing is skipped unless the session
    # grew enough since last time. Everything else always proceeds (see MIN_TURN_DELTA).
    state = _load_state()
    last_turns = int(state.get(session_id, 0))
    from_stop_hook = payload.get("hook_event_name") == "Stop"
    if from_stop_hook and not FORCE_RUN and (len(turns) - last_turns) < MIN_TURN_DELTA:
        log(f"skip: only +{len(turns) - last_turns} turns since last distill "
            f"(<{MIN_TURN_DELTA}) session {session_id[:8]}")
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

    # Mark how far this session has been distilled so the incremental guard
    # above can skip the next few Stop-hook firings.
    state[session_id] = len(turns)
    _save_state(state)

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
