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

E–G 本轮入口与边界见后文，不暴露全量 MCP 写入口。某些旧版 Plugin 缺少科研队列证据、步行预览或能耗观测，会明确停止；接口存在不代表实机兼容。移动预算依据上游条件能耗和估计 tick，不能当成游戏层实时能耗切断器；实际到达后仍检查能量。遇障碍、失败、未知状态不自动探路。

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


## E–G：直接调用与前置条件

本节同样是用户线下操作说明，本轮未执行其中任何命令，也未运行测试、编译或联调。E–G 以 UPSTREAM.md 已记录的 `19cd5eb505ffd30f4832f3bfbf0ced9ddad4a889` 源码契约映射，依赖对应能力；不能将较旧发行版的接口存在视为兼容证明。

`action` 直接走同一适配器、Schema、只读开关、预算、prepare、本地 yes、commit 与原 action 核销，不经过模型，也不需要 API Key。每次创建独立任务，输出 task_id；中断后仅 resume/reconcile 原动作。`--arguments` 只接受单个 JSON 对象，不能传原始工具名、token 或任意额外字段。不同直接命令是不同任务，各有有限预算，但同一账本的 pending 始终全局排他。

先在 PowerShell 设置可执行文件路径，读取真实对象和星球身份：

```powershell
$agent = '.\.venv\Scripts\sphere-agent.exe'
& $agent --config config.toml factory
# $objectId 必须替换为上一步的实际正数 objectId
& $agent --config config.toml inspect --object-id $objectId
& $agent --config config.toml system
```

`factory` 仅一页最多 50 个已建对象，不能根据缺席断言对象不存在。`inspect` 读取精确对象、库存/增产点数、端口和可用哈希；`system` 输出本地 starId/localPlanetId 及最多 32 个原生可见远程星球/星系，不会授权航行或泄露隐藏资源。所有 `$...Id` 和坐标变量都应由用户使用真实观测值填写，示例不构造游戏状态。

### E：单体建筑、带路和分拣器

在 `[agent]` 保持有限配置，例如：

```toml
allow_write = true
build_item_ids = [2201, 2203, 2301, 2101, 2102, 2106, 2302, 2303]
max_build_objects = 12
max_build_distance = 16
max_belt_length = 20
max_grid_snap_distance = 3
geometry_tolerance = 0.25
```

仅支持上述普通单体建筑的子集；传送带固定为基础 2001，分拣器为 2011/2012。实例必须在运行时建造目录中已解锁、可用且物料已在背包；普通建造和矿机一次一个物件。不使用瞬建、蓝图批量、替换覆盖或高架自动寻路。

```powershell
# $x/$y/$z 为用户选择的真实近距空位；原生 prepare 仍负责碰撞和网格校验
$payload = @{ item_id=2201; x=$x; y=$y; z=$z; yaw=0 } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action build --arguments $payload
# 矿机额外绑定一个真实 vein 节点；可覆盖的全部节点由原生计划披露
$payload = @{ item_id=2301; x=$x; y=$y; z=$z; yaw=$yaw; node_id=$nodeId } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action build --arguments $payload

# 起止端点必须为 inspect 得到的不同已建对象，方向从 source 指向 destination
$payload = @{ item_id=2001; source_id=$sourceId; destination_id=$destinationId;
  x=$x; y=$y; z=$z; end_x=$endX; end_y=$endY; end_z=$endZ } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action belt --arguments $payload
$payload = @{ item_id=2011; source_id=$sourceId; destination_id=$destinationId; filter_item_id=$itemId } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action sorter --arguments $payload
```

人工观察顺序：

1. 在披露计划中核对实际 itemBudget、plannedPath、source/destination IDs；矿机核对资源节点，分拣器核对端口、偏移、初始过滤器。原生吸附与用户请求之间有有限偏移上限，实际整条路径也必须在距离/对象数预算内。
2. 仅对这个精确计划输入 yes；未解锁、无材料、几何/端口冲突、过期或 blockers 均应停止，不换位重试。
3. 等原 action terminal/succeeded 和建造就绪；核销必须取得新的正数已建对象 ID，不能使用负数 prebuild 或 commit accepted 代替完成。数量、逐个位置和原生材料扣除要与计划一致。
4. 带路还必须覆盖全部新对象的单向、互反连接；分拣器必须 pick/insert 方向和 filter 一致，普通设备端口与批准计划一致。证据缺失保留 pending，不能重建同一路径来修复本地断言。

