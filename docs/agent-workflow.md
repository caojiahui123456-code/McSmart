# McSmart Agent 核心工作流设计

> 阶段: Phase 1 架构设计 | 日期: 2026-10-10
> 示例输入: "我最近减肥，但是想吃麦当劳，预算30元，帮我推荐一下"

## 1. Agent 总体架构（五段式流水线）

```
用户自然语言
    │
    ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ ① Intent    │→ │ ② Planner   │→ │ ③ Executor  │→ │ ④ Aggregator│→ │ ⑤ Presenter │
│  意图识别    │   │  工具规划    │   │  MCP 执行    │   │  结果聚合    │   │  建议生成    │
│  (LLM)      │   │  (LLM+规则) │   │  (并行DAG)   │   │  (规则+打分) │   │  (模板+LLM) │
└─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
       │                                                      ▲
       │ 信息不足 → 追问（CLARIFYING）                          │
       └──────────────────────────────────────────────────────┘
                          用户确认 → ⑥ OrderManager（下单门控）
```

| 阶段 | 模块 | 实现位置 | 技术 |
|------|------|----------|------|
| ① 意图识别 | IntentRecognizer | `src/agent/orchestrator.py` | LLM + JSON 结构化输出 |
| ② 工具规划 | Planner | `src/agent/planner.py` | 意图→DAG 模板（规则为主，LLM 兜底） |
| ③ MCP 执行 | Executor | `src/mcp/client.py` + `tools.py` | asyncio 并行，5s 超时，重试 1 次 |
| ④ 结果聚合 | Aggregator | `src/services/recommend.py` | 纯规则：营养打分 × 价格 × 券 |
| ⑤ 建议生成 | Presenter | `src/main.py` | 结构化模板（不依赖 LLM，保证 Demo 稳定） |
| ⑥ 下单门控 | OrderManager | `src/services/order_flow.py`（新增） | 确认门 + create-order |

设计原则: **LLM 只用在两端**（理解输入、润色输出），中间规划与聚合全部确定性代码——
Demo 可复现、可测试、可降级，这是比赛演示稳定性的关键。

## 2. 意图识别（IntentRecognizer）

### 输出 Schema（Pydantic: `IntentProfile`）

```json
{
  "goal": "fat_loss",            // fat_loss | muscle_gain | kids | normal
  "budget_cents": 3000,          // 预算（分），null=无预算限制
  "nutrition_prefs": {
    "max_kcal": 600,             // 减脂→单餐 ≤600kcal（可由规则表推导）
    "min_protein_g": 25,         // 增肌→≥25g；fat_loss→≥20g
    "avoid": ["油炸", "含糖饮料"]
  },
  "dining_mode": "dine_in",      // dine_in | drive_thru | delivery（默认 dine_in）
  "party_size": 1,
  "missing_slots": []            // 缺失槽位 → 触发追问
}
```

### 槽位与追问策略

| 槽位 | 来源 | 缺失时处理 |
|------|------|-----------|
| goal | 用户语句 | 默认 normal，不追问 |
| budget | "预算30元" → 3000 分 | 不追问，视为无限制 |
| 位置/门店 | 用户授权定位 或 默认地址 | **必须追问或用默认**（query-nearby-stores 需要） |
| dining_mode | 上下文 | 默认到店自取 beType=1 |

示例输入识别结果: goal=fat_loss, budget_cents=3000, max_kcal≈600, min_protein_g≈20, dining_mode=dine_in。

### goal → 营养约束规则表（存 `skill/references/nutrition-rules.md`）

| goal | 单餐热量 | 蛋白质 | 钠 | avoid |
|------|---------|--------|-----|-------|
| fat_loss | ≤600 kcal | ≥20g | ≤800mg | 油炸主食、含糖饮料 |
| muscle_gain | ≤900 kcal | ≥30g | — | — |
| kids | ≤500 kcal | — | ≤600mg | 咖啡因、辣 |
| normal | 不限 | 不限 | 不限 | — |

## 3. 工具规划（Planner）— 意图 → 调用 DAG

