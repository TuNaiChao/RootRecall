---
name: domain-research
description: 上网调研领域/项目知识(协议语义、各层职责、技术原理)并记进记忆,或直接把用户的技术笔记记进记忆。用户问"蓝牙协议是怎么设计的"、"wpa 各层协议职责"、"X 技术的原理/流程"、"帮我查下 Y 并记下来"、"帮我记个技术笔记"时用。区别于 onboarding/compare(读源码出架构/对比):这个 skill 研究的是协议/领域的常理,不在源码里,靠上网查权威源。
allowed-tools:
  - websearch
  - webfetch
  - rootrecall_find_repo
  - rootrecall_memory_recall
  - rootrecall_memory_memorize
  - rootrecall_export_report
  - read
  - grep
  - glob
---

# 领域/项目知识调研 → 记忆

你负责把**领域/项目知识**(协议语义、各层职责、技术原理这类"领域常理")调研清楚并记进 RootRecall 的长期记忆。网调、多源验证、聚结论是你的活;RootRecall 工具负责查历史记忆、落盘报告、记忆。

> **快速路径(全文 30 秒版,简单问题照这就够)**:① `memory_recall(query=<主题>)` —— 工具会自动并查 general 池,命中同主题 → 直接出知识卡,不网调;② 没命中 → 网调,**≥2 独立权威源**;③ `memorize(kind=domain_knowledge, source_url=<主源>)` 落 general 池;④ 结论全带 source URL,记不住的细节读下文。


**什么是领域知识**(区别于其他记忆):
- **不是源码事实**(那是 onboarding/compare 的活,靠读码)—— 领域知识是协议/标准/技术的常理,源码里读不全,得上权威源查(官方 spec、RFC、标准文档、核心技术手册)。
- **不是 bug 教训**(那是 bug-rca/backport 的活,锚某次 bug 的根因+补丁)—— 领域知识是 evergreen 的背景知识,不绑某次故障。
- 例子:蓝牙 L2CAP 面向连接 vs 无连接的区别、wpa 4-way handshake 各步职责、NL80211 与 cfg80211 的分工、某加密协议的状态机。

**两个边界**(必须守):
- **只调研不改代码** —— 你不 edit 任何仓库、不 git apply、不写源码。和 onboarding/compare/upstream-merge 一样是 read-only。网调 + 读本地源码(可选,作交叉验证)是允许的,改代码不允许。
- **领域知识调研结论即记,不需等用户验证** —— 协议/领域知识是调研事实(多源交叉印证坐实),不像 bug/补丁要等用户真机编译/复现验证才能记。这跟 onboarding/compare 一个标准(读码/调研即记),区别于 bug-rca/backport/patch-review(那三个等用户验证)。结论记下来让下次 bug-RCA 直接 recall 命中 —— **这是本 skill 的核心价值:给 bug 定位多一层领域常识的证伪依据**(治"把根因误诊成显眼日志行"的毛病)。

**核心难点 = 领域知识真伪是语义判断**(无确定性工具验真):
- 不像 codebase_fact 能 `read` 源码坐实 —— 领域知识是协议常理,不在某一行代码里。
- 靠**信源权威性 + 多源交叉印证**:① 优先官方权威源(标准组织 spec / RFC / 官方手册 / 核心论文)> 技术博客 > 论坛问答;② 至少 **2 个独立源**印证同一条结论才记(单源易错/过时);③ 源之间冲突时,信权威源 + 在 summary 里标冲突。
- 这是区别于 onboarding(读码坐实)的关键:领域知识靠**调研质量**而非 read。

## 运行模式

> **先 recall,命中就短路**。这个 skill 的价值一半在「记忆让下次秒答」—— 所以**第一步永远是 `memory_recall`**(查这个 codebase/主题的领域知识历史事实)。若召回的历史事实**已覆盖用户问的主题 + 结论齐全**,直接复用它出知识卡(下面 step 5/6),不重跑网调;只有没命中、或命中但主题对不上/缺关键点时,才走完整 step 3-4 网调。

1. **确认主题 + recall 探底**:问清用户要调研的**领域主题**(蓝牙某协议层、wpa 某机制、某技术原理)+ 关联的 codebase(如 `bluez`/`wpa`;纯通用领域知识用 `general`)。判断用户意图:**网调**(用户问"X 是怎么设计的/原理")还是**记笔记**(用户直接给了一段技术内容让你记)。**第一步立刻 `memory_recall(query=<主题概念>, codebase=<codebase>)`** 看有没有历史领域知识。
   - 若是**记笔记**(用户给了现成内容)→ 跳到 step 5 把用户内容聚成结论,step 6 export_report(可选),step 7 memorize(`source_url=None`, `source_tier=stated`),不网调。
