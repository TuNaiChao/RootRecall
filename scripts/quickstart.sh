#!/usr/bin/env bash
# RootRecall 一键配置:装依赖 → 交互填 .env 密钥 → 验证模型 → (可选)建索引 → (可选)接 bug 仓 → opencode 接线自检。
# 跑完在本仓库根目录(或已接线的 bug/工作仓)启动 opencode,即可用全部功能(8 个 skill + 17 个 MCP 工具)。
#
# 用法:bash scripts/quickstart.sh [--force]
#   --force  重跑完整 scripts/setup.sh(重装系统工具 + 依赖;默认 .venv 已存在时跳过该步)
#
# 设计:每步可重复执行 —— 已配置的自动跳过;密钥输入不回显、不打印;任何一步失败立即停。
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

FORCE=0
if [ "${1:-}" = "--force" ]; then FORCE=1; fi

# ── [1/7] 系统工具 + Python 依赖 + 记忆软链 ─────────────────────────────
# fresh clone 没有 .venv → 跑完整 setup.sh;本机已装过 → 跳过(重装用 --force)。
if [ "$FORCE" -eq 1 ] || [ ! -d .venv ]; then
  bash scripts/setup.sh
else
  echo "[1/7] 依赖已就绪(.venv 存在),跳过安装;重装: bash scripts/quickstart.sh --force"
fi

# ── [2/7] .env 密钥(必填 2 个;已配的保留现值,只问缺的)────────────────
[ -f .env ] || cp .env.example .env

# 读 .env 里某个 key 的值(仅内部用,不回显)
env_get() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

# 写 .env:先删该 key 旧行再追加(避开 sed 对值里特殊字符的转义坑);不打印值
env_set() {
  grep -v "^$1=" .env > .env.tmp || true
  mv .env.tmp .env
  printf '%s=%s\n' "$1" "$2" >> .env
}

# 问一个 key:已有非空值 → 跳过;输入非空 → 写入;留空且确认 → 跳过
ask_key() {
  local key="$1" hint="$2" cur val ans
  cur=$(env_get "$key")
  if [ -n "$cur" ]; then
    echo "  $key = 已配置(保留现值)"
    return 0
  fi
  while true; do
    echo "  $key —— $hint"
    printf '  粘贴后回车(输入不回显):'
    read -rs val || val=""
    echo
    if [ -n "$val" ]; then
      env_set "$key" "$val"
      echo "  ✅ 已写入 .env"
      return 0
    fi
    printf '  留空跳过(稍后可手动编辑 .env)。确认跳过?[y/N] '
    read -r ans || ans=""
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
      echo "  ⚠️ 已跳过 $key —— 用到模型/索引前需补上,否则调用会 401"
      return 0
    fi
  done
}

echo "[2/7] .env 密钥(必填 2 个;其余按需 key 见 .env.example 注释,稍后手动补)"
ask_key "DEEPSEEK_API_KEY"  "所有 LLM 角色(deepseek-v4-pro / v4-flash);https://platform.deepseek.com"
ask_key "DASHSCOPE_API_KEY" "embedding + reranker(阿里云百炼);https://bailian.console.aliyun.com"

# 最小模式提示:没配 embedding key 也能跑 —— 本地模型档(零 key)或纯记忆/结构图用法
if ! grep -q '^DASHSCOPE_API_KEY=..' .env 2>/dev/null; then
  echo "  ℹ️ 未配 DASHSCOPE_API_KEY —— 检索/索引走不了远端 embedding,两条路:"
  echo '     a) 零 key 本地档:`uv run uv sync --extra embedding-local` + config.yaml 把'
  echo "        embedding.provider 切 sentence_transformers、reranker.provider 设 off"
  echo "        (模型经 hf-mirror 本地下载,索引/检索全功能,数据不出本地)"
  echo "     b) 暂不建索引:记忆(recall/memorize)与仓库管理(repo register/checkout)可用,"
  echo "        检索类工具(search_codebase 等)等补 key 再建 —— 详见 docs/configuration.md「最小模式」"
fi

# ── [3/7] 验证配置 + 模型工厂加载(models 只查非空,不打印 key 值)────────
echo "[3/7] 验证模型配置"
uv run rootrecall models

# ── [4/7] (可选)给目标代码库建索引 ─────────────────────────────────────
# 检索类工具(search_codebase / blast_radius / call_chain…)需要索引;记忆类不需要。
echo "[4/7] 建索引(可选)"
printf '要现在给某个代码库建索引吗?输入仓库绝对路径(留空跳过,之后随时: uv run rootrecall index <仓库路径> <索引名>):'
read -r repo_path || repo_path=""
if [ -n "$repo_path" ]; then
  if [ ! -d "$repo_path" ]; then
    echo "  ⚠️ 目录不存在: $repo_path —— 跳过(路径写对后随时手动建)"
  else
    default_name=$(basename "$repo_path")
    printf '  索引名(回车默认 %s): ' "$default_name"
    read -r idx_name || idx_name=""
    idx_name=${idx_name:-$default_name}
    # 零 key 最小模式下 index 友好报错返回 2 —— 别让它把整个 quickstart 打死(set -e),后面步骤照走。
    if ! uv run rootrecall index "$repo_path" "$idx_name"; then
      echo "  ⚠️ 索引未建成(见上方提示)—— 补齐 key/配置后随时重跑: uv run rootrecall index $repo_path $idx_name"
    fi
  fi
