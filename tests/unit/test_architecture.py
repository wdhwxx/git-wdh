# tests/unit/test_architecture.py
"""架构分层门禁 v4.9（G3 常态四条新门禁，回写#267）。

v4.9（G3 常态，回写#267）：新增/强化四条常态门禁——
  L-n  R-16 装配器集中（强化，替代窄版 live→monitoring）：L0-L4 层禁止直接
       实例化 L5 组件类（monitoring/acceptance 导出的类）；跨层组件装配必须经
       core.registry 工厂（get_component / get_* 工厂）或装配器（run_实盘入口.py
       setup_monitoring_linkage），禁止跨层裸类实例化。基线 0（阻断）。
  L-o  R-17 事件契约：事件发布/订阅必须引用 EventType 契约（EventType 成员值），
       禁止 ad-hoc 字符串事件类型（非 main 语境）；订阅允许 '*' 通配符。
       基线 0（阻断）；另报告未被消费的 EventType 成员（契约完备性驱动）。
  L-p  R-18 脚本自举：scripts/ 薄入口纪律——禁止新增业务类定义；存量 8 个类定义
       （4 文件：双券商模拟实盘/实时行情驱动/实盘模拟回测 3 个策略 + 验收_回测引擎V5
       5 个验收策略）冻结允许（SCRIPT_CLASS_ALLOW），新增脚本类即红。
  L-q  R-08 配置反查：登记的关键配置键（settings.yaml monitoring 段真源）的每段
       必须被 myquant 消费（get_monitoring_config().get("...") 反查）；死配置键
       （登记但代码不读）即红。基线 = 当前已消费键集合。

v4.7（G2-5 阶段二，回写#265）：scheduler 对 monitoring 类实例化装配注册表化——
  L-m  EX-13B/D/H/I 消除：meta_monitor/watchdog/weekly_review/action_engine 四模块
       导入即 register_component 注册装配工厂（core.registry），live/scheduler 仅
       依赖 L0（core.registry/core.constants）取用并调用；watchdog 3 个装配常量
       （STALENESS_PROBE_INTERVAL_SECONDS/BROKER_PROBE_INTERVAL_SECONDS/
       RUNBOOK_BROKER_DISCONNECT）下沉 core/constants（watchdog re-export 兼容）；
       装配责任上移 run_实盘入口.py setup_monitoring_linkage（先于 attach_engine）。
       违规 13→1（仅剩永久 EX-01A）、例外表 5→1 条；VIOLATION_BASELINE 13→1。

本版在 v4.2 基础上的修正（新增 L-a~L-c）：
  L-a  新增 R-23 入口白名单冻结：scripts/（41 个，含 故障演练/灾备演练 子目录）与
       根目录（12 个 .py + 一键部署.bat）入口全量登记；新增入口未登记即红。
       新增入口必须先更新蓝图目录树 + 本白名单（代码变更治理规则 §1.3 脚本薄入口纪律）。
  L-b  新增 R-24 薄化冻结：OVERSIZE 白名单 14 项行数基线实测锁定（2026-10-08），
       白名单内文件行数只减不增（存量继续膨胀即红）；新增 >1500 仍由 R-14 拦截。
  L-c  基线锁定（G0 完成）：ZERO_COVERAGE_BASELINE 填入实测引用级零覆盖业务模块 49 个，
       R-19 由"仅报告/skip"转 CI 阻断（新增零覆盖即红）。
       G0-9 补测（回写#266）：14 模块新增测试文件后从基线移除，49→35。
  L-d  G1 例外收敛（回写#258）：EX-07B（cache_sync 因子缓存实例注入）、EX-08A
       （financial_service 因子引擎工厂注入）、EX-11A/B（rules 告警/指标显式注入）、
       EX-11C（guardian 准入守卫注册表）、EX-15A（MonitorCenterDataProvider 下沉 monitoring）
       六项消除；例外表 25→19 对；VIOLATION_BASELINE 34→27。
  L-e  G1 例外收敛次批（回写#259）：EX-12A/B（portfolio 对账器/告警管理器构造注入 +
       scheduler 装配器装配）、EX-12C/D（portfolio/run_mode 改经 guardian 注册表访问器
       取准入守卫，消除 live→api.auth 反向）；新增 EX-13K 登记 scheduler→
       monitoring.account_reconciler（装配器，同 EX-13 族）；例外表 19→16 对；
       VIOLATION_BASELINE 27→24（EX-12 四项消除 -4，EX-13K 装配引用新增 +1）；
       LAZY_CYCLE_BASELINE 3→2（环3 portfolio↔account_reconciler 随 EX-12A 消除）。
  L-f  G1 例外收敛三批（回写#259）：EX-09A（backtest.engine 撮合执行器工厂
       register_broker_factory/broker_factory 注入，装配责任上移 scripts/tests/
       批量回测入口，进程池子进程经 parallel.set_worker_initializer 重装配）、
       EX-09B（order_matcher SimBroker 真值对照段移入 __main__ 语境）；例外表
       16→14 对；VIOLATION_BASELINE 24→22。G1 限期例外全部清零（仅剩 EX-01A/
       10A/B/13A~K 永久型 14 对）。
       —— 权威基线冻结：违规 22 / 全量真环 2 / 顶层 0 / 例外 14 / 零覆盖存量 49。
       （R-19 为"被 tests import 即视为覆盖"的引用级近似；真实覆盖由 pytest-cov 判定，
        cov 实测 58% / 零覆盖业务 12 个仅作 G0-9 补测清单，二者口径不同不混用。
        G0-9 补测回写#266 后零覆盖存量 49→35，见基线注释。）

v4.2（K-f~K-h）与 v4.1（K-a~K-e）的修正结论全部沿用，摘要如下：
  K-f  修复 `_imports()` 的 __main__ 盲区：`if __name__ == "__main__":` 自测块内的
       import 归入新语境 "main"（selfcheck），环图与违规计数均排除。
       —— 这是上一轮深度评审 B-2 抓到的真缺陷：strategies/* -> backtest.engine 的
       6 处全在自测块（ma:763/768、grid:477/482、multi_factor:1272/1277、
       momentum:581/586、kunpeng:956/959、mean_reversion:497/502），v4.1 把它们当 top，
       制造了伪环。修正后全量真环 = 3（环1 core.event_bus↔utils.memory_pool、
       环2 monitoring 内部 4 模块、环3 live.portfolio↔monitoring.account_reconciler），
       LAZY_CYCLE_BASELINE 由 4 改 3。回写#259：EX-12A 消除后环3 消失，3→2。
       同一盲区还误计了违规：utils/parallel.py:945/970/988 -> factor_cache 三条
       也在 __main__ 自测块，v4/v4.1 把它们当违规计入（L0->L2=6），排除后 L0->L2=3，
       权威违规由 40 修正为 37；回写#255 P0-1/P0-2 消除后再由 37 修正为 35。
  K-g  新增 R-21：任何 myquant 文件必须可解析，_parse is None 即红 —— 封堵 v4.1 的
       errors="replace" 兜底导致的"畸形编码静默归零"盲区（评审 B-4）。
  K-h  新增 R-22：同层互引计数（只报告不阻断）—— 使 monitoring 153 处内部互引（#260 降至 111、
       #262 拆出 6 模块至 acceptance/ 后降至 74，acceptance 包内 36，L5 合计 110；
       #263 存储/基础设施子层下沉 core/ 后 monitoring 降至 56、core 38、L5 合计 82）、
       core/utils 内部耦合可度量、可递减（评审 B-5；这是 G2 拆包的驱动指标）。

另按评审采纳：S-1 `_is_packaging_cycle` 改为白名单式（显式枚举允许的 re-export
包装环包，防未来 monitoring 真环被静默豁免）；S-6 删除 _init_modules 死代码；
S-3 R-19 更名 TestR19ZeroReference（诚实命名：引用级判定，非执行覆盖）。

权威台账：docs/系统性审核/整改台账_2026Q4.md
基线口径：违规=22（语义矩阵 α，排除 TYPE_CHECKING 与 __main__ 自测块；
          P0-1/P0-2 后 37→35（#255），EX-07A 后 35→34（#257），G1 六项后 34→27（#258），
          EX-12 四项消除且 EX-13K 装配引用新增后 27→24（#259），EX-09 两项后 24→22）；
          全量真环=2（另 1 组 strategies 包 re-export 惯用环为包装环，白名单剔除；
          环3 随 EX-12A 消除，回写#259）；
          顶层真环=0；>1500 行白名单 15 项（相对路径，行数基线见 OVERSIZE_LINE_BASELINE）；
          零覆盖存量 49 个引用级业务模块（2026-10-08 冻结，新增零覆盖即红，回写#256）。
运行状态：已在 conda_wdh 环境真实运行（门禁 30 passed/1 skipped：G3 新增 R-17/R-18/R-08
          三条 + R-16 强化后，R-05 白名单 1 项按设计 skip；#266 前为 26 passed/1 skipped）。

v5.0（P0-00 子步骤 D，回写#300）：新增 R-25 包登记元门禁——
  R-25  未登记包判红：myquant/ 下真实包（含 __init__.py 的一级目录）每个必须在
        MODULE_LAYER 登记，未登记即红；反向校验登记表不得藏幽灵包（登记了但目录不存在）。
        封堵 `_layer_of_module` 对未登记包返回 None → 下游 R-02/R-11/R-16/R-22 一律
        continue 跳过导致"新包整体游离于分层门禁之外"的治理盲区；不改 _layer_of_module
        既有语义。基线 = 2026-10-11 实测 20 个顶层包全部登记（无未登记、无幽灵）。
        这是 P0-01 realism_gate 新增 `myquant.realism_gate`(L0) 登记的前置门禁。
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
    "myquant.core": "L0", "myquant.utils": "L0", "myquant.database": "L0", "myquant.leakage_gate": "L0",
    "myquant.realism_gate": "L0",  # P0-01：回测真实性门禁（与 leakage_gate 同构同层）
    "myquant.data_access": "L1",
    "myquant.models": "L2", "myquant.indicators": "L2",
    "myquant.engine": "L2E",
    "myquant.strategies": "L3", "myquant.backtest": "L3",
    "myquant.allocation": "L3", "myquant.risk": "L3", "myquant.ml": "L3",
    "myquant.execution": "L4", "myquant.live": "L4",
    "myquant.monitoring": "L5", "myquant.acceptance": "L5",  # acceptance/：验收+演练域（#262 从 monitoring 拆出）
    "myquant.api": "L6", "myquant.gui": "L6", "myquant.web": "L6",
}
LAYER_ORDER = ["L0", "L1", "L2", "L2E", "L3", "L4", "L5", "L6"]
# 同层/近层互通：L2 与 L2E 视为同级
SAME_OK: dict[str, set[str]] = {"L2": {"L2E"}, "L2E": {"L2"}}


# ═══════════════════════ 2) 例外表（点对点 + 到期日 + 台账；13 对 + P0 1 对，回写#263 消 6 条） ═══════════════════════
# 字段: id, 源文件(相对项目根), 目标模块, 收敛任务, 到期日, kind, 说明
_EXC = [
    ("EX-01A", "myquant/database/trade_log.py", "myquant.models.trade",
     "永久", "2099-12-31", "runtime", "ORM 必引领域模型"),
    # EX-10A/10B/13A/F/G/J 已随 #263 下沉消除（audit_log/metrics_history/health_check/alert_store 迁 core/，L3/L4→L0 合法）
    # EX-13C/E/K 已随 #264 注册表化消除（scheduler 经 core.registry 取 alert_manager/metrics_collector/account_reconciler 工厂，L4→L0 合法）
    # EX-13B/D/H/I 已随 #265 阶段二消除（meta_monitor/watchdog/weekly_review/action_engine 类实例化装配注册表化，L4→L0 合法）
    # 实盘安全门禁第0阶段施工例外（回写#287/#290/#294，蓝图已登记）：
    # 回写#422：EX-14A/B/E/F 随拆包迁至 engine_assembly.py（装配代码迁出 engine.py，豁免随代码迁移）
    ("EX-14A", "myquant/live/engine_assembly.py", "myquant.monitoring",
     "永久", "2099-12-31", "runtime", "0.1 熔断入进程：create_live_engine 装配 CircuitBreaker（施工清单§2，装配责任集中在引擎装配段；拆包#422 自 engine.py 迁入）"),
    ("EX-14B", "myquant/live/engine_assembly.py", "myquant.monitoring.circuit_breaker",
     "永久", "2099-12-31", "runtime", "0.1 熔断入进程：直接 import circuit_breaker 子模块实例化熔断器（拆包#422 自 engine.py 迁入）"),
    ("EX-14C", "myquant/acceptance/live_safety_gate.py", "myquant.api.middleware.auth",
     "永久", "2099-12-31", "runtime", "验收层直接断言 OperationGuard 审计留痕（0.4/0.9 验收，验收层可访问任何层）"),
    ("EX-14D", "myquant/live/brokers/base_broker.py", "myquant.api.middleware.auth",
     "永久", "2099-12-31", "runtime", "0.9 提交边界硬闸：_gate_reject 经 get_operation_guard() 留审计（薄闸审计是 broker 基类永久契约）"),
    ("EX-14E", "myquant/live/engine_assembly.py", "engine_assembly",
     "永久", "2099-12-31", "assembly", "R-16 装配例外：0.1 熔断入进程在 create_live_engine 直接实例化 CircuitBreaker（施工清单§2 落点即引擎装配段，蓝图#287；拆包#422 自 engine.py 迁入）；非跨层 import 例外（不参与 R-02 ghost 检查）"),
    ("EX-14F", "myquant/live/engine_assembly.py", "myquant.monitoring.alerting",
     "永久", "2099-12-31", "runtime", "0.8 启动自检断言：告警渠道配置非空检查经 get_alert_manager()（外部依赖降级检查，蓝图#295；拆包#422 自 engine.py 迁入）"),
]
EXCEPTIONS: dict[tuple[str, str], dict] = {
    (f, t): {"id": i, "task": tk, "expires": date.fromisoformat(e), "kind": k, "why": w}
    for i, f, t, tk, e, k, w in _EXC
}

# P0 必修（无例外）：完成后必须从代码中消除，并从 P0_MUST_FIX 删除
# 回写#255：P0-1（performance_config→ma_strategy）与 P0-2（backtest.engine→live.engine）已消除，集合清空
P0_MUST_FIX: set[tuple[str, str]] = set()

VIOLATION_BASELINE = 9           # K-a + K-f：语义矩阵 α，排除 TYPE_CHECKING 与 __main__；#255 37→35，#257 35→34，#258 34→27，#259 前段 27→24（EX-12-4 + EX-13K+1），EX-09A/B 后 24→22；#263 下沉消除 EX-10A/10B/13A/F/G/J 后 22→16；#264 注册表化消除 EX-13C/E/K 后 16→13；#265 阶段二消除 EX-13B/D/H/I 后 13→1（仅剩永久 EX-01A）；#294 实盘安全门禁施工登记 EX-14A~E 后 1→7（engine 熔断装配×2、验收审计×3、base_broker 薄闸审计×1）；#295 0.8 自检登记 EX-14F 后 7→8（engine 告警渠道断言×1）；#296 LIVE 门槛验收再增 acceptance→auth 引用后 8→9（EX-14C 文件级已豁免，计数按全部反向 import）
TOP_LEVEL_CYCLE_BASELINE = 0     # K-b：顶层真环（剔除包装惯用环与 __main__ 后）
LAZY_CYCLE_BASELINE = 0          # K-f：全量真环，只减不增（#261 环1 event_bus↔memory_pool 随事件池注入化
                                 #      打断——全系统真环清零；环2 随 G2-1，#260；环3 随 EX-12A，#259）

OVERSIZE_LIMIT = 1500
OVERSIZE_WHITELIST = {           # 相对 myquant 的路径（禁 basename）
    "monitoring/alerting.py", "live/engine.py", "core/event_bus.py",
    "live/scheduler.py", "monitoring/metrics_collector.py",
    "monitoring/action_engine.py", "data_access/manager.py",
    "live/brokers/qmt_broker.py", "live/data_feed.py",  # 回写#423：data_feed 拆桥瘦身 1605→1336（<1500），移出白名单受标准 R-14 约束
    "gui/backtest_config_dialog.py", "risk/risk_manager.py",
    "web/dashboard.py", "ml/training_pipeline.py",
    "monitoring/dynamic_baseline.py", "acceptance/live_safety_gate.py",  # 回写#296：LIVE 门槛验收（0.1~0.9 + LIVE 用例13/F-6/R-123）增至 1749 行
    "backtest/engine.py",  # 回写#349：核心引擎（create_engine 装配 + 三引擎共用
                          # _attach_realism_manifest 模块级函数 + 门禁收尾刷新）超 1500
                          # 行，加入白名单（R-24 薄化冻结：行数只减不增）
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

# ═══════════════════════════ 2b) 入口白名单冻结（R-23，G0 收尾回写#256） ═══════════════════════════
# 相对项目根；scripts/ 42 个（含 故障演练/灾备演练 子目录）+ 根目录 12 个 .py + 一键部署.bat。
# 新增入口必须：先更新蓝图目录树（docs/project_layout_gear2.md）+ 本白名单，否则门禁红。
ENTRY_ALLOWLIST: set[str] = {
    # scripts/（相对项目根，含子目录）
    "scripts/kunpeng_param_scan.py", "scripts/一致性校验.py", "scripts/一键平仓.py",
    "scripts/覆盖率门禁.py",  # ✨【回写#379】P1 覆盖率门禁薄入口（蓝图 #377 登记待建 → P1 落地）
    "scripts/门禁审计.py",  # ✨【回写#C-4·5】门禁参数变更审计薄入口（判据A 对价 + 判据B 蓝图登记）
    "scripts/仿真准入评估.py", "scripts/候选池交叉验证.py", "scripts/初始化项目目录.py",
    "scripts/参数优化.py", "scripts/双券商模拟实盘.py", "scripts/同步交易日历.py",
    "scripts/同步股本变动.py", "scripts/垃圾数据清理.py", "scripts/增量回测.py",
    "scripts/复权因子同步.py", "scripts/实时行情驱动.py", "scripts/实盘模拟回测.py",
    "scripts/性能基准测试.py", "scripts/推理延迟基准.py", "scripts/故障演练/季频故障演练.py",
    "scripts/数据下载.py", "scripts/数据全量清洗.py", "scripts/数据导出_DB到processed.py",
    "scripts/数据库初始化.py", "scripts/数据库迁移.py", "scripts/数据清洗.py",
    "scripts/标的预筛选.py", "scripts/清理历史回测输出.py", "scripts/灾备演练/年度灾备演练.py",
    "scripts/生成回测报告.py", "scripts/监控可测量性统计.py", "scripts/监控周复盘.py",
    "scripts/监控配置管理.py", "scripts/结果备份.py", "scripts/缓存管理.py",
    "scripts/静态扫描.py",  # M1B-07：静态扫描薄入口（R-18 薄入口，委托 leakage_gate.static_scan.main_cli）
    "scripts/财务CSV更新.py", "scripts/财务数据入库.py", "scripts/运行API.py",
    "scripts/验收_B包保钱包.py", "scripts/验收_C包保Alpha.py", "scripts/验收_D包自治.py",
    "scripts/验收_回测引擎V5.py", "scripts/验收_监控M1保命.py",
    "scripts/验收_实盘安全门禁.py",  # 回写#287：实盘安全门禁验收薄入口（委托 myquant/acceptance/live_safety_gate.py）
    "scripts/回测真实性基线.py",  # 回写#298：回测真实性基线建档/校验薄入口（委托 myquant/backtest/baseline_archive.py）
    "scripts/豁免管理.py",  # 回写#365：P1-14 豁免流管理薄入口（list/grant/approve/revoke，委托 myquant/leakage_gate/exemption.py）
    "scripts/台账consumers实测.py",  # 回写#P2-12：台账 consumers AST 实测薄入口（委托 leakage_gate.static_scan.collect_consumers_by_column）
    "scripts/配置死键扫描.py",  # 回写#P2-07：配置死键扫描薄入口（委托 leakage_gate.config_deadkey.scan_dead_keys）
    # 根目录入口（.py + .bat）
    "run_GUI入口.py", "run_全量清洗GUI.py", "run_回测入口.py", "run_回测配置GUI.py",
    "run_实盘入口.py", "run_批量回测.py", "run_数据下载GUI.py", "run_清理GUI.py",
    "run_鲲鹏一键应用推荐参数.py", "run_鲲鹏参数优化.py", "run_鲲鹏批量回测.py",
    "鲲鹏仿真运测.py", "一键部署.bat",
}

# ═══════════════════════════ 2d) G3 常态门禁基线（回写#267） ═══════════════════════════
# R-16 装配器集中：L0-L4 层禁止直接实例化 L5 组件类（monitoring/acceptance 导出的类）。
# 跨层组件装配必须经 core.registry 工厂（get_component / get_* 工厂）或装配器
# （run_实盘入口.py setup_monitoring_linkage）。基线 0（阻断）。
COMPONENT_HOME_PKGS = ("myquant.monitoring", "myquant.acceptance")

# R-18 脚本自举：scripts/ 薄入口纪律——禁止新增业务类定义；存量 8 个类定义冻结允许
# （4 文件：双券商模拟实盘/实时行情驱动/实盘模拟回测 3 个演示策略 + 验收_回测引擎V5 5 个验收策略）。
# 新增脚本类定义（不在本白名单）即红；存量清理只能缩。
SCRIPT_CLASS_ALLOW: frozenset[tuple[str, str]] = frozenset({
    ("scripts/双券商模拟实盘.py", "DualMAStrategy"),
    ("scripts/实时行情驱动.py", "ShortMAStrategy"),
    ("scripts/实盘模拟回测.py", "DualMAStrategy"),
    ("scripts/验收_回测引擎V5.py", "_FixedSwitchVecStrategy"),
    ("scripts/验收_回测引擎V5.py", "_TargetVecStrategy"),
    ("scripts/验收_回测引擎V5.py", "_TargetLoopStrategy"),
    ("scripts/验收_回测引擎V5.py", "_SignalStrategy"),
    ("scripts/验收_回测引擎V5.py", "_FixedSwitchLoopStrategy"),
})

# R-08 配置反查：登记的关键配置键（settings.yaml monitoring 段真源）的每段必须被
# myquant 消费（get_monitoring_config().get("...") 反查）。死配置键即红。
# 基线 = 当前已消费键（探针实测，2026-10-09）；只增不删，新增登记键必须接线。
CONFIG_KEY_REQUIRED: set[str] = {
    "monitoring.action_engine", "monitoring.alerts", "monitoring.watchdog",
    "monitoring.slo.data", "monitoring.slo.risk", "monitoring.slo.trading",
    "monitoring.slo.reconciliation", "monitoring.slo.engine_heartbeat",
    "monitoring.slo.exchange_rate_limit", "monitoring.slo.margin",
    "monitoring.slo.model", "monitoring.slo.signal_decay",
    "monitoring.slo.regime_matcher", "monitoring.slo.strategy",
    "monitoring.slo.backtest_consistency", "monitoring.slo.capacity",
    "monitoring.slo.dynamic_baseline",
}


# OVERSIZE 白名单 14 项行数基线（2026-10-08 实测冻结）：白名单内文件行数只减不增。
# 2026-10-10 实盘安全门禁第0阶段施工增长（蓝图回写#291/#293/#294/#295 已登记）：
#   engine 2869→2941（0.1 熔断入进程 + 0.5 启动门 + 0.6 pause hook + 0.9 薄闸状态源接线）→3063（0.8 启动自检断言 startup_assertions）
#   scheduler 2409→2419（0.5 watchdog 回调注入）
#   risk_manager 1901→2020（0.7 三条异常交易规则 + anomaly_rules 参数）
#   acceptance/live_safety_gate.py 1756→1749（0.1~0.9 + LIVE 门槛验收累计，#287~#296；入白名单）
OVERSIZE_LINE_BASELINE: dict[str, int] = {
    "monitoring/alerting.py": 3688, "live/engine.py": 3049, "core/event_bus.py": 2379,  # 2829→2835（#424 T4-4 fail-closed +6行）；2835→2850（#425 引擎预热/回放停止感知 _stop_requested +15 行功能必需）；2850→2869（T0-12 报单结果未知查单确认闭环 engine 端 ACK_UNKNOWN 处理 +19 行功能必需）；2869→2887（T0-10 恢复撤单移到 connect_all 后 +cancel 返回值检查 +18 行功能必需）；2887→2913（T0-11 pause 非 RUNNING 留痕 + force 参数 + _startup_blocked +26 行功能必需）；2913→2919（T0-7 启动断言 live fail-closed +6 行功能必需）；2919→2933（T0-4 对账周期调度断言 +_accountability_interval +14 行功能必需）；2933→2970（T1-2 intent-first 账本写入 +37 行功能必需）；2970→3029（T1-5/T1-6 持仓日快照+PnL快照周期任务 +59 行功能必需）；3029→3049（T1-8 落库连续失败计数+pause +20 行功能必需）
    "live/scheduler.py": 2419, "monitoring/metrics_collector.py": 2276,  # 2382→2409（#265 阶段二 7 处装配点改经注册表，代码行增长）；metrics 2268→2273→2276（#264 工厂注册代码；P0-2 ruff --fix 微增）
    "monitoring/action_engine.py": 2307, "data_access/manager.py": 2204,  # 2170→2186→2200→2204（#281 M1A-01/06 每股收益时点日基收敛+失败可观测化；M1A-10 available_time 增量派生+存量补列；P0-2 ruff --fix 微增）
    "live/brokers/qmt_broker.py": 2344, "live/data_feed.py": 1336,  # 2151→1605（#421 拆包瘦身：行情源层迁出 realtime_sources.py）；1605→1336（#423 拆包瘦身：实时桥层迁出 realtime_bridge.py，实测同步并移出 OVERSIZE_WHITELIST——1336<1500 受标准 R-14 约束，R-24 仍冻结 1336 防回升）；2170→2302（T0-12 报单结果未知查单确认闭环 _handle_unknown_submit + 查单/状态对齐 +132 行功能必需）；2302→2344（T0-12·2 order_timeout 活配置化 _build_ack_unknown_delays 动态退避 + load_config 读取 +42 行功能必需）
    "gui/backtest_config_dialog.py": 1871, "risk/risk_manager.py": 2020,
    "web/dashboard.py": 1760, "ml/training_pipeline.py": 1672,  # 1651→1672（#358 P1-11 训练出口泄漏审计报告薄接线 +21 行功能必需）
    "monitoring/dynamic_baseline.py": 1546,  # 1552→1546（#260 去全局采集器兜底）
    "acceptance/live_safety_gate.py": 1763,  # 回写#296：实盘安全门禁验收（0.1~0.9 + LIVE 门槛 31 项）；P0-2 ruff --fix 微增 1749→1757；1757→1759（#422 验收桩 patch 目标迁移 +2 行功能必需）；1759→1763（#422 ruff --fix 展开 import 块 +4 行格式化）
    "backtest/engine.py": 1869,  # T4-3 信号流记录 1858→1869
                                # 增量来源：三引擎共用 _attach_realism_manifest 模块级函数；
                                # 1524→1531（#350 基准数据治理：基准加载 adjust="raw" 语义注释）；
                                # 1531→1550（#354 P1-03 收尾：_attach 段缺失拒启——配置缺失=阻断，
                                # 方案 §4.3 fail-closed；只增不减的原则性豁免：功能必需行）；
                                # 1550→1599（#358 P1-11 回测出口泄漏审计报告 _write_backtest_
                                # leakage_report +49 行功能必需；P2-11 decision_time 取 config.end_date）；
                                # 1599→1681（#363 P1-13 回测期数据质量门禁：入口 quality 真实化
                                # _scan_entry_data_quality + 收尾 provider 累计回写（含真实覆盖率），
                                # +82 行功能必需）；
                                # 1681→1705（#364 P1-12 数据快照 pin：入口 fingerprint_hash/
                                # snapshot_id 由硬编码 None 改真实 verify_sidecar 采集，+24 行功能必需）；
                                # 1705→1716（#P2-03 基准净值接线：on_stop 从 equity_curve 各点
                                # benchmark_equity 组装 benchmark_curve 激活 alpha/beta/IR，+11 行功能必需）；
                                # 1716→1837（#371 P1-15 event 引擎结果侧不变量接线：新建 _run_realism_invariants
                                # 组合级 equity_curve→RI-3/4/6 + 成交流跨日重建持仓时间线→RI-5，on_stop 同构接线，
                                # +121 行功能必需））
}

# ─────────────────────────────── R-26 结构冻结（PR1，回写#420，2026-10-15 实测标定） ───────────────────────────────
# 三维度量口径（与现有语义矩阵一致，K-f 语境分类；实现见 _structure_metrics）：
#   lines         : splitlines 行数（与 R-24 同口径）
#   total_symbols : AST 遍历 FunctionDef/AsyncFunctionDef/ClassDef 计数，
#                   排除 __main__ 自测块与 TYPE_CHECKING 块（继承 _imports 语境语义）
#   fan_out       : 复用 _imports()，排除 main/typing 语境；模块归一为
#                   myquant.X.*→X（内部顶层包）、外部库→首段；去重集合大小
# 语义：白名单文件任一维度超基线即红（存量继续膨胀禁止）；任一维度超硬顶
#       （STRUCTURE_HARD_CAP）即红且无豁免——硬顶是防 PR2 对价机制被滥用为
#       无限抬升的死线；顶层包聚合任一维度超基线即红（防"横向分裂"：把白名单
#       文件拆成多个各自低于基线的新文件而包级总量不降）。
# 装配器类文件（R-16 装配器集中：live/engine、data_access/manager、web/dashboard 等）
# fan_out 天然偏高，冻结值已含当前状态；豁免通道留给 PR2 对价机制，本规则无豁免。
# 注意：web/dashboard.py 已由 R-24 基线 1760 瘦身至 1237（只减不增成果），
#       R-26 冻结当前实测值，不允许回弹（R-24 仍以 1760 判红，两规则独立生效）。
STRUCTURE_BASELINE: dict[str, dict[str, int]] = {
    "acceptance/live_safety_gate.py": {"lines": 1763, "symbols": 83, "fan_out": 29},  # 1757→1759（#422 验收桩 patch 目标迁移 +2 行）；1759→1763（#422 ruff --fix 展开 import 块 +4 行）
    "backtest/engine.py": {"lines": 1869, "symbols": 35, "fan_out": 13},  # T4-3 1858→1869
    "core/event_bus.py": {"lines": 2379, "symbols": 109, "fan_out": 11},
    "data_access/manager.py": {"lines": 2204, "symbols": 52, "fan_out": 13},
    "gui/backtest_config_dialog.py": {"lines": 1870, "symbols": 45, "fan_out": 13},
    "live/brokers/qmt_broker.py": {"lines": 2344, "symbols": 81, "fan_out": 9},  # 2170/79/8→2302/80/9（T0-12 报单结果未知查单确认闭环 _handle_unknown_submit 新增方法 + 真实委托/查询分支，三维实测同步功能必需）；2302/80/9→2344/81/9（T0-12·2 order_timeout 活配置化 _build_ack_unknown_delays 新增方法 +42行/+1符号功能必需）
    "live/data_feed.py": {"lines": 1336, "symbols": 42, "fan_out": 11},  # 2151/80/13→1605/55/11（#421 拆包瘦身实测同步）；1605/55/11→1336/42/11（#423 拆包瘦身：实时桥层迁出 realtime_bridge.py，-13 符号=桥类及方法，实测同步）
    "live/engine.py": {"lines": 3049, "symbols": 74, "fan_out": 15},  # T4-4 fail-closed 2829→2835；#425 引擎预热/回放停止感知 2835→2850（lines +15 功能必需，symbols/fan_out 不变）；2850→2869（T0-12 ACK_UNKNOWN 查单确认 engine 端分支 +19 行功能必需，symbols/fan_out 不变）；2869→2887（T0-10 恢复撤单时序调整 +cancel 返回值检查 +18 行功能必需，symbols/fan_out 不变）；2887→2913（T0-11 pause force参数 + _startup_blocked +26 行功能必需，symbols/fan_out 不变）；2913→2919（T0-7 启动断言 live fail-closed +6 行功能必需，symbols/fan_out 不变）；2919→2933（T0-4 对账周期调度断言 +_accountability_interval +14 行功能必需，symbols/fan_out 不变）；2933→2970（T1-2 intent-first 账本写入 +37 行功能必需，symbols/fan_out 不变）；2970→3029/74（T1-5/T1-6 持仓日快照+PnL快照周期任务 +59行/+2符号，fan_out 不变）；3029/74→3049/74（T1-8 落库连续失败计数+pause +20行，symbols/fan_out 不变）
    "live/scheduler.py": {"lines": 2419, "symbols": 84, "fan_out": 10},
    "ml/training_pipeline.py": {"lines": 1672, "symbols": 54, "fan_out": 10},
    "monitoring/action_engine.py": {"lines": 2307, "symbols": 86, "fan_out": 9},
    "monitoring/alerting.py": {"lines": 3688, "symbols": 145, "fan_out": 17},
    "monitoring/dynamic_baseline.py": {"lines": 1546, "symbols": 48, "fan_out": 9},
    "monitoring/metrics_collector.py": {"lines": 2276, "symbols": 58, "fan_out": 8},
    "risk/risk_manager.py": {"lines": 2020, "symbols": 65, "fan_out": 8},
    "web/dashboard.py": {"lines": 1237, "symbols": 44, "fan_out": 12},
}
# 硬顶 = 标定日各维度最大值×1.2 取整（lines 3688→4425 / symbols 145→174 / fan_out 29→34）。
# 与基线的区别：基线可经 PR2 对价机制抬升（功能必需 + 留痕）；硬顶不可逾越、无任何豁免通道。
STRUCTURE_HARD_CAP: dict[str, int] = {"lines": 4425, "symbols": 174, "fan_out": 34}
# 顶层包聚合基线（myquant/<pkg>/ 全文件，剔除 __init__.py 与 __pycache__；包口径同 R-25）。
# lines/symbols 求和，fan_out 取包内文件依赖面并集。21 个顶层包（2026-10-15 实测标定）。
# 正常增长通道：新建顶层包（R-25 登记，不在本表内不冻结）或拆分白名单文件（包级总量不变）；
# 需突破既有包基线时走 PR2 对价机制，本规则无豁免。
PACKAGE_STRUCTURE_BASELINE: dict[str, dict[str, int]] = {
    "acceptance": {"lines": 5618, "symbols": 254, "fan_out": 37},  # 5612→5614（#422 live_safety_gate 验收桩 +2 行功能必需）；5614→5618（#422 ruff --fix 展开 import 块 +4 行）
    "allocation": {"lines": 2083, "symbols": 56, "fan_out": 9},
    "api": {"lines": 3807, "symbols": 194, "fan_out": 26},  # T0-16 新增 /api/reconciliation/unfreeze 解冻接口 +32行/+1符号功能必需
    "backtest": {"lines": 20722, "symbols": 560, "fan_out": 37},  # T4-3 信号流 20711→20722
    "core": {"lines": 12976, "symbols": 600, "fan_out": 40},  # 12975→12976（+1，#425 引擎停止感知 _stop_requested 功能必需）
    "data_access": {"lines": 17223, "symbols": 605, "fan_out": 34},
    "database": {"lines": 3882, "symbols": 120, "fan_out": 17},  # 3608/113→3763/116（+155行/+3符号，T1-3 0009 对账账本迁移 upgrade/downgrade/辅助函数，fan_out 不变）；3763/116→3831/120（+68行/+4符号，T1-4 record_order ON CONFLICT + 新增 record_order_event/record_reconciliation_run/diff/freeze 四个方法）；3831/120→3882/120（+51行，T1-7 四类写入同步 AuditLogStore 哈希链，symbols/fan_out 不变）
    "engine": {"lines": 209, "symbols": 14, "fan_out": 4},
    "execution": {"lines": 1097, "symbols": 59, "fan_out": 6},  # 1085→1097（+12，#425 引擎停止感知 _stop_requested 状态机贯通功能必需）
    "gui": {"lines": 6603, "symbols": 242, "fan_out": 28},
    "indicators": {"lines": 5884, "symbols": 172, "fan_out": 17},
    "leakage_gate": {"lines": 4864, "symbols": 163, "fan_out": 19},  # 4466→4475（+9，C-4 行规对齐 NEW_FILE_FLOOR T0/T1 60→80 + 拆包产物 realtime_sources/realtime_bridge/engine_assembly 补注册 C-5/FILE_FLOOR——门禁治理代码自身增长，功能必需对价，2026-10-16）；4475/149→4555/150（+80 行/+1 符号=explain_violations，顶红指引引擎 _RED_GUIDES+explain_violations 功能必需对价，与 C-5 coverage_gate 242→255 联动同步，2026-10-16）；4555/150/17→4832/162/18（+277 行/+12 符号/+1 fan_out=gate_audit 门禁参数变更审计模块，判据A 对价+判据B 蓝图登记，功能必需对价 #C-4·5，2026-10-16）；4832→4840（+8，coverage_gate C-5 注册注释对价留痕）；4840/162→4855/163（+15 行/+1 符号=gate_audit 判据补全：注册条目/基线字段命中 + 独立commit去重，功能必需对价 #C-4·5）；4855/163/18→4864/163/19（+9 行/+1 fan_out=gate_audit 平台解码修复：git 输出显式 UTF-8 编码 + --no-pager + 干净环境，修 CI windows 腿 UnicodeDecodeError，功能必需对价 #C-4·5，2026-10-16）
    "live": {"lines": 18171, "symbols": 662, "fan_out": 36},  # T4-4 fail-closed 17492→17498；#425 引擎停止感知 17498→17513（lines +15 功能必需，symbols/fan_out 不变）；17513/652→17690/653（T0-12 查单确认闭环 qmt_broker/manager/engine 功能必需，+1 符号=_handle_unknown_submit 方法）；17690/653→17732/654（T0-12·2 order_timeout 活配置化 _build_ack_unknown_delays +42行/+1符号功能必需）；17732/654→17798/655（T0-8 _assert_live_reconciliation_gate +66行/+1符号功能必需）；17798/655→17856/655（T0-10 restore_state转发子券商+撤单返回值检查+connect_all后撤单 +58行，symbols/fan_out 不变）；17856/655→17882/655（T0-11 pause force参数+_startup_blocked +26行，symbols/fan_out 不变）；17882/655→17947/659（T0-1 双值并存 sync_from_broker 不覆写+adopt_broker_ledger+3property +65行/+4符号功能必需）；17947/659→17949/659（T0-15 cash_diff round 2位 + 持仓数量整数比较 +2行，symbols/fan_out 不变）；17949/659→18002/660（T0-2 BrokerQueryError异常类+get_positions/get_account改抛错 + T0-3健全性闸 + T0-6 frozen_cash缺键保留 + T0-7启动断言live fail-closed +53行/+1符号功能必需）；18002/660→18032/660（T0-4对账周期调度断言 + T0-19冻结拒绝新提交 +30行，symbols/fan_out 不变）；18032/660→18055/660/36（T1-1 TradeLedger装配+注册到registry+gate加ledger检查 +23行/+1fan_out，symbols 不变）；18055/660/36→18092/660/36（T1-2 intent-first账本写入 +37行，symbols/fan_out 不变）；18092/660/36→18151/662/36（T1-5/T1-6 持仓日快照+PnL快照周期任务 +59行/+2符号，fan_out 不变）；18151/662/36→18171/662/36（T1-8 落库连续失败计数+pause +20行，symbols/fan_out 不变）
    "ml": {"lines": 5672, "symbols": 208, "fan_out": 27},
    "models": {"lines": 1784, "symbols": 139, "fan_out": 6},  # T4-3 signals字段 1757→1760；1760→1784（+24，#425 引擎预热/回放停止感知配置模型扩展功能必需）
    "monitoring": {"lines": 24015, "symbols": 881, "fan_out": 31},  # 23932→23954（+22，T0-13 account_reconciler._submit_realtime try/except 防 in_flight 泄漏）；23954→23973（+19，T0-14 核对异常置 consistent=None + to_dict 增 success_ts/stale_seconds）；23973→24008/881（+35行/+1符号=check_freeze_timeout 方法，T0-16 冻结超时升级告警功能必需）；24008→24015（+7行，T0-5 持续冻结留痕 CRITICAL，symbols/fan_out 不变）
    "realism_gate": {"lines": 1864, "symbols": 50, "fan_out": 10},
    "risk": {"lines": 4288, "symbols": 195, "fan_out": 12},
    "strategies": {"lines": 6901, "symbols": 190, "fan_out": 21},
    "utils": {"lines": 9637, "symbols": 368, "fan_out": 47},
    "web": {"lines": 1237, "symbols": 44, "fan_out": 12},
}

# R-19 零覆盖基线：✅ 已冻结（回写#256）。冻结集合 = 2026-10-08 实测引用级零覆盖业务模块 49 个
# （被 tests import 即视为覆盖；含 gui/strategies/allocation 等业务模块，剔除 __init__.py）。
# 冻结后"新增零覆盖即红"；存量零覆盖只减不增（收敛由 G1/G2 或 G0-9 补测清单推进）。
# 注意：本判定为"引用级"近似，非执行级覆盖；真实覆盖由 pytest-cov 判定
# （cov 实测 58% / 零覆盖业务 12 个仅作 G0-9 补测清单，不混用）。
ZERO_COVERAGE_BASELINE: set[str] = {
    "myquant/allocation/base.py", "myquant/allocation/equal_weight.py", "myquant/allocation/target_volatility.py",
    "myquant/backtest/async_monte_carlo.py", "myquant/backtest/monte_carlo.py",
    "myquant/backtest/pdf_report.py", "myquant/backtest/report.py", "myquant/core/async_runtime.py",
    "myquant/core/config_loader.py",
    "myquant/data_access/cleaner/error_codes.py", "myquant/data_access/factory.py", "myquant/data_access/fetcher.py",
    "myquant/data_access/sources/composite_source.py", "myquant/database/migrations/env.py",
    "myquant/database/migrations/versions/0001_initial_schema.py",
    "myquant/database/migrations/versions/0002_add_market_data_composite_index.py",
    "myquant/database/migrations/versions/0003_align_schema_divergence.py",
    "myquant/database/migrations/versions/0004_add_trade_ledger.py",
    "myquant/database/migrations/versions/0005_add_alerts.py",
    "myquant/database/migrations/versions/0006_add_metrics_history.py",
    "myquant/database/migrations/versions/0007_add_audit_log.py",
    "myquant/database/migrations/versions/0008_add_value_encrypted.py",
    "myquant/database/migrations/versions/0009_reconciliation_ledger.py",
    "myquant/execution/base.py", "myquant/execution/state_machine.py",
    "myquant/gui/main_window.py",
    "myquant/strategies/kunpeng_strategy.py", "myquant/strategies/mean_reversion_strategy.py",
    "myquant/strategies/momentum_strategy.py", "myquant/utils/cache_sync.py", "myquant/utils/csv_utils.py",
    "myquant/utils/math_utils.py", "myquant/utils/memory_pool.py", "myquant/utils/parallel.py",
    "myquant/utils/performance_config.py", "myquant/utils/timer.py",
    # 49 → 42（G0-9 补测 #266 Group A 覆盖 7 模块移除：candidate_pool/config_builder/redis_bus/
    # broker_source/loader/candidate_report/ptrade_broker）→ 39（Group B 覆盖 3 模块移除：
    # kline_item/plot_helper/side_panel）→ 35（Group C 覆盖 4 对话框移除：
    # backtest_config_dialog/batch_backtest_tab/cleanup_dialog/data_clean_dialog）
}


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


@functools.lru_cache(maxsize=None)
def _structure_metrics(rel: str) -> tuple[int, int, frozenset]:
    """R-26 三维度量（与标定脚本 calibrate_r26.py 同口径）。

    返回 (lines, total_symbols, fan_out_set)。lines 用 splitlines 行数（与 R-24 一致）；
    total_symbols 遍历 FunctionDef/AsyncFunctionDef/ClassDef，排除 __main__ 自测块与
    TYPE_CHECKING 块；fan_out 复用 _imports() 排除 main/typing 语境，模块归一为
    myquant.X.*→X、外部库→首段，取去重集合。
    """
    path = str(MYQUANT / rel)
    src = _read_text(path)
    lines = len(src.splitlines())
    tree = _parse(path)
    if tree is None:
        return lines, 0, frozenset()
    main_r = sorted(
        (n.lineno, n.end_lineno or n.lineno)
        for n in ast.walk(tree) if _is_main_guard(n)
    )
    typing_r = sorted(
        (n.lineno, n.end_lineno or n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
        and n.test.id == "TYPE_CHECKING"
    )

    def _in(rng: list[tuple[int, int]], lineno: int) -> bool:
        return any(s <= lineno <= e for s, e in rng)

    symbols = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not _in(main_r, n.lineno) and not _in(typing_r, n.lineno)
    )
    fan: set[str] = set()
    for mod, _ln, ctx in _imports(path):
        if ctx in ("main", "typing"):
            continue
        if mod.startswith("myquant."):
            parts = mod.split(".")
            fan.add(parts[1] if len(parts) > 1 else mod)
        else:
            fan.add(mod.split(".")[0])
    return lines, symbols, frozenset(fan)


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
    """R-01b：全量导入图真环数只减不增（基线 0，回写#261 环1 消除——全系统真环清零、#260 环2、#259 环3）。"""

    def test_lazy_cycle_not_rising(self):
        cycles = _real_cycles(top_only=False)
        assert len(cycles) <= LAZY_CYCLE_BASELINE, (
            f"全量导入图真环数上升: {len(cycles)} > {LAZY_CYCLE_BASELINE}\n"
            + "\n".join(str(c) for c in cycles)
        )


