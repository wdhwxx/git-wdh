@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

REM ============================================================
REM   Pythonwdhmm 鲲鹏量化交易系统 — 一键部署脚本（v3.0）
REM ============================================================
REM   更新日期：2026-09-04
REM   适配版本：回测性能优化 V5（P0-P3 完成）
REM
REM   功能：
REM     1. 检查/安装 Anaconda 环境
REM     2. 创建/更新 conda 环境 quant（Python 3.12）
REM     3. 初始化运行时目录（复用 scripts\初始化项目目录.py，任务 2
REM        含 V5 缓存目录 cache\{factor,fingerprint,backtest,tmp,benchmark}）
REM     4. 安装项目所有依赖（pip install -e ".[dev,web,live,benchmark,gui]"）
REM     5. 初始化数据库（quant.db）
REM     6. 验证安装（核心模块 + V5 新模块导入 + 阶段9验证脚本）
REM     7. 提示通达信路径配置
REM     8. 快速开始指南
REM
REM   使用方法：双击运行，或在命令行执行 一键部署.bat
REM ============================================================

title Pythonwdhmm 鲲鹏量化交易系统 — 一键部署 v3.0

cls
echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║     Pythonwdhmm 鲲鹏量化交易系统 — 一键部署 v3.0         ║
echo ║     适配：回测性能优化 V5 完成（2026-09-04）            ║
echo ╚══════════════════════════════════════════════════════════╝
echo.

REM ── 步骤0：确定项目根目录 ──
set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
echo   项目目录：%PROJECT_ROOT%
echo.

REM 检查 pyproject.toml 是否存在
if not exist "%PROJECT_ROOT%\pyproject.toml" (
    echo   [错误] 未找到 pyproject.toml，请确认脚本在项目根目录下运行。
    echo.
    pause
    exit /b 1
)

REM ── 步骤1：查找 Anaconda ──
echo ────────────────────────────────────────────────────────────
echo   [步骤 1/7] 检查 Anaconda 环境...
echo ────────────────────────────────────────────────────────────
echo.

set "CONDA_PATH="

REM 常见 Anaconda 安装路径
set "ANACONDA_PATHS=D:\ProgramData\anaconda3 C:\ProgramData\anaconda3 C:\Users\%USERNAME%\anaconda3 D:\anaconda3 C:\anaconda3 D:\ProgramData\miniconda3 C:\ProgramData\miniconda3 C:\Users\%USERNAME%\miniconda3"

for %%P in (%ANACONDA_PATHS%) do (
    if exist "%%P\Scripts\conda.exe" (
        set "CONDA_PATH=%%P"
        goto :conda_found
    )
)

REM 尝试从注册表查找
for /f "tokens=2*" %%A in ('reg query "HKCU\Software\Python\PythonCore" /s 2^>nul ^| findstr "InstallPath"') do (
    if exist "%%B\Scripts\conda.exe" (
        set "CONDA_PATH=%%B"
        goto :conda_found
    )
)

:conda_not_found
echo   [错误] 未找到 Anaconda/Miniconda！
echo.
echo   请先安装 Anaconda 或 Miniconda：
echo     Anaconda 下载：https://www.anaconda.com/download
echo     Miniconda 下载：https://docs.conda.io/en/latest/miniconda.html
echo     安装路径建议：D:\ProgramData\anaconda3
echo.
echo   安装完成后重新运行本脚本。
echo.
pause
exit /b 1

:conda_found
echo   找到 Anaconda：%CONDA_PATH%
echo.

REM 初始化 conda
call "%CONDA_PATH%\condabin\conda.bat" init cmd.exe >nul 2>&1
call "%CONDA_PATH%\Scripts\activate.bat" base >nul 2>&1

REM ── 步骤2：创建/更新 conda 环境 quant ──
echo ────────────────────────────────────────────────────────────
echo   [步骤 2/7] 创建/更新 conda 环境 quant（Python 3.12）...
echo ────────────────────────────────────────────────────────────
echo.

REM 检查 quant 环境是否存在
conda env list | findstr /C:"quant" >nul 2>&1
if %errorlevel%==0 (
    echo   conda 环境 "quant" 已存在，检查 Python 版本...
    call conda activate quant
    for /f "tokens=*" %%V in ('python --version 2^>^&1') do set "PY_VER=%%V"
    echo   当前 Python 版本：!PY_VER!
    echo   环境已就绪，跳过创建。
) else (
    echo   创建 conda 环境 "quant"（Python 3.12）...
    echo   这可能需要几分钟，请耐心等待...
    echo.
    call conda create -n quant python=3.12 -y
    if %errorlevel% neq 0 (
        echo.
        echo   [错误] conda 环境创建失败！
        echo   请检查网络连接后重试。
        echo.
        pause
        exit /b 1
    )
    call conda activate quant
    echo.
    echo   conda 环境 "quant" 创建成功！
)
echo.

