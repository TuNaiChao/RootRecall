# RootRecall

> *Light on every root cause.*

[![CI](https://github.com/TuNaiChao/RootRecall/actions/workflows/ci.yml/badge.svg)](https://github.com/TuNaiChao/RootRecall/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/) [![uv](https://img.shields.io/badge/uv-managed-orange)](https://docs.astral.sh/uv/)

给系统软件代码库(C 为主,如 wpa_supplicant / bluez)做「带记忆的 bug 根因定位 + 深度调研」的 MCP tool/skill server。读码、改代码等重活由 opencode 承担,RootRecall 负责召回与组装精确上下文、提供工具和标准流程、沉淀并检索记忆。

## 快速开始

前置:[opencode](https://opencode.ai) 已安装并配过一次默认模型;机器上有 [uv](https://docs.astral.sh/uv/)。密钥两条路任选:chat 模型直接**复用 opencode 宿主配置**(`uv run rootrecall opencode-models` 一键采纳,url/key 运行时读宿主、不落盘),或自己配 `DEEPSEEK_API_KEY`;embedding 用 `DASHSCOPE_API_KEY`(没有也能跑最小模式:图系工具经 `index --graph-only` 可用、其余降级见 [configuration.md](docs/configuration.md));抓 GitHub PR 的 `fetch_patch` 匿名可用但限速,要稳定可配 `GITHUB_TOKEN`(见 `.env.example`)。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # 安装uv. 装完新开一个终端,uv 才在 PATH 里

git clone https://github.com/TuNaiChao/RootRecall.git && cd RootRecall
bash scripts/quickstart.sh
```

quickstart 一条链跑完:依赖 → `.env` 密钥(只做非空检查、永不回显)→ 模型验证 → 代码仓总目录 → **全局注册四件套**(skills 软链 / MCP / agent 块 / AGENTS.md 路由段,默认装不问)→ 接线自检。

### 接入你的代码库

```bash
uv run rootrecall baseline add ~/codebases/v25/bluez   # 登记 + 建索引 → bluez-v25(名字自动取)
uv run rootrecall baseline ls                          # 看全部基线 / 检出
uv run rootrecall baseline sync                        # 增量同步(fetch→ff→刷索引;deploy/ 有 systemd 样例)
```

bug 定位不必手动 checkout:话里带上「项目 + 版本」,agent 会查注册表,未命中自动开一次性检出(worktree + 播种索引一步就绪)。检索 / 记忆类工具都接受 `codebase` 参数,多库随建随切;常用环境变量(`ROOTRECALL_HOME` 数据迁家、`ROOTRECALL_MCP_TOOLS` 工具面裁剪等)见 [configuration.md](docs/configuration.md)。升级与卸载:`git pull` 后重跑 quickstart 即可(幂等,顺带刷新全局注册);不想要了 `uv run rootrecall install --global --uninstall` 整套摘除(只动 RootRecall 自己写的东西)。

装完在**任意目录** 启动`opencode`,直接问:

- 「为什么 wpa 的 P2P 会话会泄漏?」
- 「bluez v25 的 MediaItem 崩溃,结合日志分析根因并修复」
- 「这个仓库整体架构怎么组织?新人怎么上手?」

## 8 个工作流 Skill

不用人工选:opencode 里直接提问,agent 按 [AGENTS.md](AGENTS.md) 路由表自动载入对应 skill,按菜谱执行(易混场景判据见 [skill-routing-matrix.md](docs/skill-routing-matrix.md))。

| 你问的是… | skill | 产出 |
|---|---|---|
| 「为什么 X 会断 / 泄漏 / 死锁 / 崩」——查 bug 根因并修 | `bug-rca` | 根因 + 补丁(已验 apply)+ 分析报告 + 教训沉淀 |
| 「这个补丁 / PR 干啥、能不能打上、该不该合」 | `patch-review` | 补丁鉴定:做了什么、能否 apply、影响面、合入建议 |
| 「上游这些 commit 哪些该合 / 哪些已修过」 | `upstream-merge` | 逐 commit 三态(已修 / 建议合 / 冲突)+ 相关性 + 决策表 |
| 「v25 修了、v20 还没修,帮我改 v20」 | `backport` | 语义判断 v20 是否同病 → 适配出 v20 补丁(已验 apply) |
| 「v20 和 v25 在 X 流程上有什么差异」 | `compare` | 锚定两版入口逐节点对照,流程级差异报告 |
| 「这个仓整体架构怎么组织 / 帮我上手」 | `onboarding` | 结构图俯瞰 + 一条真实用户旅程端到端走读,导览报告 |
| 「蓝牙协议怎么设计的 / 帮我记个技术笔记」 | `domain-research` | 多权威源交叉印证,领域知识入记忆(下次 recall 秒答) |
| 「我们记了些啥 / 记忆库质量怎么样」 | `memory-health-check` | 全量记忆逐条审计:溯源 / 置信 / 时效 / 矛盾,健康信号 |

## 17 个 MCP 工具

**记忆(3)**

| 工具 | 作用 |
|---|---|
| `rootrecall_memory_recall` | 检索长期记忆,带 file:line 溯源;自动并查共享领域知识池,命中语义相关度低于阈值时显式劝退(防拿不相关记忆硬答) |
| `rootrecall_memory_memorize` | 写入记忆;`corrects` 声明"纠正了哪条旧结论",`verification` 声明验证档(apply 过即记但标 unverified,真机验证后升级) |
| `rootrecall_memory_dump` | 全量记忆摊开成溯源卡(含召回次数 / Web 出处),供体检审计;空作用域自动列出记忆都在哪 |

**代码情报(8)**

| 工具 | 作用 |
|---|---|
| `rootrecall_search_codebase` | 语义 + 符号检索,**只返回索引中真实存在的符号**(防幻觉) |
| `rootrecall_blast_radius` | 改动影响面(结构图 BFS);波及面过大时自动指路改用符号级调用链 |
| `rootrecall_call_chain` | 调用链:谁调它 / 它调谁(N 跳 + PageRank 排序) |
| `rootrecall_repo_map` | 全仓符号地图,按重要性打包进 token 预算(Aider 式) |
| `rootrecall_repo_overview` | 架构俯瞰:模块社区 / 枢纽 / 桥节点 / 耦合告警(自动过滤测试与生成文件) |
| `rootrecall_cross_version_diff` | 同一仓两个 git ref 之间的差异 |
| `rootrecall_when_introduced` | 某符号 / 某行由哪个 commit 引入(pickaxe + 行历史) |
| `rootrecall_merge_eval` | 上游 commit 合入三态(patch-id 已修 / merge-tree 冲突检测,零 touch);无共同祖先的独立血统直接短路指路 |

**交付硬门(3)**

| 工具 | 作用 |
|---|---|
| `rootrecall_validate_patch` | 补丁能否干净 apply;`worktree=True` 验证已改工作树的自洽性 |
| `rootrecall_export_patch` | 补丁落盘成 `.patch`(空 diff 拒写;quilt `.pc/` 产物自动排除;按 bug 号归档) |
| `rootrecall_export_report` | 报告落盘 `.md`(`topic` 防同仓多主题覆盖;**空 / 占位报告拒写**) |

**PR 抓取与开仓(3)**

| 工具 | 作用 |
|---|---|
| `rootrecall_fetch_patch` | 抓 GitHub PR 的 diff + 元数据 |
| `rootrecall_ensure_repo` | 仓库名 / URL → 本地路径,缺则自动 clone |
| `rootrecall_find_repo` | 「项目 + 版本」→ 注册表候选仓;未开仓则返回一步就绪的自动开仓命令 |

## 记忆系统

一套**带溯源、可纠正、按作用域隔离、持续巩固**的工程知识库——同一个问题第二次出现时 recall 秒答;错误的旧结论走显式纠正,而非悄悄覆盖。

**四类记忆**(共用一库,按知识的来源与生命周期划分):

| kind | 是什么 | 怎么来 | 特殊规则 |
|---|---|---|---|
| `codebase_fact` | 代码事实:模块 / 架构的关键设计 | 调研产出 | 读码即坐实,读完即记(不需验证) |
| `bug_lesson` | bug 教训:根因 / 修法 / 影响面 | 修 bug 产出 | **验证分档**:apply 过即记,但自动标 `unverified`、置信封顶;真机验证后同一补丁重提一次即升级 |
| `mental_model` | 反复出现的教训固化成的稳定规则 | **不直接写**:高召回的 bug_lesson 由巩固 pass 自动升级 | 类比程序性记忆 |
| `domain_knowledge` | 领域常理:协议语义 / 技术原理 | 多权威源网调交叉印证 / 用户笔记 | 溯源锚 source_url;强制入 general 共享池 |

**每条记忆携带可追责的元数据**:证据 `file:line`(或 URL)+ `commit_sha` + 六档来源可信度 + 置信度(重提按来源档贝叶斯累加)+ bi-temporal 双时间戳(过期结论**失效不删除**,历史可审计)+ 纠正链(`corrects` 声明"纠正了哪条"——被纠正条检索降权但留档,误诊可查)。

**生命周期**:

```
memorize   稳定 id = hash(scope+kind+结论):同主题重记 → 合并累加,不堆重复
   ↓
存储       SQLite 单库(FTS5 + jieba 分词 + sqlite-vec 向量),按 (owner, codebase, repo) 三重隔离 + general 共享池
   ↓
recall     四路召回(BM25 / 向量 / 代码 chunk / 结构图)→ RRF 融合 → exp 时间衰减 × 置信 × 纠正惩罚
           头牌语义相关度 < 0.4 时显式劝退(防拿不相关记忆硬答);命中自动累计召回次数
   ↓
consolidate 五个幂等 pass:高频教训升级 mental_model · 矛盾只标不裁(needs_review)·
           语义近邻去重候选 · 补丁已在上游打标打折 · 长期没人翻标 stale
```

读写两侧的行为假设均经真实会话压测(裂池归一、纠正链闭环、低相关拒答、占位拒写);深入设计见[文档](#文档)节。

## 特色

- **记忆是主角,不是附件**:recall-first 秒答、`corrects` 纠正链、先验永不冒充结论——完整设计见上节「记忆系统」。
- **全确定性工具,防幻觉内建**:17 个工具全部由 git / 结构图 / SQLite 驱动,零 LLM 参与——只回真实存在的符号、空 diff 和占位报告一律拒写、低相关召回显式警示并带语义相关度、大波及面自动换粒度指路。
- **菜谱经真机金标准实测**:8 个 skill 全部在真实 bluez / wpa 仓上用「自建 bug + 上游修复」逐点对照验证过;验证体系只用私有真机金标,不经公开基准(避开基准记忆污染)。
- **opencode 原生集成**:全局一次接线,任意目录开箱即用(路由表带适用范围守卫行,不扰无关会话);chat 模型可复用宿主 opencode 的 url/key,**密钥不落盘**。
- **为系统软件的现实约束设计**:自动化验证封顶在 apply,编译 / 复现诚实归还真机工程师;检索自动降权测试基建与生成文件,core 模块的信号不被噪声淹没。

## 文档

- 项目介绍(功能 / 特色 / 关键技术,对外讲解版):[docs/项目介绍.md](docs/项目介绍.md)
- Skill 路由矩阵(判据 + 易混对):[docs/skill-routing-matrix.md](docs/skill-routing-matrix.md)
- 三支柱模块分析:bug 定位 [bug-rca-module-analysis.md](docs/bug-rca-module-analysis.md) · 代码调研 [code-research-module-analysis.md](docs/code-research-module-analysis.md) · 记忆 [memory-module-analysis.md](docs/memory-module-analysis.md)
- 参考文档:MCP 工具 [mcp-tools.md](docs/mcp-tools.md) · 配置 [configuration.md](docs/configuration.md) · CLI [cli.md](docs/cli.md) · 小白向 [mcp-guide.md](docs/mcp-guide.md)

**技术栈**:Python 3.12 · uv · mcp SDK(stdio + streamable-http)· LangChain/LangGraph · tree-sitter + CRG(结构图)+ LanceDB(向量)· native 记忆后端(SQLite + FTS5 + jieba + sqlite-vec;mem0 / cognee 可换)· 多 provider 模型工厂([config/config.yaml](config/config.yaml) 配置驱动)。

## License

MIT
