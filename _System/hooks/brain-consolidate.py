#!/usr/bin/env python3
"""
brain-consolidate: LLM-wiki convergence for the brain journal layer.

Modes:
  detect (default) — READ-ONLY. Cluster the journal layer (Decisions/Mistakes/
      Patterns) into topic groups ripe for Ingest.
  --ingest N       — Consolidate cluster N into one wiki page (dry-run; --apply
      to write+archive+reindex).
  --weekly         — scheduled hybrid entry point (since 2026-07-19). Small
      load (clusters <= UNATTENDED_MAX_CLUSTERS) -> unattended --auto here
      and now; backlog -> refine, drain within AUTO_MAX_CALLS, then --queue
      the remainder (2026-09-27). See _System/consolidate.md.
  --queue          — cluster only (LLM 0 calls) and write the pending-merge
      queue to _System/audit/consolidate-queue.json. brain-health surfaces it
      at SessionStart; the live session merges clusters itself (Read/Write,
      no claude -p children).
  --auto           — UNATTENDED fallback (capped at AUTO_MAX_CALLS claude
      calls/run). For every cluster: consolidate via claude with a quality
      gate (6 rules, self-assessed). Pass -> apply. "single_topic" fail ->
      split finer and retry. Other fail -> hold (logged). Writes an audit
      receipt to _System/audit/. --dry-run previews without writing.

Quality rules (the schema the human curates; everything else is automatic):
  complete · coherent · current · single_topic · format_ok · lean
  See _System/consolidation-quality.md.

Never deletes — consolidated fragments move to <category>/archived/<year>/.
"""
from __future__ import annotations

import argparse
import datetime
import itertools
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
from _brain_lib import open_db, BRAIN_VAULT, INDEX_PATH, EMBED_DIM  # noqa: E402

JOURNAL = ("Decisions", "Mistakes", "Patterns")
HOOKS_DIR = Path(__file__).parent
CLAUDE_BIN = os.environ.get("CLAUDE_DISTILL_CLAUDE_BIN", "claude")
CLAUDE_BIN = shutil.which(CLAUDE_BIN) or CLAUDE_BIN  # resolve .CMD shim on Windows
INGEST_MODEL = os.environ.get("BRAIN_CONSOLIDATE_MODEL", "claude-sonnet-4-6")
AUTO_MAX_DEPTH = int(os.environ.get("BRAIN_CONSOLIDATE_AUTO_DEPTH", "3"))
# 런당 claude 총 호출 상한. 깊이 제한은 클러스터당이라 총량을 못 막는다 —
# 백로그가 크면 분할 재시도가 호출을 수백 회로 불려 사용 한도를 태운다
# (2026-07-19 한 기기에서: 자식 콘솔 창 ~1000개, 구독 한도 소진).
AUTO_MAX_CALLS = int(os.environ.get("BRAIN_CONSOLIDATE_MAX_CALLS", "80"))
# --queue가 쓰는 통합 대기열. per-device 런타임(_System/audit/)이라 동기화 안 됨.
QUEUE_PATH_REL = Path("_System") / "audit" / "consolidate-queue.json"
# --weekly 하이브리드 분기점: 클러스터가 이 수 이하면 평시 부하로 보고 기존
# 무인 --auto(상한 내 소수 호출)로 처리, 초과하면 백로그 상황 — 정제한 대기열을
# AUTO_MAX_CALLS 안에서 소화하고 나머지만 대기열로 넘긴다 (2026-09-27 변경,
# 원래는 LLM 0회였다 — do_weekly 주석 참고).
UNATTENDED_MAX_CLUSTERS = int(
    os.environ.get("BRAIN_CONSOLIDATE_UNATTENDED_MAX", "12"))
# 대기열에 넣을 클러스터의 최대 크기 (2026-08-10). cluster()는 단일연결
# union-find라 A~B, B~C가 각각 임계를 넘으면 A와 C가 남남이어도 한 덩어리가
# 된다 — 조각이 쌓일수록 이 체이닝으로 거대 블롭이 생긴다 (07-31 923조각,
# 08-09 333조각. 후자는 공통 슬러그 0개에 태그가 "-·workflow·image·tts"인
# 잡탕이었다). 임계값만 올리는 건 대증요법이라 크기 상한을 둔다.
QUEUE_MAX_SIZE = int(os.environ.get("BRAIN_CONSOLIDATE_QUEUE_MAX_SIZE", "12"))
AUTO_COS_STEP = float(os.environ.get("BRAIN_CONSOLIDATE_AUTO_STEP", "0.08"))
# 프로젝트 울타리 안에서 쓰는 임계 (2026-08-28). 전역 임계보다 낮다 — 도메인이
# 이미 좁혀져 있어 어휘 우연으로 이질적 조각이 붙을 여지가 작기 때문이다.
INTRA_COS = float(os.environ.get("BRAIN_CONSOLIDATE_INTRA_COS", "0.50"))
AUTO_BASE_COS = float(os.environ.get("BRAIN_CONSOLIDATE_AUTO_COS", "0.60"))
# Objective cohesion floor: a group larger than TRUST_SIZE whose min pairwise
# cosine is below HARD_COS is split before trusting the LLM's self-assessment
# (self-assess alone rated a 21-file mixed blob single_topic=true).
AUTO_HARD_COS = float(os.environ.get("BRAIN_CONSOLIDATE_HARD_COS", "0.35"))
AUTO_TRUST_SIZE = int(os.environ.get("BRAIN_CONSOLIDATE_TRUST_SIZE", "3"))
FM_TAGS_RE = re.compile(r"^---\n.*?\btags?\b\s*:\s*(.*?)(?:\n[a-zA-Z_]|\n---)",
                        re.DOTALL)
FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)

QUALITY_KEYS = ("complete", "coherent", "current", "single_topic",
                "format_ok", "lean")

INGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "human-readable page title"},
        "slug": {"type": "string",
                 "description": "kebab-case ascii filename without .md"},
        "body": {"type": "string",
                 "description": "full markdown body WITHOUT frontmatter; the "
                                "current consolidated truth of the topic"},
        "supersedes_note": {"type": "string",
                            "description": "one line: what was merged/corrected"},
        "action": {"type": "string", "enum": ["new", "update"],
                   "description": "update = 제시된 기존 페이지를 갱신, new = 새 페이지"},
        "quality": {
            "type": "object",
            "description": "통합 직후 스스로 평가한 6개 품질 룰 (정직하게)",
            "properties": {
                "complete": {"type": "boolean",
                             "description": "원본 핵심 사실·노하우 손실 없이 통합"},
                "coherent": {"type": "boolean", "description": "자기모순 없음"},
                "current": {"type": "boolean",
                            "description": "모순은 최신 우선 정리, 폐기 명시"},
                "single_topic": {"type": "boolean",
                                 "description": "한 주제로 응집(잡탕이면 false)"},
                "format_ok": {"type": "boolean",
                              "description": "제목구조·한국어톤·재사용 how-to"},
                "lean": {"type": "boolean",
                         "description": "해시·경로·스택트레이스 덤프 없음"},
                "issues": {"type": "string",
                           "description": "미달 항목 설명(전부 통과면 빈 문자열)"},
            },
            "required": list(QUALITY_KEYS),
        },
    },
    "required": ["title", "slug", "body", "quality"],
}


# ---- detect (read-only) ----------------------------------------------------

_STANDALONE_RE = re.compile(r"^consolidate:\s*standalone\s*$", re.M)


def is_standalone(fp):
    """세션이 「합칠 짝 없음」으로 판정한 조각(frontmatter `consolidate: standalone`).

    2026-10-29: 같은 혼합 묶음(「UI 착수 원칙 + CRLF」 등)이 W41~W43 세 주 연속
    single_topic 실패로 보류됐다. 짝이 2~3개라 _split_retry가 더 나눌 수 없어서
    매주 판정 호출만 쓰고 같은 결과가 났다. 세션 처리에서 단독으로 둔 조각은
    클러스터·lonely 경로 모두에서 뺀다. 저널에는 남아 검색된다.
    """
    try:
        head = (BRAIN_VAULT / fp).read_text(encoding="utf-8")[:600]
    except Exception:
        return False
    end = head.find("\n---", 3)
    return head.startswith("---") and end > 0 and bool(_STANDALONE_RE.search(head[:end]))


