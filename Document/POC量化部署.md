### **前言：**

该系统构造是专门服务于Bursa Derivatives Virtual Trading Challenge（虚拟交易挑战赛） 2026.该系统将采用半自动下单模式系统；系统生成买卖单，人工下单。

为了保证 POC（概念验证）的科学性，你必须在比赛中严格遵守以下两条军规：

### **铁律一：你的策略绝对不能是“高频/短线”策略**

* **原因**：由于从“系统生成信号 $\\rightarrow$ 你的眼睛看到 $\\rightarrow$ 鼠标点击下单”存在 **3-10 秒的人类延迟**。  
* **导师建议**：你的策略应当是**日内趋势、小时级别或日线级别**。比如基于 15 分钟或 1 小时 K 线走势的策略。这样几秒钟的延迟对最终收益率影响微乎其微。

### **铁律二：绝对不要做“自以为是”的干预（No Discretionary Trading）**

* **陷阱**：系统在 4300 点发出“做空 FCPO”的信号，但你当时看新闻觉得棕榈油还要涨，或者心里害怕，于是你决定“等一等再下”或者“不听系统的”。  
* **后果**：这会彻底毁掉你的 POC 验证。一旦加入人类情绪，你就不是在测试“量化系统”，而是在测试“你的直觉”。**当信号出现时，哪怕你心里再痛苦，也必须像一台冷酷的机器一样立刻下单。**

## **🏗️  POC 系统“铁三角”架构说明**

为了配合你的“人形 API”手动交易模式，这三个模块在代码中应该这样落地：

\[数据模块：K线获取\] ──(喂入)──\> \[算法模块：因子计算\] ──(因子值)──\> \[风控模块：仓位拦截\] ────\> \[屏幕大弹窗：通知人类下单\]

### 比赛信息：

以下是关于Bursa Derivatives Virtual Trading Challenge（虚拟交易挑战赛） 2026 所需要关注的信息

#### **1.本次系统将关注以下这两个期货指数：**

**1.1 FCPO (Crude Palm Oil Futures \- 原棕油期货) \- 高流动性，波动**  
**1.2  FKLI (FTSE Bursa Malaysia KLCI Futures \- 综指期货) \- 备选，当FCPO处于波动时FKLI 可以用来做短线的均值回归（Mean Reversion）**

#### **2.期货交易时间**：

**2.1  FCPO （https://www.bursamalaysia.com/trade/our\_products\_services/derivatives/commodity\_derivatives/crude\_palm\_oil\_futures）**

周一至周五（马来西亚时间）

* 上午交易时段：上午10:30至下午12:30  
* 下午交易时段：下午2:30至下午6:00

星期一至星期四（马来西亚时间）

* 盘后交易时段（T+1）：晚上9点至11点

	2.2  **FKLI（**

**https://www.bursamalaysia.com/trade/our\_products\_services/derivatives/equity\_derivatives/ftse\_bursa\_malaysia\_klci\_futures）**

周一至周五（马来西亚时间）

* 早盘交易时段：上午 8:45 至下午 12:45  
* 下午交易时段：下午2:30至下午5:15

星期一至星期四（马来西亚时间）

* 盘后交易时段（T+1）：晚上9点至凌晨2点30分

### **📋 0\. 全局配置中心 (Global Configuration Center)**

在系统启动前，所有业务模块必须统一读取外部配置文件，实现“代码与参数完全分离”。

