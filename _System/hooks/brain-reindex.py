#!/usr/bin/env python3
"""
brain-reindex: build/refresh the brain search index at ~/.claude/data/brain-index.sqlite.

Usage:
  python brain-reindex.py              incremental: index new/changed files
  python brain-reindex.py --full       rebuild from scratch (drops + reindexes)
  python brain-reindex.py --file PATH  reindex a single file (used by distill hook)
  python brain-reindex.py --stats      print index health, no changes
  python brain-reindex.py --dry        report what WOULD be (re)indexed

Design:
  Markdown is the source of truth. Index is derivable; deleting it loses nothing.
  Each file becomes 1..N chunks (paragraph-based, ~500 chars). Each chunk gets
  one row in `chunks`, mirrored into `chunks_fts` (BM25) and `chunks_vec` (cosine).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow `from _brain_lib import ...` when invoked from any cwd
sys.path.insert(0, str(Path(__file__).parent))

from _brain_lib import (  # noqa: E402
    BRAIN_VAULT, INDEX_PATH, EMBED_MODEL,
    open_db, iter_markdown_files, file_category,
    strip_frontmatter, chunk_markdown,
    embed_texts, vec_to_blob,
)


def cmd_stats(args) -> int:
    conn = open_db(create=False)
    cur = conn.cursor()
    n_files = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    n_chunks = cur.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_vec = cur.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    n_fts = cur.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    by_cat = cur.execute(
        "SELECT category, COUNT(*) FROM files GROUP BY category ORDER BY 2 DESC"
    ).fetchall()
    meta = dict(cur.execute("SELECT key, value FROM meta").fetchall())
    size_mb = INDEX_PATH.stat().st_size / 1024 / 1024 if INDEX_PATH.exists() else 0

    print(f"brain index: {INDEX_PATH}")
    print(f"  size:         {size_mb:.2f} MB")
    print(f"  files:        {n_files}")
    print(f"  chunks:       {n_chunks}")
    print(f"  fts5 rows:    {n_fts}  {'OK' if n_fts == n_chunks else 'OUT OF SYNC'}")
    print(f"  vec rows:     {n_vec}  {'OK' if n_vec == n_chunks else 'OUT OF SYNC'}")
    print(f"  model:        {meta.get('embed_model', '?')}  dim={meta.get('embed_dim', '?')}")
    print(f"  vault:        {BRAIN_VAULT}")
    print(f"  by category:")
    for cat, n in by_cat:
        print(f"    {cat:12s} {n}")
    return 0


def _delete_file_chunks(conn, file_path: str) -> int:
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id FROM chunks WHERE file_path = ?", (file_path,)
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return 0
    qmarks = ",".join("?" * len(ids))
    cur.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({qmarks})", ids)
    cur.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({qmarks})", ids)
    cur.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
    cur.execute("DELETE FROM files WHERE path = ?", (file_path,))
    return len(ids)


_FILENAME_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}-")
_TITLE_HEADING_RE = __import__("re").compile(r"(?m)^# +(.+?)\s*$")


def _title_boost_chunk(path: Path, body: str) -> str:
    """Build a high-signal 'title chunk' from filename + first H1.

    Distill outputs use the pattern '<category>/YYYY-MM-DD-<slug>.md' where
    the slug carries the semantic payload (e.g. 'my-project-home-color-palette
    -warm-cream-cobalt-coral'). Body text often skips re-stating these keywords
    in plain Korean, so a query like 'my-project 팔레트' can rank longer
    discursive files above the targeted decision. Prepending a synthetic
    chunk built from filename + first heading lets BM25 hit slug tokens and
    lets the vec model embed the headline concept directly.
    """
    stem = path.stem
    stem = _FILENAME_DATE_RE.sub("", stem)
    parts = [stem.replace("-", " ").replace("_", " ")]
    m = _TITLE_HEADING_RE.search(body)
    if m:
        parts.append(m.group(1).strip())
    parts.append(file_category(path))
    return " | ".join(parts)


def _index_file(conn, path: Path) -> int:
    """Index a single markdown file. Returns chunks written."""
    rel = str(path.relative_to(BRAIN_VAULT))
    text = path.read_text(encoding="utf-8", errors="replace")
    body = strip_frontmatter(text)
    body_chunks = chunk_markdown(body)
    # Always purge stale rows for this file first (idempotent re-index)
    _delete_file_chunks(conn, rel)
    if not body_chunks:
        # Still record the file so we don't keep re-trying empty content
        st = path.stat()
        conn.execute(
            "INSERT OR REPLACE INTO files(path, mtime, size, category, indexed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (rel, st.st_mtime, st.st_size, file_category(path), time.time()),
        )
        return 0

    # Synthetic title chunk first; body chunks follow with shifted indices.
    title_text = _title_boost_chunk(path, body)
    chunks: list[tuple[int, int, str]] = [(0, 0, title_text)] + body_chunks
    texts = [c[2] for c in chunks]
    embs = embed_texts(texts)
    cur = conn.cursor()
    for i, ((cs, ce, ctext), emb) in enumerate(zip(chunks, embs)):
        cur.execute(
            "INSERT INTO chunks(file_path, chunk_idx, text, char_start, char_end)"
            " VALUES (?, ?, ?, ?, ?)",
            (rel, i, ctext, cs, ce),
        )
        cid = cur.lastrowid
        cur.execute(
            "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
            (cid, ctext),
        )
        cur.execute(
            "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
            (cid, vec_to_blob(emb)),
        )
    st = path.stat()
    cur.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, category, indexed_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (rel, st.st_mtime, st.st_size, file_category(path), time.time()),
    )
    return len(chunks)


def cmd_file(args) -> int:
    p = Path(args.file).resolve()
    if not p.exists():
        print(f"file not found: {p}", file=sys.stderr)
        return 2
    if not str(p).lower().startswith(str(BRAIN_VAULT).lower()):
        print(f"file is outside BRAIN_VAULT ({BRAIN_VAULT}): {p}", file=sys.stderr)
        return 2
    conn = open_db()
    n = _index_file(conn, p)
    conn.commit()
    print(f"indexed: {p.relative_to(BRAIN_VAULT)}  chunks={n}")
    return 0


def cmd_full(args) -> int:
    # Drop everything, rebuild from scratch
    if INDEX_PATH.exists():
        INDEX_PATH.unlink()
    conn = open_db()
    return _run_walk(conn, force=True, dry=False)


def cmd_incremental(args) -> int:
    conn = open_db()
    return _run_walk(conn, force=False, dry=args.dry)


def refresh(quiet: bool = True) -> tuple[int, int]:
    """Incremental reindex, callable from other tools. Returns (indexed, deleted).

    brain-search calls this so a note is searchable the moment it is written. The
    old contract — "whoever writes a note also runs reindex" — fails silently
    whenever the writing session doesn't know the rule, and the result is
    indistinguishable from having no such note (hit 2026-07-16). Scanning costs
    ~0.3s against search's ~8.6s model load, so the caller pays ~3% to never be
    wrong."""
    conn = open_db()
    return _walk(conn, force=False, dry=False, quiet=quiet)


def _run_walk(conn, force: bool, dry: bool) -> int:
    _walk(conn, force=force, dry=dry, quiet=False)
    return 0


def _walk(conn, force: bool, dry: bool, quiet: bool) -> tuple[int, int]:
    cur = conn.cursor()
    known = {row[0]: (row[1], row[2]) for row in
             cur.execute("SELECT path, mtime, size FROM files").fetchall()}
    seen: set[str] = set()
    to_index: list[Path] = []
    for p in iter_markdown_files():
        rel = str(p.relative_to(BRAIN_VAULT))
        seen.add(rel)
        st = p.stat()
        if force or rel not in known:
            to_index.append(p)
            continue
        prev_mt, prev_sz = known[rel]
        if abs(prev_mt - st.st_mtime) > 0.5 or prev_sz != st.st_size:
            to_index.append(p)

    deleted = sorted(set(known) - seen)

    if dry:
        print(f"[dry] would index: {len(to_index)} file(s)")
        for p in to_index[:20]:
            print(f"  + {p.relative_to(BRAIN_VAULT)}")
        if len(to_index) > 20:
            print(f"  ... (+{len(to_index)-20} more)")
        print(f"[dry] would delete: {len(deleted)} file(s)")
        for rel in deleted[:20]:
            print(f"  - {rel}")
        return (0, 0)

    t0 = time.time()
    total_chunks = 0
    for i, p in enumerate(to_index, 1):
        n = _index_file(conn, p)
        total_chunks += n
        if not quiet and i % 10 == 0:
            print(f"  ...{i}/{len(to_index)} indexed", file=sys.stderr)
    for rel in deleted:
        n = _delete_file_chunks(conn, rel)
        if not quiet:
            print(f"  - {rel} (purged {n} chunks)")
    conn.commit()
    if not quiet:
        elapsed = time.time() - t0
        print(f"done: +{len(to_index)} file(s), -{len(deleted)} file(s), "
              f"{total_chunks} chunks, {elapsed:.1f}s")
    return (len(to_index), len(deleted))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--full", action="store_true",
                    help="drop index, rebuild from scratch")
    ap.add_argument("--file", help="reindex a single file path")
    ap.add_argument("--stats", action="store_true", help="print index health, no changes")
    ap.add_argument("--dry", action="store_true",
                    help="report what would be (re)indexed without writing")
    args = ap.parse_args()

    if args.stats:
        return cmd_stats(args)
    if args.file:
        return cmd_file(args)
    if args.full:
        return cmd_full(args)
    return cmd_incremental(args)


if __name__ == "__main__":
    sys.exit(main())