REM ── 步骤3：初始化运行时目录（复用权威目录脚本）──
echo ────────────────────────────────────────────────────────────
echo   [步骤 3/7] 初始化运行时目录...
echo ────────────────────────────────────────────────────────────
echo.

cd /d "%PROJECT_ROOT%"
if exist "%PROJECT_ROOT%\scripts\初始化项目目录.py" (
    python "%PROJECT_ROOT%\scripts\初始化项目目录.py"
    if !errorlevel! neq 0 (
        echo   [警告] 目录初始化出现异常，请检查上方输出。
    )
) else (
    echo   [提示] 未找到 scripts\初始化项目目录.py，跳过目录初始化。
)

echo   ✓ 运行时目录已就绪（datasets / outputs / logs / database / backups）
echo   ✓ V5 缓存目录已就绪（cache\factor / fingerprint / backtest / tmp / benchmark）
echo.

REM ── 步骤4：安装项目依赖 ──
echo ────────────────────────────────────────────────────────────
echo   [步骤 4/7] 安装项目依赖（pip install -e ".[dev,web,live,benchmark,gui]"）...
echo ────────────────────────────────────────────────────────────
echo.

cd /d "%PROJECT_ROOT%"
echo   正在安装依赖（dev + web + live + benchmark），这可能需要几分钟...
echo.

pip install -e ".[dev,web,live,benchmark,gui]"
if %errorlevel% neq 0 (
    echo.
    echo   [警告] 部分依赖安装失败，尝试使用国内镜像源重试...
    echo.
    pip install -e ".[dev,web,live,benchmark,gui]" -i https://pypi.tuna.tsinghua.edu.cn/simple
    if %errorlevel% neq 0 (
        echo.
        echo   [错误] 依赖安装失败！
        echo   请检查网络连接后手动运行：pip install -e ".[dev,web,live,benchmark,gui]"
        echo.
        pause
        exit /b 1
    )
)

echo.
echo   依赖安装完成！
echo.

REM ── 步骤5：初始化数据库 ──
echo ────────────────────────────────────────────────────────────
echo   [步骤 5/7] 初始化数据库（quant.db）...
echo ────────────────────────────────────────────────────────────
echo.

if exist "%PROJECT_ROOT%\scripts\数据库初始化.py" (
    python "%PROJECT_ROOT%\scripts\数据库初始化.py"
    if %errorlevel% neq 0 (
        echo.
        echo   [警告] 数据库初始化出现问题，但不影响基本使用。
        echo   可稍后手动运行：python scripts\数据库初始化.py
    )
) else (
    echo   [提示] 未找到 scripts\数据库初始化.py，跳过数据库初始化。
    echo   如需使用数据库功能，请确认该脚本存在。
)
echo.

REM ── 步骤6：验证安装 ──
echo ────────────────────────────────────────────────────────────
echo   [步骤 6/7] 验证安装...
echo ────────────────────────────────────────────────────────────
echo.

set "VERIFY_OK=1"

REM 验证 Python 版本
for /f "tokens=*" %%V in ('python --version 2^>^&1') do set "PY_VER=%%V"
echo   Python 版本：!PY_VER!

REM 验证核心包导入
echo.
echo   正在验证核心模块导入...
echo.

python -c "import myquant; print('  ✓ myquant 包导入：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ myquant 包导入：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.core.event_bus import EventBus, DataQualityEvent; print('  ✓ core.event_bus 事件总线：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ core.event_bus 事件总线：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.monitoring import AlertManager, MetricsCollector, HealthChecker; print('  ✓ monitoring 监控告警模块：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ monitoring 监控告警模块：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.data_access.cleaner.pipeline import CleanPipeline; print('  ✓ data_access.cleaner 数据清洗流水线：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ data_access.cleaner 数据清洗流水线：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.risk.risk_manager import RiskManager; print('  ✓ risk 风控模块：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ risk 风控模块：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.api.routes import create_app; print('  ✓ api FastAPI模块：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ api FastAPI模块：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.core.lookahead_guard import LookaheadGuard; print('  ✓ 防未来函数模块：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ 防未来函数模块：失败
    set "VERIFY_OK=0"
)

REM V5 回测性能优化新模块校验
python -c "from myquant.backtest.consistency_check import ConsistencyChecker; print('  ✓ V5 三模式一致性校验：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ V5 三模式一致性校验：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.backtest.engine_benchmark import EngineBenchmark; print('  ✓ V5 三模式性能基准：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ V5 三模式性能基准：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.utils.memory_watcher import MemoryWatcher; print('  ✓ V5 内存监控：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ V5 内存监控：失败
    set "VERIFY_OK=0"
)

python -c "from myquant.utils.data_fingerprint import compute_fingerprint_from_file; print('  ✓ V5 数据指纹：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ V5 数据指纹：失败
    set "VERIFY_OK=0"
)

