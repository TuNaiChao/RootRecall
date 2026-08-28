---
name: dsh-integration-plan
description: 2026-08-19 调研:RootRecall 三支柱挂 deepseek-harness 的落地方案 —— T0 零代码(mcp-client 一行 + wire_dsh.sh 渲染 skills)→ T1 薄 TS 插件(recall 预注入/consolidate 自转/export 硬门政策);工具名映射 mcp__rootrecall__*;含 dsh 扩展点全景与已核实事实
metadata:
  type: project
---

# dsh 集成方案交接(2026-08-19 调研,未开工)

## 背景

用户把 DeepSeek 官方开源 agent harness **deepseek-harness**(dsh,0.1.0-rc.7,TypeScript/pnpm monorepo,口号 "everything is a plugin",vendored Cordis 框架)clone 到 `deepseek-harness/`(只读参考,已 gitignore)。需求:**基于 dsh 增加本项目的三个功能**(= 三支柱:代码情报 / 记忆 / skill+硬门)。本卡是完整调研结论,可直接开工。

## 核心判断

- **两步走,不重写**:T0 用 dsh 原生 MCP client + skill 目录**零 TS 代码**全挂(天级)→ T1 写 2-3 个薄 TS 插件把"SKILL 纪律"升级成"harness 机制强制"(周级)。RootRecall 保持 Python 单仓,定位仍是 MCP tool/skill server,dsh 只是又一个宿主(与 opencode 并列)。
- **"能不能作为 dsh 插件"的准确答案**:dsh 插件 = TS npm 包(导出 `name/inject/Config/apply`,注册走 `ctx.tools.register()`/`ctx.on()`,全部可逆 effect 支持热插拔)。RootRecall 是 Python,**不必也不能直接是**——经官方 `dsh-mcp-client` 插件作"外设"接入效果等同。**判断标准:MCP 传得了工具、传不了 dsh 内部事件钩子**——只有要挂内部循环(pre-step 注入/门禁政策)才写 TS 插件。

## dsh 扩展点(已核实,含仓内路径)

- **MCP**:`packages/mcp/mcp-client`(`@deepseek-ai/dsh-mcp-client`),stdio/http 两 transport,一实例连一 server。Config 含 `serverName`(限 `^[A-Za-z0-9_-]{1,32}$`)/`command/args/env/cwd`(`src/index.ts:107-128`);断线指数退避重连 + 工具清单变化自动 re-sync;拉子进程前清洗凭据类与 `DSH_*` 环境变量(`ROOTRECALL_*` 不受影响,key 走 .env 由 RR 进程自加载,已核实无冲突)。
- **工具命名**:`mcp__<serverName>__<raw>`,超 64 字符带 hash 截断(`tools.ts:111-117`)。16 工具全安全(最长 `mcp__rootrecall__cross_version_diff` 35 字符)。
- **Skills**:`packages/skill/` 四包一 seam(provider registry + filesystem + catalog 工具)。发现路径 rank:`.dsh/skills`(100)/`.agents/skills`(200)/`customSkillDirs`(300)/dshHome(400)(`docs/subsystems/skills.md:68-77`)。格式 = `<name>/SKILL.md` 目录束,frontmatter 必填 name(kebab)+description,可选 whenToUse;**按需加载**(首 turn 注目录,模型 `skill({name})` 拉 body)。**已读码核实解析器容忍未知字段**——我们的 `allowed-tools` 被静默忽略不报错(`skill-filesystem/src/index.ts:793-816` 只取已知字段)。
- **Agent preset**(≈ 我们的 opencode agent block):`packages/preset/agent-presets` + `dsh-persona` + `tool-subagent` 的 `toolFilter.allow`(工具白名单)。范式见 `apps/cli/config/agent-presets/standard/agent.cordis.yml`。
- **事件钩子**:`agent/pre-step` 瀑布(改写/拒绝模型输入、确定性注入上下文)、`tools/pre-execute`(allow/deny/ask 政策门)、`turn/end`、`ctx.systemPrompt.section()`、`agent.inject()`。全在 `docs/cookbook/extension-cookbook.md` 的 feature→mechanism 映射表。
- **记忆**:dsh 无内置记忆插件,官方姿势就是 "section provider + tool" 或 MCP(`examples/mcp-memory/` 三份官方 overlay 全是 mcp-client 一行,与我们的路线同构)。
- **Python 侧**:`python/` 是子进程客户端 SDK + 打包 TS runtime,不是第二套 harness;扩展仍是 TS 插件。

