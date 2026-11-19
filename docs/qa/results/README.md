# 冻结集评测归档

使用 `logimate eval freeze` 生成 `synthetic-final-v1`，SHA-256 为 `009c2f3647857c9cc34d5ba4383816b0f589b22b2c7222607aeb36208a79281a`。评测摘要供工作台读取，案例文件保留各组输出及评分。

| 评测 | 无密钥模式 | 真实 DeepSeek |
| --- | --- | --- |
| 物流诊断 | [摘要](evaluation-demo-logistics.json) · [逐案例](cases-demo-logistics.json) | [摘要](evaluation-deepseek-logistics.json) · [逐案例](cases-deepseek-logistics.json) |
| 售后检索与问答 | [摘要](evaluation-demo-rag.json) · [逐案例](cases-demo-rag.json) | [摘要](evaluation-deepseek-rag.json) · [逐案例](cases-deepseek-rag.json) |
| 事实校验 | [摘要](evaluation-demo-grounding.json) · [逐案例](cases-demo-grounding.json) | [摘要](evaluation-deepseek-grounding.json) · [逐案例](cases-deepseek-grounding.json) |

结果解读见[阶段 06 验收](../../delivery/06-release.md)。工作台可通过 `LOGIMATE_EVALUATION_DIR=docs/qa/results` 读取本目录。
