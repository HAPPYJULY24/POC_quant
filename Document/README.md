# Bursa Derivatives POC Quant System (大马衍生品交易所 POC 量化交易系统)

Bursa Derivatives POC Quant System 是一款针对**马来西亚衍生品交易所 (Bursa Malaysia Derivatives, BMD)** 的棕榈油期货 (**FCPO**) 的高可靠性、多线程、人机协同（Human-in-the-Loop, 人形 API）的量化交易平台。系统集成了数据获取、算法决策与风控过滤等核心模块，通过科学的资金分配与强健的防御性编程，保障实盘手动跟单的高效性与安全性。

---

## 🏗️ 系统三层核心架构 (Three-Tier Architecture)

本系统严格按照模块化与非阻塞式的生产级标准设计，分为以下三层逻辑：

```
                    ┌──────────────────────────────────────────────┐
                    │       Layer 1: Data Ingestion (数据获取管道)   │
                    │   - BMD Active Sessions & KL Timezone        │
                    │   - TradingView (TVDatafeed) & yfinance Feed │
                    │   - Break Catch-Up (断线追赶) & Forward Fill │
                    └──────────────────────┬───────────────────────┘
                                           │ (AlignedPayload)
                                           ▼
                    ┌──────────────────────────────────────────────┐
                    │         Layer 2: Factor Brain (算法因子引擎)   │
                    │   - Polymorphic Custom Deque Window          │
                    │   - Asymmetric Threshold Signal Generators   │
                    │   - ZeroVariance & NaN-Poisoning Defenses    │
                    └──────────────────────┬───────────────────────┘
                                           │ (SignalPayload)
                                           ▼
                    ┌──────────────────────────────────────────────┐
                    │      Layer 3: Risk Sentinel (风控交互哨兵)    │
                    │   - Decoupled Producer-Consumer Queue        │
                    │   - Dynamic Position Sizing (ATR-based Lots) │
                    │   - Compliance, Margin checks, Tick rounding │
                    │   - HFE CLI Feedback Loop (Anti-typo/Abort)  │
                    └──────────────────────────────────────────────┘
```

---

## 📚 业务与逻辑模块介绍

### 📊 Layer 1: 数据获取与清洗管道 (Data Ingestion Pipeline)
* **吉隆坡时间与交易时区对齐**：系统完全采用 `Asia/Kuala_Lumpur` 吉隆坡本地时间进行调度，并在非交易时段（如周末、交易休市间歇）自动休眠，避免发送多余的 API 请求。
* **高可靠性双轨行情抓取**：优先通过 `tvDatafeed` 匿名连接 TradingView 获取高频 15分钟 K线，如遭遇连接超时或反爬拦截，系统自动无缝降级（Fallback）至 `yfinance` 行情接口，确保信号绝不断流。
* **断线追赶机制 (Catch-up Mechanism)**：每次系统冷启动或断网重连时，系统通过比对 SQLite 数据库中最新的一条记录戳，动态计算缺失的 Bar 手数，启动批量追赶抓取（Batch Ingestion），填补历史断层。
* **时序前向填充 (Forward Fill)**：多资产抓取时（FCPO、CBOT 豆油、USDMYR 汇率），若由于跨市场休市（如美国感恩节 CBOT 停盘，而 BMD 正常交易）导致行情不一致，系统自动通过 `ffill` 技术向后推演填充，保证计算矩阵始终对齐。
* **换月波动检测 (Rollover Detection)**：内置双向滑移内存，实时监测当前价格跳空。如果当前开盘价波动大于 **5.0 倍 ATR**，立即发出强烈的换月警示信号，并指挥风控模块执行紧急清仓防线，防御虚假因子得分。

