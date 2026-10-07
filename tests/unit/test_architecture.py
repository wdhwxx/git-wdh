# tests/unit/test_architecture.py
"""架构分层门禁 v4.2（全盘复查 + 评审修正版）。

本版在 v4.1 门禁基础上的修正（在 v4 §7 的 G-a~G-h 与 v4.1 的 K-a~K-e 之外，新增 K-f~K-h）：
  K-f  修复 `_imports()` 的 __main__ 盲区：`if __name__ == "__main__":` 自测块内的
       import 归入新语境 "main"（selfcheck），环图与违规计数均排除。
       —— 这是上一轮深度评审 B-2 抓到的真缺陷：strategies/* -> backtest.engine 的
       6 处全在自测块（ma:763/768、grid:477/482、multi_factor:1272/1277、
       momentum:581/586、kunpeng:956/959、mean_reversion:497/502），v4.1 把它们当 top，
       制造了伪环。修正后全量真环 = 3（环1 core.event_bus↔utils.memory_pool、
       环2 monitoring 内部 4 模块、环3 live.portfolio↔monitoring.account_reconciler），
       LAZY_CYCLE_BASELINE 由 4 改 3。
       同一盲区还误计了违规：utils/parallel.py:945/970/988 -> factor_cache 三条
       也在 __main__ 自测块，v4/v4.1 把它们当违规计入（L0->L2=6），排除后 L0->L2=3，
       权威违规由 40 修正为 37（VIOLATION_BASELINE 同步改 37）。
  K-g  新增 R-21：任何 myquant 文件必须可解析，_parse is None 即红 —— 封堵 v4.1 的
       errors="replace" 兜底导致的"畸形编码静默归零"盲区（评审 B-4）。
  K-h  新增 R-22：同层互引计数（只报告不阻断）—— 使 monitoring 153 处内部互引、
       core/utils 内部耦合可度量、可递减（评审 B-5；这是 G2 拆包的驱动指标）。

另按评审采纳：S-1 `_is_packaging_cycle` 改为白名单式（显式枚举允许的 re-export
包装环包，防未来 monitoring 真环被静默豁免）；S-6 删除 _init_modules 死代码；
S-3 R-19 更名 TestR19ZeroReference（诚实命名：引用级判定，非执行覆盖）。

权威台账：docs/系统性审核/整改台账_2026Q4.md
基线口径：违规=37（语义矩阵 α，排除 TYPE_CHECKING 与 __main__ 自测块；
          parallel.py:945/970/988 为 __main__ 自测边，L0->L2=3 非 6）；
          全量真环=3（另 1 组 strategies 包 re-export 惯用环为包装环，白名单剔除）；
          顶层真环=0；>1500 行白名单 14 项（相对路径）；
          零覆盖基线未冻结（待 pytest-cov 校准，见 G0-9）。
[待验]：本文件为静态审查 + 只读驱动核验通过，未在项目内真实运行 pytest；
        首跑若红，预期调整点仅 EXCEPTIONS 到期日/任务字段。
"""
from __future__ import annotations

import ast
import functools
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MYQUANT = PROJECT_ROOT / "myquant"

# ═══════════════════════════ 1) 层定义 ═══════════════════════════
MODULE_LAYER: dict[str, str] = {
    "myquant.core": "L0", "myquant.utils": "L0", "myquant.database": "L0",
    "myquant.data_access": "L1",
    "myquant.models": "L2", "myquant.indicators": "L2",
    "myquant.engine": "L2E",
    "myquant.strategies": "L3", "myquant.backtest": "L3",
    "myquant.allocation": "L3", "myquant.risk": "L3", "myquant.ml": "L3",
    "myquant.execution": "L4", "myquant.live": "L4",
    "myquant.monitoring": "L5",
    "myquant.api": "L6", "myquant.gui": "L6", "myquant.web": "L6",
}
LAYER_ORDER = ["L0", "L1", "L2", "L2E", "L3", "L4", "L5", "L6"]
# 同层/近层互通：L2 与 L2E 视为同级
SAME_OK: dict[str, set[str]] = {"L2": {"L2E"}, "L2E": {"L2"}}