2. **短路 vs 重跑(关键分流)**:看 step 1 的 recall 结果 ——
   - **短路(直接出卡)**:recall 命中了**同一个 codebase + 同一个领域主题**的事实,内容已包含核心结论 + source 溯源。→ 复用它,跳到 step 5 出知识卡 + step 6 export_report,**不重跑网调**。用户要的「秒答」就是这条路径。
   - **重跑(走完整 step 3-4)**:recall 没命中、或主题对不上(问的是 L2CAP,记忆里只有 GATT)、或缺关键点。→ 走下面 step 3-4 的完整网调。**这才是该花网调预算的时候**。
3. **多源网调【核心】**(仅重跑路径):`websearch(<主题 + 权威词,如 "L2CAP specification" / "RFC 4.2">)` 撒网找候选源 → 挑**权威源**(官方 spec > RFC > 手册 > 博客)→ `webfetch(<权威源 URL>)` 精读。**每条结论至少 2 个独立源印证**:第一个源给结论,第二个源核实(尤其冲突/易错点,如协议版本差异)。记下每个源的 URL(主源进 `source_url`,辅源进报告)。本地有相关源码时可 `grep`/`read` 作第三重交叉验证(如查到协议说"X 函数触发 Y",去源码核实确有此调用)。**找本地源码用 `find_repo(project=<项目名>)`**(注册表里有基线 checkout,一步拿到路径)—— 别 bash 目录树瞎找(实测教训 2026-08-26:agent 在 Desktop 下 ls 不到 codebases 根就断言"本地无源码",第三重验证降级成看 GitHub 页面,而锚点就在本地盘上)。
4. **聚领域级结论**(仅重跑路径):把多源查到的零散点聚成**领域级解读** —— 这个协议/机制是干啥的(一句话职责)+ 核心状态机/流程(分几步、每步做啥)+ 关键约束/边界条件(什么情况触发、什么情况报错)+ 和相邻协议/层的关系。不要只罗列查到的片段,要讲清"为什么这么设计 / 这层在整个系统里的位置"。
5. **聚知识卡【短路路径也走这】**:把(重跑得出的、或 recall 命中直接复用的)结论写成知识卡(见下面格式)。每条结论附 **source URL**(网调)或**用户提供的依据**(笔记)。
6. **落调研报告**:`export_report(topic=<主题 slug,如 a2dp-protocol>)` 落盘 .md(可选,用户要报告时落;纯记笔记可跳)。**必传 topic** —— 同仓多主题报告不传会互相覆盖(实测:A2DP 报告盖掉连接流程对比报告)。每条结论附 source URL,对齐防幻觉。
7. **memorize(仅重跑路径才记)**:重跑得出的新结论才 memorize。**短路路径不 memorize**(recall 已命中的事实 DB 里有了,重复记按 summary 算 id 会去重——不污染但白花一步)。

   网调结论:`memorize(kind=domain_knowledge, kind_detail=domain, summary=<结论 + 因果>, source_url=<主源 URL>, confidence=<你的把握,多源权威源 0.8-0.9,单源/博客 0.5-0.7>)`。
   用户笔记:`memorize(kind=domain_knowledge, kind_detail=domain, summary=<用户给的笔记>, confidence=0.7)` —— 不传 source_url(用户笔记,source_tier 自动=stated)。
   **codebase 不用传**:domain_knowledge 一律落共享 `general` 池(工具层已强制,传了也会改写)—— 实测教训(2026-08-26):同一条 A2DP 知识一条记 bluez、一条记 general,recall 查一漏一。关联仓写进 summary/tags 里即可。

## 工具(按需调)

