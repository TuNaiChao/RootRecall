# CLI 参考

> 入口:`uv run rootrecall <子命令>`(脚本定义见 [cli.py](../src/rootrecall/cli.py))。启动时先把 `.env` 读入环境变量,再解析 config.yaml 里的 `$VAR`。
>
> 按用途分两档(2026-08-21 CLI 瘦身):**日常档**(`baseline` / `here` / `install`)是 `--help` 里仅有的可见命令,覆盖「建基线→取版本→同步→bug 目录问话」全流程;**进阶档**(`index` / `repo` / `memory` / `mcp` / `models` / `lsp`)隐藏不删 —— `repo sync`/`repo gc` 被 systemd 定时任务调用、`mcp serve` 由 opencode 拉起、`memory`/`lsp` 供排障,命令本身照常可用,只是不再出现在 `--help`。早期自跑编排器(`bug-rca` / `research` / `patch-report` CLI)已移除(workflow 模块留仓内作参考),主线一律走 skill + MCP 工具。

## 子命令一览

| 命令 | 档 | 作用 |
|---|---|---|
| [`baseline`](#baseline) | **日常** | 基线一条龙:add 登记+建索引 / sync 同步+增量 / checkout 取版本 / ls |
| [`here`](#install--here) | **日常** | bug/工作目录轻标记(`.rootrecall.yaml` + 项目 opencode.json) |
| [`install`](#install--here) | **日常** | opencode 全局注册/卸载(任意目录免接线) |
| [`index`](#index) | 进阶 | 给仓库建索引(向量 + 结构图;`--seed` 播种增量)—— `baseline add` 的底层 |
| [`repo`](#repo) | 进阶 | 仓库注册表与生命周期:ls / register / resolve / checkout / sync / gc —— `baseline` 家族的底层 |
| [`memory`](#memory) | 进阶 | 记忆管理:recall / add / ingest / list / consolidate / invalidate / backfill |
| [`mcp serve`](#mcp-serve) | 进阶 | 启动 MCP server(17 个工具的入口) |
| [`models`](#models) | 进阶 | 列出配置的模型 + 角色路由(验证配置) |
| [`lsp`](#lsp) | 进阶 | L2 精确导航(clangd)自检 / 冒烟 |

## baseline

代码仓基线一条龙 —— 日常唯一高频入口。前提:quickstart 已建**代码仓总目录**(env `ROOTRECALL_CODEBASES`,默认 `~/codebases`),要建基线的源码仓都 git clone 进去。

```bash
# 建基线:一条命令 = 登记(role=baseline,git url/branch 自动读)+ 建索引(向量+结构图)
uv run rootrecall baseline add ~/codebases/v20/bluez       # → 基线 bluez-v20
uv run rootrecall baseline add ~/codebases/v25/bluez       # → 基线 bluez-v25
uv run rootrecall baseline add ~/codebases/upstream/bluez  # → 基线 bluez-upstream
uv run rootrecall baseline add ~/codebases/systemd         # → 基线 systemd
#   默认名 = 相对总目录的路径**倒序**连 '-'(v20/bluez → bluez-v20;直接子目录 systemd → systemd)
#   --name 覆盖;--force 全量重建;--no-graph 只建向量索引(快);--graph-only 只建结构图(零 key 可用);
#   零 key 不加开关时向量路诚实跳过、结构图照建;重跑 = upsert + 增量刷新(幂等)
#   非 git 仓拒绝(基线的 checkout/sync 都依赖 git);没 remote 也登记,但 sync 不可用(提示补 url)

# 同步:fetch→ff→增量刷索引→(可选)上游三态分析报告;缺省=全部基线(幂等,给定时器反复跑)
uv run rootrecall baseline sync [基线名...] [--analyze <发行版仓名>] [--analyze-agent] [--ingest-report] [--no-index]

# 取指定版本:worktree 秒开(共享对象库)+ 播种基线索引增量建,登记 ephemeral(不脏基线)
uv run rootrecall baseline checkout <新名> --from <基线名> --ref <tag/分支/commit> [--bug <bug号>] [--index]
uv run rootrecall baseline checkout bluez-v20-5.50.58 --from bluez-v20 --ref 5.50.58-deepin1 --bug 001 --index

uv run rootrecall baseline ls      # 全机资产一览(基线/检出/未管理)
```

| 角色 | 语义 |
|---|---|
| `baseline` | 共享基线(bluez 上游 / uos v20 线…):永久保留,`sync` 定时更新;首个 bare 镜像落 `data/mirrors/` |
| `ephemeral` | 某 bug 的一次性检出(`data/worktrees/`):到期 `repo gc` 级联回收,可点名强删 |
| `unmanaged` | `ensure_repo` 顺手 clone 的样机 / 手动 index 未声明角色的仓:gc 不碰 |

`sync --analyze` 的三态报告(已修/建议合/冲突)是纯 git 确定性事实(patch-id + merge-tree,零 LLM),
落 `data/upstream_reports/<基线名>/<时间戳>-sync.md`;「该不该真合」走 upstream-merge skill 复核。
定时部署样例(systemd user timer / cron,调的是底层 `repo sync`/`repo gc`)见 [deploy/](../deploy/README.md)。

> **自然语言 → 自动开仓**:手动 checkout 之外,在 opencode 里说「bluez **5.50.58-deepin1** 的 XX 问题」,
> `find_repo` MCP 工具按「项目+版本」查注册表;版本没有精确命中时返回基线清单 + 一条带安装根、bash 可
> 原样跑的 `baseline checkout … --index` 命令 —— agent 照跑即开仓建索引,全程不问用户要路径
> (bug-rca/backport SKILL 已接此路径)。

## index(进阶)

给代码仓建索引 —— 检索类工具(search_codebase / blast_radius / call_chain / repo_map / repo_overview)的前置;日常直接用 `baseline add`(内部就是它)。

```bash
uv run rootrecall index <repo_path> [repo_name] [--force] [--seed <基线索引名>] [--no-graph | --graph-only]
```

| 参数 | 说明 |
|---|---|
| `repo_path` | 仓库根目录 |
| `repo_name` | 索引名(默认取目录名);MCP 工具按这个名字查 |
| `--force` | 强制全量重建 |
| `--seed` | 从同线基线索引播种:拷贝向量库+manifest(+结构图)再走增量,**只重嵌差异文件**(小版本索引省 95%+ 嵌入费;目标已存在则跳过拷贝) |
| `--no-graph` | 只建向量索引不建结构图(快;图系工具将不可用) |
| `--graph-only` | 只建结构图,**不碰 embedder 与向量索引**(零 key 可用;search_codebase 将不可用)。与 `--no-graph` 互斥 |

零 key(没配 embedding key)时向量路诚实跳过、**结构图照建不再连坐**(等价自动走 `--graph-only`,rc=2 提示向量未建,指三条路:配 key 重跑增量补建 / 切本地 embedding / 只用结构图)。
结构图需要 `uv sync --extra code-review-graph`;没装会非致命降级(向量索引照建,提示装法)。

重跑语义:两条索引都增量 —— 向量按 manifest 只重嵌改动文件(重嵌前先清该文件的旧行,符号改名不留重复行;已删除的文件行也会被清掉);结构图按 `built_head` 快照只重解析改动 + 未跟踪新增的文件(社区按需重检测),无改动直接跳过。补丁打进工作区或合入后,重跑本命令刷新即可;`--force` 才全量重建(图拿不准的场合也会自动退回全量)。

`repo_path` 给哪个目录,索引就以它为根记相对路径:给仓库根就覆盖全仓,给代码子目录就只有子目录 —— **多次重建要给同一个根**,路径前缀变了等于换了一套主键。文件遍历尊重 `.gitignore`:工作区里 clone 进来当参考的外部仓只要 ignore 了,不会被扫进索引白付嵌入费。

```bash
uv run rootrecall index ~/src/wpa_supplicant wpa_supplicant
# 索引完成:向量 N chunk + 结构图 M 节点
```

## repo(进阶)

仓库注册表(`data/repos.yaml`,可用 `ROOTRECALL_HOME` 整体迁出安装根,见[配置参考](configuration.md)「数据落点」)与生命周期管理 —— 把「索引名↔仓库路径↔角色↔bug 关联」串成一条链。日常操作走 [`baseline`](#baseline) 家族(add/sync/checkout/ls 就是这里的换名转发);这里列底层全量能力:
注册表同时是 MCP 工具 `repo_path` 参数的反查源(注册表 → 索引清单 repo_path → data/repos 逐级),
`validate_patch` / `when_introduced` / `cross_version_diff` / `merge_eval` / `export_patch` 都能**直接传注册名**。

```bash
uv run rootrecall repo ls                                   # 全机资产一览(角色/路径/索引名)
uv run rootrecall repo register <名> --url <git地址> --role baseline --branch <分支>
uv run rootrecall repo register <名> --path <本地路径>       # 已有本地仓登记(upsert;--role 缺省=保留现值)
uv run rootrecall repo resolve <名或路径>                    # 反查本地绝对路径(打印命中来源)
uv run rootrecall repo rm <名>                               # 只删记录不删盘上文件

# 一次性 bug 检出(worktree 共享对象库,秒级;登记 ephemeral)
uv run rootrecall repo checkout <新名> --from <基线名> --ref <tag/分支/commit> [--bug <bug号>]
uv run rootrecall repo checkout bluez-v20-5.50.61 --from bluez-v20 --ref 5.50.61 --bug B-17 --index
#    --index:开仓顺手建索引 —— 播种基线索引后增量建(差异文件才重嵌,省 95%+ 嵌入费);
#             基线没建过索引则诚实走全量;embedder 不可用跳过建索引但不挡检出

# 基线同步(幂等,给定时器反复跑):fetch→ff→增量刷索引→(可选)上游三态分析报告
uv run rootrecall repo sync [基线名...] [--analyze <发行版仓名>] [--analyze-agent] [--ingest-report] [--no-index]
#    --analyze-agent:三态报告后 headless opencode 复核「该不该合」追加进报告(不在/失败诚实退纯三态)
#    --ingest-report:报告摄取进记忆(codebase=项目名),recall 能带出「上次评估为什么没合」

# 回收过期 ephemeral(级联:worktree+向量索引+结构图+记录;记忆不删;baseline 不碰)
uv run rootrecall repo gc [--dry-run] [--max-age-days 14] [--name <名>] [--prune-orphans]
```

| 角色 | 语义 |
|---|---|
| `baseline` | 共享基线(bluez 上游 / uos v20 线…):永久保留,`sync` 定时更新;首个 bare 镜像落 `data/mirrors/` |
| `ephemeral` | 某 bug 的一次性检出(`data/worktrees/`):到期 `gc` 级联回收,可点名强删 |
| `unmanaged` | `ensure_repo` 顺手 clone 的样机 / 手动 index 未声明角色的仓:gc 不碰 |

`sync --analyze` 的三态报告与 systemd 样例说明见 [`baseline`](#baseline) 段(同一能力,不再重复)。

## install / here(日常)

opencode 接线的两条路,取代「每个 bug 目录跑一次 wire 脚本」:

```bash
uv run rootrecall install --global            # 全机一次:skills 软链 + mcp.rootrecall + AGENTS.md 路由段
uv run rootrecall install --global --uninstall  # 卸载(只摘自己写的;别人的配置绝不动)
uv run rootrecall here [--codebase <索引名>]   # 在 bug 目录里跑:写 .rootrecall.yaml + 项目 opencode.json
```

`install --global` 后**任意目录** `opencode` 免接线直接问(skill 走 `~/.config/opencode/skills/`、
MCP 走全局 opencode.json 的 `cwd` 锚回本仓、路由表走 `~/.config/opencode/AGENTS.md` 标记段落);
`here` 在当前目录补项目级默认检索库(`ROOTRECALL_CODEBASE`),已有别人配置时备份 `.bak` 后跳过。
注意:全局 AGENTS.md 会注入本机所有 opencode 会话(路由表自带条件判据,对无关项目只多占少量
system prompt);介意就用项目级 [wire_opencode.sh](../scripts/wire_opencode.sh)。

## models(进阶)

验证配置 + 模型工厂加载,列出模型与角色路由。**配置改完先跑它**,能列出来说明 key 与反射加载都通。

```bash
uv run rootrecall models
```

## opencode-models(宿主桥接)

复用宿主 opencode 已配好的 chat 模型(url + key)—— key **不落盘**(磁盘上只有 provider/model,
运行时从 `~/.local/share/opencode/auth.json` 读)、**不显示**;embedding/reranker 不从此路(宿主
chat provider 多半没有 /embeddings 端点,向量空间也锁死 provider,embedding 保持 `.env` 显式配置)。

```bash
uv run rootrecall opencode-models                              # 探测:列出可派生模型 + key 有无(布尔)
uv run rootrecall opencode-models --adopt u/glm-5 u/kimi       # 采纳:写 config.yaml 末尾标记段(整块覆盖)
uv run rootrecall models                                       # 确认派生条目(opencode-<provider>-<model>)
```

要点:只认 `~/.config/opencode/opencode.json` 里**显式写了 `baseURL`** 且列了 `models` 的
provider(`oauth` 型凭证调不了 API,不算);采纳段是**本机私有改动**(提交时留意别把标记段带进
上游);要用派生模型就把 `model_roles` 指过去(如 `default: opencode-u-glm-5`)。派生走
OpenAI 兼容(ChatOpenAI),anthropic 原生等非兼容端点不适用。

## lsp(进阶)

L2 精确导航(clangd via multilspy)的自检与冒烟。前提:仓库根有 `compile_commands.json`。

```bash
uv run rootrecall lsp health [repo_root]                  # clangd + compile_commands 是否就位
uv run rootrecall lsp refs <file> <line> <col> [repo_root] # 打一次引用查找(1-based 行列)
```

## memory(进阶)

记忆库的命令行管理(与 MCP 的 memory_* 工具操作同一个库)。

### recall — 翻记忆

```bash
uv run rootrecall memory recall "p2p scan 泄漏" [--top-k 5] [--repo wpa_supplicant]
```

### add — 记一条(或从报告抽)

```bash
# 直接记一条
uv run rootrecall memory add --kind bug_lesson --summary "..." \
    [--root-cause "..."] [--detail "..."] [--file F --line L] \
    [--source-url URL] [--commit-sha SHA] [--repo X]

# 或从报告文件抽(走 LLM 抽取)
uv run rootrecall memory add --from-report 报告.md [--commit-sha SHA] [--repo X]
```

`--kind`:`bug_lesson` / `codebase_fact` / `domain_knowledge`。`--source-url` 配 domain_knowledge(网调知识的溯源链接)。

### ingest — 摄取文档 / 补丁 → 记忆

```bash
uv run rootrecall memory ingest <path> [--kind auto|report|patch] \
    [--source-tier imported|stated|inferred] [--commit-sha SHA] [--repo X]
```

按扩展名分流:`.md/.txt/.pdf` 走报告抽取路,`.patch/.diff` 走补丁路(按 diff 内容算 id,防重复入库)。

### list / consolidate / invalidate

```bash
uv run rootrecall memory list [--kind K] [--include-invalid] [--repo X]  # 列知识项
uv run rootrecall memory consolidate [--repo-path <git仓>] [--repo X]    # 巩固(五 pass:升级/矛盾/去重/已合入/过期)
uv run rootrecall memory invalidate <id> [--reason "..."] [--repo X]     # 失效一条(软删,留档可审计)
```

`consolidate` 给 `--repo-path` 才做「补丁已合入上游」检测(要跑 git 对账)。

### backfill — 补嵌零 key 期间写入的记忆

```bash
uv run rootrecall memory backfill [--repo X] [--dry-run]
```

零 key 期间 `memorize` 写入的条目无向量(只走 BM25 检索);配好 embedding key 后跑一次
backfill 即补嵌:**只更新向量列**(不触发置信度累加 / 合并),按 embedder 的 batch 分批嵌、
整批一提交,幂等可重跑(重复跑零变更)。`--dry-run` 只列待补条目。embedder 不可用时诚实
报错指路(见「最小模式」)。

## mcp serve(进阶)

启动 MCP server —— 17 个工具的入口,详见 [MCP 工具参考](mcp-tools.md)。

```bash
uv run rootrecall mcp serve [--codebase X] [--transport stdio|http] [--host H] [--port P]
```

| 参数 | 说明 |
|---|---|
| `--codebase` | 默认查哪个仓的索引 / 记忆(默认 `config.code_index.repo`;多仓靠工具的 per-call `codebase` 参数切) |
| `--transport` | `stdio`(默认,推荐)| `http`(warm 长进程) |
| `--host` / `--port` | http 模式绑定(默认 `127.0.0.1:8765`) |

> 早期自跑编排器 `bug-rca` / `research` / `patch-report` CLI 已移除(2026-08-21 CLI 瘦身);workflow
> 模块仍在仓内(`src/rootrecall/workflows/`)可 import 参考,主线一律走 skill + MCP 工具。

## 相关文档

- [配置参考](configuration.md) — 模型 / 记忆 / MCP 各段配置
- [MCP 工具参考](mcp-tools.md) — server 起来后有哪 17 个工具
- [README](../README.md) — quickstart 一键配置 + opencode 接入