# ═══════════════════════ 2) 例外表（点对点 + 到期日 + 台账；26 对 + P0 2 对） ═══════════════════════
# 字段: id, 源文件(相对项目根), 目标模块, 收敛任务, 到期日, kind, 说明
_EXC = [
    ("EX-01A", "myquant/database/trade_log.py", "myquant.models.trade",
     "永久", "2099-12-31", "runtime", "ORM 必引领域模型"),
    ("EX-07A", "myquant/utils/parallel.py", "myquant.indicators.factor_cache",
     "G1-1", "2026-12-31", "runtime", "并行模式开关"),
    ("EX-07B", "myquant/utils/cache_sync.py", "myquant.indicators.factor_cache",
     "G1-1", "2026-12-31", "runtime", "缓存同步"),
    ("EX-08A", "myquant/data_access/financial_service.py", "myquant.indicators.factor_engine",
     "G1-2", "2026-12-31", "runtime", "财务入库触发因子"),
    ("EX-09A", "myquant/backtest/engine.py", "myquant.execution.sim_broker",
     "G2-2", "2027-03-31", "runtime", "回测撮合"),
    ("EX-09B", "myquant/backtest/order_matcher.py", "myquant.execution.sim_broker",
     "G2-2", "2027-03-31", "runtime", "统一撮合"),
    ("EX-10A", "myquant/allocation/risk_parity.py", "myquant.monitoring.audit_log",
     "永久", "2099-12-31", "runtime", "审计横切（可注入）"),
    ("EX-10B", "myquant/strategies/param_tuner.py", "myquant.monitoring.audit_log",
     "永久", "2099-12-31", "runtime", "审计横切（可注入）"),
    ("EX-11A", "myquant/risk/rules.py", "myquant.monitoring.alerting",
     "G1-3", "2026-12-31", "runtime", "风险事件告警"),
    ("EX-11B", "myquant/risk/rules.py", "myquant.monitoring.metrics_collector",
     "G1-3", "2026-12-31", "runtime", "风险指标注册"),
    ("EX-11C", "myquant/risk/guardian.py", "myquant.api.middleware.auth",
     "G1-3", "2026-12-31", "runtime", "准入门禁"),
    ("EX-12A", "myquant/live/portfolio.py", "myquant.monitoring.account_reconciler",
     "G2-3", "2027-03-31", "runtime", "对账集成"),
    ("EX-12B", "myquant/live/portfolio.py", "myquant.monitoring.alerting",
     "G2-3", "2027-03-31", "runtime", "PnL 告警"),
    ("EX-12C", "myquant/live/portfolio.py", "myquant.api.middleware.auth",
     "G2-3", "2027-03-31", "runtime", "准入守卫"),
    ("EX-12D", "myquant/live/run_mode.py", "myquant.api.middleware.auth",
     "G2-3", "2027-03-31", "runtime", "准入守卫"),
    ("EX-13A", "myquant/live/scheduler.py", "myquant.monitoring.metrics_history",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13B", "myquant/live/scheduler.py", "myquant.monitoring.meta_monitor",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13C", "myquant/live/scheduler.py", "myquant.monitoring.metrics_collector",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13D", "myquant/live/scheduler.py", "myquant.monitoring.watchdog",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13E", "myquant/live/scheduler.py", "myquant.monitoring.alerting",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13F", "myquant/live/scheduler.py", "myquant.monitoring.health_check",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13G", "myquant/live/scheduler.py", "myquant.monitoring.alert_store",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13H", "myquant/live/scheduler.py", "myquant.monitoring.weekly_review",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13I", "myquant/live/scheduler.py", "myquant.monitoring.action_engine",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-13J", "myquant/live/scheduler.py", "myquant.monitoring.audit_log",
     "永久", "2099-12-31", "runtime", "装配器"),
    ("EX-15A", "myquant/monitoring/b_acceptance.py", "myquant.web.dashboard",
     "G1-5", "2026-12-31", "runtime", "验收数据源（L5->L6 反向，唯一保留）"),
]
EXCEPTIONS: dict[tuple[str, str], dict] = {
    (f, t): {"id": i, "task": tk, "expires": date.fromisoformat(e), "kind": k, "why": w}
    for i, f, t, tk, e, k, w in _EXC
}