### F：容器配额、配方与精确物流

```toml
max_transfer_items = 100
max_container_items = 10000
```

客户端物品总配额与原生禁用格数是两种不同约束：

```powershell
# 仅在 SQLite 保存本地 admission quota，不改原生库存、运输或格子
& $agent --config config.toml --enable-write quota --object-id $boxId --max-items 100
# 禁用箱子最后若干格：只支持普通 2101/2102 的原生 storage-capacity
$payload = @{ object_id=$boxId; banned_grids=10 } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action configure_storage --arguments $payload
# 在真实解锁目录中选择适配当前空闲制造台/熔炉的配方
$payload = @{ object_id=$machineId; recipe_id=$recipeId } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action configure_recipe --arguments $payload

$payload = @{ object_id=$boxId; item_id=$itemId; count=$count; direction='player-to-storage' } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action transfer --arguments $payload
$payload = @{ object_id=$boxId; item_id=$itemId; count=$count; direction='storage-to-player' } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action transfer --arguments $payload
```

转移前停止手工和其他 Agent 并发操作，关闭机甲自动科研取料、自动补燃料，清空手持物和 forge。当前守恒子集只接受没有投递端口、没有附属配送器且至多单层的独立箱子；不从研究点、能量数值或物流订单推算物品库存。入箱前必须设定本地总量配额。原生 prepare 仍负责实际容量、过滤器、距离和物料条件。

数量和增产点数按不同字段保存。无增产点数物料允许有限部分转移；带增产点数物料目前只允许转移来源端该物品的**完整现有存量**，避免猜测原生分堆的 removedInc。部分富集堆没有完整原生点数回执时，抛出 Unsupported，不做近似除法。

人工核对原 action 的 targetObjectId/targetItemId/requestedCount、同 tick 两端数量变化，以及新鲜背包/箱子 count/inc 的等量反向变化；其他物品和点数不得出现无法解释的变化。原生同步数量回执与跨 tick 的新鲜端点都保存到 SQLite。后续读回受到模拟或手动操作干扰时会保留 pending，并不假定旧转移没有发生。配方必须在实际设备读回，容量配置必须有 storageConfigurationReadback 且配置前后 count/inc 保持。

储罐可用 `inspect` 读取明确的 `tankFluidCount`，并用 `quota --object-id $tankId --max-items ...` 保存本地上限。再次 inspect 时，库存未知或超过配额会停止。这个上限只约束本客户端，不关闭原生管道/自动物流。当前上游普通 transfer 和 storage-capacity 不支持 tank，储罐写调用明确 Unsupported；不伪造 API、不直接改游戏内存、也不把本地配额声称为原生储罐配额开关。

### G：星际预算、授权与跨星球恢复

默认 `allow_remote_flight=false`，只读 system/route 可用。只有用户在本地启用以下配置并提供精确授权文件，才允许原生 Voyager 飞行：

```toml
allow_remote_flight = true
remote_authorization_file = "remote-authorization.json"
max_remote_distance = 100000000
max_remote_game_ticks = 36000
return_reserve_game_ticks = 18000
remote_min_core_ratio = 0.95
extra_warper_reserve = 1
max_warp_game_ticks = 1800
max_arrival_offset = 3000000
min_return_energy = 1000000
remote_wall_seconds = 600
# 飞行可能超过普通 120 秒轮询窗口；超时只恢复原 action
max_action_polls = 600
action_timeout_seconds = 600
poll_seconds = 1
```

这些键属于 `[agent]`，修改已有键，不要在 TOML 中重复定义。单次客户端 MCP 请求仍受 mcp.timeout_seconds 限制。长途 action 的原生 deadline 与客户端本次轮询 timeout 不同；客户端超时绝不重新起飞。

