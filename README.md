# McSmart 麦智点餐官

> **基于麦当劳 MCP Server 的 AI Agent Skill** — 用自然语言完成"懂营养、懂优惠、懂你"的智能点餐
>
> CSDN BuilderX 麦当劳 MCP 创新挑战赛参赛作品

---

## 1. 项目介绍

McSmart（麦智点餐官）是一个 **AI Agent Skill**：它不是又一个点餐页面，而是一个能**听懂自然语言、自主规划工具调用、综合多方数据给出决策建议**的智能体技能。

用户只需说出——

> "我最近减肥，但是想吃麦当劳，预算30元，帮我推荐一下"

McSmart 就会自动完成：**意图识别 → MCP 工具规划 → 麦当劳 MCP 真实调用 → 数据融合 → 综合评分 → 推荐方案**，全程无需用户查菜单、算热量、找优惠券。

项目同时包含 **Skill 定义层**（`skill/SKILL.md` + 触发词 + 营养规则参考）与 **代码实现层**（`src/`），评委可同时审视 Prompt 工程与代码工程。

### 三种工作模式（实测全通）

| 模式 | 触发示例 | 调用工具数 | 输出 |
|------|----------|-----------|------|
| **健康推荐** | "500卡以内，30元以内的套餐" | 6 | 营养/价格/优惠三维评分 Top 3 方案 |
| **省钱优化** | "今天吃麦当劳怎么最划算" | 6-7 | 原价 / 优惠金额 / 最终价格 / 节省比例 |
| **门店查询** | "附近哪里可以买" | 1 | 距离 / 营业状态 / 推荐理由 |

健康推荐支持 **7 种目标**：减脂 · 增肌 · 儿童 · 低糖 · 低脂 · 省钱 · 普通。

---

## 2. 背景

- **点餐决策过载**：麦当劳 SKU 数百个，用户面对"想吃得健康/划算"的诉求时，需要自己查热量表、比价、凑优惠券，决策成本高。
- **营养信息不对称**：官方营养数据（160+ 餐品的能量/蛋白/脂肪/碳水/钠）存在但没有被"翻译"成普通用户可执行的点餐建议。
- **优惠规则复杂**：平台券、门店券、促销活动分散在不同入口，"怎么买最划算"需要逐个试算。
- **MCP 让数据可用**：麦当劳官方开放 MCP Server（`mcd-mcp v1.0.0`，35 个工具），让 AI Agent 第一次能以标准化协议实时触达菜单、营养、优惠、门店、价格与交易数据——但**工具是原子化的，价值在编排**。

McSmart 的定位就是这最后一环：把 35 个原子工具，编排成一个"说一句话就办好一顿饭"的智能技能。

---

## 3. 产品价值

### 对用户：从"人找餐"到"目标驱动点餐"

| 用户说 | McSmart 做 | 实测效果 |
|--------|-----------|----------|
| "500卡以内，30元以内" | 营养表×在售餐品交叉筛选 + 券后试算 | 培根蔬萃双层牛堡 442kcal / ¥24.50 |
| "怎么最划算" | 我的券+门店券+活动全量试算排序 | 麦咖啡美式 ¥19 → **¥9.90（省 47.9%）** |
| "控糖" | 碳水 ≤50g 硬过滤 | 218kcal / 碳水 8g 三件组合 |
| "附近哪里可以买" | 单工具轻量查询 | 29米 · 营业中 · "步行即可到达" |

### 对麦当劳生态：数据的价值放大器

- 营养数据从"官网查询页"变成**驱动消费决策的实时过滤器**，健康人群点餐转化提升
- 优惠券从"入口曝光"变成**逐组合精确试算**，核销率提升（Agent 主动匹配最优券）
- 门店/预约/时段数据被组织成**可解释的推荐理由**，引导错峰到店

### 对 MCP 生态：可复用的 Agent 编排范式

- **"LLM 只在两端"**架构：意图理解与话术润色用 LLM，中间的工具规划、数据融合、评分全部是确定性代码——**Demo 可复现、报价零幻觉、失败可降级**
- **报价零幻觉**：所有价格数字只来自 `calculate-price` 实时返回，LLM 不接触数字运算——这是 AI 点餐场景可信度的底线设计
- **高危操作硬门控**：`create-order` 等工具在代码层确认门之后，LLM 无权绕过
- 该范式（意图→DAG 规划→并行调用→融合评分）可直接迁移到奶茶、咖啡、商超等任意 MCP 化的零售场景

---

## 4. 技术架构

### 五段式流水线

```
用户自然语言
   │
   ▼
① 意图识别 (agent/orchestrator)     规则引擎版，100% 可复现，预留 LLM 适配器
   │  输出 IntentProfile: goal / maxCalories / budget / city / dining_mode
   ▼
② 工具规划 (agent/planner)          意图 → 三波次调用 DAG（模板为主，LLM 仅兜底）
   │
   ▼
③ MCP 执行 (services/* + mcp/client)  波次内可并行；超时/重试/降级
   │
   ▼
④ 数据融合 (services/recommend)     营养表×餐品交叉 → 组合生成 → 批量试算
   │
   ▼
⑤ 建议生成 (models/schemas.render)  综合评分排序 → Top N 方案卡片
```

