# Skill 路由矩阵:用户问题 → 进哪个 skill

> RootRecall 的 8 个 skill 各管一类问题。本文是"总机接线表":拿到一个用户请求,先在这张表里对号入座,再进对应 SKILL.md 看详细菜谱。
> 本表 §一/§二 已浓缩进仓库根 [AGENTS.md](../AGENTS.md)(opencode 注入每个 agent 的系统提示,默认界面直接提问即自动路由)—— **改判据两处同步**:AGENTS.md 是模型消费的浓缩版,本文是完整版。
> 三条总纪律(所有 skill 共享):**只到 apply 不编译**(编译/复现/正确性一律用户真机自验);**read-only 的 skill 不改任何仓库**;**bug/补丁类结论 apply 过即可记但必须带 `verification: apply_only`**(自动打 unverified 标、置信封顶 0.5,recall 显式渲染「未真机验证」;真机通过后同补丁重提 `real_machine` 升级),读码/调研类读完即记。

---

## 一、按问题形态路由(主表)

先看用户的问题属于哪一类,再看那类的候选 skill:

| 用户问的是… | 进这个 skill | 一句话干什么 | 改代码? | 何时 memorize |
|---|---|---|---|---|
| "为什么 X 会断/泄漏/死锁/崩",查 bug 根因 + 修复 | **bug-rca** | 多假设清单 + 证伪迭代定位根因,出补丁 + 报告 | ✅ 改 | apply 后记 unverified,真机后升级 |
| "这个补丁/PR 干啥 / 能不能打上 / 该不该合 / 有没有副作用" | **patch-review** | 鉴定一个补丁:做了什么、能否 apply、影响面、合入建议 | ❌ 只读 | apply 后记 unverified,真机后升级 |
| "上游这些 commit 哪些该合进来 / 哪些已修过 / 会不会冲突"(同一个 git 仓) | **upstream-merge** | 逐 commit 三态判定(已修/建议合/冲突)+ 相关性 + 决策表 | ❌ 只评估 | apply 后记 unverified,真机后升级 |
| "v25 修了这个 bug、v20 还没修,帮我改 v20"(两个独立发行版线) | **backport** | 读 v25 fix → 语义判 v20 有无同一 bug → 适配改 v20 + 验 apply | ✅ 改 | apply 后记 unverified,真机后升级 |
| "v20 和 v25 在 X 流程上有什么差异"(调研,不修) | **compare** | 锚定两版入口函数,语义配对,逐节点读函数体对照,出流程级差异报告 | ❌ 只读 | **读码即记** |
| "这个仓库整体架构怎么组织 / 核心模块和入口在哪 / 帮我上手" | **onboarding** | 结构图俯瞰(社区/hub/bridge)+ 挑一条主旅程端到端走,出导览报告 | ❌ 只读 | **读码即记** |
| "我们对这个仓记了啥 / 记忆库质量怎么样 / 哪些记忆可信" | **memory-health-check** | 摊全量记忆逐条看溯源卡,聚健康信号(溯源弱/待巩固/已过期/未决矛盾) | ❌ 连记忆库都只读 | 仅未决矛盾记「需裁决」 |
| "蓝牙协议怎么设计的 / X 技术的原理 / 帮我记个技术笔记"(知识不在源码里) | **domain-research** | 上网多源交叉查权威源(spec/RFC),出知识卡 + 记 domain_knowledge | ❌ 只读 | **调研即记** |

## 二、易混对拆解(边界判据)

按"维度对"拆,每对给判据 —— 两边长得像,但路由结果不同:

**1. upstream-merge vs backport —— 版本线拓扑不同**
- **upstream-merge**:fork 和上游是**同一个 git 仓**(有共同祖先)。确定性工具能干活:`merge_eval` 用 patch-id 判"已修"、`merge-tree` 判"冲突"。⚠️ 血统以 `git merge-base` 为准,不看仓库观感 —— squash / 独立血统的 fork(实测 deepin bluez 长得像同仓 fork 但无共同祖先)会被 `merge_eval` 前置短路并指路:改走 backport 式逐 commit 语义评估。
- **backport**:v25 和 v20 是**两个独立仓**(无共同祖先,patch-id 失效)。只能语义判:读 v25 fix-point 对照 v20 函数体判"有没有同一个 bug"。这是两者的核心差异,也是 backport 不用 `merge_eval` 的原因。

