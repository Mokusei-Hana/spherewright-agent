# 用户手动验收与排错

本轮仅编写功能与文档，未运行测试、静态检查、编译、环境验证、模型请求、MCP 联调或游戏操作。以下命令供用户线下执行，不是通过记录。

## 安装与配置

在仓库目录使用 Python 3.11+，安装与当前游戏匹配的完整 Spherewright Plugin/MCP 组合。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
Copy-Item config.example.toml config.toml
$env:DSP_AI_API_KEY = '你的真实密钥'
```

配置示例（其余 `[agent]` 参数沿用 config.example.toml）：

```toml
[provider]
type = "openai-compatible"
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
api_key_env = "DSP_AI_API_KEY"
timeout_seconds = 60
max_tokens = 2048
context_bytes = 64000

[mcp]
command = 'C:\Spherewright\Spherewright.Mcp.exe'
args = []
timeout_seconds = 30
```

base_url 为 `/chat/completions` 之前的前缀，按服务实际要求保留 `/v1`。密钥仅来自环境变量；未提供时直接报错，不生成虚假调用。只读观察、诊断、创建任务与恢复不要求模型密钥。

## 只读起步

```powershell
.\.venv\Scripts\sphere-agent --config config.toml check
.\.venv\Scripts\sphere-agent --config config.toml doctor
.\.venv\Scripts\sphere-agent --config config.toml observe
.\.venv\Scripts\sphere-agent --config config.toml new --goal '观察当前物料和科技，给出下一步建议'
.\.venv\Scripts\sphere-agent --config config.toml status
.\.venv\Scripts\sphere-agent --config config.toml run --task-id '上一步返回的任务 UUID'
```

`create` 是 `new` 的别名；`diagnose` 是 `doctor` 的别名。`check` 只读取配置。`doctor` 先输出 Python、操作系统、账本及密钥是否配置，再连接 MCP 读取工具、状态和受管会话的 Journal。诊断不会调用模型或写游戏。只读模式不向模型提供 A–D 写动作。

默认 `[agent].allow_write=false`。写入需要在**创建任务前**设为 `true`，并对运行命令显式提供 `--enable-write`（旧名 `--allow-write` 保留兼容）：

```powershell
.\.venv\Scripts\sphere-agent --config config.toml --enable-write run --goal '观察后选择一种已有全部原料的解锁配方，只制造一次'
```

每次提交前打印精确计划和物料预算，只有本地输入 `yes` 才提交。计划过期、原生 blockers、空 token、未知状态均停止，不重新 prepare 或 commit。先手动进入健康、受管、和平、非沙盒世界；客户端不创建、加载、导入、保存或退出游戏。

## A–D 手动观察点

| 动作 | 模型入口 | 用户应观察的结果 |
|---|---|---|
| A 手工制造 | `handcraft(recipe_id,count)` | 运行时已解锁配方；已有显式原料，不允许递归补料；有限次数与累计物料预算；原 action 成功、原生净材料变化和空 forge 队列 |
| B 当地采集 | `resources`、`harvest(kind,node_id,count)` | 一页最多 50 个资源作为参考；只采一个 vein/vegetation 节点；近距及 build area；原节点减少/耗尽、原生物品增量、未耗尽时重新读取节点 |
| C 科研选择 | `select_research(tech_id)` | 使用 `selectionStateHash/selectionStateHashVersion`，不复用玩家 hash；原生 before/after 队列变化、进度和库存保持；不把选择称为研究完成 |
| D 短距移动 | `move(x,y,z)` | 存活、地面静止；默认最多 12m、绝对最多 16m；地表及障碍预览完整、干燥、单段直达；条件耗能预算与到达读回 |

E–G、建造、物流、飞行等调用抛出 `Unsupported`，不暴露全量 MCP 写入口。某些旧版 Plugin 缺少科研队列证据、步行预览或能耗观测，会明确停止；接口存在不代表实机兼容。移动预算依据上游条件能耗和估计 tick，不能当成游戏层实时能耗切断器；实际到达后仍检查能量。遇障碍、失败、未知状态不自动探路。

## 预算、暂停与恢复

步数、提交数、物料数、只读重试计数随任务保存在 SQLite；重启不会重置。`max_material_items` 保守计入原生披露预算（含产出和科研预计消耗），可能比实际扣料更早停止。只读超时/连接异常最多重试配置次数；prepare/commit 绝不自动重试。原 action 轮询同时受 `max_action_polls` 和 `action_timeout_seconds` 约束；单次 MCP 请求另有有限超时。

任务状态：`pending` 待执行、`running` 运行、`waiting_confirmation` 等待本地确认、`paused` 暂停、`blocked` 被未核销意图阻塞、`completed` 模型结束、`failed` 失败。`completed` 表示本次有界任务结束，游戏成功仍以原生证据为准。任务预算耗尽后保持暂停，不自动加预算。完成任务不能再次运行；失败任务可先显式 resume 检查状态后进入暂停。

```powershell
New-Item PAUSE -ItemType File
.\.venv\Scripts\sphere-agent --config config.toml ledger
.\.venv\Scripts\sphere-agent --config config.toml status --task-id '任务 UUID'
.\.venv\Scripts\sphere-agent --config config.toml resume --task-id '任务 UUID'
# 也可对原账本中的既有 action 使用独立只读核销命令
.\.venv\Scripts\sphere-agent --config config.toml reconcile
```

PAUSE 文件位于配置文件目录，阻止新决策和提交；已有动作只读核销继续。Ctrl+C 与 PAUSE 不会原生暂停、撤销游戏动作或保存退出。移除 PAUSE 后，先 resume，再由用户显式 run --task-id 发起新的观察/决策；恢复不会重放旧工具调用，对话历史不恢复。

`resume` 只读取原 actionId 的结果和当前状态。若意图存在但 actionId 响应丢失，写入保持冻结，由用户按 Spherewright 原生规程核查，绝不换 key、重发或删账本。失败/未知/证据缺失也保留 pending。已成功动作后本地断言失败时，仍只处理原动作证据，不重做动作。

更换模型或 endpoint 后可运行 resume：新 Provider 配置会保存，未核销意图、原 MCP 配置及原预算保持。任务创建后的 MCP 路径、数据库、安全门和预算使用任务快照；需要调整这些参数时创建新任务，且不得绕开全局 pending。Provider 请求按 UTF-8 字节预算保留系统指令、目标及最新完整 assistant/tool 对，并为工具定义和输出预留空间；指令本身过大就停止。

## 排错与记录

- 无密钥：设置 api_key_env 指定的变量；无需为只读恢复填入密钥。
- 非 JSON、截断或多工具调用：Provider 报错，整批拒绝，不执行第一个调用；选择支持串行工具调用的模型。
- MCP 启动/初始化超时：核对 command/args、完整发行组合；stdio 上下文退出时由 SDK 清理其子进程，不另开后台常驻 MCP。
- 账本锁被占用：停止使用同一数据库的其他进程；不要创建另一数据库绕过 pending。锁只保护同一路径账本，无法排他控制其他 Agent 或手动游戏操作。
- Journal 未落盘：保留 pending，待原生 Journal 恢复持久化后，仅 resume/reconcile 原动作。
- 旧版缺字段、身份变化、海岸风险、障碍预览不足、未知能耗：停止，记录原始状态；不放宽门禁或虚构数据。

记录 Spherewright 完整包/DSP 版本、模型和 endpoint（不含密钥）、任务 UUID、目标、原 actionId、终态和实际观察。SQLite 含本地游戏状态与任务目标，请自行保管；不要提交数据库、密钥或私有配置。