### 核心设计决策

| 决策 | 理由 |
|------|------|
| **LLM 只在两端** | 中段规划/融合全部确定性代码：Demo 现场可复现，价格不幻觉 |
| **三波次并行 DAG** | Wave1 门店‖营养 → Wave2 餐品‖券‖促销 → Wave3 批量试算，端到端 3-5s |
| **11 态会话状态机** | 含 CLARIFYING 追问态与 AWAITING_CONFIRMATION 硬确认门 |
| **推荐分三维合成** | `0.4×营养匹配度 + 0.3×价格优惠度 + 0.3×用户目标匹配度`，分项可解释 |
| **组合层统一 caps** | 组合合计校验热量/碳水/脂肪/钠/预算五维（实测修复过组合超限 bug） |

### 代码结构

```
McSmart/
├── README.md                  # 本文件
├── CONTEST_DECLARATION.md     # 比赛原创性声明
├── MCP_INTEGRATION.md         # MCP 集成与工具编排完整说明
├── workbuddy.md               # WorkBuddy AI 结对开发全记录
├── skill/                     # ★ Skill 定义层
│   ├── SKILL.md               #   触发词 / 能力边界 / 编排策略
│   └── references/            #   工具手册 / 营养规则 / 点餐剧本
├── src/
│   ├── main.py                # 三模式分流 + 五段流水线编排
│   ├── agent/                 # orchestrator(意图) + planner(DAG)
│   ├── mcp/                   # client(HTTP/重试/JSON提取) + tools(注册表)
│   ├── services/              # 六大能力 + deal_optimizer + recommend
│   ├── models/schemas.py      # Pydantic 领域模型 + 渲染卡片
│   └── utils/
├── tests/test_services.py     # 28 项离线单测
├── docs/                      # 架构 / Agent 工作流 / Demo 脚本
└── demo/                      # 演示截图与物料
```

### 技术栈

- **Python 3.13** · **Pydantic v2**（MCP 响应强校验，防脏数据）· **httpx**（异步 HTTP）· **pytest**

---

## 5. MCP 能力使用说明

### 接入的 MCP Server

| 项 | 值 |
|----|-----|
| Server | 麦当劳官方 `mcd-mcp v1.0.0`（35 个工具） |
| Endpoint | `https://mcp.mcd.cn` |
| 协议 | MCP `2025-03-26`，Streamable HTTP（无状态会话） |
| 鉴权 | Bearer Token（凭证存放于 `.env`，不入库） |

### 本项目使用的 10 个工具

客户端统一使用 `mcd-mcp_*` 别名，`src/mcp/tools.py` 内部映射到 Server 真实工具名。

| 能力域 | 别名 → 真实工具名 | 用途 |
|--------|-------------------|------|
| 食品查询 | `mcd-mcp_query-meals` → `query-meals` | 门店在售餐品 |
| 营养分析 | `mcd-mcp_query-nutrition-foods` → `list-nutrition-foods` ⚠️ | 160+ 餐品营养成分 |
| 优惠发现 | `mcd-mcp_available-coupons` → `available-coupons` | 可领券提醒 |
| 优惠发现 | `mcd-mcp_query-promotions` → `query-promotions` | 门店促销活动 |
| 价格计算 | `mcd-mcp_calculate-price` → `calculate-price` | **价格真值唯一来源** |
| 门店推荐 | `mcd-mcp_query-nearby-stores` → `query-nearby-stores` | 距离/营业状态/时段 |
| 智能点餐 | `mcd-mcp_create-order` → `create-order` | 下单（硬确认门后） |
| 优惠优化 | `mcd-mcp_query-my-coupons` → `query-my-coupons` | 我的券 |
| 优惠优化 | `mcd-mcp_query-store-coupons` → `query-store-coupons` | 门店券（带券 ID） |
| 优惠优化 | `mcd-mcp_auto-bind-coupons` → `auto-bind-coupons` | 自动领券 |

> ⚠️ 勘误：官方工具清单中不存在 `query-nutrition-foods`，真实名称为 `list-nutrition-foods`。

### 编排策略（详见 MCP_INTEGRATION.md）

- **健康推荐**：门店‖营养 → 餐品‖券‖促销 → 批量试算 → 三维评分
- **省钱优化**：我的券（空则自动领）‖门店 → 门店券×适用商品试算 + 组合试算 + 无券基线 → 按节省比例排序
- **门店查询**：单工具轻量链路，秒回
- **通用参数约定**：`beType` 1/2/5/6、`orderType` 1/2、金额单位**分**、用券需 `couponId`+`couponCode` 成对传
- **定位约定**：官方门店工具**不接受经纬度**，「位置」由服务端按账号判定；客户端靠 `city`（+`keyword`）收窄，城市由自然语言解析，未提及时用 `MCSMART_DEFAULT_CITY` 兜底并**显式标注**（不静默猜城市）
- **降级策略**：营养失败用缓存并标注；无券按原价试算；门店失败（根依赖）终止链路

