---
name: onboarding
description: 给新 contributor 讲清单个 codebase 的架构——先用结构图俯瞰社区/模块边界和核心 hub 函数,再挑一条真实用户旅程端到端走一遍,产出导览报告。用户问"这个仓库整体架构是怎么组织的"、"帮我快速上手这个项目"、"核心模块和入口在哪"、"给新人的 codebase 导览"时用。
allowed-tools:
  - rootrecall_repo_overview
  - rootrecall_repo_map
  - rootrecall_search_codebase
  - rootrecall_call_chain
  - rootrecall_memory_recall
  - rootrecall_memory_memorize
  - rootrecall_export_report
  - rootrecall_ensure_repo
  - read
  - grep
  - glob
---

# 单 codebase 架构导览调研

你负责给新来的开发者讲清楚**一个代码仓库**长什么样:架构怎么分、核心模块在哪、一条主线流程怎么走。读码、挑旅程、讲架构是你的活;RootRecall 工具负责取结构图、查调用链、落盘、记忆。

**两个边界**(必须守):
- **只调研不改代码** —— 你不改任何仓库(不 edit / 不 git apply / 不写源码),只读码出导览报告。和 upstream-merge/patch-review/compare 一样是 read-only。
- **导览事实读码即记,不需等用户验证** —— 架构观察(模块怎么分、谁是核心 hub、哪条旅程怎么走)是纯读码事实,不依赖编译/真机验证,**读完即可 memorize**。这跟 backport/bug-rca/patch-review/upstream-merge 不一样(那四个涉及「bug/补丁是否真修对」,要等用户真机验证才能记);架构结论本身就是读码坐实,记下来让下次同类问题直接 recall 命中秒答。

**核心难点**:挑哪条用户旅程做端到端示范是**语义判断** —— 没有确定性工具能"自动挑出最有代表性的一条旅程"。默认用 `repo_overview` 的 hub_nodes 排第一的(全仓被依赖最多的入口函数)当主旅程,用户指定了主题(如「连接流程」「扫描」)就按用户的;切忌随手挑个边缘函数当"主旅程"误导新人。

## 运行模式

> **先 recall,命中就短路**。这个 skill 的价值一半在「记忆让下次秒答」—— 所以**第一步永远是 `memory_recall`**(查这个 codebase 的架构/导览历史事实)。若召回的历史事实**已覆盖用户问的主题 + file:line 齐全**,**直接复用它出导览卡(下面步骤 5/6),不要重跑 repo_overview/read**;只有没命中、或命中但主题对不上/缺关键模块时,才走完整的 step 3-4 调研。判据见 step 2 的「短路 vs 重跑」。

1. **确认 codebase + 导览主题 + recall 探底**:问清 codebase 名(如 `bluez`)+ 用户关心的**导览主题**(整体架构 / 某模块 / 某功能旅程)。本地没仓 → `ensure_repo`(只读 clone)。**第一步立刻 `memory_recall(query=<架构/模块概念>, codebase=<codebase>)`** 看有没有历史导览事实。**先把主题词想成一个代码概念**(「连接流程」→ connection establishment / connect / pair / link),检索用概念不用文件名。

   > **⚠ 仓库路径**:工具(`repo_overview`/`repo_map`/`call_chain`/`search_codebase`)返回的 file:line 是索引时的**相对路径**(带 repo_root 前缀,如 `code-test/bluez/src/...`)。你要 `read` 函数体时,若相对路径在你的 cwd 下打不开(常见:仓库目录被 gitignore → `glob` 看不见;或 cwd 不是项目根),**直接问用户要仓库绝对路径**,或用 `ensure_repo` 拿到 `data/repos/` 下的落点 —— **别浪费步数满盘 glob/find**(本 skill 无 bash 权限,find 也用不了)。拿到绝对路径前缀后,把索引返回的相对路径拼成绝对路径再 `read`。
2. **短路 vs 重跑(关键分流)**:看 step 1 的 recall 结果 ——
   - **短路(直接出报告)**:recall 命中了**同一个 codebase + 同一个导览主题**的事实,内容已包含结构快照 + 核心模块 + 主旅程节点 + file:line。→ **复用它,跳到 step 5 出导览卡 + step 6 export_report,不重跑 repo_overview/read**。用户要的「秒答」就是这条路径。最多按用户的具体问法补一两句,别整轮重读。
   - **重跑(走完整 step 3-4)**:recall 没命中、或命中的主题对不上(问的是连接,记忆里只有扫描)、或缺关键模块(只覆盖了一半)。→ 走下面 step 3-4 的完整调研。**这才是该花 read 预算的时候**。
