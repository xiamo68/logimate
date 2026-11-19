# LogiMate 本地运行手册

阶段 01 提供原生 MySQL、合成数据、健康探针和会话接口。后续章节标注各阶段预计接入的能力。

## 环境与配置

Windows 在 PowerShell 中从仓库根目录操作。使用 Python 3.12、uv 和一个原生 MySQL 8 实例。应用默认使用回环地址 8010。Redis 和 Milvus 在阶段 04 以独立本地服务接入，需 Linux 的组件可使用已有的 WSL 环境。

| 配置 | 用途 | 约定 |
| --- | --- | --- |
| `LOGIMATE_MODE` | 模型运行模式 | 缺省 `demo`；显式选择 `deepseek` 时启用真实调用 |
| `LOGIMATE_DEMO_SIGN_IN` | 回环地址演示入口 | 仅为本地演示启用；启动时校验配置的外部地址是回环地址 |
| `LOGIMATE_PUBLIC_ORIGIN` | 同源 HTTP 地址 | 默认 `http://127.0.0.1:8010`，用于校验写请求来源 |
| `LOGIMATE_DATA_VERSION` | 合成业务数据版本 | 与应用种子和索引版本一起解析；运行开始时冻结 |
| `LOGIMATE_SESSION_SECRET`、`LOGIMATE_MCP_SIGNING_KEY` | 会话与 MCP 请求签名 | 会话密钥至少 32 个字符；MCP 密钥随阶段 02 接入 |
| `MYSQL_DSN`、`REDIS_URL`、`MILVUS_URI` | 本地依赖连接 | 当前必填 MySQL DSN；Redis 和 Milvus 地址在阶段 04 使用 |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` | 真实模型凭据和模型 ID | 只在 `deepseek` 模式提供。启动检查字段完整性，评测记录使用的模型 ID |

准备一个专用于 LogiMate 的 MySQL 数据库和用户，字符集选 `utf8mb4`。将仓库的 `.env.example` 复制为 `.env`，填入形如 `mysql+pymysql://用户:密码@127.0.0.1:3306/数据库` 的 DSN 和随机会话密钥。用户名或密码中用于 URL 的特殊字符需要编码。本机凭据和模型调用日志留在本机。付费运行前估算费用并使用已获授权的额度。

## 阶段 01 启动

```powershell
uv sync --locked
uv run --locked --env-file .env logimate migrate
uv run --locked --env-file .env logimate demo seed --data-version synthetic-v1
uv run --locked --env-file .env uvicorn logimate.api:create_app --factory --host 127.0.0.1 --port 8010
```

播种写入固定版本的业务数据和合成政策，并在 `.logimate/datasets/` 导出评测清单。应用只读取业务数据库，评测标签保存在独立目录。初始化后从另一个 PowerShell 窗口核查 readiness。

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8010/health/ready'
```

当前 readiness 检查数据库、迁移与指定数据版本。后续由 [MCP 工具合同](../api/mcp-tools.md)和[检索设计](architecture.md#检索和记忆)约定依赖状态及降级。

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
| 应用未就绪 | Uvicorn 控制台和 `/health/ready` | 区分端口冲突、MySQL 连接失败、迁移未完成和数据未播种 |
| 取证部分失败 | 打开运行轨迹，比较 `tool_call_id`、`attempt` 和错误码 | 确认超时预算与来源状态。连接恢复后新建运行，保留之前的降级结果 |
| 检索退为关键词 | 读取 `retrieval_mode`、编码耗时及 Milvus 连接状态 | 数据或版本错误时重建同版本索引；人工延迟测试应触发 `keyword_only` |
| 应用重启 | 重新启动原生 Uvicorn 进程，再查看原运行 | 阶段 04 提供中断运行的恢复入口，沿用原 ID、证据快照和版本 |
| 离线任务进度停止 | 检查 worker 日志和租约 | 服务恢复后由新租约继续未完成案例。核对已有评测记录无重复 |
| 发布返回 409 | 查看当前发布 ID 和候选关联报告 | 基线变化时从现行版本重新回放并审核；同一网络请求用原幂等键重试 |

使用 Ctrl+C 停止当前应用进程，保留持久数据。干净启动验收使用独立测试数据库和数据目录。
