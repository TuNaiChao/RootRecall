# MCP 工具参考(17 个)

> RootRecall 把自己的差异化能力做成 MCP 工具,挂在 server 名 `rootrecall` 下 —— opencode 按 `rootrecall_<工具名>` 调用(如 `rootrecall_search_codebase`)。工具实现在 [mcp_memory.py](../src/rootrecall/tools/mcp_memory.py),配置见[配置参考](configuration.md)。
>
> 一句话理解工具分工:**读码、改代码这类重活,opencode 自己就会,RootRecall 不做**;这里只提供 opencode 做不好 / 做不了的 —— 跨会话的记忆、防幻觉的代码检索、结构图与 git 的确定性分析、交付硬门。

## 启动与接入

```bash
uv run rootrecall mcp serve [--codebase 仓库名]          # stdio(默认,推荐)
uv run rootrecall mcp serve --transport http [--host --port]  # warm 长进程,端点 http://<host>:<port>/mcp
```

| transport | 原理 | 适用 |
|---|---|---|
| `stdio`(默认) | agent 每次拉起一个子进程,1:1 生命周期 | 本地单机、opencode 接入(**推荐**,工具注册最可靠) |
| `http` | 一个常驻进程,多个 agent 共用 | 省冷启动;opencode 需相应配置 |

opencode 的接线(软链、skill、启动目录要求)见 [README](../README.md)「使用」。

## 默认 codebase 与 per-call 覆盖

server 启动时解析一个**默认 codebase**,顺序:启动参数 `--codebase` → 环境变量 `ROOTRECALL_CODEBASE` → `config.code_index.repo` → 进程所在目录名。

多仓场景不用起多个 server:下表标 🔀 的 10 个工具都接受可选 `codebase` 参数,**每次调用**覆盖默认值 —— 同一个会话里可随时切仓。数据层本就按仓隔离(索引一仓一表、记忆按 scope 隔离),per-call 只是解锁调用层。

不需要 `codebase` 参数的 6 个:validate_patch / export_patch / export_report(按绝对 `repo_path` 干活)、when_introduced(同)、fetch_patch(按 URL)、ensure_repo(按名字/URL 解析路径)。

**近义名容错**(2026-08-25,实测教训:记忆吃项目名 `bluez`、索引/图吃注册名 `bluez-v25`,agent 连败多次才摸到正名):索引/图类工具的 `codebase` 参数按「精确 > 归一化(`_`↔`-`/大小写)> 唯一子串」三级解析 —— 唯一近义命中自动纠偏(输出头注明,如 `codebase 'bluez_v25' 近义解析为 'bluez-v25'`);多个候选则列出全部让 agent 一次改对;完全未知名列出本机已知清单(`rootrecall baseline ls` 同源:注册表 ∪ 索引清单 ∪ 结构图目录)。报错还区分「在册但没建图/索引」(指路 `rootrecall index` 重建)vs「完全未知」(指路 `baseline add`)。记忆三件套(memory_recall/memorize/dump)**不走**这套容错 —— 记忆 scope 吃项目名是既有语义(教训跨版本共享),版本线名留给索引/图工具。

## 工具一览

