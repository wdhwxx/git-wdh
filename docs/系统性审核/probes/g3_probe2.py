# -*- coding: utf-8 -*-
"""G3 门禁探针 2：R-16 登记组件裸实例化 / R-17 契约完备性。"""
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


# 登记组件 → 其工厂所在模块（装配责任上移后应只经 get_component / 工厂函数取用）
REG_COMPONENTS = {
    "MetaMonitor": "myquant/monitoring/meta_monitor.py",
    "StalenessDetector": "myquant/monitoring/watchdog.py",
    "EngineHeartbeatWatcher": "myquant/monitoring/watchdog.py",
    "BrokerConnectionWatcher": "myquant/monitoring/watchdog.py",
    "WeeklyReviewReport": "myquant/monitoring/weekly_review.py",
    "ActionEngine": "myquant/monitoring/action_engine.py",
    "AlertManager": "myquant/monitoring/alerting.py",
    "AccountReconciler": "myquant/monitoring/account_reconciler.py",
    "MetricsCollector": "myquant/monitoring/metrics_collector.py",
}

print("R-16 装配器集中：登记组件裸类实例化（非工厂模块即潜在违规）")
for cls, factory_mod in REG_COMPONENTS.items():
    hits = []
    for p in all_py(MYQUANT):
        tree = parse(p)
        if tree is None:
            continue
        rel = p.relative_to(MYQUANT).as_posix()
        # 定位 __main__ / TYPE_CHECKING 语境范围
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
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
               and node.func.id == cls and not excluded(node.lineno):
                hits.append((rel, node.lineno))
    if hits:
        print(f"  {cls} 非main实例化点: {hits}")
    else:
        print(f"  {cls}: 无非main直接实例化")

print()
print("R-17 事件契约完备性：EventType 成员中未被消费（发布/订阅）的成员")
eb = MYQUANT / "core" / "event_bus.py"
eb_src = read(eb)
m = re.search(r"class EventType\(Enum\):(.*?)(?=\nclass )", eb_src, re.S)
event_members = dict(re.findall(r"(\w+)\s*=\s*\"([^\"]+)\"", m.group(1)))
print(f"  EventType 成员数: {len(event_members)}")
used_members = defaultdict(int)
for p in all_py(MYQUANT):
    tree = parse(p)
    if tree is None:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
           and node.value.id == "EventType" and isinstance(node.attr, str):
            used_members[node.attr] += 1
unused = {k for k in event_members if used_members[k] == 0}
print(f"  未被任何模块引用的 EventType 成员: {len(unused)}")
for k in sorted(unused):
    print(f"    {k} = {event_members[k]!r}")

# 检查订阅侧使用的类型是否都在枚举内（订阅字符串字面量）
sub_literals = defaultdict(int)
for p in all_py(MYQUANT):
    tree = parse(p)
    if tree is None:
        continue
    rel = p.relative_to(MYQUANT).as_posix()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
           and node.func.attr in ("subscribe", "unsubscribe_all") and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                sub_literals[a.value] += 1
unknown_sub = {k for k in sub_literals if k not in event_members.values()}
print(f"  订阅用字符串类型: {dict(sub_literals)}")
print(f"  非 EventType 值的订阅字符串: {sorted(unknown_sub)}")

print()
print("R-18 脚本自举：scripts/ 内类定义的类体内行数（区分策略定义 vs 重逻辑）")
for p in sorted((ROOT / "scripts").rglob("*.py")):
    if "__pycache__" in p.parts:
        continue
    tree = parse(p)
    if tree is None:
        continue
    rel = p.relative_to(ROOT).as_posix()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            body_lines = (node.end_lineno or node.lineno) - node.lineno
            print(f"  {rel}:{node.lineno} class {node.name} (body {body_lines} 行)")