class TestR02CrossLayer:
    """R-02：跨层反向 import；例外须登记且未过期且非假豁免。基线 34（回写#257 G1 EX-07A 消除后 35→34）。"""

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
            pytest.skip("存量冻结（R-05 白名单，P1 清理）")  # P2-2 定性：设计性跳过，存量 IO 白名单冻结，清理在 P1 计划内
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
    """R-16 装配器集中（G3 强化，回写#267）：L0-L4 层禁止直接实例化 L5 组件类。

    跨层组件（monitoring/acceptance 导出的类）装配责任必须集中——经 core.registry
    工厂（get_component / get_* 工厂函数）或装配器（run_实盘入口.py setup_monitoring_linkage）
    取用；禁止在低层模块内直接 `ClassName(...)` 裸实例化 L5 组件。
    基线 0（阻断）；例外登记同 R-02 例外表。
    """

    def test_no_lower_layer_direct_assembly(self):
        bad = []
        exc_or_p0 = set(EXCEPTIONS) | P0_MUST_FIX
        lower_layers = {"L0", "L1", "L2", "L2E", "L3", "L4"}
        for path in _all_py_files():
            src_layer = _layer_of_file(path)
            if src_layer not in lower_layers:
                continue
            tree = _parse(str(path))
            if tree is None:
                continue
            rel = _rel(path)
            l5_classes = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module.startswith(COMPONENT_HOME_PKGS[0])
                    or node.module.startswith(COMPONENT_HOME_PKGS[1])
                ):
                    l5_classes.update(a.name for a in node.names if a.name != "*")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith(COMPONENT_HOME_PKGS[0]) \
                           or a.name.startswith(COMPONENT_HOME_PKGS[1]):
                            l5_classes.add(a.name.split(".")[-1])
            if not l5_classes:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                   and node.func.id in l5_classes:
                    if (rel, _src_module(str(path)).split(".")[-1]) in exc_or_p0:
                        continue
                    bad.append(f"{rel}:{node.lineno} {node.func.id}(")
        assert not bad, (
            "L0-L4 层直接实例化 L5 组件类（装配责任未集中，须经工厂/装配器）：\n"
            + "\n".join(bad)
        )


