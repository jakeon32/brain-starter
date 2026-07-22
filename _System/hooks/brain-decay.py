#!/usr/bin/env python3
"""
brain-decay: Ebbinghaus-style forgetting sweep over the journal layer.

Model (MemoryBank, arXiv 2305.10250, adapted):
    R = exp(-t / (S * SCALE_DAYS))
      t = days since last recall (fallback: creation date)
      S = strength (starts 1; brain-search bumps +1 per recall)

  R < THRESHOLD  ->  move journal to <category>/archived/<year>/
                     (excluded from the search index; NEVER deleted)
  archived + >EXPIRY_DAYS with no recall -> listed in
                     _System/audit/expiry-queue.md (human review; no auto-delete)

Scope: Decisions/Mistakes/Patterns only. Knowledge/ is the semantic layer and
is decay-exempt (Zep episode/semantic split). Memory/ and Sessions/ untouched.

Runs weekly after brain-consolidate --auto (which invokes this with --apply),
so clusterable journals converge into Knowledge BEFORE decay archives them.
MIN_AGE_DAYS additionally protects fresh journals from the first sweep.

Usage:
  python brain-decay.py            # preview (dry-run)
  python brain-decay.py --apply    # move files + write receipt + reindex
"""
import argparse
import datetime
import math
import os
import re
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))
_lib = __import__("_brain_lib")
BRAIN_VAULT = _lib.BRAIN_VAULT

JOURNAL = ("Decisions", "Mistakes", "Patterns")
SCALE_DAYS = float(os.environ.get("BRAIN_DECAY_SCALE_DAYS", "30"))
THRESHOLD = float(os.environ.get("BRAIN_DECAY_THRESHOLD", "0.3"))
MIN_AGE_DAYS = int(os.environ.get("BRAIN_DECAY_MIN_AGE_DAYS", "21"))
EXPIRY_DAYS = int(os.environ.get("BRAIN_DECAY_EXPIRY_DAYS", "180"))

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
FM_KEY_RE = re.compile(r"^([A-Za-z_]+):\s*'?\"?([^'\"\n]*)'?\"?\s*$")


def parse_frontmatter(path: Path) -> dict:
    """Cheap line-based frontmatter read: only flat `key: value` lines."""
    fm = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fm
    if not text.startswith("---"):
        return fm
    for line in text.split("\n")[1:60]:
        if line.strip() == "---":
            break
        m = FM_KEY_RE.match(line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    return fm


def parse_date(s: str | None):
    if not s:
        return None
    m = DATE_RE.match(s.strip())
    if not m:
        return None
    try:
        return datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def file_base_date(path: Path, fm: dict):
    """Creation date: frontmatter `date:` > filename prefix > mtime."""
    d = parse_date(fm.get("date")) or parse_date(path.name)
    if d:
        return d
    return datetime.date.fromtimestamp(path.stat().st_mtime)


def retention(path: Path, fm: dict, today: datetime.date):
    created = file_base_date(path, fm)
    last = parse_date(fm.get("last_recalled")) or created
    try:
        strength = max(1, int(fm.get("strength", 1)))
    except (TypeError, ValueError):
        strength = 1
    t = max(0, (today - last).days)
    r = math.exp(-t / (strength * SCALE_DAYS))
    age = (today - created).days
    return r, t, strength, age


def sweep(apply: bool):
    today = datetime.date.today()
    moved, kept, protected = [], 0, 0
    for cat in JOURNAL:
        folder = BRAIN_VAULT / cat
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md")):
            if f.name.startswith("_"):
                continue
            fm = parse_frontmatter(f)
            r, t, s, age = retention(f, fm, today)
            if age < MIN_AGE_DAYS:
                protected += 1
                continue
            if r >= THRESHOLD:
                kept += 1
                continue
            year = str(file_base_date(f, fm).year)
            dest_dir = folder / "archived" / year
            dest = dest_dir / f.name
            if apply:
                dest_dir.mkdir(parents=True, exist_ok=True)
                i = 2
                while dest.exists():
                    dest = dest_dir / f"{f.stem}-{i}{f.suffix}"
                    i += 1
                f.rename(dest)
            moved.append((cat, f.name, r, t, s))
    return moved, kept, protected


def expiry_queue(today: datetime.date) -> list[str]:
    """Archived journals with no recall for EXPIRY_DAYS -> review list."""
    rows = []
    for cat in JOURNAL:
        arch = BRAIN_VAULT / cat / "archived"
        if not arch.is_dir():
            continue
        for f in sorted(arch.rglob("*.md")):
            fm = parse_frontmatter(f)
            last = (parse_date(fm.get("last_recalled"))
                    or file_base_date(f, fm))
            idle = (today - last).days
            if idle >= EXPIRY_DAYS:
                rows.append(f"- `{f.relative_to(BRAIN_VAULT)}` — "
                            f"{idle}일 무회상")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Ebbinghaus decay sweep")
    ap.add_argument("--apply", action="store_true",
                    help="actually move files (default: preview)")
    args = ap.parse_args()
    today = datetime.date.today()

    moved, kept, protected = sweep(args.apply)

    mode = "APPLY" if args.apply else "preview"
    lines = [f"# decay 영수증 ({mode}) {today.isoformat()}",
             f"파라미터: scale={SCALE_DAYS}d threshold={THRESHOLD} "
             f"min_age={MIN_AGE_DAYS}d",
             f"아카이브 {len(moved)} / 유지 {kept} / 신규보호 {protected}", ""]
    for cat, name, r, t, s in moved[:400]:
        lines.append(f"- 📦 {cat}/{name}  (R={r:.2f}, {t}일 무회상, S={s})")
    if len(moved) > 400:
        lines.append(f"- … 외 {len(moved) - 400}개 (전체는 파일 이동 기록 참조)")

    q = expiry_queue(today)
    receipt = "\n".join(lines)
    print(receipt)

    if args.apply:
        audit = BRAIN_VAULT / "_System" / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        yr, wk, _ = today.isocalendar()
        (audit / f"decay-{yr}-W{wk:02d}.md").write_text(
            receipt + "\n", encoding="utf-8")
        # expiry queue is a full rewrite each run (idempotent review list)
        (audit / "expiry-queue.md").write_text(
            f"# 만료 후보 큐 (수동 리뷰 전용 — 자동 삭제 없음)\n"
            f"갱신: {today.isoformat()} / 기준: 아카이브 후 "
            f"{EXPIRY_DAYS}일 무회상\n\n"
            + ("\n".join(q) + "\n" if q else "(없음)\n"),
            encoding="utf-8")
        if moved:
            subprocess.run(
                [sys.executable, str(HOOKS_DIR / "brain-reindex.py")],
                capture_output=True, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
        print(f"\naudit: _System/audit/decay-{yr}-W{wk:02d}.md "
              f"/ expiry-queue.md ({len(q)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
