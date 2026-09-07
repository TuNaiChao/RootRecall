---
name: optimization-roadmap-handoff
description: 2026-09-01 优化空间总盘点(以面试招聘标准)→ 11 项三阶段后续计划;含新会话可直接开工的已核实代码事实(file:line 锚点,免重探)、依赖顺序、验收标准与明确不做清单
metadata:
  type: project
---

# 优化路线图交接(2026-09-01,纯规划未动代码)

## 背景

用户先问「还有什么优化空间?以面试招聘要求为标准」→ 给了四层分析(架构/质量评测/功能/检索天花板)+ 面试讲解策略(会话内交付,未落档);再问「列出详细的后续计划」→ 产出本卡收录的 11 项三阶段计划。**本卡目的:新会话直接开工,不重做代码勘探**——下节所有事实均已在 0b36966 亲核。

## 仓库现状快照(开工前先对表)

- 本地 = GitHub = 远端测试机 = **0b36966**,413 pytest 全绿 + ruff clean,工作树净(仅本地不入库的踩坑文档)。
- 远端测试机有两处**本机私有改动,别带进上游**:config.yaml 的 `models_from_opencode` 采纳段 + `model_roles` 全切宿主模型(heavy=glm-5-turbo / light=deepseek-v4-flash-0731)。
- 远端机器地址/密码不入卡,见 `docs/测试踩坑记录.md`(仅本机);github 间歇卡死走 git bundle 内网同步(正端点必须 ref 名)。
- 升级三连:git pull/bundle → `uv run rootrecall install --global` → 重启 opencode 会话。

## 已核实代码事实(新会话免重查)

| 事实 | 锚点 |
|---|---|
| 无 CI:`.github/workflows/` 不存在 | — |
| 零 key 时 `rootrecall index` 在 embedder 创建处直接退出,结构图构建排在向量索引之后被连坐 | cli.py:138-139(退出点)、153+(图构建) |
| `--no-graph` 反向开关已有(只建向量不建图);缺 `--graph-only` 正向开关 | cli.py:818(baseline add)、870(index) |
| `baseline checkout --index` 已有「embedder 不可用诚实跳过」逻辑,但同样连坐图 | cli.py:596-609、837 |
| memory CLI 只有 ingest / recall,**无 backfill 入口** | cli.py:252、293 |
| 零 key 期间写入的记忆 embedding 为空只走 BM25,配 key 后永远不补嵌 | memorize.py:46 `_embed_items` |
| consolidate 两处 O(n²) 两两 cosine:矛盾检测、语义近邻簇 | consolidate.py:158-160、188-190 |
| sqlite-vec 基础设施已就位(count>500 自动启用),可复用作近邻预筛 | store.py 搜索路 |
| 已有 eval 只有 L1 检索自评(runner+scorer),无 workflow 级回归 | services/code_index/eval/ |
| e2e 测试带诚实 skip(无 opencode/fixtures 自动跳过),对干净 CI runner 友好 | tests/e2e/test_p1_analyze_agent_e2e.py:96 等 |
| `requires-python >=3.12`(CI 不用开 matrix);pyproject 配了清华 uv index(默认 index,CI 上可达) | pyproject.toml |
| 图系测试需 `uv sync --extra code-review-graph`(纯 pip 可装);embedding-local 不进 CI(要下模型) | pyproject.toml optional-dependencies |

## 11 项计划(三阶段)

### 阶段一·地基(合计约 3 天,三项互相独立可并行)

**① CI(GitHub Actions),0.5–1 天 —— ✅ 已成(2026-09-02,待 commit/push 后 Actions 首绿)**
- 一个 `ci.yml` 两 job:lint(ruff check)/ test(Python 3.12 单版本,`uv sync --frozen --extra code-review-graph --extra mcp`,`uv run pytest -q`);setup-uv@v10.0.1 `enable-cache`(v8 起 tag 不可变须钉全版本号),checkout@v7。
- **落地时三处修正(均经 git worktree 干净环境实证,2026-09-02)**:① test job 必须加 `--extra mcp` —— test_mcp_tools 33 用例经 build_server 用 FastMCP(mcp_memory.py:365 函数内 import),只装 code-review-graph 会运行时 ModuleNotFoundError;② lint 只跑 `ruff check` 不跑 `format --check` —— 仓库从未 format 过,src 58 + tests 34 共 92 文件漂移,加进去 CI 立刻红(是否一次性 `ruff format` 收编待用户拍板);③ `test_search_codebase_per_call_codebase` 隐性依赖真机 .env(embedder/reranker 构造期要 api_key)→ 已桩掉两件套修复,干净环境 413 全绿、本机复测亦绿。
- 验收:push 后 Actions 绿、PR 自动跑、README 徽章(已加;badge 在 workflow 落到 GitHub main 前会 404,属预期)。

