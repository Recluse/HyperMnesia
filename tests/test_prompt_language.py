#!/usr/bin/env python3
"""Public model prompts are English; multilingual query handling belongs in data and tests."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CYRILLIC = re.compile(r"[\u0400-\u04ff]")

checked = []
for path in sorted((ROOT / "hooks").glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "PROMPT"
                   for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        assert not CYRILLIC.search(value), f"non-English model prompt in {path}"
        checked.append(path.name)

assert checked, "no model prompts found"
print(f"English prompt contract passed: {', '.join(checked)}")
