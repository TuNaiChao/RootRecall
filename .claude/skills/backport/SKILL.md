---
name: backport
description: 把一个版本(如 v25)已修的 bug 回移植到另一个独立发行版线(如 v20)——读 v25 的 fix、判断 v20 有没有同一个 bug、把修复适配到 v20 并验 apply。用户问"v25 修了这个 bug、v20 还没修、帮我改 v20"、"把这个 fix backport 到旧版"时用。
allowed-tools:
  - rootrecall_search_codebase
  - rootrecall_call_chain
  - rootrecall_blast_radius
  - rootrecall_cross_version_diff
  - rootrecall_validate_patch
  - rootrecall_export_patch
  - rootrecall_export_report
  - rootrecall_memory_recall
  - rootrecall_memory_memorize
  - rootrecall_ensure_repo
  - rootrecall_find_repo
  - read
  - edit
  - grep
  - glob
  - bash
---

# 跨版本回移植(v25 fix → v20)

你负责把一个发行版线(下称 **v25**)已修的某个 bug,回移植到另一条**独立发行版线**(下称 **v20**)。两条线是各自独立演进的仓库(共同祖先极远或无),不是同仓的旧分支。读码、判 bug、改代码都是你的活;RootRecall 工具负责取代码、查影响面、验 apply、落盘。

**两个边界**(必须守):
- **只到 apply,不编译/不复现** —— 系统软件构建环境重、信号歧义,工具只验到补丁能不能干净打上;能否编译、修没修对,用户真机自验。**apply 过 ≠ 修对**。
- **未经验证不 memorize** —— 回移植只是读码 + 适配 + apply 验(没编译没测试),不能当坐实的教训。memorize 推迟到**用户告知 backport 已编译/真机验证通过后**(可跨 session)。

**核心难点**:判"v20 有没有同一个 bug"是**语义判断**(读 v20 函数体对照 v25 的修复点推理),**没有确定性工具能下结论** —— 两条独立线之间 patch-id 等价检测失效,不能靠任何自动三态判定,只能靠你读码。这是整个 skill 最吃判断力的一步。

## 运行模式

0. **仓库就绪(别问路径)**:v25/v20 没有现成检出/索引 → `rootrecall_find_repo(project=<项目>, version=<版本>)`;没命中按其返回的**自动开仓命令**跑(bash):`baseline checkout <项目>-<版本> --from <基线> --ref <版本> --index` —— worktree+播种索引一步就绪(同 bug-rca SKILL「仓库就绪」)。连基线都没有才问用户要 git 地址。
1. **拿 v25 fix**:补丁文件用 `read` 读完整原文;源码树 commit 用 `bash git -C <v25_repo> show <sha>`;debian quilt 补丁常在 `debian/patches/<name>.patch`,用 `bash git -C <v25_repo> show <sha>:debian/patches/<name>.patch`(或直接 `read` 该文件)。
2. **理解 fix**:这个修复改了哪个函数、堵的是什么漏洞、**fix-point**(加了什么检查 / 释放了什么资源 / 拦了什么路径)。这是后面判 v20 和做适配的基准,先用你自己的话讲清楚。
3. **判 v20 有无此 bug【硬门 · 核心】** —— 用 `grep` 在 v20 仓找目标函数名(或 `search_codebase(codebase=<v20索引>)` 语义定位)→ `read` v20 里这个函数的完整函数体 → **对照 v25 fix-point 语义判断:v20 有没有同一个漏洞?** 三种结论:
   - **有同一 bug**(v20 函数体里缺 v25 加的那道检查 / 那次释放)→ 继续 step 4 适配。
   - **已修**(v20 函数里已有等价保护,只是写法不同)→ 停,输出 backport 卡标 `already_fixed`。
   - **函数不存在 / 被重写没了**(大版本跨度,v20 压根没这个函数或函数体面目全非)→ 停,标 `incompatible`,说清卡在哪。
