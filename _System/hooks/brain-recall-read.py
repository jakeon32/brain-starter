#!/usr/bin/env python3
"""brain-recall-read.py — PostToolUse(Read|Bash): 실제로 읽힌 brain 파일만 회상으로 센다.

왜 (2026-09-28): 그전까지 회상은 brain-search가 결과를 **반환**할 때 올라갔다.
top-k에 이름만 나와도 recall_count+1, 중복 판정도 +1 — 그래서 "회상률 8.9%"가
실제로 읽힌 비율인지 노출된 비율인지 알 수 없었고, 자동 회상 주입(A2)의 효과를
잴 기준이 없었다. 이제 노출은 brain-search가 usage 테이블에, 읽기는 이 훅이
reads 테이블에 따로 남긴다. 차용 근거: Letta Code memory-citations,
OpenViking active_count.

계약:
- Read 도구의 file_path, Bash 명령에 등장하는 `.md` 경로 중 볼트 안의 것만 본다.
- 같은 세션에서 같은 파일은 한 번만 센다 (한 세션에서 열 번 열어도 회상 1회).
- 저널(Decisions/Mistakes/Patterns)은 frontmatter write-back — recall_count+1,
  strength+1, last_recalled=오늘 (brain-decay의 입력). Knowledge 등은 frontmatter를
  건드리지 않는다(scope: shared 동기화 충돌) — reads 테이블에만 남긴다.
- 표준 라이브러리만 쓴다. 어떤 실패도 도구 호출을 막지 않는다 — 항상 exit 0, 무출력.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

USAGE_DB = Path(os.environ.get("BRAIN_USAGE_DB")
                or Path.home() / ".claude" / "data" / "brain-usage.sqlite")
JOURNAL_CATS = {"Decisions", "Mistakes", "Patterns"}
# Bash 명령에서 .md 경로 후보를 뽑는다. 따옴표 안(공백 허용)과 맨몸 토큰 둘 다.
_QUOTED_MD = re.compile(r"""["']([^"'\n]+?\.md)["']""", re.IGNORECASE)
_BARE_MD = re.compile(r"""(?<![\w"'])([^\s"'|;&<>()]+?\.md)\b""", re.IGNORECASE)
_FM_INT = {k: re.compile(rf"^({k}:\s*)(\d+)\s*$", re.M)
           for k in ("recall_count", "strength")}
_FM_LAST = re.compile(r"^(last_recalled:\s*)('?)[0-9-]+\2\s*$", re.M)


def find_vault() -> Path | None:
    for c in (os.environ.get("CLAUDE_DISTILL_VAULT"), os.environ.get("BRAIN_VAULT"),
              str(Path.home() / "brain")):
        if c and (Path(c) / "_System").is_dir():
            return Path(c).resolve()
    return None


def _norm(raw: str, cwd: str) -> Path | None:
    """Git Bash(`/d/claude/...`), `~`, 상대경로를 실제 경로로."""
    s = raw.strip()
    m = re.match(r"^/([a-zA-Z])/(.*)$", s)
    if m and sys.platform == "win32":
        s = f"{m.group(1)}:/{m.group(2)}"
    if s.startswith("~"):
        s = str(Path.home()) + s[1:]
    p = Path(s)
    if not p.is_absolute():
        if not cwd:
            return None
        p = Path(cwd) / p
    try:
        return p.resolve()
    except (OSError, ValueError):
        return None


def candidate_paths(tool: str, tin: dict, cwd: str) -> list[Path]:
    if tool == "Read":
        fp = tin.get("file_path") or ""
        return [p for p in [_norm(fp, cwd)] if p and p.suffix.lower() == ".md"]
    if tool == "Bash":
        cmd = tin.get("command") or ""
        raws = set(_QUOTED_MD.findall(cmd)) | set(_BARE_MD.findall(cmd))
        return [p for p in (_norm(r, cwd) for r in raws) if p]
    return []


def rel_to_vault(p: Path, vault: Path) -> str | None:
    try:
        rel = p.relative_to(vault)
    except ValueError:
        # 윈도는 대소문자를 가리지 않는다 — 드라이브 문자 대소문자 차이로 놓치지 않게.
        a, b = str(p).lower(), str(vault).lower().rstrip("\\/")
        if not a.startswith(b + os.sep.lower()) and not a.startswith(b + "/"):
            return None
        rel = Path(str(p)[len(b) + 1:])
    return rel.as_posix()


def bump_journal(p: Path, today: str) -> None:
    text = p.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return
    for key, rx in _FM_INT.items():
        if rx.search(text):
            text = rx.sub(lambda mo: f"{mo.group(1)}{int(mo.group(2)) + 1}", text, count=1)
        else:
            text = text.replace("---\n", f"---\n{key}: 1\n", 1)
    if _FM_LAST.search(text):
        text = _FM_LAST.sub(rf"\g<1>{today}", text, count=1)
    else:
        text = text.replace("---\n", f"---\nlast_recalled: {today}\n", 1)
    p.write_text(text, encoding="utf-8")


def main() -> None:
    try:
        # bytes + explicit UTF-8: on Windows the text-mode stdin decodes with the
        # locale codec (cp949 on a Korean console) and mangles non-ASCII paths.
        buf = getattr(sys.stdin, "buffer", None)
        raw = buf.read().decode("utf-8", errors="replace") if buf else sys.stdin.read()
        ev = json.loads(raw or "{}")
    except Exception:
        return
    tool = ev.get("tool_name") or ""
    tin = ev.get("tool_input") or {}
    if tool not in ("Read", "Bash") or not isinstance(tin, dict):
        return
    vault = find_vault()
    if not vault:
        return
    session = ev.get("session_id") or ""
    hits = []
    for p in candidate_paths(tool, tin, ev.get("cwd") or ""):
        rel = rel_to_vault(p, vault)
        # `_catalog.md`·`_index.md` 같은 색인 파일은 기억이 아니다 — 세지 않는다.
        # (2026-09-28 첫 실측에서 Decisions/_catalog.md frontmatter에 회상 필드가 붙었다.)
        if rel and p.is_file() and not p.name.startswith("_"):
            hits.append((rel, p))
    if not hits:
        return

    USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(USAGE_DB, timeout=2)
    con.execute("""CREATE TABLE IF NOT EXISTS reads(
        path TEXT NOT NULL, category TEXT, ts TEXT NOT NULL,
        session_id TEXT, tool TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reads_path ON reads(path)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reads_sess ON reads(session_id, path)")
    now = datetime.datetime.now()
    today = now.date().isoformat()
    for rel, p in hits:
        if session and con.execute(
                "SELECT 1 FROM reads WHERE session_id=? AND path=? LIMIT 1",
                (session, rel)).fetchone():
            continue
        cat = rel.split("/", 1)[0]
        con.execute("INSERT INTO reads(path, category, ts, session_id, tool)"
                    " VALUES(?,?,?,?,?)",
                    (rel, cat, now.isoformat(timespec="seconds"), session, tool))
        if cat in JOURNAL_CATS:
            try:
                bump_journal(p, today)
            except Exception:
                pass
    con.commit()
    con.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