def load_title_vectors(conn, categories):
    qmarks = ",".join("?" * len(categories))
    rows = conn.execute(
        f"""SELECT c.file_path, c.id FROM chunks c
            JOIN files f ON f.path = c.file_path
            WHERE c.chunk_idx = 0 AND f.category IN ({qmarks})""",
        categories,
    ).fetchall()
    vecs = {}
    for fp, cid in rows:
        if "archived" in fp.replace("\\", "/").split("/"):
            continue
        if is_standalone(fp):
            continue
        r = conn.execute("SELECT embedding FROM chunks_vec WHERE rowid = ?",
                         (cid,)).fetchone()
        if r and r[0]:
            v = struct.unpack(f"{EMBED_DIM}f", r[0])
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs[fp] = [x / n for x in v]
    return vecs


def slug_tokens(fp):
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", Path(fp).stem)
    return {t for t in re.split(r"[-_]", stem.lower()) if len(t) > 2}


def file_tags(fp):
    try:
        head = (BRAIN_VAULT / fp).read_text(encoding="utf-8")[:800]
    except Exception:
        return []
    m = FM_TAGS_RE.search(head)
    if not m:
        return []
    return [t for t in re.findall(r"[\w가-힣\-]+", m.group(1)) if t != "tags"]


def cluster(vecs, thr):
    files = list(vecs)
    parent = {f: f for f in files}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(files)):
        vi = vecs[files[i]]
        for j in range(i + 1, len(files)):
            if sum(a * b for a, b in zip(vi, vecs[files[j]])) >= thr:
                ra, rb = find(files[i]), find(files[j])
                if ra != rb:
                    parent[ra] = rb

    groups = {}
    for f in files:
        groups.setdefault(find(f), []).append(f)
    return sorted(groups.values(), key=len, reverse=True)


_PROJ_CACHE = {}
_PROJ_RE = re.compile(r"^cwd:\s*(.+)$", re.M)
_PROJ_DROP = {"works", "active", "claude", "users", "user", "documents", "src"}


def project_of(fp):
    """조각이 나온 프로젝트 키. 없으면 None.

    같은 프로젝트가 `X:/Work/my-app` 와 `x--Work-my-app`
    두 표기로 기록되므로 정규화한다(구분자 통일 + 드라이브·공통 상위 제거).
    """
    if fp in _PROJ_CACHE:
        return _PROJ_CACHE[fp]
    key = None
    try:
        head = (BRAIN_VAULT / fp).read_text(encoding="utf-8", errors="ignore")[:900]
        m = _PROJ_RE.search(head)
        if m:
            c = m.group(1).replace("\\", "/").strip().lower()
            c = re.sub(r"^[a-z]--", "", c)
            c = re.sub(r"^[a-z]:/", "", c)
            c = c.replace("-", "/").replace("_", "/")
            parts = [x for x in c.split("/") if x and x not in _PROJ_DROP]
            key = "/".join(parts[:2]) or None
    except Exception:
        key = None
    _PROJ_CACHE[fp] = key
    return key


def cluster_by_project(vecs, thr, intra_thr=None):
    """프로젝트로 먼저 나누고 그 안에서만 묶는다 (2026-08-28).

    왜: 순수 벡터 단일연결은 도메인이 전혀 다른 조각을 어휘 하나로 잇는다.
    실측(853 조각) — 현재 임계로 만들어진 클러스터의 **78%가 여러 프로젝트를
    섞었고**, 반려의 3분의 2를 차지하던 크기 2 묶음은 68%가 서로 다른
    프로젝트였다("날짜"라는 낱말만 공유하는 웹 포털 조각과 이미지 생성 조각처럼).
    품질 게이트의 single_topic 실패는 대부분 이 혼합의 뒤늦은 증상이다.

    프로젝트로 울타리를 치면 도메인이 이미 좁혀지므로 **안에서는 임계를 낮춰도
    안전하다.** 낮춰야 하는 이유는 실측에서 나온다 — 전역 0.60 에서 짝이 없던
    조각의 96.8%가 같은 프로젝트 안에 다른 조각을 갖고 있었고, 그들끼리는
    0.45~0.55 대에 몰려 있었다. 울타리 없이 임계만 낮추면 잡탕이 되지만,
    울타리 안에서는 그 완화가 곧 회수가 된다.

    cwd 가 없는 조각은 울타리를 칠 수 없으므로 종전대로 전역 임계로 묶는다.
    """
    if intra_thr is None:
        intra_thr = INTRA_COS
    buckets = {}
    for f in vecs:
        buckets.setdefault(project_of(f), []).append(f)
    out = []
    for proj, files in buckets.items():
        sub = {f: vecs[f] for f in files}
        out.extend(cluster(sub, thr if proj is None else intra_thr))
    return sorted(out, key=len, reverse=True)


def detect_clusters(conn, cats, thr, min_size):
    vecs = load_title_vectors(conn, cats)
    clusters = [c for c in cluster_by_project(vecs, thr) if len(c) >= min_size]
    return vecs, clusters


def _refine_one(members, vecs, thr, depth):
    """한 클러스터를 QUEUE_MAX_SIZE 이하로 쪼갠다.

    반환 (ok, oversized, dropped) — dropped는 재분할에서 단독으로 떨어져
    통합 짝이 없어진 조각 수. 끝내 안 쪼개지는 덩어리는 oversized로 뺀다:
    그건 하나의 주제가 아니라 체이닝의 산물이라 통합 대상이 아니고,
    조각들은 저널에 남아 brain-decay의 망각 대상이 된다.
    """
    if len(members) <= QUEUE_MAX_SIZE:
        return [members], [], 0
    if depth >= AUTO_MAX_DEPTH:
        return [], [members], 0
    sub_thr = thr + AUTO_COS_STEP
    subs = cluster({m: vecs[m] for m in members}, sub_thr)
    # 임계를 올려도 통째로 남으면 깊이만 쓰고 한 번 더 올려본다.
    if len(subs) == 1 and len(subs[0]) == len(members):
        return _refine_one(members, vecs, sub_thr, depth + 1)
    ok, over, dropped = [], [], 0
    for s in subs:
        if len(s) < 2:
            dropped += len(s)
            continue
        o, v, d = _refine_one(s, vecs, sub_thr, depth + 1)
        ok += o
        over += v
        dropped += d
    return ok, over, dropped


def refine_for_queue(clusters, vecs, thr):
    """대기열용 정제: 거대 클러스터를 재분할하고, 안 쪼개지는 건 제외한다."""
    ok, over, dropped = [], [], 0
    for members in clusters:
        o, v, d = _refine_one(members, vecs, thr, 0)
        ok += o
        over += v
        dropped += d
    ok.sort(key=len, reverse=True)
    return ok, over, dropped


def min_pairwise_cos(members, vecs):
    if len(members) < 2:
        return 1.0
    lo = 1.0
    for a, b in itertools.combinations(members, 2):
        lo = min(lo, sum(x * y for x, y in zip(vecs[a], vecs[b])))
    return lo


# ---- consolidate (claude -p + quality gate) --------------------------------

def fragment_body(fp):
    txt = (BRAIN_VAULT / fp).read_text(encoding="utf-8")
    return FRONTMATTER_RE.sub("", txt, count=1).strip()


class ClaudeBudgetExhausted(Exception):
    """런당 호출 상한(AUTO_MAX_CALLS) 도달 — do_auto가 잡아 남은 작업을 이월한다."""


_claude_calls = 0
# 이번 런의 호출 상한. 기본은 AUTO_MAX_CALLS 지만 lonely 경로는 더 좁게 잡는다.
_call_cap = AUTO_MAX_CALLS


