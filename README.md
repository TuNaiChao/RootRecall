# RootRecall

> *Light on every root cause.*

给系统软件代码库(C 为主,如 wpa_supplicant / bluez)做「带记忆的 bug 根因定位 + 深度调研」的 MCP tool/skill server。读码、改代码等重活由 opencode 承担,RootRecall 负责召回与组装精确上下文、提供工具和标准流程、沉淀并检索记忆。

## 快速开始

前置:[opencode](https://opencode.ai) 已安装并配过一次默认模型;机器上有 [uv](https://docs.astral.sh/uv/)。密钥两条路任选:chat 模型直接**复用 opencode 宿主配置**(`uv run rootrecall opencode-models` 一键采纳,url/key 运行时读宿主、不落盘),或自己配 `DEEPSEEK_API_KEY`;embedding 用 `DASHSCOPE_API_KEY`(没有也能跑最小模式,见 [configuration.md](docs/configuration.md))。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

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

bug 定位不必手动 checkout:话里带上「项目 + 版本」,agent 会查注册表,未命中自动开一次性检出(worktree + 播种索引一步就绪)。检索 / 记忆类工具都接受 `codebase` 参数,多库随建随切;常用环境变量(`ROOTRECALL_HOME` 数据迁家、`ROOTRECALL_MCP_TOOLS` 工具面裁剪等)见 [configuration.md](docs/configuration.md)。

装完**任意目录** `opencode`,直接问:

- 「为什么 wpa 的 P2P 会话会泄漏?」
- 「bluez 5.50.58 的 MediaItem 崩溃,结合日志分析根因并修复」
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

## 特色

- **记忆是主角,不是附件**:每次调研先翻记忆(recall-first),同主题第二次提问直接短路秒答;结论错了走 `corrects` 纠正链,误诊留档可审计;没过真机验证的教训自动打标、置信封顶——先验永不冒充结论。
- **全确定性工具,防幻觉内建**:17 个工具全部由 git / 结构图 / SQLite 驱动,零 LLM 参与——只回真实存在的符号、空 diff 和占位报告一律拒写、低相关召回显式警示并带语义相关度、大波及面自动换粒度指路。
- **菜谱经真机金标准实测**:8 个 skill 全部在真实 bluez / wpa 仓上用「自建 bug + 上游修复」逐点对照验证过;验证体系只用私有真机金标,不经公开基准(避开基准记忆污染)。
- **opencode 原生集成**:全局一次接线,任意目录开箱即用(路由表带适用范围守卫行,不扰无关会话);chat 模型可复用宿主 opencode 的 url/key,**密钥不落盘**。
- **为系统软件的现实约束设计**:自动化验证封顶在 apply,编译 / 复现诚实归还真机工程师;检索自动降权测试基建与生成文件,core 模块的信号不被噪声淹没。

## 文档

- 项目介绍(功能 / 特色 / 关键技术,对外讲解版):[docs/项目介绍.md](docs/项目介绍.md)
- Skill 路由矩阵(判据 + 易混对):[docs/skill-routing-matrix.md](docs/skill-routing-matrix.md)
- 三支柱模块分析:bug 定位 · 代码调研 · 记忆:[docs/](docs/)
- 参考文档:MCP 工具 [mcp-tools.md](docs/mcp-tools.md) · 配置 [configuration.md](docs/configuration.md) · CLI [cli.md](docs/cli.md) · 小白向 [mcp-guide.md](docs/mcp-guide.md)

**技术栈**:Python 3.12 · uv · mcp SDK(stdio + streamable-http)· LangChain/LangGraph · tree-sitter + CRG(结构图)+ LanceDB(向量)· native 记忆后端(SQLite + FTS5 + jieba + sqlite-vec;mem0 / cognee 可换)· 多 provider 模型工厂([config/config.yaml](config/config.yaml) 配置驱动)。

## License

MIT
