"""
Shared library for brain-reindex.py and brain-search.py.

Responsibilities:
- locate brain vault root (env override)
- open sqlite db with sqlite-vec extension loaded
- enumerate brain markdown files (with sensible exclusions)
- chunk markdown into ~500-char paragraphs
- embed via sentence-transformers (lazy import; cached model handle)
- write/read schema (chunks, chunks_fts, chunks_vec)
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Iterable, Iterator

# Quiet hugging-face telemetry/auth warnings before any HF import happens.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
# After first download the model is cached; force offline to skip auth check.
_HF_CACHE_ENV = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
if (Path(_HF_CACHE_ENV) / "hub").exists():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

# ---- Config ----------------------------------------------------------------
# BRAIN_VAULT lets this search subsystem point at a different vault than the
# rest of the pipeline; CLAUDE_DISTILL_VAULT (set by the installer's launchers)
# is the system-wide standard and takes precedence when BRAIN_VAULT is unset.
# Fallback is home-relative (not a device-specific absolute path) so a fresh
# device without either env var still resolves to the documented default
# location instead of silently pointing at another device's path.
BRAIN_VAULT = Path(
    os.environ.get("BRAIN_VAULT")
    or os.environ.get("CLAUDE_DISTILL_VAULT")
    or str(Path.home() / "brain")
)
INDEX_PATH = Path(os.environ.get(
    "BRAIN_INDEX",
    str(Path.home() / ".claude" / "data" / "brain-index.sqlite"),
))
EMBED_MODEL = os.environ.get(
    "BRAIN_EMBED_MODEL",
    "paraphrase-multilingual-MiniLM-L12-v2",
)
EMBED_DIM = int(os.environ.get("BRAIN_EMBED_DIM", "384"))

# Folders under brain vault we INDEX (runtime memory only)
INDEX_FOLDERS = ("Decisions", "Mistakes", "Patterns", "Sessions",
                 "Knowledge", "Memory", "Agents")

# Folders never indexed (system specs, planning, vault metadata)
EXCLUDE_PREFIXES = ("_System", "docs", ".obsidian", ".git", ".search")

CHUNK_TARGET = 500     # target chars per chunk (Korean+English mixed)
CHUNK_MIN = 80         # below this, merge into prior/next chunk
CHUNK_OVERLAP = 0      # paragraph-based chunks already share semantic edges

FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


# ---- DB --------------------------------------------------------------------

def open_db(path: Path = INDEX_PATH, create: bool = True) -> sqlite3.Connection:
    """Open sqlite with sqlite-vec loaded. Creates schema on first use."""
    import sqlite_vec
    path.parent.mkdir(parents=True, exist_ok=True)
    # busy_timeout > slowest single reindex: two sessions ending close together
    # each spawn a detached reindex; reindex holds the write lock for the whole
    # embed+insert pass (10-34s). Without this the loser hits sqlite's 5s default,
    # raises "database is locked", and — detached with stderr=DEVNULL — dies with
    # no trace (2026-06-18: 14 notes silently unindexed). Wait it out instead.
    conn = sqlite3.connect(str(path), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if create:
        _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing. Idempotent."""
    cur = conn.cursor()
    cur.executescript(f"""
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY,
        mtime REAL NOT NULL,
        size INTEGER NOT NULL,
        category TEXT,
        indexed_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT NOT NULL,
        chunk_idx INTEGER NOT NULL,
        text TEXT NOT NULL,
        char_start INTEGER NOT NULL,
        char_end INTEGER NOT NULL,
        FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
    """)
    # FTS5 mirror — populate manually (so we control tokenizer)
    cur.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        text,
        content='chunks',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    )
    """)
    # Vec storage — rowid mirrors chunks.id
    cur.execute(f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
        embedding float[{EMBED_DIM}]
    )
    """)
    # Meta table for index health/config
    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    cur.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?), (?, ?)",
        ("embed_model", EMBED_MODEL, "embed_dim", str(EMBED_DIM)),
    )
    conn.commit()


