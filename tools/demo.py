"""Прогон скорёра без BB, сети и платных запросов."""

import json
import tempfile
from pathlib import Path

from bb_arena import _selftest_repo, _write, score_worktree

TESTS = """from quote import rubles


def test_rounding():
    assert [rubles(value) for value in (149, 150, 250)] == [1, 2, 3]
"""
BUG = """def rubles(kopecks: int) -> int:
    return round(kopecks / 100)
"""
FIX = """def rubles(kopecks: int) -> int:
    return (kopecks + 50) // 100
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bb-agent-demo-") as tmp:
        worktree, base = _selftest_repo(Path(tmp), {"quote.py": BUG, "tests/test_quote.py": TESTS})
        task = {"id": "quote-rounding", "type": "code", "base_commit": base,
                "hidden_tests": ["tests/test_quote.py"], "zone": ["quote.py"]}
        before = score_worktree(worktree, task, "local", "demo")
        _write(worktree / "quote.py", FIX)
        after = score_worktree(worktree, task, "local", "demo")
    print(json.dumps({"before": before, "after": after}, ensure_ascii=False, indent=2))
    return 0 if not before["success"] and after["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
