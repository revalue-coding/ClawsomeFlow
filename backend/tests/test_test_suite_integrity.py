"""Guardrails for test-suite structure and discoverability."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


def _declared_test_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                names.append(node.name)
            continue
        if not isinstance(node, ast.ClassDef):
            continue
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith("test_"):
                names.append(f"{node.name}.{member.name}")
    return names


def test_no_duplicate_test_names_within_single_file() -> None:
    repo = Path(__file__).resolve().parents[2]
    duplicates: dict[str, list[str]] = {}

    for root in (repo / "backend/tests", repo / "tests"):
        for test_file in sorted(root.rglob("test_*.py")):
            names = _declared_test_names(test_file)
            repeated = sorted(name for name, count in Counter(names).items() if count > 1)
            if repeated:
                duplicates[str(test_file.relative_to(repo))] = repeated

    assert not duplicates, (
        "duplicate test names shadow earlier definitions within the same file: "
        f"{duplicates}"
    )