# P0 必修（无例外）：完成后必须从代码中消除，并从 P0_MUST_FIX 删除
P0_MUST_FIX: set[tuple[str, str]] = {
    ("myquant/utils/performance_config.py", "myquant.strategies.ma_strategy"),
    ("myquant/backtest/engine.py", "myquant.live.engine"),
}

VIOLATION_BASELINE = 37          # K-a + K-f：语义矩阵 α，排除 TYPE_CHECKING 与 __main__（实测 37）
TOP_LEVEL_CYCLE_BASELINE = 0     # K-b：顶层真环（剔除包装惯用环与 __main__ 后）
LAZY_CYCLE_BASELINE = 3          # K-f：全量真环，只减不增（环1 event_bus↔memory_pool、
                                 #      环2 monitoring 内部4模块、环3 portfolio↔account_reconciler）

OVERSIZE_LIMIT = 1500
OVERSIZE_WHITELIST = {           # 相对 myquant 的路径（禁 basename）
    "monitoring/alerting.py", "live/engine.py", "core/event_bus.py",
    "live/scheduler.py", "monitoring/metrics_collector.py",
    "monitoring/action_engine.py", "data_access/manager.py",
    "live/brokers/qmt_broker.py", "live/data_feed.py",
    "gui/backtest_config_dialog.py", "risk/risk_manager.py",
    "web/dashboard.py", "ml/training_pipeline.py",
    "monitoring/dynamic_baseline.py",
}
OVERSIZE_WARN_MARGIN = 1500      # >=1500 即临界预警（mixed_engine.py 恰 1500）

TEST_NAME_PATTERN = re.compile(r"^test_p\d+_.*\.py$")
TEST_NAME_FROZEN = {
    "test_p2_anti_future_config.py", "test_p104_fee_parity.py",
    "test_p106_report_date_normalize.py", "test_p113_webhook_nonblock.py",
    "test_p116_rsrs_validate.py", "test_p117_cn_column_residue.py",
    "test_p118_non_cash_fixes.py", "test_p119_fingerprint_index.py",
    "test_p211_terminal_state.py", "test_p212_factor_annualize.py",
}

STRATEGY_FORBIDDEN_IMPORTS = {
    "sqlite3", "requests", "urllib", "httpx", "pika", "redis", "pymongo",
    "os", "pathlib", "shutil", "socket", "subprocess",
}
STRATEGY_IO_ALLOWLIST = {
    "myquant/strategies/multi_factor_strategy.py",
}

RAW_ACCESS_PATTERNS = [
    re.compile(r"""["']datasets["']\s*/\s*["']raw["']"""),
    re.compile(r"""datasets[\\/]raw"""),
]
RAW_SCAN_LAYERS = {"L1", "L3", "L5"}
RAW_ALLOWLIST = {"myquant/data_access/financial_sync_service.py"}

# R-19 零覆盖基线：未冻结（空）。冻结流程：G0-9 跑 pytest-cov 得真实覆盖后，
# 把当前全部零覆盖业务模块写入此集合；此后"新增零覆盖即红"。
# 注意：本判定为"引用级"（被 tests import 即视为覆盖），非执行级覆盖；真覆盖由 cov 判定。
ZERO_COVERAGE_BASELINE: set[str] = set()


# ═══════════════════════════ 3) 工具 ═══════════════════════════
@functools.lru_cache(maxsize=1)
def _all_py_files() -> tuple[Path, ...]:
    return tuple(sorted(p for p in MYQUANT.rglob("*.py") if "__pycache__" not in p.parts))


