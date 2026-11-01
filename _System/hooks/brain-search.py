#!/usr/bin/env python3
"""
brain-search: hybrid (BM25 + vector) search over the brain markdown corpus.

Usage:
  python brain-search.py "your natural-language query"
  python brain-search.py "..." -k 8              top-K results (default 5)
  python brain-search.py "..." --category Decisions
  python brain-search.py "..." --json            machine-readable output
  python brain-search.py "..." --raw-scores      show BM25 + cosine debug info
  python brain-search.py "..." --no-reindex      skip the pre-search index refresh

Searching refreshes the index first (incremental, ~0.3s when nothing changed),
so a note is findable as soon as it is written — no separate reindex step.

Ranking: Reciprocal Rank Fusion (k=60) over top-30 BM25 + top-30 cosine,
then scaled by category authority and recency (see CATEGORY_WEIGHT).
Results group by file (best chunk wins) and include a 200-char snippet.
A query whose terms match nothing lexically and nothing close in vector
space returns no results rather than the top-5 of an unrelated corpus.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

# Force UTF-8 stdout on Windows (default cp949 can't render —, …, common emoji).
# Skip reconfigure if stdout is non-tty piped capture which already handles bytes.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Self-heal interpreter mismatch: the ML stack (sqlite_vec + sentence-transformers)
# lives only in a pinned interpreter on some devices, while bare `python` resolves
# to an unrelated venv (2026-06-11 postmortem; recurred 2026-07-06 in a session
# following the global CLAUDE.md `python ...` snippet). sqlite_vec is the cheap
# import proxy for the whole stack. Interpreters missing from a device are
# skipped, so this list is safe to sync across devices.
_PINNED_PYTHONS = [
    Path.home() / "AppData/Local/Programs/Python/Python310/python.exe",  # home PC
]

# The hard-coded list above only heals the device it was written on: a new
# device is not in it, so the self-heal it advertises does not exist there. The
# installer writes the interpreter it detected here, which makes the fix travel.
# File over env var — the failing caller is a human typing `python
# brain-search.py`, and their shell has no brain env set.
_PYTHON_MARKER = Path(__file__).parent / ".brain-python"


def _candidate_pythons() -> list[Path]:
    out: list[Path] = []
    try:
        if _PYTHON_MARKER.exists():
            pinned = _PYTHON_MARKER.read_text(encoding="utf-8").strip()
            if pinned:
                out.append(Path(pinned))
    except OSError:
        pass  # a marker we cannot read must not break search
    out.extend(_PINNED_PYTHONS)
    return out


def _ensure_ml_stack() -> None:
    try:
        import sqlite_vec  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    if os.environ.get("BRAIN_SEARCH_REEXEC"):
        return  # already re-exec'd once — let the real ImportError surface below
    for py in _candidate_pythons():
        if py.exists() and py != Path(sys.executable):
            env = {**os.environ, "BRAIN_SEARCH_REEXEC": "1"}
            raise SystemExit(
                subprocess.run([str(py), __file__, *sys.argv[1:]], env=env).returncode
            )


_ensure_ml_stack()

sys.path.insert(0, str(Path(__file__).parent))

from _brain_lib import (  # noqa: E402
    BRAIN_VAULT, INDEX_PATH, open_db,
    embed_one, vec_to_blob,
)

RRF_K = 60          # standard RRF constant
PER_LIST_TOP = 30   # how many to pull from each ranker before fusion
SNIPPET_LEN = 240

# Category authority. RRF alone ranks a session log that broke a rule above the
# Knowledge file that states the rule — it has no notion of which file is
# current doctrine. That is not hypothetical: the same deployment mistake
# repeated across several sessions because each one searched, read the
# session log describing the violation first, and never reached the rule
# that would have prevented it. Knowledge/Memory are what is true now;
# Sessions are what happened once and may since have been overturned.
CATEGORY_WEIGHT = {
    "Knowledge": 1.30,   # current doctrine — rules, how-tos
    "Memory":    1.20,   # current state
    "Decisions": 1.15,   # settled judgements
    "Patterns":  1.05,
    "Mistakes":  1.00,
    "Sessions":  0.70,   # narrative of one day; superseded by the above
}

# Recency. Sessions age fast (a log from May rarely governs today); doctrine
# ages slowly (a correct rule stays correct). Never zeroes a result out — an old
# file still outranks a weak match, it just stops outranking a fresh one.
RECENCY_FLOOR = 0.60
_HALF_LIFE_DAYS = {"Sessions": 120}
_HALF_LIFE_DEFAULT = 400

# Relevance floor — deliberately narrow, and it does NOT catch every off-topic
# query. RRF scores are positional, so a query with zero real matches still
# yields a full top-5 that reads as "here is your answer". The obvious fixes do
# not work; measured on this corpus 2026-07-16:
#   - BM25 threshold: relevant "노션" scores -5.77, irrelevant "올림픽 마라톤
#     기록" scores -5.35. The ranges overlap — BM25 tracks query length as much
#     as relevance, so no absolute cutoff separates them.
#   - Vector distance: irrelevant "김치찌개" sits at 0.79, relevant "ComfyUI" at
#     0.91. Also overlapping, in the wrong direction.
# So this floor fires only where the evidence is unambiguous: BM25 matched
# nothing at all AND the nearest vector neighbour is far. That leaves off-topic
# queries that snag a common token ("법", "기록") still returning results.
# Deciding a hit is junk needs a signal this index does not carry — a reranker,
# or per-query score normalisation. Until then the floor trades recall of the
# "no answer" verdict for zero false cuts: it never drops a real hit.
MIN_VEC_DISTANCE = 0.95


def bm25_search(conn: sqlite3.Connection, query: str, limit: int,
                category: str | None) -> list[tuple[int, float]]:
    """Return [(chunk_id, raw_bm25_score_lower_is_better), ...]."""
    # FTS5 bm25() returns lower-is-better; we negate for "score" semantics later
    cat_join = ""
    cat_where = ""
    params: list = [query]
    if category:
        cat_join = "JOIN chunks c ON c.id = f.rowid JOIN files fi ON fi.path = c.file_path"
        cat_where = "AND fi.category = ?"
        params.append(category)
    params.append(limit)
    sql = f"""
    SELECT f.rowid, bm25(chunks_fts)
    FROM chunks_fts f
    {cat_join}
    WHERE chunks_fts MATCH ?
    {cat_where}
    ORDER BY bm25(chunks_fts)
    LIMIT ?
    """
    # FTS5 MATCH needs the query in its expression form. Wrap quoted to allow
    # natural-language phrases without tripping FTS operators. For multi-word,
    # join terms with OR to be tolerant.
    fts_query = _fts_query(query)
    params[0] = fts_query
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Fallback if syntax fails (e.g. only stopword-like tokens)
        return []
    return [(rid, float(score)) for rid, score in rows]


_FTS_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


# Korean is agglutinative and unicode61 splits on whitespace, so a query token
# carries its particle into the index lookup: "사내서버에" never matches the
# stored token "사내서버". Natural-language queries — the exact usage the global
# CLAUDE.md prescribes — therefore lost BM25 entirely and fell back to vector
# search alone, which on long sentences returns near-neighbours of the sentence
# shape rather than the topic. Longest-first so "에서는" wins over "는".
_KO_SUFFIXES = (
    "으로부터", "이라고는", "에서부터",
    "으로서", "으로써", "에서는", "에게서", "이라고", "에게는", "까지는",
    "라고", "에서", "에게", "한테", "까지", "부터", "보다", "처럼", "마다",
    "조차", "마저", "이나", "이란", "이는", "으로", "로써", "로서", "와는",
    "과는", "에는", "에도", "이든", "든지",
    # verb/adjective endings that ride on a noun stem: "배포해도", "설치하는"
    "해야지", "했었다", "하려고", "해도", "하는", "하고", "한다", "했다",
    "하면", "해서", "하니", "되나", "되는", "된다", "됐다", "되면", "인가",
    "인지", "이다", "나요", "까요", "습니까", "ㅂ니까",
    "을", "를", "이", "가", "은", "는", "에", "의", "도", "만", "과", "와",
    "로", "랑", "나", "야",
)

_HANGUL_RE = re.compile(r"[가-힣]")
_MIN_STEM = 2       # never strip down to a 1-char stem — pure noise in FTS


def _ko_stem(tok: str) -> str | None:
    """Strip one trailing particle/ending, if what remains is still a word.
    Returns None when nothing safe can be stripped."""
    for suf in _KO_SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= _MIN_STEM:
            return tok[: -len(suf)]
    return None


def _fts_query(q: str) -> str:
    """Tokenize a free-text query into an FTS5 MATCH expression.

    Each token contributes its literal form, and — when a Korean particle or
    ending can be stripped — a prefix match on the bare stem. The prefix is what
    bridges query to index in both directions: query "사내서버에" reaches the
    stored "사내서버", and query "사내서버" still reaches a stored "사내서버는".
    """
    toks = [t for t in re.split(r"\s+", q.strip()) if t]
    # A bare one-syllable Hangul token ("법", "수", "것") matches a large slice
    # of any Korean corpus and drags in files on the strength of nothing. Drop
    # them when the query has something more specific to stand on.
    meaty = [t for t in toks
             if not (len(t) == 1 and _HANGUL_RE.match(t))]
    if meaty:
        toks = meaty
    out: list[str] = []
    for t in toks:
        lit = t.replace('"', '""')          # FTS5 reserves the double-quote
        out.append(f'"{lit}"')
        if _HANGUL_RE.search(t) or "-" in t or "_" in t:
            stem = _ko_stem(t)
            if stem:
                out.append(f'"{stem.replace(chr(34), chr(34) * 2)}"*')
    if not out:
        return '""'
    # dedupe, preserving order — a repeated term skews BM25 toward itself
    seen: set[str] = set()
    uniq = [x for x in out if not (x in seen or seen.add(x))]
    return " OR ".join(uniq)


def vec_search(conn: sqlite3.Connection, query: str, limit: int,
               category: str | None) -> list[tuple[int, float]]:
    """Return [(chunk_id, distance_lower_is_better), ...] via sqlite-vec KNN."""
    q_emb = embed_one(query)
    q_blob = vec_to_blob(q_emb)
    if category:
        # sqlite-vec doesn't join cleanly inside MATCH, so pull more and filter
        sql = """
        SELECT v.rowid, v.distance FROM (
            SELECT rowid, distance FROM chunks_vec
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
        ) v
        JOIN chunks c ON c.id = v.rowid
        JOIN files f ON f.path = c.file_path
        WHERE f.category = ?
        ORDER BY v.distance
        LIMIT ?
        """
        rows = conn.execute(sql, (q_blob, limit * 3, category, limit)).fetchall()
    else:
        sql = """
        SELECT rowid, distance FROM chunks_vec
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """
        rows = conn.execute(sql, (q_blob, limit)).fetchall()
    return [(rid, float(d)) for rid, d in rows]


def rrf_fuse(rankings: list[list[tuple[int, float]]], k: int = RRF_K
             ) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. Each input list is (id, raw_score),
    ranked by position (best first). Returns merged (id, fused_score)
    sorted by fused_score descending."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (cid, _) in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _recency_weight(mtime: float, now: float, category: str) -> float:
    """Exponential decay on file age, floored so nothing is erased by age."""
    age_days = max(0.0, (now - float(mtime or 0)) / 86400.0)
    half_life = _HALF_LIFE_DAYS.get(category, _HALF_LIFE_DEFAULT)
    decayed = 0.5 ** (age_days / half_life)
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * decayed


def authority_weight(category: str, mtime: float, now: float) -> float:
    """How much this file's kind and age should move it, independent of the
    query. Doctrine outranks narrative; fresh outranks stale."""
    return (CATEGORY_WEIGHT.get(category, 1.0)
            * _recency_weight(mtime, now, category))


def fetch_chunk_meta(conn: sqlite3.Connection, ids: list[int]) -> dict:
    if not ids:
        return {}
    qmarks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT c.id, c.file_path, c.chunk_idx, c.text,
                    f.category, f.mtime
            FROM chunks c JOIN files f ON f.path = c.file_path
            WHERE c.id IN ({qmarks})""",
        ids,
    ).fetchall()
    return {r[0]: {
        "file_path": r[1], "chunk_idx": r[2], "text": r[3],
        "category": r[4], "mtime": r[5],
    } for r in rows}


