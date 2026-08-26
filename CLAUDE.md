# CLAUDE.md

> 本文件是 Claude Code(及其他 coding agent)在本项目工作的始终生效上下文。随 git 跨机同步。

## RootRecall 是什么

**给系统软件代码库做「带记忆的 bug 根因定位 + 深度调研」的领域 harness —— 记忆 + 代码情报 + 日志取证 + 补丁验证 + 标准流程 skill,作为 MCP tool/skill server 供 opencode(主)/ codex / claude code 调用。** 不再自己调度 coding agent 跑固定管线(老 bug-RCA orchestrator 降级留参考,始末见本机归档 `docs-bak/设计/harness-pivot-design.md`);差异化在「记忆 + 持续学习 + 精准的工具与菜谱」,重活(读码/改代码)仍归成熟 coding agent(omp/opencode/codex/claude code)。三大支柱:

1. **(P1)代码仓深度调研** — 任意语言仓库(git/本地)→ 详细准确的架构/模块文档;含开源 PR 持续跟踪 + 合入建议(R4)。
2. **(P2)bug 根因定位** ★MVP — 源码 + 日志/漏洞报告 → 根因 + 补丁 + 分析报告;**重活委托** omp/opencode,RootRecall 负责召回+组装精确上下文+调度+沉淀。
3. **(P3)记忆与持续学习** ★特色 — 把"代码库调研知识"和"bug 分析报告"沉淀成可检索、带溯源、团队共享、持续学习的记忆。

三者共享一个**平台 + 共享服务层**(代码理解、记忆、沙箱、检索、可观测),解决三大痛点:① 记忆跨会话;② 省 token(精炼 MCP 工具 + just-in-time 上下文,agent 按需取、不预塞全量);③ 流水线(一条命令跑完 / 一套 skill+工具复用)。Tagline:*Light on every root cause.*

> v2(2026-07-28)产品重规划:从 v0.1"先建深地基再接场景"改为"编排 + 记忆 + 委托"。已建的 code_index(P1.0–P1.5)作为资产保留。v2 全套设计文档(architecture / memory / bug-rca / deep-research)在本机只读归档 `docs-bak/设计/`(未随 git);**随 git 的现状文档**是 docs/ 三篇模块分析(bug 定位 / 代码调研 / 记忆)。踩坑记录(`docs/踩坑记录.md` 开发/设计类 + `docs/测试踩坑记录.md` 测试过程类)2026-08-26 起也只留本机、不随 git。

## ⭐ 工作准则(必读)

**设计任何模块前,先做这两件事,再动手写代码:**

1. **前沿调研** — 用 WebSearch 调研该方向的 2025-2026 最新进展,不要只看旧资料。
2. **参考 deer-flow** — `deer-flow/` 子目录是 ByteDance 的成熟实现(只读参考,`.gitignore` 掉,各自 clone)。精读对应模块,引用具体文件路径与关键代码片段。

综合两者再给方案、再写代码。不要凭空造轮子。

**接 skill 类需求先查路由矩阵**:用户问题该进 8 个 skill 里哪个,见 [docs/skill-routing-matrix.md](docs/skill-routing-matrix.md)(问题形态主表 + 6 组易混对判据 + 工具归属 + 组合场景);别凭感觉挑 skill,易混对(upstream-merge vs backport / compare vs backport / domain-research vs onboarding)都有明确判据。

**实现时对齐 deer-flow,目标生产级(不是 demo):** 各功能优先照齐 deer-flow 对应代码的质量与边界处理;起步可做最小实现,但**必须排期迭代到生产级**。**本项目是生产级项目,不是 demo**——"最小实现"是阶段性手段,不是终点。每处简化都记入 `.claude/memory/backlog-production-grade.md`,后续补齐。

## 仓库地图

