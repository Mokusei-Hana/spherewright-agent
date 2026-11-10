# Spherewright Agent — Python MVP

可配置 AI Provider 的《戴森球计划》Agent。**已实现真实 stdio MCP 客户端与有界 A–G 动作适配器（制造、采集、科研、移动、建造连接、本地物流及受保护 Voyager）；尚未经真实游戏验收，不是自动通关成品。** DeepSeek 只是示例配置，不是固定依赖。

## 已完成

- Python 3.11+ CLI；观察、运行、查看待核销记录、原动作核销。
- Provider 协议及 OpenAI Chat Completions 兼容适配器；用户配置地址、模型、密钥环境变量、超时和输出额度。支持兼容该协议且能调用工具的服务/本地模型。
- 连接用户安装的 Spherewright MCP stdio 进程，自动发现工具及 JSON Schema，校验实际参数；读取安装版本自己的 Agent playbook。
- 有限决策/写入/单次制造次数预算；模型仅能调用显式观察入口与 A–G 有界适配器，不接触原始 commit、任意 Python 或 Shell。
- 手工制造：新鲜完整玩家 hash → 原生 prepare → 披露物料/计划并由本地用户确认 → 同一 token/会话/星球 commit → 核销原 actionId → 原生终态、产品增量与新鲜状态回读。
- SQLite WAL/FULL 保存提交前意图、唯一幂等键、原 actionId、原始终态及读回；计划 token 不落盘。未知结果冻结写入，绝不换 key 重放。
- SQLite 任务状态机与累计预算；默认只读，显式启用 A–G 写适配器；星际飞行还需要本地精确授权与原生往返预算。
- 同一账本的进程排他锁、PAUSE 文件、Ctrl+C 停止；原生和平/非沙盒/归属/健康/Journal 门禁。

## 上游核查与边界

