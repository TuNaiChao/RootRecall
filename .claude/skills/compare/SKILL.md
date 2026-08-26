---
name: compare
description: 对比两个版本/两个仓库的某个流程或模块有什么差异——锚定两版各自的流程入口函数,逐节点读函数体语义对照,聚成流程级差异报告。用户问"v20、v25 蓝牙在连接流程上有什么差异"、"这两个版本的 X 功能实现有什么不同"、"新版本改了哪条流程"时用。
allowed-tools:
  - rootrecall_find_repo
  - rootrecall_search_codebase
  - rootrecall_repo_map
  - rootrecall_call_chain
  - rootrecall_memory_recall
  - rootrecall_memory_memorize
  - rootrecall_export_report
  - rootrecall_ensure_repo
  - read
  - grep
  - glob
---

# 跨版本代码对比调研

你负责对比**两个版本**(或两个仓库)在某个流程/模块上的实现差异。比如 v25 和 v20 两条独立发行版线,用户问「蓝牙连接流程有什么差异」。读码、配对函数、对照差异都是你的活;RootRecall 工具负责取代码、查调用链、落盘、记忆。

**两个边界**(必须守):
- **只调研不改代码** —— 你不改任何仓库(不 edit / 不 git apply / 不写源码),只读码出对比报告。和 upstream-merge/patch-review 一样是 read-only。
- **对比事实读码即记,不需等用户验证** —— 对比调研是纯读码事实(读函数体对照两版),不依赖编译/真机验证,**读完即可 memorize**。这跟 backport/bug-rca/patch-review 不一样(那三个涉及「bug/补丁是否真修对」,要等用户真机验证才能记)。对比结论本身就是读码坐实,记下来让下次同类问题直接 recall 命中秒答。

**核心难点**:把两版的函数配对起来是**语义判断** —— v20 的 `foo` 和 v25 的 `bar` 可能职责相同但改名了,也可能一个函数在 v25 被拆成两个、或合并成一个。**没有确定性工具能自动配对**(各 codebase 结构图独立,无跨版本联合图)。这步靠你 `read` 函数体推理 —— 同名直接配;名字不同就读实现看是否同职责。这是整个 skill 最吃判断力的一步。

## 运行模式

> **先 recall,命中就短路**。这个 skill 的价值一半在「记忆让下次秒答」—— 所以**第一步永远是 `memory_recall`**(`codebase` 传项目名查一次这个流程主题的对比 —— 记忆按项目记、跨版本共享)。若召回的历史对比事实**已覆盖用户问的主题 + file:line 双源齐全**,**直接复用它出对比卡(下面步骤 5/6),不要重跑 search/read**;只有没命中、或命中但主题对不上/缺关键节点时,才走完整的 A→B→C 调研。判据见 step 2 的「短路 vs 重跑」。

1. **确认两版 + 流程主题 + recall 探底**:问清两个 codebase 各代表哪版(如 `bluez` = v25 新版、`bluez_v20` = v20 旧版)+ 用户关心的**流程主题**(「连接流程」/「配对流程」/「SDP 服务发现」/「GATT 发现」...)。**不知道本地有哪些仓/注册名叫什么 → `rootrecall_find_repo(project=<项目>)` 一次拿全(候选名直接可用),别 bash 查注册表绕**(实测绕 3 次才找到)。本地没仓 → `ensure_repo`(只读 clone)。**第一步立刻 `memory_recall(query=<流程主题>, codebase=<项目名>)` 查一次**,看有没有历史对比事实(记忆按项目名记、跨版本共享;**别**用两版索引名各查 —— 索引名是检索类工具用的,记忆 scope 里没有,会白查)。**先把主题词想成一个代码概念**(「连接流程」→ connection establishment / connect / pair / link),检索用概念不用文件名。

   > **⚠ 仓库路径**:工具(`search_codebase`/`repo_map`/`call_chain`)返回的 file:line 是索引时的**相对路径**(带 repo_root 前缀,如 `code-test/v25/bluez/src/...`)。你要 `read` 函数体时,若相对路径在你的 cwd 下打不开(常见:仓库目录被 gitignore → `glob` 看不见;或 cwd 不是项目根),按序试:**① 索引名直接当 repo_path 用** —— `repo_path` 参数现已吃注册名(注册表/索引清单自动反查,见 `rootrecall repo ls`);② `ensure_repo(<索引名>)` 拿绝对路径;③ 都不行才问用户。**别浪费步数满盘 glob/find**(本 skill 无 bash 权限,find 也用不了)。拿到绝对路径前缀后,把索引返回的相对路径拼成绝对路径再 `read`。
2. **短路 vs 重跑(关键分流)**:看 step 1 的 recall 结果 ——
   - **短路(直接出报告)**:recall 命中了**同一对版本 + 同一流程主题**的对比事实,内容已包含入口配对 + 流程节点差异 + 因果结论 + 双源 file:line。→ **复用它,跳到 step 5 出对比卡 + step 6 export_report,不重跑 search/read**。用户要的「秒答」就是这条路径。最多按用户的具体问法补一两句,别整轮重读。
   - **重跑(走完整 A→B→C)**:recall 没命中、或命中的主题对不上(问的是连接,记忆里只有配对)、或缺关键节点(只覆盖了一半流程)。→ 走下面 step 3-5 的完整调研。**这才是该花 read 预算的时候**。
