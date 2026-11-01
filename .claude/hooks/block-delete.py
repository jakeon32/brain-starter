#!/usr/bin/env python3
"""brain delete-block hook (PreToolUse / Bash).

Blocks delete-family commands (rm, git rm, git clean, unlink, shred,
truncate, find -delete) that target Knowledge/ or Memory/ paths, or that
are broad enough (no explicit path) to implicitly include them. Everything
else -- edits, git mv/archive moves, and deletes outside those two folders
-- passes through untouched.

Why only these two folders: files tagged scope: shared in Knowledge/Memory
can propagate across devices via git push/pull. A bad
delete here doesn't just lose local data -- it can silently disappear
everywhere on the next pull. Journals (Decisions/Mistakes/Patterns/
Sessions) and everything outside brain stay unrestricted.

Modeled on an earlier multi-person repo's .claude/hooks/block-delete.sh,
narrowed from "block everywhere" (multi-person repo, needs a hard stop
against anyone's mistake) to "block two folders" (single-user repo, the
risk is narrower now that a subset of content propagates).

Scope is judged **per command segment**, not over the whole string
(2026-08-08). Before that, any `rm` anywhere in the string plus the word
`Knowledge/` anywhere else was enough to block -- so

    cd <vault>; python3 - <<'PY' ... "Knowledge/_index.md" ... PY
    rm -f _System/audit/consolidate-queue.json

was refused, though the only delete targeted `_System/audit/`. Same class of
false positive the brain-guard marker check had: mentioning a path is not
operating on it. Splitting on `;`, `&&`, `||` and newlines fixes it.

**Pipes are deliberately NOT split** -- data flows across them, so the left
side is the right side's argument list. `ls Knowledge/ | xargs rm` has to
stay blocked, and it does.

Heredoc bodies still count as segments. Stripping them would miss the case
where the heredoc *is* the script (`bash <<EOF`), so the conservative
reading stays; write prose containing delete examples to a file and pass it
by path.

Pure stdlib (json/re/sys) -- deliberately no dependency on jq (not
installed on this device) or on the pinned-Python310 ML stack (not needed
here; any python3 on PATH works).
"""
import json
import re
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEL_RE = re.compile(
    r'(^|[\s;&|(])(rm|unlink|shred)(\s|$)'
    r'|git\s+rm(\s|$)'
    r'|git\s+clean(\s|$)'
    r'|(^|\s)truncate(\s|$)'
    r'|find\s.*-delete'
)
SCOPE_RE = re.compile(r'(^|[\s/\\;&|(\'"])(Knowledge|Memory)[/\\]')
BROAD_RE = re.compile(
    r'git\s+clean(\s+-[a-zA-Z]+)*\s*$'
    r'|rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(\.|\*)(\s|$)'
    r'|find\s+\.\s.*-delete'
)
# `;` `&&` `||` 개행으로만 나눈다. 파이프는 남긴다 -- 파이프는 데이터가 흐르므로
# 왼쪽이 오른쪽의 인자다(`ls Knowledge/ | xargs rm` 은 한 덩어리로 봐야 막힌다).
SEGMENT_RE = re.compile(r';|&&|\|\||\n')
# 세그먼트 사이로 이어지는 상태: 디렉터리 이동과 변수 대입
CD_RE = re.compile(r'(^|[\s(])(cd|pushd)(\s|$)')
CD_PROTECTED_RE = re.compile(r'(cd|pushd)\s+\S*(Knowledge|Memory)([/\\\'"]|\s|$)')
ASSIGN_RE = re.compile(r'(^|\s)[A-Za-z_][A-Za-z0-9_]*=\S*(Knowledge|Memory)')

MESSAGE = """\
brain 삭제 방지 -- Knowledge/, Memory/ 는 다른 기기와 공유될 수 있는 내용을 담고 있어 삭제를 막습니다.
(차단: rm / git rm / git clean / unlink / shred / truncate / find -delete)

대안:
  - 내용이 틀렸으면 고쳐 쓰거나 supersedes 로 새 문서가 이걸 대체한다고 표시
  - 더는 안 쓰는 내용이면 파일은 남기고 상태만 정리 (archived 이동 등)
  - 정말 파일 자체를 지워야 하면 사용자 확인 후, 세션 밖(탐색기 등)에서 직접 처리
  - Knowledge/, Memory/ 밖(저널·Sessions·다른 프로젝트)은 이 훅과 무관 -- 평소대로 자유롭게 삭제 가능
"""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd or not DEL_RE.search(cmd):
        return 0  # 삭제 계열이 아예 없으면 볼 것도 없다
    # 삭제 명령과 보호 경로가 **같은 세그먼트**에 있을 때만 막는다.
    # 단, 앞 세그먼트가 보호 폴더로 cd/pushd 했거나 보호 경로를 변수에 담았으면
    # 그 상태가 뒤 세그먼트까지 이어진다(`cd Knowledge && rm x.md`,
    # `D=Knowledge; rm -rf $D/`). 그때는 뒤의 상대 경로·변수 삭제도 막는다.
    entered = False   # 보호 폴더로 들어갔다
    tainted = False   # 보호 경로를 담은 변수가 있다
    for seg in SEGMENT_RE.split(cmd):
        if CD_RE.search(seg):
            entered = bool(CD_PROTECTED_RE.search(seg))
        if ASSIGN_RE.search(seg):
            tainted = True
        if not DEL_RE.search(seg):
            continue
        if SCOPE_RE.search(seg) or BROAD_RE.search(seg):
            print(MESSAGE, file=sys.stderr)
            return 2
        if entered or (tainted and "$" in seg):
            print(MESSAGE, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