**② 图系工具与 embedder 解耦(性价比最高),1–1.5 天 —— ✅ 已成(2026-09-02)**
- `rootrecall index <repo> --graph-only`(跳过 embedder 与向量索引只建图,`baseline add` 也有同参);零 key 不加开关时向量路诚实跳过但**图照建不再连坐**(rc=2 提示向量未建,指路文案三条路);`--no-graph` 与 `--graph-only` 互斥检查;播种分路(向量播种仅在 embedder 可用时,图播种不受影响);checkout --index 经 cmd_index 漏斗自动受益。
- 核实落定:4 图系工具(blast_radius/call_chain/repo_map/repo_overview)全走 `CodeGraph.open`,零 embedder。
- 文档同步:configuration.md 最小模式表拆细(新增「零 key 只用结构图」档)+ README 首段 + cli.md。
- 验收(2026-09-02 干净 worktree 拔 key 实测):`index --graph-only` 后 4 图系工具返真实图数据、search_codebase 诚实指路缺 key、零 key 裸跑 rc=2 图照建;全 key 回归 417 测全绿(413+4 新单测 tests/test_cli_index_graph_decouple.py)、ruff clean。

**③ 记忆向量 backfill,0.5 天 —— ✅ 已成(2026-09-03)**
- CLI `rootrecall memory backfill [--repo X] [--dry-run]`:扫 active 且 embedding 空的条目→`embed_texts`(新加的 Embedder 公共批量接口,按 batch_limit 分批)→`store.update_embeddings`(**只 UPDATE 向量列** + 同事务双写 vec0,不碰置信度/时间戳,天然不触发 Bayes/合并)。幂等可重跑;单批 API 失败跳过不阻断;零 key 诚实报错指路(dry-run 同)。quickstart/零 key 指路/configuration.md 已加「配 key 后跑一次 backfill」。
- 验收(单测 + 真 API e2e 双验,2026-09-03):单测(fake embedder,中英零共同 token)补嵌前纯语义查询空、补嵌后 top-1 命中、重跑 0/0 且置信度/valid_at 不动;真 e2e(worktree 拔 key 记 2 条→source .env→backfill)「handsfree call establishment」top-1 命中「车载免提通话链路的建立流程」(Qwen3 真跨语言语义),重跑零变更。顺带抓修 CLI dry-run 分支没接 ValueError 的 bug。4 新测,全量 421 绿。

**阶段一(①②③)至此全部完成;下一步:④ eval-L2(⑧ 的前置)。**

### 阶段二·质量与体验(合计约 8–12 天)

**④ e2e 回归 eval harness(重头)—— L2 ✅ 已成(2026-09-07);L3 未做(3–5 天,规格如下)**
- **L2 检索回归已成**:入口 `rootrecall eval run --level retrieval --mode stub|live`(cli.py cmd_eval);评测集 `eval/sets/rootrecall.jsonl` 28 条(18→28,补英文+L2 概念题;索引根=src/rootrecall 是 gold 路径契约)。harness 在 `services/code_index/eval/l2.py`。
  - **stub(进 CI,零 key 秒级)**:HashEmbedder(512 维带符号特征哈希;64 维计数版实测 hit@5 只有 0.18-0.36,RRF 被碰撞噪声污染,教训在 l2.py 注释)+ rerank off。**双门**:BM25 强门(经新增 `LanceDBStore.fts_search`,排除哈希噪声稀释;纯 BM25 排不过短包装 chunk 是已知边界 —— L1≈0.62 是 BM25 单路真实水平,拉满靠 rerank+先验,归 live)+ hybrid 冒烟弱门;外加 oracle 有效性守卫(gold 不在索引=改了符号没同步 eval 集,直接失败点名)+ memory_recall 播种断言。阈值全按实测 −5pt 校准(注释里带实测数)。
  - **live(有 key 机器,跑分对比底座)**:真 embedder+reranker 全指标,报告落 `data/eval/<ts>-retrieval.json` + 自动与上一份 diff;硬下限 hit@5≥0.80/mrr≥0.68/L1_mrr≥0.92。**2026-09-07 真机基线:hit@5=0.857 / mrr=0.738 / L1 mrr=1.000 / L2 hit@5=0.733(28 条),两跑全等(live 亦确定)**。前置:`rootrecall index src/rootrecall rootrecall` 建真索引。
  - **验收全过**:CI 加 eval 步骤;harness 自检入单测(test_eval_l2.py:打坏 FTS 查询→强门必红;哈希跨实例确定);干净 worktree(无 .env/key)CLI+单测全绿且分数与主仓一致。3 新测全量 424 绿。
  - **领导微观跑分对接**:live 报告 + diff 就是对比表数据源;grep 基线对照组如需,再加 `--mode bm25-baseline`(fts_search 已具备,半天)。