# ---- File walking ----------------------------------------------------------

def iter_markdown_files(root: Path = BRAIN_VAULT) -> Iterator[Path]:
    """Yield .md files under brain vault, filtered to INDEX_FOLDERS."""
    if not root.exists():
        return
    for folder in INDEX_FOLDERS:
        sub = root / folder
        if not sub.exists():
            continue
        for p in sub.rglob("*.md"):
            rel = p.relative_to(root)
            parts = rel.parts
            if any(parts[0] == ex for ex in EXCLUDE_PREFIXES):
                continue
            if "archived" in parts:
                # consolidated source fragments — kept for provenance, excluded from search
                continue
            if p.name in ("_index.md", "_catalog.md"):
                # MOC + auto-generated catalog files are pure link tables, low
                # value as search hits. Critically, _catalog.md must never reach
                # the consolidator: swept into a cluster it gets renamed into
                # archived/, which corrupts the merged note AND crashes
                # write_page on a name clash (the 2026-06-30 consolidate crash).
                continue
            yield p


def file_category(path: Path, root: Path = BRAIN_VAULT) -> str:
    rel = path.relative_to(root)
    return rel.parts[0] if rel.parts else "unknown"


# ---- Chunking --------------------------------------------------------------

def strip_frontmatter(text: str) -> str:
    """Drop leading YAML frontmatter block."""
    return FRONTMATTER_RE.sub("", text, count=1).lstrip()


def chunk_markdown(text: str) -> list[tuple[int, int, str]]:
    """
    Split markdown into ~CHUNK_TARGET-char chunks at paragraph boundaries.
    Returns [(char_start, char_end, chunk_text), ...] referencing the
    POST-frontmatter text (so caller should pass already-stripped text).
    """
    text = text.strip()
    if not text:
        return []
    # Paragraph split: blank lines
    paras = re.split(r"\n\s*\n", text)
    # Track char positions in the original (stripped) text
    chunks: list[tuple[int, int, str]] = []
    buf = []
    buf_start = 0
    buf_len = 0
    cursor = 0
    for p in paras:
        p_stripped = p.strip()
        if not p_stripped:
            cursor += len(p) + 2  # account for the blank line separator
            continue
        # locate this paragraph in source from cursor
        idx = text.find(p_stripped, cursor)
        if idx < 0:
            idx = cursor
        p_start = idx
        p_end = idx + len(p_stripped)
        cursor = p_end
        plen = len(p_stripped)
        if buf and buf_len + plen > CHUNK_TARGET:
            # flush current buffer
            chunk_text = "\n\n".join(buf)
            chunks.append((buf_start, buf_start + len(chunk_text), chunk_text))
            buf = []
            buf_len = 0
        if not buf:
            buf_start = p_start
        buf.append(p_stripped)
        buf_len += plen + 2
    if buf:
        chunk_text = "\n\n".join(buf)
        chunks.append((buf_start, buf_start + len(chunk_text), chunk_text))
    # Merge any leftover tiny chunk into the previous one
    if len(chunks) >= 2 and len(chunks[-1][2]) < CHUNK_MIN:
        a_start, _, a_text = chunks[-2]
        _, b_end, b_text = chunks[-1]
        chunks[-2] = (a_start, b_end, a_text + "\n\n" + b_text)
        chunks.pop()
    return chunks


# ---- Embeddings ------------------------------------------------------------

_MODEL = None


def get_model():
    global _MODEL
    if _MODEL is None:
        # Lazy import — saves ~10s on calls that don't need embeddings
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(EMBED_MODEL)
    return _MODEL


def embed_texts(texts: list[str]):
    """Returns a numpy array (n, EMBED_DIM)."""
    m = get_model()
    return m.encode(texts, show_progress_bar=False, normalize_embeddings=True)


def embed_one(text: str):
    return embed_texts([text])[0]


def vec_to_blob(v) -> bytes:
    """Pack a float vector into the sqlite-vec wire format."""
    return struct.pack(f"{len(v)}f", *(float(x) for x in v))
