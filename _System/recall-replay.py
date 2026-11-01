"""과거 세션의 실제 프롬프트를 지금 관문(pick)에 다시 넣어, 점수대별로 그 세션이 실제로 열었는지 본다.

자동 회상 관문(brain-recall-inject.py IDF_SUM_MIN)을 정할 때 쓴다. 2026-10-29 첫 판독.
실행: python -X utf8 _System/recall-replay.py  (몇 분 걸린다. 전체 비율은 recall-ratio.py)
"""
import importlib.util, json, os, sqlite3, glob, collections, sys
from pathlib import Path

HOOKS = Path.home() / ".claude" / "hooks"
spec = importlib.util.spec_from_file_location("inj", HOOKS / "brain-recall-inject.py")
inj = importlib.util.module_from_spec(spec); spec.loader.exec_module(inj)
inj.MAX_ITEMS = 10
bs = inj._load_search()

db = sqlite3.connect(os.path.expanduser("~/.claude/data/brain-usage.sqlite"))
norm = lambda p: p.replace("\\", "/")
first_inj, first_read = {}, {}
for p, ts, s in db.execute("select path,ts,session_id from injections"):
    k = (s, norm(p)); first_inj[k] = min(first_inj.get(k, ts), ts)
for p, ts, s in db.execute("select path,ts,session_id from reads"):
    k = (s, norm(p)); first_read[k] = min(first_read.get(k, ts), ts)
sessions = {s for s, _ in first_inj}

vault = Path(bs.BRAIN_VAULT)  # 기기마다 볼트 위치가 다르다
files = {Path(f).stem: f for f in glob.glob(str(Path.home() / ".claude/projects/*/*.jsonl"))}
rows = []  # (idf_sum, n_head, n_matched, rank, read)
seen = set()
for s in sessions:
    f = files.get(s)
    if not f: continue
    for line in open(f, encoding="utf-8"):
        try: r = json.loads(line)
        except Exception: continue
        if r.get("type") != "user": continue
        c = r["message"]["content"]
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        if not text.strip() or text.startswith("This session is being continued"): continue
        try: hits = inj.pick(text, bs)
        except Exception: continue
        for rank, h in enumerate(hits, 1):
            rel = norm(str(Path(h["abs_path"]).relative_to(vault))) if h["abs_path"].lower().startswith(str(vault).lower()) else None
            if not rel or (s, rel) in seen or (s, rel) not in first_inj: continue
            seen.add((s, rel))
            read = (s, rel) in first_read and first_read[(s, rel)] >= first_inj[(s, rel)]
            rows.append((h["idf_sum"], len(h["in_head"]), len(h["matched"]), rank, read, rel))
print("matched injections", len(rows), "read", sum(r[4] for r in rows))
def show(name, key, buckets):
    print("\n" + name)
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= key(r) < hi]
        if sub: print(f"  {lo:>5}~{hi:<5} n={len(sub):4d} read={sum(r[4] for r in sub):3d} {sum(r[4] for r in sub)/len(sub):.1%}")
show("idf_sum", lambda r: r[0], [(0, 8), (8, 10), (10, 12), (12, 15), (15, 20), (20, 30), (30, 999)])
show("in_head count", lambda r: r[1], [(1, 2), (2, 3), (3, 99)])
show("matched count", lambda r: r[2], [(2, 3), (3, 4), (4, 5), (5, 99)])
out = Path(os.environ.get("TEMP", "/tmp")) / "recall-replay.json"
json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print("rows ->", out)
