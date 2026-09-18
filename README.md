# Pythonwdhmm

## 鲲鹏量化交易系统

面向沪深A股日线级别的量化回测与实盘交易系统。

**核心策略**：RSRS(18/0.7) + 目标止盈+20% + 三因子交集股票池（低波动∩高价∩高动量）

**特点**：严格无未来函数、回测与实盘参数一致、支持多券商无缝对接、多渠道消息推送。

---

## 环境准备

```bash
# 创建conda环境
conda create -n quant python=3.10 -y
conda activate quant

# 安装核心依赖
pip install -e .

# 安装开发依赖（含pytest、防未来函数校验测试）
pip install -e ".[dev]"

# 安装实盘依赖（实时行情、券商对接、定时任务）
pip install -e ".[live]"
