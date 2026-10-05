"""Texts that reach Claude or the user must not guess causes (REQ-005).

Every string constant and docstring in the package is checked: tool docstrings are
the tool descriptions Claude reads, and error texts come from string constants.
Comments are not checked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import anki_relay

SRC = Path(anki_relay.__file__).parent
HEDGES = re.compile(
    r"(?i)\b(probably|usually|likely|typically|perhaps|maybe|presumably|unreachable)\b"
    r"|вероятно|обычно|скорее всего|не ответил|нет связи"
)


def test_no_guessing_words_in_texts() -> None:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                match = HEDGES.search(node.value)
                if match:
                    found.append(f"{path.name}:{node.lineno}: {match.group(0)!r}")
    assert not found, found