### 🧠 Layer 2: 算法与多态因子引擎 (Polymorphic Factor Brain)
* **多态因子框架**：以抽象基类 `BaseFactor` 为契约，核心的 `ZScoreArbitrageFactor` 派生类对价差（Spread）进行滑动标准差和均值计算，支持任何新因子的动态插拔与即插即用。
* **全局非对称阈值配置 (Asymmetric Thresholds)**：拒绝硬编码和对称负号设计，在 `settings.json` 中配置多空 entry 和 exit 参数，严格按照以下公式输出信号得分：
  * $Z_t > \text{upper\_entry\_threshold} \rightarrow \mathbf{-1}$ (做空价差)
  * $Z_t < \text{lower\_entry\_threshold} \rightarrow \mathbf{1}$ (做多价差)
  * $-\text{exit\_threshold} \le Z_t \le \text{exit\_threshold} \rightarrow \mathbf{0}$ (平仓)
  * 其他区域 $\rightarrow \mathbf{99}$ (维持上一期仓位状态，引入状态滞后 Hysteresis 防御震荡)
* **零方差熔断机制 (ZeroVariance Defense)**：当盘面长时间横盘，导致标准差 $\sigma = 0$ 时，系统通过自定义的 `ZeroVarianceException` 进行捕获拦截，避免抛出除以零的系统崩溃错误，强制降级并返回安全因子分 `0.0`。
* **NaN 毒素过滤**：任何含 `NaN` 或 `None` 的对齐异常行情数据，将直接被因子更新入口（`process`）阻断，绝不推入 Deque 滑动内存，确保历史滑动计算指标 100% 洁净。

### 🛡️ Layer 3: 风控交互哨兵 (Risk Sentinel & Human API Loop)
* **非阻塞生产者-消费者模型 (Producer-Consumer)**：
  * **后台生产者 (Producer)**：利用多线程的 `BackgroundScheduler` 异步在后台轮询、抓取行情、对齐状态并运行因子大脑，将产生的 `SignalPayload` 推入线程安全的 `queue.Queue` 中。
  * **前台消费者 (Consumer)**：主线程死循环阻塞扫描 `Queue`。一旦发现信号，风控哨兵立即拦截激活，发出铃响提示，并利用 `input()` 挂起前台。
  * **架构优势**：当人类交易员因为接听电话、处理杂务而延迟输入时，后台线程**依然照常准确抓取后续每根 K 线**，绝不导致行情断层。
* **动态算仓公式 (Dynamic Position Sizing)**：
  $$\text{Target\_Lots} = \lfloor \frac{\text{Capital} \times \text{Risk\_Pct}}{2.0 \times \text{ATR} \times \text{Multiplier}} \rfloor$$
  根据账户净资本、单笔回撤风险比以及盘面真实的 14 周期 ATR 波动率，完全数学化计算出科学下单手数，防御人类主观贪婪或恐惧。
* **可用保证金与合规防御 (Compliance & Margin Interceptions)**：
  * 单手保证金 `margin_per_lot`（默认 RM 8,000）硬性过滤。若总保证金开仓要求大于现有可用资金，手数自动向下截断（如测试用例中本金 RM 50,000 申请开仓 10 手，系统警告并强制截断下单为 6 手）。
  * 自动比对持仓是否超过配置中心的 `max_position_lots`（默认 20 手），溢出部分直接斩断。
* **Tick 圆整清洗**：建议单与止损单价格自动圆整至标的交易所的 Tick 分辨率（FCPO 为 `1.0`，FKLI 为 `0.5`），去除高频小数，防御人工手滑敲单。
* **人类工程学容错设计 (HFE CLI Validation)**：
  * **防手滑校验**：交互输入包裹在 `try-except ValueError` 中，人类误触回车、敲入英文字符（如 `4250a`）等错误不会导致主线程崩溃，系统会无限重试并红字提示。
  * **交易放弃机制 (Trade Abort)**：在限价跌停、网络卡死或券商断线等突发极端市场下，在手数栏输入 **`0`** 即可完美抛弃本次信号。系统会重置交互状态并回到扫描队列，**SQLite 内部的仓位状态与资金余额绝不发生修改**。
  * **滑点审计与记录 (Slippage TCA)**：系统支持用户输入“实际成交均价”，以此更新 `portfolio_state` 保证之后的损益和 PnL 计算无误。同时，系统在后台日志中自动记录“实际成交价”与“系统建议价”的绝对滑点差异，供后期交易成本分析（TCA）复盘审计。
  * **异步非阻塞 SQLite 写入**：因子计算与信号入库采用单线程 `ThreadPoolExecutor(1)` 异步排队写入，并利用 `Future` 回调捕获 `OperationalError`（Database Locked 冲突），确保系统高频轮询的绝对平稳。

