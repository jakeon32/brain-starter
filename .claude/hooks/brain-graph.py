#!/usr/bin/env python
"""
brain-graph.py — 기억 연결망 계산 + 시각화 (Argo의 '기억 연결 밀도'를 우리 구조로)

brain-index.sqlite에 이미 들어 있는 청크 임베딩으로 **노트끼리의 유사도 링크**를 만든다.
새로 임베딩하지 않는다(인덱스 재사용) — 그래서 빠르고, 인덱스가 갱신되면 그래프도 따라 갱신된다.

두 종류의 링크를 함께 본다:
  - explicit : 본문에 사람이 쓴 [[wikilink]]
  - implicit : 임베딩 코사인 유사도가 임계 이상인 쌍 (= brain에 없던 관점)

사용:
    python brain-graph.py                 # 통계 요약(연결 밀도·고립 노트·허브)
    python brain-graph.py --html          # 3D 그래프를 brain 볼트에 생성/갱신
    python brain-graph.py --html <경로>   # 다른 곳에 생성
    python brain-graph.py --orphans       # 아무 데도 안 이어진 노트만
    python brain-graph.py --suggest       # 붙일 만한 [[링크]] 후보 (implicit인데 explicit이 없는 쌍)
    python brain-graph.py --node <slug>   # 특정 노트의 이웃

임계는 --min-sim (기본 0.55). 낮추면 링크가 폭증한다.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sqlite3
import struct
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# 한국어 Windows 콘솔은 cp949라 em-dash 하나에도 출력이 죽는다 — starter 검증에서 실측.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
# 이 스크립트는 brain 볼트(.claude/hooks/)에 살고 3기기가 git으로 공유한다.
# _brain_lib은 기기마다 설치된 ~/.claude/hooks/ 쪽을 쓴다 — 복제하면 표류한다.
sys.path.append(str(Path.home() / ".claude" / "hooks"))
from _brain_lib import BRAIN_VAULT, INDEX_PATH, open_db  # noqa: E402

DEFAULT_MIN_SIM = 0.55
DEFAULT_TOP_K = 6          # 노트당 유지할 최대 이웃 수 (허브가 그래프를 삼키는 것 방지)

# 기본 출력은 brain 볼트 안 — 볼트를 열면 항상 최신 연결망을 볼 수 있게.
# ★_System/ 아래지만 .gitignore로 제외한다: HTML에 노트 제목이 전부 들어가는데
#   brain repo의 설계 의도는 "노트 내용은 기기별 로컬, 원격엔 뼈대만"이다.
DEFAULT_HTML = BRAIN_VAULT / "_System" / "brain-graph.html"


# ---- 로딩 -------------------------------------------------------------------

def load_chunk_vectors(conn: sqlite3.Connection):
    """chunks + chunks_vec을 읽어 파일별 평균 벡터(문서 벡터)를 만든다."""
    rows = conn.execute(
        "SELECT c.id, c.file_path, v.embedding "
        "FROM chunks c JOIN chunks_vec v ON v.rowid = c.id"
    ).fetchall()
    if not rows:
        return {}
    per_file: dict[str, list[list[float]]] = defaultdict(list)
    for _cid, fpath, blob in rows:
        n = len(blob) // 4
        vec = struct.unpack(f"{n}f", blob)
        per_file[fpath].append(vec)
    doc: dict[str, list[float]] = {}
    for fpath, vecs in per_file.items():
        dim = len(vecs[0])
        acc = [0.0] * dim
        for v in vecs:
            for i in range(dim):
                acc[i] += v[i]
        norm = math.sqrt(sum(x * x for x in acc)) or 1.0
        doc[fpath] = [x / norm for x in acc]   # 정규화 → 내적 = 코사인
    return doc


def note_meta(path: str) -> dict:
    """파일 경로에서 카테고리·제목·날짜를 뽑는다. 본문은 읽지 않는다(빠르게)."""
    p = Path(path)
    parts = p.parts
    category = "기타"
    for c in ("Decisions", "Mistakes", "Patterns", "Knowledge", "Memory", "Sessions", "Agents"):
        if c in parts:
            category = c
            break
    stem = p.stem
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)$", stem)
    date, title = (m.group(1), m.group(2)) if m else ("", stem)
    return {"category": category, "date": date, "title": title.replace("-", " "), "stem": stem}


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")


def load_explicit_links(paths: list[str]) -> dict[str, set[str]]:
    """본문의 [[wikilink]]를 읽어 파일→파일 매핑으로 바꾼다."""
    by_stem: dict[str, str] = {}
    for p in paths:
        by_stem[Path(p).stem.lower()] = p
        # 날짜 접두어를 뺀 이름으로도 찾을 수 있게
        m = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)$", Path(p).stem)
        if m:
            by_stem.setdefault(m.group(1).lower(), p)
    out: dict[str, set[str]] = defaultdict(set)
    for p in paths:
        fp = BRAIN_VAULT / p if not Path(p).is_absolute() else Path(p)
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for raw in WIKILINK_RE.findall(text):
            key = raw.strip().lower().replace("_", "-")
            tgt = by_stem.get(key)
            if tgt and tgt != p:
                out[p].add(tgt)
                out[tgt].add(p)
    return out


# ---- 그래프 -----------------------------------------------------------------

def build_graph(doc_vecs: dict[str, list[float]], min_sim: float, top_k: int):
    """전 쌍 코사인 유사도 → 상위 이웃만 남긴 무향 그래프."""
    paths = sorted(doc_vecs)
    n = len(paths)
    neighbors: dict[str, list[tuple[str, float]]] = {p: [] for p in paths}
    for i in range(n):
        vi = doc_vecs[paths[i]]
        scored = []
        for j in range(n):
            if i == j:
                continue
            vj = doc_vecs[paths[j]]
            s = sum(a * b for a, b in zip(vi, vj))
            if s >= min_sim:
                scored.append((paths[j], s))
        scored.sort(key=lambda x: -x[1])
        neighbors[paths[i]] = scored[:top_k]
    # 무향화 — 한쪽에만 있어도 링크로 인정
    edges: dict[tuple[str, str], float] = {}
    for src, lst in neighbors.items():
        for dst, s in lst:
            key = (src, dst) if src < dst else (dst, src)
            edges[key] = max(edges.get(key, 0.0), s)
    return paths, edges


def component_sizes(paths: list[str], edges: dict) -> list[int]:
    """연결 요소 크기 — '지식이 하나로 이어져 있나'를 보는 지표."""
    adj = defaultdict(set)
    for (a, b) in edges:
        adj[a].add(b)
        adj[b].add(a)
    seen: set[str] = set()
    sizes = []
    for p in paths:
        if p in seen:
            continue
        stack, size = [p], 0
        seen.add(p)
        while stack:
            cur = stack.pop()
            size += 1
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        sizes.append(size)
    return sorted(sizes, reverse=True)


# ---- 출력 -------------------------------------------------------------------

def cmd_stats(paths, edges, explicit, args):
    deg = defaultdict(int)
    for (a, b) in edges:
        deg[a] += 1
        deg[b] += 1
    n = len(paths)
    e = len(edges)
    density = (2 * e) / (n * (n - 1)) if n > 1 else 0
    orphans = [p for p in paths if deg[p] == 0]
    sizes = component_sizes(paths, edges)
    ex_pairs = sum(len(v) for v in explicit.values()) // 2

    by_cat = defaultdict(lambda: [0, 0])
    for p in paths:
        c = note_meta(p)["category"]
        by_cat[c][0] += 1
        by_cat[c][1] += deg[p]

    print(f"기억 연결망 — {BRAIN_VAULT}")
    print(f"  노트 {n}개 · 유사도 링크 {e}개 (임계 {args.min_sim}, 노트당 최대 {args.top_k})")
    print(f"  사람이 쓴 [[링크]] {ex_pairs}쌍")
    print(f"  연결 밀도 {density*100:.2f}%  ·  노트당 평균 이웃 {2*e/n:.2f}개")
    print(f"  가장 큰 덩어리 {sizes[0] if sizes else 0}개 노트"
          f" ({(sizes[0]/n*100 if sizes else 0):.0f}%) · 덩어리 총 {len(sizes)}개")
    print(f"  고립 노트 {len(orphans)}개 ({len(orphans)/n*100:.0f}%)")
    print()
    print("  카테고리별 (노트 수 / 평균 이웃)")
    for c, (cnt, dsum) in sorted(by_cat.items(), key=lambda x: -x[1][0]):
        print(f"    {c:<10} {cnt:>4}개   평균 {dsum/cnt:.2f}")
    print()
    hubs = sorted(paths, key=lambda p: -deg[p])[:10]
    print("  가장 많이 이어진 노트 (허브 = 이 주제가 여러 작업에 걸쳐 있다)")
    for p in hubs:
        m = note_meta(p)
        print(f"    {deg[p]:>3}개  [{m['category']}] {m['title'][:60]}")


def cmd_orphans(paths, edges, args):
    deg = defaultdict(int)
    for (a, b) in edges:
        deg[a] += 1
        deg[b] += 1
    orphans = [p for p in paths if deg[p] == 0]
    print(f"고립 노트 {len(orphans)}개 — 아무 노트와도 유사도 {args.min_sim} 이상으로 안 이어짐")
    print("(주제가 유일하거나, 너무 짧아 맥락이 없거나, 잘못 분류된 것)")
    for p in sorted(orphans, key=lambda x: note_meta(x)["date"], reverse=True):
        m = note_meta(p)
        print(f"  {m['date'] or '        '}  [{m['category']:<9}] {m['title'][:70]}")


def cmd_suggest(paths, edges, explicit, args):
    """유사도는 높은데 사람이 [[링크]]를 안 단 쌍 = 붙일 후보."""
    cands = []
    for (a, b), s in edges.items():
        if b in explicit.get(a, set()):
            continue
        cands.append((s, a, b))
    cands.sort(reverse=True)
    print(f"[[링크]] 후보 {len(cands)}개 — 내용은 가까운데 서로 안 이어져 있음 (상위 {args.limit}개)")
    for s, a, b in cands[:args.limit]:
        ma, mb = note_meta(a), note_meta(b)
        print(f"\n  유사도 {s:.3f}")
        print(f"    [{ma['category']}] {ma['title'][:66]}")
        print(f"    [{mb['category']}] {mb['title'][:66]}")


def cmd_node(paths, edges, explicit, args):
    q = args.node.lower()
    hits = [p for p in paths if q in Path(p).stem.lower()]
    if not hits:
        print(f"'{args.node}' 와 맞는 노트가 없습니다.")
        return
    adj = defaultdict(list)
    for (a, b), s in edges.items():
        adj[a].append((b, s))
        adj[b].append((a, s))
    for p in hits[:5]:
        m = note_meta(p)
        print(f"\n[{m['category']}] {m['title']}  ({m['date']})")
        print(f"  {p}")
        nb = sorted(adj[p], key=lambda x: -x[1])
        if not nb:
            print("  이웃 없음 (고립)")
        for other, s in nb:
            mo = note_meta(other)
            mark = "[[링크됨]]" if other in explicit.get(p, set()) else "          "
            print(f"  {s:.3f} {mark} [{mo['category']}] {mo['title'][:56]}")


CAT_COLOR = {
    "Decisions": "#B8956A", "Patterns": "#5B9BD5", "Mistakes": "#D9694E",
    "Knowledge": "#6FB98F", "Memory": "#9B8AC4", "Sessions": "#7A8794",
    "Agents": "#C77DA6", "기타": "#8A8A8A",
}


def cmd_html(paths, edges, explicit, args):
    deg = defaultdict(int)
    for (a, b) in edges:
        deg[a] += 1
        deg[b] += 1
    idx = {p: i for i, p in enumerate(paths)}
    nodes = []
    for p in paths:
        m = note_meta(p)
        nodes.append({
            "id": idx[p], "t": m["title"][:70], "c": m["category"],
            "d": m["date"], "deg": deg[p],
        })
    links = []
    for (a, b), s in edges.items():
        links.append({
            "s": idx[a], "t": idx[b], "w": round(s, 3),
            "e": 1 if b in explicit.get(a, set()) else 0,
        })
    n = len(paths)
    e = len(edges)
    density = (2 * e) / (n * (n - 1)) * 100 if n > 1 else 0
    orphan = sum(1 for p in paths if deg[p] == 0)
    sizes = component_sizes(paths, edges)
    payload = json.dumps({"nodes": nodes, "links": links}, ensure_ascii=False)
    colors = json.dumps(CAT_COLOR, ensure_ascii=False)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    out = Path(args.html)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_HTML_TEMPLATE
                   .replace("__DATA__", payload)
                   .replace("__COLORS__", colors)
                   .replace("__N__", str(n))
                   .replace("__E__", str(e))
                   .replace("__DENSITY__", f"{density:.2f}")
                   .replace("__ORPHAN__", str(orphan))
                   .replace("__BIGGEST__", str(sizes[0] if sizes else 0))
                   .replace("__STAMP__", stamp)
                   .replace("__MINSIM__", str(args.min_sim)),
                   encoding="utf-8")
    print(f"wrote {out}  (노트 {n} · 링크 {e} · 밀도 {density:.2f}% · 고립 {orphan})")


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>brain 기억 연결망</title>
<style>
 *{margin:0;padding:0;box-sizing:border-box}
 body{background:#12151b;color:#e8e6e1;font-family:Pretendard,-apple-system,"Malgun Gothic",sans-serif;overflow:hidden}
 canvas{display:block;cursor:grab}
 .hud{position:fixed;top:16px;left:16px;z-index:5;font-size:12px;line-height:1.9;
      background:rgba(18,21,27,.86);border:1px solid #2a2f39;border-radius:10px;padding:14px 16px;max-width:330px}
 .hud h1{font-size:14px;font-weight:600;margin-bottom:8px}
 .hud b{color:#B8956A;font-weight:600}
 .hud .sub{color:#8b93a1;font-size:11px;margin-top:8px;line-height:1.7}
 .legend{position:fixed;bottom:16px;left:16px;z-index:5;font-size:11px;
      background:rgba(18,21,27,.86);border:1px solid #2a2f39;border-radius:10px;padding:11px 14px}
 .legend span{display:inline-flex;align-items:center;gap:6px;margin-right:12px}
 .legend i{width:9px;height:9px;border-radius:50%;display:inline-block}
 .tip{position:fixed;z-index:9;pointer-events:none;background:#1c212b;border:1px solid #39404d;
      border-radius:8px;padding:9px 12px;font-size:12px;max-width:340px;display:none;line-height:1.6}
 .tip .c{color:#8b93a1;font-size:11px}
 input[type=range]{width:100%;margin-top:6px}
</style></head><body>
<canvas id="cv"></canvas>
<div class="hud">
  <h1>기억 연결망</h1>
  노트 <b>__N__</b>개 · 링크 <b>__E__</b>개<br>
  연결 밀도 <b>__DENSITY__%</b> · 고립 <b>__ORPHAN__</b>개<br>
  가장 큰 덩어리 <b>__BIGGEST__</b>개<br>
  <div class="sub">
    선 = 내용이 가까운 노트끼리(유사도 __MINSIM__ 이상).<br>
    <span style="color:#B8956A">굵은 선</span>은 사람이 직접 쓴 [[링크]].<br>
    점 크기 = 이웃 수 · 멀수록 흐려짐(깊이).<br>
    <b style="color:#8b93a1">왼쪽 드래그</b> 회전 · <b style="color:#8b93a1">오른쪽 드래그</b> 이동 · 휠 확대 · <b style="color:#8b93a1">Space</b> 자동회전<br>
    갱신: <code style="color:#8b93a1">brain-graph.py --html</code><br>
    __STAMP__ 기준
  </div>
  <div class="sub">최소 유사도 표시<input id="th" type="range" min="0" max="100" value="0"></div>
</div>
<div class="legend" id="lg"></div>
<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__, COLORS = __COLORS__;
const cv = document.getElementById('cv'), cx = cv.getContext('2d');
let W, H, DPR = Math.min(2, window.devicePixelRatio || 1);
function resize(){ W = innerWidth; H = innerHeight; cv.width = W*DPR; cv.height = H*DPR;
  cv.style.width = W+'px'; cv.style.height = H+'px'; cx.setTransform(DPR,0,0,DPR,0,0); }
resize(); addEventListener('resize', resize);

const N = DATA.nodes.length;
// 3D 구(球) 위에 고르게 뿌린다 — 피보나치 나선. 난수로 뿌리면 뭉치는 곳이 생긴다.
const R0 = 260;
const nodes = DATA.nodes.map((n,i)=>{
  const t = (i + 0.5) / N;
  const phi = Math.acos(1 - 2*t), theta = i * 2.39996;
  const r = R0 * Math.cbrt(0.25 + 0.75*t);
  return {...n,
    x: r*Math.sin(phi)*Math.cos(theta),
    y: r*Math.sin(phi)*Math.sin(theta),
    z: r*Math.cos(phi),
    vx:0, vy:0, vz:0, sx:0, sy:0, sr:0, sd:0 };
});
let links = DATA.links;
let view = links;

// 카테고리 범례
const cats = [...new Set(nodes.map(n=>n.c))];
document.getElementById('lg').innerHTML = cats.map(c=>
  `<span><i style="background:${COLORS[c]||'#888'}"></i>${c}</span>`).join('');

// ---- 3D 힘 배치 -------------------------------------------------------------
const K = Math.cbrt((R0*R0*R0)/N) * 2.6;
const MAXV = K * 0.6;
function step(alpha){
  for(const l of view){
    const a = nodes[l.s], b = nodes[l.t];
    let dx=b.x-a.x, dy=b.y-a.y, dz=b.z-a.z;
    const d = Math.hypot(dx,dy,dz)||1;
    const f = (d-K*0.85)/d * alpha * (0.08 + l.w*0.20);
    dx*=f; dy*=f; dz*=f;
    a.vx+=dx; a.vy+=dy; a.vz+=dz; b.vx-=dx; b.vy-=dy; b.vz-=dz;
  }
  for(let i=0;i<N;i++){
    const a = nodes[i];
    for(let j=i+1;j<N;j++){
      const b = nodes[j];
      let dx=b.x-a.x, dy=b.y-a.y, dz=b.z-a.z;
      const d2 = dx*dx+dy*dy+dz*dz;
      if(d2 > K*K*9 || d2===0) continue;
      const d = Math.sqrt(d2)||1;
      // 최소 거리 하한 — 겹친 두 점이 서로를 무한대로 밀어내는 것 방지
      const f = (K*K/Math.max(d2, K*K*0.04)) * alpha * 0.55;
      const ux=dx/d*f, uy=dy/d*f, uz=dz/d*f;
      a.vx-=ux; a.vy-=uy; a.vz-=uz; b.vx+=ux; b.vy+=uy; b.vz+=uz;
    }
    // 원점 인력 — 고립 노트도 붙잡아 둔다
    a.vx -= a.x*0.0016*alpha; a.vy -= a.y*0.0016*alpha; a.vz -= a.z*0.0016*alpha;
    a.vx*=0.86; a.vy*=0.86; a.vz*=0.86;
    const sp = Math.hypot(a.vx,a.vy,a.vz);
    if(sp > MAXV){ const k=MAXV/sp; a.vx*=k; a.vy*=k; a.vz*=k; }
    a.x+=a.vx; a.y+=a.vy; a.z+=a.vz;
  }
}

// ---- 카메라 (궤도) ----------------------------------------------------------
let yaw=0.6, pitch=0.35, dist=900, panX=0, panY=0, alpha=1, spin=true;
const FOV = 700;   // 초점거리 — 클수록 원근이 약해진다

function project(n){
  // 궤도 카메라: 월드를 -yaw/-pitch 만큼 돌려 카메라 좌표계로 옮긴다
  const cy=Math.cos(yaw), sy=Math.sin(yaw);
  let x = n.x*cy - n.z*sy;
  let z = n.x*sy + n.z*cy;
  const cp=Math.cos(pitch), sp=Math.sin(pitch);
  let y = n.y*cp - z*sp;
  z = n.y*sp + z*cp;
  const zc = z + dist;                 // 카메라 앞 거리
  if(zc < 1) { n.sd = -1; return; }    // 카메라 뒤 → 그리지 않음
  const k = FOV / zc;
  n.sx = W/2 + panX + x*k;
  n.sy = H/2 + panY + y*k;
  n.sr = k;                            // 화면 배율 (크기·굵기에 쓴다)
  n.sd = zc;                           // 깊이 (정렬·명암에 쓴다)
}

function draw(){
  cx.setTransform(DPR,0,0,DPR,0,0);
  cx.fillStyle='#12151b'; cx.fillRect(0,0,W,H);
  for(const n of nodes) project(n);

  // 깊이 정렬 — 먼 것부터 그려야 가까운 것이 위에 온다
  const order = nodes.map((n,i)=>i).filter(i=>nodes[i].sd>0).sort((a,b)=>nodes[b].sd-nodes[a].sd);
  const dmin = dist - R0*1.5, dmax = dist + R0*1.5;
  const fog = d => Math.max(0.10, Math.min(1, 1 - (d-dmin)/(dmax-dmin)));

  for(const l of view){
    const a=nodes[l.s], b=nodes[l.t];
    if(a.sd<=0 || b.sd<=0) continue;
    const f = fog((a.sd+b.sd)/2);
    // 멀수록 흐려진다 = 깊이감. 링크가 다 같은 밝기면 3D로 안 읽힌다.
    cx.strokeStyle = l.e ? `rgba(214,182,132,${.85*f})` : `rgba(168,190,220,${(.16+l.w*.34)*f})`;
    cx.lineWidth = (l.e ? 1.5 : .8) * ((a.sr+b.sr)/2);
    cx.beginPath(); cx.moveTo(a.sx,a.sy); cx.lineTo(b.sx,b.sy); cx.stroke();
  }
  for(const i of order){
    const n = nodes[i];
    const r = (2.2 + Math.sqrt(n.deg)*1.5) * n.sr;
    if(r < 0.35) continue;
    const f = fog(n.sd);
    cx.globalAlpha = 0.25 + 0.75*f;
    cx.beginPath(); cx.arc(n.sx,n.sy,r,0,6.284);
    cx.fillStyle = n.deg ? (COLORS[n.c]||'#888') : '#3a4049';
    cx.fill();
  }
  cx.globalAlpha = 1;
}

function loop(){
  if(alpha>0.015){ step(alpha); alpha*=0.991; }
  if(spin) yaw += 0.0016;          // 가만히 두면 천천히 돈다 (입체임을 알려준다)
  draw(); requestAnimationFrame(loop);
}
loop();

// ---- 조작 -------------------------------------------------------------------
// 왼쪽 드래그 = 회전(궤도) · 오른쪽 드래그 = 이동 · 휠 = 확대
let rotDrag=null, panDrag=null;
cv.addEventListener('contextmenu', e=>e.preventDefault());
cv.addEventListener('mousedown', e=>{
  if(e.button===0){ rotDrag={x:e.clientX,y:e.clientY,yaw,pitch}; spin=false; cv.style.cursor='grabbing'; }
  else if(e.button===2){ panDrag={x:e.clientX,y:e.clientY,panX,panY}; cv.style.cursor='move'; }
  e.preventDefault();
});
addEventListener('mouseup', ()=>{ rotDrag=null; panDrag=null; cv.style.cursor='grab'; });
addEventListener('mousemove', e=>{
  if(rotDrag){
    yaw = rotDrag.yaw + (e.clientX-rotDrag.x)*0.0060;
    pitch = Math.max(-1.45, Math.min(1.45, rotDrag.pitch + (e.clientY-rotDrag.y)*0.0060));
    return;
  }
  if(panDrag){ panX = panDrag.panX + (e.clientX-panDrag.x); panY = panDrag.panY + (e.clientY-panDrag.y); return; }
  // 호버 — 투영된 화면 좌표로 직접 찾는다(역투영 불필요)
  let best=null, bd=13;
  for(const n of nodes){
    if(n.sd<=0) continue;
    const d = Math.hypot(n.sx-e.clientX, n.sy-e.clientY);
    if(d<bd){ bd=d; best=n; }
  }
  const tip=document.getElementById('tip');
  if(best){ tip.style.display='block'; tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY+14)+'px';
    tip.innerHTML=`<div class="c">${best.c} · ${best.d||'-'} · 이웃 ${best.deg}</div>${best.t}`; }
  else tip.style.display='none';
});
cv.addEventListener('wheel', e=>{ e.preventDefault();
  dist = Math.max(180, Math.min(3200, dist * (e.deltaY<0 ? 0.90 : 1.11)));
},{passive:false});
addEventListener('keydown', e=>{ if(e.code==='Space'){ spin=!spin; e.preventDefault(); } });
document.getElementById('th').addEventListener('input',e=>{
  const t=e.target.value/100;
  view = links.filter(l=>l.w>=t);
  alpha=Math.max(alpha,.35);
});
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="brain 기억 연결망")
    ap.add_argument("--min-sim", type=float, default=DEFAULT_MIN_SIM)
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--html", nargs="?", const=str(DEFAULT_HTML), metavar="PATH",
                    help="3D 연결망 HTML 생성. 경로를 생략하면 brain 볼트 _System/ 아래에 만든다")
    ap.add_argument("--orphans", action="store_true")
    ap.add_argument("--suggest", action="store_true")
    ap.add_argument("--node", metavar="SLUG")
    args = ap.parse_args()

    if not INDEX_PATH.exists():
        print(f"인덱스가 없습니다: {INDEX_PATH}\n먼저 brain-reindex.py 를 실행하세요.")
        return 1

    conn = open_db(INDEX_PATH, create=False)
    doc_vecs = load_chunk_vectors(conn)
    if not doc_vecs:
        print("인덱스에 임베딩이 없습니다.")
        return 1

    paths, edges = build_graph(doc_vecs, args.min_sim, args.top_k)
    explicit = load_explicit_links(paths)

    if args.html:
        cmd_html(paths, edges, explicit, args)
    elif args.orphans:
        cmd_orphans(paths, edges, args)
    elif args.suggest:
        cmd_suggest(paths, edges, explicit, args)
    elif args.node:
        cmd_node(paths, edges, explicit, args)
    else:
        cmd_stats(paths, edges, explicit, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