else
  echo "  跳过(检索类工具用前再建: uv run rootrecall index <仓库路径> <索引名>)"
fi

# ── [5/7] 代码仓总目录(基线的家)────────────────────────────────────────
# 所有要建基线的代码仓都 clone 进这一个目录;baseline add 按目录结构自动命名
# (v20/bluez → bluez-v20、upstream/bluez → bluez-upstream、systemd → systemd)。
printf '代码仓总目录(回车默认 ~/codebases,已存在则复用):'
read -r cb_root || cb_root=""
cb_root=${cb_root/#\~/$HOME}
cb_root=${cb_root:-$HOME/codebases}
mkdir -p "$cb_root"
env_set "ROOTRECALL_CODEBASES" "$cb_root"
echo "  ✅ 总目录就绪:$cb_root(已写入 .env:ROOTRECALL_CODEBASES)"
echo "  下一步 —— 把源码 clone 进去后,每仓一条命令建基线(登记+建索引):"
echo "    uv run rootrecall baseline add $cb_root/v20/bluez     # → 基线 bluez-v20"
echo "    uv run rootrecall baseline add $cb_root/v25/bluez     # → 基线 bluez-v25"
echo "    uv run rootrecall baseline add $cb_root/systemd       # → 基线 systemd"

# ── [6/7] opencode 全局注册(默认装不问)+ 可选项目级精确定锚 ──────────────
echo "[6/7] opencode 全局注册(四件套:skills/MCP/agent 块/路由表;装一次任意目录直接用)"
# 2026-08-26 用户定:默认装不问 —— 路由表带适用范围守卫行,无关会话自动忽略;
# 介意随时 `uv run rootrecall uninstall --global` 整套摘除。
if command -v opencode >/dev/null 2>&1; then
  uv run rootrecall install --global
else
  echo "  ⚠️ 未检测到 opencode,跳过(装好 opencode 后随时: uv run rootrecall install --global)"
fi
printf '(可选)给某个 bug/工作仓做项目级精确定锚(钉默认检索库)?输入目录绝对路径,多个用空格分隔(留空跳过):'
read -r bug_dirs || bug_dirs=""
if [ -n "$bug_dirs" ]; then
  # shellcheck disable=SC2086  # 用户按空格分隔输入多个路径
  bash scripts/wire_opencode.sh $bug_dirs
else
  echo "  跳过(全局注册已够用;单个 bug 目录要定默认检索库时: cd <目录> && uv run rootrecall here --codebase <索引名>)"
fi

# ── [7/7] opencode 接线自检 + 启动指引 ──────────────────────────────────
echo "[7/7] opencode 接线自检"
ok=1
if command -v opencode >/dev/null 2>&1; then
  echo "  ✅ opencode 已安装: $(command -v opencode)"
else
  ok=0
  echo "  ⚠️ 未检测到 opencode —— 先安装:https://opencode.ai(装好即用,无需重跑本脚本)"
fi
if [ -L opencode.json ]; then
  echo "  ✅ opencode.json 软链就绪 -> $(readlink opencode.json)"
elif [ -e opencode.json ]; then
  ok=0
  echo "  ⚠️ opencode.json 存在但不是软链(疑似被覆盖)—— 应为: ln -sf config/opencode_rootrecall.json opencode.json"
else
  ln -sf config/opencode_rootrecall.json opencode.json
  echo "  ✅ 已补建 opencode.json 软链 -> config/opencode_rootrecall.json"
fi
skill_n=$(ls .claude/skills | wc -l | tr -d ' ')
echo "  ✅ skill x ${skill_n}(.claude/skills/,opencode 自动发现)"

echo ""
echo "════════════════════════════════════════════════════════"
if [ "$ok" -eq 1 ]; then
  echo "✅ 配置完成!opencode 启动位置二选一:"
  echo "   ① 默认:cd $REPO && opencode"
  echo "   ② 已接线的 bug/工作仓:cd <该仓> && opencode(MCP 经 cwd 锚到本仓根 —— uv 找得到"
  echo "      .venv、data/ 不漂移;skill 走软链发现;.env 由 rootrecall 进程自加载)"
  echo ""
  echo "   数据落点:默认仓内 $REPO/data/;要迁出(放 ~/.local/share、换盘)→ 设 ROOTRECALL_HOME"
  echo "   后重跑 install --global 生效,已有数据 mv 过去即可 —— 详见 docs/configuration.md"
  echo "   「数据落点与 ROOTRECALL_HOME」"
echo "   试用示例:「为什么 wpa 的 P2P 会话会泄漏?」(bug-rca)/"
echo "             「这个仓库整体架构怎么组织?」(onboarding)"
echo "   基线管理:baseline ls 看基线 / baseline sync 同步+增量索引 / baseline checkout 取指定版本"
else
  echo "⚠️ 上方有 ⚠️ 项未就绪 —— 按提示处理即可,其余步骤已完成无需重跑。"
fi
echo "════════════════════════════════════════════════════════"
