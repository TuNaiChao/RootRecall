---
name: bug-rca
description: 在 C/系统软件仓库(Linux 内核、BlueZ、wpa_supplicant、systemd、dbus、网络栈)定位 bug 根因并修复。用户让你查 bug/崩溃/挂起/回归/CVE 的根因、问"为什么 X 会断/泄漏/死锁"、或修这类 bug 时用。你负责推理和改代码;RootRecall 工具提供记忆、代码检索、影响面、补丁 apply 校验、补丁/报告落盘(日志用你自己的 grep/awk 切)。补丁只有在干净 apply 且经人/真机验证后才算正确——在此之前持续迭代。
allowed-tools:
  - rootrecall_find_repo
  - rootrecall_memory_recall
  - rootrecall_search_codebase
  - rootrecall_blast_radius
  - rootrecall_when_introduced
  - rootrecall_validate_patch
  - rootrecall_export_patch
  - rootrecall_memory_memorize
  - rootrecall_export_report
  - read
  - grep
  - glob
  - edit
  - bash
---

# Bug 根因定位 + 修复

你负责在 C/系统软件仓库定位根因并修复。推理和改代码是你的活;`rootrecall-*` 工具提供记忆、代码情报、日志取证、影响面、apply 校验、落盘。

## 运行模式:迭代,不是走流水线

根因很少一次猜中,补丁很少一次到位。按循环做,不要按固定顺序走:

- **假设 ↔ 证伪循环**:用 `memory_recall` / `search_codebase` 取证,大日志用 grep/awk 按时间窗切(别一次读全量);每轮主动证伪当前根因;经住证伪才定论。
- **补丁 ↔ 验证循环**:`edit` → `validate_patch`(能否干净 apply)→ 没修对就再 `edit`。循环里只有改和验,**没有落盘** —— 落盘是用户触发的:等用户说「生成补丁」「拿去真机验证」了,才调 `export_patch`;中间版不自动落。
- **验证后才沉淀,或诚实标注后早记**:补丁 apply 通过即可先记 `memorize(kind=bug_lesson, ..., verification="apply_only")` —— 工具会打「未真机验证」标(recall 里显式可见)+ 置信封顶 0.5,先验不冒充结论。**用户真机验证通过后**,对同一补丁再 `memorize(..., verification="real_machine")` 一次(同补丁幂等合并,标记升级)。没标注的未验证结论直接 memorize = 把没坐实的根因冒充已验教训,禁止。`export_report` 终版同样等用户开口要报告再调。

## 仓库就绪(别问用户要路径)

用户话里是「项目 + 版本」(如 bluez 5.50.61)时,按序走,问用户是最后手段:

1. `rootrecall_find_repo(project=<项目>, version=<版本>)` 查注册表 —— **exact 命中**(版本对上)→ 直接用候选的仓/索引名开工(同版本 ephemeral 已开过就复用,别重开);**Related**(同项目、版本没对上)→ 别拿来就改,按第 2 步开 ephemeral checkout 把版本钉死。
2. 没命中 → 回复里带了基线清单和**可原样跑的自动开仓命令**(bash 跑):`baseline checkout <项目>-<版本> --from <基线> --ref <版本> --bug <bug标识> --index` —— worktree 秒开 + 播种基线索引增量建,一步就绪;登记 ephemeral,完事 `repo gc` 回收。
3. 连基线都没注册 → 这时才问用户要 git 地址,clone 进代码仓总目录(ROOTRECALL_CODEBASES,默认 ~/codebases)后 `baseline add <路径>` 建基线,回第 2 步。

**基线只读纪律**:baseline 是共享资产 —— 永远不在 baseline 工作树上直接改代码/出补丁,要改就按第 2 步开 ephemeral。发现 baseline 工作树已被人改脏(git status 非净):在回复里说明现状再继续,别顺手 reset/checkout —— 那可能是别人的工作。

