# MCP 使用与设置(小白版)

> 一句话:**MCP 是「AI 工具的 USB-C 接口」** —— 工具方按协议做成一个 MCP server,coding agent(opencode 等)插上即用,不用为每个 agent 重写一遍集成。这份文档讲四件事:MCP 是什么、RootRecall 交给 agent 的四层东西怎么配合、为什么别家的 MCP「免接线」而本仓要「接线」、以及从这次调研里沉淀的 MCP server 设计最佳实践。

## 一、MCP 是什么:USB-C 比喻

在没有 USB-C 的年代,每个设备一根专用线:相机一根、硬盘一根、手机一根,抽屉里缠成一团。在 MCP 出现之前,AI 应用接工具也是这样:每个 agent 给每个工具写一套专门集成,写一次只能一家用。

**MCP(Model Context Protocol)** 是 Anthropic 开源的开放协议,把这些「专用线」统一成一根通用线:工具方把能力做成 **MCP server**(一个遵守协议的进程),任何支持 MCP 的 agent —— opencode、Claude Code、Cursor、Codex……—— 都能直接连。OpenAI、Google 相继跟进,MCP 已是事实标准。

三个角色,像一栋办公楼里的分工:

| 角色 | 是谁 | 干什么 |
|---|---|---|
| **Host**(宿主) | opencode | 大管家:接收提问、决定调哪个工具、组织回答 |
| **Client**(客户端) | 管家派出的联络员 | 一个 server 配一个联络员,负责传话 |
| **Server**(服务器) | RootRecall | 工具方:亮出工具清单,按调用干活、返回结果 |

Host 和 server 之间走 JSON-RPC 消息。传输方式两种:

- **stdio**(本仓用的):server 是 host 拉起的一个本地子进程,通过标准输入输出对话。像**上门私厨** —— 厨师直接进自家厨房做菜,快、私密、零网络配置。
- **Streamable HTTP**:server 是一个远程服务,多个客户端可以共享。像**云端餐厅** —— 谁都能点单,适合团队部署。

## 二、四层能力:一个工具型项目交给 agent 的东西

RootRecall 不是一个「装好就能用」的 exe,它交给 opencode 的是四层东西,缺一层都少一块能力:

| 层 | 是什么 | 比喻 | 本仓对应 |
|---|---|---|---|
| **MCP 工具** | 可被模型直接调用的函数,带名字、参数说明、返回结果 | **手** —— 能做的动作 | 17 个工具:`search_codebase` / `memory_recall` / `validate_patch`……见 [mcp-tools.md](mcp-tools.md) |
| **skill** | 一份 `SKILL.md` 说明书,教模型「遇到什么问题、按什么顺序、组合哪些工具」 | **菜谱** —— 知道先切菜还是先热锅 | 8 个 skill:`bug-rca` / `backport` / `onboarding`……路由判据见 [skill-routing-matrix.md](skill-routing-matrix.md) |
| **agent block** | 预制角色:指定模型 + 权限 + 禁令(比如只读 skill 禁 bash) | **工牌** —— 能进哪个车间、能碰哪台机器 | 10 个 block:8 个 subagent(`rootrecall-bug-rca` / `rootrecall-compare`……,要硬门隔离或点名时委派)+ 2 个隐藏内部 stage(`rootrecall-localize` / `rootrecall-repair`,老 delegate 流水线专用) |
| **配置/接线** | 让以上三样被 opencode「发现」的注册动作 | **入职引导单** —— 新员工第一天该去哪报到 | `install --global`(推荐)+ [quickstart.sh](../scripts/quickstart.sh);项目级备选 [wire_opencode.sh](../scripts/wire_opencode.sh) |

业界共识是**工具和菜谱要配套用(use both)**:工具给「能力」,skill 给「流程」。只有工具,模型知道能做什么但不知道标准工序;只有菜谱没有工具,模型知道工序却没家伙可使。philschmid 的总结一针见血:*"Skills complement MCP by teaching agents when and how to combine those tools for specific workflows. Use both."*

两层各吃多少上下文,差别很大,靠**渐进披露**控制:

- 工具的**名字 + 描述(schema)常驻**上下文 —— 模型随时要决定「调不调它」,所以必须一直看得见。这也意味着工具越多,固定开销越大。
- skill 的**元数据(约百来个 token)常驻**,正文只在触发时才读,引用文件按需再读 —— 所以 skill 可以写得很厚而不占日常开销。

## 三、别人家的 MCP 为什么「免接线」

