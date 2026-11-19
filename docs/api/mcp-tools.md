# MCP 领域工具合同

状态为设计合同。业务取证在 FastAPI 与内部领域服务之间使用 MCP Streamable HTTP，服务路径为 `/mcp`。协调图只调用经白名单注册的工具，业务表查询绑定服务端授权和观测时点。 [MCP 传输说明](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) · [工具定义](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

## 身份和调用

- 工具参数只接收已列出的业务 ID。MCP 请求凭证包含已认证的租户、主体、允许工具、业务数据版本和观测时间。工具服务验证签名、受众、期限、类型和工具范围，再应用查询条件。模型只能选择白名单工具和业务参数。
- 控制平面完成 MCP 初始化、可用工具校验和 `tools/call`；应用将 `structuredContent` 验证为结果合同。MCP 协议错误与业务结果分别处理，权限缺失只返回通用错误。记录 `run_id`、`tool_call_id`、`attempt`、时延与结果状态。
- 缺资源或不在可见范围时返回 `not_found`。合法实体查到空的轨迹窗口时返回 `ok`，并以有覆盖时间的证据表示未记录事件。超时表示来源未能作答。

## 工具和数据

所有输入为 JSON 对象，未列出字段拒绝。业务 ID 使用字母、数字、点、横线或下划线，最长 64 字符。时间使用 UTC 字符串。金额为最小货币整数单位，配 ISO 4217 币种。

| 工具 | 输入 | `ok` 的业务 `data` | 证据来源 |
| --- | --- | --- | --- |
| `order.get` | 必填 `order_id` | `order_id`、`status`、`placed_at`、可空 `marked_shipped_at`、可空 `promised_by`、`amount_minor`、`currency`、`package_ids` | `order` |
| `warehouse.get_events` | 必填 `order_id`，可选 `package_id` | `order_id`、`package_ids`、`events`、`observed_through` | `warehouse` |
| `logistics.get_tracking` | 必填 `package_id` | `package_id`、`carrier_id`、`status`、可空 `estimated_delivery_at`、`events`、`observed_through` | `logistics` |
| `aftersales.get_cases` | 必填 `order_id` | `order_id`、`cases`、`observed_through` | `aftersales` |

事件字段为 `event_id`、`package_id`、`event_type`、`occurred_at`、`observed_at`，以事件时间和 ID 排序。仓储类型为 `picked`、`outbound`、`handed_off`；物流类型为 `label_created`、`picked_up`、`in_transit`、`delivered` 或 `carrier_exception`。工单字段为 `case_id`、`status`、`reason_code`、`opened_at`、`updated_at`、`timeline`；时间线条目为 `status`、`reason_code` 和 `occurred_at`。

## 输出与失败

工具结果都有 `status`、`data` 和 `evidence`。仅 `ok` 带非空 `data`。每条证据可按 [HTTP Evidence 模型](openapi.yaml)展示，业务来源的 `policy` 为 `null`。证据 ID 在同一快照内稳定，服务端绑定到相应运行，读取时校验授权。

| 状态 | 调用方行为 |
| --- | --- |
| `ok` | 校验结果并保存证据；事件为空时检查覆盖窗口 |
| `not_found`、`forbidden`、`invalid_input` | 保留状态并结束该工具步骤 |
| `timeout`、`unavailable` | 总预算允许时尝试一次重试，仍失败时记录缺口 |

默认每次尝试最多 1.5 秒，最多两次尝试，取证阶段总预算 8 秒。超时、网络错误或临时服务不可用映射成 `timeout` 或 `unavailable`。若总预算不足，停止发起新调用，保留已核实的事实。

### LM-1001 仓储结果示例

```json
{
  "status": "ok",
  "data": {
    "order_id": "LM-1001",
    "package_ids": ["PK-1001"],
    "events": [{
      "event_id": "wh_demo_01",
      "package_id": "PK-1001",
      "event_type": "outbound",
      "occurred_at": "2026-11-16T07:30:00Z",
      "observed_at": "2026-11-16T07:31:00Z"
    }],
    "observed_through": "2026-11-19T08:00:00Z"
  },
  "evidence": [{
    "evidence_id": "ev_demo_wh_01",
    "source": "warehouse",
    "resource_id": "PK-1001",
    "locator": "warehouse:PK-1001:until:2026-11-19T08:00:00Z",
    "observed_at": "2026-11-19T08:00:00Z",
    "fields": [
      {"name": "outbound_at", "value": "2026-11-16T07:30:00Z"},
      {"name": "handoff_recorded", "value": false}
    ],
    "policy": null
  }]
}
```
