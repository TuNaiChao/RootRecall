# 定时同步部署样例(systemd user timer / cron 二选一)

`rootrecall repo sync` 是幂等命令,定时反复跑即可。它做四件事:fetch 基线仓 →
基线检出 fast-forward 跟进 → 增量刷新检索索引(sha256 增量,只重嵌变化文件)→
`--analyze <fork名>` 时对发行版仓出上游三态判定报告
(已修/建议合/冲突,纯 git 零 LLM,落 `data/upstream_reports/<名>/`)。

两个进阶档(都只丰富报告,不自动改代码):
- `--analyze-agent`:三态报告后跑 headless opencode 复核「该不该合」并**追加进报告**
  (需 `--analyze` 同用;opencode 不在 PATH / 超时 / 失败 → 诚实注明退回纯三态)。
  前提:LLM key 已在 `.env`(CLI 进程会带给子进程)。
- `--ingest-report`:把报告摄取进记忆库(codebase=项目名),之后 recall 能带出
  「这 fix 上次评估过、为什么没合」—— 接 recall-first。

配套:`rootrecall repo gc` 定时回收过期 ephemeral 检出(`deploy/rootrecall-gc.*` 样例,
每周一 08:30;级联清 worktree+索引+结构图+记录,记忆与 baseline 不碰)。

## systemd user timer(推荐,Linux 单机无需 root)

```bash
mkdir -p ~/.config/systemd/user/
cp deploy/rootrecall-sync.service deploy/rootrecall-sync.timer ~/.config/systemd/user/
cp deploy/rootrecall-gc.service deploy/rootrecall-gc.timer ~/.config/systemd/user/
cp deploy/rootrecall-memory-consolidate.service deploy/rootrecall-memory-consolidate.timer ~/.config/systemd/user/
# 按需改 service 里的 WorkingDirectory(= RootRecall 安装根)与 sync/analyze 参数;
# service 已带 EnvironmentFile=.env(--analyze-agent 的 opencode 需要 key 在环境里)
systemctl --user daemon-reload
systemctl --user enable --now rootrecall-sync.timer rootrecall-gc.timer rootrecall-memory-consolidate.timer
systemctl --user list-timers 'rootrecall-*'     # 看下次触发
journalctl --user -u rootrecall-sync.service -f # 看同步日志
```

## cron(备选)

```bash
crontab -e
# 每天 07:30 同步 bluez-v25 基线(github deepin-community 上游),对 bluez-v20 出三态报告 + agent 复核 + 入记忆
30 7 * * * cd /path/to/RootRecall && set -a && . ./.env && set +a && /path/to/.venv/bin/rootrecall repo sync bluez-v25 --analyze bluez-v20 --analyze-agent --ingest-report >> data/sync.log 2>&1
# 每周一 08:30 清理过期 ephemeral 仓(14 天到期,先 dry-run 看清楚再真跑)
30 8 * * 1 cd /path/to/RootRecall && /path/to/.venv/bin/rootrecall repo gc >> data/sync.log 2>&1
```

注意:`sync --analyze` 只出**三态判定报告**(确定性事实);`--analyze-agent` 也只是把
复核意见写进报告 —— 「哪些真的该合进发行版」走 upstream-merge skill 的人工确认,
定时器永远不自动改代码/不自动合上游。

## 记忆巩固定时(rootrecall-memory-consolidate)

`rootrecall memory consolidate` 是记忆的「夜间整理」(五个 pass:高频教训升级
mental_model · 矛盾只标 needs_review · 语义近邻去重候选 · 补丁已合入打折 · stale
标记),幂等可反复跑。纯本地零模型调用,不需要 key;2026-09-07 提速后 5000 条 0.5s
量级,每天一次毫无负担。

`deploy/rootrecall-memory-consolidate.*` 样例每天 03:40(错开 00:00 的 sync,巩固跑在
同步之后的库上)。**给 `--repo-path`** 才会做「补丁已合入上游」检测(pass ④ 的
reverse-apply 要有 git 仓可查;不给则诚实跳过该 pass)。`--repo` 是记忆池名 = 项目名
(命名约定见 configuration.md,防版本孤岛)。同 scope 的召回也带自动巩固(命中达标
即触发),定时器是兜底的全量遍历。

两个真机踩过的坑(2026-08-20 上线实录):
- systemd user 服务只有极简 PATH,**uv 不在其中**(service 已带
  `Environment="PATH=%h/.local/bin:..."` 修复);opencode 若装在 nvm 等
  版本管理器目录里同样看不见 → `ln -sf $(command -v opencode) ~/.local/bin/opencode`
  做个稳定软链(node 升级后记得重指)。
- `--analyze-agent` 的 LLM key 走 `EnvironmentFile=.env` 注入(service 已带);
  缺 key / 缺 opencode 都不会挂同步,只会诚实降级纯三态。