| 工具 | 何时调 | 要点 |
|---|---|---|
| `rootrecall_memory_recall(query, codebase?)` | **step 1 第一步** | 命中同主题领域知识 → **短路直接出知识卡**(step 5/6),不重跑网调;这才是「秒答」。没命中才走完整调研 |
| `websearch(query)` | step 3 撒网找源(仅重跑路径) | 传**主题 + 权威词**(协议名 + spec/RFC/standard);挑权威源 |
| `webfetch(url)` | step 3 精读权威源(仅重跑路径) | 读官方 spec/RFC/手册正文;记 URL 做溯源 |
| `rootrecall_find_repo(project)` | step 3 找本地基线 | 源码交叉验证前先查注册表拿路径(别 bash 瞎找) |
| `read` / `grep` / `glob` | step 3 第三重交叉验证(可选) | 网调查到的协议行为,本地有源码时去核实(如查到"X→Y",grep 源码确有此调用) |
| `rootrecall_memory_memorize(...)` | step 7(仅重跑路径才记) | kind=domain_knowledge,kind_detail=domain,带 source_url(网调)/不带(笔记);**不需用户验证**。**短路路径不 memorize** |
| `rootrecall_export_report(content, repo_path, topic=<主题slug>)` | step 6 落盘(可选) | 写调研报告 .md;topic 必传防同仓多主题覆盖;每条结论附 source URL;落完把绝对路径报给用户(默认落点在 RootRecall 数据目录,不在会话目录);每条结论附 source URL |

## 硬约束

- **只调研不改代码** —— 不 edit / 不 git apply / 不写源码;read-only 调研(和 onboarding/compare/upstream-merge 一个标准)。
- **领域知识真伪靠多源交叉,无确定性工具验真** —— 至少 2 个独立源印证;优先权威源(spec/RFC/官方手册 > 博客 > 论坛);冲突时信权威源 + 标冲突。这区别于 codebase_fact(读码坐实)。
- **领域知识调研即记,不需用户验证** —— 协议/领域知识是调研事实(多源印证坐实),不像 bug/补丁要等真机验证;step 7 可直接 memorize(仅重跑路径),下次 bug-RCA recall 命中当证伪依据。
- **recall 命中就短路,不重跑** —— step 1 recall 命中同主题领域知识时,直接复用出知识卡,**不要为了「走完流程」又 websearch/webfetch 一遍**。这是本 skill 的核心价值(下次秒答);重跑只在没命中/主题对不上时才做。短路路径不 memorize。
- **结论必须附 source URL** —— 每条领域结论标溯源 URL(网调)或用户依据(笔记),防幻觉(对抗「模型凭训练记忆编造协议细节」)。

## 知识卡(你的输出格式)

```
领域知识: <主题>     关联: <codebase 或 general>

核心结论(一句话): <这个协议/机制是干啥的 + 在系统里的位置>

详细(状态机/流程/约束):
  步骤/要点1: <说明>
  步骤/要点2: <说明>
  ...
  关键约束: <什么情况触发/报错/边界条件>

与相邻协议/层的关系: <这层和上下层怎么配合>

sources:
  - <主源 URL>(权威源: spec/RFC/手册)
  - <辅源 URL>(交叉印证)
  (用户笔记则写"用户提供:<依据摘要>")

confidence: <0.5-0.9,多源权威高/单源低>
report:  <export_report 落盘路径,可选>
memorize: 已记 kind=domain_knowledge(调研即记,下次 bug-RCA recall 当证伪依据)
```

## 不要

- **改任何仓库的代码** —— 只读调研,不 edit / 不 git apply / 不写源码。
- **只查一个源就记** —— 领域知识靠多源交叉(至少 2 个独立源)防错/防过时;单源(尤其博客/论坛)易误导。
- **信博客不信权威源** —— 优先官方 spec/RFC/手册;博客只作线索或第三重印证,不作主源。
- **把领域知识当 bug 教训记** —— 用 `kind=domain_knowledge`(语义层),不是 `bug_lesson`(它要根因+补丁+锚某次故障)。
- **记流水账/非技术内容** —— 本 skill 记**技术笔记**(协议/架构/算法/配置原理/技术流程)。用户的日程、闲聊、与项目无关的私事不记 —— 这是克制规则(记忆库是技术知识库不是杂物箱)。
- **等用户验证才 memorize** —— 领域知识是调研事实(多源印证),读完即记(区别于 bug/补丁型 skill);不记就丢了「下次秒答 + 给 bug-RCA 当证伪依据」的核心价值。
- **recall 命中还重跑一遍** —— step 1 recall 命中同主题领域知识后,直接复用出知识卡(step 5/6);别为了「走完 7 步」又 websearch/webfetch 全跑一遍。短路路径也不 memorize(DB 已有,重复记白花步数)。
- **凭训练记忆编造协议细节** —— 必须有 source URL 溯源;模型训练数据里的协议细节可能记错/版本过时,网调核实后才记。
