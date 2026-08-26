#!/usr/bin/env bash
# 项目级精确定锚(可选;2026-08-26 起全局注册已是默认姿势,本脚本退居「要按目录钉死
# 检索库 / 不想全局注入 / 无权写 ~/.config」的场景 —— 三根线原理不变,给 bug/工作仓接线):
#   门1(skill 发现): <bug仓>/.claude/skills 软链到本仓 .claude/skills
#                     (opencode 从启动目录沿 git worktree 爬,项目级 .claude/skills 会被拾取)
#   门2(路由指令): <bug仓>/AGENTS.md 软链到本仓 AGENTS.md
#                     (opencode 把 AGENTS.md 注入每个 agent 的 system prompt —— 默认界面
#                      直接提问时,agent 靠它判断该载入哪个 rootrecall skill,不用 Tab 切模式)
#   门3(MCP 锚定):   <bug仓>/opencode.json = 本仓模板 + mcp.rootrecall.cwd = 本仓根
#                     (opencode 官方 cwd 字段让 rootrecall 服务器进程在本仓根跑:
#                      uv 找得到 .venv、data/(记忆/索引)不漂到 bug 仓、.env 自加载)
#
# 用法: bash scripts/wire_opencode.sh <bug仓路径> [<bug仓路径>...] [--codebase <索引名>]
#   --codebase <名>:把该 bug 目录的默认检索库写进生成的配置(ROOTRECALL_CODEBASE env;
#                    需先 `uv run rootrecall index <源码路径> <名>` 建好索引;索引名按「项目-版本线」
#                    命名如 wpa-v25,记忆类工具按约定另传项目名,不受影响)。
#   目录不是 git 仓时自动 git init(opencode 项目发现沿 git 根;顺带 bug 材料/补丁可纳入版本管理)。
# 可重复执行(幂等);目标已有自己的 opencode.json(不含 rootrecall)→ 备份成 .bak 后跳过,不覆盖别人的配置。
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)

# --codebase 可选旗标先摘出来,剩下的都是 bug 仓路径
CODEBASE=""
RAW=()
while [ $# -gt 0 ]; do
  case "$1" in
    --codebase)
      [ $# -ge 2 ] || { echo "--codebase 需要一个参数(索引名)" >&2; exit 1; }
      CODEBASE="$2"; shift 2 ;;
    *) RAW+=("$1"); shift ;;
  esac
done

if [ ${#RAW[@]} -eq 0 ]; then
  echo "用法: bash scripts/wire_opencode.sh <bug仓路径> [<bug仓路径>...] [--codebase <索引名>]" >&2
  exit 1
fi

# 先把参数转成绝对路径(下面要 cd 回本仓根跑 uv,相对路径会跟着漂 —— 踩坑#21 同族预防)
BUGS=()
for p in "${RAW[@]}"; do
  if [ -d "$p" ]; then p=$(cd "$p" && pwd); fi
  BUGS+=("$p")
done

cd "$REPO"
TEMPLATE="$REPO/config/opencode_rootrecall.json"

for BUG in "${BUGS[@]}"; do
  echo "── 接线: $BUG"
  if [ ! -d "$BUG" ]; then
    echo "  ⚠️ 目录不存在,跳过(路径写对后重跑即可)"
    continue
  fi

  # bug 目录常不是 git 仓 —— init 一下:opencode 找项目配置/skill 沿 git 根向上爬,
  # init 后本目录即项目根(确定性强);顺带 bug 描述/日志/补丁可纳入版本管理。已有 .git 则跳过。
  if [ ! -e "$BUG/.git" ]; then
    git init -q "$BUG"
    echo "  ✅ 已 git init(opencode 项目发现沿 git 根)"
  fi

  # 门1:skills 软链
  mkdir -p "$BUG/.claude"
  ln -sfn "$REPO/.claude/skills" "$BUG/.claude/skills"
  echo "  ✅ 门1 skills 软链 -> $REPO/.claude/skills"

  # 门2:AGENTS.md 软链(默认 agent 的路由指令;单源真相,改本仓 AGENTS.md 全部 bug 仓同步生效)
  if [ -e "$BUG/AGENTS.md" ] && [ ! -L "$BUG/AGENTS.md" ]; then
    echo "  ⚠️ AGENTS.md 已存在且非软链(疑似你自己的指令文件),跳过不覆盖"
  else
    ln -sfn "$REPO/AGENTS.md" "$BUG/AGENTS.md"
    echo "  ✅ 门2 AGENTS.md 软链 -> $REPO/AGENTS.md"
  fi

  # 门3:生成 opencode.json(先两道安全检查,不动别人的配置)
  if [ -L "$BUG/opencode.json" ]; then
    echo "  ⚠️ opencode.json 已是软链(-> $(readlink "$BUG/opencode.json")),本脚本不穿透软链写文件,跳过"
    continue
  fi
  if [ -e "$BUG/opencode.json" ] && ! grep -q rootrecall "$BUG/opencode.json"; then
    cp "$BUG/opencode.json" "$BUG/opencode.json.bak"
    echo "  ⚠️ opencode.json 已存在且不含 rootrecall(疑似你自己的配置)—— 已备份为 opencode.json.bak,跳过"
    echo "     确认要覆盖:删掉 opencode.json 后重跑本脚本"
    continue
  fi
  # 注入用 uv run python 而非 jq —— jq 不在本项目 setup.sh 的依赖清单里,uv 必装(与 opencode 模板同用 --no-sync)
  uv run --no-sync python - "$TEMPLATE" "$BUG/opencode.json" "$REPO" "$CODEBASE" <<'PY'
import json, sys

template, out, repo, codebase = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
cfg = json.load(open(template, encoding="utf-8"))
srv = cfg.setdefault("mcp", {}).setdefault("rootrecall", {})
srv["cwd"] = repo
if codebase:
    # 该 bug 目录会话的默认检索库(_resolve_codebase 读 ROOTRECALL_CODEBASE):
    # 检索类工具免传 codebase;记忆类按约定传项目名覆盖它,不受影响。
    srv.setdefault("environment", {})["ROOTRECALL_CODEBASE"] = codebase
with open(out, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  echo "  ✅ 门3 opencode.json 已生成(mcp.rootrecall.cwd = $REPO${CODEBASE:+, 默认检索库 = $CODEBASE})"
  echo "  自检:cd $BUG && opencode mcp list → 应见 rootrecall ✓ connected"
done

echo ""
echo "完成。之后:cd <bug仓> && opencode —— 默认界面直接提问,agent 按 AGENTS.md 路由表自动载入对应 skill(17 个 MCP 工具 + 8 个 skill 全量可用,不用 Tab 切模式)。"
