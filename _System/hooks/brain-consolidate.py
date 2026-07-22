#!/usr/bin/env python3
"""
brain-consolidate: LLM-wiki convergence for the brain journal layer.

Modes:
  detect (default) — READ-ONLY. Cluster the journal layer (Decisions/Mistakes/
      Patterns) into topic groups ripe for Ingest.
  --ingest N       — Consolidate cluster N into one wiki page (dry-run; --apply
      to write+archive+reindex).
  --weekly         — scheduled hybrid entry point (since 2026-07-19). Small
      load (clusters <= UNATTENDED_MAX_CLUSTERS) -> unattended --auto here
      and now; backlog -> --queue (LLM 0 calls) so the next live session
      takes over. See _System/consolidate.md §Session processing.
  --queue          — cluster only (LLM 0 calls) and write the pending-merge
      queue to _System/audit/consolidate-queue.json. brain-health surfaces it
      at SessionStart; the live session merges clusters itself (Read/Write,
      no claude -p children).
  --auto           — UNATTENDED fallback (capped at AUTO_MAX_CALLS claude
      calls/run). For every cluster: consolidate via claude with a quality
      gate (6 rules, self-assessed). Pass -> apply. "single_topic" fail ->
      split finer and retry. Other fail -> hold (logged). Writes an audit
      receipt to _System/audit/. --dry-run previews without writing.

Quality rules (the schema the human curates; everything else is automatic):
  complete · coherent · current · single_topic · format_ok · lean
  See _System/consolidation-quality.md.

Never deletes — consolidated fragments move to <category>/archived/<year>/.
"""
from __future__ import annotations

import argparse
import datetime
import itertools
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
from _brain_lib import open_db, BRAIN_VAULT, INDEX_PATH, EMBED_DIM  # noqa: E402

JOURNAL = ("Decisions", "Mistakes", "Patterns")
HOOKS_DIR = Path(__file__).parent
CLAUDE_BIN = os.environ.get("CLAUDE_DISTILL_CLAUDE_BIN", "claude")
CLAUDE_BIN = shutil.which(CLAUDE_BIN) or CLAUDE_BIN  # resolve .CMD shim on Windows
INGEST_MODEL = os.environ.get("BRAIN_CONSOLIDATE_MODEL", "claude-sonnet-4-6")
AUTO_MAX_DEPTH = int(os.environ.get("BRAIN_CONSOLIDATE_AUTO_DEPTH", "3"))
# 런당 claude 총 호출 상한. 깊이 제한은 클러스터당이라 총량을 못 막는다 —
# 백로그가 크면 분할 재시도가 호출을 수백 회로 불려 사용 한도를 태운다
# (2026-07-19 한 기기에서: 자식 콘솔 창 ~1000개, 구독 한도 소진).
AUTO_MAX_CALLS = int(os.environ.get("BRAIN_CONSOLIDATE_MAX_CALLS", "80"))
# --queue가 쓰는 통합 대기열. per-device 런타임(_System/audit/)이라 동기화 안 됨.
QUEUE_PATH_REL = Path("_System") / "audit" / "consolidate-queue.json"
# --weekly 하이브리드 분기점: 클러스터가 이 수 이하면 평시 부하로 보고 기존
# 무인 --auto(상한 내 소수 호출)로 처리, 초과하면 백로그 상황 — LLM을 아예
# 안 부르고 대기열만 써서 세션이 이어받게 한다 (2026-07-19 설계).
UNATTENDED_MAX_CLUSTERS = int(
    os.environ.get("BRAIN_CONSOLIDATE_UNATTENDED_MAX", "12"))
