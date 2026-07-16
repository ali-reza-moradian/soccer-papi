"""Stale-code guard: read the current git HEAD sha so the process can exit 0 when the branch moves.

The .ps1 wrapper restarts the process on exit, so exiting on a HEAD change makes a deploy adopt fresh
bytecode without a manual stop/start. Reads the .git files directly (no subprocess): HEAD -> a ref
file or packed-refs, or a detached sha in HEAD itself.
"""
from __future__ import annotations

import os
import re
from typing import Optional

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def read_head_sha(repo_root: str) -> Optional[str]:
    """The commit sha HEAD points at, or None if it can't be resolved."""
    head_path = os.path.join(repo_root, ".git", "HEAD")
    try:
        with open(head_path, encoding="utf-8") as fh:
            head = fh.read().strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        ref_path = os.path.join(repo_root, ".git", *ref.split("/"))
        try:
            with open(ref_path, encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            pass
        packed = os.path.join(repo_root, ".git", "packed-refs")   # ref may be packed
        try:
            with open(packed, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith(("#", "^")) and line.endswith(ref):
                        return line.split(" ", 1)[0]
        except OSError:
            pass
        return None
    return head if _SHA_RE.match(head) else None


def head_changed(prev: Optional[str], cur: Optional[str]) -> bool:
    """True only when BOTH shas are known and differ (an unreadable HEAD never forces a restart)."""
    return bool(prev and cur and prev != cur)
