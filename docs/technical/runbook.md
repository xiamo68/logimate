# LogiMate 本地运行手册

状态为开发目标。当前仓库处于文档阶段，以下运行命令、端口和配置由阶段 01 至 06 实现并逐条验证。本文记录预计用法。

## 环境与配置

本地使用 Docker Desktop 的 Linux 容器和 Compose。Windows 在 PowerShell 中从仓库根目录操作。首次准备时确认 Docker daemon 可用、应用回环端口空闲、磁盘能存放镜像和向量模型缓存。阶段 01 记录实测镜像、CPU、内存和启动时长。已缓存的镜像与编码器可供无密钥演示复用。

| 配置 | 用途 | 约定 |
| --- | --- | --- |
| `LOGIMATE_MODE` | 模型运行模式 | 缺省 `demo`；显式选择 `deepseek` 时启用真实调用 |
| `LOGIMATE_DEMO_SIGN_IN` | 回环地址演示入口 | 仅为本地演示启用；启动时校验配置的外部地址是回环地址 |
| `LOGIMATE_PUBLIC_ORIGIN` | 同源 HTTP 地址 | 默认 `http://127.0.0.1:8000`，用于校验写请求来源 |
| `LOGIMATE_DATA_VERSION` | 合成业务数据版本 | 与应用种子和索引版本一起解析；运行开始时冻结 |
| `LOGIMATE_SESSION_SECRET`、`LOGIMATE_MCP_SIGNING_KEY` | 会话与 MCP 请求签名 | 本地独立生成，经环境或 Compose secrets 注入 |
| `MYSQL_DSN`、`REDIS_URL`、`MILVUS_URI` | 内部依赖连接 | 按 Compose 网络设置，凭证以配置注入 |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` | 真实模型凭据和模型 ID | 只在 `deepseek` 模式提供。启动检查字段完整性，评测记录使用的模型 ID |

阶段 01 提供只有安全占位值的配置示例。Compose 将主机端口限制为 `127.0.0.1:8000:8000`，应用在容器内监听其服务端口，其他依赖留在 Compose 内部网络。本机凭据、连接串和模型调用日志留在本机。向外部模型发送的数据为合成内容。付费运行前根据样本规模、实际模型和尝试预算估算费用，并使用已获授权的额度。

## 预定启动顺序

```powershell
docker version
docker compose version
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose run --rm app logimate demo seed --data-version synthetic-v1
```

部署启动负责数据库迁移，播种命令负责创建固定种子数据和构建同版本索引。启动检查依赖连接，并在未完成初始化时提示播种。初始化后使用 PowerShell 核查 readiness，打开回环地址的网页，选择 `alpha_operator`，在排查页提交 `LM-1001`。切换审核身份后可以复核纠正、检查报告、发布和回滚。

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/ready'
```

readiness 给出数据库、MCP、编码器与索引整体状态。应用仍可用时，单个来源异常由业务接口按[工具合同](../api/mcp-tools.md)和[检索预算](architecture.md#检索和记忆)返回降级结果。

## 评测用法

以下为预定 CLI。命令在持有冻结样本和可访问对应服务的环境执行，输出路径按运行批次区分。

```powershell
logimate eval logistics --mode demo --data-version synthetic-v1 --output artifacts/eval/demo/logistics
logimate eval rag --mode demo --data-version synthetic-v1 --output artifacts/eval/demo/rag
logimate eval grounding --mode demo --data-version synthetic-v1 --output artifacts/eval/demo/grounding
```

真实评测在确认调用量和费用后使用 `--mode deepseek` 及另外的输出目录。候选开发回放使用验证集，完整测试指标由最终评测命令计算。详见[验收口径](../qa/acceptance.md)。

## 诊断与恢复

| 现象 | 首个检查 | 预期定位或恢复 |
| --- | --- | --- |
| 页面或服务未就绪 | `docker compose ps` 和 `docker compose logs --tail 100 app` | 区分端口冲突、迁移未完成、索引未建和服务异常；按原因完成初始化或修复配置 |
| 取证部分失败 | 打开运行轨迹，比较 `tool_call_id`、`attempt` 和错误码 | 确认超时预算与来源状态。连接恢复后新建运行，保留之前的降级结果 |
| 检索退为关键词 | 读取 `retrieval_mode`、编码耗时及 Milvus 连接状态 | 数据或版本错误时重建同版本索引；人工延迟测试应触发 `keyword_only` |
| 应用重启 | `docker compose restart app` 后重新查看原运行 | 中断运行显示恢复入口。恢复后沿用原 ID、证据快照和活动版本绑定 |
| 离线任务进度停止 | 检查 worker 日志和租约 | 服务恢复后由新租约继续未完成案例。核对已有评测记录无重复 |
| 发布返回 409 | 查看当前发布 ID 和候选关联报告 | 基线变化时从现行版本重新回放并审核；同一网络请求用原幂等键重试 |

正常停止使用 `docker compose stop`，保留持久数据。干净启动验收使用独立 Compose 项目名与新卷，避免清理现有演示记录。