class TestR17EventContract:
    """R-17 事件契约（G3 新增，回写#267）：发布/订阅必须引用 EventType 契约。

    事件类型必须用 myquant.core.event_bus.EventType 成员（或其 value 字符串），
    禁止 ad-hoc 字符串事件类型；订阅允许 '*' 通配符。非 main/typing 语境。
    存量 5 个自测/演示类型（event_bus __main__ 批处理自测 + meta_monitor 自测）
    冻结允许（EVENT_ADHOC_ALLOW），新增 ad-hoc 类型即红。基线 0（阻断）。
    """

    _EVENT_TYPE_VALUES: frozenset[str] = frozenset({
        "market.tick", "market.bar", "market.order_book",
        "strategy.signal", "strategy.signal_generated",
        "order.submit", "order.filled", "order.partial_filled",
        "order.cancelled", "order.rejected", "order.failed",
        "fill.report_error", "emergency.liquidation",
        "position.update", "position.open", "position.close",
        "risk.alert", "risk.violation", "risk.limit_hit",
        "risk.paused", "risk.resumed",
        "system.start", "system.stop", "system.error", "data.loaded",
        "backtest.complete",
        "data_quality.dirty", "data_quality.pipeline_complete",
        "data_quality.alert", "data_quality.critical",
    })

    # 存量自测/演示 ad-hoc 事件类型（__main__ 自测块内，非业务）：冻结允许，待清理只能缩
    _EVENT_ADHOC_ALLOW: frozenset[str] = frozenset({
        "TEST_EVT", "B_EVT", "POOL_EVT", "CMP", "META_FILL",
    })

    def test_no_ad_hoc_event_type(self):
        bad = []
        for path in _all_py_files():
            tree = _parse(str(path))
            if tree is None:
                continue
            rel = _rel(path)
            main_ranges = sorted(
                (n.lineno, n.end_lineno or n.lineno)
                for n in ast.walk(tree) if _is_main_guard(n)
            )
            typing_ranges = sorted(
                (n.lineno, n.end_lineno or n.lineno)
                for n in ast.walk(tree)
                if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                and n.test.id == "TYPE_CHECKING"
            )
            def in_demo(ln):
                return any(s <= ln <= e for s, e in main_ranges) \
                    or any(s <= ln <= e for s, e in typing_ranges)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                literal = None
                if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                    # emit("x") / subscribe("x") / unsubscribe_all("x")
                    if fn.attr in ("emit", "subscribe", "unsubscribe_all") and node.args:
                        a = node.args[0]
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            literal = a.value
                    elif fn.attr == "publish_chain" and node.args:
                        inner = node.args[0]
                        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                           and inner.func.id == "Event" and inner.args:
                            a = inner.args[0]
                            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                                literal = a.value
                elif isinstance(fn, ast.Name) and fn.id == "Event" and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        literal = a.value
                if literal is not None and not in_demo(node.lineno) \
                   and literal != "*" and literal not in self._EVENT_TYPE_VALUES \
                   and literal not in self._EVENT_ADHOC_ALLOW:
                    bad.append(f"{rel}:{node.lineno} {literal!r}")
        assert not bad, "ad-hoc 字符串事件类型（须用 EventType 契约）：\n" + "\n".join(bad)


