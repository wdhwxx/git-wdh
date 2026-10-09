# -*- coding: utf-8 -*-
"""G3 四条新门禁探针：实测现状以定基线。置于 agent 工作区（非蓝图目录树）。"""
from __future__ import annotations

import ast
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


def parse(path: Path):
    try:
        return ast.parse(read(path), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


def all_py(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


print("=" * 60)
print("R-16 装配器集中：live 层跨层裸类实例化（非工厂/装配器）")
print("=" * 60)
# 扫描 live/ 下文件：import 了跨层类，且在该文件里出现 ClassName(...) 实例化
for p in all_py(MYQUANT / "live"):
    tree = parse(p)
    if tree is None:
        continue
    src = read(p)
    imported_classes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name not in ("*",):
                    imported_classes.add(a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported_classes.add(a.name.split(".")[-1])
    # 找出实例化的类名
    instantiated = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in imported_classes:
                instantiated.add(node.func.id)
    if instantiated:
        print(f"  {p.relative_to(MYQUANT).as_posix()}: {sorted(instantiated)}")

print("=" * 60)
print("R-17 事件契约：Event/emit/publish 用到的字符串事件类型 vs EventType 成员")
print("=" * 60)
eb = MYQUANT / "core" / "event_bus.py"
eb_src = read(eb)
eb_tree = parse(eb)
event_values = set()
for node in ast.walk(eb_tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                # 仅收集 EventType 类体内的
                pass
# 直接抓 EventType 枚举值
m = re.search(r"class EventType\(Enum\):(.*?)\n\nclass ", eb_src, re.S)
event_values = set(re.findall(r'= "([^"]+)"', m.group(1))) if m else set()
print(f"  EventType 成员值({len(event_values)}): {sorted(event_values)}")

# 扫描全 myquant 用字符串事件类型的调用
literal_types = defaultdict(list)
for p in all_py(MYQUANT):
    tree = parse(p)
    if tree is None:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            # Event("...") / emit("...") / publish_chain(Event("..."))
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
               and fn.attr in ("publish", "emit", "publish_chain") and node.args:
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and "." in a.value:
                        literal_types[a.value].append(p.relative_to(MYQUANT).as_posix())
            if isinstance(fn, ast.Name) and fn.id == "Event" and node.args:
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and "." in a.value:
                        literal_types[a.value].append(p.relative_to(MYQUANT).as_posix())
ad_hoc = {k: v for k, v in literal_types.items() if k not in event_values}
print(f"  字符串事件类型字面量总数: {len(literal_types)}")
print(f"  非 EventType 成员的 ad-hoc 事件类型: {len(ad_hoc)}")
for k, v in sorted(ad_hoc.items()):
    print(f"    {k!r} <- {set(v)}")

print("=" * 60)
print("R-18 脚本自举：scripts/ 薄入口纪律（类定义/业务逻辑）")
print("=" * 60)
scripts = all_py(ROOT / "scripts")
class_defs = []
for p in scripts:
    tree = parse(p)
    if tree is None:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_defs.append((p.relative_to(ROOT).as_posix(), node.name, node.lineno))
print(f"  scripts/*.py 总数: {len(scripts)}")
print(f"  含类定义的脚本: {len(class_defs)}")
for f, n, ln in class_defs:
    print(f"    {f}:{ln} class {n}")

print("=" * 60)
print("R-08 配置反查：config/*.yaml 顶层键 vs myquant 消费")
print("=" * 60)
cfg_dir = ROOT / "config"
if cfg_dir.is_dir():
    cfgs = sorted(p for p in cfg_dir.glob("*.yaml") if p.is_file())
    print(f"  config/*.yaml: {[p.name for p in cfgs]}")
    for cfg in cfgs:
        txt = read(cfg)
        # 顶层键（顶格 key: 或 顶格 key: ）
        keys = re.findall(r"^([a-zA-Z_][\w.]*):", txt, re.M)
        print(f"  {cfg.name} 顶层键: {sorted(set(keys))}")
else:
    print("  config/ 目录不存在")
