# MCP_INTEGRATION.md — 麦当劳 MCP Server 集成说明

McSmart（麦智点餐官）基于官方麦当劳 MCP Server 构建，
本文档说明所用 Server、Tool 清单、业务作用、完整调用流程与创新价值。

> 对接状态: 2026-10-10 已实测连通，`initialize` 握手成功，
> `tools/list` 返回 35 个工具（原始响应存档 `output/tools_list_raw.json`）。

---

## 1. 使用的麦当劳 MCP Server

| 项 | 配置 |
|----|------|
| Server | **mcd-mcp v1.0.0**（麦当劳中国官方 MCP Server） |
| Endpoint | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（无状态会话，无需维护 Mcp-Session-Id） |
| MCP 协议版本 | `2025-03-26`，声明能力: `tools` |
| 鉴权 | Bearer Token（存于 `.env`，**不入库、不写入本文档**） |
| 客户端封装 | `src/mcp/client.py`（asyncio 并行 + 5s 超时 + 重试 1 次） |
| 工具注册表 | `src/mcp/tools.py` |

连接配置（凭证从 `.env` 读取）：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCP_AUTH_TOKEN}" },
      "disabled": false
    }
  }
}
```

---

## 2. 使用的 Tool 清单

McSmart 主链路使用以下 10 个官方工具。
文档中以 `mcd-mcp_*` 表示客户端注册别名，括号内为 Server 端真实工具名
（以 `tools/list` 实测为准）。

| # | 客户端别名 | Server 真实工具名 | 能力域 |
|---|-----------|-------------------|--------|
| 1 | `mcd-mcp_query-meals` | `query-meals` | 食品查询 |
| 2 | `mcd-mcp_query-nutrition-foods` | `list-nutrition-foods` ⚠️ | 营养分析 |
| 3 | `mcd-mcp_available-coupons` | `available-coupons` | 优惠发现 |
| 4 | `mcd-mcp_query-promotions` | `query-promotions` | 优惠发现 |
| 5 | `mcd-mcp_calculate-price` | `calculate-price` | 价格计算 |
| 6 | `mcd-mcp_query-nearby-stores` | `query-nearby-stores` | 门店推荐 |
| 7 | `mcd-mcp_create-order` | `create-order` | 智能点餐 |
| 8 | `mcd-mcp_query-my-coupons` | `query-my-coupons` | 优惠优化 |
| 9 | `mcd-mcp_query-store-coupons` | `query-store-coupons` | 优惠优化 |
| 10 | `mcd-mcp_auto-bind-coupons` | `auto-bind-coupons` | 优惠优化 |

> ⚠️ 勘误说明: 官方 `tools/list` 中**不存在** `query-nutrition-foods`，
> 营养数据的真实工具名为 `list-nutrition-foods`（无参数，返回全量餐品营养数据）。
> 客户端保留 `mcd-mcp_query-nutrition-foods` 别名以统一命名风格，
> `src/mcp/tools.py` 内部映射到真实工具名。

### 通用参数约定（来自实测 schema，编码必须遵守）

- `beType`: 1=到店自取, 2=麦乐送(外送), 5=得来速(DT), 6=企业团餐
- `orderType`: 1=到店（自取+得来速）, 2=外送（麦乐送+团餐）
- `beCode`: 得来速/外送/团餐必传（来自门店查询），到店自取不传
- 金额单位: **分**，展示时 ÷100 转元
- `storeCode`: 由门店查询工具返回，后续餐品/价格/下单工具必传
- **券试算规则**: `calculate-price` 使用优惠券时 `couponId` 与 `couponCode`
  **必须成对传入**（缺一报错 600010"使用优惠券需要couponId和couponCode"）；
  券字段按 item 逐商品挂载，组合试算时券只挂到券适用商品

---

## 3. 每个工具的业务作用

### 3.1 `mcd-mcp_query-meals`（食品查询）

- **必填**: storeCode, orderType, beType
- **作用**: 拉取指定门店在售餐品全量列表（单品/套餐/随单购），是推荐候选池的唯一来源
- **在 McSmart 中的角色**: 所有推荐组合的"原料库"；套餐子项(roundList)与特制选项
  由配套工具 `query-meal-detail` 补充（如需去酱、无糖等定制再调用）

### 3.2 `mcd-mcp_query-nutrition-foods` → `list-nutrition-foods`（营养分析）

- **必填**: 无
- **作用**: 返回麦当劳常见餐品的营养成分（能量、蛋白质、脂肪、碳水、钠、钙）
- **在 McSmart 中的角色**: **营养筛选引擎的数据底座**。无参数、全量返回的特性
  使其可在会话内缓存，本地完成"高蛋白低卡"筛选，避免逐个餐品远程查询——
  这是 McSmart 营养打分低时延的关键

### 3.3 `mcd-mcp_available-coupons`（优惠发现 · 可领券）

- **必填**: 无
- **作用**: 查询当前用户可领取的麦麦省优惠券（名称/图片/促销标签）
- **在 McSmart 中的角色**: 推荐"可领未领"的券并主动提示用户；
  配套工具 `auto-bind-coupons`（一键领券）需用户确认后触发

### 3.4 `mcd-mcp_query-promotions`（优惠发现 · 门店促销）

- **必填**: storeCode, orderType, beType
- **作用**: 查询指定门店当前有效促销规则（满减/满折），仅返回适用在售餐品的规则
- **在 McSmart 中的角色**: 促销规则参与价格试算与组合优化——
  例如"满 39 减 8"时，Agent 会评估加购小食凑单是否更划算

### 3.5 `mcd-mcp_calculate-price`（价格计算）

- **必填**: storeCode, orderType, beType
- **作用**: 对商品组合精确试算（原价/现价/优惠金额/打包费/运费），返回可选取餐方式
- **在 McSmart 中的角色**: **价格真值的唯一来源**。对本地筛选出的候选组合批量试算，
  确保"预算 30 元"约束基于真实券后价判断，杜绝估算报价；
  返回的 takeWayList 同时为下单提供取餐方式参数

### 3.6 `mcd-mcp_query-nearby-stores`（门店推荐）

- **必填**: beType, searchType
- **作用**: 到店场景查询用户可点餐门店（含得来速）；外送场景由
  `delivery-query-addresses` + `delivery-query-stores` 替代
- **在 McSmart 中的角色**: 点餐链路的**根节点**——返回的 storeCode 是
  餐品/促销/价格/下单工具的前置依赖；同时为用户提供就近门店选择
- **返回字段（实测）**: storeCode / storeName / address /
  **distance（单位: 米）** / **businessStatus（营业布尔）** /
  businessStartTime-businessEndTime（营业时段）/ reservation（预约能力）
- **门店查询模式**: "附近哪里可以买"类输入（无健康/省钱目标）触发轻量链路，
  只调用本工具一个工具，输出 Top 3 门店卡片（距离 + 营业状态 + 推荐理由）。
  推荐理由为规则模板生成（非 LLM）: 营业中优先 → 距离升序；
  第 1 名且 ≤800m 标注"步行即可到达"，打烊早于 21:00 提示营业时段，
  未营业门店排后并提示开门时间
- ⚠️ **定位能力边界（重要，实测确认）**: 本工具入参**只有**
  `beType` / `city` / `keyword` / `searchType`，**不接受经纬度**。
  - `searchType=2` 语义为"按位置搜索"，该"位置"由**服务端按账号判定**，
    客户端无法传入手机 GPS 坐标；返回的 `distance` 是服务端基准算出的值
  - 因此「先取操作者本地位置再匹配门店」在到店链路上**不可实现**，
    客户端唯一可控锚点是 `city`（+ `keyword` 如地标名）
  - 真正"先取用户位置 → 再匹配门店"的官方路径是**外送链路**:
    `delivery-query-addresses`（取账号收货地址）→ `delivery-query-stores`（按 addressId
    返回可配送门店）。实测该账号地址列表为空，Demo 阶段暂无数据
  - 工程对策: 城市槽位由自然语言解析（词表覆盖 60 个主要城市），
    未提及时用 `MCSMART_DEFAULT_CITY`（默认「北京」）兜底，
    并在结果中**显式标注"按默认城市查询，可加『在XX』指定"**——不静默猜城市

### 3.7 `mcd-mcp_create-order`（智能点餐）

- **必填**: storeCode, orderType, beType；到店场景另需 takeWayCode
- **作用**: 创建订单，支持优惠券、特制选项、随单购、预约，返回取餐码与支付链接
- **在 McSmart 中的角色**: 推荐结果到交易的**闭环终点**。置于硬确认门之后：
  仅当用户在 AWAITING_CONFIRMATION 状态下显式确认才调用，
  参数快照来自已试算的推荐方案，不重新构造（防价格与下单不一致）；
  配套 `query-order`（回查状态）与 `cancel-order`（取消）

### 3.8 `mcd-mcp_query-my-coupons`（优惠优化 · 我的券）

- **必填**: 无（page/pageSize 可选）
- **作用**: 查询当前账户已领优惠券（名称/用券价/有效期）
- **实测返回形态**: **Markdown 文本**，不含 couponId → 仅作概览展示
- **在 McSmart 中的角色**: "怎么吃最划算"链路的第一步——
  我的券为空时触发 `auto-bind-coupons` 自动领券后复查

### 3.9 `mcd-mcp_query-store-coupons`（优惠优化 · 门店可用券）

- **必填**: storeCode, orderType, beType
- **作用**: 查询指定门店+订单类型下可用的券，返回 **JSON**
- **关键字段**: `couponId` + `couponCode` + `products[].productCode`
  （券标识与适用商品的唯一结构化来源）
- **在 McSmart 中的角色**: **券后试算的输入源**。
  券 × 适用商品逐对送 `calculate-price` 试算，产出
  原价/优惠金额/最终价格/节省比例 四项指标（"报价零幻觉"的数据保障）

### 3.10 `mcd-mcp_auto-bind-coupons`（优惠优化 · 一键领券）

- **必填**: 无
- **作用**: 自动领取全部当前可领的麦麦省优惠券（无需指定券）
- **在 McSmart 中的角色**: 仅在"我的券为空"时自动触发（并打印提示），
  使"怎么最划算"链路在冷启动账户上依然可用；重复调用返回
  "暂无可领取"，幂等安全

---

## 4. 完整调用流程

### 4.1 健康推荐模式: "我最近减肥，但是想吃麦当劳，预算30元"

```
用户需求
  "我最近减肥，但是想吃麦当劳，预算30元，帮我推荐一下"
    │
    ▼
