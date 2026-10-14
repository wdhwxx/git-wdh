# -*- coding: utf-8 -*-
"""G3 探针 4：R-08 配置反查——settings.yaml monitoring 键消费集合。"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"D:\Projects\Pythonwdhmm")
MYQUANT = ROOT / "myquant"


def read(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def all_py(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# 收集所有 myquant 中 get_monitoring_config() 后 .get("k", ...) / ["k"] 的一级键
first_level = defaultdict(list)
second_level = defaultdict(list)
for p in all_py(MYQUANT):
    src = read(p)
    rel = p.relative_to(MYQUANT).as_posix()
    # get_monitoring_config() ... .get("X", ...) 链条里的一级键
    for m in re.finditer(r'get_monitoring_config\(\)[^;]*?\.get\("([^"]+)"', src):
        first_level[m.group(1)].append(rel)
    for m in re.finditer(r'\.get\("([^"]+)",\s*\{\}\)\s*\.get\("([^"]+)"', src):
        second_level[(m.group(1), m.group(2))].append(rel)

print("get_monitoring_config() 一级键消费:")
for k, files in sorted(first_level.items()):
    print(f"  {k!r}: {len(set(files))} 处 -> {sorted(set(files))[:6]}")

print("二级键消费 (一级, 二级):")
for (k1, k2), files in sorted(second_level.items()):
    print(f"  {k1}.{k2}: {sorted(set(files))[:6]}")

# 也看 settings.py 的加载与对外访问器
print()
print("settings.py 提供的访问器:")
sp = MYQUANT / "core" / "settings.py"
if sp.exists():
    src = read(sp)
    for m in re.finditer(r"^def (\w+)\(.*?\)\s*->", src, re.M):
        print(f"  def {m.group(1)}()")
