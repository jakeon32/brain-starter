#!/usr/bin/env python3
"""Guards the update-existing-page path in brain-consolidate.py.

Why this file exists (2026-09-28):
consolidation used to create a new page per cluster and overwrite a same-slug page
without a trace. Knowledge/ is outside git, so an overwrite was unrecoverable.
Invariants checked, with claude and the vector lookup stubbed out:
  - action=update keeps the page's frontmatter (scope, tags) and its changelog,
    bumps `updated:`, appends one changelog line, archives the fragments
  - the previous version is copied to Knowledge/archived/<year>/ before writing
  - an update that shrinks the body below COLLAPSE_RATIO fails the quality gate
  - a new page whose slug already exists gets -2 instead of overwriting
  - action=update without a target (LLM ignored the rule) is treated as new

Run (no framework): python _System/hooks/tests/test_consolidate_update.py
Exit 0 = pass.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]


def load(vault: Path):
    os.environ["BRAIN_VAULT"] = str(vault)
    os.environ["CLAUDE_DISTILL_VAULT"] = str(vault)
    sys.path.insert(0, str(HOOKS))
    for m in ("_brain_lib", "bc"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("bc", HOOKS / "brain-consolidate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.argv = ["brain-consolidate.py"]
    spec.loader.exec_module(mod)
    return mod


OLD = """---
title: 한국어 텍스트 처리 함정
type: knowledge
tags: [korean, text]
updated: 2026-08-01
---

# 한국어 텍스트 처리 함정

## 폭 계산
한글은 표시 폭이 2칸이다. 글자 수로 자르면 넘친다. 이 절은 길게 유지되어야 한다.
여러 줄의 기존 노하우가 여기 있다. 이 줄들은 갱신 후에도 남아 있어야 한다.

## 변경 이력

- **2026-08-01** Ingest — 3개 저널 조각 통합.
"""


def main() -> int:
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        for d in ("_System", "Knowledge", "Decisions"):
            (vault / d).mkdir()
        page = vault / "Knowledge" / "korean-text.md"
        page.write_text(OLD, encoding="utf-8")
        def make_frags():
            out = []
            for i in range(2):
                f = vault / "Decisions" / f"2026-09-0{i + 1}-korean-width-{i}.md"
                f.write_text("---\ntitle: x\n---\n조각 본문\n", encoding="utf-8")
                out.append(f"Decisions/{f.name}")
            return out
        frags = make_frags()

        # 2026-10-29: a fragment the session marked standalone is never clustered again
        bc = load(vault)
        (vault / "Decisions" / "solo.md").write_text(
            "---\ntype: decisions\nconsolidate: standalone\n---\n본문\n", encoding="utf-8")
        (vault / "Decisions" / "body-mention.md").write_text(
            "---\ntype: decisions\n---\nconsolidate: standalone 은 본문이라 무시\n", encoding="utf-8")
        check(bc.is_standalone("Decisions/solo.md"), "frontmatter standalone must be detected")
        check(not bc.is_standalone("Decisions/body-mention.md"), "marker in body must not count")
        check(not bc.is_standalone(frags[0]), "plain fragment must not be standalone")
        for n in ("solo.md", "body-mention.md"):
            (vault / "Decisions" / n).unlink()

        bc = load(vault)
        target_body = bc.CHANGELOG_RE.sub(
            "", bc.FRONTMATTER_RE.sub("", OLD, count=1)).strip()
        target = {"path": page, "slug": "korean-text", "cos": 0.8, "text": OLD,
                  "front": bc.FRONTMATTER_RE.match(OLD).group(0), "body": target_body}
        good_q = {k: True for k in bc.QUALITY_KEYS}

        def fake(draft):
            bc.call_claude = lambda *a, **k: dict(draft, quality=dict(good_q))

        # 1. update path
        bc.nearest_page = lambda members: target
        fake({"title": "t", "slug": "whatever", "action": "update",
              "supersedes_note": "Slack 글자 수 제한 추가.",
              "body": target_body + "\n\n## Slack\n글자 수 제한은 표시 폭 기준이 아니다.\n"})
        d = bc.consolidate_once(frags, "low")
        check(bc.quality_pass(d["quality"]), "valid update should pass quality")
        slug, full, year = bc.build_page_text(d, frags)
        check(slug == "korean-text", f"update must keep slug, got {slug}")
        bc.write_page(slug, full, frags, year, reindex=False)
        new = page.read_text(encoding="utf-8")
        check("tags: [korean, text]" in new, "frontmatter must be preserved")
        check("updated: 2026-08-01" not in new and "updated: " in new, "updated must bump")
        check("2026-08-01** Ingest" in new and "Update — 2개" in new,
              "old changelog kept + new entry appended")
        check(new.count("## 변경 이력") == 1, "exactly one changelog section")
        check("## Slack" in new, "new content written")
        backups = list((vault / "Knowledge" / "archived" / year).glob("korean-text.*.md"))
        check(len(backups) == 1 and backups[0].read_text(encoding="utf-8") == OLD,
              "previous version must be archived verbatim")
        check(not any((vault / f).exists() for f in frags), "fragments archived")

        frags = make_frags()   # 1번에서 보관 폴더로 옮겨졌다

        # 2. collapse guard
        fake({"title": "t", "slug": "korean-text", "action": "update",
              "body": "# 한국어\n짧다.\n"})
        d = bc.consolidate_once(frags, "low")
        check(not bc.quality_pass(d["quality"]) and "붕괴" in d["quality"]["issues"],
              "shrinking update must fail the quality gate")

        # 3. new page with colliding slug -> -2
        bc.nearest_page = lambda members: None
        fake({"title": "새 주제", "slug": "korean-text", "action": "new", "body": "# 새\n본문\n"})
        d = bc.consolidate_once(frags, "low")
        slug, full, year = bc.build_page_text(d, frags)
        check(slug == "korean-text-2", f"collision must not overwrite, got {slug}")

        # 4. update claimed without a target -> new
        fake({"title": "t", "slug": "fresh-topic", "action": "update", "body": "# x\n"})
        d = bc.consolidate_once(frags, "low")
        check("_target" not in d, "update without target must be treated as new")
        slug, _, _ = bc.build_page_text(d, frags)
        check(slug == "fresh-topic", f"got {slug}")

        # 5. prompt carries the existing page only when a target exists
        pr = bc.build_ingest_prompt([], target)
        check("기존 페이지 본문" in pr and "폭 계산" in pr, "prompt must include target body")
        check("기존 페이지 본문" not in bc.build_ingest_prompt([], None), "no target -> no body")

    if fails:
        print("FAIL\n  - " + "\n  - ".join(fails))
        return 1
    print("PASS consolidate update path (5 groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