3. **锚定流程入口【核心·阶段 A】**(仅重跑路径):对**两版各跑一次** `search_codebase(query=<流程概念>, codebase=<各>)` + `repo_map(codebase=<各>, compact=true)`(compact 只出地图树,大仓省一半输出)。拿到**两版各自的入口函数群 + file:line**(工具只回索引内真实符号,防幻觉)。流程跨多个函数 → 用 `call_chain(symbol=<入口函数>, codebase=<各>)` 从入口多跳展开,看清整条流程涉及的函数链。
4. **逐节点对照【阶段 B】**(仅重跑路径):① 把两版入口函数群**配对** —— 同名直接配;名字不同就 `read` 函数体判**是否同职责**(v20 的 `foo` ↔ v25 是否拆成了 `foo`+`bar`?)。配不上的标「v20 无 / v25 新增」。**函数配对是语义判断,无确定性工具**(各 codebase 结构图独立无联合图)。② 对配上的每对函数,`read` 两版完整函数体,讲清差异 —— 逻辑分叉 / 参数变化 / 新增校验 / 删除的步骤 / 重构。流程每个关键节点(入口、状态转换、资源释放...)都对照一遍。
5. **聚流程级结论【阶段 C·短路路径也走这】**:把(重跑得出的、或 recall 命中直接复用的)节点差异聚成**流程级差异** —— 入口差异 / 状态机差异 / 新增环节 / 删除环节 / 重命名映射。给出因果解读:为什么 v25 多了某个环节(如新协议层)/ 为什么改名(职责拆分)。不要只罗列文件差异,要讲清流程层面变了什么。
6. **落对比报告**:`export_report(topic=<主题 slug,如 connect-flow-compare>)` 落盘对比报告 .md(**必传 topic**,同仓多主题报告不传会互相覆盖)。**每条结论必须附双源 file:line**(v25 的 + v20 的),对齐 cited-reporter 防幻觉。**用户显式要求 AGENTS.md 时**才在同一调用传 `agents_md=True`(蒸馏 ≤60 行 agent 版写进目标仓根;默认不传——不问自写用户仓 = 越界;两版对比时只写用户指定的那个目标仓,不两个都写)。
7. **memorize(仅重跑路径才记)**:重跑得出的新结论才 `memorize(kind=codebase_fact, kind_detail=architecture, summary=<两版流程差异 + 因果>, evidence=[<双源 file:line + 代码片段>], codebase=<项目名,如 bluez> —— 别带版本(对比事实跨版本复用,版本已体现在双源 evidence 里), confidence=<你的把握>)`。**短路路径不要 memorize**(recall 已命中的事实 DB 里有了,重复记浪费调用,且按 summary 算 id 会去重——不污染但白花一步)。这条事实读码即坐实,**不需等用户验证**。

## 工具(按需调)

| 工具 | 何时调 | 要点 |
|---|---|---|
| `rootrecall_find_repo(project)` | step 1 不知道本地有哪些仓/注册名 | 一次拿全候选(名字直接可用);别 bash 查注册表绕 |
| `rootrecall_search_codebase(query, codebase?)` | step 2 锚定入口 | 传**概念**别传文件名(如"蓝牙连接建立流程");两 codebase 各跑一次;只回真实符号 |
| `rootrecall_repo_map(codebase?)` | step 2 俯瞰两版骨架 | Aider repomap 式 PageRank 符号地图,找流程入口模块;两 codebase 各跑一次 |
| `rootrecall_call_chain(symbol, codebase?)` | step 2 流程展开 | 从入口种子多跳展开,看流程涉及的函数链;两 codebase 各跑 |
| `read` / `grep` / `glob` | step 4 读两版函数体(仅重跑路径) | **核心**:step 4 配对判同职责 + 逐节点对照全靠 read 两版函数体。**短路路径不用** |
| `rootrecall_memory_recall(query, codebase?)` | **step 1 第一步**(codebase=项目名,查一次) | 命中同主题对比事实 → **短路直接出报告**(step 5/6),不重跑;这才是「秒答」。没命中才走完整调研 |
| `rootrecall_memory_memorize(...)` | step 7(仅重跑路径才记) | kind=codebase_fact,kind_detail=architecture,带双源 evidence;`codebase` 传项目名(如 `bluez`,不带版本);**不需用户验证**。**短路路径不 memorize**(DB 已有) |
| `rootrecall_export_report(content, repo_path, topic=<主题slug>)` | step 6 落盘 | 写对比报告 .md;topic 必传防同仓多主题覆盖;**落点默认在 RootRecall 数据目录(不在用户会话目录)——落完把返回的绝对路径原样报给用户,用户要指定目录传 out_dir** |
| `rootrecall_ensure_repo(name)` | 本地没仓 | 只读 clone |