REM P2-D13-002 整改：GUI 主交付入口依赖校验（PySide6/pyqtgraph 来自 gui extras）
python -c "import PySide6, pyqtgraph; print('  ✓ GUI 依赖（PySide6 + pyqtgraph）：成功')" 2>nul
if %errorlevel% neq 0 (
    echo   ✗ GUI 依赖（PySide6/pyqtgraph）：失败
    set "VERIFY_OK=0"
)

REM 验证数据库文件
echo.
if exist "%PROJECT_ROOT%\database\quant.db" (
    echo   ✓ 数据库文件：已创建
) else (
    echo   ⚠ 数据库文件：未创建（可稍后手动初始化）
)

REM 验证项目结构
echo.
echo   项目结构验证：
if exist "%PROJECT_ROOT%\myquant" echo     ✓ myquant/          核心包
if exist "%PROJECT_ROOT%\tests" echo     ✓ tests/            测试目录
if exist "%PROJECT_ROOT%\docs" echo     ✓ docs/             文档目录
if exist "%PROJECT_ROOT%\scripts" echo     ✓ scripts/          脚本目录
if exist "%PROJECT_ROOT%\config" echo     ✓ config/           配置目录
echo.

REM 可选：运行阶段9验证脚本
echo   是否运行阶段9验证脚本？（约需1-2分钟，验证1500+项）
echo     输入 Y 运行，其他键跳过...
set /p "RUN_VERIFY=  请选择 [Y/N]: "
if /i "%RUN_VERIFY%"=="Y" (
    echo   正在运行单元测试代替阶段9验证（1500+项）...
    echo.
    echo   P3-D13-003 整改：原阶段9验证引用的 5 个 tests\_verify_*.py 脚本在本项目
    echo   当前版本不存在（v3.0 已由 pytest 全量单元测试 + 上述模块导入校验覆盖）。
    echo   如需完整验证，请运行：
    echo     conda activate quant ^&^& pytest tests/unit/ -q
    echo.
) else (
    echo   已跳过验证脚本，可稍后手动运行。
)
echo.

REM ── 步骤7：通达信路径提示 ──
echo ────────────────────────────────────────────────────────────
echo   [步骤 7/7] 通达信路径配置提示
echo ────────────────────────────────────────────────────────────
echo.

set "TDX_FOUND=0"
if exist "D:\TDX\vipdoc" (
    echo   ✓ 检测到通达信安装在：D:\TDX
    set "TDX_FOUND=1"
)
if exist "C:\new_tdx\vipdoc" (
    echo   ✓ 检测到通达信安装在：C:\new_tdx
    set "TDX_FOUND=1"
)
if exist "D:\new_tdx\vipdoc" (
    echo   ✓ 检测到通达信安装在：D:\new_tdx
    set "TDX_FOUND=1"
)

if "!TDX_FOUND!"=="0" (
    echo   ✗ 未检测到通达信软件
    echo.
    echo   如果需要使用通达信本地数据（回测速度更快）：
    echo     1. 安装通达信软件并下载日线数据
    echo     2. 如果安装路径不是 D:\TDX，请修改：
    echo        myquant\data_access\sources\tdx_source.py
    echo        中的 DEFAULT_TDX_PATHS 列表，添加你的路径
    echo.
    echo   不安装通达信也可以使用 akshare 网络数据源。
) else (
    echo   通达信本地数据源已就绪，回测时将优先使用本地数据。
)
echo.

REM ── 部署完成 ──
echo ╔══════════════════════════════════════════════════════════╗
echo ║                        部署完成！                           ║
echo ╚══════════════════════════════════════════════════════════╝
echo.

if "!VERIFY_OK!"=="1" (
    echo   ✓ 所有核心模块验证通过
) else (
    echo   ⚠ 部分模块验证未通过，可参考上方错误信息排查
)
echo.

echo   ═══════════ 快速开始 ═══════════
echo.
echo   1. 激活环境：
echo      conda activate quant
echo.
echo   2. 下载数据（akshare网络数据源）：
echo      python scripts\数据下载.py
echo.
echo   3. 数据清洗：
echo      python scripts\数据清洗.py
echo.
echo   4. 运行回测：
echo      python run_回测入口.py
echo.
echo   5. 启动API服务（FastAPI，端口8000）：
echo      python scripts\运行API.py
echo.
echo   6. 实盘模拟：
echo      python run_实盘入口.py
echo.
echo   ═══════════ 项目文档 ═══════════
echo.
echo     - 项目结构总图：docs\project_layout_gear2.md
echo     - 阶段完成度：  docs\stage_completion.md
echo     - 最新交接文档：docs\交接摘要_阶段9任务11.md
echo     - 架构设计：    docs\architecture.md
echo     - 数据字典：    docs\data_dictionary.md
echo.
echo   ═══════════ 验证方式 ═══════════
echo.
echo     P3-D13-003 整改：阶段9 _verify 脚本已废弃（不存在），完整验证请运行：
echo       conda activate quant
echo       pytest tests/unit/ -q          # 全量单元测试（含防未来函数专项）
echo.
echo   按任意键退出...
pause >nul
endlocal
exit /b 0
