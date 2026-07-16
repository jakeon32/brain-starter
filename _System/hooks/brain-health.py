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
  5. Memory/session-resume.md bloat: size/section caps (append-instead-of-
     overwrite regression guard; the file hit 132KB/42 sections by 2026-06-12)
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
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Home-relative fallback (not a device-specific absolute path) — the installer's
# generated launcher always sets CLAUDE_DISTILL_VAULT explicitly; this fallback
# only matters for an ad-hoc invocation with no env var set.
BRAIN = Path(os.environ.get("CLAUDE_DISTILL_VAULT", str(Path.home() / "brain")))
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
MEMORY_STALE_DAYS = 60
# Caps raised 2026-07-15 (were 35KB/12). The 2026-06-12 caps were set right
# after the 132KB/42-section append regression, when far fewer projects were
# live. With 13 genuinely active projects each carrying 5-7 entry points, the
# old caps fired every session on a *healthy* file -- and a warning that is
# always on trains you to skim past the block that carries the real ones.
RESUME_MAX_KB = 55
RESUME_MAX_SECTIONS = 18
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
    if receipts:
        age_d = (datetime.now().timestamp() - receipts[-1].stat().st_mtime) / 86400
        if age_d > CONSOLIDATE_MAX_DAYS:
            warnings.append(
                f"주간 consolidate 영수증이 {age_d:.0f}일 전 ({receipts[-1].name}) — "
                f"스케줄(brain-consolidate-weekly, 일요일 04:00, 로그인 시에만 실행)이 "
                f"미스됐을 수 있음 → 수동: python ~/.claude/hooks/brain-consolidate.py --auto"
            )
        return
    journal_files = sum(
        1 for d in ("Decisions", "Mistakes", "Patterns")
        for f in (BRAIN / d).glob("*.md") if f.name != "_index.md"
    )
    if journal_files >= CONSOLIDATE_NEVER_RUN_MIN_FILES:
        warnings.append(
            f"저널({journal_files}개 파일)이 쌓였는데 consolidate가 한 번도 실행된 적 없음 "
            f"— 이 기기에 주간 스케줄이 설정 안 됐을 가능성 (install-stop-hook은 이 스케줄을 "
            f"자동 등록하지 않음) → 수동 1회: python ~/.claude/hooks/brain-consolidate.py --auto "
            f"/ 반복 설정: _System/consolidate.md §Schedule"
        )


def check_memory_staleness(warnings: list) -> None:
    mem = BRAIN / "Memory"
    if not mem.is_dir():
        return
    cutoff = datetime.now().timestamp() - MEMORY_STALE_DAYS * 86400
    stale = [f.name for f in mem.glob("*.md")
             if f.name != "_index.md" and f.stat().st_mtime < cutoff]
    if stale:
        shown = ", ".join(sorted(stale)[:5])
        more = f" 외 {len(stale)-5}개" if len(stale) > 5 else ""
        warnings.append(
            f"Memory {MEMORY_STALE_DAYS}일+ 미갱신: {shown}{more} — 내용이 여전히 맞는지 검토 권장"
        )


def check_resume_bloat(warnings: list) -> None:
    """session-resume.md must stay one-section-per-project, overwritten each
    wrap (recording-protocol §세션 연속성).

    Two different failures trip this, and they need different fixes, so name the
    one that actually happened. Blaming append when the section count is fine
    sends the reader looking for duplicate sections that are not there — and a
    warning that misdiagnoses is how a reader learns to skim past the ones that
    do not (2026-07-16: fired at 14 sections, limit 18, and said "append is
    back")."""
    resume = BRAIN / "Memory" / "session-resume.md"
    if not resume.exists():
        return
    size_kb = resume.stat().st_size / 1024
    try:
        sections = sum(
            1 for ln in resume.read_text(encoding="utf-8", errors="replace")
            .splitlines() if ln.startswith("## ")
        )
    except OSError:
        return
    over_sections = sections > RESUME_MAX_SECTIONS
    over_size = size_kb > RESUME_MAX_KB
    if not (over_sections or over_size):
        return
    if over_sections:
        diagnosis = (
            "섹션 수 초과. 프로젝트당 1섹션 덮어쓰기가 append로 돌아갔거나 "
            "끝난 프로젝트 섹션이 남아 있다. 끝난 섹션 삭제가 1순위"
        )
    else:
        diagnosis = (
            "섹션 수는 정상이고 append도 아니다. 섹션당 내용이 길어진 것. "
            "끝난 프로젝트 섹션을 지우고, 남길 섹션은 '다음 진입점' 위주로 압축"
        )
    warnings.append(
        f"session-resume.md 비대화: {size_kb:.0f}KB / 섹션 {sections}개 "
        f"(한계 {RESUME_MAX_KB}KB·{RESUME_MAX_SECTIONS}개) — {diagnosis}. "
        f"일지 가치가 있는 과거 섹션은 Sessions/로 이관 "
        f"(recording-protocol §세션 연속성)"
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
        )
    except Exception:
        return  # timeout or any other failure -- treat like offline, stay silent
    if proc.returncode != 0:
        warnings.append(
            "공유 지식(Knowledge/Memory scope:shared)이 원격과 갈라져 자동 병합 불가 "
            "→ brain 저장소에서 수동 `git pull` 필요\n"
            f"  → {proc.stderr.strip()[:200]}"
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
    check_memory_staleness(warnings)
    check_resume_bloat(warnings)
    check_index_drift(warnings)
    check_shared_sync(warnings)
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