```
RootRecall/
├── src/rootrecall/
│   ├── platform/     # ✅ Harness(已实现):模型工厂 / 配置 / 反射 / 沙箱 / 可观测(runtime 中间件)
│   ├── services/
│   │   ├── code_index/  # ✅ 代码理解(已实现 P1.0–P1.5):parser/chunker/embed/store/retrieval/index/lsp/outline/eval
│   │   └── memory/      # ✅ 记忆核心(R1 已实现):MemoryService 契约 + native 后端(SQLite+FTS5+向量)+ tools/mcp
│   ├── workflows/    # ✅ bug_rca(R3.1,降级参考)+ deep_research(R3.2)+ patch_report(P-A 1b);pr_tracker 撤
│   ├── tools/        # ✅ MCP 工具(mcp_memory,17 个给 coding agent)+ delegate(R2,降级参考)
│   └── cli.py        # ✅ 入口(models/index/lsp/memory/mcp/bug-rca/research/patch-report)
├── config/           # config.yaml(模型/沙箱/记忆/委托)+ opencode_rootrecall.json(opencode agent+MCP)
├── docs/             # 模块分析×3 + 参考×4(mcp-tools/configuration/cli/mcp-guide)+ skill-routing-matrix(踩坑记录×2 仅本机,不入库)
├── example/          # demo1/demo2 金标准(输入 wpa + 日志/漏洞 → 补丁 + 报告)
├── scripts/          # setup.sh(系统工具) / setup_claude.sh(记忆软链)
├── .claude/memory/   # Claude Code 项目记忆(随 git 跨机)
├── deer-flow/        # 只读参考(.gitignore)— 架构主脊 + Reporter + MemoryManager
├── oh-my-pi/         # 只读参考(.gitignore)— 委托目标 omp + mnemopi 记忆
└── code-review-graph/  # 只读参考(.gitignore)— 结构图引擎(blast-radius/架构地图)
```

> 状态标记:✅ 已实现 · 🆕 待建(R1 起)。日志符号化(log_symbolizer)/ 静态分析(static_analysis)在 v2 **裁出 v1**(委托给 omp/opencode),记 backlog。

## 模型:多 provider 自适应

不硬编码任何厂家。在 `config/config.yaml` 的 `models:` 每项声明 `use: <module>:<ClassName>`,工厂 `rootrecall.platform.models.create_chat_model` 用反射加载任意 LangChain chat model 类。**加新 provider 通常零代码,只改配置。** 详见 architecture.md §4.1。

## 命令

```bash
uv sync --extra mcp --extra code-review-graph   # 装/同步依赖(+MCP server / 结构图两个产品 extra;两台机一致,靠 uv.lock)
uv run rootrecall models   # 列出配置的模型(验证 config + 工厂加载)
uv run pytest            # 测试
uv run ruff check .      # lint
bash scripts/setup.sh    # 装系统工具(Linux/macOS 自动适配)+ 记忆软链
```

## 两台机协作(Linux + macOS)

- **Python**:uv + `uv.lock` 保证两台一致;`.python-version` 锁定解释器版本。
- **系统工具**:`scripts/setup.sh` 按 OS 分发(macOS 用 brew / Linux 用 apt)。
- **记忆**:每次 fresh clone 后跑 `bash scripts/setup_claude.sh`,把 Claude Code 记忆软链到仓库内 `.claude/memory/`,随 git 同步。建议两台机仓库路径一致(如都放 `~/Desktop/Agent/RootRecall`),记忆 slug 自然对齐。
- **密钥**:`.env`(gitignore)按 `.env.example` 填,勿提交。

## 扩展性

工具是**声明式 + 反射 + 插件**:`config.yaml` 的 `tools:` 声明工具的 `use: <module>:func`,按需加载。domain 工具(bluez/wpa 解析等)放 `src/rootrecall/tools/plugins/<name>/`,在配置里开关,不改核心。

## 路线(v2,2026-07-28 重规划)