def _snippet(text: str, query: str, max_len: int = SNIPPET_LEN) -> str:
    """Pick a centered snippet around the first query-term hit, or head."""
    import re
    text = text.strip()
    if len(text) <= max_len:
        return text
    # naive: pick the first query token occurrence
    toks = [t for t in re.split(r"\s+", query) if t]
    pos = -1
    for t in toks:
        i = text.lower().find(t.lower())
        if i >= 0:
            pos = i
            break
    if pos < 0:
        return text[:max_len].rstrip() + "…"
    half = max_len // 2
    start = max(0, pos - half)
    end = min(len(text), start + max_len)
    s = text[start:end].strip()
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def auto_reindex() -> tuple[int, int]:
    """Bring the index up to date before searching. Returns (indexed, deleted).

    A note that exists but isn't indexed is a note that doesn't exist — search
    reports "nothing found", which reads identically to "you have no such note".
    That is not hypothetical: on 2026-07-16 a session wrote a Knowledge file,
    searched for it, got zero hits, and only found it after running reindex by
    hand. Making the writer remember to reindex is the contract that already
    failed; making the reader refresh is the one that cannot.

    Fail-open by design — a stale index still answers, a raised exception does
    not. Set BRAIN_SEARCH_NO_REINDEX=1 to skip (used by the reindex tool itself
    and by anything that must not write to the db)."""
    if os.environ.get("BRAIN_SEARCH_NO_REINDEX"):
        return (0, 0)
    try:
        import importlib.util
        path = Path(__file__).parent / "brain-reindex.py"
        if not path.exists():
            return (0, 0)
        spec = importlib.util.spec_from_file_location("_brain_reindex", path)
        if spec is None or spec.loader is None:
            return (0, 0)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Same process, so _brain_lib's cached model handle is shared — the
        # embedding model is loaded once for both reindex and the query.
        return mod.refresh(quiet=True)
    except Exception:
        return (0, 0)   # never let index maintenance break the search itself


