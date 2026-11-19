# LogiMate 本地运行手册

当前提供中文演示工作台、原生后端服务、冻结集评测命令和评测归档入口。

## 环境与配置

Windows 在 PowerShell 中从仓库根目录操作。使用 Python 3.12、uv 和一个原生 MySQL 8 实例。应用默认使用回环地址 8010。Redis 可运行在 WSL，Milvus Lite 可在 Windows 进程内使用本地数据文件。

| 配置 | 用途 | 约定 |
| --- | --- | --- |
| `LOGIMATE_MODE` | 模型运行模式 | 缺省 `demo`；显式选择 `deepseek` 时启用真实调用 |
| `LOGIMATE_DEMO_SIGN_IN` | 回环地址演示入口 | 仅为本地演示启用；启动时校验配置的外部地址是回环地址 |
| `LOGIMATE_PUBLIC_ORIGIN` | 同源 HTTP 地址 | 默认 `http://127.0.0.1:8010`，用于校验写请求来源 |
| `LOGIMATE_DATA_VERSION` | 合成业务数据版本 | 与应用种子和索引版本一起解析；运行开始时冻结 |
| `LOGIMATE_SESSION_SECRET`、`LOGIMATE_MCP_SIGNING_KEY` | 会话与 MCP 请求签名 | 各至少 32 个字符，独立生成；应用和 MCP 进程使用同一个 MCP 签名密钥 |
| `LOGIMATE_MCP_URL` | MCP 服务地址 | 默认 `http://127.0.0.1:8011/mcp` |
| `MYSQL_DSN`、`REDIS_URL`、`LOGIMATE_MILVUS_URI` | 本地依赖连接 | MySQL DSN 必填。Redis 使用本地 URL，Milvus 使用本地 Lite 路径或原生服务 URL |
| `LOGIMATE_CHECKPOINT_DB` | LangGraph SQLite 检查点 | 默认示例为 `.logimate/checkpoints.db`，续跑期间保留该文件 |
| `LOGIMATE_EMBEDDING_MODEL`、`LOGIMATE_MODEL_CACHE` | 中文编码模型与缓存目录 | 默认 `BAAI/bge-small-zh-v1.5` 与 `.logimate/models`，首次构建需要下载 |
| `LOGIMATE_EVALUATION_DIR` | 已归档的评测摘要目录 | 默认为 `.logimate/evaluations`，读取该目录下的 `evaluation-*.json` |
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

## 阶段 02 MCP 排查

在 `.env` 设置 MCP 签名密钥，另开一个 PowerShell 窗口，从仓库根目录启动 MCP。主应用仍用阶段 01 命令启动。已初始化的数据库重新执行 `logimate migrate`，以创建运行与证据表。

```powershell
uv run --locked --env-file .env uvicorn logimate.mcp_service:create_app --factory --host 127.0.0.1 --port 8011
```

无密钥模式通过 HTTP 联调说明登录、创建会话并提交 `LM-1001`。运行完成后查看结论、轨迹和证据明细。DeepSeek 模式需显式配置 `LOGIMATE_MODE=deepseek`、`DEEPSEEK_MODEL` 与 `DEEPSEEK_API_KEY`。当前适配器在意图识别节点请求模型，工具授权、取证和诊断在本地执行。正式真实模型验收和费用授权仍在最终阶段。

## 阶段 03 规则治理

升级已有本地数据库时重新执行 `logimate migrate`，并保证阶段 01 生成的验证数据可读取。新开 PowerShell 窗口运行原生 worker。

```powershell
uv run --locked --env-file .env logimate worker
```

加 `--once` 可处理一项可领取任务后退出。若数据文件导出在其他目录，传入 `--datasets`。任务逐案例保存结果；异常停止后，新 worker 在原租约过期后继续剩余案例。DeepSeek 模式的 worker 使用同一套模型配置，从已复核的规则候选和有历史回放依据的技能变体中生成提案。两种模式均走同一审批和发布门槛。

## 阶段 04 检索、记忆与恢复

先确保 `REDIS_URL` 指向回环地址上的 Redis，例如在已有 WSL 的终端用 `redis-server --bind 127.0.0.1 --port 6379`。在主应用停止时运行以下命令，再用阶段 01 命令启动应用。Milvus Lite 数据目录由一个进程持有。

```powershell
uv run --locked --env-file .env logimate migrate
uv run --locked --env-file .env logimate index-policies --data-version synthetic-v1
```

询问退款或赔付时，可从证据接口读取政策版本、条款和原文。稠密路径超时后检查 `retrieval_mode` 是否为 `keyword_only`。Redis 缓存不可用时由 MySQL 重建近期问题窗口和旧订单号摘要。偏好索引失败可在应用停止后运行 `logimate index-preferences`。更换向量模型时使用独立索引目录并重新建立索引。

主应用使用单进程运行。中断后的运行在启动时标记为 `interrupted`，按原 ID 调用 `POST /runs/{id}/resume`。恢复保留原模型模式、合成快照和规则版本。长期保留同一个检查点文件，清空它会失去已执行节点的进度。

## 阶段 05 演示工作台

应用、MCP 和离线 worker 就绪后，用浏览器打开[本地工作台](http://127.0.0.1:8010/)。网页与 FastAPI 同源，无需另起前端进程。

1. 用 `Alpha · 运营` 身份运行默认的 `LM-1001` 物流问题。左右查看诊断、证据和调用轨迹，展开仓储记录核对交接状态，再通过「跟进上单」演示多轮。
2. 展开「提交运营纠正」，选「等待交接」并填写依据。切换到 `Alpha · 审核`，进入「策略改进」，展开引用证据并复核。
3. 切回运营，在「策略改进」运行基础层。任务完成后切换审核，打开候选版本并查看回放，审批后发布。切回运营再次排查，确认诊断变为「等待交接」。同一页面可运行元层并查看技能对照。
4. 审核员从初始版本详情回滚。运营再次排查可确认恢复为「揽收超时」。如任务中断，可在原运行使用「继续排查」按钮。
5. 「评测」读取合法的项目相对路径归档摘要。阶段 06 生成实际报告后该视图显示组别、样本量、指标和案例。归档目录为空时显示空状态。

## 评测用法

从仓库根目录冻结最终集。先完成一次基础层和一次元层任务，并从工作台或版本列表获取生成的候选 ID。物流评测要求 B、C 同起始规则、同反馈来源、同模型和同合成业务数据版本。评测 Milvus Lite 时先停止占用同一个索引文件的应用进程。

```powershell
uv run --locked logimate eval freeze
uv run --locked --env-file .env logimate eval logistics --fixed-version $fixedVersion --evolved-version $evolvedVersion
uv run --locked --env-file .env logimate eval rag
uv run --locked --env-file .env logimate eval grounding
```

命令默认输出至 `.logimate/evaluations`。每次生成一份可在工作台读取的 `evaluation-*.json` 摘要及一份 `cases-*.json` 明细。最终冻结集只供评测读取，生成和筛选候选继续使用改进集与开发验证集。详见[验收口径](../qa/acceptance.md)。

真实评测确认费用后启用 `deepseek` 模式，使用同一模型生成 B/C。为临时验收应用配置 `LOGIMATE_DEEPSEEK_MAX_REQUESTS=2`，离线 worker 配置为 3。物流实时调用数为零，它离线读取 B/C 产物。RAG 命令使用 `--mode deepseek --max-requests 60`，事实校验对照用 `--mode deepseek --max-requests 120`。应用的真实售后回答经 DeepSeek 选择候选条款并通过证据校验，显示的引用来自政策原文。每次模型评测保存实际请求及 token 计数。

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
