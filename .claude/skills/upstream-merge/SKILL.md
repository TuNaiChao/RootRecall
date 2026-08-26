---
name: upstream-merge
description: 评估上游仓库的一批 commit 该不该 backport 到当前 fork——把上游拉到本地,逐个 commit 判断「fork 已修 / 建议合 / 冲突大」,再查相关性。用户问"上游这些 commit 哪些该合进来 / 哪些已经修过 / 会不会冲突"时用。
allowed-tools:
  - rootrecall_merge_eval
  - rootrecall_ensure_repo
  - rootrecall_search_codebase
  - rootrecall_call_chain
  - rootrecall_blast_radius
  - rootrecall_validate_patch
  - rootrecall_memory_recall
  - rootrecall_memory_memorize
  - read
  - grep
  - glob
  - bash
---

# 上游 commit 合入评估

你负责判断上游仓库一段范围里的 commit 该不该 backport 到用户的 fork。读码和相关性推理是你的活;`rootrecall_merge_eval` 负责逐 commit 的确定性三态判定(已修/能合/冲突),其他 `rootrecall-*` 工具取代码、查影响面、验 apply、存记忆。

**两个边界**(必须守):
- **只评估不修改** —— 你不改用户的 fork(不 `apply`/`cherry-pick`/`merge`/`reset`/写文件)。合不合、怎么合,用户自己定、自己做。
- **未经验证不 memorize** —— 评估只是确定性比对 + 读码推理(没编译没测试),不能当坐实的教训写进记忆。memorize 推迟到**用户告知某个 commit 已 backport 并验证通过后**(可跨 session),跟 patch-review 一个标准。

## 运行模式

1. **确认 fork 现状**:问清本地 fork 仓路径 + fork 对照分支(`fork_ref`,如 `release/eagle`)。没有本地仓 → `ensure_repo` clone(注意:必须是**非浅克隆**,有历史才能跑 cherry/patch-id 比对)。
2. **拉上游到本地**(用户定方向:本地分析):`bash` 跑 `git -C <repo> remote add upstream <url>`(幂等:已存在则跳过)→ `git -C <repo> fetch upstream --no-tags`。或上游单独 clone 到临时目录再对比。
3. **确认 fork_ref 在本地可解析**:`git -C <repo> rev-parse --verify <fork_ref>` 通即可。冲突检查是零 touch 的(`merge-tree --write-tree`,git ≥ 2.38,在对象库里试合并)——**不需要** checkout fork_ref、worktree 脏也不影响三态(仅老 git 回退 `apply --check` 时才要切干净态,note 会提示)。
4. **定上游范围**:确认 `upstream_base_ref..upstream_head_ref`(如「上次同步点」..`upstream/master` 最新)。范围太大 → 用 `concern_files` 收窄到 fork 关心的模块。
5. **跑三态表【硬门】** `merge_eval(upstream_base_ref, upstream_head_ref, fork_ref, repo_path, concern_files?, codebase?)` —— 拿到逐 commit 的 `already_fixed`/`recommend_merge`/`conflict`/`uncertain`。这是确定性地板(patch-id 等价 + apply 检查),**不**判断相关性。**若返回 note 说「fork 与上游无共同祖先」**(squash/独立血统的 fork,实测 deepin bluez):三态地板**整体不可用**,别重试也别对着 uncertain 硬啃 —— 直接转逐 commit 语义评估:`git show` 读每个 commit 的 diff + 对照 fork 对应代码判断「fork 有没有这 bug / 要不要这修」(backport 工作流的标准打法)。
6. **查相关性(对 recommend_merge 逐个)**:能合 ≠ fork 需要它。对每个 `recommend_merge` 的 commit:`call_chain`/`blast_radius`(触及的函数/文件)/`search_codebase` 看 fork 真有这个 bug 吗、改动触及的代码 fork 在用吗。不相关 → 标 `not_relevant`(从建议合里剔除)。
7. **出决策表**:逐 commit 决策 + 聚合计数(见输出格式)。**到这一步止**:给评估结论;**先别 memorize**。
8. **用户验证通过后才 memorize** —— 用户反馈某个 commit 已 backport 并编译/真机验证通过后,再 `memorize(kind=bug_lesson, summary=<决策+原因>, commit_sha=<上游 sha>, tags=["upstream_merge","backport"])`。未验证就 memorize = 把没坐实的判断当教训,污染后续同类检索。

## 工具(按需调)