本地授权文件由用户根据真实 `system` 结果填写；使用有效 UTC 期限，必须覆盖整个往返 wall window。以下为字段格式，不是已授予的权限或可直接照抄的游戏身份：

```json
{
  "id": "用户为本次往返分配的唯一授权标识",
  "scope": "one_roundtrip_manual_recovery_review",
  "session_id": "真实受管 sessionId",
  "home_planet_id": 101,
  "home_star_id": 1,
  "destination_planet_id": 201,
  "destination_star_id": 2,
  "expires_at_utc": "替换为覆盖完整往返窗口的有效 UTC 时间",
  "maximum_game_ticks": 36000,
  "maximum_wall_seconds": 600,
  "return_authorized": true,
  "manual_recovery_review_authorized": true,
  "maximum_progress_loss_ticks": 0
}
```

数值身份全部换为真实观测值。文件是用户给客户端的精确路线/返航/人工恢复审查授权，不是原生 checkpoint reload 能力，也不替代上游恢复所需的后续披露及明确授权。当前实现不暴露存档 reload、自动恢复、瞬移、补能、补曲率资源或任意新游戏入口。授权 ID 在提交意图落盘时一次性占用；断连不能用同一授权开启另一个 outbound。授权文件内容 hash 随意图和旅程保存，返航不得悄悄更换文件或延长原旅程。

```powershell
# 只读预览不要求开启远程写入；budgetSufficient=true 也不是执行许可
& $agent --config config.toml route --destination-planet-id $remotePlanetId --destination-star-id $remoteStarId
$payload = @{ destination_planet_id=$remotePlanetId; destination_star_id=$remoteStarId } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action voyager_flight --arguments $payload
# 原生往返中的回程必须保留并指定第一次着陆读回的真实 journeyId
$payload = @{ destination_planet_id=$homePlanetId; destination_star_id=$homeStarId; journey_id=$journeyId } | ConvertTo-Json -Compress
& $agent --config config.toml --enable-write action --action voyager_flight --arguments $payload
& $agent --config config.toml resume --task-id $interruptedTaskId
```

人工核对：

1. 未配置授权、配置默认禁用、授权过期/身份不符/窗口不足、原生 visibility/technology/energy/warp/return reserve 任意未知或不足时，应在 commit 前停止。不给模型传入恢复布尔值作为许可。
2. route 请求绑定玩家、原生可见目录和独立 route hash；preview 必须 tokenless 且不声明 executable/commitAllowedNow。不能用星系主题推断矿物，不能猜测隐藏目的地；本客户端只接受精确可见的固体目的星球。
3. 原动作的 planetId 始终保留起飞星球；actualPlanetId/actualStarId 是独立着陆证据。只有原 native warp entry/真实曲率资源消耗、exit、稳定着陆、能量及返航资源读回都成立，才核销跨星球意图。
4. SQLite 保存 worlds、action_bindings、journeys、flight_authorities 与 container_quotas。动作 ID、原 key、session/planet/star 不可重绑；全局 pending 不因切星消失。旅程、任务目的作用域和核销证据在同一 SQLite 事务里更新，防止宕机后意图孤立。
5. 回程只用原 journeyId、原 home 身份与原 deadline/剩余返航窗口。回家着陆记录为 home_awaiting_save，不能声称原生保存或整个旅程持久关闭。之后的原生保存由用户按 Spherewright 规程处理；本客户端不自动补做。
6. 断连、没有 actionId、原生失败、outcome_unknown 或星球/会话不一致时保留 pending，所有新写入冻结；只对原动作只读 resume/reconcile，禁止重放飞行、换 checkpoint 或回滚更新的主存档。

### 仓库交付范围

只提交必要源码、示例配置、README 与本手册；任务书、个人授权文件、密钥配置、账本、缓存及本地历史验证记录不上传。`.gitignore` 排除这些本地材料。上游契约来源保留在已有 UPSTREAM.md。