---

## 📂 项目目录结构 (Directory Structure)

```
d:\personal\quant\Poc\
│
├── config/
│   └── settings.json           # 全局配置中心 (包含时区、本金、非对称风控阈值、标的乘数与Tick)
│
├── src/
│   ├── __init__.py
│   ├── config.py               # 配置文件解析与实时开盘时间校验器
│   ├── data_sources.py         # 行情 API 抓取适配器 (TradingView + yfinance Fallback 双轨)
│   ├── data_ingestion.py       # 历史补缺、前向填充对齐管道、换月警示
│   ├── database.py             # SQLite DDL 创建与异步写回 SQL 适配层
│   ├── factors.py              # 多态因子抽象基类、Z-Score 因子计算、零方差熔断与NaN保护
│   ├── models.py               # 强类型数据契约 (AlignedPayload, SignalPayload, PortfolioState)
│   ├── risk_sentinel.py        # 风控模块主引擎 (仓位感知、动态算仓、平仓优先、CLI HFE 逻辑)
│   └── utils.py                # 全局日志系统、吉隆坡时间戳、Windows UTF-8 终端编码转换器
│
├── Tests/                      # 全套测试套件
│   ├── __init__.py
│   ├── test_ingestion.py       # Layer 1 管道数据对齐、前向填充、换月与断线追赶测试
│   ├── test_factors.py         # Layer 2 因子 asymmetric 信号、零方差熔断、NaN毒素测试
│   └── test_risk.py            # Layer 3 风控 tick 圆整、动态算仓、反向平仓优先、保证金截断测试
│
├── main.py                     # 全局多线程主程序 (生产者-消费者调度中枢)
├── requirements.txt            # 项目 Python 依赖清单
├── bursa_poc.db                # SQLite 历史 aligned 数据库 & 仓位状态快照 (运行后自动创建)
└── poc_quant.log               # 系统运行全局回执日志 (运行后自动创建)
```

---

## 🛠️ 环境准备与系统运行 (Installation & Execution)

### 1. 配置 Python 环境
本系统要求使用 **Python 3.10+**。推荐在 Miniconda/Anaconda 中创建虚拟环境：

```powershell
# 1. 创建名为 poc_quant 的虚拟环境 (Python 3.10)
conda create -n poc_quant python=3.10 -y

# 2. 激活虚拟环境
conda activate poc_quant

# 3. 安装依赖库
pip install -r requirements.txt
```

> **Requirements 说明**：项目核心库包含 `pandas`, `numpy`, `pytz`, `apscheduler`, `yfinance` 以及 TradingView SDK `tvdatafeed`（安装命令已在 `requirements.txt` 中就绪）。

---

### 2. 运行自动化测试套件
在主程序上线前，必须确保本地 20 个单元测试（7 个数据层、6 个算法层、7 个风控层）全部通过，在项目根目录下执行：

```powershell
# 运行全套 20 个单元测试
python -m unittest discover -s Tests -p "test_*.py"
```

**测试通过标准输出回执：**
```
Ran 20 tests in 0.160s

OK
```

---

### 3. 运行量化交易系统 (主程序)
通过主轮询运行核心主程序，它将初始化数据库架构、执行断线 catch-up 追赶、开启 APScheduler 异步线程、并等待信号推送至前台命令行交互：

```powershell
python main.py
```