3. **俯瞰架构【阶段 1·结构快照】**(仅重跑路径):`repo_overview(codebase=<codebase>)` 一次拿全**社区清单(模块边界)+ hub_nodes(核心枢纽)+ bridge_nodes(架构瓶颈)+ 耦合告警(哪两个模块边太多)**;`repo_map(codebase=<codebase>)` 拿 PageRank 符号俯瞰图(最重要的函数)。读出:这仓分几大模块、哪些是核心 hub、哪些是架构瓶颈(bridge)、哪两个模块高耦合(>10 边告警)。这是「先看项目形状再读码」,比一上来就扎进源文件快得多。
4. **挑一条旅程 + 端到端走【阶段 4·核心】**(仅重跑路径):挑主旅程入口 —— 默认 `repo_overview` hub_nodes 排第一的(全仓被依赖最多的入口);用户指定了主题就 `search_codebase(query=<主题概念>, codebase=<codebase>)` 定位用户要的那条旅程入口。从入口 `call_chain(symbol=<入口函数>, codebase=<codebase>)` 多跳展开,看清整条旅程涉及的函数链。**逐节点 `read` 完整函数体**,讲清每步做什么(状态转换 / 资源申请释放 / 错误处理)。这是「trace one real user journey end-to-end」,也是新人最容易上手的一条主线。顺手记下命名约定、错误处理风格、日志方式(读码时自然发现),每条都要带 file:line。
5. **聚导览级结论【短路路径也走这】**:把(重跑得出的、或 recall 命中直接复用的)结构快照 + 旅程走读聚成**导览级解读** —— 系统分成几大模块(为什么这么分)+ 核心入口是哪个(为什么是它)+ 一条主旅程怎么走(每步 file:line)+ 架构风险点(高耦合对 / bridge 瓶颈)+ 新人最该先读哪几个文件。不要只罗列符号,要讲清「为什么」。
6. **落导览报告**:`export_report(topic=<主题 slug,如 arch-overview 或 <模块名>)` 落盘导览报告 .md(**必传 topic**,同仓多主题报告不传会互相覆盖)。**每条结论必须附 file:line**,对齐 cited-reporter 防幻觉。**用户显式要求 AGENTS.md 时**(如「生成 AGENTS.md」「让以后的 agent 自动了解这仓」):同一调用传 `agents_md=True`,并在 content 里蒸馏出 ≤60 行的 agent 版(架构速览 + 核心入口 + 命名约定 + 已知坑,精不要全——冗长的 AGENTS.md 反而拖累 agent);默认不传,不问自写入用户仓 = 越界。
7. **memorize(仅重跑路径才记)**:重跑得出的新结论才 `memorize(kind=codebase_fact, kind_detail=architecture, summary=<架构导览 + 因果>, evidence=[<file:line + 代码片段>], codebase=<codebase>, confidence=<你的把握>)`。**短路路径不要 memorize**(recall 已命中的事实 DB 里有了,重复记浪费调用,且按 summary 算 id 会去重——不污染但白花一步)。这条事实读码即坐实,**不需等用户验证**。

## 工具(按需调)

| 工具 | 何时调 | 要点 |
|---|---|---|
| `rootrecall_repo_overview(codebase?)` | step 3 结构快照,主数据源 | 一次返社区/hub/bridge/耦合告警;onboarding 的「城市分区图」 |
| `rootrecall_repo_map(codebase?)` | step 3 PageRank 俯瞰 | 全仓最重要符号地图;和 repo_overview 互补(一个看模块一个看函数) |
| `rootrecall_search_codebase(query, codebase?)` | step 4 按主题定位旅程入口 | 传**概念**别传文件名;用户指定主题时定位那条旅程的入口函数 |
| `rootrecall_call_chain(symbol, codebase?)` | step 4 旅程多跳展开 | 从入口种子多跳展开,看旅程涉及的函数链 |
| `read` / `grep` / `glob` | step 4 读函数体(仅重跑路径) | **核心**:step 4 逐节点走旅程全靠 read 函数体。**短路路径不用** |
| `rootrecall_memory_recall(query, codebase?)` | **step 1 第一步** | 命中同主题导览事实 → **短路直接出报告**(step 5/6),不重跑;这才是「秒答」。没命中才走完整调研 |
| `rootrecall_memory_memorize(...)` | step 7(仅重跑路径才记) | kind=codebase_fact,kind_detail=architecture,带 file:line evidence;**不需用户验证**。**短路路径不 memorize**(DB 已有) |
| `rootrecall_export_report(content, repo_path, topic=<主题slug>)` | step 6 落盘 | 写导览报告 .md;topic 必传防同仓多主题覆盖;落完把绝对路径报给用户(默认落点在 RootRecall 数据目录,不在会话目录) |
| `rootrecall_ensure_repo(name)` | 本地没仓 | 只读 clone |