AI 意图分析（IntentRecognizer · LLM + JSON Schema）
  抽取: goal=fat_loss · budget_cents=3000 · dining_mode=dine_in
  规则表推导: 单餐 ≤600kcal · 蛋白质 ≥20g · 避开油炸/含糖饮料
  缺失槽位(如定位) → 追问补齐（CLARIFYING 状态）
    │
    ▼
MCP 工具选择（Planner · 意图模板生成调用 DAG，LLM 仅兜底）
  Wave 1（并行）: query-nearby-stores ‖ list-nutrition-foods
  Wave 2（并行，依赖 storeCode）: query-meals ‖ available-coupons ‖ query-promotions
    │
    ▼
数据融合（Aggregator · 确定性规则，非 LLM）
  ① 营养表 × 在售餐品交叉 → 高蛋白低卡候选 8-12 个
  ② 候选组合 × 可用券/促销规则 → 组合-券匹配对
  ③ calculate-price 批量试算 → 真实券后价（单位换算: 分→元）
  ④ 打分: score = 0.5×营养分 + 0.3×价格分(预算内优先) + 0.2×优惠分
    │
    ▼
推荐生成（Presenter · 模板渲染，数值全部来自工具返回）
  输出 Top 2-3 套方案: 菜品组合 + 热量/蛋白占比 + 券后价 + 就近门店
  → RECOMMENDATION_READY，等待用户"选1/2/3"
    │
    ▼