Planner 以「意图模板 + 依赖规则」生成 DAG，不自由生成（防幻觉调用）。

### 本例生成的调用 DAG

```
T1 query-nearby-stores (beType=1, searchType=定位)     ┐ 并行
T2 list-nutrition-foods ()                             ┘
        │                       │
        ▼                       ▼
T3 query-meals (storeCode←T1)         T4 available-coupons ()      ┐ 并行
                                      T5 query-promotions (storeCode←T1) ┘
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
       [本地] 候选筛选: T2×T3 交叉 → 营养达标 × 在售
                       ▼
T6 calculate-price (storeCode←T1, items←候选, couponId←T4/T5)
                       ▼
       [本地] Aggregator 打分排序 → Top2-3 方案
```

### 意图 → 工具模板映射表（Planner 核心）

| 意图/约束 | 追加工具 | 依赖 |
|-----------|----------|------|
| 任意点餐 | query-nearby-stores / delivery-query-stores | 根节点 |
| goal≠normal 或问营养 | list-nutrition-foods | 根节点（无参，恒并行） |
| 有预算或问优惠 | available-coupons | 根节点 |
| 团餐(beType=6) | query-promotions, query-meal-assistance | 依赖 storeCode |
| 需要精确价格 | calculate-price | 依赖 候选+券+storeCode |
| 用户确认下单 | create-order | **确认门后**才进 DAG |
| 外送模式 | delivery-query-addresses → delivery-query-stores（替换 T1） | 串行 |

### 并行与降级

- T1‖T2、T3‖T4‖T5 各为一个并行波次（wave），Executor 按波次调度
- 任一工具失败: 重试 1 次 → 降级策略：
  - T2 失败 → 用本地缓存的营养数据（`output/nutrition_cache.json`），标注"营养数据为缓存"
  - T4/T5 失败 → 直接 calculate-price（无券价），标注"未匹配到优惠"
  - T1 失败 → 终止并请用户检查定位授权（无 storeCode 则链路不可达）

## 4. 状态流转设计（会话状态机）

```
IDLE ──输入──► INTENT_PARSING ──┬─槽位完整─► PLANNING
                               │
                               └─缺槽位──► CLARIFYING ──补充──► INTENT_PARSING

PLANNING ──DAG生成──► EXECUTING ──全部成功/降级──► AGGREGATING ──► RECOMMENDATION_READY
                        │
                        └─不可降级失败──► ERROR（附 traceId，提示重试）

RECOMMENDATION_READY ──用户选择方案──► AWAITING_CONFIRMATION
                          （展示方案明细+取餐方式）
AWAITING_CONFIRMATION ──明确确认──► ORDERING ──create-order成功──► ORDER_CREATED
                          │                                        （返回取餐码/支付链接）
                          └─修改需求──► INTENT_PARSING（保留上下文）
ORDERING ──失败──► RECOMMENDATION_READY（回退，保留方案）
```

| 状态 | 持久化内容 | 可迁移到 |
|------|-----------|----------|
| IDLE | — | INTENT_PARSING |
| INTENT_PARSING | 原始输入 | CLARIFYING / PLANNING |
| CLARIFYING | 缺失槽位列表 | INTENT_PARSING |
| PLANNING | IntentProfile, DAG | EXECUTING |
| EXECUTING | 各工具状态(running/ok/degraded/failed), traceId | AGGREGATING / ERROR |
| AGGREGATING | 候选池, 打分 | RECOMMENDATION_READY |
| RECOMMENDATION_READY | Top 方案集 | AWAITING_CONFIRMATION / INTENT_PARSING |
| AWAITING_CONFIRMATION | 用户选中方案 | ORDERING / RECOMMENDATION_READY |
| ORDERING | 订单参数快照 | ORDER_CREATED / RECOMMENDATION_READY |
| ORDER_CREATED | orderId, 取餐码, payH5Url | IDLE（新会话） |