| 工具 | 何时调 | 要点 |
|---|---|---|
| `rootrecall_merge_eval(base, head, fork_ref, repo_path, concern_files?, codebase?)` | 主扫描,每个评估都调 —— 硬门 | 逐 commit 三态;确定性地板,不判相关性 |
| `rootrecall_ensure_repo(name)` | 本地没这个仓 | clone;确认非浅克隆(有历史) |
| `rootrecall_call_chain(seed)` | 判相关性 | commit 改的符号在 fork 调用链里的位置 |
| `rootrecall_blast_radius(files)` | 判影响面 | commit 触及的文件波及谁 |
| `rootrecall_search_codebase(query)` | 理解 commit 涉及的代码 | 传概念别传文件名;只回真实存在的符号 |
| `rootrecall_validate_patch(patch, repo_path)` | 细查单个 commit 的 apply | 传该 commit 的 `git show` diff |
| `rootrecall_memory_recall(query)` | 评估前后 | 翻同类上游变更 / 历史 backport 决策 |
| `rootrecall_memory_memorize(...)` | **用户验证通过后**才调 | `commit_sha` 传上游 sha;决策作 summary |
| `bash` | 拉 upstream / 查 refs | **只许** `fetch`/`log`/`show`/`rev-parse`/`status`/`diff`(读类);**禁** `apply`/`cherry-pick`/`merge`/`reset`/`checkout`/写类 |

## 硬约束

- **只评估不修改** —— `bash` 只跑读类 git(`fetch` 拉上游除外);不 `apply`/`cherry-pick`/`merge`/`reset`/写文件。改 fork 是用户的活。
- **merge_eval 过(能 apply)≠ fork 需要它** —— 三态的 `recommend_merge` 只表示「fork 没等价、能干净打上」;**必须再查相关性**(步骤 6)才决定是否真建议合,否则把无关改动塞进 fork。
- **冲突检查零 touch(2026-08-17 起)** —— merge_eval 用 `merge-tree --write-tree` 在对象库判冲突,不依赖 checkout/worktree 状态;仅 git < 2.38 回退 `apply --check` 对当前 worktree(note 会提示,那时才需要切干净态)。
- **无共同祖先 = 三态地板不可用(2026-08-26 起)** —— squash/独立血统的 fork(无 merge-base)会被 merge_eval 前置短路并给指引:此时逐 commit 全是 uncertain 不是工具坏了,是 patch-id 与 merge-tree 都失去参照;直接走语义评估(git show + 对照 fork 代码),判定标准同 backport(「fork 有没有同一 bug」),别把「能 apply」当「该合」的依据。
- **编译 / 正确性不自动验证** —— 工具只到 apply;能否编译、修对,用户自验。
- **未经验证不 memorize** —— 评估是确定性比对 + 读码推理,不算坐实;memorize 推迟到用户验证通过后(可跨 session)。

## 决策表(你的输出格式)

```
fork: <fork_ref @ repo>          upstream range: <base..head>  (N commits)

SHA        subject                         state           relevant   decision
abc1234    fix: harden foo                 recommend_merge yes        建议合
def5678    refactor: rename bar            already_fixed   —          已修,不合
9abcdef    fix: touch b.c (fork 已改 50)   conflict        —          冲突大,人工
0123456    feat: add unrelated module      recommend_merge no         不相关,不合

聚合: 已修 1 / 建议合 1(相关) / 不相关 1 / 冲突 1 / 不确定 0
建议: <一段话——优先合哪个、冲突的处理思路、不相关的为何排除>
notes: apply 过 ≠ fork 需要它;编译 / 正确性必须由用户自验;backport 验证通过后我再 memorize
```

`state` 取自 merge_eval(`already_fixed`/`recommend_merge`/`conflict`/`uncertain`);`relevant` 是你查相关性后的判断(对 `recommend_merge` 才填,其余打 `—`);`decision` = 建议合 / 已修,不合 / 冲突,人工 / 不相关,不合 / 不确定,人工。

## 不要

- 把 `recommend_merge`(能合)当"该合" —— 能合只算 fork 没等价、打得上;是否真相关要你查(步骤 6),无关改动一律标 `not_relevant` 排除。
- 改用户的 fork(只评估;`apply`/`cherry-pick`/`merge`/写文件都不要)。
- 调 merge_eval 前 `checkout fork_ref` / 切分支 —— 冲突检查是零 touch 的(merge-tree 对象库试合并),切分支既没必要还动用户现场;fork_ref 用 `rev-parse --verify` 验可解析即可。
- 范围太大不给 `concern_files` 收窄 —— 全上游历史扫一遍既慢又淹没重点。
- **未经验证就 memorize** —— 等用户 backport 并验证通过后再记。
- 不读 commit 涉及的代码就下相关性结论 —— 用 `call_chain`/`blast_radius`/`search_codebase`/`read` 把改动放进上下文再看。