**R0** ✅规划落地(文档/裁剪)→ **R1** ✅记忆核心(MemoryService + native 后端 code_index+code-review-graph + MCP + CLI,2026-07-29)→ **R2** ✅bug-RCA MVP(委托 opencode **多阶段** localize→repair + **A+C**:自定义 agent + `steps` 强制收敛 + session 续接;2026-07-30 端到端 delegate 收敛达标,产出报告+补丁+记忆闭环;patch apply + 根因准确性留 R3)→ **R3** 代码仓深度调研 + **workspace_changes**(opencode edit + git diff 根治 patch 格式)+ 多候选/repro(根因准确性)+ runtime 骨架 + CRG(R3.0 runtime ✅ + R3.1 bug-RCA 工具驱动 ✅ + R3.2 深度调研 ✅代码完 2026-08-03,e2e 待跑)→ **R4** ~~团队/多用户(租户隔离 + 鉴权)~~ + **多库**(升级为地基,见下节)+ PR 跟踪(R4.1 PR 批量分析+聚合报告 ✅ 已完成)+ skills/MCP(MCP ✅ D0;skills S1-5 暂缓 YAGNI)→ ~~**R5** 生产化(沙箱 Docker + artifacts + 前端 + 可观测)~~。⚠️ **2026-08-07 pivot 后复核:R4 租户/鉴权 + R5 全部取消,详见下节「路线复核」**。**这些是规划内扩展面,非临时发现**:runtime 从 R3.0 起即保扩展口 —— `create_rootrecall-agent(middleware=...)` 接任意链、create_agent 自动合并 middleware 的 `state_schema`、RootRecallState 是 TypedDict,将来 skills/鉴权/沙箱/artifacts 等「加而不改」(中间件按 **pull-by-need** 加,链 >7 再移植 `@Next/@Prev`;记忆仍走自有 MemoryService,不抄 deer-flow MemoryMiddleware)。runtime 扩展口详见本机归档 `docs-bak/设计/architecture.md` §8。

**三锁定决策:** ① 记忆 = 自有 MemoryService 契约 + v1 native 后端(组合 code_index+code-review-graph),cognee/mem0 可换;② bug-RCA 委托给 coding agent,抽象 `CodingAgentDelegate`,v1 默认 omp,opencode 可换;③ MVP 先 bug-RCA。详见各设计文档。

## 路线复核(2026-08-07 pivot 后,以此为准)

**复核 lens**:RootRecall 收敛成**三件事** —— ① 代码情报(检索/调用链/影响面)② 记忆(bug 教训+代码事实,带溯源持续学习)③ 标准流程 skill+工具(bug-RCA/patch-review/research + apply 验证 + 日志取证)。**不在三件里的 = 偏离 = 砍**。逐项三标准审:① 落在三件事内?② 被「不编译/不复现」影响?③ YAGNI?

**砍/obsolete**:R4 多用户·租户·鉴权(本地 harness 不需要);R5 Docker 沙箱(不编译/不复现→无用途)、前端(harness 无 UI,交互在 coding agent)、artifacts(**并入记忆**不单建)、Tier1 运行时验证(与「不编译」冲突);③ opencode serve persistent(delegate cold-boot 前提消失 + D0 http + lazy 已覆盖);build_check 接回流程(与「不编译」冲突,工具保留按需);按 intent lazy-load MCP 工具(当前 ~15 工具 YAGNI,等 20+);Skill 子系统 S1-5(opencode 原生发现 `.claude/skills/` 已工作,等跨 agent 再建)。

**保留碎片**(并入功能线,不单独成阶段):多库支持(同时多仓刚需→地基性)、可观测增强(可选运维)。

**验证封顶(用户定,强化)**:apply(Tier 0,RootRecall 验)。**编译/测试/复现永不做 —— 全部用户(真机)自验**(系统软件环境重+信号歧义,不值)。`correctness` 基于 apply+读码推理,不报 tested/verified。

**当前核心待做顺序**(用户 2026-08-07 拍板 + 2026-08-10 复核):
1. ✅ **多库地基**(同时多仓刚需;code_index 多实例 + 工具加 codebase 参数 + 记忆全局带 codebase 标签;2a/2b 依赖故前移)—— `7127bb1`
2. ✅ feature 2a 调用链(`call_chain` 工具,CRG 多跳+PageRank)—— `02037c0`,第 10 个 MCP 工具
3. ✅ feature 2b 跨版本 diff(`cross_version_diff`,常用,依赖 2a)—— `8a2c821`,第 11 个 MCP 工具
4. ✅ 记忆自动 query(P1)—— `21d792e`(定位后用 `problem_summary` 召回历史修法;A1 日志摘要被探针证伪转 B)
>
> **#1–#4 全落地(2026-08-11)**;**上游 commit 合入评估 全落地(2026-08-11,第 13 MCP 工具 `merge_eval` + `upstream-merge` skill)**;**architecture-review §五 建议 A/B/C/D 全落地(2026-08-13)**;**功能 1 onboarding 导览 skill 全落地(2026-08-13,第 14 MCP 工具 `repo_overview` + `onboarding` skill)**;**功能 2 记忆体检 skill 全落地(2026-08-13,第 15 MCP 工具 `memory_dump` + `memory-health-check` skill)**。下一优先见「低优 backlog」。
~~原 #1 `filter_logs` 强制注入因果起点行~~ → **2026-08-10 复核撤销**:deer-flow/omp 双证专门日志切片工具没必要(opencode 的 read/grep/awk 等价且更灵活,踩坑#2);治踩坑#11 的领域知识(从更早切/用日志词汇/窗口可能漏根因/重心代码)转进 bug-rca SKILL/prompt,`filter_logs` MCP 工具 + `log_filter.py` 删除。详见踩坑#11。

