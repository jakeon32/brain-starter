#!/usr/bin/env python3
"""
SessionStart health check for the brain long-term-memory pipeline.

Why this exists:
  2026-06-08~11 the distill Stop-hook silently failed for 3+ days (stale
  pinned claude path after the npm->native CLI migration). Nothing surfaced
  the failure; the search index went stale with it. This hook makes such
  breakage visible at the next session start instead of weeks later.

Contract (_System/stop-hook.md / _System/search.md, this brain vault):
  - READ-ONLY checks, no LLM calls, target < 1s. mtime + log-tail only.
  - Prints NOTHING when healthy (SessionStart stdout becomes session context;
    silence keeps healthy sessions clean).
  - On problems prints a compact Korean warning block.
  - Side effect: spawns distill-catchup.py DETACHED so missed sessions are
    recovered automatically (deferred recovery, zero loss). Opt out with
    BRAIN_HEALTH_NO_CATCHUP=1.

Checks:
  1. distill.log tail: failures in the last 72h
  2. search index staleness: newest brain .md vs brain-index.sqlite mtime
  3. weekly consolidate receipt missing (> 9 days)
  4. Memory/*.md untouched > 60 days (reference facts going stale)
  5. resume bloat: per-project state files under Memory/resume/ (and the
     session-resume.md index) growing past their caps -- usually knowledge
     leaking into a state file
  6. _index.md catalog drift: tracked _index.md must stay curated prose + a
     _catalog.md pointer, never a hand-written file listing (Knowledge/_index.md
     silently regrew 21 hand-written links by 2026-06-24 despite the catalog
     automation; git diff only surfaces it at commit time)
  7. scope:shared pull (2026-07-13, docs/plans/2026-07-13-refined-layer-
     cross-device-sync-design.md): one deliberate exception to "read-only" --
     `git pull --ff-only` for any Knowledge/Memory content tagged scope:
     shared. Bounded by a timeout so a dead network delays session start by
     at most a few seconds, never hangs it. Silent on success/no-op/offline;
     warns only when the fast-forward genuinely can't apply.
  8. hook deploy drift: _System/hooks/*.py (canonical, git-tracked) vs
     ~/.claude/hooks/*.py (deployed, what actually runs). git pull updates
     only the canonical copy -- a device that never re-runs the installer
     keeps executing stale code with no symptom (2026-08-10: this device ran
     07-20 builds for 3 weeks; the 08-03 distill dedup guard never executed
     once).
  9. orphan sessions: a claude.exe whose parent terminal died -- the window is
     gone but the process (and its ~30 MCP server children) lives on, with no
     way for the user to /exit it. 2026-08-30: four sessions sat 22h and one bg
     session 6 days this way; ListAgents did not even list some of them.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Home-relative fallback (not a device-specific absolute path) — the installer's
# generated launcher always sets CLAUDE_DISTILL_VAULT explicitly; this fallback
# only matters for an ad-hoc invocation with no env var set.
# BRAIN_VAULT first (2026-08-18): _brain_lib.py accepts BRAIN_VAULT *or*
# CLAUDE_DISTILL_VAULT, this file only accepted the latter. An ad-hoc run with
# BRAIN_VAULT set therefore pointed BRAIN at the nonexistent default, and every
# path check reported the vault as empty — it warned "저널 백업 영수증이 하나도
# 없음" while 13 receipts sat in _System/audit/. A diagnostic that lies about
# the thing it diagnoses is worse than no diagnostic. Same contract everywhere.
BRAIN = Path(
    os.environ.get("BRAIN_VAULT")
    or os.environ.get("CLAUDE_DISTILL_VAULT")
    or str(Path.home() / "brain")
)
LOG = Path(os.environ.get(
    "CLAUDE_DISTILL_LOG",
    str(Path.home() / ".claude" / "logs" / "distill.log"),
))
INDEX = Path.home() / ".claude" / "data" / "brain-index.sqlite"
AUDIT = BRAIN / "_System" / "audit"
RUNTIME_DIRS = ["Decisions", "Mistakes", "Patterns", "Sessions",
                "Knowledge", "Memory", "Agents"]

FAIL_WINDOW_H = 72
INDEX_STALE_H = 24
CONSOLIDATE_MAX_DAYS = 9
CONSOLIDATE_NEVER_RUN_MIN_FILES = 15
# 저널은 gitignore된 per-device 런타임이라 push가 안 나른다 — 이 디스크가 죽으면
# 그대로 사라진다. 백업이 조용히 멈추면 "있다고 믿는" 상태가 되어 더 나쁘다.
BACKUP_MAX_DAYS = 8
MEMORY_STALE_DAYS = 60
# Caps raised 2026-07-15 (were 35KB/12). The 2026-06-12 caps were set right
# after the 132KB/42-section append regression, when far fewer projects were
# live. With 13 genuinely active projects each carrying 5-7 entry points, the
# old caps fired every session on a *healthy* file -- and a warning that is
# always on trains you to skim past the block that carries the real ones.
# 2026-07-29: 한 파일에 전 프로젝트를 담던 구조를 프로젝트당 파일로 분리했다.
# 크기 = 프로젝트 수 x 섹션 크기였던 탓에, 덮어쓰기 규칙을 지켜도 2~3주마다
# 한계를 넘었다(6/12 132KB · 7/2 49KB · 7/15 62KB · 7/29 68KB — 네 번 슬림).
# 이제 검사 단위는 파일 하나가 아니라 Memory/resume/*.md 각각이다.
# 2026-08-22: 한계를 **바이트에서 글자로** 바꿨다. UTF-8 에서 한글은 글자당 3바이트라
# 같은 정보량이라도 한글로 쓰면 1.5~1.7배로 부푼다 — resume 23개 전부 한글 비율
# 23~41% 라 모두 같은 페널티를 받고 있었다(실측: transportation-portal 11.6KB =
# 7,302자 · 영문이었다면 7.3KB 로 한계 안). 이 한계의 목적은 "세션 시작에 읽는 양"
# 이므로 재는 대상은 바이트가 아니라 분량이어야 하고, 글자 수가 언어 중립적이다.
# 인덱스는 프로젝트당 한 줄이라 **프로젝트 수에 비례해 정상적으로 자란다**(한 줄 ~180자).
# 23개인 지금이 4,371자 — 4,000 으로 잡았더니 건강한 파일에 매 세션 경고가 떴다.
# 늘 켜져 있는 경고는 읽는 사람을 훈련시켜 진짜 경고까지 건너뛰게 만든다(위 2026-07-15 교훈).
RESUME_INDEX_MAX_CHARS = 5500    # 프로젝트 ~28개까지 여유. 넘으면 본문이 새어든 것
RESUME_INDEX_MAX_SECTIONS = 6    # 새 인덱스의 ## 는 3개. 늘면 옛 단일파일로 회귀한 것
RESUME_FILE_MAX_CHARS = 5000     # 프로젝트 파일 권장 ~4,000자 — 넘으면 대개 지식이 섞인 것
# hot.md는 *직전 세션* 캐시 — 정상 크기는 몇 KB다. 2026-07-18에 160KB(3주치
# 블록 누적)로 발견됐는데 resume만 검사하고 hot은 검사 항목이 아니어서 조용히
# 컸다. 같은 append-회귀 병이므로 같은 방식으로 감시한다.
HOT_MAX_KB = 40
INDEX_LINK_MAX = 5  # _index.md is prose + a _catalog pointer; more file links = drift
SYNC_PULL_TIMEOUT_S = 10  # bounds the one non-read-only check; see check_shared_sync

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def newest_md_mtime() -> float:
    newest = 0.0
    for d in RUNTIME_DIRS:
        folder = BRAIN / d
        if not folder.is_dir():
            continue
        for f in folder.glob("*.md"):
            m = f.stat().st_mtime
            if m > newest:
                newest = m
    return newest


def check_distill_failures(warnings: list) -> None:
    """Warn only when failures are the *latest* state: a failure newer than the
    last success means the pipeline is currently broken. Old failures already
    followed by successful distills are history, not a live problem."""
    if not LOG.exists():
        return
    cutoff = datetime.now() - timedelta(hours=FAIL_WINDOW_H)
    fails, last_fail_line = 0, ""
    last_fail_ts = last_ok_ts = None
    try:
        tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        return
    for line in tail:
        m = TS_RE.match(line)
        ts = None
        if m:
            try:
                ts = datetime.fromisoformat(m.group(1))
            except ValueError:
                pass
        if "wrote" in line or "marked skip" in line:
            last_ok_ts = ts or last_ok_ts
            continue
        if "failed" in line or "not found" in line:
            if ts and ts < cutoff:
                continue
            fails += 1
            last_fail_line = line
            last_fail_ts = ts or last_fail_ts
    currently_broken = fails and (
        last_ok_ts is None or last_fail_ts is None or last_fail_ts > last_ok_ts
    )
    if currently_broken:
        warnings.append(
            f"distill 실패 {fails}건 (최근 {FAIL_WINDOW_H}h), 마지막 이벤트가 실패 — "
            f"파이프라인 점검 필요. 마지막: {last_fail_line.strip()[:160]}\n"
            f"  → 로그: {LOG}"
        )


def check_index_staleness(warnings: list) -> None:
    content = newest_md_mtime()
    if not content:
        return
    if not INDEX.exists():
        warnings.append(f"brain-search 인덱스 없음: {INDEX} → brain-reindex.py --full 필요")
        return
    lag_h = (content - INDEX.stat().st_mtime) / 3600
    if lag_h > INDEX_STALE_H:
        warnings.append(
            f"brain-search 인덱스가 콘텐츠보다 {lag_h:.0f}시간 뒤처짐 "
            f"→ python ~/.claude/hooks/brain-reindex.py 권장"
        )


def check_consolidate_receipt(warnings: list) -> None:
    """Two distinct failure modes, both surfaced: a schedule that regressed
    (had receipts, now stale) and a schedule that was never set up at all
    (fresh/new device — no receipts, no warning otherwise possible, so a
    device could accumulate journal content forever without ever converging
    to Knowledge/ and nobody would know)."""
    receipts = []
    if AUDIT.is_dir():
        receipts = sorted(AUDIT.glob("consolidate-*.md"),
                          key=lambda f: f.stat().st_mtime)
    # 의도적 정지(consolidate-paused.md)는 고장이 아니다. 다만 조용히 잊히면
    # 대기열이 영원히 안 줄므로, 경고 대신 상태를 알리고 한 달이 넘으면 다시
    # 강하게 경고한다. (2026-08-28)
    # 재개한 뒤에도 파일은 기록으로 남는다(status: resolved). 그때 여기서 return 하면
    # «49일째 정지» 오경보가 뜨고 아래 영수증 나이 점검까지 건너뛴다. (2026-10-20)
    paused = AUDIT / "consolidate-paused.md"
    if paused.exists() and not re.search(
            r"^status:\s*resolved\s*$",
            paused.read_text(encoding="utf-8", errors="replace")[:500], re.M):
        pd = (datetime.now().timestamp() - paused.stat().st_mtime) / 86400
        if pd > 30:
            warnings.append(
                f"주간 consolidate가 {pd:.0f}일째 의도적 정지 중 — 재개 조건을 "
                f"검토할 때 (_System/audit/consolidate-paused.md)")
        return

    if receipts:
        age_d = (datetime.now().timestamp() - receipts[-1].stat().st_mtime) / 86400
        if age_d > CONSOLIDATE_MAX_DAYS:
            warnings.append(
                f"주간 consolidate 영수증이 {age_d:.0f}일 전 ({receipts[-1].name}) — "
                f"대기열 스케줄 미스 또는 세션 처리가 밀렸을 수 있음 → "
                f"python ~/.claude/hooks/brain-consolidate.py --queue 후 "
                f"세션에서 처리 (_System/consolidate.md §Session processing)"
            )
        return
    journal_files = sum(
        1 for d in ("Decisions", "Mistakes", "Patterns")
        for f in (BRAIN / d).glob("*.md") if f.name != "_index.md"
    )
    if journal_files >= CONSOLIDATE_NEVER_RUN_MIN_FILES:
        warnings.append(
            f"저널({journal_files}개 파일)이 쌓였는데 consolidate가 한 번도 실행된 적 없음. "
            + _consolidate_schedule_diagnosis()
            + " → python ~/.claude/hooks/brain-consolidate.py --queue 후 "
            "세션에서 처리 (_System/consolidate.md §Session processing)"
        )


def check_backup_receipt(warnings: list) -> None:
    """저널 백업이 최근에 돌았는지. 안 돌면 이 디스크가 유일본이 된다.

    영수증이 아예 없는 경우와 오래된 경우를 나눈다 — 전자는 "설정한 적 없음",
    후자는 "돌다가 멈춤"이고 대응이 다르다. brain-sync의 영수증 방식과 같다."""
    receipts = []
    if AUDIT.is_dir():
        receipts = sorted(AUDIT.glob("backup-*.md"), key=lambda f: f.stat().st_mtime)
    if not receipts:
        warnings.append(
            "저널 백업 영수증이 하나도 없음 — 저널은 gitignore라 원격에 안 올라간다. "
            "이 디스크가 죽으면 그대로 유실 → python ~/.claude/hooks/brain-backup.py "
            "실행 + 예약작업 등록"
        )
        return
    age_d = (datetime.now().timestamp() - receipts[-1].stat().st_mtime) / 86400
    if age_d > BACKUP_MAX_DAYS:
        warnings.append(
            f"저널 백업이 {age_d:.0f}일째 안 돌았음 ({receipts[-1].name}) — "
            f"예약작업이 멈췄을 수 있다 → python ~/.claude/hooks/brain-backup.py"
        )


def check_consolidate_queue(warnings: list) -> None:
    """통합 대기열(consolidate-queue.json)에 남은 클러스터를 세션에 알린다.

    2026-07-19 구조 전환: 주간 잡은 클러스터링(--queue, LLM 0회)까지만 하고,
    통합 자체는 세션 안의 claude가 직접 한다 (claude -p 자식 스폰 없음 —
    한 기기에서 무인 --auto가 자식 프로세스를 폭증시켜 구독 한도를 태운 사건이
    계기). 조각은 대기열 생성 후 이동/아카이브될 수 있으므로 살아있는 조각이
    2개 이상인 클러스터만 센다."""
    q = AUDIT / "consolidate-queue.json"
    if not q.exists():
        return
    try:
        data = json.loads(q.read_text(encoding="utf-8"))
    except Exception:
        warnings.append(f"통합 대기열 파일이 손상됨({q.name}) — "
                        "python ~/.claude/hooks/brain-consolidate.py --queue 재생성")
        return
    alive = []
    for c in data.get("clusters", []):
        files = [f for f in c.get("files", []) if (BRAIN / f).exists()]
        if len(files) >= 2:
            alive.append(len(files))
    if alive:
        warnings.append(
            f"통합 대기 {len(alive)}클러스터/{sum(alive)}조각 "
            f"({data.get('generated', '?')} 생성) — 이 세션에서 직접 처리 권장: "
            f"_System/consolidate.md §Session processing"
        )


def _consolidate_schedule_diagnosis() -> str:
    """스케줄 상태를 *추정하지 말고 조회*해서 붙인다.

    2026-07-18: 이 경고가 "주간 스케줄이 설정 안 됐을 가능성"이라고 단정조로 출력했는데
    실제로는 등록돼 있었고, 진짜 원인은 작업 설정(StartWhenAvailable=False +
    DisallowStartIfOnBatteries=True)이라 노트북이 일요일 새벽에 깨어 있고 전원까지
    꽂혀 있어야만 도는 상태였다. 틀린 추정을 사실처럼 내면 엉뚱한 데를 파게 된다 —
    경고 문구가 원인을 지목할 거면 근거를 실제로 확인하고 지목한다.
    """
    if os.name != "nt":
        return "반복 설정 확인: _System/consolidate.md §Schedule"
    try:
        xml = subprocess.run(
            ["schtasks", "/query", "/tn", "brain-consolidate-weekly", "/xml"],
            capture_output=True, text=True, timeout=10,
            # under pythonw this console query would otherwise flash a window
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return "스케줄 상태 조회 실패 — 반복 설정: _System/consolidate.md §Schedule"
    if xml.returncode != 0:
        return ("이 기기에 주간 스케줄(brain-consolidate-weekly)이 **없음**(조회 확인) "
                "— 등록법: _System/consolidate.md §Schedule")
    out = xml.stdout
    blockers = []
    if "<StartWhenAvailable>true</StartWhenAvailable>" not in out:
        blockers.append("StartWhenAvailable=False(예약 시각에 꺼져 있으면 소급 실행 안 함)")
    if "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" not in out:
        blockers.append("DisallowStartIfOnBatteries=True(배터리면 실행 안 함)")
    if blockers:
        return ("스케줄은 등록돼 있으나 설정이 실행을 막는다: " + " / ".join(blockers)
                + " — 둘 다 해제해야 실제로 돈다")
    return "스케줄은 등록·설정 정상 — 다음 실행 시각까지 기다리면 된다"


# 끝난 일의 *기록*은 오래된 게 정상이다 — 갱신 대상이 아니라서 경고에 뜨면 오탐이다.
# (2026-07-18: triage-log·quartz-setup-guide 같은 1회성 기록이 매 세션 경고에 실려
#  나왔다. 상시 오탐은 health 출력 자체를 건너뛰게 만들고, 그러면 진짜 경고를 놓친다
#  — 같은 날 distill이 몇 주간 무증상으로 죽어 있던 사고와 같은 계열의 위험.)
STALENESS_EXEMPT_STATUS = {"confirmed", "deployed", "done", "archived", "retired"}
_FM_STATUS_RE = re.compile(r"(?m)^status:\s*['\"]?([a-z-]+)", re.I)
_FM_EXEMPT_RE = re.compile(r"(?m)^staleness:\s*exempt\b", re.I)


def _staleness_exempt(path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:800]
    except Exception:
        return False
    if _FM_EXEMPT_RE.search(head):        # 명시 옵트아웃
        return True
    m = _FM_STATUS_RE.search(head)
    return bool(m and m.group(1).lower() in STALENESS_EXEMPT_STATUS)


def check_memory_staleness(warnings: list) -> None:
    mem = BRAIN / "Memory"
    if not mem.is_dir():
        return
    cutoff = datetime.now().timestamp() - MEMORY_STALE_DAYS * 86400
    stale = [f.name for f in mem.glob("*.md")
             if f.name != "_index.md" and f.stat().st_mtime < cutoff
             and not _staleness_exempt(f)]
    if stale:
        shown = ", ".join(sorted(stale)[:5])
        more = f" 외 {len(stale)-5}개" if len(stale) > 5 else ""
        warnings.append(
            f"Memory {MEMORY_STALE_DAYS}일+ 미갱신: {shown}{more} — 내용이 여전히 맞는지 검토 권장"
        )


def check_resume_bloat(warnings: list) -> None:
    """resume 상태는 프로젝트당 한 파일(Memory/resume/<project>.md)이고
    Memory/session-resume.md는 그 인덱스다 (session-start-protocol.md).

    2026-07-29 개편 전에는 한 파일에 전 프로젝트가 들어 있었다. 그 구조에서는
    파일 크기 = 프로젝트 수 x 섹션 크기라, 덮어쓰기 규칙을 지켜도 프로젝트가
    늘면 반드시 한계를 넘었다(네 번 슬림했다). 이제 부푸는 단위가 파일별이므로
    **어느 파일인지 이름을 대고** 경고한다 — 자기 프로젝트가 아니면 그 세션은
    넘어가면 된다. 이름을 안 대면 무관한 세션이 매번 경고를 받고, 늘 켜져 있는
    경고는 읽는 사람을 훈련시켜 진짜 경고까지 건너뛰게 만든다."""
    index = BRAIN / "Memory" / "session-resume.md"
    resume_dir = BRAIN / "Memory" / "resume"

    if index.exists():
        try:
            text = index.read_text(encoding="utf-8", errors="replace")
            chars = len(text)
            sections = sum(1 for ln in text.splitlines() if ln.startswith("## "))
        except OSError:
            chars, sections = 0, 0
        if sections > RESUME_INDEX_MAX_SECTIONS or chars > RESUME_INDEX_MAX_CHARS:
            warnings.append(
                f"session-resume.md(인덱스) 비대화: {chars:,}자 / ## {sections}개 "
                f"(한계 {RESUME_INDEX_MAX_CHARS:,}자·{RESUME_INDEX_MAX_SECTIONS}개) — "
                f"먼저 의심할 것은 프로젝트 내용 유입이 아니라 **정본 복제**다: "
                f"규칙 본문(사용법·재개 단계·크기 기준)은 session-start-protocol.md"
                f"가 정본이니 링크로 대체하고, 표의 진입점 칸에 새어든 "
                f"완료 이력·함정은 Memory/resume/<프로젝트>.md로 되돌린다 "
                f"(2026-08-13 실측: 8.8KB 중 3.5KB가 규칙 재복제였다)"
            )

    if not resume_dir.is_dir():
        return
    def _chars(f):
        try:
            return len(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return 0
    try:
        fat = sorted(
            (
                (n, f.name)
                for f, n in ((f, _chars(f)) for f in resume_dir.glob("*.md"))
                if n > RESUME_FILE_MAX_CHARS
            ),
            reverse=True,
        )
    except OSError:
        return
    if not fat:
        return
    shown = " · ".join(f"{name} {n:,}자" for n, name in fat[:4])
    more = f" 외 {len(fat) - 4}개" if len(fat) > 4 else ""
    warnings.append(
        f"resume 파일 비대화 ({RESUME_FILE_MAX_CHARS:,}자 초과): {shown}{more} — "
        f"진입점·재개 식별자·그 프로젝트 고유 함정만 남기고, 재사용 지식은 "
        f"Knowledge/로 옮긴 뒤 [[링크]]만 둔다. 끝난 프로젝트면 "
        f"resume/_done/으로 이동이 먼저. "
        f"자기 프로젝트 파일이 아니면 이번 세션은 넘어가도 된다"
    )


def check_hot_bloat(warnings: list) -> None:
    """hot.md가 캐시 크기를 넘으면 경고. 세션랩이 새 블록을 얹기만 하고 지난
    블록을 안 내리면 커진다 — resume와 동일한 회귀."""
    hot = BRAIN / "Memory" / "hot.md"
    if not hot.exists():
        return
    size_kb = hot.stat().st_size / 1024
    if size_kb > HOT_MAX_KB:
        warnings.append(
            f"hot.md 비대화: {size_kb:.0f}KB (한계 {HOT_MAX_KB}KB) — hot은 직전 세션 "
            f"캐시다. 새 블록을 얹을 때 지난 세션 블록을 Sessions/archived/로 내릴 것 "
            f"(살릴 것은 미결·이월 항목뿐)"
        )


# List-item link to a sibling .md, e.g. "- [title](slug.md) — desc". A burst of
# these in a tracked _index.md means a hand-written listing crept back in.
_INDEX_LINK_RE = re.compile(r"^\s*[-*+]\s*\[[^\]]+\]\(([^)]+\.md)\)")


def check_index_drift(warnings: list) -> None:
    """Tracked _index.md must stay curated prose + a _catalog.md pointer — the
    file listing is brain-catalog.py's job. A pile of list-item .md links means
    the 2026-06-11 catalog-automation contract regressed to hand-maintenance
    (Knowledge/_index.md silently regrew 21 links by 2026-06-24). git diff only
    catches this at commit time and runtime sessions rarely commit the skeleton,
    so surface it at session start instead."""
    offenders = []
    for d in RUNTIME_DIRS:
        idx = BRAIN / d / "_index.md"
        if not idx.exists():
            continue
        try:
            lines = idx.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        n = 0
        for ln in lines:
            m = _INDEX_LINK_RE.match(ln)
            if not m:
                continue
            target = m.group(1).lower()
            # the curated _catalog.md / _index.md pointers are legitimate
            if target.endswith("_catalog.md") or target.endswith("_index.md"):
                continue
            n += 1
        if n > INDEX_LINK_MAX:
            offenders.append(f"{d}/_index.md ({n}개)")
    if offenders:
        warnings.append(
            f"_index.md 수동 파일목록 회귀: {', '.join(offenders)} — _index.md는 "
            f"curated prose + _catalog.md 포인터만 유지(목록은 brain-catalog.py "
            f"자동생성). 손으로 쓴 링크 목록 제거 (health.md §brain-catalog)"
        )


def check_hook_drift(warnings: list) -> None:
    """정본(_System/hooks/*.py)과 배포본(~/.claude/hooks/*.py)의 바이트 비교.

    훅은 git pull로 설치되지 않는다 — pull은 정본만 갱신하고, 설치기를 다시
    돌리기 전까지 기기는 낡은 코드를 증상 없이 계속 돌린다. 2026-08-10: 이
    기기의 배포본이 07-20판인 채 3주를 돌아 08-03 distill 중복 방지 가드가
    한 번도 실행되지 않았고, brain-sync 하위 폴더 수정도 정본에만 있었다.
    설치기 재실행을 사람이 기억하는 구조는 반드시 밀린다 — 세션 시작마다
    기계가 비교한다. 설치기의 산출물(launcher 등 정본에 없는 파일)은 비교
    대상이 아니다: 정본 쪽 파일 목록만 순회한다."""
    src_dir = BRAIN / "_System" / "hooks"
    dst_dir = Path(os.environ.get("BRAIN_HOOKS_DIR",
                                  str(Path.home() / ".claude" / "hooks")))
    if not src_dir.is_dir() or not dst_dir.is_dir():
        return
    stale = []
    for src in sorted(src_dir.glob("*.py")):
        dst = dst_dir / src.name
        try:
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                stale.append(src.name)
        except OSError:
            continue
    if stale:
        shown = ", ".join(stale[:4])
        more = f" 외 {len(stale)-4}개" if len(stale) > 4 else ""
        warnings.append(
            f"훅 배포본이 정본과 다름: {shown}{more} — git pull은 훅을 설치하지 "
            f"않는다 (정본 _System/hooks/ ≠ 실행본 {dst_dir}). 설치기 재실행 → "
            f"powershell -File _System\\install-stop-hook.ps1 "
            f"(macOS/Linux: ./_System/install-stop-hook.sh)"
        )


def check_git_lock(warnings: list) -> None:
    """남은 .git/index.lock 을 잡는다. 2026-10-13 커밋 도중 끊긴 git 이 잠금 파일을
    남겨 brain-sync 커밋이 일주일 동안 전부 실패했는데, 어떤 점검도 말하지 않았다.
    정상 커밋은 1초 안에 지우므로 10분 넘은 잠금만 남은 것으로 본다."""
    lock = BRAIN / ".git" / "index.lock"
    try:
        age_m = (datetime.now().timestamp() - lock.stat().st_mtime) / 60
    except OSError:
        return
    if age_m > 10:
        warnings.append(
            f"git 잠금 파일이 {age_m / 60:.0f}시간째 남아 brain 커밋·동기화가 멈춰 있다 — "
            f"돌고 있는 git 이 없는지 확인하고 지울 것: {lock}")


def check_shared_sync(warnings: list) -> None:
    """git pull --ff-only for scope:shared Knowledge/Memory content. The one
    check here that isn't purely read-only (docs/plans/2026-07-13-refined-
    layer-cross-device-sync-design.md) -- bounded by SYNC_PULL_TIMEOUT_S so a
    dead network can't hang session start, only delay it briefly. Silent on
    success/no-op/offline (brain-sync.py itself already tells those apart);
    only a genuine divergence surfaces here."""
    sync = Path(__file__).parent / "brain-sync.py"
    if not sync.exists():
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(sync), "--pull"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=SYNC_PULL_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return  # timeout or any other failure -- treat like offline, stay silent
    if proc.returncode == 0:
        return
    err = (proc.stderr or "").strip()

    # exit 1은 두 가지다: cmd_pull이 refs를 실측해 진짜 divergence를 보고한 1과,
    # brain-sync 자체가 미처리 예외로 죽어 파이썬이 낸 1. 둘을 섞으면 도구 고장을
    # "수동 git pull 필요"로 보고하게 되고, 시킨 대로 pull해도 갈라진 것이 없다 —
    # 헛수고 한 번이 진짜 경고를 무시하도록 길들인다 (2026-08-30: 다른 세션의
    # SessionEnd와 겹쳐 죽은 brain-sync의 잘린 Traceback이 경고에 그대로 실렸다).
    # cmd_pull의 분류 자체는 이미 옳다(2026-08-06) — offline과 일시적 실패는 0으로
    # 삼키고 ahead/behind가 둘 다 있을 때만 1을 낸다. 여기서 가릴 것은 그 1이
    # 계약대로 나온 것인지 예외로 나온 것인지뿐이다.
    if "Traceback (most recent call last)" in err:
        tail = [l for l in err.splitlines() if l.strip()]
        warnings.append(
            "공유 지식 동기 점검(brain-sync --pull)이 예외로 실패 — 갈라짐 여부는 "
            "판정되지 않았다(수동 pull이 답이라는 뜻이 아니다). 다른 세션의 "
            "SessionEnd와 겹친 일시적 실패면 다음 세션에서 사라진다. 남아 있으면 "
            "`python ~/.claude/hooks/brain-sync.py --pull`로 재현할 것 → "
            + (tail[-1][:160] if tail else "(stderr 없음)")
        )
        return

    warnings.append(
        "공유 지식(Knowledge/Memory scope:shared)이 원격과 갈라져 자동 병합 불가 "
        "→ brain 저장소에서 수동 `git pull` 필요 → "
        + (err.splitlines()[0][:200] if err else "(stderr 없음)")
    )


def _process_parent_map() -> dict:
    """{pid: ppid} 스냅샷. 외부 프로세스를 띄우지 않는 경로를 우선한다 —
    이 훅은 세션 시작마다 돌고 1초 예산을 쓴다. 윈도는 ctypes로 커널
    스냅샷을 직접 읽고(수 ms), 그 외에서만 ps를 한 번 부른다."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _PE32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snap == wintypes.HANDLE(-1).value:
            return {}
        out = {}
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = k32.Process32First(snap, ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = int(e.th32ParentProcessID)
                ok = k32.Process32Next(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return out
    try:
        raw = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return {}
    out = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
    return out


def check_orphan_sessions(warnings: list) -> None:
    """창이 닫혔는데 살아남은 claude 세션(고아)을 세션 시작에 드러낸다.

    2026-08-30: 터미널 창을 닫아도 claude.exe가 함께 정리되지 않아 세션
    4개가 22시간, bg 하나가 6일을 idle로 떠 있었다. 붙은 창이 없으니
    사람이 /exit으로 끌 수 없고, 세션마다 MCP 서버 자식을 30개씩 그대로
    물고 있었다. ListAgents에도 일부는 아예 잡히지 않아 살아있다는 사실을
    알 방법이 없었다 — 그래서 기계가 세션 시작마다 대신 본다.

    판정은 직계 부모 생존이다: 창이 닫히면 부모(cmd.exe / 터미널 호스트)가
    먼저 죽고 claude.exe만 남아 PPID가 사라진다. MainWindowHandle은 쓸 수
    없다 — 콘솔 앱이라 정상 세션도 0으로 나온다(2026-08-30 실측). 자기
    자신의 조상 사슬은 제외한다: 지금 이 세션의 부모는 살아있지만, 훅이
    자기를 고아로 신고하는 사고를 구조적으로 막는다."""
    sess_dir = Path.home() / ".claude" / "sessions"
    if not sess_dir.is_dir():
        return
    parents = _process_parent_map()
    if not parents:
        return

    # 자기 조상 사슬 — 이 세션과 그 부모들은 판정 대상에서 뺀다
    mine = set()
    cur = os.getpid()
    for _ in range(12):
        if cur in mine or cur not in parents:
            break
        mine.add(cur)
        cur = parents[cur]

    now_ms = datetime.now().timestamp() * 1000
    orphans = []
    for f in sorted(sess_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = d.get("pid")
        if not isinstance(pid, int) or pid in mine:
            continue
        if pid not in parents:
            continue  # 이미 죽은 등록 — 남은 파일은 곧 정리된다
        if parents[pid] in parents:
            continue  # 부모가 살아있다 = 창에 붙어 있다
        seen = d.get("statusUpdatedAt") or d.get("updatedAt") or 0
        idle_h = int((now_ms - seen) / 3_600_000) if seen else -1
        orphans.append((pid, d.get("name") or "?", d.get("kind") or "?",
                        idle_h, d.get("cwd") or "?"))

    if not orphans:
        return
    orphans.sort(key=lambda o: -o[3])
    shown = " · ".join(
        f"{n}({p}, {k}, {'idle ' + str(h) + 'h' if h >= 0 else 'idle ?'})"
        for p, n, k, h, _ in orphans[:4]
    )
    more = f" 외 {len(orphans)-4}개" if len(orphans) > 4 else ""
    pids = " ".join(str(o[0]) for o in orphans)
    warnings.append(
        f"고아 세션 {len(orphans)}개: {shown}{more} — 창이 닫혔는데 프로세스만 "
        f"남았다. 붙은 터미널이 없어 /exit으로 끌 수 없고, 세션마다 MCP 서버 "
        f"자식을 수십 개 물고 있다. 쓸 것이면 `claude --resume <session-id>`로 "
        f"되살리고, 아니면 트리째 종료 → "
        + ("taskkill /PID <pid> /T /F" if os.name == "nt" else "kill -- -<pid>")
        + f" (대상: {pids})"
    )


def spawn_background_maintenance() -> None:
    """Detached: distill-catchup (recover missed sessions) + brain-reindex
    (incremental self-heal: re-indexes any note whose own reindex died — e.g. a
    concurrent-spawn lock collision; catchup can't catch this because it sees the
    session as already distilled) + brain-catalog (refresh _catalog.md listings).
    Never blocks session start."""
    if os.environ.get("BRAIN_HEALTH_NO_CATCHUP"):
        return
    env = os.environ.copy()
    env.setdefault("CLAUDE_DISTILL_VAULT", str(BRAIN))
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    for name in ("distill-catchup.py", "brain-reindex.py", "brain-catalog.py"):
        script = Path(__file__).parent / name
        if not script.exists():
            continue
        try:
            subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags,
                close_fds=True, env=env,
            )
        except Exception:
            pass  # maintenance is best-effort; never break session start


def check_recall_wiring(warnings: list) -> None:
    """회상 훅이 «실제로 돌고 있는가»를 본다 — 파일이 깔렸는지가 아니라.

    2026-10-09 검수: brain-recall-inject/read 는 09-28 에 배포됐지만 settings.json 에
    연결되지 않았고, 런처는 볼트를 다른 드라이브로 옮기기 전의 옛 경로를 가리켰다.
    11일 동안 주입·열람이 0건이었는데 이 점검은 «배포본이 다름»만 말했다.
    그래서 세 가지를 잰다: ① 설정에 연결돼 있나 ② 런처의 볼트 경로가 있나
    ③ 최근 3일 안에 주입이 한 번이라도 있었나(세션이 있었는데 0이면 죽은 것)."""
    home = Path.home() / ".claude"
    try:
        cfg = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return
    hooks = json.dumps(cfg.get("hooks", {}), ensure_ascii=False)
    missing = [n for n in ("brain-recall-inject.py", "brain-recall-read.py") if n not in hooks]
    if missing:
        warnings.append(f"회상 훅이 settings.json 에 연결돼 있지 않음: {', '.join(missing)} — 자동 회상이 "
                        f"돌지 않는다. 설치기 재실행 → powershell -File _System\\install-stop-hook.ps1")
        return
    launcher = home / "hooks" / "brain-hook-launch.py"
    try:
        m = re.search(r'CLAUDE_DISTILL_VAULT",\s*r?"([^"]+)"', launcher.read_text(encoding="utf-8"))
        if m and not Path(m.group(1)).is_dir():
            warnings.append(f"훅 런처가 없는 볼트를 가리킴: {m.group(1)} — 훅이 조용히 실패한다. 설치기 재실행")
            return
    except OSError:
        pass
    db = home / "data" / "brain-usage.sqlite"
    if not db.exists():
        return
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        last = con.execute("SELECT max(ts) FROM injections").fetchone()[0]
        con.close()
    except Exception:
        return
    if not last:
        return
    try:
        age = (datetime.now() - datetime.fromisoformat(last)).days
    except ValueError:
        return
    if age >= 3:
        warnings.append(f"자동 회상 주입이 {age}일째 0건 (마지막 {last[:16]}) — 훅이 연결돼 있는데도 "
                        f"멈췄을 수 있다. ~/.claude/logs 와 brain-usage.sqlite injections 표 확인")


def main() -> None:
    # Recursion guard: distill's `claude -p` child sessions also fire
    # SessionStart hooks — stay silent and spawn nothing there.
    if os.environ.get("CLAUDE_DISTILL_CHILD"):
        return

    # Hook stdout is decoded as UTF-8 by Claude Code; without this Windows
    # Python writes cp949 and the Korean warnings arrive as mojibake.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    warnings: list = []
    check_distill_failures(warnings)
    check_index_staleness(warnings)
    check_consolidate_receipt(warnings)
    check_consolidate_queue(warnings)
    check_backup_receipt(warnings)
    check_memory_staleness(warnings)
    check_resume_bloat(warnings)
    check_hot_bloat(warnings)
    check_index_drift(warnings)
    check_git_lock(warnings)
    check_shared_sync(warnings)
    # after check_shared_sync on purpose: its pull may have just updated the
    # canonical hooks, and this ordering surfaces the drift in the same session
    check_hook_drift(warnings)
    check_recall_wiring(warnings)
    check_orphan_sessions(warnings)
    spawn_background_maintenance()

    if warnings:
        print("[brain-health] 장기기억 파이프라인 경고:")
        for w in warnings:
            print(f"- {w}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # health check must never break session start