- **L3 workflow 回归(未做)**:每金标场景一个 case 文件(问题/开仓参数/oracle:期望根因文件、补丁目标、报告必备要素);runner 走 delegate 老 headless 或 MCP 序列回放;scorer 分卡:根因文件命中率、file:line 引用真伪率(逐条对仓核)、补丁 apply 率、工具调用次数(检测 recall-first 短路失效)。样本从 3 金标(wpa P2P / bluez SDP / EATT)扩到 6–8。

**⑤ consolidate 提速 + 调度 —— ✅ 已成(2026-09-07)**
- 近邻 pass:分块矩阵 **top-K 预筛**(每条 top-50 再判 0.92,O(n²) 比较变 O(n·50) 进并查集;归一化后点积即 cosine;混合维度按 dim 分桶,跨维不比 = 旧 `_cosine` 语义;块 512 控内存)。**与卡的偏差**:没用 sqlite-vec ANN —— n 次 vec0 KNN 往返(估 2.5–10s)不如一次性 BLAS 分块 matmul(5000×16 实测 0.47s 全程),且是**精确** top-K 非近似;sqlite-vec 路留在检索路不动。
- 矛盾 pass:桶式预分组(symptom 精确桶 + 证据文件×kind_detail 桶,组内两两仍过 `_same_subject` 行窗;跨桶对不可能同主题,**与暴力两两语义等价**,对拍单测锁死,边界例含 symptom/证据/混合路径/行窗内外)。
- deploy/ 加 `rootrecall-memory-consolidate.{service,timer}`(每日 03:40 错开 sync;`--repo-path` 给 pass④ 仓;纯本地零模型零 key)+ README 一段。
- 验收:合成 5000 条 consolidate **0.47s**(验收线 10s,20 倍裕量);两 pass 与暴力参考对拍一致(含混合维度/零向量/无向量);3 新测,全量 427 绿,ruff clean。

**⑥ seed 记忆冷启动 —— ✅ 已成(2026-09-07)**
- `rootrecall memory seed <codebase> [--repo-path P] [--max-modules 12] [--light]`:full 档走 `architecture_overview().communities`(裸 communities() 的 sample_members 实测全空,真机抓出)→ 轻 LLM(title 角色)每模块一条 codebase_fact(evidence 真 file:line 从图节点取,GraphNode 对象/dict 双形态兼容);--light 摄取 README/CHANGELOG(各截 8k)成 domain_knowledge 入 general 池。全标 `source_tier: inferred`(conf 0.35),真结论经 Bayes 接管。
- **幂等双层**:id 级(同 summary 同 id)+ **锚点级**(同 scope 已 seed 卡的首证据文件 → 跳过该模块;真 LLM 两次措辞不同会让 id 级失效,锚点级是真机幂等的关键,重跑实测写 0 跳 6)。
- 验收(单测 + 真机):seed 后 recall「检索融合的模块在哪」top 命中(conf=0.35、tier=inferred、evidence=chunker.py:73 真行号);重跑零新增;--light 真机写 5 条;图未建/零 key/缺 README 均诚实指路。3 单测,全量 434 绿。

**⑦ delegate/runtime 双轨清理 —— 轻档 ✅ 已成(2026-09-07);重档待下个大版本(物理删 + deprecation,远端机器还依赖时别删)**
- 轻档落地:configuration.md 两段(delegate/runtime)挪文末「附录·legacy」并加统一口径框(**建过又砍掉,保留作演化记录;主线不走**);项目介绍.md 架构形态节补「模块留仓作演化记录」;mcp-guide.md 隐藏 stage 措辞改「遗留,仅演化记录用」。正文任何路径不再把 delegate 当活路径(验收过:正文零 delegate/runtime 出现)。

