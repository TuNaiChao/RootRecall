---
name: memory-health-check
description: 给一个 codebase 的长期记忆做"体检"——一次性把所有记忆条目摊开,逐条看它多可信(confidence)、来自哪(source_tier/commit_sha/evidence)、还有没有效(bi-temporal),聚出健康信号(溯源弱/待巩固/已过期/未决矛盾)和建议。用户问"我们对这个仓到底记了啥"、"记忆库质量怎么样"、"哪些记忆可信"、"帮我审一下记忆库"、"这些教训还有效吗"时用。
allowed-tools:
  - rootrecall_find_repo
  - rootrecall_memory_dump
  - rootrecall_memory_recall
  - rootrecall_memory_memorize
  - rootrecall_search_codebase
  - read
  - grep
  - glob
---

# 记忆库体检

你负责给 RootRecall 关于**一个代码仓库**的长期记忆做体检:把库里所有记忆条目摊开,逐条看清楚它**多可信、哪来的、还有没有效**,然后聚出健康信号和改进建议。读记忆卡、判健康信号是你的活;RootRecall 工具负责把记忆摊开(`memory_dump`)、查特定主题补充(`memory_recall`)、必要时核验引用的代码是否真存在。

**两个边界**(必须守):
- **只读不改代码,也默认不改记忆库** —— 你不 edit 源码(不 edit / 不 git apply);对记忆库你也**只看不动**(体检 = 出体检卡 + 建议,**不自动删 stale、不自动改 confidence**)。改记忆库是人的活(对齐「未经验证不 memorize / invalidate 谨慎」)。和 onboarding/compare 一样是 read-only,且更严——连记忆库都只读不写。
- **体检默认不 memorize** —— 体检本身**不产生新知识**(只是把已有记忆摊开看),所以**默认不 memorize**。**唯一例外**:体检中发现记忆库有**未决矛盾**(两条都 active、confidence 都高、但结论冲突)—— 这是关于记忆库本身的新观察,可 memorize 一条 `codebase_fact`「记忆库存矛盾:...」并标**需人工裁决**(你不确定谁对,只记录"这里有冲突待裁")。除此之外不记。

**核心难点**:体检不是「列个清单」就完——得从 dump 出来的条目里**读出健康信号**,这是**语义判断**(没有确定性工具能"自动给记忆打健康分")。四类信号是**双层结构**:consolidate(巩固)已经自动打了**治理标签**(dump 卡上的 `[needs_review]`/`[merged_upstream]`/`[stale]`),你的活是在标签之上**逐条读出语义**:① **溯源弱**——高 confidence 却没 evidence(file:line)也没 commit_sha(结论很自信但追不到代码,该补锚点);② **待巩固**——低 confidence 却高 access_count(被反复召回却不自信,可能值得 consolidate 升级);③ **已过期/被纠正**——invalid_at 已设 / 被 superseded_by 取代 / 被 corrected_by 纠正(标了 STALE 或 CORRECTED,占位但不该再用);④ **未决矛盾**——两条都 active、都高 confidence、结论却冲突(记忆库自相打架,要人裁;带 `[needs_review]` 标签的已被 consolidate 圈出,你核读双方内容判能否闭环)。`memory_dump` 只摊数据+标签,**读信号靠你**。

## 运行模式

> **先 dump,逐条读,聚信号**。和调研型 skill(onboarding/compare)的「先 recall」不同——体检的第一步是**把全量摊开**(`memory_dump`),不是按 query 挑几条。因为你审的是「整个库长啥样」,不是「某主题命中啥」。