4. **取 v20 上下文**:`call_chain`/`blast_radius`(传 v20 的 codebase)看目标函数的调用链和影响面 —— 适配时别漏 caller(改了函数行为可能波及调用方),也别漏 callee。
5. **适配 fix 到 v20**:用 `edit` 直接改 v20 仓里目标函数对应的文件。路径/签名在两版间常漂移(如 `lib/bluetooth/sdp.c` vs `lib/sdp.c`),别指望 v25 的 patch 原样打得上 —— 读懂 v20 现状,照 v25 的**修复意图**改,不是照搬行号。可用 `cross_version_diff` 或 `bash git diff` 看两版差异辅助理解,但改的是 v20 的现状代码。
6. **验 apply【硬门】**:`validate_patch(<你改出的补丁>, <v20_repo_path>)`。补丁从你对 v20 工作区的实际改动取(或 `export_patch` 落盘后读出来)。打不上 → 看日志找原因(上下文漂移?改错文件?)→ 回 step 5 改 → 再验,直到干净 apply(或判定真 `incompatible` 报告卡住点)。
7. **出 backport 卡 + 落盘**:`export_patch` 落盘适配后的补丁 + `export_report(topic=<bug 短标识>)` 落盘 backport 卡(topic 防同仓多主题报告互相覆盖)。**落完把返回的绝对路径原样报给用户**——默认落点在 RootRecall 数据目录(`data/bug_rca/`,不在用户会话目录),用户找不到文件 = 没交付;用户指定了目录就传 `out_dir`。**到这一步止,先别 memorize**。
8. **用户真机验证通过后才 memorize** —— 用户反馈 backport 已编译/真机验证通过后,再 `memorize(kind=bug_lesson, summary=<漏洞+fix-point+两版适配要点>, commit_sha=<v25 fix sha>, fix_patch=<适配后补丁>, tags=["backport","<v20代号>","<v25代号>"])`。

## 工具(按需调)

| 工具 | 何时调 | 要点 |
|---|---|---|
| `read` / `grep` / `glob` | 全程读码 | v25 读 fix、v20 找目标函数 + 读函数体。**核心**:step 3 靠 grep 定位 + read 读完整函数体 |
| `rootrecall_search_codebase(query, codebase?)` | step 3 定位 v20 函数 | 传**概念**别传文件名;`codebase=` 指向 v20 的索引(如 `bluez_v20`)。grep 更直接就 grep,两者择一 |
| `rootrecall_call_chain(symbol, codebase?)` | step 4 影响面 | 目标函数在 v20 调用链的位置;`codebase=` 指向 v20 |
| `rootrecall_blast_radius(files, codebase?)` | step 4 影响面 | 改动波及谁;`codebase=` 指向 v20 |
| `rootrecall_cross_version_diff(base, head, repo_path)` | step 5 理解两版差异(可选) | 同仓两 ref 对比;两独立仓需先 `git fetch` 一边到另一边的仓里 |
| `rootrecall_validate_patch(patch, repo_path)` | step 6 —— 硬门 | 只验 apply 不验修对;`repo_path` 传 v20 仓;改完 v20 工作树后验自洽传 `worktree=True`(封装 reverse --check) |
| `rootrecall_export_patch(repo_path, out_dir)` | step 7 落盘 | 把 v20 工作区的适配改动写成补丁;落完把绝对路径报给用户(默认在 RootRecall 数据目录,不在会话目录) |
| `rootrecall_export_report(content, repo_path, topic=<bug 短标识>)` | step 7 落盘 | 写 backport 卡 .md;topic 防同仓覆盖;路径纪律同 export_patch |
| `rootrecall_memory_recall(query, codebase?)` | step 3 前后 | 翻同类漏洞 / 历史回移植决策(先验是线索不是答案);`codebase` 传**项目名**(如 `sdp`,不带 v20/v25) |
| `rootrecall_memory_memorize(...)` | **用户验证通过后**才调 | `commit_sha` 传 v25 fix sha;`fix_patch` 传适配后补丁;`codebase` 传**项目名** —— **别传 v20 索引名**:记忆按 codebase 隔离,传版本名会把教训锁进版本孤岛(v25 侧翻不到);版本信息写进 summary/sha |
| `rootrecall_ensure_repo(name)` | 本地没这个仓 | clone |
| `bash` | 读 git(show/log/diff/fetch) | **只许** `show`/`log`/`diff`/`fetch`/`status`(读类)+ `checkout`(切 v20 干净态验 apply);改动通过 `edit` 做,不靠 `git apply` |

