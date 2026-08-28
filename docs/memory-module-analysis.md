# 记忆模块设计分析

> 这是 RootRecall 三大支柱里 P3「记忆与持续学习」的当前实现分析。
> 源码真相在 `src/rootrecall/services/memory/`,本文档只做"讲清它在干什么、怎么设计的"。

---

## 0. 一句话:记忆模块在干什么

普通 coding agent(opencode / claude code)有个致命毛病:**每开新会话就失忆**——上次定位过的 bug、上次摸清的模块结构,它全忘了,得从头读代码读日志,费 token 又费时间。

RootRecall 的记忆模块就是给 agent 装一本**跨会话、能自己变聪明的"长期笔记本"**。

**比喻**:把 agent 想象成一个新来的实习生。普通 agent 是「干完活就把笔记撕了」的实习生;RootRecall 的记忆模块是「每干完一件活就往团队共享笔记本里记一条,还带页码和出处,下次遇到类似的能直接翻到」的实习生。而且这本笔记本**会自己整理**(consolidate 巩固):重复的合并、反复出现的教训升级成"规律"、互相打架的结论标「待裁决」、补丁已修的标「已合入上游」打折、长期没人翻的标「过期」、被推翻的旧结论不撕掉但打上「已被纠正」的标签排到后面。

记下来的东西分三类来源:
- **P1 调研**产出的"这个库长啥样、关键模块怎么实现" → 记下来(带源码 commit SHA 溯源)。
- **P2 bug-RCA** 产出的"这个 bug 根因是啥、怎么修的" → 记下来,并连到相关的代码库知识。
- **外部文档/补丁**也能吃进来(team 历史 bug 报告、上游已合入补丁)。

记完的回报:下次任何人遇到类似问题 → 先翻笔记本("这模式之前见过,这是当时的修法")→ 省推导、省 token。

**为什么是"持续学习"而不只是"存储":** 笔记本会**去重、衰减、把反复出现的教训升级成稳定规则**(像人把短期记忆巩固成长期记忆)。否则就是越攒越乱的垃圾堆。

**与检索的分工(别混淆):** 记忆模块**复用**已有的两个检索引擎当后端——`code_index`(按"意思"语义找)和 `code-review-graph`(按"结构/调用关系"找)。记忆模块自己**不建第三个检索库**,它是在两者之上的"知识层 + 持续学习逻辑"。

---

## 1. 一条记忆的结构(KnowledgeItem)