**2. compare vs backport —— 目的不同**
- 问法都是"两个版本对照"。**compare 是调研**(只要差异报告,不改代码,读码即记);**backport 是修复**(要产出 v20 的补丁,等用户验证才记)。
- "v25 比 v20 多了什么防护?" → compare。"把这个防护搬到 v20" → backport。

**3. onboarding vs compare —— 几个仓**
- **onboarding** 讲**一个** codebase 的架构(模块怎么分、主线怎么走);**compare** 对照**两个**版本的同一流程。给新人讲仓 → onboarding;跨版本找差异 → compare。

**4. domain-research vs onboarding/compare —— 知识在不在源码里**
- 答案能靠 `read` 源码坐实的(架构/流程/差异)→ onboarding/compare;答案在协议规范/技术文档里、源码查不到的(协议语义、各层职责、技术原理)→ domain-research。
- 判据:这个问题"读代码能回答吗?"能 → 读码系;不能 → 网调系。

**5. patch-review vs upstream-merge —— 鉴定对象不同**
- **patch-review** 鉴定**一个**补丁/PR(该不该合这个);**upstream-merge** 评估**一批**上游 commit(逐个三态 + 整体决策表)。"这个 PR 能合吗" → patch-review;"上游这 20 个 commit 哪些该 backport" → upstream-merge。

**6. bug-rca vs 其他全部 —— 有没有"要修的 bug"**
- 只有 **bug-rca** 和 **backport** 改代码;其余全 read-only。用户带着"现象/崩溃/回归"来 → bug-rca;带着"补丁/commit/版本差异/架构/知识/记忆"来 → 对应其他 skill。

## 三、按工具看(谁用什么)

| 工具 | 哪些 skill 用 | 定位 |
|---|---|---|
| `memory_recall` / `memory_memorize` | 全部 8 个 | 召回先验 / 沉淀结论(recall-first 是所有 skill 的第一步) |
| `search_codebase` / `repo_map` / `call_chain` / `blast_radius` / `when_introduced` | bug-rca(主)/ compare / onboarding / backport(辅) | 代码情报五件套:检索/符号图/调用链/影响面/引入史 |
| `repo_overview` | onboarding | 架构层俯瞰(社区/hub/bridge/耦合告警) |
| `validate_patch` / `fetch_patch` | bug-rca / patch-review / backport | apply 硬门(Tier 0,RootRecall 验的唯一一层) |
| `merge_eval` | upstream-merge(专用) | 三态判定(patch-id + merge-tree) |
| `find_repo` | bug-rca / backport / domain-research / compare / onboarding(仓库就绪第一步) | 「项目+版本」查注册表;未开仓给一步就绪的开仓命令 |
| `ensure_repo` | compare / onboarding / domain-research(本地没仓时) | 只读 clone 到 data/repos |
| `memory_dump` | memory-health-check(专用) | 摊全量记忆出溯源卡;空作用域列非空作用域 |
| `cross_version_diff` | backport(可选辅路) | 只对同一个 git 仓的两个 ref 有效;两独立仓要先 fetch 进同一仓才可用 |
| `websearch` / `webfetch`(opencode 内置) | domain-research(专用) | 网调权威源,多源交叉 |

## 四、组合场景(一个问题走多个 skill)

- **"v20 上也有这个 bug 吗?帮我修一下"** → 先 compare(锚定 v25 fix-point 在 v20 的对应函数,判有没有同一 bug 的证据)→ 再 backport(适配 + 验 apply)。compare 产出的配对事实会进记忆,backport recall 直接复用。
- **"刚定位完这个 bug,这个修法之前是不是踩过坑"** → bug-rca 自带 recall 步骤;若要系统翻旧账,补一发 memory-health-check。
- **"上游合完这批 commit 后,帮新人讲讲现在的架构"** → upstream-merge(评估合入)→ onboarding(合并后的架构导览,recall 命中旧导览则短路秒答)。

## 五、全 skill 公共纪律(路由之外的底线)

1. **验证封顶 apply**(Tier 0):`validate_patch` 是 RootRecall 唯一自动跑的验证;编译/测试/复现永不做,correctness 只报 apply-based / reasoning-based。
2. **溯源铁律**:每条结论带 file:line(compare/onboarding 带双源);domain-research 带 source_url;记忆条目带 evidence/commit_sha。
3. **recall-first**:所有 skill 第一步 `memory_recall` 探底,命中同主题直接短路出报告(「下次秒答」);只有没命中/主题不符才重跑调研。
4. **诚实信号**:不静默截断、不报没验过的 tested/verified、工具降级时 note 明说。