交易闭环（确认门后）
  用户确认 → create-order（复用试算快照）→ 取餐码/支付链接
  → query-order 回查状态
```

**容错降级**: 任一工具失败重试 1 次后按能力降级——营养数据用本地缓存并标注、
无券则按原价试算并标注；门店查询失败（无 storeCode）则链路终止并提示，
因为它是不可降级的根依赖。

### 4.2 省钱模式（优惠优化）: "今天吃麦当劳怎么最划算"

```
用户需求 "今天吃麦当劳怎么最划算"
    │
    ▼
AI 意图分析: goal=省钱（BEST_VALUE）→ 进入优惠优化专用 DAG
    │
    ▼
MCP 工具选择
  Wave 1（并行）: query-nearby-stores ‖ query-my-coupons
  Wave 2（并行，依赖 storeCode）: query-store-coupons ‖ query-promotions
                                  ‖ query-meals ‖ available-coupons
    │      （我的券为空 → auto-bind-coupons 自动领券后复查）
    ▼
数据融合 + 试算
  ① query-store-coupons 的券 × 适用商品 → calculate-price 券后试算
     （couponId + couponCode 成对传入，券只挂适用商品）
  ② 最优券商品 + 1~2 件低价正餐 → 组合试算（预算预过滤防无效调用）
  ③ 无券基线: 低价正餐按平台价试算（对比项）
    │
    ▼