#### 💡 系统运行日志回执预览 (Live Session Ingestion Logs)：
```text
[2026-06-01 23:35:01] [INFO] [orchestrator]: ==================================================
[2026-06-01 23:35:01] [INFO] [orchestrator]:    Starting Bursa derivatives POC Quant System    
[2026-06-01 23:35:01] [INFO] [orchestrator]: ==================================================
[2026-06-01 23:35:01] [INFO] [orchestrator]: Configuration settings loaded successfully.
[2026-06-01 23:35:01] [INFO] [data_sources]: TradingView anonymous client initialized successfully.
[2026-06-01 23:35:01] [INFO] [factors]: Initialized ZScore_FCPO_ZL (Lookback=40, UpperEntry=2.0, LowerEntry=-2.5, Exit=0.5)
[2026-06-01 23:35:01] [INFO] [orchestrator]: Bootstrapping ingestion pipeline...
[2026-06-01 23:35:01] [INFO] [data_ingestion]: Initializing Database schemas...
[2026-06-01 23:35:01] [INFO] [data_ingestion]: Portfolio state initialized/loaded for POC_ZSCORE_FCPO.
[2026-06-01 23:35:02] [INFO] [data_ingestion]: Checking for data sync gaps (断线追赶机制)...
[2026-06-01 23:35:03] [INFO] [data_ingestion]: Catch-up alignment complete. No missing gaps.
[2026-06-01 23:35:03] [INFO] [orchestrator]: Bootstrap complete!
[2026-06-01 23:35:03] [INFO] [orchestrator]: Pre-warming factor brain memory deques from SQLite aligned history...
[2026-06-01 23:35:03] [INFO] [orchestrator]: Factor brain memory warmed up with 40 historical records.
[2026-06-01 23:35:03] [INFO] [orchestrator]: Cron BackgroundScheduler aligned to */15m:05s (Kuala Lumpur Time).
[2026-06-01 23:35:03] [INFO] [orchestrator]: Starting scheduler thread...
[2026-06-01 23:35:03] [INFO] [orchestrator]: Starting foreground CLI signal consumer loop. Press Ctrl+C to exit.
```

当 15分钟 K线触发信号被推入前台时，控制台将播放 Terminal Bell 铃响，并打印高对比度提示：

```text
======================================================================
🚨🚨🚨 【风控拦截系统：审核绿灯通过 - 交易指令下达】 🚨🚨🚨
======================================================================
  [时间]   : 2026-06-01 23:30:00
  [标的]   : FCPO (大马棕榈油期货)
  [类型]   : OPEN (BUY LONG (做多开仓))
  [手数]   : 3 手
  [建议价] : RM 4520.0
  [止损价] : RM 4480.0 (硬性止损！请在交易端同时挂单！)
----------------------------------------------------------------------
  请立即前往 Bursa derivatives 虚拟交易软件执行上述操作！
  执行完毕后，请在下方交互栏中如实输入实际成交数据进行状态确认。
======================================================================
👉 [请输入实际成交手数 (建议: 3 手) | 输入 0 放弃本次交易]: 3
👉 [请输入实际成交价格 (建议: RM 4520.0)]: 4522
[2026-06-01 23:35:48] [INFO] [risk_sentinel]: TCA Slippage Audit: Suggested Price = RM 4520.0 | Actual Price = RM 4522.0 | Slippage Cost = -2.0 points.
[2026-06-01 23:35:48] [INFO] [risk_sentinel]: Portfolio state successfully synced in SQLite: Capital = RM 100000.0 | Position = 1 (3 lots) | Avg Price = 4522.0
✅ [交易状态同步成功] 正在返回前台扫描死循环...
```

---

## 🛡️ 生产环境安全约束 (Production Restrictions)

1. **SQLite 锁防御**：系统不允许多个外部进程同时频繁写入 SQLite，所有的因子结果入库由 `ThreadPoolExecutor` 的后台队列排队写入，彻底隔绝 `Database Locked` 的锁冲突崩溃问题。
2. **时效高对比显示**：前台 CLI 提示与校验代码通过 `sys.stdout.reconfigure` 强制重载，不受 Windows 平台系统区域语言环境影响，在所有终端下皆能以 UTF-8 显示大字画板与 Emojis 状态。
3. **极速止损提示**：在发出开仓命令时，系统会自动利用 ATR 计算出硬性止损价，并在命令行醒目提示。交易员必须在交易软件上**同时挂出限价建议单与止损触发单**，以防御反向跳空风险。