1. **确认 codebase + 体检范围**:问清 codebase 名(如 `bluez`)+ 范围——整体审 / 只审某 kind(codebase_fact / bug_lesson / mental_model)/ 要不要连失效条目一起审(`include_invalid=True`)。然后 `memory_dump(kind=<可选>, include_invalid=<可选>, codebase=<codebase>)` 一次拉全量摊开。**注意翻页**:`memory_dump` 默认每页 60 条,header 若提示 `[showing 1-60 of N, more → memory_dump(offset=60)]` 说明没拿全——**体检要审全量,务必 bump offset 翻页直到拿完**(漏看一半会误判健康度,尤其可能漏掉未决矛盾的另一半)。**作用域发现走 dump/find_repo,别拿 recall 硬探**(2026-08-26 实测:recall 是主题检索,拿它探「哪些库有记忆」既浪费调用、非主题查询词还会被 sim<0.4 护栏正确拦下)——不知道记忆记在哪个项目名下时:dump 空结果会列出全部非空作用域,照着重调即可;或 `rootrecall_find_repo(project)` 看注册了哪些项目。**别漏 general 池**:domain_knowledge 全在共享池,`memory_dump(codebase="general")` 单独拉一次,否则漏审一整类知识。
2. **逐条读溯源卡**:对 dump 返回的每张卡,看四个维度——`conf`(置信度)/ `tier`(来源档:delegate/stated 最可信,tool 最低)/ `@file:line` 或 `@无证据`(溯源锚点)/ `sha`(commit 溯源)/ `STALE`(是否失效或被取代)/ `hits`(被召回次数)/ `[标签]`(治理标签:needs_review=未决矛盾候选,merged_upstream=补丁已在上游且 conf 已打折,stale=长期没人翻)。把可疑的(高 conf 无溯源 / 低 conf 高 hits / STALE / 互相打架 / 带治理标签)挑出来。
3. **聚健康信号**(你的核心推理活):把挑出的可疑条目归成四类——
   - **溯源弱**:高 conf(如 ≥0.7)但 `@无证据` 且无 `sha`。→ 建议:补 evidence/commit_sha 再信。
   - **待巩固**:低 conf(如 <0.4)但 `hits` 高(被反复用)。→ 建议:考虑 consolidate 升级成 mental_model(或确认是否该降权)。
   - **已过期**:`STALE` 标记(invalid_at / superseded_by)。→ 建议:人裁是否物理清理 / 确认取代链完整。
   - **未决矛盾**:两条都 active、都高 conf、结论冲突(如「X 函数是线程安全」vs「X 函数非线程安全」)。→ 建议:人裁;**这是唯一可 memorize 的情形**(记一条「记忆库存矛盾,需裁决」)。
     - **先判能不能闭环**:发现矛盾后,先读双方的 summary/detail 找有没有一方**显式说「纠正/推翻/更正」另一方**(如 summary 含「纠正先前…误诊」)。如果能判定谁对(corrector 明确)→ **不记「需裁决」**,而是调 `memory_memorize(kind=codebase_fact, corrects=[旧条id], summary="...", codebase=...)` 写纠正关系(旧条自动被标 `corrected_by` + 检索降权)。只有**真正无法裁定**的矛盾才留「需裁决」。
4. **出体检卡**(见下格式):总数 + 按 kind 分布表 + 四类健康信号(各几条 + 举例 summary + file:line)+ 建议。
5. **仅发现未决矛盾才 memorize**:step 3 发现矛盾 → 先判能不能闭环(见上)。
   - **能闭环**(一方明确纠正另一方)→ `memory_memorize(corrects=[旧条id])` 写纠正关系,旧条降权 → 矛盾已解,不记「需裁决」。
   - **不能闭环**(真正无法裁定)→ `memorize(kind=codebase_fact, summary="记忆库存矛盾:<A vs B>", codebase=<codebase>, confidence=<你的把握>)`,标**需人工裁决**。
   - 无矛盾 → **不记**(体检不产新知识,不污染库)。

## 工具(按需调)

| 工具 | 何时调 | 要点 |
|---|---|---|
| `rootrecall_memory_dump(kind?, include_invalid?, codebase?)` | **step 1 主数据源** | 一次返全量,每条带溯源卡(conf/tier/evidence/sha/STALE/hits)。体检的入口,必调 |
| `rootrecall_memory_recall(query, codebase?)` | step 3 查特定主题补充 | dump 只给 summary,某条你想看细节(detail/root_cause)→ recall 拉。或核对「这两条是否真矛盾」时按主题召全 |
| `read` / `grep` / `glob` / `rootrecall_search_codebase` | step 3 核验引用代码 | **必要时**才核:某条溯源弱但你怀疑它指向的代码早改了 → read/search_codebase 核 file:line 还在不在(防「记忆指向不存在的代码」)。不必每条都核(省 token) |
| `rootrecall_memory_memorize(...)` | step 5(仅发现矛盾) | 能闭环 → `corrects=[旧id]` 写纠正关系;不能闭环 → kind=codebase_fact 标「需人工裁决」;**无矛盾不记** |

