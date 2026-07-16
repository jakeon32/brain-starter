#!/usr/bin/env python3
"""brain delete-block hook (PreToolUse / Bash).

Blocks delete-family commands (rm, git rm, git clean, unlink, shred,
truncate, find -delete) that target Knowledge/ or Memory/ paths, or that
are broad enough (no explicit path) to implicitly include them. Everything
else -- edits, git mv/archive moves, and deletes outside those two folders
-- passes through untouched.

Why only these two folders: files tagged scope: shared in Knowledge/Memory
now propagate across devices via git push/pull (see
docs/plans/2026-07-13-refined-layer-cross-device-sync-design.md). A bad
delete here doesn't just lose local data -- it can silently disappear
everywhere on the next pull. Journals (Decisions/Mistakes/Patterns/
Sessions) and everything outside brain stay unrestricted.

Modeled on an earlier multi-person repo's .claude/hooks/block-delete.sh,
narrowed from "block everywhere" (multi-person repo, needs a hard stop
against anyone's mistake) to "block two folders" (single-user repo, the
risk is narrower now that a subset of content propagates).

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
        return 0
    if SCOPE_RE.search(cmd) or BROAD_RE.search(cmd):
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