@functools.lru_cache(maxsize=None)
def _read_text(path: str) -> str:
    """K-c：utf-8-sig 读取，兼容 UTF-8 BOM 文件（v4 用 utf-8 必炸 manager.py/data_download_dialog.py）。
    errors="replace" 兜底仅对无法识别的字节容错，任何 _parse is None 的文件由 R-21 兜底必红。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@functools.lru_cache(maxsize=None)
def _parse(path: str) -> ast.Module | None:
    try:
        return ast.parse(_read_text(path), filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return None


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


@functools.lru_cache(maxsize=None)
def _src_module(path: str) -> str:
    """把文件路径映射为模块名（包 __init__.py → 包名）。"""
    p = Path(path)
    parts = p.relative_to(MYQUANT).parts
    if p.name == "__init__.py":
        return "myquant." + ".".join(parts[:-1])
    return "myquant." + ".".join(parts[:-1] + (p.stem,))


def _is_main_guard(node: ast.AST) -> bool:
    """识别 `if __name__ == "__main__":` 自测/脚本块（K-f：其内 import 归入 selfcheck）。"""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__"
                for c in node.test.comparators)
    )


@functools.lru_cache(maxsize=None)
def _imports(path_str: str) -> tuple[tuple[str, int, str], ...]:
    """返回 ((目标模块, 行号, 语境), ...)；语境 ∈ {main, typing, func, top}。

    精确分类：`if __name__ == "__main__":` 自测块 / TYPE_CHECKING 块 / 函数体 / 模块顶层。
    相对导入按 PEP 328 解析为绝对模块名。
    """
    path = Path(path_str)
    tree = _parse(path_str)
    if tree is None:
        return ()
    pkg = ("myquant", *path.relative_to(MYQUANT).parts[:-1])

    main_ranges = sorted(
        (n.lineno, n.end_lineno or n.lineno)
        for n in ast.walk(tree) if _is_main_guard(n)
    )
    typing_ranges = sorted(
        (n.lineno, n.end_lineno or n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING"
    )
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    func_ranges = [(f.lineno, f.end_lineno or f.lineno) for f in funcs]

    def ctx(lineno: int) -> str:
        if any(s <= lineno <= e for s, e in main_ranges):
            return "main"
        if any(s <= lineno <= e for s, e in typing_ranges):
            return "typing"
        if any(s <= lineno <= e for s, e in func_ranges):
            return "func"
        return "top"

    def resolve(node: ast.ImportFrom) -> str | None:
        if node.level == 0:
            return node.module
        if node.level > len(pkg):
            return None
        base = pkg[: len(pkg) - (node.level - 1)]
        suffix = node.module.split(".") if node.module else []
        return ".".join((*base, *suffix))

    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno, ctx(node.lineno)))
        elif isinstance(node, ast.ImportFrom):
            mod = resolve(node)
            if mod:
                out.append((mod, node.lineno, ctx(node.lineno)))
    return tuple(out)


def _layer_of_module(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != "myquant":
        return None
    return MODULE_LAYER.get(f"myquant.{parts[1]}")


def _layer_of_file(path: Path) -> str | None:
    return _layer_of_module(f"myquant.{path.relative_to(MYQUANT).parts[0]}")


def _top_pkg(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "myquant":
        return parts[1]
    return None


def _is_allowed(src: str, dst: str) -> bool:
    if src == dst or dst in SAME_OK.get(src, set()):
        return True
    return LAYER_ORDER.index(dst) < LAYER_ORDER.index(src)


def _violations(*, top_only: bool) -> list[tuple[str, int, str, str, str, str]]:
    """返回跨层反向 import（排除 typing 与 main 语境）。
    行: (文件, 行, 目标, 源层, 目标层, 语境)。同层互引不计入（由 R-22 报告）。"""
    rows: list[tuple[str, int, str, str, str, str]] = []
    for path in _all_py_files():
        src_layer = _layer_of_file(path)
        if src_layer is None:
            continue
        src_mod = _src_module(str(path))
        for target, lineno, c in _imports(str(path)):
            if c in ("typing", "main") or (top_only and c != "top"):
                continue
            if target == src_mod:
                continue
            dst_layer = _layer_of_module(target)
            if dst_layer is None or _is_allowed(src_layer, dst_layer):
                continue
            rows.append((_rel(path), lineno, target, src_layer, dst_layer, c))
    return rows


# ── 环检测（K-b：统一模块名命名空间；K-f：排除 __main__；S-1：白名单式包装环剔除） ──
# 允许的 re-export 包装环包（单顶层包 + 含该包 __init__ 的惯用环才豁免；其余一律算真环）
_PACKAGING_CYCLE_ALLOW = frozenset({
    "core", "utils", "data_access", "indicators", "live", "strategies",
})


def _module_graph(top_only: bool) -> dict[str, set[str]]:
    g: dict[str, set[str]] = defaultdict(set)
    for path in _all_py_files():
        src = _src_module(str(path))
        for target, _ln, c in _imports(str(path)):
            if c in ("typing", "main") or (top_only and c != "top"):
                continue
            if target.startswith("myquant.") and target != src:
                g[src].add(target)
    return g


def _is_packaging_cycle(scc: list[str]) -> bool:
    """白名单式：仅当 SCC 是"单顶层包 + 含该包 __init__ + 包名在白名单"时判为包装惯用环。

    白名单之外的任何单包 __init__ 环一律算真环（防 monitoring 未来加 re-export 被静默豁免）。
    """
    pkgs = {_top_pkg(m) for m in scc if _top_pkg(m)}
    if len(pkgs) != 1:
        return False
    pkg = pkgs.pop()
    return f"myquant.{pkg}" in scc and pkg in _PACKAGING_CYCLE_ALLOW


def _tarjan(graph: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = [0]
    cycles: list[list[str]] = []
    nodes = set(graph) | {t for ts in graph.values() for t in ts}

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                cycles.append(sorted(scc))

    for v in nodes:
        if v not in index:
            strongconnect(v)
    return cycles


def _real_cycles(top_only: bool) -> list[list[str]]:
    return [scc for scc in _tarjan(_module_graph(top_only)) if not _is_packaging_cycle(scc)]


# ═══════════════════════════════ 4) 用例 ═══════════════════════════════
class TestR01aTopLevelCycle:
    """R-01a：模块顶层导入图不得有真环（剔除包装惯用环与 __main__）。基线 0。"""

    def test_no_top_level_cycle(self):
        cycles = _real_cycles(top_only=True)
        assert len(cycles) <= TOP_LEVEL_CYCLE_BASELINE, (
            "模块顶层导入存在真环（须打断）：\n" + "\n".join(str(c) for c in cycles)
        )


class TestR01bLazyCycle:
    """R-01b：全量导入图真环数只减不增（基线 3）。"""

    def test_lazy_cycle_not_rising(self):
        cycles = _real_cycles(top_only=False)
        assert len(cycles) <= LAZY_CYCLE_BASELINE, (
            f"全量导入图真环数上升: {len(cycles)} > {LAZY_CYCLE_BASELINE}\n"
            + "\n".join(str(c) for c in cycles)
        )


class TestR02CrossLayer:
    """R-02：跨层反向 import；例外须登记且未过期且非假豁免。基线 40。"""

    def test_count_not_rising(self):
        found = _violations(top_only=False)
        assert len(found) <= VIOLATION_BASELINE, (
            f"跨层反向 import 上升: {len(found)} > {VIOLATION_BASELINE}\n"
            + "\n".join(f"  {f}:{ln} {s}->{d} {t}" for f, ln, t, s, d, _c in found)
        )

    def test_all_registered(self):
        bad = [f"{f}:{ln} -> {t}"
               for f, ln, t, _s, _d, _c in _violations(top_only=False)
               if (f, t) not in EXCEPTIONS and (f, t) not in P0_MUST_FIX]
        assert not bad, "未登记的跨层引用：\n" + "\n".join(bad)

    def test_no_expired_exception(self):
        today = date.today()
        bad = [f"{k[0]} -> {k[1]}（{v['id']} 到期 {v['expires']}，任务 {v['task']}）"
               for k, v in EXCEPTIONS.items() if v["expires"] < today]
        assert not bad, "例外已到期未收敛：\n" + "\n".join(bad)

    def test_no_ghost_exception(self):
        actual = {(f, t) for f, _l, t, _s, _d, _c in _violations(top_only=False)}
        ghosts = [f"{k[0]} -> {k[1]}（{v['id']}）"
                  for k, v in EXCEPTIONS.items()
                  if k not in actual and v["kind"] == "runtime"]
        assert not ghosts, "假豁免（代码中已不存在或并非违规，须删除）：\n" + "\n".join(ghosts)


class TestR05StrategyNoIO:
    """R-05：策略层禁止直接访问 DB/网络/文件系统。"""

    @pytest.mark.parametrize(
        "py",
        [p for p in _all_py_files() if p.parts[-2] == "strategies"],
        ids=lambda p: p.relative_to(MYQUANT).as_posix(),
    )
    def test_no_io(self, py: Path):
        if _rel(py) in STRATEGY_IO_ALLOWLIST:
            pytest.skip("存量冻结（R-05 白名单，P1 清理）")
        bad = [f"L{ln} {m.split('.')[0]}" for m, ln, _c in _imports(str(py))
               if m.split(".")[0] in STRATEGY_FORBIDDEN_IMPORTS]
        assert not bad, f"{_rel(py)} 策略层禁止 IO：{bad}"


class TestR09TestNaming:
    """R-09：禁止新增 test_p<编号>_*.py（存量冻结 10 个）。"""

    def test_no_new_naming(self):
        bad = [p.name for p in (PROJECT_ROOT / "tests").rglob("test_*.py")
               if TEST_NAME_PATTERN.match(p.name) and p.name not in TEST_NAME_FROZEN]
        assert not bad, f"新增编号式测试命名：{bad}"


class TestR11RawData:
    """R-11：L1/L3/L5 禁止直读 datasets/raw/。"""

    def test_no_raw_access(self):
        bad = []
        for py in _all_py_files():
            rel = _rel(py)
            if _layer_of_file(py) not in RAW_SCAN_LAYERS or rel in RAW_ALLOWLIST:
                continue
            src = _read_text(str(py))
            bad += [f"{rel} 命中 {pat.pattern}" for pat in RAW_ACCESS_PATTERNS if pat.search(src)]
        assert not bad, "禁止直读 datasets/raw/：\n" + "\n".join(bad)


class TestR13DynamicImport:
    """R-13：禁止任何层动态 import myquant.*（须静态可审计）。"""

    PATTERNS = [
        re.compile(r"""importlib\.import_module\s*\(\s*["']myquant\."""),
        re.compile(r"""__import__\s*\(\s*["']myquant\."""),
    ]

    def test_no_dynamic(self):
        bad = []
        for py in _all_py_files():
            src = _read_text(str(py))
            for pat in self.PATTERNS:
                bad += [f"{_rel(py)}:{src[:m.start()].count(chr(10)) + 1}"
                        for m in pat.finditer(src)]
        assert not bad, "禁止动态 import：\n" + "\n".join(bad)


