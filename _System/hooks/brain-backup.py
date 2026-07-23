#!/usr/bin/env python3
"""brain-backup: dated archive of the vault onto a different physical disk.

Why this exists: the journal (Decisions/Mistakes/Patterns/Sessions) is
per-device runtime and gitignored on purpose, so `git push` carries only the
handful of files tagged `scope: shared`. Everything else — 900+ notes, months
of distilled sessions — lives on exactly one disk and nowhere else. A disk
failure loses all of it silently. This closes that hole (2026-07-23).

What it deliberately does NOT do:
  - It does not sync. Backup is one-way; nothing here ever writes into the
    vault. Sameness across devices is a different goal with different costs,
    and conflating the two is what drags in merge/conflict handling nobody
    needs for durability.
  - It does not back up the search index. That is derivable from the notes
    (rebuild with brain-reindex.py --full); the files are the truth.
  - It does not delete anything in the vault. Old *archives* beyond KEEP are
    pruned, and that is the only deletion this script performs.

Usage:
    python brain-backup.py            # archive + prune + write receipt
    python brain-backup.py --dry-run  # report what would happen, write nothing

Env:
    BRAIN_BACKUP_DEST   where archives go (default: a brain-backup folder on
                        C: for Windows, ~/brain-backup otherwise)
    BRAIN_BACKUP_KEEP   how many dated archives to retain (default 14)
    CLAUDE_DISTILL_VAULT / BRAIN_VAULT   the vault to archive

Exit 0 = archived (possibly with warnings), 1 = failed.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import zipfile
from pathlib import Path

if sys.platform == "win32":
    # A legacy console codepage raises UnicodeEncodeError on the first non-ASCII
    # character and takes the whole run with it — the same way it killed the
    # watchdog mid-alarm (2026-07-22).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VAULT = Path(os.environ.get("BRAIN_VAULT")
             or os.environ.get("CLAUDE_DISTILL_VAULT")
             or (Path.home() / "brain"))

# Home is only a starting point: it is very often the same disk as the vault,
# in which case the archive protects against an accidental delete but not a
# disk failure. same_volume() below says so out loud; point BRAIN_BACKUP_DEST
# at a genuinely separate disk to get the durability this exists for.
_default_dest = Path.home() / "brain-backup"
DEST = Path(os.environ.get("BRAIN_BACKUP_DEST", str(_default_dest)))
KEEP = int(os.environ.get("BRAIN_BACKUP_KEEP", "14"))

# .git is already on the remote; __pycache__ is build output. Everything else,
# including archived/, is backed up — archived notes are history, not garbage.
SKIP_DIRS = {".git", "__pycache__", ".obsidian", "node_modules"}
SKIP_SUFFIX = {".pyc", ".sqlite", ".sqlite-wal", ".sqlite-shm"}

# Files worth keeping that live outside the vault. The global CLAUDE.md is the
# one that wires everything else together — it names which canonical docs to
# read — and it is hand-written, so nothing regenerates it. settings.json and
# the deployed hooks are left out on purpose: the installer rebuilds both.
#
# Honest limit: these sit on the same volume the archive is written to, so they
# are covered against an accidental edit or delete (the common case) but not
# against that disk failing. Mirroring the archive to a second disk would fix
# that; it was judged not worth the complexity for one file.
EXTRA_FILES = [(Path.home() / ".claude" / "CLAUDE.md", "_claude-home/CLAUDE.md")]

# Shapes of real credentials. The vault is meant to hold work notes, not keys;
# a hit means a key got distilled out of a transcript verbatim and is now
# sitting in plain text (one Google API key had been doing exactly that since
# 2026-05). Backing up is still the right move — losing the notes would be
# worse — so this reports rather than blocks.
SECRET_RE = re.compile(
    r"AIza[A-Za-z0-9_\-]{30,}"
    r"|xox[baprs]-[A-Za-z0-9]{8,}-[A-Za-z0-9]{8,}"
    r"|ntn_[A-Za-z0-9]{30,}"
    r"|ghp_[A-Za-z0-9]{30,}"
    r"|sk-[A-Za-z0-9]{32,}"
    r"|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"
)


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in SKIP_SUFFIX:
                continue
            yield p


def scan_secrets(files: list[Path]) -> list[str]:
    hits = []
    for p in files:
        if p.suffix.lower() != ".md":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in SECRET_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            hits.append(f"{p.relative_to(VAULT).as_posix()}:{line}")
    return hits


def same_volume(a: Path, b: Path) -> bool:
    """On Windows a backup onto the vault's own drive protects against nothing."""
    if sys.platform != "win32":
        try:
            return a.stat().st_dev == b.stat().st_dev
        except OSError:
            return False
    return a.resolve().drive.upper() == b.resolve().drive.upper()


