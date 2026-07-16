#!/usr/bin/env python3
"""Tests for check-purity.py — the gate that keeps this repo publishable.

A public repo cannot be un-published: clones and caches survive a force-push.
So the scanner is the only real defence, and a scanner nobody tested is not a
defence. These tests prove it catches what it claims to and does not cry wolf.

Every fixture below is deliberately fake: shaped like the real thing so it
still proves the pattern fires, but no value here is an actual company name,
person's name, IP, or database/channel id. Real proper nouns belong only in
a maintainer's own gitignored tools/denylist.local.txt, never in this file.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

SCANNER = Path(__file__).resolve().parents[1] / "check-purity.py"

DIRTY = {
    "personal-path": [
        r"C:\Users\alice\notes", r"c:\users\bob\notes",
        r"D:\Example\Vault", "/Users/bob/x",
    ],
    "internal-ip":   ["192.168.0.1", "10.11.12.13"],
    "vault-id":      [
        "deadbeef-0000-0000-0000-000000000000", "C0FAKE0000",
        "DEADBEEF-0000-0000-0000-000000000000", "C1A2B3C4D5",
    ],
}

CLEAN = [
    "the user's vault lives at ~/brain",
    "SessionEnd hook returns in 0.08s",
    "pip install sqlite-vec sentence-transformers",
    "example: my-project/notes.md",
    "127.0.0.1:8188 is a loopback address",   # not an internal IP
    "the jakefile task runner",   # must not fire on bare-name word-boundary match
    "jakarta server pages",       # must not fire on bare-name word-boundary match
    "her second-brain app is called MyNotebook",  # must not fire past the word boundary
    "see docs/06-claude-notes.md for details",    # hyphenated, not the literal phrase
]


def load():
    spec = importlib.util.spec_from_file_location("purity", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["purity"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = load()
    failures = []

    # 1. every dirty sample is caught, by the right category
    for category, samples in DIRTY.items():
        for s in samples:
            hits = mod.scan_text(s)
            got = {c for c, _ in hits}
            ok = category in got
            print(f"{'PASS' if ok else 'FAIL'}  catch [{category}] {s!r} -> {sorted(got) or 'nothing'}")
            if not ok:
                failures.append(f"missed {category}: {s!r}")

    # 2. clean samples produce no hits at all (no crying wolf)
    for s in CLEAN:
        hits = mod.scan_text(s)
        ok = not hits
        print(f"{'PASS' if ok else 'FAIL'}  clean {s!r} -> {[c for c, _ in hits] or 'clean'}")
        if not ok:
            failures.append(f"false positive: {s!r} -> {hits}")

    # 3. the allowlist actually exempts
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "fixture.py"
        p.write_text("session abcdef01-0000-0000-0000-000000000000\n", encoding="utf-8")
        hits_raw = mod.scan_file(p, allow=[])
        hits_alw = mod.scan_file(p, allow=["vault-id"])
        ok = bool(hits_raw) and not hits_alw
        print(f"{'PASS' if ok else 'FAIL'}  allowlist exempts vault-id (raw={len(hits_raw)} allowed={len(hits_alw)})")
        if not ok:
            failures.append("allowlist did not exempt")

    # 4. the optional local denylist: present -> its terms are caught under
    # the "denylist" category; absent -> the scanner still runs clean on
    # shape patterns alone (it must not crash or refuse). Both checks point
    # at a temp-dir path, never the real tools/denylist.local.txt, so this
    # test stays hermetic regardless of whether a maintainer has their own
    # local wordlist in place.
    with tempfile.TemporaryDirectory() as td:
        wordlist = Path(td) / "denylist.local.txt"
        missing = Path(td) / "does-not-exist.txt"
        sample_text = "internal doc mentions Acme-Widgets by its project codename"

        wordlist.write_text(
            "# comment lines are ignored\nacme-widgets\nproject codename\n",
            encoding="utf-8",
        )

        patterns_present = mod.active_patterns(denylist_path=wordlist)
        hits_present = mod.scan_text(sample_text, patterns_present)
        ok = any(cat == "denylist" for cat, _ in hits_present)
        print(f"{'PASS' if ok else 'FAIL'}  denylist present catches its term -> {hits_present or 'nothing'}")
        if not ok:
            failures.append("denylist present did not catch its own term")

        patterns_absent = mod.active_patterns(denylist_path=missing)
        hits_absent = mod.scan_text(sample_text, patterns_absent)
        ok = not hits_absent
        print(f"{'PASS' if ok else 'FAIL'}  denylist absent stays clean on shape patterns -> {hits_absent or 'clean'}")
        if not ok:
            failures.append(f"denylist absent produced hits: {hits_absent}")

        ok = mod.load_denylist(missing) is None
        print(f"{'PASS' if ok else 'FAIL'}  load_denylist() on a missing file returns None, not a crash")
        if not ok:
            failures.append("load_denylist did not return None for a missing file")

    print()
    if failures:
        print(f"FAILED {len(failures)}:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