AUTO_COS_STEP = float(os.environ.get("BRAIN_CONSOLIDATE_AUTO_STEP", "0.08"))
AUTO_BASE_COS = float(os.environ.get("BRAIN_CONSOLIDATE_AUTO_COS", "0.60"))
# Objective cohesion floor: a group larger than TRUST_SIZE whose min pairwise
# cosine is below HARD_COS is split before trusting the LLM's self-assessment
# (self-assess alone rated a 21-file mixed blob single_topic=true).
AUTO_HARD_COS = float(os.environ.get("BRAIN_CONSOLIDATE_HARD_COS", "0.35"))
AUTO_TRUST_SIZE = int(os.environ.get("BRAIN_CONSOLIDATE_TRUST_SIZE", "3"))
FM_TAGS_RE = re.compile(r"^---\n.*?\btags?\b\s*:\s*(.*?)(?:\n[a-zA-Z_]|\n---)",
                        re.DOTALL)
FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)

QUALITY_KEYS = ("complete", "coherent", "current", "single_topic",
                "format_ok", "lean")

INGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "human-readable page title"},
        "slug": {"type": "string",
                 "description": "kebab-case ascii filename without .md"},
        "body": {"type": "string",
                 "description": "full markdown body WITHOUT frontmatter; the "
                                "current consolidated truth of the topic"},
        "supersedes_note": {"type": "string",
                            "description": "one line: what was merged/corrected"},
        "quality": {
            "type": "object",
            "description": "통합 직후 스스로 평가한 6개 품질 룰 (정직하게)",
            "properties": {
                "complete": {"type": "boolean",
                             "description": "원본 핵심 사실·노하우 손실 없이 통합"},
                "coherent": {"type": "boolean", "description": "자기모순 없음"},
                "current": {"type": "boolean",
                            "description": "모순은 최신 우선 정리, 폐기 명시"},
                "single_topic": {"type": "boolean",
                                 "description": "한 주제로 응집(잡탕이면 false)"},
                "format_ok": {"type": "boolean",
                              "description": "제목구조·한국어톤·재사용 how-to"},
                "lean": {"type": "boolean",
                         "description": "해시·경로·스택트레이스 덤프 없음"},
                "issues": {"type": "string",
                           "description": "미달 항목 설명(전부 통과면 빈 문자열)"},
            },
            "required": list(QUALITY_KEYS),
        },
    },
    "required": ["title", "slug", "body", "quality"],
}


# ---- detect (read-only) ----------------------------------------------------

def load_title_vectors(conn, categories):
    qmarks = ",".join("?" * len(categories))
    rows = conn.execute(
        f"""SELECT c.file_path, c.id FROM chunks c
            JOIN files f ON f.path = c.file_path
            WHERE c.chunk_idx = 0 AND f.category IN ({qmarks})""",
        categories,
    ).fetchall()
    vecs = {}
    for fp, cid in rows:
        if "archived" in fp.replace("\\", "/").split("/"):
            continue
        r = conn.execute("SELECT embedding FROM chunks_vec WHERE rowid = ?",
                         (cid,)).fetchone()
        if r and r[0]:
            v = struct.unpack(f"{EMBED_DIM}f", r[0])
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs[fp] = [x / n for x in v]
    return vecs


def slug_tokens(fp):
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", Path(fp).stem)
    return {t for t in re.split(r"[-_]", stem.lower()) if len(t) > 2}


def file_tags(fp):
    try:
        head = (BRAIN_VAULT / fp).read_text(encoding="utf-8")[:800]
    except Exception:
        return []
    m = FM_TAGS_RE.search(head)
    if not m:
        return []
    return [t for t in re.findall(r"[\w가-힣\-]+", m.group(1)) if t != "tags"]


def cluster(vecs, thr):
    files = list(vecs)
    parent = {f: f for f in files}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(files)):
        vi = vecs[files[i]]
        for j in range(i + 1, len(files)):
            if sum(a * b for a, b in zip(vi, vecs[files[j]])) >= thr:
                ra, rb = find(files[i]), find(files[j])
                if ra != rb:
                    parent[ra] = rb

    groups = {}
    for f in files:
        groups.setdefault(find(f), []).append(f)
    return sorted(groups.values(), key=len, reverse=True)