## T0:零代码全挂(半天)

1. **挂 MCP**(cordis.yml 或 patch overlay 一行,照抄 `examples/mcp-memory/memorix.cordis.yml`):
   ```yaml
   - id: rootrecall
     name: '@deepseek-ai/dsh-mcp-client'
     config:
       serverName: rootrecall
       transport: stdio
       command: uv
       args: [run, rootrecall, mcp, serve]
       cwd: /home/tnc/Desktop/Agent/RootRecall   # 同 opencode mcp.rootrecall.cwd 机理:锚 .venv/data/.env
       env: { ROOTRECALL_MCP_TOOLS: full }        # 门控照常生效
   ```
2. **搬 skills**:唯一实质改动 = **SKILL 正文工具名** `rootrecall_*` → `mcp__rootrecall__*`(8e524de 命名漂移同类工程)。做成生成脚本 `scripts/wire_dsh.sh`(镜像 `wire_opencode.sh`,sed 前缀替换 + 剥 `allowed-tools` 段),单一事实源防两宿主漂移(踩坑#17 教训)。
3. **preset × 8**(T0.5 可选):persona + toolFilter 复刻 agent block。
4. **验证**:复用归档的 TEST-PLAN 场景(docs/archive/TEST-PLAN.md,金标部分仍有效)换宿主跑 `pnpm dsh --profile headless "…"`(dsh 侧取证看它的 session 事件流 JSONL,比 opencode.db 更友好);**换宿主不换判据**(sdp :1255 / compare 四差异等金标不变)。

## T1:薄 TS 插件(2-3 天,把纪律变机制)

| 插件 | 挂载点 | 替代现在的什么 | 量级 |
|---|---|---|---|
| `dsh-rootrecall-recall` | `agent/pre-step` 瀑布:用户消息提 query → recall → 注入记忆卡 | 路线#4 记忆自动 query 现靠 SKILL 提示 → 变每 turn 确定性预注入 | ~150 行 |
| `dsh-rootrecall-consolidate` | `turn/end` → 异步 consolidate | CLI 手动跑 | ~80 行 |
| `dsh-rootrecall-gate`(可选) | `tools/pre-execute`:同 turn 没 validate 过就 deny export_* | 硬门从模型纪律 → 政策拒绝,跳不过 | ~120 行 |

按 `docs/cookbook/adding-a-tool.md` + extension-cookbook hook 范式;注意 dsh 工程门槛(strict TS / per-file 100% 覆盖门 / 快照测试 / 双语文档同步,见其 AGENTS.md)。

## 明确不做

- **TS 重写服务层**(违背 delegate 原则 + 保不住 311 测资产);
- **改 dsh agent-loop**(微内核红线,动 loop 要重写它的架构文档);
- **上游提 PR**:dsh 是 developer preview 明说会破坏兼容,先做 out-of-tree 插件装自己 profile(`dsh plugin --profile <name>` 支持),等正式 tag 再议。

## 坑位

① 工具名映射是最大搬运成本(SKILL × 8),用生成脚本别手改;② dsh skill 按需加载 → description 是路由依据(踩坑#13 同构,质量更关键);③ 网上 deepseekdocs.com / deepseekharness.io / dsh.deepseek404.com 等域**非官方**(内容农场嫌疑),开发只信本地 clone 的 docs/ 与代码。

关联 [[colleague-onboarding-toolset-handoff]](wire 脚本先例)/ [[opencode-mcp-wiring]](宿主接线坑)/ [[opencode-config-drift]](单一事实源教训)/ [[l2-granularity-prior-handoff]](TEST-PLAN 金标,已归档至 docs/archive/)。