接别的 MCP(比如 Playwright、Context7)只在 opencode 全局配置里写一段就完事,在任何目录启动都能用;RootRecall 历史上却要「从本仓根启动」或「先接线」。差别不是玄学,是三件结构性的事 —— 想明白这三件,也就明白 RootRecall 的全局注册是怎么把它们逐一解掉的:

| # | 别人家的 MCP | RootRecall | 怎么解 |
|---|---|---|---|
| 1. 注册位置 | 在**全局配置** `~/.config/opencode/opencode.json` 注册一次;opencode 配置分层合并(全局层 → 项目层,只覆盖冲突键),处处可用 | 同款:`install --global` 把 `mcp.rootrecall` 块合进全局配置 | ✅ 已同构 |
| 2. 启动命令 | **自足**:`npx -y xxx` / `uvx xxx` 从全局缓存拉起,启动目录无关 | `uv run rootrecall mcp serve` 按**当前目录**找本仓 `.venv` | ✅ 注册块里写死 `cwd`(绝对路径锚回安装根)—— 目录依赖在注册时一次性解决,不用 `uvx` 自足分发 |
| 3. 有无家当 | **无状态**:不依赖固定目录的数据;真需要路径就当参数传 | 三样**家当锚在安装根**:`.venv`(运行环境)、`data/`(记忆库 + 索引,绝不能漂)、`.env`(密钥) | ✅ `cwd` 锚定 + 进程自加载 `.env`;`data/` 想迁出用 `ROOTRECALL_HOME`(见 [configuration.md](configuration.md)),迁完重跑一次 `install --global` 透传 |

打个比方:**`npx` 型 MCP 像外卖店** —— 店开在全局缓存,客人坐哪个目录点单都行;**RootRecall 像自家厨房** —— 锅碗(`data/`)、灶具(`.venv`)、秘方本(`.env`)都锁在厨房里。全局注册相当于**把厨房的地址登记进总机**(`cwd` 字段):不管你在哪下单,外卖员都认得路回这家厨房出餐。

顺带一提:本仓 17 个工具都接受 per-call `codebase` 参数,和 Serena 的 `--project` 是同一族思路 —— 路径当参数传,数据不搬家;但 `.venv`/`.env` 这些「厨房基础设施」还是得靠 `cwd` 锚定解决。

## 四、本仓的三种接入姿势

### 姿势 ① 全局注册(推荐):装一次,任意目录直接问

```bash
uv run rootrecall install --global
```

一条命令干四件事:8 个 skill 软链进 `~/.config/opencode/skills/`、`mcp.rootrecall`(带 `cwd` 锚)合进全局 `opencode.json`、10 个 `rootrecall-*` subagent 定义(「逃生舱」委派的实体,不合并则任意目录 `@` 点名解析不到)同样合进全局 `opencode.json`、路由表以**标记段落**写进 `~/.config/opencode/AGENTS.md`。之后 `mkdir 任意bug目录 && cd && opencode`,停在默认界面直接一句话提问,agent 按路由表自动载入 skill —— 「空 bug 目录 + 一句自然语言 → 自动开仓 → 根因 + 补丁」的全链就是在这条路上验证的。

工程细节:幂等(重跑同步升级,换目录重装会自动把旧安装根的 skill 软链换到新根);`--uninstall` 只摘自己写的,别人的配置绝不动;安装时 shell 里设了 `ROOTRECALL_HOME` 会透传进 mcp 块的 environment。

代价也要讲清:opencode 的**每一个**会话 —— 哪怕和 RootRecall 毫无关系的项目 —— 都会常驻这 17 个工具 schema + 8 个 skill 元数据 + 10 个 subagent 定义 + 路由表。两个减负旋钮:`ROOTRECALL_MCP_TOOLS`(预设 `minimal` 8 个 / `research` / `full`,未注册的不进 tools/list,真省上下文);实在介意 AGENTS.md 全局注入的,退姿势 ③。

> 决策演化(诚实记录):早期拍板过「按仓接线、不设全局」—— 理由就是上面这笔「过路费」。后来真实用法站在了反面:主场景是「在任何 bug 目录一句话」,每仓接一遍线的摩擦比 schema 常驻更贵,且工具门控把常驻成本压到了可控;F2 落地 `install --global` 后经多轮真机 e2e 反转拍板。代价没消失,只是换来换去选了更值的一边。

### 姿势 ② 从本仓根启动(零接线,装好后的第一条路)

```bash
cd RootRecall && opencode
```