## 硬约束

- **只调研不改代码** —— 不 edit / 不 git apply / 不写源码;read-only 调研(和 upstream-merge/patch-review 一个标准)。
- **search 命中全是外围就转 grep** —— 大仓(含 emulator/test 基建)对「连接流程」这类泛概念,search_codebase 会回外围符号(实测 bluez:top-6 全是 emulator/btdev.c、android/gatt.c,核心入口 device_connect_le 挤不进);此时果断改 grep 已知命名模式(`btd_`、`_connect$`、`device_`)锚定 src/ 核心文件再 read,别在搜索词上死磕。搜索留着探索未知模块用。
- **两版函数配对是语义判断** —— 没有确定性工具能自动配对;各 codebase 结构图独立无跨版本联合图,`cross_version_diff` 也只支持同仓两 ref(两个独立仓无效)。必须 `read` 函数体判同职责。
- **不用 cross_version_diff** —— 它是「同一个 git 仓的两个 ref」对比,v20/v25 这种两独立仓无效;两版差异靠各 codebase 检索 + read 对照。
- **结论必须附双源 file:line** —— 每条差异结论都要标 v25 的 + v20 的 file:line,防幻觉,对齐 cited-reporter。双源 file:line 锚「两版各自怎么写」;凡断言「与官方标准/SIG/RFC 不符」,必须另附规范原文 source_url(抓不到就写「标准值未核」,不凭训练记忆报标准值 —— 幻觉高发区)。
- **「哪边改的」要三源归因,且对照的是 fork 的同步点** —— v20/v25 差异要说「fork 改的还是上游演进的」,有 upstream 基线必须对照第三源(upstream 同位置)再归因;没对照过就只报差异本身 + 双源 file:line,别猜方向。**对照的 upstream 必须是 fork 的上游同步点版本,不是上游当前 HEAD** —— HEAD 已含后来的修复,拿它对照会错判「上游没有」→ 把上游老债记成 fork 特有(实测 2026-08-26:`folder->msg` 重构被错判 fork 特有,实为上游 2016 年引入)。找同步点:有共同祖先 → `git merge-base`;squash 血统 → 同步记录 / 上游仓 `when_introduced` 查引入 commit。
- **对比事实读码即记** —— 不像 bug/补丁要等真机验证;对比结论读码坐实,step 7 可直接 memorize(仅重跑路径),下次秒答。
- **recall 命中就短路,不重跑** —— step 1 recall 命中同主题对比事实时,直接复用出报告,**不要为了「走完流程」又 search/read 一遍**。这是本 skill 的核心价值(下次秒答);重跑只在没命中/主题对不上时才做。短路路径不 memorize(DB 已有,重复记白花一步)。

## 对比卡(你的输出格式)

```
对比: <流程主题>     v25: <bluez @ ref>   ↔   v20: <bluez_v20 @ ref>

入口函数:
  v25 <func@file:line>  ↔  v20 <func@file:line>   状态: 同名 / 重命名 / 仅一版有

流程节点差异:
  节点          v25                              v20
  连接入口      <func:line>                      <func:line>
  ATT 建立      <func:line>(v25 新拆分)          —(v20 尚无独立 ATT 层)
  ...           ...                              ...

结论: <一段话——流程级差异 + 因果解读(为何 v25 多了某环节 / 为何重命名)>
sources: 每条结论附双源 file:line
report:  <export_report 落盘路径>
memorize: 已记 kind=codebase_fact(读码即记,下次同类问题 recall 秒答)
```

`状态` = 你 step 3 语义配对的结论(同名 / 重命名 / 仅一版有);`流程节点差异` 每行取自 step 4 读两版函数体的对照;`结论` 是 step 5 的流程级因果解读。

## 不要

- **改任何仓库的代码** —— 只读调研,不 edit / 不 git apply / 不写源码。
- **拿 cross_version_diff 比两个独立仓** —— 它只支持同一个 git 仓的两个 ref,两个独立仓(v20/v25)无效;两版差异靠各 codebase 检索 + read 对照。
- **指望有确定性工具自动配对两版函数** —— 没有;各 codebase 结构图独立无联合图。必须 `read` 函数体判同职责。
- **只罗列文件差异不讲流程级结论** —— step 5 要把节点差异聚成流程变了什么 + 为什么,不是 git diff 堆栈。
- **结论不带双源 file:line** —— 每条差异都要标两版的 file:line,防幻觉。
- **等用户验证才 memorize** —— 对比是读码事实,读完即记(区别于 bug/补丁型 skill);不记就丢了「下次秒答」。
- **recall 命中还重跑一遍** —— step 1 recall 命中同主题对比事实后,直接复用出报告(step 5/6);别为了「走完 7 步」又 search×4 + read×20 全跑一遍。那是冷路径才该做的事,热路径重跑 = 浪费步数、「秒答」价值落空。
- **短路路径重复 memorize** —— recall 已命中的事实 DB 里有,短路时再 memorize 是浪费调用(虽按 summary 算 id 会去重不污染,但白花步数)。只有重跑得出**新结论**才 memorize。