class TestR14Oversize:
    """R-14：禁止新增 >1500 行文件（白名单按相对路径，14 项）。"""

    def test_no_new_oversize(self):
        oversize, warn = [], []
        for py in _all_py_files():
            lines = len(_read_text(str(py)).splitlines())
            rel = py.relative_to(MYQUANT).as_posix()
            if lines > OVERSIZE_LIMIT and rel not in OVERSIZE_WHITELIST:
                oversize.append(f"{rel} ({lines})")
            elif lines >= OVERSIZE_WARN_MARGIN:
                warn.append(f"{rel} ({lines})")
        assert not oversize, (
            f"新增超 {OVERSIZE_LIMIT} 行文件：\n" + "\n".join(oversize)
            + "\n临界预警：\n" + "\n".join(warn)
        )


class TestR16AssemblerScope:
    """R-16：live 层的 monitoring 装配只允许 scheduler.py（已登记例外除外）。

    K-e：v4 的 R-16 未排除 EXCEPTIONS（EX-12A/B 的 portfolio->monitoring），
    上线即红；本版与例外表保持一致。
    """

    def test_only_scheduler(self):
        exc_or_p0 = set(EXCEPTIONS) | P0_MUST_FIX
        bad = [f"{f}:{ln} -> {t}"
               for f, ln, t, _s, _d, _c in _violations(top_only=False)
               if t.startswith("myquant.monitoring")
               and f.startswith("myquant/live/")
               and not f.endswith("live/scheduler.py")
               and (f, t) not in exc_or_p0]
        assert not bad, "live 层新增 L4->L5 装配（非 scheduler 且未登记例外）：\n" + "\n".join(bad)


