# LogiMate

LogiMate 是一个供应链异常排查与策略改进项目。目标是在同一个中文工作台里，让运营人员用自然语言查询订单、仓储、物流和售后，查看判断依据，并把已复核的纠正转化为可回放、可审核、可回滚的排查规则。

**当前进度为阶段 03 规则治理。** 合成数据、会话、LangGraph 经 MCP 的排查链路和双层改进已接入。按[本地运行手册](docs/technical/runbook.md#阶段-01-启动)启动应用、MCP 和离线 worker。从 [Swagger UI](http://127.0.0.1:8010/docs)查看已实现接口。后续阶段见[交付索引](docs/delivery/README.md)。

## 计划交付

- LangGraph 编排协调、订单、仓储、物流、售后五类 Agent，通过 MCP 查询合成业务记录并返回可追溯证据。
- 基础层从已复核反馈改进规则，元层从历史回放改进规则生成技能。审核员检查候选与现行版本的对照后决定是否激活，也可以回滚历史组合。
- 售后政策使用 Milvus 稠密检索与中文 BM25 双路召回，Redis 保存热会话，Milvus 索引长期偏好，检查点支持应用重启续跑。
- 同源轻量网页提供排查、改进、复测。合成 demo 无需模型密钥，最终公开报告也包含真实 DeepSeek 评测。

## 文档索引

| 文档 | 使用场景 |
| --- | --- |
| [已确认总体规格](docs/superpowers/specs/2026-11-19-logimate-design.md) | 查项目目标、范围和选型背景 |
| [产品需求 PRD](docs/product/prd.md) | 查使用场景、功能编号和可观察的验收结果 |
| [HTTP 联调说明](docs/api/README.md) · [OpenAPI 合同](docs/api/openapi.yaml) · [MCP 工具](docs/api/mcp-tools.md) | 查 HTTP 和工具字段、权限、错误及示例 |
| [技术设计](docs/technical/architecture.md) · [本地运行手册](docs/technical/runbook.md) | 查数据流、状态机、服务配置和排障 |
| [评测与验收](docs/qa/acceptance.md) | 查数据拆分、指标、用例和验收依据 |
| [六阶段交付](docs/delivery/README.md) | 查阶段依赖、产物和实际验证记录 |
| [开发协作约定](AGENTS.md) | 查开发、文档及发布规则 |

所有业务数据、政策材料和评测案例均为合成。完成六阶段功能验收和真实模型评测后发布 GitHub。MCE 双层机制参考 Ye 等人的论文并按物流场景实现。 [ICML 2026 论文](https://proceedings.mlr.press/v306/ye26g.html)