## 硬约束

- **"判 v20 有无 bug"是语义判断** —— 没有确定性工具下结论;两条独立线之间 patch-id 等价失效,**不能**靠任何自动判定。必须 `read` v20 函数体,对照 v25 fix-point 推理。
- **apply 过 ≠ 修对** —— `validate_patch` 只查补丁能不能干净打上,不查语义对错、不编译。能否编译、修没修对,用户真机自验。
- **v20 函数不存在 / 大跨度 → 标 `incompatible`,不硬造** —— 目标函数在 v20 已被删/重写/语义面目全非时,不要强行编一个修复,如实报告卡住点。
- **适配照 v25 的修复意图,不是照搬行号** —— 两版路径/签名/上下文常漂移;读懂 v20 现状,按 v25 的**意图**(加什么检查/释放什么)改 v20 的真实代码。
- **未经验证不 memorize** —— 回移植是读码 + 适配 + apply 验,不算坐实;memorize 推迟到用户验证通过后(可跨 session)。

## backport 卡(你的输出格式)

```
v25 fix: <sha / 补丁名>        →  v20 target: <repo @ ref>

fix-point:    <v25 改了什么函数 / 什么漏洞 / 加了什么保护>
v20 状态:     有同一 bug / already_fixed / incompatible(函数没了)
v20 位置:     <file:line>(目标函数在 v20 的位置)
适配方式:     direct(原样打)/ adapted(改路径·签名·上下文)/ incompatible(停)
apply:        clean / 3way / failed
backport:     <export_patch 落盘路径>
correctness:  apply 已验;编译 / 修对必须用户自验
建议:         <一段话——为何判 v20 有 bug、适配要点、incompatible 的话说清卡在哪>
notes:        apply 过 ≠ 修对;用户真机验证通过后我再 memorize
```

`v20 状态` = 你 step 3 读码语义判断的结论(有同一 bug / already_fixed / incompatible);`适配方式` = direct(v25 补丁原样能在 v20 apply,罕见,因两版独立漂移)/ adapted(改了路径或签名或上下文)/ incompatible(step 3 判函数没了,停);`apply` 取自 step 6 的 `validate_patch`。

## 不要

- **拿任何确定性工具判"v20 有无 bug"** —— 两条独立线 patch-id 等价失效,只能 `read` v20 函数体对照 v25 fix-point 语义判断。别指望有个三态工具替你下结论。
- **v25 补丁原样往 v20 打** —— 两版路径/签名/上下文常漂移,原样 apply 多半失败;要读懂 v20 现状,按 v25 修复**意图**适配。
- **apply 过当修对** —— `validate_patch` 只验打得上,不验语义对错、不编译;能否编译、修没修对必须用户真机自验。
- **函数没了硬造修复** —— v20 目标函数被删/重写/面目全非时如实标 `incompatible`,别编。
- **只看目标函数不看调用链** —— step 4 用 `call_chain`/`blast_radius` 把改动放进 v20 上下文,改函数行为可能波及调用方,漏了就埋新 bug。
- **未经验证的结论不裸 memorize** —— 先记必须带 `verification="apply_only"`(诚实标注),真机验证后重提 `real_machine` 升级;两步都比「憋着不记」好(教训不丢,先验不冒充)。
- **编译 / 跑测试自动验证** —— 系统软件构建环境重 + 信号歧义,这步永远归用户;工具只到 apply。