实际检查了 [Spherewright](https://github.com/Arcueid77/Spherewright) 和 [dsp-agent](https://github.com/etalam/dsp-agent) 源码，固定身份见 [UPSTREAM.md](UPSTREAM.md)。后者目前包含同步 Mock Agent，本项目借鉴其观察→决策→执行→验证结构，未复制其 Mock 代码，也不声称已经集成其 WorldState/Planner。

建议先使用 Spherewright **v0.4.0** 的整套正式包；对应 DSP/BepInEx 版本按其安装文档。main 的 0.5 源码能力不代表正式包能力。运行时发现和 Schema 校验仅确认接口存在，**不能证明具体安装组合已实机兼容**。缺失工具或证据即停止。

遵循 [上游安装指南](https://github.com/Arcueid77/Spherewright/blob/main/docs/release-installation.zh-CN.md) 安装匹配的 Plugin/MCP/共享程序集。准备一个已有的、健康的 Spherewright 受管和平非沙盒存档。MVP 不负责创建、导入、读档、跨版本恢复、保存或退出游戏。

## Windows 启动

```powershell
git clone https://github.com/Mokusei-Hana/spherewright-agent.git
cd spherewright-agent
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
Copy-Item config.example.toml config.toml
```

编辑 `config.toml` 的 `[mcp].command` 为实际 `Spherewright.Mcp.exe` 完整路径。框架自己启动 MCP 子进程，不需要 HTTP 地址，不传 Bridge token/存档身份。若使用自行构建的 DLL，可将 command 改为 `dotnet`，args 改为 DLL 路径数组。

编辑 `[provider]`：`type="openai-compatible"`、`base_url`、`model`、`api_key_env`。base_url 是 `/chat/completions` 之前的完整前缀：DeepSeek 示例为 `https://api.deepseek.com`；某些服务需要以 `/v1` 结尾。API key 只从环境读取，不写入仓库。无认证本地服务可将相同环境变量设为非空占位值。

```powershell
$env:DSP_AI_API_KEY = '你的密钥'
.\.venv\Scripts\sphere-agent --config config.toml check
# 手动启动游戏，进入健康受管存档，再执行只读观察
.\.venv\Scripts\sphere-agent --config config.toml observe
.\.venv\Scripts\sphere-agent --config config.toml run --goal '查看当前配方和物料，给出下一步建议'
```

确认只读工作正常后，将 `[agent].allow_write` 设为 `true`，再显式启用写入：

```powershell
.\.venv\Scripts\sphere-agent --config config.toml --enable-write run --goal '从当前解锁配方中选择一种能制造的物品，只制造一次并汇报结果'
```

每个精确 prepare 计划会显示材料预算和完成条件，输入 `yes` 才提交。用户确认期间计划过期会停止，不自动刷新后沿用旧批准。模型必须支持串行工具调用；返回多个并行调用会停止。对话按 context_bytes 滑动裁剪，保留系统指令、目标和完整工具对，为工具定义和输出预留空间；不自动探测模型上下文上限。

## 暂停、重连与核销

在配置文件目录创建 `PAUSE` 文件会阻止下一次决策/commit；删除后可启动新一轮。正在执行的原生动作仍须核销，**暂停文件和 Ctrl+C 不等于游戏原生暂停/撤销**，也不会保存、退出或重启游戏。

```powershell
New-Item PAUSE -ItemType File
.\.venv\Scripts\sphere-agent --config config.toml ledger
.\.venv\Scripts\sphere-agent --config config.toml reconcile
```

有 actionId 时只轮询原动作并重新读回；失败、超时、身份不符或证据缺失保持 pending。commit 响应完全丢失时可能没有 actionId：只保留原 key/意图并冻结，由用户按 Spherewright 原生规程核查，MVP 不提供清隔离/重放/删除账本来继续的按钮。原生成功后的本地验证失败仍保留成功终态，不能重新制造“修复”本地断言。

使用同一配置/账本；不要另开不同数据库绕过 pending。运行前停止其他游戏写入者，手工操作也不要与制造并发。锁仅保护同一账本，不宣称能排他控制所有其他 Agent。进程重启不是游戏重启许可，模型更换不清理 pending。对话历史不恢复；新一轮重新观察，已接受动作只核销。

## 任务持久化与用户验收

`new/create` 创建持久任务，`status` 查看状态与累计预算，`run --task-id UUID` 启动任务。
`resume --task-id UUID` 仅检查账本与原动作，成功后停在 paused；用户显式 run 才开始新决策。
`doctor/diagnose` 输出本地环境、MCP 和 Journal 状态。Provider 可在存在 pending 时切换并持久保存，原 MCP 配置及预算保留。

本轮没有运行测试、静态检查、编译或任何环境/游戏验证。启动命令、配置、A–D 验收与排错见 [MANUAL-ACCEPTANCE.md](MANUAL-ACCEPTANCE.md)。

## E–G 入口与能力边界

`factory` / `inspect --object-id ID` 读取本地实际对象，`system` / `route` 读取星系及原生往返预算。
`action --action NAME --arguments JSON` 不经过模型，使用相同的安全提交门和任务账本；支持 build、belt、sorter、transfer、configure_recipe、configure_storage、voyager_flight。每个提交仍需配置 allow_write、命令 --enable-write 与本地 yes。

单体建筑、原生网格路径和分拣器必须读回实际正数对象 ID、几何和连接；配方与箱子容量必须读回实际设备配置。转移分别核算 count/inc：无增产点数允许有界部分转移，带点数仅允许完整物品存量，以避免猜测原生分堆回执。储罐只支持原生库存观察与客户端配额，缺少原生写映射时 Unsupported。

Voyager 默认禁用，用户必须提供精确本地路线/返航/人工恢复审查授权；原生能量、曲率资源、返航阈值和期限缺一即停。账本按会话/星系/星球隔离并保留全局 pending；恢复只读核销原 action，不自动 reload，也不把回家着陆当成原生保存完成。完整命令、配置和人工观察步骤见 [MANUAL-ACCEPTANCE.md](MANUAL-ACCEPTANCE.md)。

## 未完成

未进行真实 LLM 请求、MCP/Plugin 游戏联调或实际制造。未实现原生 Anthropic/Gemini 协议适配（兼容网关可配置）、供应商能力探测、运行中热切换模型、持久对话规划恢复、Web 控制台、跨节点自动规划、任意蓝图/物流网络、自动存档恢复或自动建成戴森球。后续每种写入都必须单独实现其原生 prepare/commit/终态/材料/拓扑验证，不能通配开放 MCP 写工具。

本地数据库包含游戏状态和目标，请自行保管；默认忽略配置、密钥文件和账本，提交仅含示例配置与源码。