启动目录 = 厨房本身 —— `uv run` 就地解析 `.venv`,skill 从 `.claude/skills/` 自动发现,`.env` 由 rootrecall 进程启动时自行加载,什么都不用额外做。仓库根本身的 `AGENTS.md` 会被 opencode 注入系统提示,默认界面直接提问即自动路由(8 个 `rootrecall-*` 模式已从 Tab 列表撤下,改为后台 subagent 供 `@` 点名或硬门隔离时委派)。

### 姿势 ③ 项目级接线(备选)

调试系统软件时,工作现场往往在 bug 仓(比如一份 wpa_supplicant 检出)。不想全局注入、或无权写 `~/.config` 时,把三根线拉到具体目录:

```bash
bash scripts/wire_opencode.sh /path/to/bug仓 [--codebase <索引名>]
```

- **门 1(skill 发现线)**:opencode 从启动目录沿 git worktree 向上爬找 `.claude/skills/`;脚本给 bug 仓放一个软链,指向本仓的 `.claude/skills`,8 个菜谱就地可见。
- **门 2(路由指令线)**:软链一份 `AGENTS.md` 指向本仓根的同名文件 —— 默认界面直接提问时,agent 靠这张「点单对照表」判断该载入哪个 skill。单源真相:改本仓一份,所有接线过的 bug 仓同步生效。
- **门 3(MCP 锚定线)**:脚本在 bug 仓生成一份 `opencode.json`,里面用 `mcp.rootrecall.cwd` 把 rootrecall 服务器进程**锚回本仓根** —— `.venv` 找得到、`data/` 不漂、`.env` 照常自加载。

安全性:幂等(重复跑无害);bug 仓已有自己的 `opencode.json`(不含 rootrecall)时**备份成 `.bak` 后跳过**,绝不覆盖别人的配置;也不穿透软链写文件。接完 `cd <bug仓> && opencode`,`opencode mcp list` 应见 `rootrecall ✓ connected`。同款还有 `rootrecall here`(轻量:只写 `.rootrecall.yaml` 默认检索库标记 + 项目 opencode.json,配合全局注册用)。

## 五、设计 MCP server 的最佳实践(调研汇总)

这是对 MCP 官方规范、Anthropic 工程博客、philschmid 实践文的调研沉淀,每条都标了本仓的落地情况。

### 工具怎么设计

- **面向结果,不面向操作**:别把 REST 端点 1:1 包一层;把「查影响面」这类多步操作合成一个高层工具,让模型一句话完成意图。本仓 17 个工具全是这个粒度(`blast_radius` 内部做完 BFS,不暴露走图原语)。
- **数量克制**:业界建议单 server 5–15 个工具、全局 3–5 个 server / 30–50 个工具封顶。工具 schema 常驻上下文,堆多了挤占正事 —— 有实测案例工具定义吃掉约八成上下文;Claude Code 的 ToolSearch 延迟加载就是治这个的,可省约 85% 相关 token。本仓 17 个略超单 server 建议,但全在「代码情报 + 记忆 + 硬门」一个域内、远低于全局上限,🟡 继续加工具时优先合并而不是新增。
- **命名 `{服务}_{动作}`**:opencode 自动给工具加 server 名前缀(`rootrecall_search_codebase`),调用方一眼可辨来源。规范硬性要求:名字 ≤128 字符、仅字母数字与 `_` `.` `-`、server 内唯一;**工具列表顺序保持稳定** —— 顺序一变 prompt cache 全失效,白花钱。
- **description 就是 prompt engineering**:写给「第一天上班的新员工」看 —— 何时该用、何时别用、参数怎么给、返回长什么样。Anthropic 自述**仅靠打磨工具描述**就拿过 SWE-bench 同期最佳;本仓工具描述已达「够用」,🟡 还欠一轮用 opencode e2e 真实调用记录回喂打磨(记 backlog)。
- **错误要教学**:用 MCP 规范的 `isError: true` 返回**可行动**的修正提示(「分页已到末尾,共 N 条」),不甩裸报错。模型读得懂的错误能自我纠正。
- **分页 + 诚实截断**:大返回给 `limit`/`offset` + 是否还有下一页的显式提示;必须截断时说明截了多少、去哪补齐。本仓 `memory_dump` 的分页与五个工具的 `_honest_truncate` 就是这条的落地(静默截断曾真踩过坑:体检 skill 被截掉一半记忆,靠 13 次补捞才救回来)。

### skill 怎么写