def call_claude(prompt, schema, model, effort, timeout):
    global _claude_calls
    if _claude_calls >= _call_cap:
        raise ClaudeBudgetExhausted(f"claude 호출 상한({_call_cap}회) 도달")
    _claude_calls += 1
    # --strict-mcp-config + 빈 mcp-config: 자식 헤드리스 세션이 MCP·채널 플러그인을
    # 부팅하지 않게 차단. Discord 플러그인이 부팅되면 호출 1회당 봇 게이트웨이 세션
    # 1회를 소모 — 대량 런이 24h 한도(1000회)를 태워 봇을 오프라인시킨다 (2026-07-19).
    cmd = [CLAUDE_BIN, "-p", "--model", model, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--effort", effort,
           "--disable-slash-commands", "--no-session-persistence",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    env = os.environ.copy()
    env["CLAUDE_DISTILL_CHILD"] = "1"  # stop child Stop-hook re-distilling
    try:
        # CREATE_NO_WINDOW: Windows에서 자식마다 콘솔 창이 뜨는 것 차단
        # (POSIX에서는 속성이 없어 0 — 무동작).
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, env=env,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print(f"claude call failed: {e!r}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"claude rc={proc.returncode}; stderr={proc.stderr[:400]!r}",
              file=sys.stderr)
        return None
    try:
        wrapper = json.loads((proc.stdout or "").strip())
    except Exception as e:
        print(f"wrapper parse failed: {e!r}", file=sys.stderr)
        return None
    s = wrapper.get("structured_output")
    if isinstance(s, dict):
        return s
    rt = wrapper.get("result", "")
    if isinstance(rt, str):
        rt = re.sub(r"^```(?:json)?\s*", "", rt.strip())
        rt = re.sub(r"\s*```$", "", rt)
        try:
            return json.loads(rt)
        except Exception:
            pass
    return None


# ---- 기존 Knowledge 페이지 갱신 (2026-09-28, 로드맵 P3 / 차용 검토 A3) --------
# 그전까지 통합은 새 페이지만 만들었다. 같은 주제의 조각이 몇 주 간격으로 다시 오면
# 페이지가 하나 더 생겼고(Cloudflare 터널 4개, 히어로 섹션 6개 — 08-28 구조 감사),
# 이름이 같으면 기존 페이지를 경고 없이 덮어썼다. 이제 조각 묶음과 가장 가까운
# 기존 페이지를 찾아 LLM에게 함께 보여 주고 갱신/신규를 고르게 한다.
# 기준 0.68: 09-28 대기열 70묶음 실측에서 0.72 이상은 전부, 0.67~0.68도 대부분
# 같은 주제였다. 최종 판단은 LLM이 하므로 후보는 넉넉하게 제시한다.
UPDATE_COS = float(os.environ.get("BRAIN_CONSOLIDATE_UPDATE_COS", "0.68"))
# 갱신 결과가 기존 본문보다 이만큼 짧아지면 붕괴로 보고 보류한다 (ACE context collapse).
COLLAPSE_RATIO = float(os.environ.get("BRAIN_CONSOLIDATE_COLLAPSE", "0.6"))
UPDATE_MAX_CHARS = 30000     # 이보다 큰 페이지는 프롬프트에 통째로 넣지 않는다
CHANGELOG_RE = re.compile(r"\n## 변경 이력\n.*\Z", re.S)
_JOURNAL_CATS = ["Decisions", "Mistakes", "Patterns"]
_vec_cache: dict = {}


def _vectors(kind):
    if kind not in _vec_cache:
        conn = open_db(create=False)
        v = load_title_vectors(conn, ["Knowledge"] if kind == "k" else _JOURNAL_CATS)
        if kind == "k":
            v = {fp: x for fp, x in v.items() if not Path(fp).name.startswith("_")}
        _vec_cache[kind] = v
    return _vec_cache[kind]


def nearest_page(members):
    """조각 묶음의 제목 벡터 중심과 가장 가까운 Knowledge 페이지. 없으면 None."""
    try:
        jv, kv = _vectors("j"), _vectors("k")
    except Exception as e:
        print(f"nearest_page: 벡터 로드 실패 ({e!r}) — 신규로만 진행", file=sys.stderr)
        return None
    mv = [jv[m] for m in members if m in jv]
    if not mv or not kv:
        return None
    cen = [sum(col) for col in zip(*mv)]
    n = math.sqrt(sum(x * x for x in cen)) or 1.0
    cen = [x / n for x in cen]
    fp, cos = max(((fp, sum(a * b for a, b in zip(cen, v))) for fp, v in kv.items()),
                  key=lambda t: t[1])
    if cos < UPDATE_COS:
        return None
    path = BRAIN_VAULT / fp
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = FRONTMATTER_RE.match(text)
    # scope: shared 는 다른 기기로 퍼지는 문서라 자동 갱신하지 않는다 — 사람이 고친다.
    if fm and re.search(r"^scope:\s*shared\s*$", fm.group(0), re.M):
        return None
    if len(text) > UPDATE_MAX_CHARS:
        return None
    body = CHANGELOG_RE.sub("", FRONTMATTER_RE.sub("", text, count=1)).strip()
    return {"path": path, "slug": path.stem, "cos": round(cos, 3), "text": text,
            "front": fm.group(0) if fm else "", "body": body}


def build_ingest_prompt(members, target=None):
    parts = [
        "당신은 Karpathy LLM Wiki를 유지하는 사서다. 아래 저널 조각들을 "
        "'현재의 진실'을 담은 위키 페이지 하나로 통합하라.",
        "- 모순은 최신 조각(날짜 큰 것) 우선. 대체된 주장은 supersedes_note에 한 줄.",
        "- 한국어로. body는 frontmatter 없는 markdown. 재사용 how-to/원칙 위주.",
        "- slug는 kebab-case 영문.",
        "- '어제', '지난주' 같은 상대 날짜는 조각 날짜 기준 절대 날짜(YYYY-MM-DD)로 바꿔라.",
        "- body에 '변경 이력' 절을 넣지 마라. 시스템이 붙인다.",
        "",
        "통합 후 6개 품질 룰을 스스로 정직하게 평가해 quality에 기록하라:",
        "  complete: 원본 핵심이 손실 없이 들어갔나",
        "  coherent: 자기모순 없나",
        "  current: 모순을 최신 우선 정리하고 폐기된 건 명시했나",
        "  single_topic: 한 주제로 응집됐나 — 여러 주제가 섞였으면 false (정직히)",
        "  format_ok: 제목구조·한국어톤·재사용 how-to 형식인가",
        "  lean: 커밋해시·파일경로·스택트레이스 등 형상관리 덤프가 없나",
        "single_topic이 false면 시스템이 더 잘게 나눠 재시도하니 솔직히 false로 표시하라.",
        "",
        f"조각 {len(members)}개:",
    ]
    if target:
        parts[-1:-1] = [
            "",
            f"## 기존 페이지 (갱신 후보: Knowledge/{target['slug']}.md)",
            "조각이 이 페이지와 같은 주제면 action=update로 이 페이지를 갱신하라:",
            "  - 기존 내용 중 여전히 유효한 것은 전부 유지한다. 줄이지 마라.",
            "  - 새 사실·노하우를 알맞은 절에 더한다.",
            "  - 조각이 기존 주장을 뒤집으면 최신으로 고치고 supersedes_note에 한 줄 남긴다.",
            "  - slug는 기존 페이지와 같게 둔다.",
            "주제가 다르면 action=new로 새 페이지를 만들고 기존 페이지는 무시하라.",
            "",
            "--- 기존 페이지 본문 ---",
            target["body"],
            "--- 기존 페이지 끝 ---",
            "",
        ]
    else:
        parts[-1:-1] = ["action은 new로 둔다.", ""]
    for i, fp in enumerate(sorted(members), 1):
        mo = re.match(r"(\d{4}-\d{2}-\d{2})", Path(fp).stem)
        parts.append(f"\n--- [{i}] {mo.group(1) if mo else '?'}  {fp} ---")
        parts.append(fragment_body(fp))
    return "\n".join(parts)


def consolidate_once(members, effort):
    target = nearest_page(members)
    draft = call_claude(build_ingest_prompt(members, target), INGEST_SCHEMA,
                        INGEST_MODEL, effort, 300)
    if not draft:
        return draft
    if draft.get("action") == "update" and target:
        draft["_target"] = target
        new_len, old_len = len(draft.get("body", "").strip()), len(target["body"])
        if old_len and new_len < old_len * COLLAPSE_RATIO:
            # 품질 게이트로 보류시킨다 — 조각은 그대로 남아 다음 런에서 다시 시도된다.
            q = draft.setdefault("quality", {})
            q["complete"] = False
            q["issues"] = (f"붕괴 방지: 갱신본 {new_len}자 < 기존 {old_len}자의 "
                           f"{COLLAPSE_RATIO:.0%}. " + q.get("issues", "")).strip()
    return draft


def quality_pass(q):
    return bool(q) and all(q.get(k) for k in QUALITY_KEYS)


def build_page_text(draft, members):
    today = datetime.date.today().isoformat()
    year = today[:4]
    note = draft.get("supersedes_note", "")
    target = draft.get("_target")
    if target:
        # 갱신: 기존 frontmatter(태그·scope 등)와 변경 이력을 살리고 updated만 바꾼다.
        slug = target["slug"]
        front = target["front"] or f"---\ntitle: {draft['title']}\ntype: knowledge\n---\n"
        if re.search(r"^updated:.*$", front, re.M):
            front = re.sub(r"^updated:.*$", f"updated: {today}", front, count=1, flags=re.M)
        else:
            front = front.replace("\n---\n", f"\nupdated: {today}\n---\n", 1)
        m = CHANGELOG_RE.search(target["text"])
        log = m.group(0).strip() if m else "## 변경 이력"
        entry = (f"- **{today}** Update — {len(members)}개 저널 조각 반영. {note} "
                 f"이전 판은 `Knowledge/archived/{year}/`.")
        full = front + "\n" + draft["body"].strip() + "\n\n" + log + "\n\n" + entry + "\n"
        return slug, full, year
    slug = re.sub(r"[^a-z0-9\-]+", "-", draft["slug"].lower()).strip("-")
    # 같은 이름의 페이지를 조용히 덮어쓰지 않는다 — 그전까지 write_page가 그렇게 했다.
    base, n = slug, 2
    while (BRAIN_VAULT / f"Knowledge/{slug}.md").exists():
        slug, n = f"{base}-{n}", n + 1
    front = (f"---\ntitle: {draft['title']}\ntype: knowledge\nstatus: active\n"
             f"updated: {today}\n---\n\n")
    full = front + draft["body"].strip() + (
        f"\n\n## 변경 이력\n\n- **{today}** Ingest — {len(members)}개 저널 조각 "
        f"통합. {note} 원본은 `<category>/archived/{year}/`.\n")
    return slug, full, year


def write_page(slug, full, members, year, reindex=True):
    page = BRAIN_VAULT / f"Knowledge/{slug}.md"
    if page.exists():
        # 여기까지 온 기존 페이지는 의도된 갱신이다 (신규의 이름 충돌은 build_page_text가
        # 피한다). Knowledge 는 git 밖이라 이 사본이 유일한 되돌리기 수단이다.
        bk = BRAIN_VAULT / "Knowledge" / "archived" / year
        bk.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(page, bk / f"{slug}.{stamp}.md")
    page.write_text(full, encoding="utf-8")
    # NOTE: do NOT append to _index.md here. Since the 2026-06-11 catalog
    # policy, _index.md stays curated prose + a _catalog.md pointer, and the
    # auto-generated _catalog.md (gitignored) already lists every note. The old
    # Karpathy 'update index' append (index_add) leaked per-device note links
    # into the tracked/shared skeleton, forcing a manual revert every audit
    # (6/24, 6/30). Removed 2026-06-30.
    for fp in members:
        cat = fp.replace("\\", "/").split("/")[0]
        d = BRAIN_VAULT / cat / "archived" / year
        d.mkdir(parents=True, exist_ok=True)
        (BRAIN_VAULT / fp).rename(d / Path(fp).name)
    if reindex:
        subprocess.run([sys.executable, str(HOOKS_DIR / "brain-reindex.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)


def do_ingest(conn, members, apply, effort):
    print(f"## Ingest {len(members)}개 ({'APPLY' if apply else 'dry-run'})",
          file=sys.stderr)
    draft = consolidate_once(members, effort)
    if not draft:
        print("통합 실패 (claude 무응답).", file=sys.stderr)
        return 1
    q = draft.get("quality", {})
    slug, full, year = build_page_text(draft, members)
    ok = quality_pass(q)
    print("=" * 60)
    print(f"제안: Knowledge/{slug}.md   품질 {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(f"  미달: {[k for k in QUALITY_KEYS if not q.get(k)]} — {q.get('issues','')}")
    print("=" * 60)
    print(full)
    print("=" * 60)
    if not apply:
        print("\n(dry-run)")
        return 0
    if not ok:
        print(f"\n품질 미달 → 적용 보류. (단일주제 아니면 --auto가 세분화 처리)")
        return 1
    write_page(slug, full, members, year)
    print(f"\n✅ wrote Knowledge/{slug}.md + archived {len(members)}")
    return 0


# ---- auto (unattended, quality-gated, recursive split) ---------------------

def _split_retry(conn, members, vecs, effort, apply, thr, depth, ledger):
    sub_thr = thr + AUTO_COS_STEP
    subs = [s for s in cluster({m: vecs[m] for m in members}, sub_thr)
            if len(s) >= 2]
    if not subs:
        return False
    for s in subs:
        _auto_recur(conn, s, vecs, effort, apply, sub_thr, depth + 1, ledger)
    return True


def _auto_recur(conn, members, vecs, effort, apply, thr, depth, ledger):
    # 조각은 실행 중에 이동·아카이브될 수 있다 (2026-07-18: 중복 정리가 도중에
    # 조각 690개를 옮겨서 fragment_body의 FileNotFoundError로 70분 런 전체가
    # 죽었다). 없어진 조각은 빼고 진행하고, 통합할 짝이 안 남으면 스킵한다 —
    # 클러스터 하나의 문제가 런 전체를 무너뜨리지 않게 (fail-open).
    alive = [m for m in members if (BRAIN_VAULT / m).exists()]
    gone = [m for m in members if m not in alive]
    if gone:
        ledger.append(("hold", gone, "조각 소실(이동/아카이브됨) — 이번 런에서 제외"))
    if len(alive) < 2:
        return
    members = alive
    cohesion = min_pairwise_cos(members, vecs)
    # Objective floor first: split big+incoherent groups BEFORE spending a
    # claude call (self-assessment runs too lenient on mixed blobs).
    if (len(members) > AUTO_TRUST_SIZE and cohesion < AUTO_HARD_COS
            and depth < AUTO_MAX_DEPTH):
        if _split_retry(conn, members, vecs, effort, apply, thr, depth, ledger):
            return

    draft = consolidate_once(members, effort)
    if not draft:
        ledger.append(("fail", members, "claude 무응답"))
        return
    q = draft.get("quality", {})
    if quality_pass(q):
        slug, full, year = build_page_text(draft, members)
        if apply:
            write_page(slug, full, members, year, reindex=False)
        ledger.append(("merged", members, slug))
        return
    # single_topic fail → split finer and retry
    if not q.get("single_topic") and depth < AUTO_MAX_DEPTH:
        if _split_retry(conn, members, vecs, effort, apply, thr, depth, ledger):
            return
    # other fails or cannot split further → hold (logged, fragments stay)
    bad = [k for k in QUALITY_KEYS if not q.get(k)]
    ledger.append(("hold", members, f"{bad}: {q.get('issues','')}"))


def do_auto(conn, vecs, clusters, dry, effort):
    print(f"## auto-consolidate ({'preview' if dry else 'APPLY'}) — 품질 게이트 무인",
          file=sys.stderr)
    ledger = []
    budget_hit = False
    for i, c in enumerate(clusters):
        try:
            _auto_recur(conn, c, vecs, effort, not dry, AUTO_BASE_COS, 0, ledger)
        except ClaudeBudgetExhausted as e:
            # 진행 중이던 클러스터 포함 남은 전부를 보류로 이월 — 조각은 그대로
            # 남으므로 다음 주간 런이 이어서 처리한다.
            for rest in clusters[i:]:
                ledger.append(("hold", rest, f"{e} — 다음 런으로 이월"))
            budget_hit = True
            break
    if not dry and any(s == "merged" for s, _, _ in ledger):
        subprocess.run([sys.executable, str(HOOKS_DIR / "brain-reindex.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)

    merged = [(m, i) for s, m, i in ledger if s == "merged"]
    held = [(m, i) for s, m, i in ledger if s == "hold"]
    failed = [(m, i) for s, m, i in ledger if s == "fail"]

    lines = [f"# consolidate 영수증 ({'preview' if dry else 'applied'}) "
             f"{datetime.date.today().isoformat()}",
             f"통합 {len(merged)} / 보류 {len(held)} / 실패 {len(failed)}",
             f"claude 호출 {_claude_calls}회 / 상한 {AUTO_MAX_CALLS}회"
             + (" — 상한 도달로 조기 종료" if budget_hit else ""), ""]
    for m, slug in merged:
        lines.append(f"- ✅ 통합 → Knowledge/{slug}.md  ({len(m)}개 조각)")
    for m, iss in held:
        common = (set.intersection(*[slug_tokens(x) for x in m])
                  if len(m) > 1 else set())
        lines.append(f"- ⏸ 보류 [{len(m)}개] "
                     f"{', '.join(sorted(common)) or '혼합'} — {iss}")
    for m, iss in failed:
        lines.append(f"- ✗ 실패 [{len(m)}개] — {iss}")
    receipt = "\n".join(lines)
    print("\n" + receipt)

    if not dry:
        yr, wk, _ = datetime.date.today().isocalendar()
        audit = BRAIN_VAULT / "_System" / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        (audit / f"consolidate-{yr}-W{wk:02d}.md").write_text(
            receipt + "\n", encoding="utf-8")
        print(f"\naudit: _System/audit/consolidate-{yr}-W{wk:02d}.md")
        # P2: decay sweep runs AFTER consolidation so clusterable journals
        # converge into Knowledge before low-retention ones get archived.
        # Best-effort — decay failure must not fail the consolidate run.
        decay = HOOKS_DIR / "brain-decay.py"
        if decay.exists():
            try:
                subprocess.run([sys.executable, str(decay), "--apply"],
                               capture_output=True, text=True,
                               encoding="utf-8", timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
                print("decay: swept (receipt in _System/audit/)")
            except Exception as e:
                print(f"decay: skipped ({e!r})", file=sys.stderr)
        # P0 (2026-07-13): push any scope:shared Knowledge/Memory changes to
        # the remote after this run. Runs last (after decay) -- order doesn't
        # matter functionally (decay only touches journals, sync only touches
        # Knowledge/Memory) but this matches "chain more steps at the end."
        # Best-effort, same as decay -- a push failure must not fail consolidate.
        sync = HOOKS_DIR / "brain-sync.py"
        if sync.exists():
            try:
                sp = subprocess.run([sys.executable, str(sync), "--push"],
                                    capture_output=True, text=True,
                                    encoding="utf-8", timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
                if sp.returncode == 0:
                    print("sync: shared content pushed (or nothing to push)")
                else:
                    print(f"sync: needs attention ({sp.stderr.strip()[:200]})",
                          file=sys.stderr)
            except Exception as e:
                print(f"sync: skipped ({e!r})", file=sys.stderr)
    return 0


# ---- queue (cluster only, LLM 0회 — 세션이 이어받는다) ----------------------

def _cluster_entry(members):
    common = set.intersection(*[slug_tokens(m) for m in members])
    tagc = Counter(t for m in members for t in file_tags(m))
    return {"size": len(members), "common_slug": sorted(common),
            "top_tags": [t for t, _ in tagc.most_common(4)],
            "files": sorted(members)}


def do_queue(vecs, clusters, thr=AUTO_BASE_COS):
    """클러스터링 결과를 대기열 파일로 저장만 한다. claude 호출 0회.

    처리 주체는 다음 세션의 claude (brain-health가 대기 건수를 알리고,
    세션이 _System/consolidate.md §Session processing 계약대로 직접 통합).
    빈 클러스터여도 파일을 써서 brain-health가 '대기 0'을 알 수 있게 한다.

    2026-08-10: 저장 전에 QUEUE_MAX_SIZE로 정제한다. 안 그러면 체이닝으로
    생긴 블롭(333조각)이 대기열의 2/3를 차지한 채 영원히 처리 불가로 남아,
    세션이 매번 "대기 498조각" 경고만 보고 아무것도 못 한다.
    """
    refined, oversized, dropped = refine_for_queue(clusters, vecs, thr)
    payload = {
        "generated": datetime.date.today().isoformat(),
        "singletons": len(vecs) - sum(len(c) for c in clusters) + dropped,
        "clusters": [_cluster_entry(m) for m in refined],
    }
    if oversized:
        # 큐에는 안 넣지만 조용히 사라지면 안 된다 — 무엇을 뺐는지 남긴다.
        payload["oversized"] = [_cluster_entry(m) for m in oversized]
    out = BRAIN_VAULT / QUEUE_PATH_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    n_frag = sum(len(c) for c in refined)
    print(f"통합 대기열 갱신: {len(refined)}클러스터 / {n_frag}조각 → "
          f"{QUEUE_PATH_REL}")
    if oversized:
        n_over = sum(len(c) for c in oversized)
        print(f"⚠️ 재분할 실패로 제외: {len(oversized)}덩어리 / {n_over}조각 "
              f"(단일 주제가 아님 — 통합 말고 망각 대상). "
              f"목록은 대기열 파일의 oversized 항목에.", file=sys.stderr)
    return 0


def do_weekly(conn, vecs, clusters, effort, thr=AUTO_BASE_COS):
    """주간 스케줄 진입점 (하이브리드, 2026-07-19 / 백로그 경로 2026-09-27).

    평시(클러스터 ≤ UNATTENDED_MAX_CLUSTERS)는 기존 무인 --auto로 그 자리에서
    끝낸다. 백로그(초과)면 대기열용으로 정제한 클러스터를 AUTO_MAX_CALLS 안에서
    무인 통합하고, 남은 것만 대기열로 넘긴다.

    백로그 경로가 원래 LLM 0회였던 이유는 폭주 차단이었지만, 그건 호출 상한이
    이미 막는다. 0회로 두자 매주 신규(21~29클러스터)가 임계 12를 넘어 무인
    경로가 한 번도 안 돌았고, 세션 처리(2~3개/세션)는 따라잡지 못해 대기열이
    W33 이후 345 → 392클러스터로 불기만 했다 (2026-09-27 실측).
    """
    if len(clusters) <= UNATTENDED_MAX_CLUSTERS:
        print(f"weekly: {len(clusters)}클러스터 ≤ 임계 {UNATTENDED_MAX_CLUSTERS}"
              f" — 무인 처리", file=sys.stderr)
        rc = do_auto(conn, vecs, clusters, dry=False, effort=effort)
        # 무인으로 다 처리했으면 이전 주의 대기열은 낡은 정보 — 치운다.
        stale = BRAIN_VAULT / QUEUE_PATH_REL
        if stale.exists():
            stale.unlink()
        # 짝 없는 조각은 위 경로가 영원히 처리하지 못한다 (묶을 상대가 없다).
        # 남은 예산 안에서 전용 경로로 이어 돌린다 (2026-09-01).
        if _claude_calls < AUTO_MAX_CALLS:
            try:
                do_lonely(conn, vecs, clusters, effort, apply=True)
            except Exception as e:
                print(f"lonely 경로 실패(무시하고 진행): {e!r}", file=sys.stderr)
        return rc
    print(f"weekly: {len(clusters)}클러스터 > 임계 {UNATTENDED_MAX_CLUSTERS}"
          f" — 백로그로 판단, 상한 {AUTO_MAX_CALLS}회 안에서 소화 후 나머지 대기열로",
          file=sys.stderr)
    # 원본 클러스터엔 체이닝 블롭(W33: 2192조각)이 섞여 있어 그대로 넣으면 재분할이
    # 예산을 다 먹는다 — 대기열과 같은 정제를 거친 것만 소화한다.
    refined, _, _ = refine_for_queue(clusters, vecs, thr)
    rc = do_auto(conn, vecs, refined, dry=False, effort=effort)
    # 통합된 조각은 archived/로 옮겨졌다 — 남은 조각으로 대기열을 다시 쓴다.
    left = [[m for m in c if (BRAIN_VAULT / m).exists()] for c in clusters]
    do_queue(vecs, [c for c in left if len(c) >= 2], thr)
    # 백로그 주에도 짝 없는 조각을 남은 예산으로 돌린다 — 이 분기만 빠져 있어서
    # 적체가 이어지는 동안 565건이 한 번도 처리되지 않았다 (2026-09-28 실측).
    if _claude_calls < AUTO_MAX_CALLS:
        try:
            do_lonely(conn, vecs, clusters, effort, apply=True)
        except Exception as e:
            print(f"lonely 경로 실패(무시하고 진행): {e!r}", file=sys.stderr)
    return rc


# ---- lonely fragments (가치 판정 → 주제 배정 → 좁은 통합) ------------------
# 2026-09-01. 왜 따로 경로가 필요한가: 이 파이프라인의 묶기 규칙은 "비슷한 조각이
# 둘 이상 있어야 지식이 된다"는 것 하나인데, 백로그의 절반 이상이 짝이 없어
# **아무리 돌려도 처리되지 않았다**(2026-08-28 실측 852개 중 468개). 그래서
# 2026-08-28에 주간잡을 멈췄다(_System/audit/consolidate-paused.md).
#
# 09-01에 209개를 손으로 돌려 방법을 찾았다 —
#   ① (프로젝트 × kind)로 묶는 것은 **안 통한다**. 9조 중 8조가 single_topic
#      반려. 서랍은 주제가 아니기 때문이다.
#   ② LLM에 제목·요지만 주고 **주제로 배정**시키면 통한다. 그리고 주제는
#      좁을수록 붙는다 — 넓은 28주제 57% → 좁은 26주제 69%.
#   ③ 최종 150/209(72%)가 문서 35개로 올라갔고, 두 번 다 안 붙은 59개는 남겼다.
# 이 경로는 그 순서를 그대로 코드로 옮긴 것이다.

# 이 경로 한 번에 허용할 claude 호출 수. 전체 상한(AUTO_MAX_CALLS)과 별개로
# 더 좁게 잡는다 — 무인 실행에서 폭주하지 않게 (2026-07-19 한도 소진 사건).
LONELY_MAX_CALLS = int(os.environ.get("BRAIN_CONSOLIDATE_LONELY_MAX", "30"))
# 판정·시도 이력. per-device 런타임이라 동기화되지 않는다.
VERDICTS_REL = Path("_System") / "audit" / "promotion-verdicts.json"
# 통합에 실패한 조각을 다시 시도하기까지의 유예(일). 정지 사유가 바로 이것이었다 —
# W33·W34 보류 목록의 라벨 11개가 그대로 겹쳐 매주 예산의 30%를 같은 실패에 태웠다.
LONELY_RETRY_DAYS = int(os.environ.get("BRAIN_CONSOLIDATE_LONELY_RETRY_DAYS", "45"))
LONELY_TRIAGE_BATCH = 25



TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "promote": {"type": "boolean"},
            "kind": {"type": "string",
                     "description": "principle | pitfall | howto | fact | log | context"},
            "reason": {"type": "string", "description": "한 줄, 20자 내외"},
        },
        "required": ["index", "promote", "kind", "reason"]}}},
    "required": ["verdicts"],
}

THEME_SCHEMA = {
    "type": "object",
    "properties": {"themes": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "kebab-case, 영문, 4~6단어"},
            "title": {"type": "string", "description": "한국어 문서 제목"},
            "members": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["slug", "title", "members"]}}},
    "required": ["themes"],
}


def lonely_files(vecs, clusters):
    """어느 클러스터에도 들지 못한 조각."""
    inside = {m for c in clusters for m in c}
    return sorted(f for f in vecs if f not in inside)


def load_verdicts():
    p = BRAIN_VAULT / VERDICTS_REL
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_verdicts(store):
    p = BRAIN_VAULT / VERDICTS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")


def _cooling(rec, today):
    """직전 통합 시도가 실패했고 아직 유예 기간이면 True."""
    last = rec.get("last_try")
    if not last or rec.get("last_result") == "merged":
        return False
    try:
        d = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days
    except Exception:
        return False
    return d < LONELY_RETRY_DAYS


def fragment_brief(fp, i):
    """판정·배정용 요약 한 줄. 본문 전체를 넣지 않는다 — 판정에는 제목과 요지면
    충분하고, 그래야 25개를 한 번의 호출에 담을 수 있다."""
    txt = (BRAIN_VAULT / fp).read_text(encoding="utf-8", errors="ignore")
    imp = re.search(r"^importance:\s*(\d+)", txt, re.M)
    body = re.sub(r"^#.*\n+", "", txt.split("---", 2)[-1].strip()).replace("\n", " ")
    return (f"[{i}] ({Path(fp).parent.name}/중요도{imp.group(1) if imp else '?'}) "
            f"{Path(fp).stem}\n     {body[:230]}")


TRIAGE_PROMPT = """저널 조각들이 있다. 각각 **혼자서 재사용 가능한 지식 문서가 될 가치가 있는지** 판정하라.
묶을 짝이 없어 통합되지 못한 것들이라, 승격하지 않으면 그대로 묻힌다.

승격 O — 다음 세션에서도 재사용할 원칙·절차 / 함정·실패 패턴과 그 이유 /
         다른 프로젝트에도 적용 가능한 노하우 / 나중에 다시 찾을 확정 사실
승격 X — 특정 세션의 일회성 메모 / 단순 진행 경과 / 그 상황에만 유효한 맥락 /
         내용이 너무 얇아 문서 한 장이 안 되는 것

kind 는 principle(원칙) · pitfall(함정) · howto(절차) · fact(확정사실) ·
log(진행경과) · context(일회성맥락) 중 하나.
엄격하게 보라. 애매하면 promote=false.

"""

THEME_PROMPT = """아래는 저널 조각 목록이다. 각 줄은 [번호] (종류/프로젝트) 슬러그 — 요지.
이것들을 재사용 가능한 지식 문서로 통합하려 한다. **주제로 묶어라.**

규칙
- 응집축은 프로젝트가 아니라 **내용**이다. 서로 다른 프로젝트의 조각이 같은
  주제면 같이 묶어라. 반대로 같은 프로젝트라도 주제가 다르면 갈라라.
- 한 묶음은 **2~5개**. 6개를 넘기지 마라.
- 「보안」·「UI 디자인」·「파일 변환」처럼 **도메인 이름으로 묶지 마라.**
  한 문서로 읽었을 때 한 가지 이야기가 되는 좁기로 묶는다.
  예: 「보안」(X) → 「XSS·프록시 입력 검증」(O) / 「시크릿 스캔과 히스토리 정리」(O)
- 서로 다른 서비스·도구는 같은 묶음에 넣지 마라 (Slack과 Discord, deck.gl과 Mapbox 등).
- 억지로 다 넣지 마라. 어느 주제에도 자연스럽게 안 붙는 조각은 **빼라**.
- 한 번호는 한 묶음에만 넣는다.

"""


def triage_fragments(files, effort, store, today):
    """가치 판정. 이미 판정한 조각은 다시 묻지 않는다."""
    todo = [f for f in files if f not in store]
    if not todo:
        return 0
    judged = 0
    for s in range(0, len(todo), LONELY_TRIAGE_BATCH):
        chunk = todo[s:s + LONELY_TRIAGE_BATCH]
        items = "\n".join(fragment_brief(f, i) for i, f in enumerate(chunk))
        r = call_claude(TRIAGE_PROMPT + items, TRIAGE_SCHEMA,
                        INGEST_MODEL, effort, 300)
        if not r:
            print(f"triage 배치 {s // LONELY_TRIAGE_BATCH + 1}: 무응답",
                  file=sys.stderr)
            continue
        got = set()
        for v in r.get("verdicts", []):
            i = v.get("index")
            if isinstance(i, int) and 0 <= i < len(chunk) and i not in got:
                got.add(i)
                store[chunk[i]] = {"promote": bool(v.get("promote")),
                                   "kind": v.get("kind", "?"),
                                   "reason": v.get("reason", ""),
                                   "judged_at": today}
        judged += len(got)
        # 번호가 어긋나면 조용히 사라진다 (2026-08-31: 322개 중 297개가 그렇게
        # 없어졌는데 로그는 "13배치 전부 성공"으로 보였다). 세어서 알린다.
        if len(got) != len(chunk):
            print(f"⚠️ triage 배치 {s // LONELY_TRIAGE_BATCH + 1}: "
                  f"{len(chunk)}개 요청 / {len(got)}개만 판정됨 "
                  f"— {len(chunk) - len(got)}개 누락", file=sys.stderr)
    save_verdicts(store)
    return judged


def assign_themes(files, store, effort):
    """주제 배정. 프로젝트가 아니라 내용으로 묶는다."""
    if len(files) < 2:
        return []
    lines = []
    for i, f in enumerate(files):
        rec = store.get(f, {})
        proj = project_of(f) or "-"
        lines.append(f"[{i}] ({rec.get('kind','?')}/{proj}) "
                     f"{Path(f).stem[11:]} — {rec.get('reason','')}")
    r = call_claude(THEME_PROMPT + "\n".join(lines), THEME_SCHEMA,
                    INGEST_MODEL, effort, 600)
    if not r:
        return []
    seen, out = set(), []
    for t in r.get("themes", []):
        ms = [m for m in t.get("members", [])
              if isinstance(m, int) and 0 <= m < len(files) and m not in seen]
        seen.update(ms)
        if len(ms) >= 2:
            out.append({"slug": t.get("slug", ""), "title": t.get("title", ""),
                        "files": [files[m] for m in ms]})
    return out


def do_lonely(conn, vecs, clusters, effort, apply=True, budget=None):
    """짝 없는 조각 경로. 판정 → 주제 배정 → 주제별 품질 게이트 통합."""
    global _call_cap
    _call_cap = min(AUTO_MAX_CALLS,
                    _claude_calls + (budget or LONELY_MAX_CALLS))
    today = datetime.date.today().isoformat()
    store = load_verdicts()
    files = lonely_files(vecs, clusters)
    print(f"## lonely ({'APPLY' if apply else 'dry-run'}) — 짝 없는 조각 "
          f"{len(files)}개, 호출 예산 {_call_cap - _claude_calls}회", file=sys.stderr)

    try:
        n = triage_fragments(files, effort, store, today)
        if n:
            print(f"가치 판정 {n}개 (신규)", file=sys.stderr)
        promote = [f for f in files
                   if store.get(f, {}).get("promote")
                   and not _cooling(store[f], today)]
        cooled = sum(1 for f in files if f in store and store[f].get("promote")
                     and _cooling(store[f], today))
        print(f"승격 대상 {len(promote)}개 (유예 중 {cooled}개 제외)", file=sys.stderr)
        themes = assign_themes(promote, store, effort)
        print(f"주제 {len(themes)}개 · 배정 "
              f"{sum(len(t['files']) for t in themes)}개", file=sys.stderr)

        merged, held = [], []
        for t in themes:
            members = [f for f in t["files"] if (BRAIN_VAULT / f).exists()]
            if len(members) < 2:
                continue
            draft = consolidate_once(members, effort)
            if not draft:
                held.append((t["title"], members, "claude 무응답"))
                continue
            q = draft.get("quality", {})
            bad = [k for k in QUALITY_KEYS if not q.get(k)]
            if bad:
                held.append((t["title"], members, f"{bad}: {q.get('issues','')}"))
                for f in members:
                    store.setdefault(f, {}).update(
                        {"last_try": today, "last_result": "hold"})
                continue
            # 이름 충돌 회피와 기존 페이지 갱신은 build_page_text가 맡는다 (2026-09-28).
            slug, full, year = build_page_text(draft, members)
            if apply:
                write_page(slug, full, members, year, reindex=False)
            for f in members:
                store.setdefault(f, {}).update(
                    {"last_try": today, "last_result": "merged"})
            merged.append((slug, members))
    except ClaudeBudgetExhausted as e:
        print(f"lonely: {e} — 나머지는 다음 런으로", file=sys.stderr)
    finally:
        save_verdicts(store)
        _call_cap = AUTO_MAX_CALLS

    if apply and merged:
        subprocess.run([sys.executable, str(HOOKS_DIR / "brain-reindex.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"\n## lonely 결과 — 통합 {len(merged)}문서 "
          f"/ {sum(len(m) for _, m in merged)}조각 · 보류 {len(held)}주제")
    for slug, m in merged:
        print(f"  ✅ Knowledge/{slug}.md  ({len(m)}조각)")
    for title, m, why in held:
        print(f"  ⏸ {title} ({len(m)}조각) — {str(why)[:90]}")
    write_lonely_receipt(merged, held, len(files))
    return 0


def write_lonely_receipt(merged, held, n_lonely):
    """영수증. 돌았는지 안 돌았는지를 나중에 눈으로 확인할 수 있게."""
    today = datetime.date.today()
    p = BRAIN_VAULT / "_System" / "audit" / f"lonely-{today.isoformat()}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 짝 없는 조각 처리 — {today.isoformat()}", "",
             f"- 대상 {n_lonely}조각",
             f"- 통합 {len(merged)}문서 / {sum(len(m) for _, m in merged)}조각",
             f"- 보류 {len(held)}주제", ""]
    for slug, m in merged:
        lines.append(f"- ✅ `Knowledge/{slug}.md` — {len(m)}조각")
    for title, m, why in held:
        lines.append(f"- ⏸ {title} — {len(m)}조각 · {str(why)[:120]}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- main ------------------------------------------------------------------

# ── 프로젝트 노하우 북으로 보내기 (2026-10-09) ──────────────────────────────
# 검수 결과: 통합이 사건 하나를 페이지 하나로 만들어 Knowledge 가 1,140개의 조각 페이지가 됐고,
# 프로젝트 노하우는 어디에도 모이지 않았다. 프로젝트 폴더에서 나온 조각은 이제 일반 위키로
# 가지 않고 그 프로젝트 노하우 북(Knowledge/Projects/<slug>.md, frontmatter folders:)의
# 받은 편지함(<slug>.inbox.md)으로 간다. 노하우 북에 반영하는 일은 그 프로젝트 세션이 한다 —
# 무인 LLM 이 정본을 직접 고치면 정본이 다시 조각 모음이 된다.
PROJECTS_DIR = BRAIN_VAULT / "Knowledge" / "Projects"
_KIND = {"Mistakes": "주의사항 후보", "Patterns": "노하우 후보", "Decisions": "노하우 후보(결정)"}


def _squash(p):
    """경로 비교용: 드라이브·구분자·기호를 지운다. `x--Work-a` 와 `x:\\Work\\a` 가 같아진다."""
    p = (p or "").strip().strip("\"'").lower()
    p = re.sub(r"^[a-z](?::|--)", "", p)
    return re.sub(r"[^0-9a-z가-힣]", "", p)


def _fm_list(head, key):
    m = re.search(rf"^{key}:[^\n]*\n((?:[ \t]*-[ \t]*.+\r?\n?)+)", head, re.M)
    if not m:
        return []
    return [x.strip().lstrip("-").strip().strip("\"'") for x in m.group(1).splitlines() if x.strip()]


def project_books():
    """[{book, folders(squashed), keywords(lower)}].

    folders 만으로는 부족하다 — 2026-10-09 실측: 여러 프로젝트가 한 작업 폴더(cwd)를
    같이 쓰면 폴더로는 가를 수 없다. 그래서 프로젝트 핵심어(keywords:)로도 가린다."""
    out = []
    if not PROJECTS_DIR.is_dir():
        return out
    for f in sorted(PROJECTS_DIR.glob("*.md")):
        if f.name.endswith(".inbox.md"):
            continue
        head = f.read_text(encoding="utf-8", errors="ignore")[:4000]
        folders = [d for d in (_squash(x) for x in _fm_list(head, "folders")) if d]
        kws = [k.lower() for k in _fm_list(head, "keywords") if len(k) >= 2]
        if folders or kws:
            out.append({"book": f, "folders": sorted(folders, key=len, reverse=True), "keywords": kws})
    return out


# 핵심어는 그 프로젝트에만 나오는 고유한 말로 고른다(노하우 북 keywords:). 그래서 하나면 충분하다 —
# 2개 기준은 조각이 짧아 실제 게임 조각 6개를 하나도 못 잡았다 (2026-10-09).
ROUTE_MIN_KEYWORDS = 1


def book_for_fragment(fp, books):
    try:
        text = (BRAIN_VAULT / fp).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = _PROJ_RE.search(text[:900])
    c = _squash(m.group(1)) if m else ""
    best, best_len = None, 0
    for b in books:
        for d in b["folders"]:
            if c and (c == d or c.startswith(d)) and len(d) > best_len:
                best, best_len = b["book"], len(d)
    if best:
        return best
    low = text.lower()
    scored = [(sum(1 for k in b["keywords"] if k in low), b["book"]) for b in books]
    scored = [x for x in scored if x[0] >= ROUTE_MIN_KEYWORDS]
    return max(scored, key=lambda x: x[0])[1] if scored else None


def route_to_projects(apply=True):
    """프로젝트 폴더에서 나온 살아 있는 조각 → 그 노하우 북의 받은 편지함 + 조각 보관. 반환: 옮긴 수."""
    books = project_books()
    if not books:
        return 0
    year = str(datetime.date.today().year)
    moved = 0
    for cat in JOURNAL:
        for f in sorted((BRAIN_VAULT / cat).glob("*.md")):
            if f.name.startswith("_"):
                continue
            fp = f"{cat}/{f.name}"
            book = book_for_fragment(fp, books)
            if not book:
                continue
            body = fragment_body(fp)
            title = re.search(r"^#\s+(.+)$", body, re.M)
            title = title.group(1).strip() if title else f.stem
            body = re.sub(r"^#\s+.+\n", "", body, count=1).strip()
            if len(body) > 1200:
                body = body[:1200].rstrip() + " …"
            inbox = book.with_name(book.stem + ".inbox.md")
            entry = (f"\n## {f.stem[:10]} · {_KIND.get(cat, cat)} · {title}\n\n{body}\n\n"
                     f"출처: `{cat}/archived/{year}/{f.name}`\n")
            print(f"  → {book.stem}: {fp}", file=sys.stderr)
            if apply:
                if not inbox.exists():
                    inbox.write_text(
                        f"---\ntitle: {book.stem} 노하우 북 — 받은 편지함\ntype: knowhow-inbox\n"
                        f"project: {book.stem}\n---\n\n"
                        f"# 받은 편지함 — [[{book.stem}]] 에 반영할 후보\n\n"
                        "이 프로젝트 폴더의 세션이 남긴 조각이 자동으로 모인다. 그 프로젝트 세션이 읽고:\n"
                        "- 다시 쓸 방법·기준이면 → 노하우 북의 해당 절에 녹여 넣는다\n"
                        "- 하지 말 것·함정이면 → 해당 절 끝 «주의»에 한 줄로\n"
                        "- 한 일·진행 상태면 → 버린다(지식이 아니다)\n"
                        "반영하거나 버린 항목은 이 파일에서 지운다.\n", encoding="utf-8")
                with inbox.open("a", encoding="utf-8") as fh:
                    fh.write(entry)
                d = BRAIN_VAULT / cat / "archived" / year
                d.mkdir(parents=True, exist_ok=True)
                f.rename(d / f.name)
            moved += 1
    if moved and apply:
        subprocess.run([sys.executable, str(HOOKS_DIR / "brain-reindex.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
    return moved


def main():
    ap = argparse.ArgumentParser(description="brain LLM-wiki consolidation")
    ap.add_argument("--threshold", type=float, default=0.60)
    ap.add_argument("--category", help="restrict to one journal category")
    ap.add_argument("--min-size", type=int, default=2)
    ap.add_argument("--ingest", type=int, metavar="N",
                    help="consolidate cluster N (1-based)")
    ap.add_argument("--apply", action="store_true",
                    help="with --ingest: write+archive+reindex if quality passes")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--queue", action="store_true",
                    help="cluster only (LLM 0회) and write the pending queue "
                         "for in-session processing")
    ap.add_argument("--weekly", action="store_true",
                    help="scheduled hybrid: small load -> unattended --auto, "
                         "backlog -> capped --auto on the refined queue, "
                         "remainder -> --queue")
    ap.add_argument("--auto", action="store_true",
                    help="unattended quality-gated consolidation of all clusters")
    ap.add_argument("--lonely", action="store_true",
                    help="fragments with no pair: triage -> theme assign -> "
                         "narrow merge (2026-09-01)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --auto/--lonely: preview without writing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--route-projects", action="store_true",
                    help="project-folder fragments -> Knowledge/Projects/<slug>.inbox.md "
                         "(with --dry-run: list only) (2026-10-09)")
    args = ap.parse_args()

    if args.route_projects:
        n = route_to_projects(apply=not args.dry_run)
        print(f"프로젝트 받은 편지함으로 {'보낼' if args.dry_run else '보낸'} 조각: {n}")
        return 0

    if not INDEX_PATH.exists():
        print(f"index missing: {INDEX_PATH}", file=sys.stderr)
        return 2

    cats = (args.category,) if args.category else JOURNAL
    if args.weekly:
        # 프로젝트 조각은 일반 위키로 묶기 전에 그 프로젝트 노하우 북으로 먼저 보낸다
        route_to_projects(apply=True)
    conn = open_db(create=False)
    vecs, clusters = detect_clusters(conn, cats, args.threshold, args.min_size)

    if args.weekly:
        return do_weekly(conn, vecs, clusters, args.effort, args.threshold)

    if args.queue:
        return do_queue(vecs, clusters, args.threshold)

    if args.lonely:
        return do_lonely(conn, vecs, clusters, args.effort,
                         apply=not args.dry_run)

    if args.auto:
        return do_auto(conn, vecs, clusters, args.dry_run, args.effort)

    if args.ingest is not None:
        if not (1 <= args.ingest <= len(clusters)):
            print(f"cluster {args.ingest} 없음 (1..{len(clusters)})", file=sys.stderr)
            return 2
        return do_ingest(conn, clusters[args.ingest - 1], args.apply, args.effort)

    # detect mode
    singletons = len(vecs) - sum(len(c) for c in clusters)
    payload = []
    for members in clusters:
        common = set.intersection(*[slug_tokens(m) for m in members])
        tagc = Counter(t for m in members for t in file_tags(m))
        payload.append({"size": len(members), "common_slug": sorted(common),
                        "top_tags": [t for t, _ in tagc.most_common(4)],
                        "files": sorted(members)})
    if args.json:
        print(json.dumps({"threshold": args.threshold, "journal_files": len(vecs),
                          "clusters": payload, "singletons": singletons},
                         ensure_ascii=False, indent=2))
        return 0
    print("## brain-consolidate: 통합 후보 주제군 (저널 layer)")
    print(f"cosine>={args.threshold}, 저널 {len(vecs)}파일 중 "
          f"{len(vecs)-singletons}개가 {len(clusters)}개 클러스터로 묶임 "
          f"(단독 {singletons}개)\n")
    for i, c in enumerate(payload, 1):
        label = ", ".join(c["common_slug"]) or ", ".join(c["top_tags"]) or "(라벨 없음)"
        print(f"[{i}] {c['size']}개 — 공통: {label}")
        for f in c["files"]:
            print(f"    - {f}")
        print()
    print("무인 통합: python brain-consolidate.py --auto   (미리보기 --dry-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