**⑧ HyDE/query 改写 —— ✅ 机制已成,默认关(A/B 负增益,按纪律不上)**
- 实现:`_rewrite_query` 挂 `retrieve()` 入口(retrieval.py),`ROOTRECALL_QUERY_REWRITE=1` 开启;title 便宜角色;进程内缓存;零 key/失败降级原查询;话痨防御(>500 字符丢弃)。eval 与 search_codebase 共用此路,A/B 免费获得。
- **A/B 结果(2026-09-07 live eval 28 条,真机)**:改写开 → hit@5 0.857→**0.821**、mrr 0.738→**0.662**、L1 mrr 1.000→**0.942**、L2 hit@5 0.733→0.667 —— 全面负增益,连 live 硬下限都跌破(mrr 0.662 < 0.68,门正确变红 = 顺带二次验证了 eval harness 敏感性)。按「有增益才上」纪律**默认保持关**;换更强改写模型再开开关复测(报告 diff 自动对比)。4 单测(默认旁路/开启+缓存/失败降级/话痨防御)。

**至此 11 项中 ①②③④L2⑤⑥⑦轻⑧ 全部落地;剩 L3 workflow 回归(3–5 天大件)、⑨⑩⑪(按触发条件)、⑦重档(下个大版本)。**

### 阶段三·择机(按触发条件启动,合计约 5 天)

| # | 触发条件 | 要点 |
|---|---|---|
| ⑧ HyDE/query 改写,2 天 | ④L2 建好后 | 检索前轻 LLM 改写(中文口语→查询语言+符号名),title 便宜角色,零 key 跳过;**必须 A/B,L2 有增益才上** |
| ⑨ schema migration 框架,1 天 | 下次 schema 变更时 | `PRAGMA user_version` 版本化迁移链,收编已有三次手写迁移(T11 双列/jieba 重灌/T23 版本 scope);备份目录有各版本真 DB 可当测试样本 |
| ⑩ 记忆脱敏,1 天 | 记忆要出机器时 | memorize 入口 redact pass:内网路径前缀/IP/token 模式 |
| ⑪ 多用户共享,1–2 天(B 档) | 第二个真人用户时 | B 档 `memory export/import`:id 内容寻址,两库 merge 天然无冲突(同 id=同事实=Bayes 合并);A 档(共享 Postgres)等真需求 |

## 依赖与开工顺序

```
①CI ──(测试基线固化)──→ ④eval-L2 ──→ ④eval-L3 ──→ ⑧HyDE(A/B 依赖 eval)
②③ 互相独立,与 ① 也可并行
⑤⑥⑦⑨⑩⑪ 互相独立,按触发条件排期
```

建议:①→②③(可并行)作阶段一一次交付;④L2 先行(是 ⑧ 前置,也给 CI 加回归网);⑤⑥⑦ 穿插。
顺序理由:CI 复利越早越便宜;②③ 是零 key 用户最大可感知解锁;④ 唯一能守护现有质量战绩(零幻觉/42→4)不被后续改动磨掉,面试里「有可回归 eval harness」比「测过一轮」硬一个量级。

## 明确不做(YAGNI,防跑偏)

迁 Neo4j 知识图 / 物理删除 eviction / 工作记忆分层建库 / 置信度可视化 —— SQLite+软删+标签已覆盖当前量级。同源结论在 docs/memory-module-analysis.md §8。

## 纪律提醒(新会话开工必守)

- commit 节奏:每完成一个完整功能(单测+e2e+文档)提醒用户该 commit,同意后显式路径 `git add <files>`(不 -A);push 单独确认,commit ≠ push。
- 敏感信息(IP/密码/key 值)一律不写进任何入库文档;永不打印 key 值。
- `docs/踩坑记录.md` 与 `docs/测试踩坑记录.md` 仅本机不入库;Python语法.md、todo.md 永不入库。
- 只到 apply 不编译的公共纪律不变;新功能落地后自跑 e2e(不等用户)。

关联 [[agent-project-overview]](三支柱总览)/ [[pitfall-log]](设计前先查坑)/ [[memory-design-review-2026-08-12]](记忆层评审口径)/ [[harness-pivot-handoff]](⑦ 双轨的历史由来)。