## 硬约束

- **只读不改代码,默认不改记忆库** —— 不 edit 源码;对记忆库只看不动(不自动删 stale / 不自动改 confidence / 不自动 consolidate)。体检出的是**建议**,动手是人的活。比 onboarding/compare 更严:连记忆库都只读。
- **体检默认不 memorize** —— 体检不产新知识(只摊开已有记忆看),默认不记。**例外**:发现矛盾时能闭环(一方显式纠正另一方)→ `memory_memorize(corrects=[旧id])` 标纠正关系;不能闭环 → 记一条「存矛盾,需裁决」。无矛盾不记。
- **健康信号是语义判断** —— 没有确定性工具能自动给记忆打健康分;memory_dump 只摊数据,四类信号(溯源弱/待巩固/已过期/未决矛盾)靠你逐条读出来。这是本 skill 的核心价值。
- **溯源弱 ≠ 错** —— 高 conf 无 evidence/sha 只代表"该补锚点",不代表结论错;别在体检里推翻结论(推翻要读码/验证,是别的 skill 的活)。体检只标「信号 + 建议」。
- **核验 file:line 是「必要时」不是「每条」** —— 大多数记忆指向真代码,只有溯源弱且你怀疑代码已变时才 read/search_codebase 核;每条都核是浪费 token。

## 体检卡(你的输出格式)

```
记忆体检: <codebase>     范围: <整体 / kind=X / 含失效>

总量: N 条    codebase_fact: A   bug_lesson: B   mental_model: C
(若 include_invalid:)其中已失效/被取代: S 条

健康信号:
  ① 溯源弱(高 conf 无 evidence/sha): K 条
    - <summary>  conf=X.XX tier=Y  @无证据   → 建议补锚点
    ...
  ② 待巩固(低 conf 高 hits): K 条
    - <summary>  conf=X.XX hits=N  → 建议考虑 consolidate / 确认降权
    ...
  ③ 已过期/被纠正(STALE/CORRECTED/治理标签): K 条
    - <summary>  STALE(invalid/superseded)  → 建议人裁清理
    - <summary>  CORRECTED(by xxxxxxxx)  → 已闭环(纠正者已标记),检索已降权
    - <summary>  [merged_upstream] conf=X.XX(已打折)  → 补丁已在上游,确认无误后可人裁 invalidate
    - <summary>  [stale]  → 长期未被召回,建议读码核实是否过时
    ...
  ④ 未决矛盾(都 active 高 conf 结论冲突): K 组
    - A: <summary>  vs  B: <summary>  → 建议人裁(已 memorize 标需裁决)
    ...(带 [needs_review] 标签的已被 consolidate 圈出,你判能否闭环)

建议(给人的,不是自动执行):
  - <一句话:补溯源 / consolidate / 清 stale / 裁决矛盾 的优先级>
sources: 信号举例附 summary + file:line
memorize: 仅记了「存矛盾」(若适用);否则无(体检不产新知识)
```

`总量` 和 `kind 分布` 取自 step 1 的 dump 计数;四类信号是 step 3 的推理产出;`建议` 是 step 4 的优先级排序。

## 不要

- **改记忆库(删/改/自动 consolidate)** —— 体检只看 + 建议,改库是人的活。别为了"清理"自动 invalidate/改 confidence。
- **把体检当 recall 用** —— recall 是按 query 挑几条(解决"我查个主题");体检是摊全量看健康(解决"这库质量咋样")。用户要查具体主题 → 用 memory_recall,别误用本 skill。
- **只列清单不读信号** —— step 3 要从 dump 里读出四类健康信号,不是把条目罗列一遍就完。那只是 dump 的复读机,没价值。
- **溯源弱就推翻结论** —— 高 conf 无 evidence 只代表"该补锚点",不代表结论错;别在体检里判对错(那是 bug-rca/compare 读码验证的活)。体检只标信号。
- **每条都核 file:line** —— 大多数记忆指向真代码,只有溯源弱且怀疑代码已变才核;每条核是浪费 token。
- **无矛盾也 memorize** —— 体检默认不记(不产新知识);发现矛盾时能闭环→标 corrects,不能闭环→记「需裁决」。无矛盾不记。乱记污染库。