**门控规则（硬编码，LLM 无权绕过）**:
- `create-order` 仅当 state == AWAITING_CONFIRMATION 且用户回复确认词
- `draw-lottery` / `auto-bind-coupons` / `cancel-order` 同样要求显式确认
- 会话状态用 Pydantic `SessionState` 建模，单会话内存态，可序列化到 output/ 供 Demo 回放

## 5. Prompt 设计方案

三层 Prompt，全部放 `src/agent/prompts/`（代码内常量，非文件加载，便于审计）。

### 5.1 System Prompt（意图识别）— `intent_system.txt`

```
你是麦当劳点餐意图分析器。只输出 JSON，不输出任何解释。

任务: 从用户输入抽取点餐意图，严格遵循 schema:
{goal, budget_cents, nutrition_prefs{max_kcal,min_protein_g,avoid[]},
 dining_mode, party_size, missing_slots[]}

规则:
- goal 枚举: fat_loss(减脂/减肥/轻食) / muscle_gain(增肌/健身/蛋白质)
  / kids(儿童/孩子/小朋友) / normal
- 出现"预算X元/X块"→ budget_cents = X*100
- goal=fat_loss → max_kcal=600, min_protein_g=20, avoid=[油炸,含糖饮料]
- goal=muscle_gain → max_kcal=900, min_protein_g=30
- dining_mode: 提到外送/到家→delivery; 车道/得来速→drive_thru; 默认 dine_in
- 未提及定位且无默认地址 → missing_slots 加入 "location"
- 不得编造用户未提到的数值
```

### 5.2 Planner Prompt（兜底，仅当模板未命中）— `planner_system.txt`

```
你是 MCP 工具规划器。给定 IntentProfile 与工具注册表，输出调用 DAG(JSON)。

硬规则:
- 只能从工具注册表选择工具，禁止发明工具名
- calculate-price 必须排在 query-meals / available-coupons / query-promotions 之后
- create-order 永不进入推荐 DAG（由确认门单独触发）
- 输出: {waves: [[{tool, params, depends_on}]]}，同一 wave 内可并行
```

### 5.3 Presenter 模板（无 LLM 依赖）— `presenter.py` 渲染

```
【麦智方案 {i}】{方案名}（{goal_label}友好 · 券后 ¥{price/100}）
  🍽 组合: {productName×qty 列表}
  🔥 热量 {kcal}kcal（占单餐目标 {pct}%）· 蛋白质 {protein}g
  🎫 优惠: {couponTitle}（省 ¥{discount/100}）
  🏪 门店: {storeName}（{distance}）
  ⚠️ 数据说明: {degraded 标注位}
回复"选{i}"查看下单明细，或说"换一批"。
```

### 5.4 Prompt 工程纪律

1. 意图识别输出必须过 Pydantic 校验，失败 → 重问 1 次 → 降级 normal
2. 所有 LLM 调用 temperature=0，保证 Demo 可复现
3. Presenter 不用 LLM 生成数字——所有数值来自工具返回，杜绝幻觉报价
4. Few-shot 示例按 goal 四类各备 1 条，存 `src/agent/prompts/fewshots.py`

## 6. 本例端到端时序（预算 30 元减脂推荐）

```
用户: 我最近减肥，但是想吃麦当劳，预算30元，帮我推荐一下
 1. IntentRecognizer → {goal: fat_loss, budget: 3000, ...} (缺 location → 追问/默认地址)
 2. Planner → DAG: [T1‖T2] → [T3‖T4‖T5] → [T6]
 3. Wave1: query-nearby-stores + list-nutrition-foods 并行（~1s）
 4. Wave2: query-meals + available-coupons + query-promotions 并行（~1s）
 5. 本地筛选: 营养表×在售餐品交叉 → 高蛋白低卡候选 8-12 个
 6. Wave3: calculate-price×候选组合（批量）→ 券后价
 7. Aggregator: score = 0.5×营养分 + 0.3×价格分 + 0.2×优惠分 → Top3
 8. Presenter 渲染 → 状态 RECOMMENDATION_READY，等待"选1/2/3"
```

预估总时延: 3-5 秒（取决于 calculate-price 批量次数，可上限截断到前 5 组合）。