class TestR17EventContractCompleteness:
    """R-17 契约完备性（只报告、不阻断）：未被消费的 EventType 成员数。

    驱动指标：每个 EventType 成员应被某个模块发布或订阅；无人消费 = 死契约，
    提示后续接线或清理。不作为 CI 阻断。
    """

    def test_report_unused_event_types(self):
        eb = MYQUANT / "core" / "event_bus.py"
        tree = _parse(str(eb))
        unused = []
        if tree is not None:
            members: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "EventType":
                    for st in node.body:
                        if isinstance(st, ast.Assign):
                            for t in st.targets:
                                if isinstance(t, ast.Name) and isinstance(st.value, ast.Constant):
                                    members.add(t.id)
            used: set[str] = set()
            for path in _all_py_files():
                t2 = _parse(str(path))
                if t2 is None:
                    continue
                for node in ast.walk(t2):
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                       and node.value.id == "EventType" and isinstance(node.attr, str):
                        used.add(node.attr)
            unused = sorted(members - used)
        print(f"\nR-17 未被消费的 EventType 成员（{len(unused)}）: {unused}")


class TestR18ScriptThinEntry:
    """R-18 脚本自举（G3 新增，回写#267）：scripts/ 薄入口纪律。

    scripts/ 禁止业务逻辑（代码变更治理规则 §1.3）；业务类定义必须归 myquant。
    存量 8 个类定义冻结允许（SCRIPT_CLASS_ALLOW，4 文件演示/验收策略，待清理）；
    新增脚本类定义即红（只减不增）。
    """

    def test_no_new_script_class(self):
        bad = []
        for p in sorted((PROJECT_ROOT / "scripts").rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            tree = _parse(str(p))
            if tree is None:
                continue
            rel = p.relative_to(PROJECT_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if (rel, node.name) not in SCRIPT_CLASS_ALLOW:
                        bad.append(f"{rel}:{node.lineno} class {node.name}")
        assert not bad, (
            "scripts/ 新增业务类定义（薄入口纪律，须归 myquant）：\n" + "\n".join(bad)
            + "\n（存量 8 个已冻结于 SCRIPT_CLASS_ALLOW，待清理只能缩）"
        )


class TestR08ConfigReverseLookup:
    """R-08 配置反查（G3 新增，回写#267）：登记的关键配置键必须被消费。

    配置真源反查：settings.yaml monitoring 段的关键键（CONFIG_KEY_REQUIRED）每段
    必须被 myquant 读取（get_monitoring_config().get("...")）。死配置键
    （登记但代码不再消费）即红；只增不删。
    """

    def test_no_dead_config_key(self):
        all_src = "\n".join(_read_text(str(p)) for p in _all_py_files())
        dead = []
        for key in sorted(CONFIG_KEY_REQUIRED):
            segments = key.split(".")[1:]  # 去掉 monitoring 前缀
            missing = [s for s in segments
                       if not re.search(r'\.get\("' + re.escape(s) + r'"', all_src)]
            if missing:
                dead.append(f"{key}（未消费段: {missing}）")
        assert not dead, "死配置键（登记但代码未读取，须接线或移除登记）：\n" + "\n".join(dead)


class TestR19ZeroReference:
    """R-19：零覆盖模块只减不增（✅ 已冻结回写#256；引用级判定，非执行覆盖）。

    关键事实（全盘复查）：文档的「15 个零覆盖模块清单」是抽查，不是全量——
    实测字符串级零引用 74 个、精确 import 级 68 个（含 __init__ 后）。真实覆盖率
    必须由 pytest-cov 插桩（G0-9）判定；本判定为"被 tests import 即视为覆盖"的
    引用级近似（S-3 诚实命名：TestR19ZeroReference，真覆盖由 cov 判定）。
    冻结（回写#256）：2026-10-08 实测当前全部引用级零覆盖业务模块 49 个写入
    ZERO_COVERAGE_BASELINE，此后"新增零覆盖即红"；存量零覆盖只减不增。
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
        # ZERO_COVERAGE_BASELINE 已冻结（49 个零覆盖模块，R-19 转 CI 阻断），此规则不再报告模式
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


class TestR23EntryFreeze:
    """R-23：入口白名单冻结（G0 收尾，回写#256）。

    scripts/（41 个，含 故障演练/灾备演练 子目录）与根目录入口（12 个 .py + 一键部署.bat）
    全量登记于 ENTRY_ALLOWLIST；新增入口未登记即红。新增入口必须：先更新蓝图目录树
    （docs/project_layout_gear2.md）→ 再更新本白名单 → 再创建文件（蓝图优先纪律）。
    扫描范围：scripts/ 递归 *.py + 项目根 *.py / *.bat（__pycache__ 除外）。
    """

    def test_no_new_entry(self):
        scripts: list[Path] = [p for p in (PROJECT_ROOT / "scripts").rglob("*.py")
                               if "__pycache__" not in p.parts]
        roots: list[Path] = [p for p in PROJECT_ROOT.iterdir()
                             if p.is_file() and p.suffix in (".py", ".bat")]
        found = [p.relative_to(PROJECT_ROOT).as_posix() for p in scripts + roots]
        unlisted = sorted(set(found) - ENTRY_ALLOWLIST)
        assert not unlisted, (
            "新增入口未登记白名单（须先更新蓝图目录树 + ENTRY_ALLOWLIST）：\n"
            + "\n".join(unlisted)
        )


class TestR24OversizeFreeze:
    """R-24：薄化冻结——OVERSIZE 白名单内文件行数只减不增（G0 收尾，回写#256）。

    基线 = 2026-10-08 实测（OVERSIZE_LINE_BASELINE，14 项）；白名单内文件行数
    超过基线即红（存量继续膨胀禁止）；新增 >1500 行文件仍由 R-14 拦截（白名单外）。
    文件被删除/迁移时不误报（由蓝图一致性检查另行处理）。
    """

    def test_oversize_not_rising(self):
        over = []
        for rel, baseline in sorted(OVERSIZE_LINE_BASELINE.items()):
            p = MYQUANT / rel
            if not p.exists():
                continue
            lines = len(_read_text(str(p)).splitlines())
            if lines > baseline:
                over.append(f"{rel}: {lines} > 基线 {baseline}")
        assert not over, "白名单内超大文件行数上涨（薄化冻结，只减不增）：\n" + "\n".join(over)


class TestR25RegisteredPackage:
    """R-25：myquant 每个顶层包必须登记 MODULE_LAYER（P0-00 子步骤 D，回写#300）。

    治理盲区（施工任务清单 P0-00 卡："`_layer_of_module` None → 判红"）：
    `_layer_of_module`（本文件 §3）对未登记的顶层包返回 None，而下游 R-02 跨层引用、
    R-11 原始数据、R-16 装配器集中、R-22 同层互引对 `None` 一律 `continue` 跳过——
    新增一个未登记的 `myquant/<pkg>/` 包，其全部文件既不当分层源、也不当分层目标，
    会整体游离于分层门禁之外（彻底失明）。

    本规则作为元门禁从源头封堵，不改 `_layer_of_module` 既有语义（避免影响全量门禁）：
      test_every_package_registered：扫描 myquant/ 下真实包（含 __init__.py 的一级
        目录），每个包必须在 MODULE_LAYER 登记；未登记即红。
      test_no_ghost_registration：反向校验——MODULE_LAYER 登记了但文件系统已不存在的
        "幽灵登记"同样判红（保证登记表不藏失真项，P0-01 登记 realism_gate 前置可信）。
    基线 = 2026-10-11 实测 20 个顶层包全部登记（_layer_of_module 不返回 None）。
    """

    @staticmethod
    def _registered_top_pkgs() -> set[str]:
        """从 MODULE_LAYER 提取已登记的 myquant 顶层包名（去掉 myquant. 前缀）。"""
        return {k.split(".", 1)[1] for k in MODULE_LAYER if k.startswith("myquant.")}

    def test_every_package_registered(self):
        actual = {
            p.name
            for p in MYQUANT.iterdir()
            if p.is_dir() and (p / "__init__.py").is_file()
        }
        unregistered = sorted(actual - self._registered_top_pkgs())
        assert not unregistered, (
            "myquant 顶层包未登记 MODULE_LAYER（分层门禁将对其整体失明，须先登记层位）：\n"
            + "\n".join(f"  myquant.{p}/" for p in unregistered)
        )

    def test_no_ghost_registration(self):
        ghosts = sorted(
            pkg for pkg in self._registered_top_pkgs()
            if not (MYQUANT / pkg).is_dir()
        )
        assert not ghosts, (
            "MODULE_LAYER 登记了但文件系统不存在的幽灵包（登记表失真，须删除该登记）：\n"
            + "\n".join(f"  myquant.{pkg}/" for pkg in ghosts)
        )


class TestR26StructureFreeze:
    """R-26：结构冻结——白名单文件三维度量 + 顶层包聚合只减不增（PR1，回写#420）。

    度量口径（与现有语义矩阵一致，K-f 语境分类；实现见 _structure_metrics）：
      lines         : splitlines 行数（与 R-24 同口径）
      total_symbols : AST 遍历 FunctionDef/AsyncFunctionDef/ClassDef，
                      排除 __main__ 自测块与 TYPE_CHECKING 块
      fan_out       : import 依赖面（排除 main/typing 语境），
                      myquant.X.*→X、外部库→首段，去重集合大小
    语义：白名单文件任一维度超基线即红（存量继续膨胀禁止，比纯行数难作弊——
          压缩行不影响符号数、搬代码不降低依赖面）；任一维度超硬顶
          （STRUCTURE_HARD_CAP）即红且无豁免——硬顶是防 PR2 对价机制被滥用为
          无限抬升的死线；顶层包聚合任一维度超基线即红（防"横向分裂"：把白名单
          文件拆成多个各自低于基线的新文件而包级总量不降，配合 R-22/R-25 闭环）。
    装配器类文件（R-16 装配器集中）fan_out 天然偏高，冻结值已含当前状态；
    豁免通道留给 PR2 对价机制，本规则无豁免。
    基线 = 2026-10-15 实测标定（先测量再冻结；标定脚本 calibrate_r26.py）。
    """

    def test_whitelisted_not_rising(self):
        over = []
        for rel, base in sorted(STRUCTURE_BASELINE.items()):
            p = MYQUANT / rel
            if not p.exists():
                continue
            lines, symbols, fan = _structure_metrics(rel)
            for name, val, b in (("lines", lines, base["lines"]),
                                 ("symbols", symbols, base["symbols"]),
                                 ("fan_out", len(fan), base["fan_out"])):
                if val > b:
                    over.append(f"{rel}: {name} {val} > 基线 {b}")
        assert not over, "白名单文件结构指标上涨（结构冻结，只减不增）：\n" + "\n".join(over)

    def test_hard_cap_no_exemption(self):
        over = []
        for rel in sorted(STRUCTURE_BASELINE):
            p = MYQUANT / rel
            if not p.exists():
                continue
            lines, symbols, fan = _structure_metrics(rel)
            for name, val, cap in (("lines", lines, STRUCTURE_HARD_CAP["lines"]),
                                   ("symbols", symbols, STRUCTURE_HARD_CAP["symbols"]),
                                   ("fan_out", len(fan), STRUCTURE_HARD_CAP["fan_out"])):
                if val > cap:
                    over.append(f"{rel}: {name} {val} > 硬顶 {cap}（无豁免通道）")
        assert not over, "白名单文件结构指标超硬顶（硬顶不可逾越）：\n" + "\n".join(over)

    def test_packages_not_rising(self):
        over = []
        for pkg, base in sorted(PACKAGE_STRUCTURE_BASELINE.items()):
            pdir = MYQUANT / pkg
            if not pdir.is_dir():
                continue
            files = [p for p in pdir.rglob("*.py")
                     if "__pycache__" not in p.parts and p.name != "__init__.py"]
            lines = symbols = 0
            fan_union: set[str] = set()
            for p in files:
                rel = p.relative_to(MYQUANT).as_posix()
                n_lines, n_symbols, n_fan = _structure_metrics(rel)
                lines += n_lines
                symbols += n_symbols
                fan_union |= set(n_fan)
            for name, val, b in (("lines", lines, base["lines"]),
                                 ("symbols", symbols, base["symbols"]),
                                 ("fan_out", len(fan_union), base["fan_out"])):
                if val > b:
                    over.append(f"{pkg}: {name} {val} > 基线 {b}")
        assert not over, "顶层包结构指标上涨（防横向分裂，只减不增）：\n" + "\n".join(over)


class TestP104ExemptRegistry:
    """P1-04 旁路收敛：REALISM_EXEMPT_ENTRY 例外入口白名单固化（方案 §4.4，回写#351）。

    背景：门禁例外清单此前仅存在于方案/注释（全仓零命中常量），P1-04 卡要求固化为
    白名单并接受架构门禁登记。登记语义：这些入口直构引擎但**非回测真实性判定对象**
    （性能基准/对拍工具/实盘引擎），豁免门禁；非例外回测入口必须经门禁
    （create_engine / from_data_provider，#349 起直构路径已收敛）。
    白名单为 frozenset（不可变），防误改；查询走 is_realism_exempt_entry。
    """

    _EXPECTED = {
        "engine_benchmark",
        "consistency_check",
        "live_safety_gate",
        "scripts/实时行情驱动.py",
        "scripts/双券商模拟实盘.py",
        "scripts/实盘模拟回测.py",
    }

    def test_whitelist_registered(self):
        from myquant.realism_gate.gate import REALISM_EXEMPT_ENTRY

        assert REALISM_EXEMPT_ENTRY == self._EXPECTED, (
            "REALISM_EXEMPT_ENTRY 与方案 §4.4 例外清单不一致（新增例外须先评审并同步登记）：\n"
            f"  实际: {sorted(REALISM_EXEMPT_ENTRY)}"
        )

    def test_whitelist_immutable(self):
        from myquant.realism_gate.gate import REALISM_EXEMPT_ENTRY

        assert isinstance(REALISM_EXEMPT_ENTRY, frozenset), "白名单必须不可变（防误改）"

    def test_query_works(self):
        from myquant.realism_gate.gate import is_realism_exempt_entry

        assert is_realism_exempt_entry("engine_benchmark") is True
        assert is_realism_exempt_entry("scripts/双券商模拟实盘.py") is True
        assert is_realism_exempt_entry("scripts/生成回测报告.py") is False  # 非例外（#349 已收敛）
        assert is_realism_exempt_entry("param_optimizer") is False  # 非例外（#327 已收敛）