所有记忆都是同一个类 `KnowledgeItem`([schema.py:132](../src/rootrecall/services/memory/schema.py#L132)),按 `kind` 分成**四类**,共用一张 SQLite 表。一条记忆有 7 组字段。

**比喻**:一条记忆就像图书馆里的一张「索引卡片」,上面盖满了不同颜色的章——身份章、出处章、可信度章、时间章、关系章……

### 身份章(这条记的是谁)

| 字段 | 含义 | 比喻 |
|---|---|---|
| `id` | `sha256(scope+kind+内容)[:16]`,**内容决定** | 卡片编号;同一件事重记 → 同编号 → 合并而非新增 |
| `kind` | 四类(见下) | 这张卡归哪个书架 |
| `repo` / `scope` | 哪个库 / `(owner, codebase)` 谁管的哪个库 | 哪个案件、哪个侦探的笔记本 |
| `summary` | 人读一句话摘要 | 卡片标题;检索 + 注入模型时最先看到 |

**四类 `kind`**([schema.py:141](../src/rootrecall/services/memory/schema.py#L141)):
- `codebase_fact` —— 代码事实(这个模块/符号/架构是干啥的)
- `bug_lesson` —— bug 教训(根因 + 修法 + 影响面)
- `mental_model` —— 稳定规则(被反复召回 ≥N 次的教训"毕业"成的规律)
- `domain_knowledge` —— 领域知识(蓝牙协议 / wpa 各层职责这类"常理")

这四类正好对齐 2026 业界共识的 agent memory 四分类:**语义记忆(semantic)= domain_knowledge / codebase_fact、情景记忆(episodic)= bug_lesson、程序性记忆(procedural)= mental_model**(第四类"工作记忆"随 workflow state 走、不单独建库,见 §8 明确不做)。这套分类不是拍脑袋,是踩在认知科学共识上的。

### 内容章(展开细节)

`detail`(正文)、bug_lesson 专用四件套(`symptom` / `root_cause` / `fix_patch` / `blast_radius_files`)、`kind_detail`(module / symbol / architecture / domain)。

### 出处章 —— 差异化王牌

| 字段 | 含义 |
|---|---|
| `commit_sha` | 溯源到具体 git commit(记忆的"保质期锚点") |
| `evidence` | `[file:line + 原文片段]` 列表 |
| `source_url` | 外部溯源 URL(domain_knowledge 网调来的) |
| `source_tier` | 来源可信度档(6 档,见可信度章) |

这是 RootRecall 区别于所有通用 agent memory 的核心。Graphiti / Zep / mem0 记的是"用户说过的话",**没有 file:line + commit 这种代码锚点**。每条结论都能追到「在哪个 commit、哪个文件第几行」,模型才敢用它下根因判断。

### 可信度章(持续学习信号)

`source_tier`(6 档权重 [schema.py:61](../src/rootrecall/services/memory/schema.py#L61)):delegate / stated 1.0(侦探当面查 / 报告明说)> unknown 0.8(保守)> inferred 0.7(推断)> imported 0.6(外部导入)> tool 0.5(工具检索,可能是噪声)。

`confidence`(0..1 Bayes 累加)、`access_count`(被召回几次)、`last_recalled`。

### 时间章 —— 双时间轴(bi-temporal,借 Graphiti)

`valid_at`(这件事"在真"的起点)、`invalid_at`(失效点,None = 仍有效)、`created_at`(我们何时记下它)。

**比喻**:普通笔记本只有一个时间"什么时候写的"。双时间轴有两个:「这件事发生的时间」(valid_at)和「我知道这件事的时间」(created_at)。这让记忆能回答"**这个 bug 在去年 3 月时还存不存在**"——系统考古的关键,Graphiti / Zep 就是靠这个在矛盾处理 benchmark 上领先。

### 关系章 / 软删章

`related`(关联卡 id)、`tags`、`superseded_by`(被哪条取代)。

### 纠正章 —— 纠正关系闭环

这是记忆模块里**设计最精巧的一块**,两个字段分工明确([schema.py:178-188](../src/rootrecall/services/memory/schema.py#L178-L188)):
- `corrects` —— 新条说"我纠正了谁"(临时指令,写入时消费掉回填旧条,不入库;[schema.py:187](../src/rootrecall/services/memory/schema.py#L187) 字段定义)
- `corrected_by` —— 旧条上"我被谁纠正了"(持久化,检索降权 0.3,仍可见作参考)

**为什么不复用 `superseded_by`:** 这是关键设计。superseded_by 绑定 `active`,设了会让旧条从 active 视图**消失**;但"被纠正" ≠ "失效"——被推翻的旧根因仍要能检索到、体检能看到(审计可追溯),只是排到纠正者后面。所以独立 `corrected_by` 解耦。场景:bug 根因被推翻(A 派"abort-failure"是错的,B 派"scan-only 竞态"纠正它)→ B.corrects=[A.id],写入时自动回填 A.corrected_by=B.id → recall 时 A 被降权排后面,但还能看到,讲清思路演变。

---

## 2. 怎么管理记忆(架构骨架)

**分层契约**([manager.py:33](../src/rootrecall/services/memory/manager.py#L33)):`MemoryService` 是个抽象基类,分三层:
- **tier-1**(必须实现):`memorize`(记)、`recall`(翻)
- **tier-2**(管理,默认未实现,后端按需覆盖):`search` / `get` / `list_items` / `memorize_report`
- **tier-3**(可选钩子):`consolidate`(巩固)、`invalidate`(失效)

**后端可换**([manager.py:109](../src/rootrecall/services/memory/manager.py#L109)):丢一个 `backends/<name>/` 文件夹(暴露 `BACKEND_CLASS`)+ 配置 `memory.backend`。v1 只实现 `native`,mem0 / cognee 留接口位(需要时一行配置换上,零锁死)。**拒绝静默回退**——后端名配错必须报错(记忆是持久状态,不能偷偷用别的)。

**比喻**:这是"笔记本的标准接口"——不管后端是自家 SQLite 还是外接 mem0 / cognee,对外都只认"记"和"翻"两个动作。换笔记本品牌不用改上面的代码。

**单例** `get_memory_service()`(double-checked lock),对应 deer-flow 的 `get_memory_manager()`。

---

## 3. 怎么新增记忆(写路径 memorize)

入口 `memorize_items`([memorize.py:159](../src/rootrecall/services/memory/backends/native/memorize.py#L159)),五步流水线:

1. **嵌向量** `_embed_items`:给 summary 算 embedding(复用 code_index 的 embedder;off 则只走 BM25)
2. **连图边** `_link_related`:找同 scope 内 evidence 文件有交集的现有项 → 填 `related`(便宜有用)
3. **合并 / 冲突** `_merge_on_remention` —— 见第 6 节
4. **纠正链**:新条若声明 `corrects` → `mark_corrected` 回填旧条 `corrected_by`
5. **upsert** 入库([store.py:417](../src/rootrecall/services/memory/backends/native/store.py#L417))。写侧还有一条硬规则(2026-08-26 实测教训):`kind=domain_knowledge` **无视传参强制落 general 共享池**(工具层改写并注明原传值)—— 领域知识按当时代码库上下文随手记进项目池,会让别的会话 recall 查一个漏一个。

**置信度怎么算**(Bayes 累加,借 mnemopi):新条初始 = tier_weight · 0.5;重提同事实时 `conf += (1-conf)·tier·step`(step=0.3,封顶 1.0)。cur 越接近 1 增量越小(饱和);tier 越可信权重越大。

### 验证档(verification)—— 没坐实的教训,标着用而不是憋着

MCP 入口 `memory_memorize` 带 `verification` 参数(2026-08-20,真 e2e 发现"验证前禁记"的纪律 agent 守不住后,从禁令改成结构化标注):

- `apply_only`(早记档):补丁过了 `validate_patch` 就可以记 —— 工具层自动打 `unverified` 标 + **置信封顶 0.5**;recall 命中时渲染带「(未真机验证)」,下游 agent 一眼看出这条没坐实。先验价值保留,可信度打折显式可见。
- `real_machine`(升级档):真机验证通过后,**同一补丁重提一次** —— 补丁内容算 id,同 id 走 Bayes 合并,新条的 tags 替换旧的(unverified 洗掉、换上 `verified_real_machine`),置信度恢复正常累加。升级不是编辑旧条,是内容寻址的天然结果。

这条与 §6 的"只追加不取代"一脉相承:验证状态是条目自描述的一部分,不靠外部流程约束。

### 文档摄取通道(外部文档 → 记忆)

除了 workflow 内部产出,外部文档也能吃进来([ingest.py](../src/rootrecall/services/memory/ingest.py)),按扩展名分流:
- **报告路**(.md / .pdf / .txt):`parse_issue` 取文本 → `LongDocChunker` 按 markdown header 切块(一章太厚再按段切,顺着作者本来的边界,不劈断句子)→ 每块 LLM 抽 KI → memorize
- **补丁路**(.patch / .diff):`PatchIngestPipeline` 解 hunk → `code_index.retrieve` 取周围代码上下文 → LLM 抽根因 → 组装 bug_lesson。**补丁的 id 按 diff 内容算**(不是 LLM 的总结),保证同一补丁摄取两次 → 同 id → Bayes 合并

**补丁为什么要先 retrieve 再 summarize**:裸 diff 缺周围代码上下文,LLM 难判根因;先取被改符号周围代码再喂 LLM(依据:PATCH / ACM 2025、SpecRover / ICSE 2025)。repo 没建索引 → retrieve 返空 → 降级只喂 diff(不阻塞)。

**比喻**:新增不是"无脑往里塞"。像图书管理员收到一份新资料:先看是不是已有的(同编号 → 合并增强可信度),再看和哪些已有资料相关(连边),最后盖上"来源 / 时间 / 可信度"章归架。

---

## 4. 怎么检索记忆(读路径 recall)

`recall`([recall.py:131](../src/rootrecall/services/memory/backends/native/recall.py#L131))——**四路召回 → 融合 → 精排 → 衰减加权**:

```
四路(各路可选,失败降级不崩):
  memory·BM25   ← store.search_bm25(FTS5 全文,始终在)
  memory·vector ← store.search_vector(cosine;count>500 切 sqlite-vec ANN 加速)
  code          ← code_index retrieve(可选,repo 没索引跳过)
  structural    ← code-review-graph blast_radius(可选)
        ↓
  RRF 融合(K=60,各路排名加权:多路都命中的更靠前)
        ↓
  可选 rerank(复用 code_index 的 qwen3-rerank)
        ↓
  decay × confidence 加权(exp 时间衰减 + 置信度 + 纠正降权)
        ↓
  top-k(每条带 溯源 + 置信度 + 时间戳,注入提示词)
```

命中的 memory 条顺手 `bump_access`(access_count++,升级 mental_model 的依据),并后台 fire-and-forget 跑一次 consolidate(自动巩固)。工具层(MCP 入口)在读路径之上叠了三个护栏(各来自一条实测坑):**并查 general 池**(领域知识所在,跨池命中带 `[general]` 前缀);**低相关劝退**(向量路原始余弦挂在每条命中上,头牌 <0.40 时明确「按 miss 处理」,防无关查询被小池排名当命中);**空结果 / 劝退时列非空作用域**(默认 scope 落空时一步找到记忆在哪)。

**向量检索的渐进式设计**:count(scope) ≤ 500 走 Python 逐行 cosine(小规模更快);> 500 切 sqlite-vec vec0 KNN(C 扩展,快 2-4×)。加载失败降级纯 loop。契约不变,recall 无感。

**BM25 路的中文分词(CJK)**:FTS5 自带的 unicode61 分词器只认空格,中文没空格 → 整句"扫描会阻塞所有站点"被当成**一个**词,搜"扫描"匹配不上,纯中文查询此前只能靠向量路。现在索引侧(入库前)和查询侧(`_fts_query` 前)**同用 jieba 分词**([tokenize.py](../src/rootrecall/services/memory/backends/native/tokenize.py)):只切中文段(英文标识符原样保留,不切碎),切完空格连回,两侧切法一致就能对上。代价是 FTS 表从「SQL 触发器自动同步」改成「upsert 时同事务维护」(触发器在 SQL 层调不了 Python 分词);老库打开时自动迁移重灌,分词失败降级回原行为,不崩。就像中文书脊原本是一整句话,只报其中两个词找不到书;入库时把书脊切成词卡、检索时把查询也切成词卡,对卡就行。

**比喻**:翻记忆不是"只翻一个抽屉",是**四个侦探同时翻四个档案柜**(关键词柜 / 语义柜 / 代码柜 / 结构图),各翻各的,最后把四份名单合在一起按"多侦探都提名 → 更靠前"排,再按"新旧 + 可信度"微调。这叫**多路召回融合(RRF)**,是检索工程的成熟做法。

---

## 5. 怎么删除 / 过期记忆(失效)—— 永不物理删

**铁律:永不物理删除**(bi-temporal 软删)。这是对的——能回答"这 bug 在 X 时点还在不在",审计可追溯。

- **手动失效** `invalidate` → `set_invalid`([store.py:550](../src/rootrecall/services/memory/backends/native/store.py#L550)):设 `invalid_at`(+可选 `superseded_by`)
- **管理视图(list / count)只看 active**:`invalid_at IS NULL AND superseded_by IS NULL`(失效和被取代的都不算数)
- **召回(BM25 / vector)只滤 `invalid_at`,不过滤 `superseded_by`**——被取代的旧版本仍能召回作参考,靠 decay 排到后面;只有手动 invalidate(错 fact)才真正隐藏。管理视图和召回的过滤口径**故意不同**:前者管"现在有几条有效记忆"(计数要干净),后者管"翻参考"(旧结论也有参考价值)

**比喻**:过期记忆不撕掉,是"盖个作废章放进档案室"。平时翻笔记本默认只看有效的;但旧版本还能翻出来当参考(比如"以前以为根因是 A,后来发现是 B"——A 留着能讲清思路演变)。

---

## 6. 怎么处理冲突记忆 —— 演进史

这是整套记忆里**最考验设计**的一块,经历了四个阶段:

**阶段 1(旧设计)**:写时 supersede——同主题不同结论,**新覆盖旧**(旧条设 invalid_at + superseded_by)。研究称这是"最站得住脚的默认"。

**阶段 2(对标 mem0 v3)**:改成**只追加不取代**([memorize.py:136](../src/rootrecall/services/memory/backends/native/memorize.py#L136))。同主题不同结论不再盖戳作废,新旧都 active 并存,靠检索侧 decay 排"最新为主、旧作参考"。同 id 重提(同 content_key)仍 Bayes 合并增强。这正是 mem0 v3 的 ADD-only architecture。

**阶段 3(纠正关系闭环)**:加 `corrects` / `corrected_by`。新条声明"我纠正了谁" → 旧条降权 0.3 仍可见。

**阶段 4(consolidate 显式矛盾检测)**:写入侧仍只追加,但巩固 pass 会主动扫「同主题不同结论」的 active 高置信度对,打 `needs_review` 标签交给人裁(只标不裁——谁对是语义判断,系统不自动选边)。标签供 memory-health-check 体检 skill 聚焦提示。

**当前最终态** = mem0 v3 的 append-only 写入 + 检索侧时序 / 纠正降权 + 巩固侧显式矛盾标记,这是 2026 业界主流。

### 三个判定的边界(helper 函数)

- `_same_subject`(同主题):bug_lesson 优先比 symptom;symptom 任一为空(CLI/MCP 写入常见)回退比**首证据文件 + 行号邻近**(同文件且行号差 ≤5 才算同主题——同文件 ≠ 同 bug,一个 scan.c 里前后脚几十个 bug;同 bug 两派诊断的证据锚点通常收在同一处)。codebase_fact 比 kind_detail + 同样的证据邻近判定。
- `_same_conclusion`(同结论):bug_lesson 比 root_cause;codebase_fact 比 summary
- 冲突 = 同 subject 不同 conclusion → 只追加不取代;consolidate 扫到 → 打 needs_review

---

## 7. 怎么巩固 / 持续学习(consolidate)

`consolidate`([consolidate.py:60](../src/rootrecall/services/memory/backends/native/consolidate.py#L60))——recall 命中后后台自动跑(fire-and-forget)或 CLI 手动触发,五个 pass:

| pass | 干什么 | 写入动作 |
|---|---|---|
| ① 升级 mental_model | 被召回 ≥N 次(默认 3)的 codebase_fact / bug_lesson "毕业"成稳定规则 | 改 kind;domain_knowledge 排除(领域常理 evergreen 不"毕业") |
| ② 矛盾检测 | 同主题不同结论的 active 高置信度对 | 打 `needs_review` 标签;**只标不裁**(谁对是语义判断,人裁) |
| ③ 语义去重候选 | 同 kind 内 embedding cosine ≥0.92 的近邻簇(并查集聚类) | **只报候选不自动合**(误合两个不同 bug 比留重复更糟);domain_knowledge 排除 |
| ④ 已合入上游 | bug_lesson 的 fix_patch 用 `git apply --check --reverse` 判改动是否已在仓里 | 打 `merged_upstream` 标签 + confidence×0.5;**不 invalidate** |
| ⑤ 过期 | last_recalled / created_at 超 365 天没人翻 | 打 `stale` 标签;**不降权**(recall 已有 exp 衰减,再降是双杀) |

**两个关键设计决策**(当初规划是另一套,实现时被否,记录防漂移):

- **pass ④ 不 set_invalid,只标 + 打折**。原案"补丁合入 → 失效"被否,理由:① `invalid_at` 的语义是"知识错了",不是"bug 修了"——考古查询("X 时点在不在")要靠这条记录;② reverse-apply 只证"改动在树里",可能是**等价修复**(别人用别的方式修的)→ 留人在环,确认后可手动 invalidate。判定用 reverse-apply 而非 merge_eval 的 patch-id(那是上游两 ref 对比,这里是仓 vs 补丁,问题形状不同)。repo_path 只在显式 consolidate(CLI `--repo-path`)时给——recall 的自动巩固不知道仓在哪,不猜。
- **pass ⑤ 只标不降权**。recall 打分已有 exp 时间衰减,consolidate 再降 confidence 是同一条被两处扣分(双杀)。标签供体检预警 + 注入时提示"这条很久没人验证过了"。

**计数与幂等语义**:计数 = 当前态(已标的再跑仍计入,统计稳定——体检要的是"总共有几条已合入");写入 = 幂等(标签只加一次、打折只打一次,防 confidence 被反复折到 0)。

**标签互写的坑(e2e 抓过)**:五个 pass 共享同一份条目快照,后跑的 pass 若用旧快照整体覆盖 tags 列,会把先跑 pass 刚打的标签洗掉 → 所有打标签操作统一走 `_add_tag`(写前重读 DB 最新值再合并)。

这套对应 2026 业界共识的 **keeps(①)/ merges(③候选)/ evicts(④⑤软逐)** 三件事,全部落在"只标不删"的软治理上(bi-temporal 铁律:考古与审计永远可回溯)。

---

## 8. 对照 2026 业界:当前在什么位置

调研了 Graphiti / Zep、mem0 v3、Letta、Cognee + 学术 survey 后的定位:

| 维度 | RootRecall | Graphiti / Zep | mem0 v3 | Letta |
|---|---|---|---|---|
| 双时间轴 bi-temporal | 有 | 领先(矛盾处理 63.8%) | 部分 | 弱 |
| 写时 append-only + 检索降权 | 有 | 有 | v3 核心 | — |
| 纠正链(失效 ≠ 删除) | 精巧(独立 corrected_by) | 边级失效 | Dream consolidation | — |
| 来源可信度分层加权 | 6 档 Bayes | — | 有 | — |
| access_count 巩固升级 | → mental_model | — | Dream merges | 3+ 规则 |
| 代码锚点溯源(file:line + sha) | **独有** | 无 | 无 | 无 |
| 记忆 + 代码 + 结构三路融合 | **独有** | 无 | 无 | 无 |
| 四类记忆 taxonomy | 有(对齐认知科学) | 弱 | 弱 | 部分 |
| 补丁合入上游检测 | 有(reverse-apply 标签+打折) | 自动失效 | Dream prunes | — |
| 语义近邻去重 | 有(cosine 簇,报候选) | — | Dream merges | — |
| 显式矛盾检测 | 有(needs_review 标签) | 强 | Dream resolves | — |
| 治理标签体检(dump 浏览+审计) | **独有**(溯源卡+健康概要) | 无 | 无 | 无 |
| 中文 BM25(jieba 两侧分词) | 有 | — | — | — |
| 外部依赖 | SQLite(零外部) | Neo4j(重) | 向量库 | 自管 |

**结论**:基础架构已经是 2026 一线水平(bi-temporal + 纠正链 + 来源加权 + append-only + 四类 taxonomy + 三路融合 + 五 pass 巩固),且在"代码库 bug-RCA"这个垂直生态位有**通用 agent memory 没有的差异化王牌**:① file:line + sha 代码锚点溯源;② 记忆 / 代码 / 结构同召回;③ 带溯源 + 治理标签的可审计团队记忆(Mem0 / Cognee 都没有的记忆体检)。原来的短板(consolidate 太薄)已补齐——keeps / merges / evicts 三件事全落地,且全部是"只标不删"的软治理。领域知识进 recall 的价值命题已有真数据实证:网调入库的 domain_knowledge 条目在 recall 中以最高分命中、排在一组 bug_lesson 前——给 bug-RCA 多一层证伪依据,正是治"锚定显眼日志行"误诊的设计意图。

**配套的记忆体检**:`memory_dump` 工具把全库摊开成带溯源的体检卡(confidence / tier / evidence / sha / 双时间轴 / 治理标签),memory-health-check skill 在标签之上语义读健康信号(溯源弱 / 待巩固 / 已过期 / 未决矛盾)——双层结构:确定性工具自动打标,agent 做语义判断。

### 明确不做(YAGNI,防未来跑偏)

调研里看着诱人但对本项目不值的,记下来:

- **迁 Neo4j 知识图(Graphiti 式)**:重依赖;SQLite + sqlite-vec 够用且零外部服务。记忆量级(单库几百到几千条)SQLite 完全 hold 住。
- **工作记忆 / 情景记忆分层(OpenHands 式随 workflow state)**:随 workflow state 走不另建。
- **物理删除 / eviction**:bi-temporal 软删是 2026 正确做法,别开倒车。
- **置信度曲线可视化**:MCP 工具输出是文本,画曲线是展示端的事;标签 + 计数已够体检用。
