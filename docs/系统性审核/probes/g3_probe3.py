# -*- coding: utf-8 -*-
"""G3 探针 3：R-16 泛化——L0-L4 层直接实例化 monitoring/acceptance 类。"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(r"D:\Projects\Pythonwdhmm")
MYQUANT = ROOT / "myquant"

LAYERS = {
    "core": "L0", "utils": "L0", "database": "L0",
    "data_access": "L1", "models": "L2", "indicators": "L2", "engine": "L2E",
    "strategies": "L3", "backtest": "L3", "allocation": "L3", "risk": "L3", "ml": "L3",
    "execution": "L4", "live": "L4",
}


def read(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse(path: Path):
    try:
        return ast.parse(read(path), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


def all_py(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


hits = []
for p in all_py(MYQUANT):
    top = p.relative_to(MYQUANT).parts[0]
    layer = LAYERS.get(top)
    if layer is None or layer in ("L5", "L6"):
        continue
    tree = parse(p)
    if tree is None:
        continue
    rel = p.relative_to(MYQUANT).as_posix()
    main_ranges = sorted(
        (n.lineno, n.end_lineno or n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__"
                for c in n.test.comparators)
    )
    typing_ranges = sorted(
        (n.lineno, n.end_lineno or n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
        and n.test.id == "TYPE_CHECKING"
    )
    def excluded(ln):
        return any(s <= ln <= e for s, e in main_ranges) \
            or any(s <= ln <= e for s, e in typing_ranges)
    l5_classes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module \
           and (node.module.startswith("myquant.monitoring")
                or node.module.startswith("myquant.acceptance")):
            for a in node.names:
                if a.name not in ("*",):
                    l5_classes.add(a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(("myquant.monitoring", "myquant.acceptance")):
                    l5_classes.add(a.name.split(".")[-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
           and node.func.id in l5_classes and not excluded(node.lineno):
            hits.append((rel, node.lineno, node.func.id))

print(f"L0-L4 层直接实例化 L5(monitoring/acceptance) 类总数: {len(hits)}")
for h in hits:
    print(f"  {h[0]}:{h[1]} {h[2]}(")