def prune(receipt: list[str], dry_run: bool) -> None:
    archives = sorted(DEST.glob("brain-????-??-??.zip"))
    doomed = archives[:-KEEP] if len(archives) > KEEP else []
    for old in doomed:
        if dry_run:
            receipt.append(f"- would prune {old.name}")
            continue
        try:
            old.unlink()
            receipt.append(f"- pruned {old.name}")
        except OSError as e:
            receipt.append(f"- prune failed for {old.name}: {e!r}")
    remaining = len(sorted(DEST.glob("brain-????-??-??.zip"))) if DEST.is_dir() else 0
    receipt.append(f"- retained {remaining} of max {KEEP}")


def write_receipt(lines: list[str], ok: bool) -> None:
    audit = VAULT / "_System" / "audit"
    try:
        audit.mkdir(parents=True, exist_ok=True)
        path = audit / f"backup-{datetime.date.today().isoformat()}.md"
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        head = f"\n## {stamp} — {'ok' if ok else 'FAILED'}\n"
        prev = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(prev + head + "\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"receipt could not be written: {e!r}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    receipt: list[str] = []
    if not VAULT.is_dir():
        receipt.append(f"- vault not found: {VAULT}")
        write_receipt(receipt, ok=False)
        print(f"vault not found: {VAULT}", file=sys.stderr)
        return 1

    receipt.append(f"- vault: {VAULT}")
    receipt.append(f"- destination: {DEST}")

    if same_volume(VAULT, DEST if DEST.exists() else DEST.parent):
        receipt.append("- **WARNING: destination is on the vault's own volume — "
                       "this protects against nothing if that disk fails**")

    files = list(iter_files(VAULT))
    extras = [(src, arc) for src, arc in EXTRA_FILES if src.is_file()]
    total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
    receipt.append(f"- {len(files)} file(s), {total_mb:.1f} MB before compression")
    if extras:
        receipt.append(f"- plus {len(extras)} file(s) from outside the vault: "
                       + ", ".join(a for _, a in extras))
    for src, arc in EXTRA_FILES:
        if not src.is_file():
            receipt.append(f"- note: {arc} not found at {src} — skipped")

    secrets = scan_secrets(files)
    if secrets:
        receipt.append(f"- **WARNING: {len(secrets)} credential-shaped string(s) "
                       f"in the vault — rotate and redact these:**")
        receipt += [f"  - {h}" for h in secrets[:20]]

    archive = DEST / f"brain-{datetime.date.today().isoformat()}.zip"
    if args.dry_run:
        receipt.append(f"- [dry-run] would write {archive}")
        prune(receipt, dry_run=True)
        write_receipt(receipt, ok=True)
        print("\n".join(receipt))
        return 0

    try:
        DEST.mkdir(parents=True, exist_ok=True)
        tmp = archive.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in files:
                z.write(f, f.relative_to(VAULT).as_posix())
            for src, arcname in extras:
                z.write(src, arcname)
        # A backup nobody has opened is not a backup. Read the archive back and
        # confirm every entry's CRC before claiming success.
        with zipfile.ZipFile(tmp) as z:
            bad = z.testzip()
            if bad is not None:
                raise OSError(f"archive verification failed at {bad}")
            written = len(z.namelist())
        expected = len(files) + len(extras)
        if written != expected:
            raise OSError(f"archive holds {written} entries, expected {expected}")
        tmp.replace(archive)  # only becomes the dated name once it is complete
    except Exception as e:
        receipt.append(f"- archive failed: {e!r}")
        write_receipt(receipt, ok=False)
        print(f"backup failed: {e!r}", file=sys.stderr)
        return 1

    size_mb = archive.stat().st_size / 1024 / 1024
    receipt.append(f"- wrote {archive.name} ({size_mb:.1f} MB)")
    prune(receipt, dry_run=False)
    write_receipt(receipt, ok=True)
    print("\n".join(receipt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
