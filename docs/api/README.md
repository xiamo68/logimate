# LogiMate HTTP 接口联调

已实现健康探针、演示身份、会话、排查提交以及运行、轨迹和证据读取。其余接口为已确认的设计合同。[OpenAPI 3.1](openapi.yaml)定义接口及字段，[MCP 工具合同](mcp-tools.md)定义内部取证接口。

## 通用约定

- 默认基地址为 `http://127.0.0.1:8010/api/v1`。正文为 UTF-8 JSON，时间戳使用 UTC `Z`。资源 ID 为不透明字符串，业务订单号在查询正文使用。
- `POST /demo/sign-in` 接收 `profile`，可选 `alpha_operator`、`alpha_reviewer`、`beta_operator`、`beta_reviewer`。服务端映射固定角色与租户，并签发 HttpOnly、SameSite=Strict 的 `lm_session` Cookie。入口只在回环演示启用时开放。更换身份签发新会话，前端清除上一身份的显示数据。
- 状态写入必须带与配置地址一致的 `Origin`。网页同源发送，CLI 从已配置的本地地址构造请求头并保留会话。租户、用户、角色从服务端上下文获取。网页按权限显示操作，接口单独检查权限和资源归属。
- 除登录外，写操作接受可选 `Idempotency-Key`，前端生成并在本次操作重试时复用。以租户、用户、HTTP 方法和具体资源路径去重。同键同规范化输入返回原操作的状态码及正文，同键不同输入返回 409。去重和业务写入在同一事务提交。首次提交回放或排查任务返回 202，客户端按 ID 查询状态。
- 列表使用 `limit` 和 `cursor`，默认 20、最多 100 条。游标由上一页 `next_cursor` 提供，并绑定主体和过滤条件；末页为 `null`。会话详情按消息顺序向后翻页，其他列表按创建时间和 ID 新到旧。
- 运行在 `queued`、`running`、`completed`、`failed` 间变化，阶段 04 接入 `interrupted` 与恢复入口。运行错误使用 `error`。成功完成后，`result.status` 区分 `answered`、`partial`、`needs_clarification`，证据与轨迹随完成结果持久化。网页每秒轮询，遇终态停止。
- 版本详情包含 `reviewed_report_id` 与 `activation_events`，分别用于定位最近审核决定及历史激活记录。复测页从 `GET /evaluations` 获取对照组和可展开的合成 `case_examples`。完整逐案例归档用 `artifact_path` 指明项目相对位置。

## 权限与错误

| 操作 | 运营员 | 审核员 |
| --- | --- | --- |
| 查询、继续会话和恢复运行 | 本人会话 | 本人会话 |
| 读取证据 | 本人运行关联的证据 | 本人运行与本租户审核队列直接引用的证据 |
| 提交、查看运营纠正 | 本人运行提交与本人记录 | 查看并复核本租户队列，提交人与复核人不同 |
| 改进任务与版本 | 创建本租户任务，查看本租户候选和报告 | 查看本租户任务，审核、发布和回滚 |
| 偏好、报告 | 本人偏好与公开合成评测 | 本人偏好与公开合成评测 |

| HTTP 状态 | 处理 |
| --- | --- |
| 400 | 修复无法解码的请求 |
| 401 | 重新建立演示会话 |
| 403 | 当前来源或角色不允许该操作 |
| 404 | 请求的资源不存在、不可见，或本地演示入口关闭 |
| 409 | 重读活动版本、运行状态或检查幂等键。常见错误码为 `stale_baseline`、`invalid_state`、`idempotency_conflict` |
| 422 | 根据字段约束修正输入 |

```json
{"code":"stale_baseline","message":"活动版本已更新，请重新回放候选。","request_id":"req_demo_021"}
```

## LM-1001 联调示例

以下为合成合同示例，ID、请求正文和预计状态用于联调，实际请求 ID 由服务端生成。写请求带同源 `Origin`，登录后的请求带会话 Cookie。每项写入使用独立幂等键。

### 1. 以运营员身份排查

| 请求 | 示例正文 | 预期 |
| --- | --- | --- |
| `POST /demo/sign-in` | `{"profile":"alpha_operator"}` | 200，收到运营员 Cookie |
| `POST /sessions` | `{"title":"LM-1001 物流排查"}` | 201，返回 `sess_demo_01` |
| `POST /sessions/sess_demo_01/runs` | `{"question":"LM-1001 显示已发货，为什么三天还没有物流更新？"}` | 202，返回 `run_demo_01` |
| `GET /runs/run_demo_01` | — | 运行完成，绑定 `rel_initial`，模拟旧版分类 `pickup_overdue` |
| `GET /runs/run_demo_01/trace` | — | 可核对订单、仓储与物流取证 |
| `GET /evidence/ev_demo_wh_01` | — | 仓库已出库，权威记录到查询时点仍无交接 |
| `GET /evidence/ev_demo_trk_01` | — | 承运商已创建标签且未记录揽收 |

### 2. 提交纠正与复核

向 `POST /runs/run_demo_01/feedback` 提交以下正文，收到 201 及 `fb_demo_01`，状态为 `pending`。

```json
{
  "corrected_classification": "awaiting_handoff",
  "reason": "仓储查询已覆盖这个包裹的事件窗口。只有出库记录，尚无交接，承运商标签也未转为揽收。",
  "evidence_ids": ["ev_demo_wh_01", "ev_demo_trk_01"]
}
```

切换到 `alpha_reviewer`，用 `GET /feedback?status=pending` 找到纠正，调用 `POST /feedback/fb_demo_01/verify`，正文为 `{"decision":"approve","reason":"交接和揽收证据核对一致。"}`。返回 200 和 `verified`。使用 `beta_reviewer` 请求同一纠正时返回 404。

### 3. 生成候选并激活

1. 切回 `alpha_operator`。用 `POST /evolution/jobs` 和 `{"kind":"base","feedback_ids":["fb_demo_01"]}` 申请规则改进，返回 202 与 `job_demo_01`。查询 `GET /evolution/jobs/job_demo_01`，完成后返回候选 `ver_base_01`。
2. 通过 `GET /versions/ver_base_01` 检查差异，定位活动基线回放 `rep_demo_01`。验证集达标的候选状态为 `passed`。作业执行完且候选未过门槛时，作业可为 `completed`，候选为 `failed`。
3. 切换到 `alpha_reviewer`，调用 `POST /versions/ver_base_01/review`，正文为 `{"decision":"approve","report_id":"rep_demo_01","reason":"验证集误判下降，重点案例无新增错误。"}`。版本详情的 `reviewed_report_id` 为 `rep_demo_01`。随后用 `POST /versions/ver_base_01/release` 和 `{"expected_release_id":"rel_initial"}` 激活，返回 200 与新发布 ID `rel_base_01`。发布读取已审核报告及其绑定的哈希。
4. 运营员在原会话发起新一轮排查。新运行绑定 `rel_base_01` 并得到 `awaiting_handoff`；旧运行仍可查看原证据和旧版本。以 `{"kind":"meta"}` 发起元层任务时，由服务端读取本租户历史。元候选额外返回 `skill_ablation` 报告，部署仍走活动基线验证。

### 4. 回滚和重新排查

审核员对 `POST /versions/ver_initial/rollback` 提交 `{"expected_release_id":"rel_base_01","reason":"验证退回初始规则组合。"}`。返回独立回滚事件，活动规则与技能回到 `ver_initial` 的组合，旧审计记录可查询。重放原幂等键返回原回滚结果；换键但仍传过期发布 ID 时返回 409。