**debian quilt 仓**:`.pc/` 与已应用的 quilt 补丁让树「天生脏」(正常现象;export_patch 已排除 `.pc/`)。但其余既有改动不是你写的 —— 开工前 `git status` 认一遍,别让它们混进你的补丁。

## 工具(按需调,无固定顺序)

| 工具 | 何时调 | 要点 |
|---|---|---|
| `rootrecall_find_repo(project, version?)` | 开工前——「项目+版本」还没定用哪个仓/索引 | 项目/版本从用户问话里自己解析;候选名直接当 repo_path/codebase 用(注册名可解析) |
| `rootrecall_memory_recall(query)` | ① 定位前(发散找线索)② 候选定稿前(定向复核,必调)——本仓库历史同类 bug | 先验是线索不是答案,以本次证据为准;定向复核的 query 用 problem_summary(现象一句话),别用原始日志原文;`codebase` 传项目名(如 `wpa`,不带版本号 —— 教训跨版本共享) |
| `rootrecall_search_codebase(query)` | 找入口符号——传概念,别传猜的文件名 | 只回真实存在的符号,不会编路径 |
| `rootrecall_blast_radius(files)` | 改之前——看连带波及谁 | 图驱动;图没建会提示 |
| `rootrecall_when_introduced(repo_path, symbol=\|file+line)` | 候选难分胜负时——「这段缺陷逻辑哪个 commit 带进来的」 | 纯 git 候选表(时间倒序+added/removed);引入 commit 通常是最老 added>0/removed==0 那条,中间成对的多是重构搬移;哪条真引入语义裁决(git show 逐条读);引入 commit 的 message/diff 常直接暴露根因意图——假设循环的辅助证据,不是硬门 |
| `rootrecall_validate_patch(patch, repo_path)` | 每版补丁都调 | 只验 **apply,不验修对**;改完工作树后验自洽传 `worktree=True`(封装 reverse --check,免手搓 bash 反向 apply) |
| `rootrecall_export_patch(repo_path)` | 用户开口才调——说「生成补丁」「拿去真机验证」时 | 落 `data/bug_rca/<repo>.patch`,供人/真机验证;迭代中间版不自动落盘;**落点默认在 RootRecall 数据目录,不在用户会话目录——调完把返回的绝对路径原样报给用户,用户要指定目录就传 out_dir** |
| `rootrecall_memory_memorize(...)` | apply 过即可记(带 `verification="apply_only"`);真机验证后重提 `verification="real_machine"` 升级 | kind=bug_lesson;`codebase` 传项目名(不带版本号,教训跨版本共享) |
| `rootrecall_export_report(content, repo_path, topic=<bug 短标识>)` | 验证通过、用户说「生成报告」才调 | 最终报告落 `data/bug_rca/<repo>-<topic>-rca.md`(同仓多 bug 不传 topic 会互相覆盖);同 export_patch:落完把绝对路径报给用户 |

## 硬约束