推荐生成（按节省比例降序，Top 3）
  每个方案输出: 原价 / 优惠金额 / 最终价格 / 节省比例
  + 优惠情报概览（我的券 / 门店活动 / 可领券）
  实测示例: 麦咖啡美式 ¥19.00 → ¥9.90（用券，省 47.9%）
```

### 4.3 门店查询模式: "附近哪里可以买"

```
用户需求 "附近哪里可以买"
    │
    ▼
AI 意图分析: goal=普通 + 命中门店触发词（附近哪里/最近的麦当劳/哪里可以买...）
  → store_query=true → 门店查询轻量链路（单工具，秒回）
    │
    ▼
MCP 工具选择
  query-nearby-stores（beType=1, searchType=2, city=意图槽位）
    │
    ▼
数据融合: 营业中优先排序 → 距离升序 → Top 3
  推荐理由 = 规则模板（距离档位 + 营业状态 + 打烊提示 + 预约能力）
    │
    ▼
推荐生成: 每家门店输出 距离 / 营业状态（含时段）/ 地址 / 推荐理由
  实测示例: 中粮广场餐厅 29米 · 营业中(07:00-22:00) · "距离最近，步行即可到达，支持预约取餐"
```

---

## 5. 创新价值

### 5.1 对用户: 从"人找餐"到"目标驱动点餐"

传统点餐 App 要求用户自己浏览菜单、比价、算营养；McSmart 反转流程——
用户只表达**目标**（减脂/预算/遛娃），Agent 把目标翻译成营养约束 + 预算约束，
在 35 个官方工具的能力面上自动完成检索、筛选、比价、推荐、下单的全链路。

### 5.2 对 MCP 生态: 展示"工具编排"而非"工具转发"

McSmart 不做单工具的问答机器人，其价值在编排层：

- **波次并行 DAG**: 门店+营养无依赖并行，餐品+券+促销依赖 storeCode 后并行，
  端到端 3-5 秒，而非串行 10 秒+
- **本地融合计算**: 营养筛选、组合优化、多维打分在本地确定性完成，
  LLM 不接触数字——**报价零幻觉**，这是 AI 点餐可信度的核心
- **确认门控状态机**: 高危工具（create-order 等）由 11 态状态机硬门控，
  为"AI 代理交易"提供了可审计的安全范式

### 5.3 对麦当劳生态: 双向价值

- 营养数据 + 优惠数据 + 交易链路首次被 AI 融合成一个决策面，
  帮助用户"吃得明白、花得划算"，天然提升客单与复购
- 健康化消费趋势（减脂/增肌/儿童）通过 Agent 被翻译成
  精确的餐品组合推荐，激活长尾健康餐品的曝光

### 5.4 工程可复用性

五段式流水线（意图→规划→执行→融合→呈现）与工具注册表设计
可平移到任意 MCP Server（咖啡、便利店、外卖平台），
McSmart 的编排框架本身即是一份 **MCP Agent 开发范本**。

---

## 附: 验收清单

- [x] Server 连通性实测（2026-10-10，35 工具盘点）
- [x] 主链路 7 工具 + 优惠优化 3 工具业务作用与参数规则回填
- [x] 减脂/预算推荐链路真实调用打通（北京/上海双城市验证）
- [x] 优惠优化（省钱模式）链路真实调用打通（含券后试算与组合试算）
- [x] 券试算规则实测: couponId+couponCode 必须成对（错误码 600010）
- [ ] 并行调用与超时降级测试（波次内当前为串行简化实现）
- [ ] Demo 场景全真实链路（见 docs/demo-guide.md）
