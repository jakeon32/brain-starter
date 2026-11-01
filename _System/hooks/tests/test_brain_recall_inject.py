#!/usr/bin/env python3
"""Guards brain-recall-inject.py: the relevance gate and the hook contract.

Why this file exists (2026-09-28):
BM25 returns hits for any query, so the hook's gate is what keeps every prompt
from getting five unrelated titles. The gate was tuned on 120 real prompts; this
file pins the behaviours that tuning settled on, on a tiny fake corpus so the
result does not depend on this device's vault:
  - a topical term in a document's title/heading + a second term in its body -> inject
  - common words only (title match on a frequent word) -> no inject
  - conversational prompts ("진행해", "확인해줘") -> no search at all
  - CLAUDE_DISTILL_CHILD, slash commands, garbage stdin -> silent exit 0
  - output is UserPromptSubmit additionalContext wrapped in <brain-recall>

Run (no framework): python _System/hooks/tests/test_brain_recall_inject.py
Exit 0 = pass.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "brain-recall-inject.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("inj", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_search_module(vault: Path, docs: dict[str, str]):
    """A stand-in for brain-search: same FTS table names, BM25 order, no ML."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE files(path TEXT)")
    db.execute("CREATE TABLE chunks(id INTEGER PRIMARY KEY, file_path TEXT)")
    db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(text, tokenize='unicode61')")
    for i, (rel, body) in enumerate(docs.items(), 1):
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        db.execute("INSERT INTO files VALUES(?)", (rel,))
        db.execute("INSERT INTO chunks VALUES(?,?)", (i, rel))
        db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (i, body.lower()))
    # 문서 수를 늘려 IDF가 의미를 갖게 한다 — 흔한 단어는 채움 문서 대부분에 넣는다.
    for j in range(40):
        rel = f"Knowledge/filler-{j}.md"
        body = "# 메모\n페이지 관리 폴더 작업 기록\n"
        i = 1000 + j
        db.execute("INSERT INTO files VALUES(?)", (rel,))
        db.execute("INSERT INTO chunks VALUES(?,?)", (i, rel))
        db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (i, body))

    def search(query, k=5, fast=True, **_):
        terms = " OR ".join(f'"{t}"*' for t in query.split())
        rows = db.execute(
            "SELECT c.file_path FROM chunks_fts f JOIN chunks c ON c.id=f.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (terms, k)).fetchall()
        return [{"file": r[0], "abs_path": str(vault / r[0]),
                 "category": r[0].split("/")[0]} for r in rows]

    return types.SimpleNamespace(
        search=search, open_db=lambda create=False: db,
        _ko_stem=lambda t: None, INDEX_PATH=vault)


def run_hook(stdin: str, **env) -> subprocess.CompletedProcess:
    e = dict(os.environ, **env)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin.encode("utf-8"),
                          env=e, capture_output=True)


def main() -> int:
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    inj = load_hook()
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        bs = fake_search_module(vault, {
            "Knowledge/shuttle.md": "---\ntitle: 셔틀 노선 운영\n---\n# 셔틀 노선\n귀가행 노선과 로비 승차 위치\n",
            "Knowledge/common.md": "---\ntitle: 페이지 관리\n---\n# 페이지 관리\n페이지 폴더 정리 방법\n",
        })

        got = inj.pick("귀가행 셔틀 노선 확인해줘", bs)
        check([h["file"] for h in got] == ["Knowledge/shuttle.md"],
              f"topical prompt should inject shuttle.md, got {[h['file'] for h in got]}")

        # 2026-10-29: a hit below the score floor is not injected (low scores were read 5~9%)
        floor, inj.IDF_SUM_MIN = inj.IDF_SUM_MIN, 999
        got = inj.pick("귀가행 셔틀 노선 확인해줘", bs)
        inj.IDF_SUM_MIN = floor
        check(got == [], f"hit below IDF_SUM_MIN must not inject, got {[h['file'] for h in got]}")

        got = inj.pick("페이지 폴더 관리 좀 해줘", bs)
        check(got == [], f"common-word prompt must not inject, got {[h['file'] for h in got]}")

        for p in ("진행해", "확인해줘", "응 그것도 확인해서 정리해줘", "128449", ""):
            check(inj.pick(p, bs) == [], f"conversational prompt injected: {p!r}")

        check(inj.key_terms("영상이 부산에", lambda t: None) == ["영상", "부산에"],
              f"particle strip: {inj.key_terms('영상이 부산에', lambda t: None)}")
        check("히어로" in inj.key_terms("히어로 이미지", lambda t: None),
              "must not strip 로 from 히어로")

        txt = inj.render([{"category": "Knowledge", "title": "T", "abs_path": "/x.md",
                           "matched": ["a"]}])
        check(txt.startswith("<brain-recall>") and txt.endswith("</brain-recall>"),
              "render must wrap in <brain-recall>")

    # 2026-10-09: system text in the prompt must not become search terms
    inj = load_hook()
    noisy = ('<task-notification><output-file>X:/tmp/AppData/Local/Temp/x.output'
             '</output-file></task-notification> <channel source="plugin:chatbridge:chatbridge" '
             'chat_id="1"> 셔틀 노선</channel>')
    cleaned = inj.clean_prompt(noisy)
    check("appdata" not in cleaned.lower() and "chatbridge" not in cleaned.lower()
          and "셔틀 노선" in cleaned, f"clean_prompt must drop system blocks/tags/paths: {cleaned!r}")

    # 2026-10-09: the project know-how book is picked by the session's working folder
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        (vault / "Knowledge" / "Projects").mkdir(parents=True)
        (vault / "Knowledge" / "Projects" / "game.md").write_text(
            "---\ntitle: 게임 노하우 북\nfolders:\n  - G:/claude/content\n  - G:\\claude\\content\\game-1003\n---\n# x\n",
            encoding="utf-8")
        bs = types.SimpleNamespace(BRAIN_VAULT=vault)
        b = inj.project_book("G:\\claude\\content\\game-1003\\sub", bs)
        check(b is not None and b["title"] == "게임 노하우 북", f"project_book must match by folder prefix: {b}")
        check(inj.project_book("G:/claude/contentX", bs) is None, "project_book must not match a sibling prefix")
        check(inj.project_book("C:/elsewhere", bs) is None, "project_book must return None outside project folders")

    ev = json.dumps({"prompt": "셔틀 노선 귀가행", "session_id": "t"})
    for label, stdin, env in (
            ("child", ev, {"CLAUDE_DISTILL_CHILD": "1"}),
            ("disabled", ev, {"BRAIN_RECALL_INJECT": "0"}),
            ("slash", json.dumps({"prompt": "/loop 5m"}), {}),
            ("garbage", "not json", {}),
            ("empty", "", {})):
        r = run_hook(stdin, **env)
        check(r.returncode == 0 and r.stdout == b"", f"{label}: must exit 0 silently")

    if fails:
        print("FAIL\n  - " + "\n  - ".join(fails))
        return 1
    print("PASS brain-recall-inject (gate + contract)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
