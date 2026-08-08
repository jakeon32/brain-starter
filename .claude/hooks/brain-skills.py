#!/usr/bin/env python
"""
brain-skills.py — 반복 작업 감지 + 스킬 초안 생성 (Argo의 '2번 하면 자동 스킬화'를 우리 구조로)

전제: distill이 세션마다 Patterns/Decisions/Mistakes 노트를 쌓고 있고,
      brain-index.sqlite에 그 노트들의 임베딩이 이미 있다.

원리:
  1. 노트를 임베딩 유사도로 군집화한다 → 한 군집 = "같은 종류의 일"
  2. 군집 크기 N = 그 일을 N번 했다는 뜻 (서로 다른 세션에서 나왔을 때만 센다)
  3. 이미 스킬/런북/Knowledge 정본이 있는 주제는 뺀다
  4. 남은 것 = "N번 반복했는데 아직 정본이 없는 일" = 스킬로 만들 후보

Argo와 다른 점: 자동으로 만들어 버리지 않고 **후보로 보고**한다.
잘못 뽑힌 스킬이 다음 세션의 행동을 바꾸면 되돌리기 어렵기 때문이다(초안 생성은 --draft로 명시 호출).

사용:
    python brain-skills.py                    # 반복 주제 리포트 (기본 3회 이상)
    python brain-skills.py --min-count 2      # Argo 기준(2회)으로 보기
    python brain-skills.py --all              # 정본이 이미 있는 주제도 함께
    python brain-skills.py --draft <번호>     # 그 주제로 스킬 초안 생성 (claude CLI 호출)
    python brain-skills.py --json             # 기계 판독용
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# 한국어 Windows 콘솔은 cp949라 em-dash 하나에도 출력이 죽는다 — starter 검증에서 실측.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
# 이 스크립트는 brain 볼트(.claude/hooks/)에 살고 3기기가 git으로 공유한다.
# _brain_lib은 기기마다 설치된 ~/.claude/hooks/ 쪽을 쓴다 — 복제하면 표류한다.
sys.path.append(str(Path.home() / ".claude" / "hooks"))
from _brain_lib import BRAIN_VAULT, INDEX_PATH, open_db  # noqa: E402

# 군집 임계 — 이 값 이상 붙으면 "같은 종류의 일"로 본다.
# 0.55(그래프 기본값)는 너무 느슨해 다른 일까지 묶인다. 실측으로 0.68이 적당.
CLUSTER_SIM = float(os.environ.get("BRAIN_SKILL_SIM", "0.68"))
DEFAULT_MIN_COUNT = 3

# 정본이 이미 있는지 확인할 위치
SKILL_DIRS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "plugins",
]
RUNBOOK_GLOBS = [
    Path.home() / ".claude" / "projects",       # */memory/runbooks/*.md
]
KNOWLEDGE_DIR = BRAIN_VAULT / "Knowledge"


# ---- 노트 로딩 ---------------------------------------------------------------

def load_notes(conn):
    """파일별 문서 벡터 + 메타(카테고리·세션·날짜·제목)."""
    rows = conn.execute(
        "SELECT c.file_path, v.embedding FROM chunks c "
        "JOIN chunks_vec v ON v.rowid = c.id"
    ).fetchall()
    per_file = defaultdict(list)
    for fpath, blob in rows:
        n = len(blob) // 4
        per_file[fpath].append(struct.unpack(f"{n}f", blob))
    notes = {}
    for fpath, vecs in per_file.items():
        dim = len(vecs[0])
        acc = [0.0] * dim
        for v in vecs:
            for i in range(dim):
                acc[i] += v[i]
        nrm = math.sqrt(sum(x * x for x in acc)) or 1.0
        notes[fpath] = [x / nrm for x in acc]
    return notes


FM_SESSION = re.compile(r"^session:\s*(\S+)", re.M)
FM_CWD = re.compile(r"^cwd:\s*(.+)$", re.M)


def note_info(rel_path: str) -> dict:
    p = Path(rel_path)
    parts = p.parts
    category = next((c for c in ("Decisions", "Mistakes", "Patterns", "Knowledge",
                                 "Memory", "Sessions", "Agents") if c in parts), "기타")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)$", p.stem)
    date, slug = (m.group(1), m.group(2)) if m else ("", p.stem)
    full = BRAIN_VAULT / rel_path if not p.is_absolute() else p
    session, cwd, body = "", "", ""
    try:
        text = full.read_text(encoding="utf-8", errors="ignore")
        ms, mc = FM_SESSION.search(text), FM_CWD.search(text)
        session = ms.group(1) if ms else ""
        cwd = mc.group(1).strip() if mc else ""
        body = text.split("---", 2)[-1].strip()
    except OSError:
        pass
    return {"path": rel_path, "category": category, "date": date, "slug": slug,
            "title": slug.replace("-", " "), "session": session, "cwd": cwd,
            "body": body}


# ---- 군집화 -----------------------------------------------------------------

def cluster(notes: dict, sim: float) -> list[list[str]]:
    """그리디 응집 — 가장 유사한 쌍부터 묶는다. 900노트면 O(n^2)로 충분하다."""
    paths = sorted(notes)
    n = len(paths)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        vi = notes[paths[i]]
        for j in range(i + 1, n):
            vj = notes[paths[j]]
            if sum(a * b for a, b in zip(vi, vj)) >= sim:
                union(i, j)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(paths[i])
    return sorted(groups.values(), key=len, reverse=True)


# ---- 정본 존재 확인 -----------------------------------------------------------

def load_existing_canon() -> list[tuple[str, str]]:
    """(이름, 종류) 목록 — 스킬·런북·Knowledge 정본."""
    out = []
    for d in SKILL_DIRS:
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.is_dir():
                out.append((p.name, "스킬"))
    for root in RUNBOOK_GLOBS:
        if not root.exists():
            continue
        for p in root.glob("*/memory/runbooks/*.md"):
            out.append((p.stem, "런북"))
    if KNOWLEDGE_DIR.exists():
        for p in KNOWLEDGE_DIR.rglob("*.md"):
            out.append((p.stem, "Knowledge"))
    return out


def tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9가-힣]+", s.lower()) if len(t) > 2}


def canon_match(cluster_titles: list[str], canon: list[tuple[str, str]]):
    """군집 제목들과 겹치는 정본이 있으면 반환. 토큰 겹침 기반(느슨하게)."""
    ctok = set()
    for t in cluster_titles:
        ctok |= tokens(t)
    best, best_score = None, 0.0
    for name, kind in canon:
        ntok = tokens(name)
        if not ntok:
            continue
        overlap = len(ctok & ntok) / len(ntok)
        if overlap > best_score:
            best, best_score = (name, kind), overlap
    return best if best_score >= 0.5 else None


# ---- 리포트 -----------------------------------------------------------------

def build_report(args):
    conn = open_db(INDEX_PATH, create=False)
    notes = load_notes(conn)
    if not notes:
        return None, "인덱스에 임베딩이 없습니다. brain-reindex.py 를 먼저 실행하세요."

    groups = cluster(notes, args.sim)
    canon = load_existing_canon()
    infos = {p: note_info(p) for p in notes}

    results = []
    for g in groups:
        if len(g) < 2:
            continue
        metas = [infos[p] for p in g]
        # ★같은 세션에서 쏟아진 노트는 1회로 센다 — "반복"은 다른 날/다른 세션에 다시 했을 때다
        sessions = {m["session"] for m in metas if m["session"]}
        repeat = len(sessions) if sessions else len(g)
        if repeat < args.min_count:
            continue
        titles = [m["title"] for m in metas]
        # ★군집이 지나치게 크면 "같은 일"이 아니라 임계가 느슨해 뭉뚱그려진 것이다.
        #   (실측: 81개 세션짜리 군집이 엉뚱한 스킬에 매칭됐다)
        #   스킬 후보로 올리지 않고 임계를 올리라고 안내한다.
        too_broad = repeat > args.max_count
        hit = canon_match(titles, canon)
        cats = defaultdict(int)
        projects = defaultdict(int)
        for m in metas:
            cats[m["category"]] += 1
            if m["cwd"]:
                projects[Path(m["cwd"]).name] += 1
        dates = sorted(m["date"] for m in metas if m["date"])
        results.append({
            "notes": g, "repeat": repeat, "size": len(g),
            "titles": titles, "canon": hit, "too_broad": too_broad,
            "categories": dict(cats), "projects": dict(projects),
            "first": dates[0] if dates else "", "last": dates[-1] if dates else "",
        })
    results.sort(key=lambda r: (-r["repeat"], -r["size"]))
    return results, None


def label_of(r) -> str:
    """군집을 한 줄로 부르는 이름 — 제목들에서 가장 흔한 단어를 뽑는다."""
    cnt = defaultdict(int)
    for t in r["titles"]:
        for w in tokens(t):
            cnt[w] += 1
    top = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:4]
    return " · ".join(w for w, _ in top) or r["titles"][0][:40]


def print_report(results, args):
    fresh = [r for r in results if not r["canon"] and not r["too_broad"]]
    known = [r for r in results if r["canon"] and not r["too_broad"]]
    broad = [r for r in results if r["too_broad"]]
    print(f"반복 작업 감지 — {BRAIN_VAULT}")
    print(f"  군집 임계 {args.sim} · {args.min_count}회 이상 반복만")
    line = f"  스킬 후보 {len(fresh)}건 · 이미 정본 있는 주제 {len(known)}건"
    if broad:
        line += f" · 너무 넓어 제외 {len(broad)}건"
    print(line)
    print()
    if not fresh:
        print("  새로 스킬로 만들 만한 반복 주제가 없습니다.")
    for i, r in enumerate(fresh, 1):
        print(f"[{i}] {label_of(r)}")
        print(f"    {r['repeat']}개 세션에서 반복 · 노트 {r['size']}개 · {r['first']} ~ {r['last']}")
        if r["projects"]:
            pr = ", ".join(f"{k}({v})" for k, v in sorted(r["projects"].items(), key=lambda x: -x[1])[:3])
            print(f"    프로젝트: {pr}")
        for t in r["titles"][:4]:
            print(f"      - {t[:72]}")
        if len(r["titles"]) > 4:
            print(f"      ... 외 {len(r['titles'])-4}건")
        print(f"    → 초안 만들려면: brain-skills.py --draft {i}")
        print()
    if broad:
        for r in broad:
            print(f"  ⚠ '{label_of(r)[:44]}' 는 {r['repeat']}개 세션에 걸쳐 있어 하나의 일로 보기 어렵습니다.")
            print(f"    --sim {min(0.92, args.sim + 0.08):.2f} 로 올려 다시 보세요.")
        print()
    if args.all and known:
        print("  이미 정본이 있는 반복 주제 (참고)")
        for r in known:
            name, kind = r["canon"]
            print(f"    {r['repeat']}회  {label_of(r)[:44]}  →  {kind} '{name}'")


# ---- 초안 생성 ---------------------------------------------------------------

DRAFT_PROMPT = """당신은 이 사용자의 작업 기록에서 재사용 가능한 **스킬 문서**를 만드는 역할입니다.

