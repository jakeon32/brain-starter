#!/usr/bin/env python3
r"""block-delete.py 동작 검증 — 실제로 아무것도 지우지 않는다.

한국어 요약: 훅에 가짜 페이로드를 먹여 차단/통과가 의도대로인지 확인한다.
훅을 고칠 때마다 돌린다. 이 훅은 배포 복사가 없다 — 저장소의
`.claude/hooks/block-delete.py` 를 settings.json 이 직접 실행한다.

    python3 _System/block-delete-test.py
"""
import json
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "block-delete.py"

CASES = [
    # 보호 대상 — 막혀야 한다
    ("Knowledge 파일 rm", "BLOCK", "rm Knowledge/foo.md"),
    ("Memory 파일 rm", "BLOCK", "rm -f Memory/tools.md"),
    ("Knowledge 하위 rm -rf", "BLOCK", "rm -rf Knowledge/toolbox/"),
    ("git rm Knowledge", "BLOCK", "git rm Knowledge/foo.md"),
    ("파이프로 xargs rm", "BLOCK", "ls Knowledge/*.md | xargs rm"),
    ("find -delete on Memory", "BLOCK", "find Memory/ -name '*.md' -delete"),
    ("광범위 git clean", "BLOCK", "git clean -fd"),
    ("광범위 rm -rf .", "BLOCK", "rm -rf ."),
    ("앞 세그먼트가 삭제", "BLOCK", "rm Knowledge/a.md; echo done"),
    ("&& 뒤 세그먼트가 삭제", "BLOCK", "cd /tmp && rm Memory/x.md"),
    ("보호 폴더로 cd 뒤 상대 경로 삭제", "BLOCK", "cd Knowledge && rm x.md"),
    ("pushd Memory 뒤 삭제", "BLOCK", "pushd Memory; rm -f a.md"),
    ("보호 경로 변수 뒤 삭제", "BLOCK", "D=Knowledge; rm -rf $D/"),

    # 오탐 방지 — 통과해야 한다 (2026-08-08 실측 회귀)
    ("무관한 rm + 다른 곳의 Knowledge 언급", "PASS",
     "cd /vault; python3 -c \"p='Knowledge/_index.md'\"\nrm -f _System/audit/queue.json"),
    ("저널 삭제", "PASS", "rm Decisions/2026-01-01-x.md"),
    ("_System 삭제", "PASS", "rm -f _System/audit/consolidate-queue.json"),
    ("/tmp 삭제", "PASS", "rm -rf /tmp/scratch"),
    ("Knowledge 읽기만", "PASS", "cat Knowledge/foo.md"),
    ("Knowledge 이동(아카이브)", "PASS", "mv Knowledge/foo.md Knowledge/archived/"),
    ("Knowledge grep 후 무관한 삭제", "PASS",
     "grep -l shared Knowledge/*.md; rm /tmp/out.txt"),
    ("커밋 메시지에 경로 언급", "PASS",
     "git commit -m 'Knowledge/foo.md 정리'\nrm /tmp/msg.txt"),
    ("삭제 없음", "PASS", "ls -la Knowledge/"),
    ("보호 폴더에서 나온 뒤 삭제", "PASS", "cd Knowledge && ls; cd /tmp && rm x.txt"),
    ("다른 폴더로 cd 뒤 삭제", "PASS", "cd /tmp/work && rm -f out.txt"),
]


def run(cmd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    r = subprocess.run([sys.executable, str(HOOK)], input=payload,
                       capture_output=True, encoding="utf-8", errors="replace")
    return r.returncode


def main():
    if not HOOK.exists():
        print("훅을 찾을 수 없습니다: %s" % HOOK)
        return 1
    print("훅: %s\n" % HOOK)
    failed = []
    for desc, want, cmd in CASES:
        got = "BLOCK" if run(cmd) == 2 else "PASS"
        if got != want:
            failed.append(desc)
        print("%s %-34s want=%-5s got=%s" % ("OK  " if got == want else "FAIL", desc, want, got))
    print("\n%d/%d 통과" % (len(CASES) - len(failed), len(CASES)))
    if failed:
        print("실패: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