* **核心载体**：settings.json。  
* **配置域划分**： \* account\_settings: 初始本金 ($Capital$)、单笔风险容忍度 ($Risk\_{pct}$)、最大总杠杆限制 。 \* strategy\_settings: 因子时间周期 (如 15m, 1h)、回溯窗口大小 (lookback\_period)、触发阈值 。 \* system\_settings: 数据库路径 (sqlite:///bursa\_poc.db)、API 轮询间隔 (15 mins) 。`trading_sessions` 参数。数据模块的定时调度器（Scheduler）必须具备“休市意识”，在午休和周末自动挂起（Sleep），只在合法的交易时段内唤醒   
* （补充）**调度时钟对齐 (Cron Alignment)**：系统的轮询触发点不应是开盘瞬间，而必须对齐到 K 线的**收盘瞬间 \+ 5秒延迟**。即早盘苏醒后，第一次拉取数据的物理时间必须是 `10:45:05`，接下来是 `11:00:05`，依此类推。 

#### **0.1 文件默认格式**

### **📄 统一配置文件：`settings.json`**

JSON  
{  
  "system\_settings": {  
    "environment": "POC\_LOCAL",  
    "database\_path": "sqlite:///bursa\_poc.db",  
    "poll\_interval\_seconds": 900,  
    "log\_level": "INFO"  
  },  
    
  "account\_settings": {  
    "initial\_capital\_rm": 100000.0,  
    "risk\_per\_trade\_pct": 0.01,  
    "max\_position\_lots": 20  
  },  
    
  "strategy\_settings": {  
    "strategy\_id": "POC\_ZSCORE\_FCPO",  
    "lookback\_period": 40,  
    "zscore\_entry\_threshold": 2.0,  
    "zscore\_exit\_threshold": 0.5,  
    "assets": {  
      "FCPO": {  
        "multiplier": 25.0,  
        "tick\_size": 1.0  
      },  
      "FKLI": {  
        "multiplier": 50.0,  
        "tick\_size": 0.5  
      }  
    }  
  },  
    
  "trading\_sessions": {  
    "timezone": "Asia/Kuala\_Lumpur",  
    "ignore\_weekends": true,  
"fcpo\_active\_sessions": \[   
{"start": "10:30", "end": "12:30"},   
{"start": "14:30", "end": "18:00"},   
{"start": "21:00", "end": "23:00"} \]  
      }  
}

### **🛠️ 各模块用途深度解析**

#### **1\. `system_settings` (系统底层配置)**

* **用途**：这是系统的“引擎室”。它不涉及任何金融逻辑，只管程序的运行状态。  
* **参数解析**：  
  * `environment`: 标识当前是本地测试还是云端实盘。  
  * `database_path`: 告诉系统去哪里读写 SQLite 数据库。  
  * `poll_interval_seconds`: 轮询间隔（15分钟 \= 900秒），数据模块（Layer 1）的定时器会读取这个值。  
* **导师视角**：当你未来把系统从本地搬到 VPS 时，你**只需要修改这个模块里的路径**，其他代码一行都不用动。

#### **2\. `account_settings` (账户与风控配置)**

* **用途**：这是风控模块（Risk Sentinel）的“红头文件”，专门用来做数学清洗和算仓位。  
* **参数解析**：  
  * `initial_capital_rm`: 比赛给你的虚拟初始本金。  
  * `risk_per_trade_pct`: 0.01 代表 1%。系统据此计算：如果你亏损，最多只能亏 RM 1,000。  
  * `max_position_lots`: 无论公式算出你应该下多少手，绝对不能超过这个“天花板”硬限制。

#### **3\. `strategy_settings` (策略与标的物理属性配置)**

* **用途**：算法大脑和数学圆整模块的“参数字典”。  
* **参数解析**：  
  * `lookback_period` / `threshold`: 算法大脑初始化时，会提取这两个值去建立 `deque` 的长度和触发条件。  
  * `assets`: 这是遗漏细节三（最小跳动单位）的核心。风控模块在吐出最终限价单时，会去这里查 FCPO 的 `tick_size` 是不是 1.0，并执行数学圆整。

#### **4\. `trading_sessions` (交易时段与休眠配置)**

* **用途**：数据模块的“智能闹钟”，这是解决休市期间抓取“死水数据”的关键。  
* **参数解析**：  
  * `timezone`: 强制系统以吉隆坡时间为准，防止 VPS 服务器在别的国家导致时区错乱。  
  * `fcpo_active_sessions`: 定义了早盘和午盘的时间段。  
* **导师视角**：调度器在每次准备发出 API 请求前，会先获取当前时间，判断是否落在这两个区间内。如果不在，程序直接 `Sleep`，彻底杜绝在午休时算出标准差为 0 的异常因子。

### **📋 1\. 数据模块（数据输入：获取目标 K 线数据）**

在开发和虚拟赛初期，千万不要去写复杂的 WebSocket 实时行情接收器，太容易断线了。

* **最稳妥的 CS 做法**：写一个轻量级的 **定时轮询（Polling）脚本**。  
* **业务逻辑**：如果你的策略是看 15 分钟 K 线，那就让你的系统每 15 分钟触发一次，调用行情 API 或者爬虫，把最新的这一根 K 线数据拉下来，追加到你的本地缓存（内存 Queue 或 SQLite）中。

#### **1.1 数据模块供应商：**

首选：TradingView 衍生数据接口（通过 Python `tvdatafeed`） 

备选：Yahoo Finance API（通过 `yfinance`） 

### **1.2 整个数据获取模块的业务逻辑框架（4层架构）**

在工业级量化流水线中，数据模块通常被设计为一个独立的**四层管道（Pipeline）**。每一层各司其职，保证最终吐给算法模块的数据是绝对干净、对齐的。

#### **Layer 1: 接入层（Ingestion Layer）—— 负责把数据“活着”拉回来**

* **核心组件**：定时调度器（Scheduler）+ API 客户端。  
* **业务逻辑**：每隔 15 分钟（加 5 秒延迟偏移），触发事件，同时向数据源（如 TradingView）发出两个请求：拉取最新的 FCPO 和 ZL 的 K 线。此外如果需要，系统需要而外请求本地MYR于目标期货使用的货币汇率如MYRUSD等等。  
* **容错机制**：如果请求超时（Timeout）或报错，触发 **Retry 机制**（每隔 1 秒重试，最多 3 次）。  
* **拉取美元兑马币汇率（USD/MYR）**。例如在 TradingView 上的 Ticker 可以是 `FX_IDC:USDMYR` 

  ### 

  ### 

  ### 

  ### 

  ### **📋 Layer 1.1: 主力换月清洗机制 (Rollover Adjustment)**

对于期货衍生品，单纯拉取连续合约容易在换月日产生“价格断层”，引发伪信号 。

* **容错防线**：在每日收盘后或数据拉取时，计算两根相邻 K 线的绝对价差。如果价差超过历史正常波动率的 5 倍以上（极大概率是发生了合约换月），系统应当触发\*\*因子缓存重置（Reset State）\*\*或抛出警告，要求暂停交易，直到累积足够的新合约数据。


#### **Layer 2: 清洗与对齐层（Processing & Alignment Layer）—— 负责去伪存真**

* **核心组件**：数据解析器（Parser）+ 对齐引擎。  
* **业务逻辑**：  
  1. **结构标准化**：将不同供应商返回的稀奇古怪的 JSON 统一解析为标准的 `Datatype`（Timestamp, Open, High, Low, Close, Volume）。  
  2. **时间戳本地化**：统一将所有时间戳转化为吉隆坡时间（UTC+8）。  
  3. **多品种正交对齐**：使用 Pandas 的 `merge(how='left', on='timestamp')`，以 FCPO（主交易标的）的时间戳为基准。如果某时刻 ZL 已经休市（返回 NaN），则使用 `fillna(method='ffill')` 沿用 ZL 休市前的最后一口价，因为此时内盘价格的物理状态就是“停滞”的 

####  **Layer 2.2: 清洗与对齐层（Processing & Alignment Layer）—— 新增“单位统一化”**

* **改动点**：在完成原有的“时间戳本地化”和“多品种正交对齐”之后，必须强制加入一道单位统一化（Unit Normalization）工序。  
* **业务逻辑**：利用对齐好的那一刻的实时汇率，将大连豆油（ZL）的价格转化为马币（MYR）。  
  * （金融常识 \+ 完整对齐公式”为准 ）  
* **注意**：如果有必要，这一步还要处理**合约乘数**对齐。比如计算每吨的绝对差价，确保两者在同一物理重量维度上比较。  
* **金融常识**：1 公吨 (Metric Ton) ≈ 2204.62 磅 (Pounds)。  
* **完整对齐公式（请务必加入你的代码中）**：  
  $$Price\_{ZL\\\_USD\\\_Per\\\_Ton} \= (Price\_{ZL\\\_Raw} \\div 100\) \\times 2204.62$$  
  *(解释：除以 100 是把美分换成美元，乘以 2204.62 是把磅换成公吨)*

**最终你的大统一算式应该是：**

$$Price\_{ZL\\\_MYR\\\_Per\\\_Ton} \= Price\_{ZL\\\_USD\\\_Per\\\_Ton} \\times Rate\_{USD/MYR}$$

$$Spread \= Price\_{FCPO} \- Price\_{ZL\\\_MYR\\\_Per\\\_Ton}$$

#### **Layer 3: 持久化存储层（Persistence Layer）—— 负责留存证据与备查**

* **核心组件**：SQLite 数据库写入器。  
* **业务逻辑**：将对齐好的标准数据通过 `INSERT OR IGNORE` 写入本地 SQLite 数据库。这一步不仅是为了防止关机，更是为了后续你的系统重新上线时，能直接从本地库里秒级读取历史数据，不再重复请求外网 API。

### 

### 

#### **Layer 4: 分发与触发层（Dispatcher / Trigger Layer）—— 负责叫醒大脑**

* **核心组件**：事件通知器（Event Notifier）。  
* **业务逻辑**：一旦对齐的数据成功落库，该层就会向主程序发送一个通知事件（信号），并将这条最新的、干净的对齐数据塞入内存的滑动窗口（`deque`）中。随后，**算法模块被正式唤醒**。

### 

### **1.3 SQL 数据表**

#### **📊 1\. 基础行情表：market\_data\_15m**

这张表用于接收 Layer 1 抓取回来的各品种原始/标准化 K 线数据。我们采用“单表复合品种”的设计，而不是一个品种建一张表，这样更符合 CS 的规范，便于扩展新标的。

**📋 表结构设计**

| 字段名 (Column) | 类型 (Type) | 约束 (Constraint) | 说明 (Comment) |
| :---- | :---- | :---- | :---- |
| symbol | TEXT | PRIMARY KEY (组合) | 交易标的代码 (如 MYX:FCPO1\!, DCE:ZL1\!) |
| datetime | TEXT | PRIMARY KEY (组合) | K 线时间戳，统一使用吉隆坡时间 (YYYY-MM-DD HH:MM:SS) |
| open | REAL | NOT NULL | 开盘价 |
| high | REAL | NOT NULL | 最高价 |
| low | REAL | NOT NULL | 最低价 |
| close | REAL | NOT NULL | 收盘价（因子计算核心） |
| volume | INTEGER | NOT NULL | 成交量 |
| updated\_at | TEXT | DEFAULT CURRENT\_TIMESTAMP | 本地入库/更新时间（用于 Debug） |

**🔨 DDL 语句**  
SQL

CREATE TABLE IF NOT EXISTS market\_data\_15m (

    symbol TEXT NOT NULL,

    datetime TEXT NOT NULL,

    open REAL NOT NULL,

    high REAL NOT NULL,

    low REAL NOT NULL,

    close REAL NOT NULL,

    volume INTEGER NOT NULL,

    updated\_at TEXT DEFAULT CURRENT\_TIMESTAMP,

    PRIMARY KEY (symbol, datetime) \-- 核心：通过组合主键实现幂等性，防止重复数据插入

);

\-- 为时间戳建立独立索引，极大提升断线追赶时的 MAX(datetime) 查询速度

CREATE INDEX IF NOT EXISTS idx\_md\_datetime ON market\_data\_15m (datetime);

#### 📊 **2\. 品种对齐与因子输出表：aligned\_factor\_stream**

这是在 Layer 2 完成时间戳对齐后，由 Layer 3 落地存储的**宽表**。它直接把 FCPO 和 豆油（ZL）在同一时刻的价格并排拼好，并顺便存下当时算出来的因子值。

* **为什么要建这张表？**：当你双击重启系统时，程序可以直接通过这张表**秒级秒出**最近 40 期的历史因子，瞬间完成算法预热，完全不需要在内存里重新做复杂的 SQL JOIN。

**📋 表结构设计**

| 字段名 (Column) | 类型 (Type) | 约束 (Constraint) | 说明 (Comment) |
| :---- | :---- | :---- | :---- |
| datetime | TEXT | PRIMARY KEY | 严格对齐后的时间戳 |
| fcpo\_close | REAL | NOT NULL | 大马棕榈油收盘价 **(计价: MYR)** |
| **zl\_close\_USD** | REAL | NOT NULL | **\[修改\]** 大连豆油原始收盘价 **(计价: USD)** |
| **fx\_rate** | REAL | NOT NULL | **\[新增\]** 当时对齐的人民币兑马币实时汇率 |
| **zl\_close\_myr** | REAL | NOT NULL | **\[新增\]** 折算后的豆油价格 (zl\_close\_USD \* fx\_rate) **(计价: MYR)** |
| spread | REAL | NOT NULL | 统一货币后的纯净价差 (fcpo\_close \- zl\_close\_myr) |
| factor\_score | REAL | DEFAULT NULL | Z-Score 因子值 |
| signal | INTEGER | DEFAULT 0 | 逻辑信号 (1, \-1, 0) |

🔨 升级后的 DDL 语句

SQL

CREATE TABLE IF NOT EXISTS aligned\_factor\_stream (

    datetime TEXT PRIMARY KEY, 

    fcpo\_close REAL NOT NULL,

    zl\_close\_USD REAL NOT NULL,     \-- 明确原始货币

    fx\_rate REAL NOT NULL,          \-- 留存汇率证据

    zl\_close\_myr REAL NOT NULL,     \-- 预计算好的统一货币价格

    spread REAL NOT NULL,

    factor\_score REAL,

    signal INTEGER DEFAULT 0

);

#### 

#### 

#### 

#### 

#### 

#### 

#### 

#### 

#### 

#### **📊 3\. 数据同步元数据表：data\_sync\_metadata**

这张表是解决“断线缺失数据如何追赶”的灵魂。它不存行情，只记录每个品种的健康状况和同步进度。

**📋 表结构设计**

| 字段名 (Column) | 类型 (Type) | 约束 (Constraint) | 说明 (Comment) |
| :---- | :---- | :---- | :---- |
| symbol | TEXT | PRIMARY KEY | 交易标的代码 |
| last\_sync\_time | TEXT | NOT NULL | 上一次成功完整同步的最新 K 线时间 |
| is\_active | INTEGER | DEFAULT 1 | 该标的是否还在监控中 (1: 激活, 0: 挂起) |

**🔨 DDL 语句**  
SQL

CREATE TABLE IF NOT EXISTS data\_sync\_metadata (

    symbol TEXT PRIMARY KEY,

    last\_sync\_time TEXT NOT NULL,

    is\_active INTEGER DEFAULT 1

);

 

#### **📊4..“仓位记忆”数据表（Portfolio State Persistence）**

**📋 表结构设计**

| 字段名 (Column) | 类型 (Type) | 约束 (Constraint) | 说明 (Comment) |
| :---- | :---- | :---- | :---- |
| strategy\_id  | TEXT | PRIMARY KEY | 仓位代码 |
| current\_capital  | REAL  | NOT NULL | 当前的账户资金 |
| position\_direction  | INTEGER | DEFAULT 0 | 持仓方向 (1: 多, \-1: 空, 0: 空仓)  |
| position\_lots  | INTEGER | NOT NULL | 持仓手数 (绝对值)  |
| average\_entry\_price  | REAL  |  | 记录开仓成本价  |
| last\_updated  | TEXT  | DEFAULT CURRENT\_TIMESTAMP  | 最后一次更新时间  |

 

**🔨 DDL 语句**

SQL

CREATE TABLE IF NOT EXISTS portfolio\_state (

    strategy\_id TEXT PRIMARY KEY,    \-- 策略唯一标识，例如 'POC\_ZSCORE\_FCPO'

    current\_capital REAL NOT NULL,   \-- 当前账户总资金 (RM)

    position\_direction INTEGER,      \-- 持仓方向 (1: 多, \-1: 空, 0: 空仓)

    position\_lots INTEGER,           \-- 持仓手数 (绝对值)

    average\_entry\_price REAL,    \-- 记录开仓成本价！ 

    last\_updated TEXT DEFAULT CURRENT\_TIMESTAMP \-- 最后一次更新时间

);

#### 

#### 

#### 

#### 

#### 

#### **📊4.1.“仓位记忆”数据表（Portfolio State Persistence）(补充）**

####  **执行逻辑与关系放置位置**

这个表的读写逻辑需要横跨两个不同的生命周期阶段：

* **动作 A：系统启动时的“恢复记忆”（读取）**  
  * **放在哪里**：放在系统的主程序 `QuantOrchestrator` 的**初始化阶段（Bootstrapping）**。在启动 15 分钟定时轮询之前执行。  
  * **业务逻辑**：系统启动时，执行 `SELECT * FROM portfolio_state WHERE strategy_id = 'POC_ZSCORE_FCPO'`。如果查到数据，直接把这三个值赋给风控模块内存里的 `self.capital`, `self.pos_dir`, `self.pos_lots`；如果查不到数据（第一次运行），则初始化为配置中心的默认本金和 0 仓位，并执行一次 `INSERT` 建立初始行。  
* **动作 B：交易完成后的“状态快照”（写入）**  
  * **放在哪里**：放在 **风控模块 Layer 5（状态回写与人工反馈注入）**。  
  * **业务逻辑**：当系统在屏幕上弹出指令，而你手动在网页上下单完毕，并在控制台敲下“回车”输入实际成交价和手数后。系统立刻计算出最新的剩余资金和仓位，并执行一句更新指令，将内存状态持久化到硬盘：

SQL

UPDATE portfolio\_state 

SET current\_capital \= ?, position\_direction \= ?, position\_lots \= ?, average\_entry\_price \= ?, last\_updated \= CURRENT\_TIMESTAMP 

WHERE strategy\_id \= 'POC\_ZSCORE\_FCPO'

### **📋 2\. 算法模块（大脑中枢：计算植入的表达式因子）**

这就是你最核心的资产，也就是你那张 HTML 研报里展示的逻辑。

* **业务逻辑**：数据模块每追加一根新 K 线，算法模块就被唤醒一次。它把内存中最近 40 根 K 线（根据你之前的参数设定）提取出来，扔进你的因子表达式类里。  
* **输出结果**：计算出一个当下的**因子得分（Factor Score）**。

### **2.1🧠 算法模块的四层内部逻辑流 (Internal Logic Flow)** 

### **Layer 1: 动态状态维护层 (Dynamic State Management)**

算法模块不再是一个只有固定记忆的笨蛋，而是一个能够根据“挂载的因子插件”自动分配内存池的智能容器。

* **业务动作**：当 update\_data() 被调用时，将最新 K 线的 close 压入队列。  
* **CS 实现要点**：不再写死 40，而是使用 collections.deque(maxlen=self.lookback\_period)。这个 lookback\_period 由因子在初始化（\_\_init\_\_）时向系统动态申报。  
* **边界条件防御**：调用 is\_ready() 方法。如果当前队列长度小于声明的 lookback\_period，直接触发 return None（观望），阻断后续计算，保护系统不报索引错误。

### **Layer 2: 多态计算引擎 (Polymorphic Computation Engine)**

这里不再只存放某一个特定的数学公式，而是提供了一个名为 compute() 的**抽象接口（Abstract Method）**。具体的数学逻辑被下放到各个子类中。

* **业务动作**：提取动态窗口内的数据，执行具体的矩阵/向量运算。  
* **以 Z-Score 套利子类为例的实现**：  
  子类内部重写 compute()，计算实时价差（Spread），均值（$\\mu$）和标准差（$\\sigma$）。  
  $$Z \= \\frac{Spread\_{current} \- \\mu\_{window}}{\\sigma\_{window}}$$  
* **异常防御（熔断机制）**：无论子类写什么逻辑，必须防御除以零的致命 Bug。如果连续横盘导致 $\\sigma\_{window} \= 0$，系统强制返回极小值或直接抛出自定义的 ZeroVarianceException 并捕获它，输出安全信号 0。

### **Layer 3: 参数化信号离散层 (Parameterized Signal Discretization)**

绝对不允许在代码里出现 2.0 或 \-0.5 这种“魔法数字（Magic Numbers）”。所有的阈值都必须是因子的**实例属性（Instance Attributes）**。

* **业务动作**：通过比对计算出的连续值与实例属性阈值，将得分映射为离散的交易信号（1, \-1, 0）。  
* **逻辑映射示例（动态化）**：  
  * 如果 Z-Score \> self.upper\_entry\_threshold $\\rightarrow$ 信号设为 \-1 (做空指令)  
  * 如果 Z-Score \< self.lower\_entry\_threshold $\\rightarrow$ 信号设为 1 (做多指令)  
  * 如果 abs(Z-Score) \< self.exit\_threshold $\\rightarrow$ 信号设为 0 (平仓指令)

### 

### **Layer 4: 标准化协议分发 (Standardized Protocol Routing)**

大脑算完了，绝不能随便丢一个 Tuple 或者零散的变量出去。我们需要定义一个**严格的数据契约（Data Contract）**。

* **业务动作**：将结果封装为强类型的对象（推荐使用 Python 的 @dataclass 或者 TypedDict），比如 SignalPayload，然后向外抛出。  
* **Payload 包含的标准结构**：  
  * timestamp: K 线的时间戳（用于对齐验证）  
  * factor\_name: 因子名称（如 "ZScore\_FCPO\_ZL"）  
  * raw\_score: 原始因子得分（如 2.45，用于存入 SQLite 供未来复盘）  
  * action\_signal: 离散信号（\-1, 1, 0，发送给风控模块）  
  * `current_price`: 当前触发信号时的标的价格（用于风控校验保证金和打印限价单）。   
  * `volatility_metric`: 当前的波动率或止损距离点数（例如当前的 ATR 值）。   
* **去向**：这个 SignalPayload 就像流水线上的标准集装箱，稳稳地顺着管道流向本地数据库（归档）和**风控模块（审核）**。

### **📋 3\. 风控模块（最后防线：仓位风控业务）**

这是你研报里做的最棒的部分（Risk Sentinel）。即使是人工下单，风控也必须由系统算死，绝不能靠人类肉眼去估算。

* **业务逻辑**：  
  1. **信号翻译**：根据因子得分，判定是要买、要卖、还是不动。  
  2. **动态算仓**：读取你当前的比赛账户总资金，根据止损距离，计算出**这一次你应该手动下一手还是三手**。  
  3. **规则拦截**：检查这笔交易是否超过了最大持仓限制（比如 20 手限制），或者是否满足硬止损标准。  
* **终点输出**：如果在本地通过了所有合规检查，系统直接在屏幕上弹出大字（或者发送消息到你的手机）：“**【风控通过】请立即在比赛软件上手动买入 FCPO 2手，限价：4250**”。

## 

## 

#### 

#### **🛡️ 风控模块的四层核心业务逻辑 (Risk Sentinel Logic Flow)**

### **📋 Layer 1: 仓位状态感知与防呆拦截 (State Awareness & Anti-Fool)**

算法大脑是“无状态”的，它只知道“现在价差很大，该做空”。但风控模块必须知道“我们现在的实际仓位是什么”。

* **业务逻辑**：对比 SignalPayload.action\_signal 与本地记录的 current\_position。  
* **动作示例**：  
  * **防呆拦截**：如果大脑发出 1（买入信号），但此时系统记录我们**已经持有该方向的满仓**，风控模块直接拦截指令，返回 观望，防止系统在同一个价位重复发单导致爆仓。  
  * **反向平仓**：如果大脑发出 \-1（做空信号），但当前我们持有 \+2 手多单。风控模块会将其拆解为两个动作：先平掉 2 手多单（归零），再反手开空。

### 

### **📋 Layer 2: 动态算仓引擎 (Dynamic Position Sizing) —— 最核心的数学！**

这是专业量化与散户的分水岭。散户每次都凭感觉下 1 手或 2 手，而你的系统必须根据**账户余额**和**当前市场波动率**自动算出精确的手数。

假设你在虚拟比赛中的本金是 $Capital$，你设定的单笔最大亏损比例是 $Risk\_{pct}$（比如 1%）。

以大马棕榈油（FCPO）为例，FCPO 每跳动 1 个点，盈亏是 RM 25（这就是**合约乘数 $Multiplier$**）。

* **业务逻辑**：系统需要知道你的**止损距离（Stop Loss Distance）**（例如，通过 14 周期的 ATR 算出止损放在 40 个点之外）。然后通过公式算出绝对手数：  
  $$Max\\\_Loss\\\_Amount \= Capital \\times Risk\_{pct}$$  
  $$Risk\\\_Per\\\_Lot \= Stop\\\_Loss\\\_Distance \\times Multiplier$$  
  $$Target\\\_Lots \= \\lfloor \\frac{Max\\\_Loss\\\_Amount}{Risk\\\_Per\\\_Lot} \\rfloor$$  
* **例子**：RM 100,000 本金，1% 风险 \= 允许亏 RM 1,000。  
  止损设在 40 个点外，每手亏损风险 \= 40 \* 25 \= RM 1,000。  
  最终计算出：本单只能下 **1 手**。向下取整（Floor）是为了绝对不超额。

### **📋 Layer 3: 合规与硬性拦截网 (Compliance & Hard Interception)**

算出了理想的 Target\_Lots 后，还不能直接发单，必须经过最后一道安全检查。

* **业务逻辑**：  
  1. **最大持仓限制（Max Position Limit）**：比如比赛规定单品种不能超过 20 手，如果算出来是 22 手，风控强行截断（Truncate）为 20 手。  
  2. **可用保证金检查（Margin Check）**：计算这笔订单需要的保证金，如果超过了账户的可用余额（Free Margin），直接驳回。  
  3. **流动性/黑天鹅熔断（Circuit Breaker）**：如果发现当日行情已经跌停（Limit Down），风控模块会拒绝发出市价买单，防止被闷杀。

（补充）**Layer 3: 合规与硬性拦截网**  
：**换月强制清仓 (Forced Rollover Liquidation)**：在期货主力合约即将换月的前 1-2 个交易日（或者检测到跳空换月当天），风控系统必须拒绝任何新建仓指令，并强制抛出信号，要求人类手动平掉所有底层持仓，实现“空仓跨月”。

 

### **📋 Layer 4: 终点路由与人类交互 (Output Routing & Human Interface)**

既然你是“人形 API”，那么最后一步就是将冷冰冰的计算结果，转化为你能瞬间读懂并执行的操作指南。

* **业务逻辑**：触发系统的报警机制（声音/控制台高亮提示）。  
* **控制台输出示例**：  
* Plaintext

\======================================================

🚨🚨🚨 【风控系统：绿灯通过】 交易指令下达 🚨🚨🚨

\======================================================

\[时间\] 2026-05-31 10:45:00

\[标的\] FCPO (大马棕榈油)

\[动作\] BUY LONG (做多)

\[手数\] 2 手

\[限价\] RM 4,250 

\[止损\] RM 4,210 (硬性止损！务必在软件中同时挂单！)

\------------------------------------------------------

请立即前往 Bursa 虚拟交易软件执行上述操作！执行后请按回车键确认...

### **📋 Layer 5: 状态回写与人工反馈注入 (State Sync & Feedback Loop)**

由于脱离了真实交易所的 API 回调（Callback），系统处于“盲飞”状态，必须由人类在完成交易后，将实际结果手动注入回系统闭环中。 \* **业务逻辑**：在弹出指令并等待人类执行后（Layer 4），系统主线程挂起（Pause），等待人类在控制台输入实际的成交数据 。

* **输入要求**：实际成交方向、实际成交手数、实际成交均价。  
* **状态刷新**：系统接收到人类反馈后，更新本地的 current\_position 和 account\_balance 变量，并写入 SQLite 交易日志表，为下一次的风控拦截提供准确的持仓基准。

### **补充：**

在风控模块 Layer 2 计算手数，以及 Layer 4 终点输出限价单和止损价时 ，纯数学计算可能会得出类似 `4250.37` 这样的价格。如果你拿着这个价格去 Bursa 软件里下单，会被直接报错。因为 FCPO 的最小变动价位是 1 个点（即必须是整数），而 FKLI 的最小变动价位是 0.5 个点 。   
 **数学清洗的公式与放置位置**

* **放在哪里**：这个圆整逻辑必须放在 **风控模块 Layer 4（终点路由与人类交互）**，也就是在 `Target_Lots`（目标手数）和 `Stop_Loss_Price`（止损价）算出之后，**但在控制台 `print` 打印出来给你看之前**。  
* **核心逻辑代码实现**： 你需要用配置里的 `tick_size` 对原始计算价格进行对齐处理。  
* Python

\# 假设从配置中读取到了 FCPO 的 tick\_size

tick\_size \= 1.0 

\# 假设算法结合波动率算出的原始止损价是一个带长小数的浮点数

raw\_stop\_loss \= 4250.3784


\# 【核心数学清洗公式】

clean\_stop\_loss \= round(raw\_stop\_loss / tick\_size) \* tick\_size

\# 打印给人类看的结果

print(f"\[止损\] RM {clean\_stop\_loss}") \# 结果会完美变成 4250.0

*   
  * *原理解析*：如果 `tick_size` 是 `0.5` (FKLI)，原价是 `1600.8`。`1600.8 / 0.5 = 3201.6`。`round(3201.6)` 变成 `3202`。`3202 * 0.5 = 1601.0`。完美卡在了交易所允许的档位上！