아래는 서로 다른 {repeat}개 세션에서 반복적으로 나온 같은 종류의 작업 기록입니다.
이걸 다음 번에 같은 일을 할 때 그대로 따라할 수 있는 문서 하나로 정리하세요.

규칙:
- 한국어로 씁니다.
- 맨 위에 언제 이 문서를 꺼내야 하는지(트리거)를 한 줄로 씁니다.
- 실제 기록에 있는 것만 씁니다. 일반론이나 교과서적 설명을 채워 넣지 마세요.
- 반복해서 틀렸던 지점이 있으면 "함정" 항목으로 따로 뺍니다 — 이게 이 문서의 가장 큰 값입니다.
- 명령어·경로는 기록에 있는 그대로 정확히 옮깁니다.
- 기록마다 결론이 다르면 최신 날짜 것을 정본으로 삼고, 바뀐 이유를 한 줄 남깁니다.
- 분량은 실제 내용이 있는 만큼만. 빈 섹션을 만들지 마세요.

--- 기록 시작 ---
{body}
--- 기록 끝 ---

마크다운 문서 본문만 출력하세요. 다른 말은 붙이지 마세요.
"""


def resolve_claude_bin() -> str | None:
    for cand in ("claude", "claude.cmd"):
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, timeout=20)
            if r.returncode == 0:
                return cand
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def cmd_draft(results, args):
    idx = args.draft
    fresh = [r for r in results if not r["canon"] and not r["too_broad"]]
    if idx < 1 or idx > len(fresh):
        print(f"번호는 1~{len(fresh)} 사이여야 합니다.")
        return 1
    r = fresh[idx - 1]
    parts = []
    for p in r["notes"]:
        info = note_info(p)
        parts.append(f"### [{info['category']}] {info['title']} ({info['date']})\n{info['body']}\n")
    body = "\n".join(parts)[:60000]

    claude = resolve_claude_bin()
    if not claude:
        print("claude CLI를 찾지 못했습니다. 아래 프롬프트를 직접 쓰세요.\n")
        print(DRAFT_PROMPT.format(repeat=r["repeat"], body=body[:2000] + "\n...(생략)"))
        return 1

    prompt = DRAFT_PROMPT.format(repeat=r["repeat"], body=body)
    print(f"초안 생성 중 — '{label_of(r)}' (노트 {r['size']}개)...", file=sys.stderr)
    proc = subprocess.run([claude, "-p", prompt], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    if proc.returncode != 0:
        print(f"claude 호출 실패: {proc.stderr[:400]}")
        return 1
    draft = (proc.stdout or "").strip()
    if not draft:
        print("빈 응답을 받았습니다.")
        return 1

    slug = re.sub(r"[^a-z0-9가-힣]+", "-", label_of(r).lower()).strip("-")[:50] or "skill"
    out_dir = Path(args.out or (BRAIN_VAULT / "Knowledge" / "_drafts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y-%m-%d}-{slug}.md"
    header = (
        "---\n"
        "type: knowledge\n"
        "status: draft\n"
        f"date: {datetime.now():%Y-%m-%d}\n"
        "source: brain-skills.py (반복 작업 자동 감지)\n"
        f"repeat_sessions: {r['repeat']}\n"
        f"from_notes: {r['size']}\n"
        "---\n\n"
        f"> ⚠️ **자동 생성 초안입니다.** {r['repeat']}개 세션에서 같은 일이 반복돼 뽑았습니다.\n"
        "> 사람이 검토·수정한 뒤에 정본으로 옮기세요. 근거 노트는 아래에 있습니다.\n\n"
    )
    footer = "\n\n---\n\n## 근거 노트\n\n" + "\n".join(f"- `{p}`" for p in r["notes"]) + "\n"
    out.write_text(header + draft + footer, encoding="utf-8")
    print(f"\n초안 저장: {out}")
    print(f"근거 노트 {r['size']}개 · {r['repeat']}개 세션")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="반복 작업 감지 + 스킬 초안")
    ap.add_argument("--sim", type=float, default=CLUSTER_SIM)
    ap.add_argument("--min-count", type=int, default=DEFAULT_MIN_COUNT)
    ap.add_argument("--max-count", type=int, default=25,
                    help="이보다 많은 세션에 걸친 군집은 '주제가 뭉뚱그려진 것'으로 보고 제외")
    ap.add_argument("--all", action="store_true", help="정본이 이미 있는 주제도 표시")
    ap.add_argument("--draft", type=int, metavar="N", help="N번 주제로 스킬 초안 생성")
    ap.add_argument("--out", metavar="DIR", help="초안 저장 폴더")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not INDEX_PATH.exists():
        print(f"인덱스가 없습니다: {INDEX_PATH}")
        return 1

    results, err = build_report(args)
    if err:
        print(err)
        return 1

    if args.draft:
        return cmd_draft(results, args)
    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "notes"} | {"label": label_of(r)}
                          for r in results], ensure_ascii=False, indent=1))
        return 0
    print_report(results, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