class TestR19ZeroReference:
    """R-19：零覆盖模块只减不增（待校准；引用级判定，非执行覆盖）。

    关键事实（全盘复查）：文档的「15 个零覆盖模块清单」是抽查，不是全量——
    实测字符串级零引用 74 个、精确 import 级 68 个（含 __init__ 后）。真实覆盖率
    必须由 pytest-cov 插桩（G0-9）判定；本判定为"被 tests import 即视为覆盖"的
    引用级近似（S-3 诚实命名：TestR19ZeroReference，真覆盖由 cov 判定）。
    因此在覆盖率基线未冻结前，本规则只收集报告、不设 CI 阻断；冻结方式：
    先跑 pytest-cov 得到真实覆盖，把当前全部零覆盖业务模块写入
    ZERO_COVERAGE_BASELINE，此后"新增零覆盖即红"。
    """

    @staticmethod
    def _tests_imported_modules() -> set[str]:
        refs: set[str] = set()
        for p in (PROJECT_ROOT / "tests").rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                tree = ast.parse(_read_text(str(p)), filename=str(p))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith("myquant"):
                            refs.add(a.name)
                elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("myquant"):
                    refs.add(node.module)
        return refs

    def test_report_zero_coverage(self):
        if not ZERO_COVERAGE_BASELINE:
            pytest.skip("覆盖率基线未冻结（需 pytest-cov 校准，见 G0-9）；本规则当前仅报告")
        refs = self._tests_imported_modules()
        current_zero = {
            p.relative_to(PROJECT_ROOT).as_posix()
            for p in _all_py_files()
            if p.name != "__init__.py" and _src_module(str(p)) not in refs
        }
        newly = current_zero - ZERO_COVERAGE_BASELINE
        assert not newly, (
            "新增零覆盖模块（不在存量基线内）：\n" + "\n".join(sorted(newly))
            + f"\n当前零覆盖总数：{len(current_zero)}"
        )