低优 backlog:~~**上游 commit 合入评估**~~ **✅已成(2026-08-11)** —— 采 `merge_eval` MCP 工具 + `upstream-merge` skill 路线(pivot 对齐:1 薄工具 + 1 skill,不包 fetch_upstream 工具/不建 workflow):`merge_eval` 逐 commit 三态判定(patch-id 等价 → `already_fixed` / merge-tree 或 apply 检查 → `recommend_merge`|`conflict`;2026-08-17 升 `merge-tree --write-tree` 零 touch,checkout 硬门降为 rev-parse),skill 负责拉上游本地 + 查相关性 + 报告;详见 [upstream-merge-handoff](.claude/memory/upstream-merge-handoff.md)。未做的原 sub-need ① `fetch_upstream_commit`(按 SHA 抓单 commit diff)暂不做(agent `git show` 等价,踩坑#2)。 / ~~stdio→http(待 opencode 解注册,踩坑#10)~~ **⚠️ 2026-08-12 obsolete**:不在三件事内(纯传输协议优化)+ 踩坑#10 opencode http MCP 不注册原生工具(上游 bug,RootRecall 改不了)+ cold-boot 痛点已消失(stdio timeout 120s 够,listTools 便宜不加载 embedder);RootRecall 侧 `cli.py` 已支持 http,等 opencode 上游修了零改动可切,不主动做。 / P-A 遗留(~~1b deep~~ 已删参 2026-08-17 ·去重·Gerrit 凭据;~~patch_search CLI 已 2026-08-10 撤~~)/ 委托项(log_symbolizer·static_analysis 归 omp/opencode)/ 生产级补齐 backlog `.claude/memory/backlog-production-grade.md` #1–64(~~#55 obsolete~~;已完成:#22/#23/#54/#57/#58/#61/#62/#63/#64;~~#60 merge-tree apply 升级待触发~~ ✅2026-08-17 已成)。**✅ index 前置门槛已成(2026-08-11)**:`rootrecall index` 一键建向量索引+结构图(`--no-graph` 只建向量;CRG 没装非致命降级);`CodeGraph.impact_radius` 路径容错(CRG 存 `repo_root` 前缀路径,agent 给仓库相对路径→后缀解析,免静默返空);bluez 真机 4 工具全通。详见 [tier2-index-prerequisite-handoff](.claude/memory/tier2-index-prerequisite-handoff.md)。**✅ P-A 遗留第 1 档已成(2026-08-11)**:Gerrit 私仓鉴权(`/a/`+Basic,env `GERRIT_USERNAME`/`GERRIT_HTTP_PASSWORD`)+ URL 分流(`fetcher_for_url` 按 URL 选 Gerrit/GitHub,治静默失败)+ 报告行锚定验证(`diff_hunk_lines`+`verify_and_append` 行级软查,bluez 真补丁探针三态全对);`--deep` 逐 PR ReAct + 跨 PR 语义去重缓(YAGNI)。详见 [tier2-pa-gerrit-lineverify-handoff](.claude/memory/tier2-pa-gerrit-lineverify-handoff.md)。**✅ 跨版本 backport 工作流已成(2026-08-12)**:`backport` skill + `rootrecall-backport` opencode agent block(**0 新 MCP 工具** —— 用户拍板 #1;grep+read 取 v20 函数体够用,symbol_lookup 与 delegate 重叠不建,踩坑#2;按触发再建同 backlog #60)。场景=两独立发行版线(v25 已修→改 v20);**核心差异 vs upstream-merge**:无确定性"判 v20 有 bug"工具(VeriPort arXiv 2606.22704 vulnerability oracle = PoC,RootRecall 永不编译→只能语义判:opencode 读 v20 函数体对照 v25 fix-point = A 方案);`merge_eval` 跨独立线不可用(patch-id 需共同祖先)故 skill 不用它。sdp 真数据探针全绿(step3 grep 定位 `sdp_extract_seqtype` v20 `lib/sdp.c:1222`[v25 路径漂移 `lib/bluetooth/sdp.c`]→ read 函数体判 `:1255` 有同一溢出 → step5 路径/行号适配 → step6 `validate_patch` strict 一次过)。skill 7 步(判 bug【硬门·核心】/ 验 apply【硬门】),只到 apply 不编译,用户真机验证通过后才 memorize。详见 [backport-workflow-handoff](.claude/memory/backport-workflow-handoff.md)。**✅ 跨版本代码对比调研已成(2026-08-12)**:`compare` skill + `rootrecall-compare` opencode agent block(**0 新 MCP 工具**)。填矩阵唯一空白:4 旧 skill(backport/bug-rca/patch-review/upstream-merge)全 bug/补丁导向,无调研/对比型;用户问「v20、v25 蓝牙连接流程有什么差异」这类**跨版本流程对比**就靠它。3 阶段对比法(锚定两版流程入口→语义配对→逐节点读函数体对照,方法论取 Code Researcher ICLR 2026 + Augment 2025 分层检索律);**核心难点=两版函数配对是语义判断**(改名/拆分/合并,各 codebase 结构图独立无联合图,同 backport「判 bug」同构);**刻不带 `cross_version_diff`**(单仓两 ref 专用,两独立仓无效,code_graph.py:152-156 三证)。**memorize 读码即记**——本 skill vs 其他 4 个的核心差异(那些等用户真机验证;对比是纯读码事实读完即记)=「下次秒答」机制(用户拍板)。蓝连流程真数据探针全绿(`device_add_connection` v25 多 `flags` 参 + `btd_bearer_connected` bearer 通知 + initiator 记录,v20 无)。**opencode e2e 全绿:`rootrecall-compare` 自驱 44 工具 16 步,报告落盘 + 2 条 codebase_fact 写 DB(raw 查证非幻觉),agent 自驱结论(主流程 1:1 同构 + 4 控制面差异:bearer 子系统/结构化错误码/权能护栏/LE 状态前置)与手工探针一致**;steps 20→28(读两版函数体需预算);两环境坑(opencode 不读 `.env` 走 shell env / code-test gitignored→glob 看不见)记 [opencode-mcp-wiring](.claude/memory/opencode-mcp-wiring.md)。详见 [compare-skill-handoff](.claude/memory/compare-skill-handoff.md)。 **✅ 代码 onboarding 导览 skill 已成(2026-08-13,architecture-review §六 功能1)**:`onboarding` skill + `rootrecall-onboarding` opencode agent block(steps 24,read-only) + **第 14 MCP 工具 `repo_overview`**(用户拍板:加 1 薄工具,非原规范的「0 工具」——`architecture_overview`/`hub_nodes`/`communities` 是 CodeGraph 已实现的方法不是 MCP 工具,原「0 新工具」前提错;`repo_overview` 聚合这三 + `bridge_nodes` 一次返,纯图查询无 LLM,图驱动防幻觉)。填矩阵唯一空白:5 旧 skill 全 bug/补丁/对比导向,无「给新人讲清单仓架构」纯调研型;用户问「这个仓库整体架构怎么组织 / 核心模块入口在哪」就靠它。onboarding 是**第一个真需「模块/耦合」视角的 skill**(bug-RCA/compare 要具体调用链不是模块布局)——`repo_map`=符号层(PageRank 最重要的函数)、`repo_overview`=架构层(社区/模块边界+枢纽+瓶颈+耦合告警),分层检索。方法论取 theroadtoenterprise 2026-05 六阶段 onboarding 循环(map→stack→patterns→trace journey→spot→document:phase1=repo_overview+repo_map 结构快照,phase4=call_chain+read 端到端走一条主旅程)。**镜像 compare**:memorize 读码即记(架构是纯读码事实不等用户验证,同 compare 区别于 bug/补丁型)+ recall-first 短路(命中同 codebase 同主题导览事实直接复用出报告)+ 核心难点「挑哪条旅程是语义判断」(默认 hub_nodes 排第一,用户指定优先)。2 单测(图未建降级/假图聚合+格式化+top_n 透传)+ 全 mcp_tools 25 绿。**opencode e2e 已真机全绿(2026-08-13 补跑,见 [e2e-validation-2026-08-13-handoff](.claude/memory/e2e-validation-2026-08-13-handoff.md))**。详见 [onboarding-skill-handoff](.claude/memory/onboarding-skill-handoff.md)。 **✅ 记忆体检 skill 已成(2026-08-13,architecture-review §六 功能2)**:`memory-health-check` skill + `rootrecall-memory-health` opencode agent block(steps 16,read-only) + **第 15 MCP 工具 `memory_dump`**(非 spec-drift:本会话查实确无浏览/导出工具,memory_recall 是 query 式 / memory_memorize 是 write,缺一个「摊全量」入口;`MemoryService.list_items` 早已是契约只是没暴露成 MCP → 加薄工具 wrap 它,0 新服务代码)。`memory_dump(kind?, include_invalid?, codebase?)` 包 `svc.list_items`,每条 KI 渲染成溯源卡(confidence/source_tier/evidence file:line/commit_sha/bi-temporal STALE/access_count)。**填记忆能力唯一空白**:从「只能按 query 搜(memory_recall)」补上「能浏览/审计(memory_dump)」—— 用户问「我们对这个仓到底记了啥 / 哪些记忆可信 / 审一下记忆库」就靠它。**差异化卖点**:2025-2026 治理型 agent memory 关键维度 = provenance + confidence + staleness + audit(Atlan/Mem0/OvalEdge/PMC-NIH 多源),RootRecall 的 KnowledgeItem 天生带这套字段(bi-temporal valid_at/invalid_at + source_tier/evidence/commit_sha + access_count + superseded_by),功能 2 把「可审计知识库」做成可见;Mem0/Cognee 没这种带溯源的团队记忆体检(调研坐实)。**比 onboarding/compare 更严的边界**:连记忆库都只读不写(体检只看+建议,不自动删 stale/改 confidence/consolidate,改库是人的活);体检默认不 memorize(不产新知识),唯一例外是发现未决矛盾(两条都 active+高 conf+结论冲突)才记一条「需人工裁决」。核心难点「从 dump 读出四类健康信号(溯源弱/待巩固/已过期/未决矛盾)是语义判断无确定性工具」(memory_dump 只摊数据,读信号靠 agent,同 onboarding「挑旅程」/compare「函数配对」同构)。2 单测(空库降级/假 svc 注入 2 KI 验溯源卡渲染)+ 全 mcp_tools 27 绿。**opencode e2e 真机全绿(本会话自跑)**:`rootrecall-memory-health` 自驱审 wpa 48 条记忆,发现真未决矛盾(同一 P2P scan 泄漏 bug 存两派打架根因)+ memorize 1 条待裁决卡(DB raw 查证非幻觉),守边界(体检只读)。e2e 暴露并修掉 1 个真问题:`memory_dump` 旧 `[:8000]` 截断吞掉一半条目逼 agent 13 次 recall 补捞 → 改 `limit/offset` 分页 + 显式翻页提示(诚实信号不静默截断)。详见 [memory-health-check-handoff](.claude/memory/memory-health-check-handoff.md)。**✅ 纠正关系闭环已成(2026-08-13)**:memory-health-check e2e 暴露 wpa P2P scan 泄漏 bug 存两派打架根因(旧错派 abort-failure 4 条仍 active 高 conf 无纠正标记 vs 新对派 scan-only 覆盖竞态 3 条明写「纠正先前误诊」)→ 补「纠正」维度。**双字段**:`corrects: list[str]`(正向,新条上,**transit 不入库**——写入时消费掉回填旧条)+ `corrected_by: str|None`(反向,旧条上,**持久化** SQLite 列 + 检索降权 `CORRECTED_PENALTY=0.3`)。**不复用 `superseded_by`**(它绑 `active` 属性 + `list_items` WHERE,设了会让被纠正条从 active 视图消失——纠正≠失效,被纠正条仍可检索/体检可见,只降权;独立 `corrected_by` 解耦)。`memory_memorize` 加 `corrects` 参数(agent 显式声明「我纠正了谁」);`memorize_items` 消费它调 `store.mark_corrected`(镜像 `set_invalid` 但不设 `invalid_at`,幂等);`recall._apply_decay_confidence` 对 `corrected_by` 非空条目降权(仍可见作参考,排纠正者后面);`_render_audit_card` + `RecallHit.render()` 加 CORRECTED 标记。**体检 skill 升级闭环已解矛盾**:step 3 发现矛盾先判能不能闭环(一方显式纠正另一方→标 `corrects` 闭环,只留真正未裁的「待裁决」)。调研坐实:Vectorize 四杠杆框架「explicit invalidation 而非 delete,old state recoverable for audit」+ Graphiti/Zep 边级失效 + mem0 v3 contradiction handling(只追加缺的正是这块)。2+1 单测全绿(标旧条/降权/工具参数),ruff clean。**opencode e2e 已真机复检全绿(2026-08-17:4 条 corrected_by 在库 + recall `render()` 带 `(已被纠正)` 标记 + 降权后仍可见)**。详见 [correction-link-handoff](.claude/memory/correction-link-handoff.md)。**✅ 领域知识记忆已成(2026-08-13)**:记忆加第 4 类 kind `domain_knowledge`(semantic memory 语义层,填前三类 codebase_fact/bug_lesson/mental_model 的语义层空白)+ `source_url` 溯源字段(独立字段不进 Evidence,Evidence 是代码锚点签名)+ **第 8 skill `domain-research`** + `rootrecall-domain-research` opencode agent block(**0 新 MCP 工具** —— opencode 自带 `websearch`[Exa AI 免 key]/`webfetch` 内置工具,agent block 授 permission key 即可,踩坑#2 同源教训)。用户三档需求全覆盖:① 记领域知识(蓝牙协议/wpa 各层)② opencode 网调→本项目记 ③ 记用户任意技术笔记。**8 设计决策全据代码证据定**:① domain_knowledge 不自动升级 mental_model(consolidate.py 排除——语义层 evergreen 不像 bug 教训会「毕业」成程序性规则)② kind_detail 加 domain(语义清晰非功能刚需,Plan agent 实证 recall 零引用)③ repo 保持必填(skill 传 codebase 或 "general")④ 不动 extract.py(走专用 skill 不自动抽取)⑤ recall 自动带(recall.py kind-agnostic 0 改,**治踩坑#11 误诊的核心价值**:领域知识进 recall 给 bug-RCA 多层证伪依据)⑥ memory_dump 自动带 ⑦ 用户笔记同 kind + source_tier=stated ⑧ source_tier 按 source_url 有无自动分层(imported 网调 0.6 / stated 用户笔记 1.0;bug/codebase 维持 delegate)。**store.py source_url 6 接触点**(字段清单/DDL/幂等 migration/序列化两向/UPSERT,漏一个静默不持久化,仿 corrected_by 迁移模式)。**镜像 compare/onboarding**:memorize 读码/调研即记(多源交叉印证坐实,不等用户验证)+ recall-first 短路;**核心难点=协议知识真伪是语义判断**(无确定性工具验真,靠 ≥2 独立权威源交叉,区别于 codebase_fact 读码坐实);**克制规则**:只记技术笔记不记流水账(在 SKILL 不在代码)。2 单测(domain_knowledge+url→imported / 无 url→stated)+ 全 mcp_tools 33 绿 + 全记忆 46 绿 + ruff clean。**opencode e2e 真机全绿(2026-08-17)**:rootrecall-domain-research 自驱网调 BLE LE Data Channel L2CAP(7 源交叉:Core Spec 6.1 主源 + 6.2/5.4 版本一致 + Silicon Labs/MathWorks + 内核 l2cap_core.c 代码级印证),报告落盘 + 1 条 domain_knowledge 写 DB(`source_tier=imported` + `source_url` 指官方 spec,DB raw 查证非幻觉);**recall 闭环验证**:新条目以最高分命中排在 bug_lesson 前(「领域知识进 recall 治踩坑#11 误诊」价值命题真数据起效);agent 还自查出 LE/BR-EDR「connectionless CID 0x0002」概念澄清防误套。详见 [domain-knowledge-handoff](.claude/memory/domain-knowledge-handoff.md)。 **✅ P1/P2 两支柱分析 + 改进落地(2026-08-14/17)**:三篇文档(代码调研/bug 定位模块分析 + backlog,commit e6492df)+ 🔴 高优先 4 项(7535527,CRG 建图降级/双纪律/诚实截断,**e2e 真机全绿**金标逐点吻合)+ 🟡 5-8 全落(#7 第 16 MCP 工具 `when_introduced` SZZ 式引入 commit 候选 07a37c8;#5 `export_report` 加 `agents_md` opt-in 产 AGENTS.md / #6 `merge_eval` 升 `merge-tree --write-tree` 零 touch 判冲突(原 backlog #60,checkout 硬门降为 rev-parse)/ #8 删 deep 空壳参数,3ebe7e3);287 测绿。见 [p1p2-high-priority-handoff](.claude/memory/p1p2-high-priority-handoff.md) / [when-introduced-handoff](.claude/memory/when-introduced-handoff.md) / [p1p2-backlog-568-handoff](.claude/memory/p1p2-backlog-568-handoff.md)。**✅ 同事安装四件套(2026-08-18)**:① MCP 工具门控 `ROOTRECALL_MCP_TOOLS`(预设 minimal/research/full 或逗号清单;未注册不进 tools/list 真省上下文,未知名启动即 ValueError)② 同事安装链路(wire 脚本 git init + `--codebase` 默认库注入 + `ROOTRECALL_CLAUDE_LINK=0` 跳软链 + README「给同事安装」节;拍板**每-bug 接线为主不做全局**,全局方案记档 mcp-guide 姿势③)③ 两套命名约定落文档(索引名=项目-版本线 `wpa-v25` / 记忆 codebase=项目名 `wpa` 防版本孤岛,docstring×3+SKILL×3)④ **CRG 增量刷新接入**(2026-08-18):`CodeGraph.update()` + built_head 快照,`rootrecall index` 已建图从「静默跳过」改为增量刷新(git diff∪未跟踪 → CRG incremental_update,社区按需;图/快照缺失·非 git·快照不可解析→兜底全量),CLI 真机冒烟三路径全对(全量/noop/增量,新符号带调用边进图)。详见 [colleague-onboarding-toolset-handoff](.claude/memory/colleague-onboarding-toolset-handoff.md)。**✅ 检索三修 bug + L2 粒度先验(2026-08-18)**:① 撞 chunk id(parser 作用域栈含函数名,函数局部同名类不再同 id;chunk_repo 去重护栏;`delete_by_file` 清增量幽灵行,还 index.py:22 旧账)② **iter_source_files 尊重 .gitignore**(git ls-files 两路并集,非 git 兜底 rglob—— 以仓库根建索引曾把 clone 进来的参考仓全量扫入,嵌入账单爆炸)③ 重排池扩满 + **符号粒度先验**(`_granularity_prior`:module 0.65/私有·嵌套 0.80/公共入口 1.0;远端 rerank 本就全量打分,扩池零成本)。eval 实测(758 chunk 干净索引,注意**索引根=src/rootrecall** 是 eval gold 路径契约):**L1 mrr 0.854→1.000**、L2 gold 位次大前移(21→7/24→6)但 top-line 持平 0.700 —— 剩余 miss 定性为**另一失败模式**(同域公共符号 cross-encoder 平局 0.003~0.03 分差 + parse_repo 不在召回 50 池),拒绝调乘数过拟合 10 条查询;再抬方向=rerank 文档表示/更强 reranker。311 测绿。详见 [l2-granularity-prior-handoff](.claude/memory/l2-granularity-prior-handoff.md)。
