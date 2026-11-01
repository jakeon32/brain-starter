#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brain-status: 실물에서 뽑는 브레인 현황판.

개요(한국어): 손으로 적은 숫자는 반드시 낡는다. 이 스크립트는 어떤 값도
문서에서 읽지 않고 파일시스템·색인·예약작업·영수증에서 직접 잰다. 문서와
실제가 어긋나면 그 자리에서 경고한다.

  python brain-status.py           요약
  python brain-status.py --full    구간 분포까지
"""
from __future__ import annotations
import argparse, collections, datetime, os, re, sqlite3, subprocess, sys, io
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

VAULT = Path(__file__).resolve().parents[2]
if not (VAULT / "_System").is_dir():
    # 배포본(~/.claude/hooks)에서 돌면 위 추정이 틀린다 — 환경변수 먼저, 그다음 기본 위치.
    _cands = (os.environ.get("BRAIN_VAULT"), os.environ.get("CLAUDE_DISTILL_VAULT"),
              str(Path.home() / "brain"))
    VAULT = next((Path(c) for c in _cands if c and (Path(c) / "_System").is_dir()),
                 Path.home() / "brain")
DATA = Path.home() / ".claude" / "data"
INDEX, USAGE = DATA / "brain-index.sqlite", DATA / "brain-usage.sqlite"
JOURNAL = ("Decisions", "Mistakes", "Patterns")
W = 62


def hdr(t): print(f"\n{'─' * W}\n {t}\n{'─' * W}")


def live(cat):
    return [p for p in (VAULT / cat).glob("2*.md")]


def scale():
    hdr("규모")
    rows = []
    for c in JOURNAL:
        arc = len(list((VAULT / c).glob("archived/**/*.md")))
        rows.append((c, len(live(c)), arc))
    kn = len([p for p in (VAULT / "Knowledge").glob("*.md")
              if not p.name.startswith("_")])
    tot_live = sum(r[1] for r in rows)
    for c, l, a in rows:
        print(f"  {c:<10} 미처리 {l:>4}   처리됨 {a:>4}")
    print(f"  {'합계':<9} 미처리 {tot_live:>4}")
    print(f"\n  Knowledge(완성된 지식) {kn}")
    return tot_live, kn


def backlog_age():
    hdr("밀린 조각의 나이")
    c = collections.Counter()
    for cat in JOURNAL:
        for p in live(cat):
            m = re.match(r"(\d{4}-\d{2})", p.name)
            if m:
                c[m.group(1)] += 1
    today = datetime.date.today()
    for k in sorted(c):
        y, mo = map(int, k.split("-"))
        age = (today.year - y) * 12 + today.month - mo
        flag = "  ← 오래됨" if age >= 1 else ""
        print(f"  {k}: {c[k]:>4}개{flag}")


def usage_stats(kn):
    hdr("완성된 지식이 실제로 쓰이는가")
    if not USAGE.exists():
        print("  사용 기록 없음 (brain-search가 아직 안 돌았거나 계기판 미설치)")
        return
    con = sqlite3.connect(USAGE)
    n = con.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    k_used = con.execute("SELECT COUNT(DISTINCT path) FROM usage "
                         "WHERE category='Knowledge'").fetchone()[0]
    first = con.execute("SELECT MIN(ts) FROM usage").fetchone()[0]
    print(f"  기록 시작: {first or '-'}   총 조회 {n}건")
    print(f"  한 번이라도 쓰인 지식: {k_used} / {kn} "
          f"({k_used / kn * 100:.1f}%)" if kn else "")
    top = con.execute("""SELECT path, COUNT(*) n FROM usage
                         WHERE category='Knowledge' GROUP BY path
                         ORDER BY n DESC LIMIT 5""").fetchall()
    if top:
        print("  많이 쓰인 지식:")
        for p, v in top:
            print(f"    {v:>3}회  {Path(p).name[:52]}")
    if (datetime.date.today() - datetime.date.fromisoformat(
            (first or "2000-01-01")[:10])).days < 14:
        print("  ※ 2주는 모여야 판단 근거가 됩니다")


def schedules():
    hdr("예약 작업 (실제 등록값)")
    if os.name != "nt":
        print("  Windows 작업 스케줄러만 읽는다 — 이 OS에서는 crontab -l / launchctl list 로 확인")
        return
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-ScheduledTask | Where-Object {$_.TaskName -match 'brain'} |"
             " ForEach-Object { $i=Get-ScheduledTaskInfo $_.TaskName;"
             " \"$($_.TaskName)|$($_.State)|\" +"
             " ($_.Actions | ForEach-Object {\"$($_.Execute) $($_.Arguments)\"}) +"
             " \"|$($i.LastRunTime)|$($i.LastTaskResult)\" }"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace").stdout
    except Exception as e:
        print(f"  조회 실패: {e!r}")
        return
    for ln in (l for l in out.splitlines() if "|" in l):
        name, state, action, last, rc = (ln.split("|") + [""] * 5)[:5]
        arg = re.sub(r".*\\", "", action.strip())[:46]
        mark = "정지" if state.strip() == "Disabled" else state.strip()
        print(f"  {name:<26} {mark:<9} {arg}")
        print(f"  {'':<26} 최근실행 {last.strip()[:19] or '-'}  결과 {rc.strip()}")


def receipts():
    hdr("최근 통합 결과 (영수증)")
    # 주차 영수증만. consolidate-paused.md 같은 운영 문서가 glob 에 걸려
    # 영수증 행세를 하던 것을 막는다 (2026-09-01).
    aud = VAULT / "_System" / "audit"
    fs = sorted(f for f in aud.glob("consolidate-*.md") if f.stem[13:14].isdigit())[-4:]
    fs += sorted(aud.glob("lonely-*.md"))[-2:]
    if not fs:
        print("  영수증 없음")
        return
    for f in fs:
        body = f.read_text(encoding="utf-8", errors="ignore").splitlines()[:8]
        line = next((l.lstrip("- ") for l in body if "통합" in l), "")
        print(f"  {f.stem.replace('consolidate-', ''):<14} {line[:70]}")


def hook_drift(hits):
    """정본(_System/hooks)과 실제 도는 배포본(~/.claude/hooks)의 어긋남.

    문서를 고쳐도 배포본이 옛것이면 동작은 안 바뀐다. 이 조합이 실제로
    "고쳤는데 왜 그대로냐"를 만든다."""
    src = VAULT / "_System" / "hooks"
    dep = Path.home() / ".claude" / "hooks"
    if not (src.is_dir() and dep.is_dir()):
        return
    for f in sorted(src.glob("*.py")):
        t = dep / f.name
        if not t.exists():
            hits.append(f"{f.name}: 정본에 있으나 배포되지 않았다 (installer 재실행 필요)")
        elif f.read_bytes() != t.read_bytes():
            hits.append(f"{f.name}: 정본과 배포본의 내용이 다르다 (고친 쪽이 안 돌고 있을 수 있다)")


def wiring_drift(hits):
    """settings.json·예약작업이 가리키는 스크립트가 실제로 있는가."""
    import json
    st = Path.home() / ".claude" / "settings.json"
    if not st.exists():
        return
    try:
        d = json.loads(st.read_text(encoding="utf-8"))
    except Exception:
        return
    for ev, groups in (d.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                # command 한 필드에 "실행기경로 스크립트경로"가 같이 오는 형태가
                # 있어 통째로 검사하면 전부 오탐이 된다. 공백으로 쪼개 .py 토큰만 본다.
                raw = [h.get("command", "")] + list(h.get("args") or [])
                toks = [t for chunk in raw for t in str(chunk).split()]
                # brain-hook-launch.py 는 두 번째 인자를 "경로가 아니라 이름"으로
                # 받아 hooks 디렉터리에서 찾는다. 이름만 온 토큰은 그 기준으로 본다.
                hooks_dir = Path.home() / ".claude" / "hooks"
                for tok in toks:
                    if not tok.lower().endswith(".py"):
                        continue
                    cand = Path(tok)
                    if not cand.is_absolute() and "/" not in tok and "\\" not in tok:
                        cand = hooks_dir / tok
                    if not cand.exists():
                        hits.append(f"{ev} 훅이 없는 파일을 가리킨다: {tok}")


def doc_link_drift(hits):
    """architecture.md 가 링크한 문서가 실제로 있는가."""
    import re
    arch = VAULT / "_System" / "architecture.md"
    if not arch.exists():
        return  # 조립도 문서는 선택 사항이다
    for m in re.finditer(r"\]\(([^)#]+\.md)\)", arch.read_text(encoding="utf-8")):
        t = (arch.parent / m.group(1)).resolve()
        if not t.exists():
            hits.append(f"architecture.md 가 없는 문서를 링크한다: {m.group(1)}")


def drift():
    hdr("문서와 실제가 어긋난 곳")
    hits = []
    hook_drift(hits)
    wiring_drift(hits)
    doc_link_drift(hits)
    doc = VAULT / "_System" / "consolidate.md"
    if doc.exists():
        t = doc.read_text(encoding="utf-8", errors="ignore")
        try:
            if os.name != "nt":
                raise OSError("no Task Scheduler")
            ps = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-ScheduledTask -TaskName 'brain-consolidate-weekly')."
                 "Actions.Arguments"], capture_output=True, text=True,
                timeout=40, encoding="utf-8", errors="replace").stdout
        except Exception:
            ps = ""
        if "--auto" in ps and "--weekly" in t:
            hits.append("consolidate.md는 --weekly를 정본으로 적었는데 "
                        "예약작업은 --auto로 등록돼 있다")
    for c in JOURNAL + ("Knowledge",):
        idx = VAULT / c / "_index.md"
        if idx.exists():
            t = idx.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"(\d{2,4})\s*(?:개|files|문서)", t):
                claimed = int(m.group(1))
                real = len(list((VAULT / c).glob("*.md")))
                if abs(claimed - real) > max(5, real * 0.1):
                    hits.append(f"{c}/_index.md는 {claimed}개라 적었는데 "
                                f"실제 {real}개")
                break
    print("  " + ("\n  ".join(hits) if hits else "발견된 불일치 없음"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    print(f"\n{'=' * W}\n brain 현황  {datetime.datetime.now():%Y-%m-%d %H:%M}"
          f"   ({VAULT})\n{'=' * W}")
    tot, kn = scale()
    backlog_age()
    usage_stats(kn)
    schedules()
    receipts()
    drift()
    print(f"\n{'=' * W}\n 이 값은 전부 실물에서 잰 것이다. 문서에서 읽지 않는다.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