class TestR20TestCount:
    """R-20：测试函数计数基线（3310）只增不降（作为门禁层内的近似兜底；
    权威计数在 CI 步骤）。注：口径仅 tests/unit，与 pyproject testpaths
    （tests/unit + tests/benchmarks）不完全同源（S-5 口径声明）。"""

    def test_function_count_not_decreasing(self):
        count = 0
        for p in (PROJECT_ROOT / "tests" / "unit").rglob("test_*.py"):
            count += len(re.findall(r"^\s*def\s+test_", _read_text(str(p)), re.M))
        assert count >= 3310, f"测试函数数下降: {count} < 3310"


class TestR21NoUnparsableFile:
    """R-21：门禁自检——任何 myquant 文件必须可解析（_parse is None 即红）。

    封堵 v4.1 的盲区：errors="replace" 兜底让畸形编码被当作零 import，静默失明
    （评审 B-4）。这是"门禁自身可信度"的元门禁。
    """

    def test_no_unparsable_file(self):
        bad = [_rel(p) for p in _all_py_files() if _parse(str(p)) is None]
        assert not bad, "无法解析的文件（门禁盲区，须修复编码/语法）：\n" + "\n".join(bad)


class TestR22SelfLayerReport:
    """R-22：同层互引计数（只报告、不阻断）。

    使 monitoring 内部互引（含 alerting↔metrics_collector 等）、core/utils 内部
    耦合可度量、可递减（评审 B-5）；这是 G2 拆包的驱动指标。不作为 CI 阻断。
    """

    def test_report_self_layer_imports(self):
        per_layer: dict[str, int] = defaultdict(int)
        per_pkg: dict[str, int] = defaultdict(int)
        for path in _all_py_files():
            src_layer = _layer_of_file(path)
            if src_layer is None:
                continue
            pkg = _top_pkg(_src_module(str(path))) or "?"
            src_mod = _src_module(str(path))
            for target, _ln, c in _imports(str(path)):
                if c in ("typing", "main"):
                    continue
                if target == src_mod:
                    continue
                dst_layer = _layer_of_module(target)
                if dst_layer == src_layer:
                    per_layer[src_layer] += 1
                    per_pkg[pkg] += 1
        print("\nR-22 同层互引计数（仅报告，不阻断）")
        print("  按层:", dict(sorted(per_layer.items())))
        print("  按顶层包:", dict(sorted(per_pkg.items())))
