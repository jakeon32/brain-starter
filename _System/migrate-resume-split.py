#!/usr/bin/env python3
"""session-resume.md(단일 파일) -> Memory/resume/<project>.md + 인덱스로 분리.

2026-07-29 구조 개편의 1회성 마이그레이션. 한 파일에 전 프로젝트를 담으면
파일 크기 = 프로젝트 수 x 섹션 크기라, 덮어쓰기 규칙을 지켜도 몇 주마다 한계를
넘는다(원본 볼트 실측: 132KB → 슬림 → 49KB → 62KB → 68KB — 네 번 반복).
이 골격을 새로 시작했다면 필요 없다 — 예전 단일 파일 레이아웃에서 올라오는
기존 볼트만 해당된다.

사용법:
    python migrate-resume-split.py <볼트 경로> [--apply]

    --apply 없이 돌리면 무엇을 만들지만 출력하고 파일은 건드리지 않는다(dry-run).

하는 일:
    1. Memory/session-resume.md를 '## ' 헤더로 분해
    2. 각 프로젝트 섹션 -> Memory/resume/<slug>.md (frontmatter 부착)
       - slug는 헤더 안 백틱 경로의 마지막 폴더명에서 뽑는다. 없으면 헤더 텍스트.
       - '현재 트랙 요약' -> _cross-track.md / 'Open decisions' -> _open-decisions.md
    3. 원본을 인덱스로 재작성(사용법 + 프로젝트 표). 원본은 .bak-<날짜>로 백업
    4. 'How to use' 섹션 내용은 인덱스가 새로 쓰므로 옮기지 않는다

주의: 인덱스 표의 '다음 진입점' 칸은 헤더의 '다음 = ...' 문구에서 기계적으로
뽑는다. 사람이 읽을 문장으로 다듬는 건 마이그레이션 후 Claude가 할 일이다.
"""
import re
import sys
import shutil
import datetime
from pathlib import Path

# 한국어 Windows 콘솔은 cp949라 em-dash 하나에도 출력이 죽는다 — 포팅 dry-run에서 실측.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FM = "---\ntype: resume\nproject: {path}\nupdated: {date}\n---\n\n"


def slugify(header: str) -> str:
    """헤더에서 파일명을 만든다. 백틱 경로의 마지막 폴더명을 최우선으로."""
    if header.startswith("## 현재 트랙 요약"):
        return "_cross-track"
    if header.startswith("## Open decisions"):
        return "_open-decisions"
    m = re.search(r"`([A-Za-z]:\\[^`]+|~[^`]+|/[^`]+)`", header)
    if m:
        raw = m.group(1).replace("/", "\\").rstrip("\\")
        parts = [p for p in raw.split("\\") if p and not p.endswith(":")]
        # docs·src 같은 일반 폴더가 leaf면 프로젝트를 식별 못 한다 → 위로 올라간다
        # (실측: `~/projects/acme/docs` 가 docs.md 로 나왔다)
        GENERIC = {"docs", "doc", "src", "app", "public", "dist", "web", "site", "output"}
        while len(parts) > 1 and parts[-1].lower() in GENERIC:
            parts.pop()
        if parts:
            return re.sub(r"[^\w.-]+", "-", parts[-1]).strip("-").lower()
    title = header.lstrip("#").strip()
    title = re.split(r"[(—\[]", title)[0].strip()
    return re.sub(r"[^\w.-]+", "-", title).strip("-").lower() or "untitled"


def extract_path(header: str) -> str:
    paths = re.findall(r"`([A-Za-z]:\\[^`]+|~[^`]+|/[^`]+)`", header)
    return " · ".join(paths) if paths else "(경로 미기재 — 손으로 채울 것)"


def extract_date(header: str) -> str:
    m = re.search(r"(20\d\d-\d\d-\d\d)", header)
    return m.group(1) if m else ""


