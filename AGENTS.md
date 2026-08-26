# RootRecall 工作须知(默认 agent 的路由指令)

> **适用范围**:本表仅当用户请求**涉及代码库调研 / bug 根因 / 补丁 / 记忆**时适用。与 RootRecall 无关的常规开发任务(写业务代码、改配置、答通用编程题),忽略本表正常作答,不要调 rootrecall 工具。

本仓是 RootRecall —— 给系统软件代码库做「带记忆的 bug 根因定位 + 深度调研」的 MCP tool/skill server,向当前会话提供 17 个 `rootrecall_*` MCP 工具和 8 个工作流 skill。

## 何时走 RootRecall 工作流(路由表)

用户的请求若是对某个代码库 / bug / 补丁 / 记忆的**调研分析类需求**,先按下表对号入座,用 `skill` 工具载入对应工作流,按 SKILL.md 菜谱执行;不要绕开菜谱凭空裸答。多库机器不确定检索库注册名时,先 `bash` 跑 `uv run rootrecall baseline ls` 看(或直接传项目名,工具层会列近义候选)。

| 用户问的是… | 载入 skill |
|---|---|
| "为什么 X 会断/泄漏/死锁/崩"——查 bug 根因 + 修复 | `bug-rca`(改代码) |
| "这个补丁/PR 干啥 / 能不能打上 / 该不该合" | `patch-review`(只读) |
| "上游这些 commit 哪些该合"(同一个 git 仓) | `upstream-merge`(只评估) |
| "v25 修了、v20 还没修,帮我改 v20"(两条独立发行版线) | `backport`(改代码) |
| "v20 和 v25 在 X 流程上有什么差异"(调研不修) | `compare`(只读) |
| "这个仓库整体架构怎么组织 / 帮我上手" | `onboarding`(只读) |
| "我们对这个仓记了啥 / 记忆库质量怎么样" | `memory-health-check`(连记忆库都只读) |
| "蓝牙协议怎么设计的 / X 技术原理 / 帮我记个技术笔记"(知识不在源码里) | `domain-research`(网调,只读) |

易混判据:**upstream-merge**=fork 与上游同一 git 仓(有共同祖先,patch-id 可判)vs **backport**=两条独立线(无共同祖先,只能语义判);**compare**=只要差异报告 vs **backport**=要产出 v20 补丁;**onboarding**=讲一个仓 vs **compare**=对照两个版本;读代码能回答 → onboarding/compare,只有协议规范/技术文档里有 → domain-research;带着"现象/崩溃/回归"来 → bug-rca。组合场景(先对比再回移植等)照 `docs/skill-routing-matrix.md` §四。

## 怎么执行

- **默认:当前会话直接跑** —— `skill(name)` 载入菜谱后在本会话执行,保持追问连续(用户常连着追问)。
- **逃生舱:委派 subagent** —— 同名 `rootrecall-*` subagent(各带步数预算、权限禁令、模型配置)供用户 `@` 点名或需要硬门隔离的重活使用;两者菜谱同源,结果一致。
- **仓库就绪三步,不问路径**:用户话里是「项目+版本」(如 bluez 5.50.61)→ ① `rootrecall_find_repo(project, version)` 查注册表;② 没命中 → 按工具返回的命令 `bash` 自动开仓(`baseline checkout <名> --from <基线> --ref <版本> --index`:worktree+播种索引一步就绪,登记 ephemeral);③ 连基线都没有 → 问用户要 git 地址,clone 进代码仓总目录后 `baseline add <路径>` 建基线,回②。

## 公共纪律(所有工作流共享)

只到 apply 不编译(编译/复现/正确性一律用户真机自验);每条结论带 file:line(对比类带双源)或 source_url 溯源;recall-first(第一步 `memory_recall` 探底,命中同主题直接短路复用,不重跑);不静默截断、不报没验过的 tested/verified。

## 何时不要路由

开发 RootRecall 本身(改本仓的 Python/配置/文档/测试)是正常 coding 任务,直接做,不走上述工作流;一般编程问题也直接答。请求缺关键参数(哪个 codebase / 两个仓的路径 / 补丁在哪),先问清再路由,别硬猜。
