#!/usr/bin/env python3
"""brain-recall-inject.py — UserPromptSubmit: 관련 brain 문서의 제목·경로를 자동으로 알린다.

왜 (2026-09-28): 회상이 Claude가 brain-search를 부르기로 판단할 때만 일어났다.
살아 있는 일지 766건 중 회상 기록이 있는 것이 68건(8.9%)이었고, 전역 CLAUDE.md의
"착수 전에 검색하라" 규칙으로는 그 판단이 습관이 되지 않았다. Hindsight·Cognee·
OpenViking 같은 에이전트 메모리 오픈소스는 매 프롬프트마다 훅이 검색해서 주입한다.

계약:
- BM25(--fast와 같은 경로)만 쓴다. 벡터 검색은 모델 로딩에 13초가 들어 매 프롬프트에
  쓸 수 없다.
- 본문은 넣지 않는다. 제목과 경로 최대 MAX_ITEMS건만 넣고, 열어 볼지는 Claude가
  정한다. 실제로 열면 brain-recall-read.py가 회상으로 센다 — 주입 자체는 회상이 아니다.
- 관련성 관문: BM25는 관련 없는 질문에도 결과를 돌려주므로(brain-search.py
  MIN_VEC_DISTANCE 주석의 2026-07-16 실측), 질문의 핵심어가 문서에 몇 개 들어
  있는지로 거른다. 핵심어가 2개 미만인 짧은 질문(인사·확인 응답)은 검색하지 않는다.
- `claude -p` 하위 실행(CLAUDE_DISTILL_CHILD)에서는 돌지 않는다 — 통합·증류
  프롬프트에 섞이면 결과가 오염된다.
- 주입 블록은 <brain-recall> 태그로 감싼다. distill-session.py가 증류 전에 이
  태그를 지워서, 주입된 제목이 새 일지로 다시 저장되지 않게 한다.
- 어떤 실패도 프롬프트를 막지 않는다 — 항상 exit 0. 켜고 끄기: BRAIN_RECALL_INJECT=0.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_ITEMS = int(os.environ.get("BRAIN_RECALL_MAX", "3"))
TOP_K_GATE = 15          # BM25 상위 몇 건까지 관문에 올릴지
# 관문 기준 — 단어의 희소도 IDF = ln(전체 문서 수 / 그 단어가 든 문서 수).
# 2026-09-28 실제 프롬프트 표본으로 정했다: "페이지"(441/1747, 1.38)·"폴더"(203, 2.15)
# 처럼 흔한 말로만 맞은 문서는 잡음이었고, 행사 이름·제품 이름처럼(IDF 2.9~3.5)
# 드문 말이 맞은 문서는 관련 문서였다.
# 그러나 IDF만으로는 부족했다: 한국어 기능어는 활용형이 갈라져 드문 단어로 계산된다
# ("전혀" 4.17, "이렇게" 3.73 > 드문 제품 이름 2.87). 그래서 판정 신호는 **제목·파일명·소제목**
# 이다. 제목에는 주제 명사가 오고 기능어는 거의 오지 않는다. IDF는 흔한 말을 거르는
# 데만 쓴다.
IDF_INFORMATIVE = float(os.environ.get("BRAIN_RECALL_IDF_MIN", "1.8"))   # 이보다 흔하면 무시
# 제목에 걸린 단어도 흔하면 안 된다 — "관리"(2.56)·"라이브"(2.19)가 제목에 걸린 문서는
# 잡음이었다. 2.8은 문서 1,747개 중 약 105개 이하에 나오는 단어다.
IDF_HEAD_MIN = float(os.environ.get("BRAIN_RECALL_IDF_HEAD", "2.8"))
# 점수(idf_sum) 하한 — 2026-10-29 판독: 9/28~10/29 주입 3,750건 중 세션이 실제로 연 것은 7.6%였다.
# 과거 프롬프트 2,409건을 다시 넣어 보니 점수 12 미만은 5~9%, 15~20은 14%, 20 이상은 25~31%가
# 읽혔다. 12 이상만 넣으면 주입이 절반(51%)으로 줄고 실제로 읽힌 것의 71%가 남는다.
# 15로 올리면 주입 26%·읽힘 48%라 놓치는 것이 너무 많다.
IDF_SUM_MIN = float(os.environ.get("BRAIN_RECALL_SUM_MIN", "12"))
TAG = "brain-recall"
USAGE_DB = Path(os.environ.get("BRAIN_USAGE_DB")
                or Path.home() / ".claude" / "data" / "brain-usage.sqlite")

# 대화에 늘 나오지만 주제를 가리키지 않는 말. 이 단어가 문서에 있다고 관련 문서는 아니다.
STOP = set("""
해줘 해주세요 해 하자 하고 해서 했어 했는데 하는 하면 할 합니다 됩니다 된다 되는 돼 됐어
부탁 부탁해 부탁해요 부탁합니다 좀 이거 그거 저거 이것 그것 여기 거기 지금 다시 이제 그럼 그리고
근데 그런데 그래서 우리 내가 너가 네가 어떻게 뭐 뭘 왜 언제 어디 있는지 없는지 있어 없어 있나
확인 진행 수정 정리 작업 부분 내용 관련 문제 방법 경우 이번 다음 먼저 계속 전부 모두 같이
봐줘 알려줘 만들어 만들어줘 추가 삭제 변경 업데이트 검토 분석 심층 설명 결과 상태 가능 필요
따로 누가 곳에 곳이 그런 이런 저런 만든 만들고 이렇게 그렇게 저렇게 전혀 없어서 있어서 하지 않아
정도 자리 위한 통한 대한 많이 달라 없을 나오 남은 아래 위에 일단 아직 혹시 다른 바로 모두 계속
무슨 나오고 나와 눌러 변화 것과 활용 이런식 어쨌든 그러네 최근 지난 최선 아이디어 너무 조금
""".split() + """
ok yes no the a an and or to of in on for is are be this that it with please check fix make
""".split())
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣_\-]*")
# 서술어로 끝나는 말은 주제어가 아니다 ("해줘", "만들자", "맞지", "있는데").
_PRED_END = ("줘", "요", "까", "죠", "냐", "니까", "는데", "은데", "인데", "거든", "거지",
             "었어", "았어", "했어", "할께", "할게", "하자", "하고", "해서", "하면", "하는",
             "한다", "했다", "된다", "되는", "보자", "만들자", "싶은", "같아", "같고",
             "는거", "는걸", "한거", "된거", "을까", "을지", "할지", "나요", "네요", "어요")
_HANGUL = re.compile(r"[가-힣]")
_URL_RE = re.compile(r"https?://\S+")
# 프롬프트에 섞여 들어오는 시스템 글 (2026-10-09 실측: 채팅 채널 플러그인으로 들어온 세션 주입의
# 72%가 메시지 머리말 <channel source="plugin:…"> 의 channel·plugin 같은 낱말에 걸린 같은 문서
# 3개였고, 작업 알림 블록의 임시 폴더 경로가 «디스크 정리» 문서를 매번 불렀다). 블록은 통째로,
# 태그는 껍데기만, 파일 경로는 통째로 지운다.
_SYS_BLOCK_RE = re.compile(r"<(system-reminder|task-notification|brain-recall)\b[^>]*>.*?</\1>", re.S)
_TAG_RE = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?>")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/[a-z]/)[^\s\"'`<>|]*")


def clean_prompt(prompt: str) -> str:
    """검색어를 뽑기 전에 시스템이 끼워 넣은 글을 걷어 낸다."""
    text = _SYS_BLOCK_RE.sub(" ", prompt)
    text = _TAG_RE.sub(" ", text)
    return _PATH_RE.sub(" ", text)


def _load_search():
    spec = importlib.util.spec_from_file_location("brain_search", HERE / "brain-search.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 로·도·에·와·과는 떼지 않는다 — "히어로"가 "히어"가 됐다 (2026-09-28 표본).
_PARTICLE_1 = tuple("이가을를은는의")


def key_terms(prompt: str, stem) -> list[str]:
    """질문에서 주제를 가리키는 단어만 남긴다 (조사 제거, 불용어·한 글자 제외)."""
    text = _URL_RE.sub(" ", clean_prompt(prompt))
    out: list[str] = []
    for t in _TOKEN_RE.findall(text):
        t = t.lower().strip(".-_")
        if _HANGUL.search(t):
            t = stem(t) or t
            # brain-search의 어간 처리가 남기는 한 글자 조사 ("영상이" → "영상").
            if len(t) >= 3 and t.endswith(_PARTICLE_1):
                t = t[:-1]
        if len(t) < 2 or t in STOP or t.isdigit():
            continue
        if _HANGUL.search(t) and t.endswith(_PRED_END):
            continue
        if t not in out:
            out.append(t)
    return out[:12]


def idf_table(terms: list[str], conn) -> dict[str, float]:
    """단어마다 그 단어(접두 일치)가 든 문서 수로 IDF를 잰다. 단어당 약 1ms."""
    n = conn.execute("SELECT count(*) FROM files").fetchone()[0] or 1
    out = {}
    for t in terms:
        try:
            df = conn.execute(
                "SELECT count(DISTINCT c.file_path) FROM chunks_fts f"
                " JOIN chunks c ON c.id = f.rowid WHERE chunks_fts MATCH ?",
                ('"' + t.replace('"', '""') + '"*',)).fetchone()[0]
        except sqlite3.OperationalError:
            df = 0
        if df:
            out[t] = math.log(n / df)
    return out


_HEADING_RE = re.compile(r"^(?:#{1,6}\s+.+|title:.+|tags:.+|aliases:.+|description:.+)$", re.M)


def heading_text(path: Path, body: str) -> str:
    """제목·파일명·소제목·태그만 모은 글. 주제어가 여기 있어야 관련 문서로 본다."""
    parts = [path.stem.replace("-", " ")] + _HEADING_RE.findall(body)
    return "\n".join(parts).lower()


def title_of(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as f:
            head = f.read(2000)
    except OSError:
        return path.stem
    m = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", head, re.M)
    if m:
        return m.group(1)
    m = re.search(r"^#\s+(.+)$", head, re.M)
    return m.group(1).strip() if m else path.stem


def pick(prompt: str, bs) -> list[dict]:
    terms = key_terms(prompt, bs._ko_stem)
    if not terms:
        return []
    conn = bs.open_db(create=False)
    idf = idf_table(terms, conn)
    info = [t for t in terms if idf.get(t, 0) >= IDF_INFORMATIVE]
    if len(info) < 2:
        return []
    # 검색 질의는 주제어만으로 만든다 — 흔한 말이 OR로 섞이면 아무 문서나 걸린다.
    hits = bs.search(" ".join(info), k=TOP_K_GATE, fast=True)
    scored = []
    for h in hits:
        p = Path(h["abs_path"])
        try:
            body = p.read_text(encoding="utf-8").lower()
        except OSError:
            continue
        matched = [t for t in info if t in body]
        heads = heading_text(p, body)
        in_head = [t for t in matched if t in heads and idf[t] >= IDF_HEAD_MIN]
        # 조건: 주제어가 제목·소제목에 1개 이상, 본문 전체로는 2개 이상.
        if not in_head or len(matched) < 2:
            continue
        h["matched"] = sorted(matched, key=lambda t: (t not in in_head, -idf[t]))
        h["in_head"] = in_head
        # 제목 일치에 가중치 2배 — 제목에 걸린 문서가 그 주제를 다루는 문서다.
        h["idf_sum"] = round(sum(idf[t] for t in matched) + sum(idf[t] for t in in_head), 2)
        if h["idf_sum"] < IDF_SUM_MIN:
            continue
        h["title"] = title_of(p)
        scored.append(h)
    scored.sort(key=lambda h: -h["idf_sum"])
    return scored[:MAX_ITEMS]


def already_injected(session: str) -> set[str]:
    """이 세션에서 이미 넣은 문서(경로). 같은 제목을 매 프롬프트 반복하지 않는다 (10-09: 31회 반복)."""
    if not session or not USAGE_DB.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{USAGE_DB.as_posix()}?mode=ro", uri=True, timeout=2)
        rows = con.execute("SELECT DISTINCT path FROM injections WHERE session_id = ?", (session,)).fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _norm(p: str) -> str:
    return p.replace("\\", "/").rstrip("/").lower()


def _fm_list(head: str, key: str) -> list[str]:
    m = re.search(rf"^{key}:[^\n]*\n((?:[ \t]*-[ \t]*.+\r?\n?)+)", head, re.M)
    if not m:
        return []
    return [x.strip().lstrip("-").strip().strip("\"'") for x in m.group(1).splitlines() if x.strip()]


def project_book(cwd: str, bs, prompt: str = "") -> dict | None:
    """이 세션이 하는 프로젝트의 노하우 북 (Knowledge/Projects/*.md).

    판정: frontmatter folders: 가 작업 폴더를 덮거나(가장 긴 쪽), 질문에 keywords: 가 하나 이상
    나오면. 핵심어가 필요한 이유 — 여러 프로젝트를 한 폴더에서 하는 세션은 폴더로 가를 수
    없다 (2026-10-09 실측). 세션이 노하우를 키워드 운에 맡기지 않고 받게 한다.
    Knowledge/Projects/ 가 없으면 아무것도 하지 않는다 — 쓰는 사람만 켜지는 기능이다
    (_System/knowledge-model.md)."""
    root = Path(bs.BRAIN_VAULT) / "Knowledge" / "Projects"
    c = _norm(cwd) if cwd else ""
    low = clean_prompt(prompt).lower() if prompt else ""
    best = None   # (score, book)
    for f in sorted(root.glob("*.md")) if root.is_dir() else []:
        if f.name.endswith(".inbox.md"):
            continue
        try:
            head = f.read_text(encoding="utf-8")[:4000]
        except OSError:
            continue
        score = 0
        for line in _fm_list(head, "folders"):
            d = _norm(line)
            if c and d and (c == d or c.startswith(d + "/")):
                score = max(score, 1000 + len(d))
        if not score and low:
            hits = sum(1 for k in _fm_list(head, "keywords") if len(k) >= 2 and k.lower() in low)
            score = hits
        if score and (best is None or score > best[0]):
            best = (score, f)
    if not best:
        return None
    f = best[1]
    rel = f.relative_to(Path(bs.BRAIN_VAULT)).as_posix()
    return {"abs_path": str(f), "file": rel, "category": "프로젝트 노하우", "title": title_of(f),
            "matched": ["작업 폴더" if best[0] >= 1000 else "프로젝트 핵심어"]}


def render(items: list[dict]) -> str:
    lines = [f"<{TAG}>",
             "brain에 이 요청과 관련 있을 수 있는 기록이 있습니다 (자동 키워드 검색, 제목만).",
             "관련 있어 보이면 Read로 열어 확인하고, 아니면 무시하세요."]
    for h in items:
        lines.append(f"- [{h['category']}] {h['title']} — {h['abs_path']}"
                     f"  (일치: {', '.join(h['matched'][:4])})")
    lines.append(f"</{TAG}>")
    return "\n".join(lines)


def log_injection(items: list[dict], prompt: str, session: str) -> None:
    """주입(노출)을 남긴다. 나중에 reads와 맞춰 보면 '주입 → 실제로 읽음' 비율이 나온다."""
    try:
        USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(USAGE_DB, timeout=2)
        con.execute("""CREATE TABLE IF NOT EXISTS injections(
            path TEXT NOT NULL, category TEXT, ts TEXT NOT NULL,
            session_id TEXT, query_hash TEXT, rank INTEGER)""")
        qh = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        con.executemany(
            "INSERT INTO injections(path, category, ts, session_id, query_hash, rank)"
            " VALUES(?,?,?,?,?,?)",
            [(h["file"].replace("\\", "/"), h["category"], now, session, qh, i)
             for i, h in enumerate(items, 1)])
        con.commit()
        con.close()
    except Exception:
        pass


def main() -> None:
    if os.environ.get("CLAUDE_DISTILL_CHILD") or os.environ.get("BRAIN_RECALL_INJECT") == "0":
        return
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        return
    prompt = (ev.get("prompt") or "").strip()
    if not prompt or prompt.startswith("/") or f"<{TAG}>" in prompt:
        return
    bs = _load_search()
    if not bs.INDEX_PATH.exists():
        return
    session = ev.get("session_id") or ""
    seen = already_injected(session)
    book = project_book(ev.get("cwd") or "", bs, prompt)
    items = [h for h in pick(prompt, bs) if h["file"].replace("\\", "/") not in seen]
    if book and book["file"] not in seen:
        items = [book] + [h for h in items if h["file"].replace("\\", "/") != book["file"]][:MAX_ITEMS - 1]
    blocks, logged = [], list(items)
    if items:
        blocks.append(render(items))
    if not blocks:
        return
    log_injection(logged, prompt, session)
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                  "additionalContext": "\n".join(blocks)}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