def extract_next(header: str) -> str:
    m = re.search(r"다음\s*=\s*([^)/]+)", header)
    return m.group(1).strip() if m else "(파일 안 '다음 진입점' 1번을 옮겨 적을 것)"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    vault = Path(sys.argv[1]).expanduser()
    apply = "--apply" in sys.argv
    src = vault / "Memory" / "session-resume.md"
    outdir = vault / "Memory" / "resume"

    if not src.exists():
        print(f"[중단] 없음: {src}")
        return 1
    text = src.read_text(encoding="utf-8")
    heads = [(m.start(), m.group(0)) for m in re.finditer(r"^## .*$", text, re.M)]
    if not heads:
        print("[중단] '## ' 섹션이 없다. 이미 분리된 인덱스이거나 형식이 다르다.")
        return 1
    if (outdir).is_dir() and any(outdir.glob("*.md")):
        print(f"[중단] {outdir} 에 이미 파일이 있다. 손으로 확인할 것.")
        return 1

    rows, files = [], []
    for i, (st, head) in enumerate(heads):
        if head.startswith("## How to use"):
            continue  # 인덱스가 새로 쓴다
        en = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        body = text[st:en].rstrip().rstrip("-").rstrip()
        slug = slugify(head)
        date = extract_date(head) or datetime.date.today().isoformat()
        files.append((slug, FM.format(path=extract_path(head), date=date) + body + "\n"))
        if not slug.startswith("_"):
            title = re.split(r"[(—\[]", head.lstrip("#").strip())[0].strip()
            rows.append((title, slug, extract_path(head), date[5:], extract_next(head)))

    print(f"{'파일':28s} 크기")
    for slug, content in files:
        print(f"{slug + '.md':28s} {len(content.encode('utf-8')) / 1024:5.1f}KB")
    print(f"---- {len(files)}개 / 인덱스 행 {len(rows)}개")
    if not apply:
        print("\n(dry-run — 실제로 쓰려면 --apply)")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "_done").mkdir(exist_ok=True)
    for slug, content in files:
        (outdir / f"{slug}.md").write_text(content, encoding="utf-8")

    today = datetime.date.today().isoformat()
    shutil.copyfile(src, str(src) + ".bak-" + today.replace("-", ""))
    idx = [
        "---", "title: Session resume — 인덱스", "type: state", "status: live",
        f"updated: {today}", "---", "",
        "> 직전 세션이 어디서 끝났고 다음에 어디로 진입할지의 단일 진입점.",
        "> **이 파일은 인덱스다** — 내용은 `Memory/resume/<프로젝트>.md`에 프로젝트당 한 파일로 있다.",
        "", "## How to use", "",
        "- **Session start:** 아래 표에서 **현재 cwd에 해당하는 줄 하나**를 찾아 그 파일만 Read한다.",
        "- **재개 3단계**: ① 해당 파일을 실제 Read ② 최신성 교차검증(파일 `updated`보다 새 git log·작업 로그가 있으면 낡은 것) ③ \"직전 세션 = X / 진입점 1번 = Y → 여기부터 재개\" 선언 후 시작.",
        "- **Session end:** 자기 프로젝트 파일 하나만 덮어쓰고, 아래 표의 그 줄을 갱신한다. 다른 파일은 건드리지 않는다.",
        "- **종료 시:** 파일을 `resume/_done/`으로 이동 → 잔여 미결은 `resume/_open-decisions.md`에 한 줄 → 표에서 줄 삭제.",
        "- **쓰는 것** = 다음 진입점 · 재개 식별자(경로·포트·커밋·id) · 그 프로젝트 고유 함정.",
        "  **쓰지 않는 것** = 이번 세션 서사(세션 로그 담당) · 범용 지식(`Knowledge/` 정본, 여기엔 `[[링크]]`만).",
        "- 크기: 프로젝트 파일 ~6KB · 인덱스 ~6KB. 정본 규칙은 기록 관례를 모아둔 런타임 문서를 따른다.",
        "", "---", "", "## 프로젝트 (`Memory/resume/`)", "",
        "| 프로젝트 | 파일 | cwd | 최종 | 다음 진입점 1번 |", "|---|---|---|---|---|",
    ]
    for title, slug, path, date, nxt in rows:
        idx.append(f"| {title} | `{slug}.md` | `{path}` | {date} | {nxt} |")
    idx += ["", "## 프로젝트에 안 걸리는 것", "",
            "- **`_cross-track.md`** — 전역 미결·개인작업 트랙",
            "- **`_open-decisions.md`** — 종료한 프로젝트의 잔여 미결",
            "- **`_done/`** — 종료 프로젝트 보관", ""]
    src.write_text("\n".join(idx), encoding="utf-8")
    print(f"\n[완료] {outdir} 에 {len(files)}개 · 인덱스 재작성 · 백업 {src.name}.bak-{today.replace('-', '')}")
    print("다음: 인덱스 표의 '다음 진입점' 칸을 사람이 읽을 문장으로 다듬을 것.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