def search(query: str, k: int = 5, category: str | None = None,
           raw_scores: bool = False, fast: bool = False) -> list[dict]:
    """fast=True: BM25 only, no embedding model.

    Why it exists: the vector half needs sentence-transformers, and importing
    plus loading that model costs ~13s on this device while the actual lookup
    costs 31ms (measured 2026-08-28: import 6.5s, load 6.7s, embed 26ms,
    FTS 5ms). A 13-second check is one nobody runs mid-task, so the habit
    never forms and 96% of the corpus stays unread. BM25-only lands at ~0.1s,
    which is cheap enough to run before starting any piece of work. It is a
    presence check ("is there anything on this?"), not the full recall — when
    it hits, re-run without --fast for the semantic neighbours."""
    conn = open_db(create=False)

    bm = bm25_search(conn, query, PER_LIST_TOP, category)
    vc = [] if fast else vec_search(conn, query, PER_LIST_TOP, category)

    # Nothing matched lexically and nothing sits close in vector space: the
    # corpus has no answer. Say so instead of returning its five nearest
    # strangers, which read as an answer and get acted on.
    if not bm and (not vc or min(d for _, d in vc) > MIN_VEC_DISTANCE):
        return []

    fused = rrf_fuse([bm, vc])

    # Dedupe by file_path: keep best chunk per file
    bm_rank = {cid: i + 1 for i, (cid, _) in enumerate(bm)}
    vc_rank = {cid: i + 1 for i, (cid, _) in enumerate(vc)}

    # Weight the whole fused pool, not a prefix of it: authority reorders the
    # list, so a slice taken before weighting would drop files that belong on
    # page one.
    meta = fetch_chunk_meta(conn, [cid for cid, _ in fused])
    now = time.time()
    ranked: list[tuple[int, float]] = []
    for cid, fused_score in fused:
        m = meta.get(cid)
        if m is None:
            continue
        ranked.append((cid, fused_score * authority_weight(
            m["category"], m["mtime"], now)))
    ranked.sort(key=lambda x: x[1], reverse=True)

    seen_files: set[str] = set()
    out: list[dict] = []
    for cid, fused_score in ranked:
        if cid not in meta:
            continue
        m = meta[cid]
        if m["file_path"] in seen_files:
            continue
        seen_files.add(m["file_path"])
        # If the winning chunk is the synthetic title boost (chunk_idx 0),
        # show the first body chunk's text in the snippet for human-readable
        # context. Falls back to title text if there's no body chunk.
        snippet_text = m["text"]
        if m["chunk_idx"] == 0:
            body = conn.execute(
                "SELECT text FROM chunks WHERE file_path = ? AND chunk_idx = 1",
                (m["file_path"],),
            ).fetchone()
            if body:
                snippet_text = body[0]
        item = {
            "file": m["file_path"],
            "abs_path": str(BRAIN_VAULT / m["file_path"]),
            "category": m["category"],
            "score": round(fused_score, 5),
            "snippet": _snippet(snippet_text, query),
        }
        if raw_scores:
            item["debug"] = {
                "chunk_id": cid,
                "chunk_idx": m["chunk_idx"],
                "bm25_rank": bm_rank.get(cid),
                "vec_rank": vc_rank.get(cid),
                "authority": round(authority_weight(
                    m["category"], m["mtime"], now), 3),
            }
        out.append(item)
        if len(out) >= k:
            break
    return out