- skill 已是**开放标准**(agentskills.io,Claude Code / Codex / Cursor / opencode 等 40+ 客户端都认):一个目录 + `SKILL.md`(frontmatter 写 name/description),可选带 `scripts/`、`references/`。
- 三级渐进披露(元数据 → 正文 → 引用文件)是省 token 的关键;正文超 500 行就该拆引用文件。
- **skill 的受众是模型不是人**:指令式写法(「第 3 步做 X,若 Y 则跳到第 5 步」),别写项目内部八卦(本仓踩坑 #13)。

### 分发与路径锚定

- 传输:本地工具用 stdio(零网络配置),团队共享服务用 Streamable HTTP。本仓两者都支持,默认 stdio。
- 路径锚定三派:CLI 显式参数(Serena `--project`)/ 配置 `cwd` 字段(本仓门 2)/ 数据放仓内目录(本仓 `data/`)。没有对错,按家当多少选。

### 别和宿主比手艺

宿主 agent 自带的工具(read/grep/bash)往往比 MCP 里重新包一遍的更灵活。Serena 的做法是**检测到自己跑在 coding harness 里就禁用自家 read/grep** —— 不抢宿主的活。本仓踩坑 #2 同一教训:RootRecall 只做宿主没有的(记忆 / 结构图 / 硬门验证),读文件改代码的活全归 opencode。

## 六、常见问题速查

- **在 bug 仓启动,为什么别的 MCP 不用接线,RootRecall 要?** → §三:三样家当(`.venv`/`data/`/`.env`)锚在安装根;全局注册用 `cwd` 把进程锚回去(姿势①),项目级接线则是拉三根线:skill 发现 / 路由表 / 进程工作目录(姿势③)。
- **接线会不会动到 bug 仓自己的 opencode.json?** → 不会。已有且不含 rootrecall 的配置备份成 `.bak` 后跳过;软链不穿透写;幂等可重跑。
- **忘了接线也没全局注册,在 bug 仓启动会怎样?** → MCP 拉不起来(`uv` 在 bug 仓找不到 `.venv`)、skill 发现不了 —— 只是「连不上」,没有任何破坏;回本仓根启动、补跑接线脚本、或 `install --global` 任选其一。
- **全局装行不行?** → 行,且是推荐姿势(§四①)。代价(所有会话常驻工具 schema + 路由表)与两个减负旋钮(`ROOTRECALL_MCP_TOOLS` 裁剪 / 介意注入退项目级)同节;决策演化有诚实记录。
- **`codebase` 参数该传什么名?** → 两套命名:**检索/情报类工具**(search_codebase、blast_radius、call_chain、repo_map、repo_overview、cross_version_diff、merge_eval)传「项目-版本线」名(when_introduced / validate_patch 等按 repo_path 工作,不吃 codebase 参数)(如 `wpa-v25`,即索引名);**记忆类**(memory_recall / memory_memorize / memory_dump)传「项目名」(如 `wpa`)。原因:记忆按 codebase 标签隔离,传版本名会把教训锁进版本孤岛 —— v20 会话永远翻不到 v25 记下的东西;版本上下文写进 summary / evidence 即可。想裁剪注册的工具数 → `ROOTRECALL_MCP_TOOLS`(见下一条)。
- **17 个工具全注册太占上下文,能只开一部分吗?** → 能。环境变量 `ROOTRECALL_MCP_TOOLS` 门控注册:预设 `minimal`(find_repo 开仓查表+记忆3+search_codebase+硬门3)/ `research`(find_repo+记忆3+情报8)/ `full`(默认,17 个),或显式逗号清单(如 `memory_recall,validate_patch`)。写在 opencode 的 `mcp.rootrecall.environment` 里即可。没注册的工具不进 tools/list,模型看不见 —— 真省上下文(permission deny 只是调不了,schema 照占位)。
- **17 个工具各是什么?** → [mcp-tools.md](mcp-tools.md);8 个 skill 怎么选 → [skill-routing-matrix.md](skill-routing-matrix.md);配置项详解 → [configuration.md](configuration.md)。

## 参考链接

- MCP 规范与文档:<https://modelcontextprotocol.io>
- opencode 配置(分层合并 / MCP 注册):<https://opencode.ai/docs/config/> 与 <https://opencode.ai/docs/mcp/>
- opencode Skills(发现路径 / 权限):<https://opencode.ai/docs/skills/>
- Agent Skills 开放标准:<https://agentskills.io>
- Anthropic 工程博客(渐进披露 / 工具设计):<https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills>
- philschmid《How to correctly use MCP servers with your AI Agents》:<https://www.philschmid.de/mcp-best-practices>
- cra.mr《MCP, Skills, and Agents》科普长文:<https://cra.mr/mcp-skills-and-agents/>