| 工具 | 组 | 一句话 |
|---|---|---|
| [memory_recall](#memory_recall) | 记忆 🔀 | 按问题翻长期记忆(bug 教训 / 代码事实 / 领域知识),带 file:line 溯源 |
| [memory_memorize](#memory_memorize) | 记忆 🔀 | 写一条记忆;支持声明「纠正了哪条旧结论」 |
| [memory_dump](#memory_dump) | 记忆 🔀 | 全量摊开做体检 / 审计(区别于 recall 的按问检索) |
| [search_codebase](#search_codebase) | 代码情报 🔀 | 语义+符号检索,**只回索引里真实存在的符号**(防幻觉) |
| [blast_radius](#blast_radius) | 代码情报 🔀 | 改动影响面:动这些文件会波及谁(涟漪图) |
| [call_chain](#call_chain) | 代码情报 🔀 | 一个函数的 N 跳调用链 + 谁结构上重要(手电筒照一条路) |
| [repo_map](#repo_map) | 代码情报 🔀 | 全仓最重要符号排行榜,按 token 预算打包 |
| [repo_overview](#repo_overview) | 代码情报 🔀 | 架构卫星图:模块社区 / 枢纽 / 咽喉 / 耦合告警 |
| [cross_version_diff](#cross_version_diff) | 代码情报 🔀 | 同仓两个版本间改了啥 / 修了没(git 为核,图可选) |
| [merge_eval](#merge_eval) | 代码情报 🔀 | 上游 commit 逐个三态判定:已修 / 建议合 / 冲突 |
| [when_introduced](#when_introduced) | 代码情报 | 一段缺陷逻辑是哪个 commit 引入的(纯 git) |
| [validate_patch](#validate_patch) | 硬门 | 补丁能否干净 apply(零 LLM 的执行关卡) |
| [export_patch](#export_patch) | 硬门 | 把改动落盘成 `.patch`(用户开口才调;空 diff 拒写) |
| [export_report](#export_report) | 硬门 | 把报告落盘成 `.md`(用户开口才调;可另蒸馏一份 AGENTS.md) |
| [fetch_patch](#fetch_patch) | PR 抓取 | PR 链接 → diff + 元数据(GitHub / Gerrit) |
| [ensure_repo](#ensure_repo) | PR 抓取 | 仓库名/URL → 本地路径,缺则自动 clone |
| [find_repo](#find_repo) | 开仓 | 「项目+版本」→ 注册表候选仓;没开过仓就给自动开仓命令 |

## 记忆(3 个)

记忆库像一间**案卷室**:每条知识带置信度、来源、代码锚点和生效时间,recall 是按问题翻卷宗,dump 是把卷宗全摊在桌上审计。

### memory_recall

```python
memory_recall(query: str, top_k: int = 5, kind: str | None = None, codebase: str | None = None) -> str
```

| 参数 | 说明 |
|---|---|
| `query` | 自然语言问题(描述概念,不是猜符号名) |
| `top_k` | 返回条数(默认 5) |
| `kind` | 过滤:`bug_lesson`(历史修法)/ `codebase_fact`(代码事实)/ `domain_knowledge`(领域知识)/ `mental_model`(经验法则);省略 = 全部。给了 kind 会多取再过滤,不会饿死结果 |
| `codebase` 🔀 | 覆盖查哪个仓的记忆(默认 = server 默认仓);记忆按仓隔离,不串库。**general 池永远并查**(见下) |

定位 / 改补丁前先调它,复用同仓的历史根因和修法。输出每条带 file:line 溯源 + 置信度 + 时间 + tags(主题域标签,短路判定用);被纠正过的条目带「已被纠正」标记且检索降权(仍可见,作参考);`unverified` 条目带「(未真机验证)」显式渲染 —— 先验可用,坐实与否一眼可辨。

**general 池并查**(2026-08-26 实测教训:同一条 A2DP 知识一条记 bluez、一条记 general,单池 recall 查一个漏一个):每次 recall 除你传的 codebase 外**总是并查共享 `general` 池**(领域知识所在地),命中按 id 去重、跨池条目前缀 `[general]` 标明;空结果列出非空作用域清单,`codebase` 传错一次就能改对(服务器默认作用域常常是空池)。配套写侧规则:`memorize(kind=domain_knowledge)` **无视传参强制落 general 池**。

### memory_memorize

```python
memory_memorize(kind, summary, file=None, line=None, evidence=None, root_cause="", fix_patch="",
                symptom="", blast_radius_files=None, commit_sha=None, tags=None, corrects=None,
                kind_detail=None, confidence=None, source_url=None, codebase=None,
                verification=None) -> str
```

| 参数 | 说明 |
|---|---|
| `kind` | `codebase_fact` / `bug_lesson` / `domain_knowledge` |
| `summary` | 一句话事实 / 教训 |
| `evidence` | 多锚点溯源,`[{"file": 路径, "line": 行号?, "snippet": 片段?}]`;架构类事实(跨多处代码)优先用它,单锚点场景才用 `file`+`line` |
| `fix_patch` | unified diff;给了则条目 id 按补丁内容算 —— 同一补丁重复 memorize 会**合并**(置信度累加)而不是重复入库 |
| `corrects` | 被本条纠正的旧条目 id 列表(在 recall / dump 输出里看到的 id)。旧条目保留可审计、检索降权,不删除 |
| `kind_detail` | 细分类:`module` / `symbol` / `architecture` / `domain`(bug_lesson 不用) |
| `confidence` | 0..1 显式置信度;不给则按来源档取默认 |
| `source_url` | domain_knowledge 的外部溯源 URL;给了记 `imported`(网调),不给记 `stated`(使用者笔记) |
| `verification` | 验证档:`apply_only`(默认可早记 —— 补丁过 validate 后就记,条目自动打 `unverified` 标 + 置信封顶 0.5,recall 渲染带「(未真机验证)」)/ `real_machine`(真机验证通过后**同一补丁重提一次**,同 id 合并升级、洗掉 unverified 标) |
| `codebase` 🔀 | 覆盖写进哪个仓的记忆 |

bug-RCA / 补丁鉴定流程会自动 memorize;这个入口用于现场发现的事实 / 教训。验证纪律是结构化的:**没真机验证过的教训照样能记(先验有价值),但 `unverified` 标 + 低置信让每次召回都看得见「未坐实」** —— 升级不是改条目,是同补丁重提一次。

### memory_dump

```python
memory_dump(kind=None, include_invalid=False, codebase=None, limit=60, offset=0) -> str
```

| 参数 | 说明 |
|---|---|
| `kind` / `include_invalid` | 过滤种类 / 是否连失效条目一起看(默认只看 active) |
| `limit` / `offset` | 分页,默认每页 60 条。header 会明示「showing 1-60 of N」—— 体检要全量,按提示翻页,别只审一片切片 |
| `codebase` 🔀 | 覆盖看哪个仓的记忆 |

每条渲染成溯源卡:置信度 / 来源档 / evidence file:line / commit_sha / 双时间戳(失效标 STALE、被纠正标 CORRECTED)/ 被召回次数 / 条目 id。header 附健康概览(治理标签计数:待复核 / 已合入上游 / 长期未翻)。

## 代码情报(8 个)

三种「俯瞰 vs 聚焦」的视角互补:repo_overview 是**卫星图**(城市怎么分区、哪个路口是枢纽),repo_map 是**排行榜**(全城最重要的符号),call_chain 是**手电筒**(照一条调用路径),blast_radius 是**涟漪图**(丢颗石头看波及多远)。时间轴上另有 cross_version_diff / merge_eval / when_introduced 三个 git 系工具。

前置:这组工具依赖**向量索引 / 结构图**,先建:

```bash
uv run rootrecall index <仓库路径> <仓库名>   # 向量索引 + 结构图一次到位
```

### search_codebase

```python
search_codebase(query: str, top_k: int = 5, codebase: str | None = None) -> str
```

| 参数 | 说明 |
|---|---|
| `query` | **概念 / 自然语言**(如 `"p2p scan result routing"`),不是猜的文件名 / 函数名 |
| `top_k` | 返回条数(默认 5) |
| `codebase` 🔀 | 覆盖查哪个仓的索引 |

**防幻觉契约**:结果只来自真实索引,每条带 `file:行区间 (kind symbol) score` + 首行内容 —— 模型拿不到一个编造的路径。检索路径(BM25+向量+RRF+重排)见[记忆模块分析](memory-module-analysis.md)。

**排序先验**(2026-08-26 实测教训:bluez 问「连接流程」,top-6 全是 emulator/android 外围符号,核心入口 `device_connect_le` 挤不进):重排后按「符号粒度(module/私有 helper 降,公共入口不动)× 路径基建(test/tests/emulator/unit/example 等目录段、`*-test*`/`*-tester` 文件名降 0.70)」叠乘再排序 —— 测试/仿真基建**降而不剔**,专门查它们时仍可进 top-k。实务建议:泛概念查询若命中全是外围,果断转 grep 已知命名模式锚定核心文件(见 compare skill 的战术条)。

### blast_radius

```python
blast_radius(changed_files: list[str], codebase: str | None = None) -> str
```

给一组被改文件,返回还会波及谁(caller / callee / 依赖方,结构图 BFS)。评估补丁 / PR 影响面用。文件路径传仓库相对或绝对都行(内部做后缀解析容错)。需结构图;未建返回带建法提示的可操作串。

### call_chain

```python
call_chain(symbol: str, direction: str = "both", depth: int = 2, top_n: int = 15, codebase: str | None = None) -> str
```

| 参数 | 说明 |
|---|---|
| `symbol` | 函数名;裸名(`wpa_supplicant_init`)或带文件限定(`wpa_supplicant.c::wpa_supplicant_init`)都行 |
| `direction` | `callers`(谁调它)/ `callees`(它调谁)/ `both`(默认) |
| `depth` | 跳数(默认 2,封顶 5 防大图爆炸) |
| `top_n` | 每方向返回上限(按「跳数升序 → PageRank 降序」取,默认 15) |

输出每个节点带 file:line、跳数、PageRank 分。与 blast_radius 的分工:blast_radius 是**文件**种子问「波及面」,call_chain 是**符号**种子问「调用上下文」。

### repo_map

```python
repo_map(map_tokens: int = 2048, codebase: str | None = None) -> str
```

全仓调用图跑 PageRank,把最重要的符号按文件分组打包进 `map_tokens` 预算(Aider 式 repo map)。调研 / 定位前先拿一张「哪些函数结构上最核心」的骨架图。要更小的图就减小 map_tokens 重调。

### repo_overview

```python
repo_overview(top_n: int = 15, max_communities: int = 30, codebase: str | None = None) -> str
```

| 参数 | 说明 |
|---|---|
| `top_n` | 枢纽 / 咽喉节点各返回几个(默认 15) |
| `max_communities` | 模块社区上限(默认 30,按大小降序;header 诚实报真实总数) |

一次聚合四个图查询(社区检测 / 枢纽 / 咽喉 / 跨社区耦合告警),纯图计算零 LLM —— 讲「这仓分几大模块」靠社区检测,不是模型编的。输出顺序刻意把枢纽 / 咽喉 / 告警放前、社区清单放后,大仓截断也先丢最不重要的部分。

### cross_version_diff

```python
cross_version_diff(base_ref: str, head_ref: str, repo_path: str,
                   concern_files=None, concern_symbols=None, top_commits=30, codebase=None) -> str
```

| 参数 | 说明 |
|---|---|
| `base_ref` / `head_ref` | 同仓两个 git ref(tag / commit / branch,如 `5.50` / `5.85`) |
| `repo_path` | 仓库工作树**绝对路径** |
| `concern_files` / `concern_symbols` | 只关心这些文件 / 符号(给了才回 concern 的完整 diff,防全量 diff 爆炸) |
| `top_commits` | commit 列表上限(默认 30) |

回答「旧版本有的问题,新版本修了没、怎么修的」:base..head 提交清单(确定性门)+ concern 的 git diff + (有图时)触及函数 + cherry 等价摘要。**git 为核,图可选** —— 没建结构图也能跑核心产出。

### merge_eval

```python
merge_eval(upstream_base_ref: str, upstream_head_ref: str, fork_ref: str, repo_path: str,
           concern_files=None, max_commits=50, codebase=None) -> str
```

维护 fork 时的逐 commit 三态判定:`already_fixed`(fork 里已有 patch-id 等价提交)/ `recommend_merge`(没合过、能干净合)/ `conflict`(合不干净);另有 `uncertain` 兜底。冲突检查用 `git merge-tree --write-tree`(git 2.38+)在对象库完成,**零 touch 工作树** —— 不需要 checkout、不要求干净树;老 git 自动回退 `git apply --check` 并在 note 里声明三态可能失真。

两个边界:① 上游要先 fetch 进本仓让 ref 可解析(agent 自己跑 `git remote add + fetch`,工具不做);② 「能不能合」是确定性地板,**「该不该合」(fork 是否真有这个 bug)是语义判断**,用触及文件 + search_codebase + call_chain 综合评估。

### when_introduced

```python
when_introduced(repo_path: str, symbol: str | None = None, file: str | None = None,
                line: int | None = None, line_end: int | None = None, max_commits: int = 20) -> str
```

双锚点(二选一)找「缺陷逻辑是哪个 commit 引入的」:

| 锚点 | 机制 | 适合 |
|---|---|---|
| `symbol`(配 `file` 收窄) | pickaxe(`git log -S`):哪些 commit 增删过这个字符串 | 知道函数 / 标识符名 |
| `file` + `line`(可带 `line_end`) | 行历史(`git log -L`):哪些 commit 动过这行区间,改名自动跟随 | 锚到具体行 |

候选表按时间倒序带 added/removed 计数:引入者通常是最老的 added>0 / removed==0 那条,中间成对增删的多是重构搬移。**哪条真引入了缺陷是语义判断**:逐条 `git show`,引入 commit 的 message / diff 常直接暴露根因意图 —— 定位时是一路交叉证据。纯 git,零 LLM 零图依赖。

## 硬门(3 个)

交付标准:**聊天回复不算交付** —— 补丁要上盘成 `.patch`,报告要上盘成 `.md`,才叫跑完。

### validate_patch

```python
validate_patch(patch: str, repo_path: str) -> str
```

正向 `git apply --check`(strict → `--3way` → `patch -p1` 逐级降级),返回能否干净 apply + 方法 + git 诊断。只做正向、只验 apply(Tier 0):**不保证补丁语义对** —— 语义靠读码推理 + 真机验证。入口已对传参做换行归一化(防 agent 传参时 rstrip 掉末尾换行造成「补丁损坏」误判)。

### export_patch

```python
export_patch(repo_path: str, out_dir: str = "data/bug_rca") -> str
```

何时调:**用户开口**(「生成补丁」/ 要拿去真机验证)才调,迭代中间版不自动落盘 —— 循环里 `edit` + `validate_patch` 就够,落盘是交付动作不是迭代步骤。收集 repo_path 里**全部未提交改动**(`git add -A && git diff --cached`,含新增文件),写 `<out_dir>/<仓库名>.patch`。**空 diff 拒写** —— 治「改错树 / 没保存 / 被 gitignore」这类静默失败。两个顺手活:debian 源码仓的 quilt 构建产物 `.pc/` 自动排除(否则几行修复会混进几十万行垃圾);该检出在注册表里带 bug 号时,同款补丁**另归档一份**到 `<out_dir>/<bug号>/`(`repo gc` 回收 ephemeral 后交付物仍可按 bug 追溯)。副作用:会 stage 改动(可 `git reset` 撤)。apply 验证不在这做(对已改过的树正向 check 必失败),先过 validate_patch。

### export_report

```python
export_report(content: str, repo_path: str, out_dir: str = "data/bug_rca", agents_md: bool = False,
              topic: str | None = None) -> str
```

| 参数 | 说明 |
|---|---|
| `content` | 完整 markdown 报告(根因 + 证据 + 补丁要点 + validate 结果 + patch 路径 + memorize id) |
| `topic` | 主题短 slug(如 `connect-flow-compare` / `a2dp-protocol` / `bug-1234`)→ 文件名 `<repo>-<topic>-rca.md`。**同仓多主题报告必传** —— 不传共用 `<repo>-rca.md` 会互相覆盖(2026-08-26 实测:A2DP 报告盖掉连接流程对比报告);同 topic 重跑 = 幂等覆盖并注明。省略 = 旧文件名(向后兼容) |
| `agents_md` | 额外把报告蒸馏成 `<repo_path>/AGENTS.md`(「给 agent 看的 README」,opencode / claude code / cursor 原生读取)。**默认关** —— 不问不写进使用者的仓;仓里已有 AGENTS.md 时拒写不覆盖 |

空 / 空白内容拒写。何时调同 export_patch:**用户开口要报告才调**(通常在真机验证通过后,迭代中不自动写)。检出带 bug 号时按 `<bug号>/` 双写归档。建议顺序:export_patch → memory_memorize → export_report(报告引用前两步的路径与 id,闭环才完整)。

## PR 抓取(2 个)

### fetch_patch

```python
fetch_patch(url: str) -> str
```

给 PR 链接,抓回 unified diff + 元数据(标题 / 正文 / 变更文件 / merge commit)。GitHub 走 REST(配 `GITHUB_TOKEN` 可私有仓 / 提额),Gerrit 走 `/a/` + Basic 鉴权(配 `GERRIT_USERNAME` / `GERRIT_HTTP_PASSWORD`)—— 按 URL 自动分流。网络错 / 404 / 不认识的 URL 返回可操作错误串。

### ensure_repo

```python
ensure_repo(name_or_url: str) -> str
```

把代码库解析成本地绝对路径:仓库名(查 `config.patch.git.remotes` 的自定义镜像)、git URL、或已有本地路径均可。本地没有则 clone 到 `data/repos/<名>`(幂等,已有即复用)。validate_patch 前仓不在本地,先调它。

## 开仓(1 个)

### find_repo

```python
find_repo(project: str, version: str | None = None, role: str | None = None) -> str
```

「项目+版本」→ 注册表(`data/repos.yaml`)候选仓,**候选名可直接当其他工具的 repo_path 用**
(注册名可解析)。按注册名/分支/url 模糊匹配,baseline 优先;带 `version` 时区分**版本精确命中**
(该版本已开仓,直接用)与 **Related**(同项目但版本没配上 —— 单列,不冒充命中)。没有精确命中时,
返回注册的基线清单 + 一条带安装根、bash 可原样跑的自动开仓命令(`baseline checkout … --index`:
worktree + 播种基线索引增量建,一步就绪);连基线都没有则引导:向用户要 git 地址、clone 进代码仓总目录后
`baseline add` 建基线。

**自动开仓链的第一环**:用户问「分析 bluez 5.50.61 根因」,agent 把问话解析成
`find_repo(project="bluez", version="5.50.61")`,命中即用、没命中照命令开仓,全程不问用户要路径。

## 共同契约

- **返回字符串,不抛异常**:失败(未索引 / 未装依赖 / 网络错)一律转成带建法提示的可操作串,不崩调用方。
- **诚实截断**:可能很大的返回(图 / git 系)超长时截断,但尾部明说「截掉了多少、怎么补取」(通常指一条收窄参数重调的路),不静默丢尾。
- **确定性归工具,语义归 agent**:git / 图系工具只出确定性地板(候选 / 三态 / 影响面),「真根因是谁、该不该合、哪条算引入」的裁决留给 agent 结合读码做 —— 这是全组工具的设计分工。
- 依赖速查:`search_codebase` 需向量索引;`blast_radius` / `call_chain` / `repo_map` / `repo_overview` 需结构图(`uv sync --extra code-review-graph` + index);git 系三件只需 git 仓;记忆 / 硬门 / PR 抓取无索引前置。

## 相关文档

- [配置参考](configuration.md) — server 怎么起、codebase / 模型 / 记忆后端配置
- [CLI 参考](cli.md) — `rootrecall` 命令行入口(建索引 / 记忆管理 / 起 server)
- [skill 路由矩阵](skill-routing-matrix.md) — 8 个 skill 各自怎么组合这些工具
