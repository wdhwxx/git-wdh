# 文件名：myquant/backtest/report.py
"""
backtest/report.py - 报告生成器
==================================
整合绩效指标、净值曲线、交易明细、蒙特卡洛分析，生成HTML/文本报告。

设计原则（规则2：为实盘无缝对接准备）:
  - ReportGenerator 回测/实盘共用，输入 BacktestResult 即可生成报告
  - 实盘运行时可定期生成日报/周报
  - 支持HTML（含图表）和文本两种格式
  - 报告模板可扩展，支持自定义样式

第4项增强（回测报告增强·B项）:
  - HTML 报告新增蒙特卡洛分析区块：
      * 蒙特卡洛统计卡（中位收益/P5/P95/亏损概率/中位回撤/回撤95%CI/破产概率）
      * 最终收益分布直方图（Chart.js bar）
      * 最大回撤分布直方图（Chart.js bar）
      * 收益路径分位带图（Chart.js line，P5/P25/P50/P75/P95）
  - generate_html 新增 include_monte_carlo/mc_n_simulations/mc_horizon_days/mc_seed 可选参数
  - 蒙特卡洛固定 seed（默认42）→ 报告完全可复现

防未来函数（规则4）:
  - 报告只展示已实现的回测结果，不涉及未来数据
  - 所有图表基于历史净值和成交记录绘制
  - 蒙特卡洛只重采样已实现的历史收益（pct_change），报告标注统计模拟性质
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from myquant.backtest.monte_carlo import MonteCarloAnalyzer, MonteCarloResult
from myquant.backtest.pdf_report import PdfReportGenerator, downsample_equity_curve
from myquant.core.logger import get_logger
from myquant.models.backtest import BacktestResult
from myquant.realism_gate.report import write_manifest_to_results
from myquant.utils.csv_utils import CSV_HEADER_MAPS, export_csv_cn

logger = get_logger(__name__)


class ReportGenerator:
    """
    报告生成器

    核心功能:
      - generate_html(result, output_path): 生成HTML报告（含图表）
      - generate_text(result): 生成文本摘要
      - generate_trades_csv(result, output_path): 导出交易明细CSV
      - generate_equity_csv(result, output_path): 导出净值曲线CSV

    使用方式:
        generator = ReportGenerator()
        html_path = generator.generate_html(result, "outputs/report.html")
        text = generator.generate_text(result)
        print(text)
    """

    def __init__(self, title: str = "鲲鹏量化交易系统 - 回测报告"):
        """
        初始化报告生成器

        参数:
            title: 报告标题
        """
        self.title = title
        logger.info(f"报告生成器初始化: title={title}")

    # ------------------------------------------------------------------
    # 文本报告
    # ------------------------------------------------------------------

    def generate_text(self, result: BacktestResult) -> str:
        """
        生成文本摘要报告

        参数:
            result: 回测结果

        返回:
            str: 文本报告
        """
        lines = []
        lines.append("=" * 60)
        lines.append(f"  {self.title}")
        lines.append("=" * 60)
        lines.append("")

        # 基本信息
        lines.append("【基本信息】")
        lines.append(f"  策略名称:   {result.config.strategy_name}")
        lines.append(f"  回测期间:   {result.config.start_date} ~ {result.config.end_date}")
        lines.append(f"  标的列表:   {', '.join(result.config.symbols)}")
        lines.append(f"  初始资金:   {result.config.initial_capital:,.0f}")
        lines.append(f"  运行ID:     {result.run_id}")
        lines.append(f"  运行耗时:   {result.duration_seconds:.2f}秒")
        lines.append("")

        # 绩效指标
        lines.append("【绩效指标】")
        m = result.metrics
        lines.append(f"  总收益率:     {m.total_return:>10.2%}")
        lines.append(f"  年化收益率:   {m.annualized_return:>10.2%}")
        lines.append(f"  年化波动率:   {m.annualized_volatility:>10.2%}")
        lines.append(f"  夏普比率:     {m.sharpe_ratio:>10.4f}")
        lines.append(f"  索提诺比率:   {m.sortino_ratio:>10.4f}")
        lines.append(f"  Calmar比率:   {m.calmar_ratio:>10.4f}")
        lines.append(f"  最大回撤:     {m.max_drawdown:>10.2%}")
        lines.append(f"  最大回撤天数: {m.max_drawdown_duration:>10d}")
        lines.append("")

        # 交易统计
        lines.append("【交易统计】")
        lines.append(f"  总交易次数:   {m.total_trades:>10d}")
        lines.append(f"  盈利次数:     {m.winning_trades:>10d}")
        lines.append(f"  亏损次数:     {m.losing_trades:>10d}")
        lines.append(f"  胜率:         {m.win_rate:>10.2%}")
        _pl_ratio = "∞（全盈利）" if m.profit_loss_ratio == float('inf') else f"{m.profit_loss_ratio:.4f}"
        lines.append(f"  盈亏比:       {_pl_ratio:>12}")
        lines.append(f"  平均盈利:     {m.avg_win:>10.2f}")
        lines.append(f"  平均亏损:     {m.avg_loss:>10.2f}")
        lines.append(f"  最大连胜:     {m.max_consecutive_wins:>10d}")
        lines.append(f"  最大连亏:     {m.max_consecutive_losses:>10d}")
        lines.append("")

        # 成本
        lines.append("【交易成本】")
        lines.append(f"  总手续费:     {m.total_commission:>10.2f}")
        lines.append(f"  总滑点:       {m.total_slippage:>10.2f}")
        lines.append(f"  换手率:       {m.turnover:>10.2f}")
        lines.append("")

        # 最终净值
        lines.append("【最终结果】")
        lines.append(f"  最终净值:     {result.final_equity:>10.4f}")
        final_value = result.config.initial_capital * result.final_equity
        lines.append(f"  最终资产:     {final_value:>12,.2f}")
        lines.append("")

        # 回测真实性门禁清单（方案 §4.3"清单强制写入报告页"；回写#355）
        _rg = getattr(result, "realism_manifest", None)
        if _rg:
            _rg_status = _rg.get("status", "unknown")
            _rg_violations = _rg.get("violations", []) or []
            lines.append("【回测真实性门禁清单】")
            lines.append(f"  清单状态:   {_rg_status}")
            lines.append(f"  违规数:     {len(_rg_violations)}")
            if _rg_violations:
                for _v in _rg_violations[:10]:
                    _crit = bool(_v.get("critical", False))
                    lines.append(f"    - [{'critical' if _crit else 'warn'}] "
                                 f"{_v.get('field_path')}: {_v.get('message', '')}")
            lines.append("")

        lines.append("=" * 60)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # HTML报告
    # ------------------------------------------------------------------

    def generate_html(
        self,
        result: BacktestResult,
        output_path: str = "outputs/backtest_report.html",
        include_monte_carlo: bool = True,
        mc_n_simulations: int = 2000,
        mc_horizon_days: int = 252,
        mc_seed: int = 42,
    ) -> str:
        """
        生成HTML报告（含图表，可选蒙特卡洛分析区块）

        参数:
            result: 回测结果
            output_path: 输出路径
            include_monte_carlo: 是否包含蒙特卡洛分析区块（默认True）
            mc_n_simulations: 蒙特卡洛模拟次数（默认2000）
            mc_horizon_days: 蒙特卡洛模拟期数/交易日（默认252≈1年）
            mc_seed: 蒙特卡洛随机种子（默认42，固定保证报告可复现）

        返回:
            str: 报告文件路径
        """
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        mc = self._run_monte_carlo(
            result,
            include_monte_carlo,
            mc_n_simulations,
            mc_horizon_days,
            mc_seed,
        )
        html = self._build_html(result, mc)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)

        logger.info(f"HTML报告已生成: {output_path}")
        return output_path

    # ------------------------------------------------------------------
    # 蒙特卡洛（第4项B：HTML报告增强）
    # ------------------------------------------------------------------

    def _run_monte_carlo(
        self,
        result: BacktestResult,
        include: bool,
        n_simulations: int,
        horizon_days: int,
        seed: int,
    ) -> Optional[MonteCarloResult]:
        """执行蒙特卡洛分析（净值曲线过短时跳过，返回None）

        蒙特卡洛只消费已实现的历史净值（pct_change 有放回重采样），
        报告展示的是收益/回撤的统计分布，非未来预测（防未来铁律）。
        固定 seed 保证同一回测结果的报告可复现。
        """
        if not include:
            return None
        if not result.equity_curve or len(result.equity_curve) < 30:
            logger.info("净值曲线过短(<30点)，跳过蒙特卡洛区块")
            return None
        analyzer = MonteCarloAnalyzer(
            n_simulations=n_simulations,
            horizon_days=horizon_days,
            seed=seed,
            initial_capital=result.config.initial_capital,
        )
        return analyzer.analyze(result.equity_curve)

    def _build_html(
        self, result: BacktestResult, mc: Optional[MonteCarloResult] = None,
    ) -> str:
        """构建HTML内容"""
        m = result.metrics
        final_value = result.config.initial_capital * result.final_equity

        # 净值曲线数据（JS数组，渲染层降采样：301万点按日取末点，防超大 HTML）
        _curve = downsample_equity_curve(result.equity_curve)
        equity_dates = [ep.date for ep in _curve]
        equity_values = [round(ep.equity, 6) for ep in _curve]
        drawdown_values = [round(ep.drawdown * 100, 2) for ep in _curve]

        # 蒙特卡洛区块（mc 为 None 时为空，区块隐藏）
        mc_blocks = ""
        mc_scripts = ""
        if mc is not None:
            mc_blocks, mc_scripts = self._build_monte_carlo_blocks(mc)

        # 交易明细表格
        trades_rows = ""
        for i, t in enumerate(result.trades[-50:]):  # 最近50笔
            pnl_color = "#e74c3c" if t.pnl < 0 else "#27ae60"
            trades_rows += f"""
            <tr>
                <td>{i+1}</td>
                <td>{t.symbol}</td>
                <td>{t.side}</td>
                <td>{t.quantity:.0f}</td>
                <td>{t.price:.2f}</td>
                <td style="color:{pnl_color}">{t.pnl:.2f}</td>
                <td>{t.commission:.2f}</td>
                <td>{datetime.fromtimestamp(t.timestamp).strftime('%Y-%m-%d') if t.timestamp else ''}</td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{self.title}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f7fa; color: #333; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 12px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 24px; margin-bottom: 10px; }}
        .header .meta {{ opacity: 0.9; font-size: 14px; }}
        .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .card h2 {{ font-size: 18px; margin-bottom: 16px; color: #2c3e50; border-left: 4px solid #667eea; padding-left: 12px; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
        .metric-item {{ background: #f8f9fa; padding: 16px; border-radius: 8px; }}
        .metric-item .label {{ font-size: 12px; color: #7f8c8d; margin-bottom: 4px; }}
        .metric-item .value {{ font-size: 20px; font-weight: 600; color: #2c3e50; }}
        .metric-item .value.positive {{ color: #27ae60; }}
        .metric-item .value.negative {{ color: #e74c3c; }}
        .chart-container {{ position: relative; height: 300px; margin-bottom: 20px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; font-weight: 600; color: #7f8c8d; }}
        tr:hover {{ background: #f8f9fa; }}
        .footer {{ text-align: center; color: #95a5a6; font-size: 12px; padding: 20px; }}
        .risk-note {{ background: #fff8e1; color: #8a6d3b; border-left: 4px solid #f0ad4e; padding: 10px 14px; border-radius: 6px; font-size: 13px; margin-top: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{self.title}</h1>
            <div class="meta">
                策略: {result.config.strategy_name} | 
                期间: {result.config.start_date} ~ {result.config.end_date} | 
                初始资金: {result.config.initial_capital:,.0f} | 
                运行ID: {result.run_id}
            </div>
        </div>

        <div class="card">
            <h2>核心绩效</h2>
            <div class="metrics-grid">
                <div class="metric-item"><div class="label">总收益率</div><div class="value {'positive' if m.total_return >= 0 else 'negative'}">{m.total_return:.2%}</div></div>
                <div class="metric-item"><div class="label">年化收益率</div><div class="value {'positive' if m.annualized_return >= 0 else 'negative'}">{m.annualized_return:.2%}</div></div>
                <div class="metric-item"><div class="label">夏普比率</div><div class="value">{m.sharpe_ratio:.4f}</div></div>
                <div class="metric-item"><div class="label">最大回撤</div><div class="value negative">{m.max_drawdown:.2%}</div></div>
                <div class="metric-item"><div class="label">胜率</div><div class="value">{m.win_rate:.2%}</div></div>
                <div class="metric-item"><div class="label">盈亏比</div><div class="value">{'∞（全盈利）' if m.profit_loss_ratio == float('inf') else f'{m.profit_loss_ratio:.4f}'}</div></div>
                <div class="metric-item"><div class="label">总交易次数</div><div class="value">{m.total_trades}</div></div>
                <div class="metric-item"><div class="label">最终资产</div><div class="value {'positive' if final_value >= result.config.initial_capital else 'negative'}">{final_value:,.0f}</div></div>
            </div>
        </div>

        <div class="card">
            <h2>净值曲线</h2>
            <div class="chart-container"><canvas id="equityChart"></canvas></div>
        </div>

        <div class="card">
            <h2>回撤曲线</h2>
            <div class="chart-container"><canvas id="drawdownChart"></canvas></div>
        </div>

        {mc_blocks}

        <div class="card">
            <h2>交易明细（最近50笔）</h2>
            <table>
                <thead><tr><th>#</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>盈亏</th><th>手续费</th><th>日期</th></tr></thead>
                <tbody>{trades_rows}</tbody>
            </table>
        </div>

        <div class="footer">
            生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 鲲鹏量化交易系统 Pythonwdhmm
        </div>
    </div>

    <script>
        const equityDates = {equity_dates};
        const equityValues = {equity_values};
        const drawdownValues = {drawdown_values};

        new Chart(document.getElementById('equityChart'), {{
            type: 'line',
            data: {{
                labels: equityDates,
                datasets: [{{
                    label: '策略净值',
                    data: equityValues,
                    borderColor: '#667eea',
                    backgroundColor: 'rgba(102, 126, 234, 0.1)',
                    fill: true,
                    tension: 0.1,
                    pointRadius: 0,
                    borderWidth: 2
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: true }} }} }}
        }});

        new Chart(document.getElementById('drawdownChart'), {{
            type: 'line',
            data: {{
                labels: equityDates,
                datasets: [{{
                    label: '回撤 (%)',
                    data: drawdownValues,
                    borderColor: '#e74c3c',
                    backgroundColor: 'rgba(231, 76, 60, 0.1)',
                    fill: true,
                    tension: 0.1,
                    pointRadius: 0,
                    borderWidth: 2
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: true }} }} }}
        }});
        {mc_scripts}
    </script>
</body>
</html>"""
        return html

    def _build_monte_carlo_blocks(self, mc: MonteCarloResult) -> Tuple[str, str]:
        """构建蒙特卡洛HTML区块与JS脚本

        返回 (html_blocks, js_scripts)：
          - 蒙特卡洛统计卡（中位/P5/P95收益、亏损概率、回撤、95%CI、破产概率）
          - 最终收益分布直方图卡
          - 最大回撤分布直方图卡
          - 收益路径分位带卡（含风险提示：统计模拟，非未来预测）
        """
        # 直方图数据（区间中点标签）
        ret_labels, ret_counts = self._histogram_data(mc.final_returns, 30)
        dd_labels, dd_counts = self._histogram_data(mc.max_drawdowns, 30)

        # 分位带数据（P5/P25/P50/P75/P95 五条路径）
        q_labels = list(range(int(mc.quantile_equity.shape[1])))
        q_colors = ['#95a5a6', '#bdc3c7', '#667eea', '#bdc3c7', '#95a5a6']
        q_datasets = []
        for i, p in enumerate(mc.quantile_percentiles):
            q_datasets.append({
                "label": f"P{int(p)}",
                "data": [round(float(v), 4) for v in mc.quantile_equity[i]],
                "borderColor": q_colors[i],
                "borderWidth": 3 if p == 50 else 1.5,
                "pointRadius": 0,
                "fill": False,
                "tension": 0.1,
            })

        # 统计指标卡
        def _metric(label: str, value: str, cls: str = "") -> str:
            return (f'<div class="metric-item"><div class="label">{label}</div>'
                    f'<div class="value {cls}">{value}</div></div>')

        stats_grid = "".join([
            _metric("中位收益(模拟)", f"{mc.median_final_return:.2%}",
                    "positive" if mc.median_final_return >= 0 else "negative"),
            _metric("P5收益", f"{mc.p5_final_return:.2%}"),
            _metric("P95收益", f"{mc.p95_final_return:.2%}",
                    "positive" if mc.p95_final_return >= 0 else "negative"),
            _metric("亏损概率", f"{mc.prob_negative:.2%}",
                    "negative" if mc.prob_negative > 0.5 else ""),
            _metric("中位最大回撤", f"{mc.median_max_drawdown:.2%}", "negative"),
            _metric("最大回撤95%CI",
                    f"{mc.max_drawdown_ci_low:.2%} ~ {mc.max_drawdown_ci_high:.2%}", "negative"),
            _metric("破产概率", f"{mc.prob_ruin:.2%}",
                    "negative" if mc.prob_ruin > 0.05 else ""),
        ])

        # JS 数据（json 安全序列化）
        ret_labels_json = json.dumps(ret_labels, ensure_ascii=False)
        ret_counts_json = json.dumps(ret_counts)
        dd_labels_json = json.dumps(dd_labels, ensure_ascii=False)
        dd_counts_json = json.dumps(dd_counts)
        q_labels_json = json.dumps(q_labels)
        q_datasets_json = json.dumps(q_datasets, ensure_ascii=False)

        blocks = f"""
        <div class="card">
            <h2>蒙特卡洛分析（{mc.n_simulations}次模拟 × {mc.horizon_days}个交易日）</h2>
            <div class="metrics-grid">
                {stats_grid}
            </div>
        </div>

        <div class="card">
            <h2>最终收益分布（蒙特卡洛）</h2>
            <div class="chart-container"><canvas id="mcReturnChart"></canvas></div>
        </div>

        <div class="card">
            <h2>最大回撤分布（蒙特卡洛）</h2>
            <div class="chart-container"><canvas id="mcDrawdownChart"></canvas></div>
        </div>

        <div class="card">
            <h2>收益路径分位带（蒙特卡洛模拟）</h2>
            <div class="chart-container"><canvas id="mcQuantileChart"></canvas></div>
            <div class="risk-note">风险提示：以上为基于历史收益的统计模拟，不代表未来实际表现。</div>
        </div>
"""

        scripts = f"""
        const mcRetLabels = {ret_labels_json};
        const mcRetCounts = {ret_counts_json};
        const mcDdLabels = {dd_labels_json};
        const mcDdCounts = {dd_counts_json};
        const mcQLabels = {q_labels_json};
        const mcQDatasets = {q_datasets_json};

        new Chart(document.getElementById('mcReturnChart'), {{
            type: 'bar',
            data: {{ labels: mcRetLabels, datasets: [{{
                label: '路径数', data: mcRetCounts,
                backgroundColor: 'rgba(102, 126, 234, 0.6)', borderColor: '#667eea', borderWidth: 1
            }}] }},
            options: {{ responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{ x: {{ ticks: {{ maxRotation: 45 }}, title: {{ display: true, text: '期末收益率' }} }},
                          y: {{ title: {{ display: true, text: '路径数' }} }} }} }}
        }});

        new Chart(document.getElementById('mcDrawdownChart'), {{
            type: 'bar',
            data: {{ labels: mcDdLabels, datasets: [{{
                label: '路径数', data: mcDdCounts,
                backgroundColor: 'rgba(231, 76, 60, 0.6)', borderColor: '#e74c3c', borderWidth: 1
            }}] }},
            options: {{ responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{ x: {{ ticks: {{ maxRotation: 45 }}, title: {{ display: true, text: '最大回撤' }} }},
                          y: {{ title: {{ display: true, text: '路径数' }} }} }} }}
        }});

        new Chart(document.getElementById('mcQuantileChart'), {{
            type: 'line',
            data: {{ labels: mcQLabels, datasets: mcQDatasets }},
            options: {{ responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ display: true, position: 'bottom' }} }},
                scales: {{ x: {{ title: {{ display: true, text: '模拟期数(交易日)' }} }},
                          y: {{ title: {{ display: true, text: '模拟净值' }} }} }} }}
        }});
"""
        return blocks, scripts

    @staticmethod
    def _histogram_data(values: List[float], bins: int = 30) -> Tuple[List[str], List[int]]:
        """数值数组 → 直方图数据（标签=区间中点百分比，计数），供 Chart.js bar"""
        arr = np.asarray(values, dtype=float)
        if len(arr) == 0:
            return [], []
        counts, edges = np.histogram(arr, bins=bins)
        labels = [f"{(edges[i] + edges[i + 1]) / 2:.1%}" for i in range(len(counts))]
        return labels, [int(c) for c in counts]

    # ------------------------------------------------------------------
    # CSV导出
    # ------------------------------------------------------------------

    def generate_trades_csv(self, result: BacktestResult, output_path: str = "outputs/trades.csv") -> str:
        """导出交易明细CSV（#P3-1修复：无成交时跳过导出，不抛空表异常）"""
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        data = [t.to_dict() for t in result.trades]
        if not data:
            logger.info(f"无成交记录，跳过交易明细CSV导出: {output_path}")
            return output_path
        df = pd.DataFrame(data)
        export_csv_cn(df, output_path, header_map=CSV_HEADER_MAPS["trades"])
        logger.info(f"交易明细已导出: {output_path} ({len(df)} 行)")
        return output_path

    def generate_equity_csv(self, result: BacktestResult, output_path: str = "outputs/equity.csv") -> str:
        """导出净值曲线CSV（#P3-1修复：空净值时跳过导出，不抛空表异常）"""
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        data = [ep.to_dict() for ep in result.equity_curve]
        if not data:
            logger.info(f"净值曲线为空，跳过净值CSV导出: {output_path}")
            return output_path
        df = pd.DataFrame(data)
        export_csv_cn(df, output_path, header_map=CSV_HEADER_MAPS["equity"])
        logger.info(f"净值曲线已导出: {output_path} ({len(df)} 行)")
        return output_path

    # ------------------------------------------------------------------
    # 完整报告生成
    # ------------------------------------------------------------------

    def generate_full_report(
        self,
        result: BacktestResult,
        output_dir: str = "outputs",
        include_monte_carlo: bool = True,
        mc_n_simulations: int = 2000,
        mc_horizon_days: int = 252,
        mc_seed: int = 42,
        include_pdf: bool = True,
    ) -> Dict[str, str]:
        """
        生成完整报告（HTML + PDF + 文本 + CSV，HTML/PDF含蒙特卡洛区块）

        参数:
            result: 回测结果
            output_dir: 输出目录
            include_monte_carlo: 是否包含蒙特卡洛分析（默认True）
            mc_n_simulations: 蒙特卡洛模拟次数
            mc_horizon_days: 蒙特卡洛模拟期数/交易日
            mc_seed: 蒙特卡洛随机种子
            include_pdf: 是否同时生成PDF报告（默认True）

        返回:
            Dict: 各报告文件路径
        """
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        paths = {
            'html': self.generate_html(
                result, os.path.join(output_dir, f"report_{timestamp}.html"),
                include_monte_carlo, mc_n_simulations, mc_horizon_days, mc_seed,
            ),
            'trades_csv': self.generate_trades_csv(result, os.path.join(output_dir, f"trades_{timestamp}.csv")),
            'equity_csv': self.generate_equity_csv(result, os.path.join(output_dir, f"equity_{timestamp}.csv")),
        }

        # PDF 报告（第4项C：PdfReportGenerator）
        if include_pdf:
            pdf_gen = PdfReportGenerator(title=self.title)
            paths['pdf'] = pdf_gen.generate_pdf(
                result, os.path.join(output_dir, f"report_{timestamp}.pdf"),
                include_monte_carlo, mc_n_simulations, mc_horizon_days, mc_seed,
            )

        # 文本报告
        text = self.generate_text(result)
        text_path = os.path.join(output_dir, f"summary_{timestamp}.txt")
        with open(text_path, 'w', encoding='utf-8') as f:
            f.write(text)
        paths['text'] = text_path

        # 真实性门禁清单落盘（方案 §4.3"必须落盘：清单强制写入 results.json"；
        # 回写#355：正常路径（回测成功）同样落盘——此前仅阻断路径 blocked 产物
        # 已接线，正常路径清单无处可查，生产回测暴露后补齐）
        manifest = getattr(result, "realism_manifest", None)
        if manifest:
            write_manifest_to_results(os.path.join(output_dir, "results.json"), manifest)
            logger.info(f"真实性门禁清单已写入 results.json: {output_dir}")

        logger.info(f"完整报告已生成: {output_dir}")
        return paths
