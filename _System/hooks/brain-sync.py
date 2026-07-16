#!/usr/bin/env python3
"""
brain-sync: git sync for the opt-in shared layer (Knowledge/*.md + Memory/*.md
files tagged `scope: shared` in frontmatter).

Design: docs/plans/2026-07-13-refined-layer-cross-device-sync-design.md

Modes:
  --pull   — SessionStart-time. `git pull --ff-only`. Exits 0 (silent) on
      success, no-op, or "can't reach remote" (offline is normal — a laptop
      not always connected shouldn't warn). Exits 1 only when the fast-
      forward genuinely can't apply (local unpushed commits diverged from a
      remote update) — brain-health.py should surface that as a warning.
  --push   — SessionEnd / post-consolidate-time. Force-adds every file
      tagged `scope: shared`, commits if anything actually changed, pushes.
      On a rejected push (non-fast-forward): one `git pull --rebase` retry.
      If that still fails (real conflict, not just staleness): abort the
      rebase, leave the local commit intact-but-unpushed, exit 2. Never
      attempts interactive/LLM conflict resolution — that's a human's call.

Everything here only touches Knowledge/*.md and Memory/*.md. Journals
(Decisions/Mistakes/Patterns), Sessions/, and Agents/ are never staged —
this script doesn't even look at them.

Promotion to `scope: shared` itself is never done by this script — per
the design doc's §7 Q4, that's always a proposed-out-loud, user-confirmed
edit to a file's frontmatter, made before this script ever runs.
"""
from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
from _brain_lib import BRAIN_VAULT  # noqa: E402

SHARED_FOLDERS = ("Knowledge", "Memory")
SCOPE_RE = re.compile(r"^scope:\s*shared\s*$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# Known-benign git-pull failure text — offline/unreachable, not a real conflict.
_OFFLINE_HINTS = (
    "could not resolve host", "couldn't resolve host", "connection timed out",
    "unable to access", "could not read from remote", "network is unreachable",
    "temporary failure in name resolution",
)


def is_shared(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    m = FRONTMATTER_RE.match(text)
    if not m:
        return False
    return bool(SCOPE_RE.search(m.group(1)))


def find_shared_files(root: Path = BRAIN_VAULT) -> list[Path]:
    out = []
    for folder in SHARED_FOLDERS:
        sub = root / folder
        if not sub.exists():
            continue
        for p in sorted(sub.glob("*.md")):
            if p.name in ("_index.md", "_catalog.md"):
                continue
            if is_shared(p):
                out.append(p)
    return out


def run_git(args: list[str], cwd: Path = BRAIN_VAULT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def write_receipt(lines: list[str]) -> None:
    audit = BRAIN_VAULT / "_System" / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    path = audit / f"sync-{stamp}.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    path.write_text(existing + f"\n## {ts}\n" + "\n".join(lines) + "\n", encoding="utf-8")


def cmd_pull(args) -> int:
    proc = run_git(["pull", "--ff-only"])
    if proc.returncode == 0:
        return 0  # up to date or fast-forwarded — silent per brain-health's contract
    stderr_lower = proc.stderr.lower()
    if any(hint in stderr_lower for hint in _OFFLINE_HINTS):
        return 0  # offline is normal, not a problem to surface
    # Anything else (usually: local commits diverged from remote) is worth a warning.
    print("brain-sync --pull: local shared content has diverged from the remote "
          "and could not fast-forward. Run `git pull` manually in the brain vault.",
          file=sys.stderr)
    print(proc.stderr.strip(), file=sys.stderr)
    return 1


def cmd_push(args) -> int:
    shared = find_shared_files()
    if not shared:
        return 0  # nothing tagged shared yet — nothing to do

    for p in shared:
        add = run_git(["add", "-f", "--", str(p.relative_to(BRAIN_VAULT))])
        if add.returncode != 0:
            print(f"brain-sync --push: git add -f failed for {p}: {add.stderr.strip()}",
                  file=sys.stderr)

    staged = run_git(["diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return 0  # nothing actually changed — no commit, no push needed

    rel_paths = [str(p.relative_to(BRAIN_VAULT)) for p in shared]
    summary = f"brain-sync: {len(shared)} shared file(s) checked, staged changes found"
    body = "\n".join(f"- {r}" for r in rel_paths)
    if args.dry_run:
        print(summary)
        print(body)
        run_git(["reset"])  # undo the staging, dry-run shouldn't leave state behind
        return 0

    commit = run_git(["commit", "-m", summary, "-m", body])
    if commit.returncode != 0:
        print(f"brain-sync --push: commit failed: {commit.stderr.strip()}", file=sys.stderr)
        return 2

    push = run_git(["push"])
    if push.returncode == 0:
        write_receipt([summary, body, "pushed: yes"])
        return 0

    # Rejected — most likely non-fast-forward. One rebase-and-retry attempt.
    rebase = run_git(["pull", "--rebase"])
    if rebase.returncode == 0:
        push2 = run_git(["push"])
        if push2.returncode == 0:
            write_receipt([summary, body, "pushed: yes (after rebase retry)"])
            return 0

    # Rebase itself failed (real conflict) or the retried push still failed.
    run_git(["rebase", "--abort"])  # best-effort cleanup; no-op if no rebase in progress
    write_receipt([
        summary, body,
        "pushed: NO — commit is local-only, needs manual `git pull`/merge in the brain vault",
    ])
    print("brain-sync --push: could not push after one rebase retry — "
          "the commit is safe locally but needs manual resolution.", file=sys.stderr)
    return 2


def _respawn_detached() -> bool:
    """Re-run this push detached; return True if a child took over.

    Why (health.md §sequel postmortem): `--push` is a SessionEnd hook, and a
    network `git push` is slow enough to still be in flight when session
    teardown kills the hook's process tree. That is not hypothetical —
    it happened the day the hook was wired: a commit landed one second after
    a SessionEnd fired, and its push never ran. No receipt was written (this
    script always writes one, pushed yes *or* no), so the process died
    mid-push. The commit sat local-only, which is the exact failure the
    SessionEnd wiring existed to prevent — the shared layer receiving but not
    sending. The same teardown killed that session's distill.

    Detaching makes the hook return immediately, so there is nothing to cancel.
    Only `--push` detaches: `--pull` runs at SessionStart and its result is
    needed *before* the session reads anything.

    Fail-open: if the spawn fails we fall through and push inline.
    """
    import os
    if os.environ.get("BRAIN_SYNC_DETACHED"):
        return False
    try:
        env = dict(os.environ, BRAIN_SYNC_DETACHED="1")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--push"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=env,
            **kwargs,
        )
        return True
    except Exception as e:  # noqa: BLE001 - fail-open by design
        print(f"brain-sync --push: detach failed ({e!r}); pushing inline",
              file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pull", action="store_true")
    mode.add_argument("--push", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                     help="with --push: report what would sync, change nothing")
    args = ap.parse_args()

    if args.pull:
        return cmd_pull(args)
    # --dry-run must stay in-process: the caller wants to see the report.
    if not args.dry_run and _respawn_detached():
        return 0
    return cmd_push(args)


if __name__ == "__main__":
    sys.exit(main())
