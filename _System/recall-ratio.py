"""자동 회상: 주입한 문서 중 같은 세션이 실제로 연 비율(분류·순위·날짜별). 2026-10-29 첫 판독. 점수대별은 recall-replay.py."""
import sqlite3, collections, os, sys
db = sqlite3.connect(os.path.expanduser("~/.claude/data/brain-usage.sqlite"))
norm = lambda p: p.replace("\\", "/")

inj = [(s, norm(p), c, ts, r, q) for p, c, ts, s, q, r in db.execute("select path,category,ts,session_id,query_hash,rank from injections")]
rd = [(s, norm(p), c, ts, t) for p, c, ts, s, t in db.execute("select path,category,ts,session_id,tool from reads")]
print("injections", len(inj), "range", min(x[3] for x in inj), "~", max(x[3] for x in inj))
print("reads", len(rd), "range", min(x[3] for x in rd), "~", max(x[3] for x in rd))

first_read = {}
for s, p, c, ts, t in rd:
    k = (s, p)
    if k not in first_read or ts < first_read[k]: first_read[k] = ts

# 주입 단위(세션·경로 첫 주입)로 그 뒤 같은 세션에서 읽었는지
first_inj = {}
for s, p, c, ts, r, q in inj:
    k = (s, p)
    if k not in first_inj or ts < first_inj[k][0]: first_inj[k] = (ts, c, r)

def week(ts): return ts[:10]
by_day = collections.defaultdict(lambda: [0, 0])
by_cat = collections.defaultdict(lambda: [0, 0])
by_rank = collections.defaultdict(lambda: [0, 0])
for (s, p), (ts, c, r) in first_inj.items():
    hit = (s, p) in first_read and first_read[(s, p)] >= ts
    for d, key in ((by_day, ts[:10]), (by_cat, c), (by_rank, r)):
        d[key][0] += 1; d[key][1] += hit
tot = sum(v[0] for v in by_cat.values()); hits = sum(v[1] for v in by_cat.values())
print(f"\n(session,path) injected {tot}, read after {hits} = {hits/tot:.1%}")
print("\nby category"); [print(" ", k, v, f"{v[1]/v[0]:.1%}") for k, v in sorted(by_cat.items(), key=lambda x: -x[1][0])]
print("\nby rank"); [print(" ", k, v, f"{v[1]/v[0]:.1%}") for k, v in sorted(by_rank.items(), key=lambda x: (x[0] is None, x[0]))]
print("\nby day"); [print(" ", k, v, f"{v[1]/v[0]:.1%}") for k, v in sorted(by_day.items())]

# 세션 수, 주입이 있었던 세션 중 하나라도 읽은 세션
sess = collections.defaultdict(lambda: [0, 0])
for (s, p), (ts, c, r) in first_inj.items():
    sess[s][0] += 1; sess[s][1] += ((s, p) in first_read and first_read[(s, p)] >= ts)
print("\nsessions with injections", len(sess), "sessions that read >=1 injected", sum(1 for v in sess.values() if v[1]))
print("avg injected paths per session", round(tot / len(sess), 1))

# 주입 없이 읽은 것(자체 검색·프로토콜 읽기)
proto = lambda p: p.startswith("Memory/resume/") or p in ("Memory/session-resume.md",) or p.endswith("session-start-protocol.md")
unprompted = [(s, p) for (s, p), ts in first_read.items() if not ((s, p) in first_inj and first_inj[(s, p)][0] <= ts)]
print("\nreads (session,path) total", len(first_read), "without prior injection", len(unprompted),
      "of which protocol files", sum(proto(p) for s, p in unprompted))
c = collections.Counter(p for s, p in unprompted if not proto(p))
print("top unprompted non-protocol reads:"); [print("  ", n, p) for p, n in c.most_common(12)]
c = collections.Counter(p for (s, p) in first_inj)
print("\nmost injected paths:")
for p, n in c.most_common(12):
    r = sum(1 for (s, q) in first_inj if q == p and (s, q) in first_read and first_read[(s, q)] >= first_inj[(s, q)][0])
    print("  ", n, r, p)
