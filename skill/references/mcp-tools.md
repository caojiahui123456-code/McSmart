# MCP 工具清单与参数

> 2026-10-10 通过 `tools/list` 实测获取，共 35 个工具。
> 完整 inputSchema / outputSchema 见 `output/tools_list_raw.json`（git 忽略）。
> 服务端: mcd-mcp v1.0.0，协议 2025-03-26，Streamable HTTP 无状态会话。

## 通用枚举

- `beType`: 1=到店自取, 2=麦乐送(外送), 5=得来速(DT), 6=企业团餐
- `orderType`: 1=到店（自取+得来速）, 2=外送（麦乐送+团餐）
- `beCode`: 得来速/外送/团餐必传；到店自取不传；来源 query-nearby-stores / delivery-query-stores
- 金额单位: **分**，展示时 ÷100 转元

## 点餐主链路（13 个）

| 工具 | 必填参数 | 说明 |
|------|----------|------|
| `query-nearby-stores` | beType, searchType | 到店/得来速可点餐门店 |
| `delivery-query-addresses` | — | 外送配送地址列表 |
| `delivery-create-address` | address, addressDetail, city, contactName, phone | 新建配送地址 |
| `delivery-query-stores` | beType, addressId | 地址可配送门店（返回 beCode） |
| `query-meals` | storeCode, orderType, beType | 餐品列表（含随单购） |
| `query-meal-detail` | storeCode, orderType, beType, code | 餐品详情 + 特制选项（selectedKey/unselectedKey） |
| `list-nutrition-foods` | — | 常见餐品营养成分 |
| `calculate-price` | storeCode, orderType, beType | 价格试算（含优惠、打包费、取餐方式 takeWayList） |
| `create-order` | storeCode, orderType, beType | 创建订单（券/特制/随单购/预约） |
| `query-order` | orderId | 订单详情 |
| `cancel-order` | orderId, cancelReasonCode | 取消订单 |
| `order-list` | — | 历史订单 |
| `query-meal-assistance` | storeCode | 团餐助餐服务（calculate-price 前） |

## 优惠与促销（5 个）

| 工具 | 必填参数 | 说明 |
|------|----------|------|
| `available-coupons` | — | 当前可领的麦麦省优惠券 |
| `auto-bind-coupons` | — | 一键领取全部可领券 |
| `query-my-coupons` | — | 卡包券资产 |
| `query-store-coupons` | orderType, beType, storeCode | 指定门店+订单类型可用券 |
| `query-promotions` | storeCode, orderType, beType | 团餐满减/满折规则 |

## 积分与商城（7 个）

| 工具 | 必填参数 | 说明 |
|------|----------|------|
| `query-my-account` | — | 积分账户（可用/过期/冻结） |
| `mall-points-products` | catRuleIds(选) | 积分可兑换商品列表 |
| `mall-product-detail` | spuId | 兑换商品详情 |
| `mall-create-order` | skuId, spuCategory | 积分兑换下单（shopId=2） |
| `mall-order-list` | — | 商城订单列表 |
| `mall-order-detail` | orderId | 商城订单详情 |
| `query-survey-coupon` | orderId | 订单满意度答卷 + 关联奖券 |

## 派对活动（5 个）

| 工具 | 必填参数 | 说明 |
|------|----------|------|
| `query-party-city` | spuId | 派对场次城市列表 |
| `query-party-store` | code | 城市下可办派对门店 |
| `query-party-store-date` | spuId, storeCode | 门店可预约日期 |
| `query-party-store-session` | spuId, storeCode, dateStr | 场次列表 |
| `party-order-create` | partyType | 创建派对订单（shopId=5） |

## 抽奖与活动（4 个）

| 工具 | 必填参数 | 说明 |
|------|----------|------|
| `query-lottery-info` | — | 抽奖活动信息与消耗规则（先展示再确认） |
| `draw-lottery` | — | 执行抽奖（须用户明确确认，严禁自动连抽） |
| `query-my-prizes` | — | 我的奖品 |
| `campaign-calendar` | — | 当月营销活动日历 |

## 辅助（1 个）

| 工具 | 必填参数 | 说明 |
|------|----------|------|
| `now-time-info` | — | 服务器当前时间（预约/活动日历场景用） |

## 高风险工具调用纪律

- `draw-lottery` / `create-order` / `mall-create-order` / `party-order-create` /
  `auto-bind-coupons`：**必须**先向用户展示关键信息并获明确确认后才可调用
- `cancel-order`：必须二次确认
- `create-order` 后返回 payH5Url，引导用户支付后回查 `query-order` 状态