def detect_clusters(conn, cats, thr, min_size):
    vecs = load_title_vectors(conn, cats)
    clusters = [c for c in cluster(vecs, thr) if len(c) >= min_size]
    return vecs, clusters


def min_pairwise_cos(members, vecs):
    if len(members) < 2:
        return 1.0
    lo = 1.0
    for a, b in itertools.combinations(members, 2):
        lo = min(lo, sum(x * y for x, y in zip(vecs[a], vecs[b])))
    return lo


# ---- consolidate (claude -p + quality gate) --------------------------------

def fragment_body(fp):
    txt = (BRAIN_VAULT / fp).read_text(encoding="utf-8")
    return FRONTMATTER_RE.sub("", txt, count=1).strip()


class ClaudeBudgetExhausted(Exception):
    """런당 호출 상한(AUTO_MAX_CALLS) 도달 — do_auto가 잡아 남은 작업을 이월한다."""


_claude_calls = 0


def call_claude(prompt, schema, model, effort, timeout):
    global _claude_calls
    if _claude_calls >= AUTO_MAX_CALLS:
        raise ClaudeBudgetExhausted(f"claude 호출 상한({AUTO_MAX_CALLS}회) 도달")
    _claude_calls += 1
    # --strict-mcp-config + 빈 mcp-config: 자식 헤드리스 세션이 MCP·채널 플러그인을
    # 부팅하지 않게 차단. Discord 플러그인이 부팅되면 호출 1회당 봇 게이트웨이 세션
    # 1회를 소모 — 대량 런이 24h 한도(1000회)를 태워 봇을 오프라인시킨다 (2026-07-19).
    cmd = [CLAUDE_BIN, "-p", "--model", model, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--effort", effort,
           "--disable-slash-commands", "--no-session-persistence",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    env = os.environ.copy()
    env["CLAUDE_DISTILL_CHILD"] = "1"  # stop child Stop-hook re-distilling
    try:
        # CREATE_NO_WINDOW: Windows에서 자식마다 콘솔 창이 뜨는 것 차단
        # (POSIX에서는 속성이 없어 0 — 무동작).
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, env=env,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print(f"claude call failed: {e!r}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"claude rc={proc.returncode}; stderr={proc.stderr[:400]!r}",
              file=sys.stderr)
        return None
    try:
        wrapper = json.loads((proc.stdout or "").strip())
    except Exception as e:
        print(f"wrapper parse failed: {e!r}", file=sys.stderr)
        return None
    s = wrapper.get("structured_output")
    if isinstance(s, dict):
        return s
    rt = wrapper.get("result", "")
    if isinstance(rt, str):
        rt = re.sub(r"^```(?:json)?\s*", "", rt.strip())
        rt = re.sub(r"\s*```$", "", rt)
        try:
            return json.loads(rt)
        except Exception:
            pass
    return None


def build_ingest_prompt(members):
    parts = [
        "당신은 Karpathy LLM Wiki를 유지하는 사서다. 아래 저널 조각들을 "
        "'현재의 진실'을 담은 위키 페이지 하나로 통합하라.",
        "- 모순은 최신 조각(날짜 큰 것) 우선. 대체된 주장은 supersedes_note에 한 줄.",
        "- 한국어로. body는 frontmatter 없는 markdown. 재사용 how-to/원칙 위주.",
        "- slug는 kebab-case 영문.",
        "",
        "통합 후 6개 품질 룰을 스스로 정직하게 평가해 quality에 기록하라:",
        "  complete: 원본 핵심이 손실 없이 들어갔나",
        "  coherent: 자기모순 없나",
        "  current: 모순을 최신 우선 정리하고 폐기된 건 명시했나",
        "  single_topic: 한 주제로 응집됐나 — 여러 주제가 섞였으면 false (정직히)",
        "  format_ok: 제목구조·한국어톤·재사용 how-to 형식인가",
        "  lean: 커밋해시·파일경로·스택트레이스 등 형상관리 덤프가 없나",
        "single_topic이 false면 시스템이 더 잘게 나눠 재시도하니 솔직히 false로 표시하라.",
        "",
        f"조각 {len(members)}개:",
    ]
    for i, fp in enumerate(sorted(members), 1):
        mo = re.match(r"(\d{4}-\d{2}-\d{2})", Path(fp).stem)
        parts.append(f"\n--- [{i}] {mo.group(1) if mo else '?'}  {fp} ---")
        parts.append(fragment_body(fp))
    return "\n".join(parts)


def consolidate_once(members, effort):
    return call_claude(build_ingest_prompt(members), INGEST_SCHEMA,
                       INGEST_MODEL, effort, 300)


def quality_pass(q):
    return bool(q) and all(q.get(k) for k in QUALITY_KEYS)


def build_page_text(draft, members):
    slug = re.sub(r"[^a-z0-9\-]+", "-", draft["slug"].lower()).strip("-")
    today = datetime.date.today().isoformat()
    year = today[:4]
    note = draft.get("supersedes_note", "")
    front = (f"---\ntitle: {draft['title']}\ntype: knowledge\nstatus: active\n"
             f"updated: {today}\n---\n\n")
    full = front + draft["body"].strip() + (
        f"\n\n## 변경 이력\n\n- **{today}** Ingest — {len(members)}개 저널 조각 "
        f"통합. {note} 원본은 `<category>/archived/{year}/`.\n")
    return slug, full, year


def write_page(slug, full, members, year, reindex=True):
    (BRAIN_VAULT / f"Knowledge/{slug}.md").write_text(full, encoding="utf-8")
    # NOTE: do NOT append to _index.md here. Since the 2026-06-11 catalog
    # policy, _index.md stays curated prose + a _catalog.md pointer, and the
    # auto-generated _catalog.md (gitignored) already lists every note. The old
    # Karpathy 'update index' append (index_add) leaked per-device note links
    # into the tracked/shared skeleton, forcing a manual revert every audit
    # (6/24, 6/30). Removed 2026-06-30.
    for fp in members:
        cat = fp.replace("\\", "/").split("/")[0]
        d = BRAIN_VAULT / cat / "archived" / year
        d.mkdir(parents=True, exist_ok=True)
        (BRAIN_VAULT / fp).rename(d / Path(fp).name)
    if reindex:
        subprocess.run([sys.executable, str(HOOKS_DIR / "brain-reindex.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)


def do_ingest(conn, members, apply, effort):
    print(f"## Ingest {len(members)}개 ({'APPLY' if apply else 'dry-run'})",
          file=sys.stderr)
    draft = consolidate_once(members, effort)
    if not draft:
        print("통합 실패 (claude 무응답).", file=sys.stderr)
        return 1
    q = draft.get("quality", {})
    slug, full, year = build_page_text(draft, members)
    ok = quality_pass(q)
    print("=" * 60)
    print(f"제안: Knowledge/{slug}.md   품질 {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(f"  미달: {[k for k in QUALITY_KEYS if not q.get(k)]} — {q.get('issues','')}")
    print("=" * 60)
    print(full)
    print("=" * 60)
    if not apply:
        print("\n(dry-run)")
        return 0
    if not ok:
        print(f"\n품질 미달 → 적용 보류. (단일주제 아니면 --auto가 세분화 처리)")
        return 1
    write_page(slug, full, members, year)
    print(f"\n✅ wrote Knowledge/{slug}.md + archived {len(members)}")
    return 0


# ---- auto (unattended, quality-gated, recursive split) ---------------------

def _split_retry(conn, members, vecs, effort, apply, thr, depth, ledger):
    sub_thr = thr + AUTO_COS_STEP
    subs = [s for s in cluster({m: vecs[m] for m in members}, sub_thr)
            if len(s) >= 2]
    if not subs:
        return False
    for s in subs:
        _auto_recur(conn, s, vecs, effort, apply, sub_thr, depth + 1, ledger)
    return True


def _auto_recur(conn, members, vecs, effort, apply, thr, depth, ledger):
    # 조각은 실행 중에 이동·아카이브될 수 있다 (2026-07-18: 중복 정리가 도중에
    # 조각 690개를 옮겨서 fragment_body의 FileNotFoundError로 70분 런 전체가
    # 죽었다). 없어진 조각은 빼고 진행하고, 통합할 짝이 안 남으면 스킵한다 —
    # 클러스터 하나의 문제가 런 전체를 무너뜨리지 않게 (fail-open).
    alive = [m for m in members if (BRAIN_VAULT / m).exists()]
    gone = [m for m in members if m not in alive]
    if gone:
        ledger.append(("hold", gone, "조각 소실(이동/아카이브됨) — 이번 런에서 제외"))
    if len(alive) < 2:
        return
    members = alive
    cohesion = min_pairwise_cos(members, vecs)
    # Objective floor first: split big+incoherent groups BEFORE spending a
    # claude call (self-assessment runs too lenient on mixed blobs).
    if (len(members) > AUTO_TRUST_SIZE and cohesion < AUTO_HARD_COS
            and depth < AUTO_MAX_DEPTH):
        if _split_retry(conn, members, vecs, effort, apply, thr, depth, ledger):
            return

    draft = consolidate_once(members, effort)
    if not draft:
        ledger.append(("fail", members, "claude 무응답"))
        return
    q = draft.get("quality", {})
    if quality_pass(q):
        slug, full, year = build_page_text(draft, members)
        if apply:
            write_page(slug, full, members, year, reindex=False)
        ledger.append(("merged", members, slug))
        return
    # single_topic fail → split finer and retry
    if not q.get("single_topic") and depth < AUTO_MAX_DEPTH:
        if _split_retry(conn, members, vecs, effort, apply, thr, depth, ledger):
            return
    # other fails or cannot split further → hold (logged, fragments stay)
    bad = [k for k in QUALITY_KEYS if not q.get(k)]
    ledger.append(("hold", members, f"{bad}: {q.get('issues','')}"))


def do_auto(conn, vecs, clusters, dry, effort):
    print(f"## auto-consolidate ({'preview' if dry else 'APPLY'}) — 품질 게이트 무인",
          file=sys.stderr)
    ledger = []
    budget_hit = False
    for i, c in enumerate(clusters):
        try:
            _auto_recur(conn, c, vecs, effort, not dry, AUTO_BASE_COS, 0, ledger)
        except ClaudeBudgetExhausted as e:
            # 진행 중이던 클러스터 포함 남은 전부를 보류로 이월 — 조각은 그대로
            # 남으므로 다음 주간 런이 이어서 처리한다.
            for rest in clusters[i:]:
                ledger.append(("hold", rest, f"{e} — 다음 런으로 이월"))
            budget_hit = True
            break
    if not dry and any(s == "merged" for s, _, _ in ledger):
        subprocess.run([sys.executable, str(HOOKS_DIR / "brain-reindex.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)

    merged = [(m, i) for s, m, i in ledger if s == "merged"]
    held = [(m, i) for s, m, i in ledger if s == "hold"]
    failed = [(m, i) for s, m, i in ledger if s == "fail"]

    lines = [f"# consolidate 영수증 ({'preview' if dry else 'applied'}) "
             f"{datetime.date.today().isoformat()}",
             f"통합 {len(merged)} / 보류 {len(held)} / 실패 {len(failed)}",
             f"claude 호출 {_claude_calls}회 / 상한 {AUTO_MAX_CALLS}회"
             + (" — 상한 도달로 조기 종료" if budget_hit else ""), ""]
    for m, slug in merged:
        lines.append(f"- ✅ 통합 → Knowledge/{slug}.md  ({len(m)}개 조각)")
    for m, iss in held:
        common = (set.intersection(*[slug_tokens(x) for x in m])
                  if len(m) > 1 else set())
        lines.append(f"- ⏸ 보류 [{len(m)}개] "
                     f"{', '.join(sorted(common)) or '혼합'} — {iss}")
    for m, iss in failed:
        lines.append(f"- ✗ 실패 [{len(m)}개] — {iss}")
    receipt = "\n".join(lines)
    print("\n" + receipt)

    if not dry:
        yr, wk, _ = datetime.date.today().isocalendar()
        audit = BRAIN_VAULT / "_System" / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        (audit / f"consolidate-{yr}-W{wk:02d}.md").write_text(
            receipt + "\n", encoding="utf-8")
        print(f"\naudit: _System/audit/consolidate-{yr}-W{wk:02d}.md")
        # P2: decay sweep runs AFTER consolidation so clusterable journals
        # converge into Knowledge before low-retention ones get archived.
        # Best-effort — decay failure must not fail the consolidate run.
        decay = HOOKS_DIR / "brain-decay.py"
        if decay.exists():
            try:
                subprocess.run([sys.executable, str(decay), "--apply"],
                               capture_output=True, text=True,
                               encoding="utf-8", timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
                print("decay: swept (receipt in _System/audit/)")
            except Exception as e:
                print(f"decay: skipped ({e!r})", file=sys.stderr)
        # P0 (2026-07-13): push any scope:shared Knowledge/Memory changes to
        # the remote after this run. Runs last (after decay) -- order doesn't
        # matter functionally (decay only touches journals, sync only touches
        # Knowledge/Memory) but this matches "chain more steps at the end."
        # Best-effort, same as decay -- a push failure must not fail consolidate.
        sync = HOOKS_DIR / "brain-sync.py"
        if sync.exists():
            try:
                sp = subprocess.run([sys.executable, str(sync), "--push"],
                                    capture_output=True, text=True,
                                    encoding="utf-8", timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
                if sp.returncode == 0:
                    print("sync: shared content pushed (or nothing to push)")
                else:
                    print(f"sync: needs attention ({sp.stderr.strip()[:200]})",
                          file=sys.stderr)
            except Exception as e:
                print(f"sync: skipped ({e!r})", file=sys.stderr)
    return 0


# ---- queue (cluster only, LLM 0회 — 세션이 이어받는다) ----------------------

def do_queue(vecs, clusters):
    """클러스터링 결과를 대기열 파일로 저장만 한다. claude 호출 0회.

    처리 주체는 다음 세션의 claude (brain-health가 대기 건수를 알리고,
    세션이 _System/consolidate.md §Session processing 계약대로 직접 통합).
    빈 클러스터여도 파일을 써서 brain-health가 '대기 0'을 알 수 있게 한다.
    """
    payload = {
        "generated": datetime.date.today().isoformat(),
        "singletons": len(vecs) - sum(len(c) for c in clusters),
        "clusters": [],
    }
    for members in clusters:
        common = set.intersection(*[slug_tokens(m) for m in members])
        tagc = Counter(t for m in members for t in file_tags(m))
        payload["clusters"].append({
            "size": len(members), "common_slug": sorted(common),
            "top_tags": [t for t, _ in tagc.most_common(4)],
            "files": sorted(members)})
    out = BRAIN_VAULT / QUEUE_PATH_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    n_frag = sum(len(c) for c in clusters)
    print(f"통합 대기열 갱신: {len(clusters)}클러스터 / {n_frag}조각 → "
          f"{QUEUE_PATH_REL}")
    return 0


def do_weekly(conn, vecs, clusters, effort):
    """주간 스케줄 진입점 (하이브리드, 2026-07-19).

    평시(클러스터 ≤ UNATTENDED_MAX_CLUSTERS)는 기존 무인 --auto로 그 자리에서
    끝내고, 백로그(초과)면 LLM 호출 없이 대기열만 남겨 세션이 이어받게 한다.
    문제 없을 때의 자동화 편의와 문제 상황의 폭주 차단을 동시에 잡는 분기.
    """
    if len(clusters) <= UNATTENDED_MAX_CLUSTERS:
        print(f"weekly: {len(clusters)}클러스터 ≤ 임계 {UNATTENDED_MAX_CLUSTERS}"
              f" — 무인 처리", file=sys.stderr)
        rc = do_auto(conn, vecs, clusters, dry=False, effort=effort)
        # 무인으로 다 처리했으면 이전 주의 대기열은 낡은 정보 — 치운다.
        stale = BRAIN_VAULT / QUEUE_PATH_REL
        if stale.exists():
            stale.unlink()
        return rc
    print(f"weekly: {len(clusters)}클러스터 > 임계 {UNATTENDED_MAX_CLUSTERS}"
          f" — 백로그로 판단, LLM 호출 없이 대기열 생성 (세션 처리로 이월)",
          file=sys.stderr)
    return do_queue(vecs, clusters)


# ---- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="brain LLM-wiki consolidation")
    ap.add_argument("--threshold", type=float, default=0.60)
    ap.add_argument("--category", help="restrict to one journal category")
    ap.add_argument("--min-size", type=int, default=2)
    ap.add_argument("--ingest", type=int, metavar="N",
                    help="consolidate cluster N (1-based)")
    ap.add_argument("--apply", action="store_true",
                    help="with --ingest: write+archive+reindex if quality passes")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--queue", action="store_true",
                    help="cluster only (LLM 0회) and write the pending queue "
                         "for in-session processing")
    ap.add_argument("--weekly", action="store_true",
                    help="scheduled hybrid: small load -> unattended --auto, "
                         "backlog -> --queue for in-session processing")
    ap.add_argument("--auto", action="store_true",
                    help="unattended quality-gated consolidation of all clusters")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --auto: preview without writing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not INDEX_PATH.exists():
        print(f"index missing: {INDEX_PATH}", file=sys.stderr)
        return 2

    cats = (args.category,) if args.category else JOURNAL
    conn = open_db(create=False)
    vecs, clusters = detect_clusters(conn, cats, args.threshold, args.min_size)

    if args.weekly:
        return do_weekly(conn, vecs, clusters, args.effort)

    if args.queue:
        return do_queue(vecs, clusters)

    if args.auto:
        return do_auto(conn, vecs, clusters, args.dry_run, args.effort)

    if args.ingest is not None:
        if not (1 <= args.ingest <= len(clusters)):
            print(f"cluster {args.ingest} 없음 (1..{len(clusters)})", file=sys.stderr)
            return 2
        return do_ingest(conn, clusters[args.ingest - 1], args.apply, args.effort)

    # detect mode
    singletons = len(vecs) - sum(len(c) for c in clusters)
    payload = []
    for members in clusters:
        common = set.intersection(*[slug_tokens(m) for m in members])
        tagc = Counter(t for m in members for t in file_tags(m))
        payload.append({"size": len(members), "common_slug": sorted(common),
                        "top_tags": [t for t, _ in tagc.most_common(4)],
                        "files": sorted(members)})
    if args.json:
        print(json.dumps({"threshold": args.threshold, "journal_files": len(vecs),
                          "clusters": payload, "singletons": singletons},
                         ensure_ascii=False, indent=2))
        return 0
    print("## brain-consolidate: 통합 후보 주제군 (저널 layer)")
    print(f"cosine>={args.threshold}, 저널 {len(vecs)}파일 중 "
          f"{len(vecs)-singletons}개가 {len(clusters)}개 클러스터로 묶임 "
          f"(단독 {singletons}개)\n")
    for i, c in enumerate(payload, 1):
        label = ", ".join(c["common_slug"]) or ", ".join(c["top_tags"]) or "(라벨 없음)"
        print(f"[{i}] {c['size']}개 — 공통: {label}")
        for f in c["files"]:
            print(f"    - {f}")
        print()
    print("무인 통합: python brain-consolidate.py --auto   (미리보기 --dry-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