# Recall write-back moved out of search (2026-09-28): a hit that is only
# *returned* is exposure, not recall -- it inflated recall_count and gave no
# baseline for measuring auto-injection. Exposure goes to the usage table
# below; an actual read is counted by brain-recall-read.py (PostToolUse),
# which owns the journal frontmatter write-back now. See _System/decay.md.


USAGE_DB = Path.home() / ".claude" / "data" / "brain-usage.sqlite"


def record_usage(results: list[dict], query: str) -> None:
    """Every returned hit is a usage event, Knowledge included.

    Why a side DB instead of frontmatter: Knowledge pages must not be rewritten on recall -- 109 of
    them are `scope: shared` and a per-device counter would conflict on every
    sync. The search index is disposable (reindex rebuilds it); usage history
    is not, so it lives in its own file. Raw queries are never stored, only a
    truncated hash, so repeated questions can be grouped without keeping text.
    Best-effort -- a failed write must never break search."""
    if not results:
        return
    try:
        USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
        qh = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        con = sqlite3.connect(USAGE_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS usage(
            path TEXT NOT NULL, category TEXT, ts TEXT NOT NULL,
            query_hash TEXT, rank INTEGER, score REAL)""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_path ON usage(path)")
        con.executemany(
            "INSERT INTO usage(path, category, ts, query_hash, rank, score)"
            " VALUES(?,?,?,?,?,?)",
            [(r.get("file"), r.get("category"), now, qh, i, r.get("score"))
             for i, r in enumerate(results, 1)])
        con.commit()
        con.close()
    except Exception:
        pass


def format_human(results: list[dict], query: str) -> str:
    if not results:
        return f"## brain-search: no results for {query!r}\n"
    lines = [f"## brain-search results for {query!r}",
             f"({len(results)} files; index: {INDEX_PATH.name})", ""]
    for i, r in enumerate(results, 1):
        head = f"{i}. [{r['category']}] {r['file']}  (score {r['score']:.4f})"
        if "debug" in r:
            d = r["debug"]
            head += f"  [bm25_rank={d['bm25_rank']} vec_rank={d['vec_rank']}]"
        lines.append(head)
        lines.append(f"   {r['abs_path']}")
        lines.append(f"   > {r['snippet']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("query", help="natural-language query")
    ap.add_argument("-k", type=int, default=5, help="top-K (default 5)")
    ap.add_argument("--category", help="restrict to a category folder")
    ap.add_argument("--fast", action="store_true",
                    help="BM25 only (~0.1s, no embedding model). Presence "
                         "check before starting work; rerun without it to "
                         "get semantic neighbours.")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--raw-scores", action="store_true",
                    help="show BM25 + vec ranks in output")
    ap.add_argument("--no-touch", action="store_true",
                    help="skip exposure logging (usage table)")
    ap.add_argument("--no-reindex", action="store_true",
                    help="skip the incremental reindex done before searching")
    args = ap.parse_args()

    if not INDEX_PATH.exists():
        print(f"index missing: {INDEX_PATH}\n"
              f"build it with: python brain-reindex.py --full",
              file=sys.stderr)
        return 2

    if not args.no_reindex and not args.fast:
        indexed, deleted = auto_reindex()
        if indexed or deleted:
            # Say so: silence here would look like the note was always indexed,
            # and the delay (embedding runs per changed file) needs a reason.
            print(f"[index refreshed: +{indexed} -{deleted}]", file=sys.stderr)

    results = search(args.query, k=args.k, category=args.category,
                     raw_scores=args.raw_scores, fast=args.fast)
    if not args.no_touch:
        record_usage(results, args.query)
    if args.json:
        print(json.dumps({"query": args.query, "results": results},
                          ensure_ascii=False, indent=2))
    else:
        print(format_human(results, args.query))
    return 0


if __name__ == "__main__":
    sys.exit(main())