- `validate_patch` 过 ≠ 修对。它只查补丁能否 apply。系统软件通常没有单元测试,**真正的 oracle 是人/真机复现原故障**。
- 未经验证的结论不裸 `memorize` —— 要么等真机验证,要么带 `verification="apply_only"`(工具打「未真机验证」标 + 置信封顶,先验不冒充结论)。
- **大日志用 grep/awk 自己切**(无专门切片工具 —— 切片 opencode 的 read/grep/awk 就够,deer-flow/omp 均无专门工具,重造即踩坑#2):按故障时间窗(HH:MM:SS)+ **日志词汇**关键词(scan/result/p2p/timeout,**别用代码符号** 如 scan_res_handler —— 日志是散文形,子串不匹配)筛,封顶行数;别一次 read 全量(1.6 万行撑爆上下文)。read 给行范围、grep 给上下文,够用。
- **切窗是线索不是答案**:根因形态多样 —— 可能在窗口上游更早、很久以前的持久化状态/配置、别的日志源、或源码逻辑,不一定在本窗口、不一定是某条日志行。窗口只见现象(abort/ERROR)没看到因时:**逐步扩大窗口 / 换日志源 / 查源码与配置**,别锚定窗口里最响的行。
- **日志是线索,代码是确定答案**:日志切到的现象只是线索;真根因(状态机/分支逻辑/持久化状态)用 `search_codebase` 在源码里确定。代码情报比日志推断可靠 —— 重心放代码。
- **标准值断言必须带规范原文出处**:报告里凡写「官方标准/SIG Assigned Numbers/RFC 规定是 X」「与标准不符」,必须先抓到规范原文(web 搜官方文档),给 source_url 并引关键句 —— file:line 只证明「代码里确实这么写」,证明不了「标准要求什么」。抓不到原文就降级写「实测代码值 X(标准值未核)」;凭训练记忆报标准号/UUID 是幻觉高发区(实测教训 2026-08-25:bluez RCA 把 BASS 官方 UUID 0x184F 报成 0x185F,还连带误判成 fork 偏差)。
- **归因断言先对照 fork 的同步点,不是上游 HEAD**:凡写「这是 fork 改的 / 上游本来就这样 / 上游没有这段代码」,必须对照 **fork 最近一次同步上游时**的代码版本——上游当前 HEAD 已经含了后来的修复,拿 HEAD 对照会错判「上游没有」→ 把上游老债记成 fork 特有(实测 2026-08-26:`folder->msg` 重构被错判 fork 特有,实为上游 2016 年引入、fork 停在旧同步点,错误结论一度入库靠 corrects 才纠正)。找同步点:有共同祖先 → `git merge-base`;squash 血统 → fork 提交信息里的同步记录 / 逐个 `git show` 老 commit 找引入点(`when_introduced` 在上游仓跑)。

## 证伪纪律(避免误诊)

模型会锚定显眼日志行(ERROR/失败)误当根因。对抗:

- **先列候选再淘汰,别认定**:定位阶段先列 **2-3 个候选根因**(按记忆先验 + 日志线索),逐个找证据/反证,按证据强度淘汰;单候选思维 = 锚定。全部候选被淘汰才回头扩大搜索(更早窗口/别的日志源/别的子系统)。候选难分胜负时可查引入史:根因锚定到符号/file:line 后 `when_introduced` 出候选表,引入 commit 的 message/diff 常暴露缺陷意图(辅助证据路,非硬门)。
- 立根因后,**先找推翻它的证据**,找不到再定论。
- **时序检查**:现象不得早于 purported 根因。早于 = 你抓的大概率是症状(如"abort failed"其实是"扫描早完成、状态没清"的后果),回去往更早查。
- **别用残缺证据证伪先验**:日志切片可能漏了更早事件时,不能断言"X 没发生过"。
- **候选定稿前必查记忆**:根因候选收敛成 1-2 个时,**必须**用 `memory_recall(query=problem_summary)` 再召回一次本仓历史修法(定位前的召回是发散找线索,这次是定向复核——"这模式之前见过吗?当时怎么修的?")。命中的先验对照本次证据复核,与候选冲突时优先解释冲突再定论。

## 不要

- 强行一次走完固定流程——迭代。
- 把 `validate_patch` 通过当"修对"——只查 apply。
- 未验证就 `memorize`。
- 用户没要就擅自 `export_patch` / `export_report` —— 落盘是用户触发的交付,不是迭代步骤。
- `export_patch` / `export_report` 落盘后不报绝对路径 —— 默认落点在 RootRecall 数据目录(不在用户会话目录),用户找不到文件 = 没交付;用户给了目录就传 `out_dir`。
- 抓最响的日志行当根因,不查它之前的现象。
- 只立一个候选就闷头修——先列 2-3 个候选淘汰。
- 候选定稿前跳过 `memory_recall` 定向复核。
- 顺手重构——只做修这个 bug 的最小改动。