---

## 6. 安装运行方式

### 环境要求

- Python ≥ 3.13

### 三步运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 MCP 凭证（不入库）
cp .env.example .env
#    编辑 .env: MCP_SERVER_URL / MCP_AUTH_TOKEN
#    可选: MCSMART_DEFAULT_CITY（未在需求中提及城市时使用，默认「北京」）

# 3. 运行（真实 MCP 全链路）
python -m src.main "帮我推荐一个500卡以内，30元以内的麦当劳套餐"
```

### 运行单元测试（离线，不访问 MCP Server）

```bash
python -m pytest tests/ -v        # 29 项测试
```

### 更多用法

```bash
python -m src.main "我最近减肥，预算30元，在上海"          # 健康推荐（城市槽位）
python -m src.main "今天吃麦当劳怎么最划算"                 # 省钱优化模式
python -m src.main "附近哪里可以买"                        # 门店查询模式
python -m src.main "增肌期练完，要高蛋白的麦当劳"           # 增肌目标
python -m src.main "我在控糖，推荐个低糖的麦当劳晚餐"       # 低糖目标
```

不带参数运行时使用默认示例输入。

---

## 7. 使用案例

### 案例 1：健康推荐（减脂 + 预算约束）

```
$ python -m src.main "帮我推荐一个500卡以内，30元以内的麦当劳套餐"

[Agent] 意图: {"goal": "减脂", "maxCalories": 500, "budget": 30}
[Agent] 门店: 麦当劳北京中粮广场餐厅 (北京, 共5家)
[Agent] 营养数据: 160 条餐品 | 在售餐品: 129 个 | 可领优惠: 6 张

已为「减脂」目标生成 1 套方案:
【麦智方案 1】轻食单品 · 综合推荐分 79.63
  组合: 培根蔬萃双层牛堡
  热量 442kcal | 蛋白质 24.0g | 脂肪 23.0g | 碳水 34.0g | 钠 728mg
  价格: ¥24.50（实时试算）| 可领优惠 6 张
  门店: 麦当劳北京中粮广场餐厅
```

### 案例 2：省钱优化（优惠券全量试算）

```
$ python -m src.main "今天吃麦当劳怎么最划算"

已为你算出最划算的吃法（价格均为实时试算）:
  我的券: 6张 | 门店活动: 3 条进行中

【省钱方案 1】用券吃「麦咖啡™美式」 · 节省比例 47.9%
  原价: ¥19.00 | 优惠金额: ¥9.10 | 最终价格: ¥9.90
  用券: 9.9元中杯冰美式（2026-10-10 ~ 10-16）

【省钱方案 2】超值组合「麦咖啡™美式 + 学生专享饮品随心选」 · 节省比例 31.4%
  原价: ¥29.00 | 优惠金额: ¥9.10 | 最终价格: ¥19.90
```

### 案例 3：门店查询（轻量秒回）

```
$ python -m src.main "附近哪里可以买"

「北京」附近有 5 家麦当劳可点餐，为你推荐:
【门店 1】麦当劳北京中粮广场餐厅
  距离: 29米 | 营业状态: 营业中 ✅（07:00-22:00）
  地址: 建国门内大街8号北京中粮广场C座G138-141
  推荐理由: 距离最近，步行即可到达，支持预约取餐
```

### 案例 4：低糖/低脂细分目标

```
$ python -m src.main "我在控糖，推荐个低糖的麦当劳晚餐"

【麦智方案 1】低糖单品
  组合: 薄皮焦香V翅 + 鲜萃咖啡 + 浓缩咖啡
  热量 218kcal | 碳水 8.0g | 钠 597mg
```

> 以上输出均为真实 MCP Server 调用结果（2026-10-10 实测，北京/上海双城市验证），
> 完整开发与调试过程见 `workbuddy.md`。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [MCP_INTEGRATION.md](MCP_INTEGRATION.md) | MCP 工具清单、参数约定、三种模式调用流程、创新价值 |
| [CONTEST_DECLARATION.md](CONTEST_DECLARATION.md) | 比赛原创性声明 |
| [workbuddy.md](workbuddy.md) | WorkBuddy AI 结对开发全记录 |
| [docs/agent-workflow.md](docs/agent-workflow.md) | Agent 架构 / 工具 DAG / 状态机 / Prompt 设计 |
| [docs/demo-guide.md](docs/demo-guide.md) | Demo 演示场景脚本 |
| [skill/SKILL.md](skill/SKILL.md) | Skill 定义（触发词 / 能力边界） |