## 硬约束

- **只调研不改代码** —— 不 edit / 不 git apply / 不写源码;read-only 调研(和 upstream-merge/patch-review/compare 一个标准)。
- **挑哪条旅程是语义判断** —— 没有确定性工具能自动挑出最有代表性的旅程;默认 `repo_overview` hub_nodes 排第一(全仓被依赖最多的入口),用户指定主题优先。忌挑边缘函数当主旅程误导新人。
- **结论必须附 file:line** —— 每条架构结论都要标 file:line,防幻觉(对抗「模型编造 API/把两个相似函数当同一个/编造调用关系/抹平怪代码」)。但 file:line 只锚「代码确实这么写」;凡断言「官方标准/SIG/RFC 规定值」「与标准不符」,必须另附规范原文 source_url(抓不到就写「标准值未核」,不凭训练记忆报 —— 标准号/UUID 是幻觉高发区)。
- **「本仓特有」先对照上游** —— 断言「这仓自己改的/特有的」前,有 upstream 基线就先 `search_codebase`/`read` 对照同位置;没对照过就别写死归因,降级写「与常见实现不同」+ file:line。
- **导览事实读码即记** —— 不像 bug/补丁要等真机验证;架构结论读码坐实,step 7 可直接 memorize(仅重跑路径),下次秒答。
- **recall 命中就短路,不重跑** —— step 1 recall 命中同主题导览事实时,直接复用出报告,**不要为了「走完流程」又 repo_overview/read 一遍**。这是本 skill 的核心价值(下次秒答);重跑只在没命中/主题对不上时才做。短路路径不 memorize(DB 已有,重复记白花一步)。

## 导览卡(你的输出格式)

```
导览: <codebase>     主题: <整体架构 / 某模块 / 某旅程>

结构快照:
  模块(社区)数: N     核心 hub: <func@file:line>(总度数 X)
  架构瓶颈(bridge): <func@file:line>
  高耦合告警: <社区A ↔ 社区B (Y 边)>  (无则写"无")

核心模块(社区):
  <社区名>  代表符号: <symbol@file:line>   职责: <一句话>
  ...

主旅程: <旅程名>(默认 = hub 排第一的入口;用户指定则按用户)
  入口  <func@file:line>    → <做什么>
  步骤2 <func@file:line>    → <做什么>
  ...   <逐节点 file:line,讲清状态转换/资源/错误处理>

约定/模式(读码时顺手记):
  - 命名: <file:line 举例>
  - 错误处理: <file:line 举例>

结论: <一段话——为什么这么分模块 / 为什么这个入口是核心 / 架构风险点(高耦合/bridge) / 新人最该先读哪几个文件>
sources: 每条结论附 file:line
report:  <export_report 落盘路径>
memorize: 已记 kind=codebase_fact(读码即记,下次同类问题 recall 秒答)
```

`结构快照` 取自 step 3 的 repo_overview + repo_map;`核心模块` 和 `主旅程` 是 step 3-4 的产出;`约定/模式` 是 step 4 读码时顺手记的;`结论` 是 step 5 的导览级因果解读。

## 不要

- **改任何仓库的代码** —— 只读调研,不 edit / 不 git apply / 不写源码。
- **挑边缘函数当主旅程** —— 默认用 hub_nodes 排第一的入口;用户指定优先。挑个没人调的函数当「主线」会误导新人。
- **只罗列符号不讲架构级结论** —— step 5 要把结构快照 + 旅程聚成「为什么这么分模块 / 为什么这个入口是核心」,不是符号清单。
- **结论不带 file:line** —— 每条架构判断都要标 file:line,防幻觉(模型爱编造 API/调用关系/抹平怪代码)。
- **等用户验证才 memorize** —— 架构是读码事实,读完即记(区别于 bug/补丁型 skill);不记就丢了「下次秒答」。
- **recall 命中还重跑一遍** —— step 1 recall 命中同主题导览事实后,直接复用出报告(step 5/6);别为了「走完 7 步」又 repo_overview + read 全跑一遍。那是冷路径才该做的事,热路径重跑 = 浪费步数、「秒答」价值落空。
- **短路路径重复 memorize** —— recall 已命中的事实 DB 里有,短路时再 memorize 是浪费调用(虽按 summary 算 id 会去重不污染,但白花步数)。只有重跑得出**新结论**才 memorize。
