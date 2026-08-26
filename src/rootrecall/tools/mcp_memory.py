"""RootRecall MCP server —— 把 RootRecall 的差异化能力做成工具,给 delegate(opencode)现场调。

不是"MCP 驱动 delegate",而是"delegate 查 RootRecall":opencode 干活时经 MCP 调本服务暴露的
工具(见 bug-rca-design.md §6 反向 MCP)。一组工具(harness 转向:精炼工具面,只做 coding agent
做不好/做不了的 —— 记忆/代码情报/影响面/补丁验证/补丁落盘/报告落盘;定位推理+改代码+日志切片都留给 opencode 的 read/grep/awk):
  - memory_recall     翻长期记忆(历史 bug 教训 / 代码库事实),带 file:line 溯源。
  - memory_memorize   写一条记忆(ad-hoc;报告/补丁走 workflow 自动记)。
  - memory_dump       把记忆库摊开做体检(浏览/审计,区别于 recall 的 query 式检索)。
  - search_codebase   语义+符号检索代码,**只回索引里真实存在的符号**(emit-concept 防幻觉)。
  - blast_radius      改动影响面(结构图 BFS:改这些文件会波及谁;harness 转向 D0)。
  - call_chain        符号中心的 N 跳调用链(仅 CALLS 边)+ PageRank 重要度(谁调它/它调谁;P1.5 caller/callee 进适配层)。
  - repo_map          全仓 PageRank 排名符号地图(Aider repomap 式,塞进 token 预算;俯瞰「哪些函数结构上最核心」;#38)。
  - cross_version_diff 同仓两 git ref 跨版本对比(base..head 提交门 + concern diff + 触及函数 + cherry 等价;feature 2b;git 为核图可选)。
  - merge_eval         上游 commit 合入评估(逐 commit 三态:已修/建议合/冲突;patch-id 等价 + apply 检查;低优#1;git 为核图可选,全程 local-git)。
  - validate_patch    补丁能否干净 apply(`git apply --check`,执行硬门零 LLM;harness 转向 D0)。
  - export_patch      把补丁落盘成 .patch 文件(交付硬门 —— 聊天不算交付;空 diff 自检;harness 转向 D1)。
  - export_report     把分析报告落盘成 .md 文件(交付硬门 —— 报告跟补丁一样要上盘;空内容自检;harness 转向 D1)。

防幻觉契约(§6.1 search_codebase):模型传一个**概念/自然语言**(不是猜的文件名/函数名),
工具从**真实索引**里检索 → 只回**索引中确实存在**的 file:symbol:line。因为结果来自实际索引,
模型拿不到一个编造的文件路径 —— 幻觉在结构上不可能。这正是 2026 主流(Claude Code 弃向量库
改 agentic search / Cursor codebase indexing):agent 发概念,工具回验过的真实符号。

入口:`rootrecall mcp serve [--codebase NAME]`。需 `uv sync --extra mcp`。
  transport:stdio(默认,agent 拉起子进程 1:1)| http(`--transport http`,warm 长进程,
             多 agent 共用,省 cold-boot —— 解 ③;端点 http://host:port/mcp)。
--codebase:查哪个代码库的索引/记忆(= LanceDB 表名 + memory scope);不传则按
            config.code_index.repo → 进程 cwd 目录名 兜底(opencode 常在项目根拉起 MCP)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from rootrecall.platform.config import get_app_config
from rootrecall.services.memory.schema import Scope


def _resolve_codebase(explicit: str | None) -> str:
    """定查哪个代码库:--codebase > ROOTRECALL_CODEBASE env > config.code_index.repo > 唯一注册库 > cwd 目录名。

    ROOTRECALL_CODEBASE 由 delegate(opencode 父进程)注入、opencode 透传给 MCP 子进程
    (local server 的 environment 字段不展开 {env:},靠进程 env 继承 —— 2026-08-03 源码核实)。
    「唯一注册库」档(2026-08-26 全局化配套):全局安装后服务器 cwd 恒为安装根,cwd 目录名
    永远无意义;单库机器(systemd-only)恰好只有一个注册基线时直接当默认,零配置零传参。
    多库机器不定默认 —— 交给近义容错(传项目名列候选)+ find_repo 自动开仓。
    """
    import os
    if explicit:
        return explicit
    env_cb = os.environ.get("ROOTRECALL_CODEBASE")
    if env_cb:
        return env_cb
    cfg = get_app_config()
    repo = getattr(cfg.code_index, "repo", None)
    if repo:
        return repo
    try:
        from rootrecall.services.repos.registry import known_codebases

        known = known_codebases()
        if len(known) == 1:
            return next(iter(known))
    except Exception:  # noqa: BLE001 —— 注册表坏不影响默认链,继续走 cwd 兜底
        pass
    return Path.cwd().name


def _resolve_repo_path_arg(name_or_path: str) -> str:
    """repo_path 参数「名字/路径」两吃(F1 repo registry)。

    路径样输入且存在 → 原样返回(老行为,零变化);光秃名字 → 走 resolve_repo_path
    反查(注册表 > 索引清单 repo_path > data/repos 落点),命中换成解析出的绝对路径。
    解析失败**不改原值**(让下游按"路径不存在"的老报错走,附带本函数的提示更友好)。
    """
    p = Path(name_or_path).expanduser()
    if (p.is_absolute() or "/" in name_or_path or "\\" in name_or_path) and p.is_dir():
        return str(p.resolve())
    try:
        from rootrecall.services.repos.registry import resolve_repo_path

        resolved, source = resolve_repo_path(name_or_path)
    except Exception:  # noqa: BLE001 —— 注册表层坏(文件损坏等)不挡工具主链路
        return name_or_path
    if resolved is not None:
        return str(resolved)
    return name_or_path  # 查不到 → 原样交下游报错(agent 会看到老格式的"不是目录"提示)


# ── codebase 近义名容错(2026-08-25 实测教训)─────────────────────────────────
# 记忆吃项目名(bluez,教训跨版本共享),索引/图吃注册名(bluez-v25)—— agent 从记忆
# 拿到 "bluez" 直接传工具,四连败后才摸到正名。这里做「精确 > 归一化 > 唯一子串」三级
# 解析:唯一命中自动纠偏并注明,多个命中列出候选让 agent 一次改对,全落空给出本机已
# 知清单。只影响原本就要报错的名字;精确名走快路,零行为变化。


def _norm_codebase(name: str) -> str:
    """近义匹配归一:去首尾空白与斜杠 + lower + 下划线归一连字符。"""
    return name.strip().strip("/").lower().replace("_", "-")


def _match_codebase(requested: str, known) -> tuple[str | None, list[str]]:
    """在 known(可迭代名集)里找 requested 的近义名。

    匹配级:精确(原样在册)→ 归一化精确(bluez_v25 ↔ bluez-v25)→ 子串双向包含
    (bluez ⊂ bluez-v25;bluez-v25-5.85 ⊃ bluez-v25)。子串命中**唯一**才自动采用;
    多个 → (None, 候选)交调用方列举;全落空 → (None, [])。空请求 → (None, [])。
    """
    q = _norm_codebase(requested)
    if not q:
        return None, []
    if requested in known:
        return requested, []
    by_norm = {n: k for k in known if (n := _norm_codebase(k))}
    if q in by_norm:
        return by_norm[q], []
    subs = sorted(orig for norm, orig in by_norm.items() if q in norm or norm in q)
    if len(subs) == 1:
        return subs[0], subs
    return None, subs


def _resolve_active_codebase(raw: str) -> tuple[str | None, str, dict[str, set[str]]]:
    """工具层 codebase 名解析(近义容错)。返回 (规范名 | None, 说明行, 名→来源集)。

    名字为 None 时说明行是完整错误串(工具直接 return 它);名字在时说明行要么为空
    (精确命中,输出零变化)要么是一行纠偏注记(拼进输出头,agent 可见被纠到哪个库)。
    来源集来自 registry.known_codebases(),调用方用它区分「没建索引」vs「没建图」。
    """
    from rootrecall.services.repos.registry import known_codebases

    known = known_codebases()
    if raw in known:
        return raw, "", known
    matched, subs = _match_codebase(raw, known)
    if matched:
        return matched, f"codebase '{raw}' 近义解析为 '{matched}'。", known
    if subs:
        return None, (f"codebase '{raw}' 匹配到多个代码库:{'、'.join(subs)}。"
                      f"传其一重试(本机全部:`rootrecall baseline ls`)。"), known
    if known:
        return None, (f"没有叫 '{raw}' 的代码库。本机已知:{'、'.join(sorted(known))};"
                      f"查全部 `rootrecall baseline ls`。"), known
    return None, "本机还没有任何已注册/已建索引的代码库,先 `rootrecall baseline add <仓库路径>`。", known


def _graph_missing_msg(target: str, known: dict[str, set[str]]) -> str:
    """图缺失分支的区分报错:在册没图(重建 index)vs 完全未知(先 baseline add)。"""
    if known.get(target, set()) - {"graph"}:  # 注册表/索引里有它,只缺图
        return (f"代码库 '{target}' 已注册/有索引,但结构图未建(data/structgraph/{target}/graph.db 不在;"
                f"可能建时 --no-graph 或图构建失败)。重建:`uv run rootrecall index <仓库路径> {target}`。")
    return (f"代码库 '{target}' 的结构图未建。先建:`rootrecall baseline add <仓库路径> --name {target}`。")


# recall 语义相关度警示阈值(text-embedding-v4,2026-08-26 远端真库标定):
# 相关查询 0.64-0.92,无关查询 0.18-0.28 —— 0.40 居中、两侧余量充足。换嵌入模型需重标定。
_RECALL_SIM_WARN = 0.40


# ── 工具门控(省上下文):ROOTRECALL_MCP_TOOLS 决定 server 注册哪些工具 ──────────
#
# 为什么在「注册」层做、而不是 opencode 的 permission deny:deny 只是"看得见但调不了",
# 工具 schema 仍在上下文里照常占位;不注册的工具根本不进 tools/list,模型看不见 = 真·零开销。
# 用法(opencode 的 mcp.rootrecall.environment,或 shell env):
#   ROOTRECALL_MCP_TOOLS=minimal                        # 预设:bug-RCA 最小集(记忆3+search+硬门3)
#   ROOTRECALL_MCP_TOOLS=research                       # 预设:调研集(记忆3+情报8)
#   ROOTRECALL_MCP_TOOLS=full                           # 预设:全部 16 个(= 不设置,默认)
#   ROOTRECALL_MCP_TOOLS=memory_recall,validate_patch   # 显式清单(工具短名,逗号分隔)
# 拼错名字 → 启动即 ValueError 列出全部可用名(诚实失败,防静默裁错集)。

_ALL_MCP_TOOLS: frozenset[str] = frozenset({
    "memory_recall", "memory_memorize", "memory_dump",
    "search_codebase", "blast_radius", "call_chain", "cross_version_diff",
    "merge_eval", "when_introduced", "repo_map", "repo_overview",
    "validate_patch", "export_patch", "export_report", "fetch_patch", "ensure_repo",
    "find_repo",
})

_MCP_TOOL_PRESETS: dict[str, frozenset[str] | None] = {
    # bug-RCA 最小集:开仓查表 + 翻记忆 + 检索定位 + 三个交付硬门,够走完一条 bug-RCA 主线
    "minimal": frozenset({
        "memory_recall", "memory_memorize", "memory_dump", "find_repo",
        "search_codebase", "validate_patch", "export_patch", "export_report",
    }),
    # 调研集:记忆3 + 代码情报8 + find_repo(onboarding/compare/research 向;不含交付硬门与 PR 抓取)
    "research": frozenset({
        "memory_recall", "memory_memorize", "memory_dump", "find_repo",
        "search_codebase", "blast_radius", "call_chain", "repo_map", "repo_overview",
        "cross_version_diff", "merge_eval", "when_introduced",
    }),
    "full": None,  # 全部 17 个(与不设置等价)
}



def _archive_bug_copy(path: Path, name_or_path: str) -> str | None:
    """交付物按 bug_id 归档(P2-2):gc 回收 ephemeral 仓后,补丁/报告仍可按 bug 追溯。

    name_or_path 是注册名或注册记录的路径,对应记录带 bug_id → 在 <平铺目录>/<bug_id>/ 再写
    一份(同 bug 重跑覆盖;平铺「最新一份」约定不变)。查不到 bug_id / 注册表不可用 → 静默
    跳过(返回 None)—— 归档是增强,绝不挡交付物落盘。
    """
    import shutil

    try:
        from rootrecall.services.repos.registry import RepoRegistry

        reg = RepoRegistry()
        rec = reg.get(name_or_path)
        if rec is None and ("/" in name_or_path):
            rp = Path(name_or_path).resolve()
            rec = next((r for r in reg.list() if r.path and Path(r.path).resolve() == rp), None)
        if rec is None or not rec.bug_id:
            return None
        d = path.parent / rec.bug_id
        d.mkdir(parents=True, exist_ok=True)
        dst = d / path.name
        shutil.copyfile(path, dst)
        return str(dst)
    except Exception:  # noqa: BLE001 —— 归档失败不影响主交付
        return None


def _resolve_mcp_tools() -> frozenset[str] | None:
    """解析 ROOTRECALL_MCP_TOOLS 门控:返回 None = 全量注册(未设置/空串/full);否则 = 允许集。

    取值要么是单个预设名(minimal/research/full),要么是逗号分隔的工具短名清单
    (短名即函数名;opencode 里显示为 rootrecall_<短名>)。预设与清单不混用 —— 要微调就写全清单(YAGNI)。
    名字拼错 → ValueError 附全部可用名 + 预设,启动即炸:好过静默裁错集后 agent 到处找不到工具。
    """
    import os

    raw = (os.environ.get("ROOTRECALL_MCP_TOOLS") or "").strip()
    if not raw or raw == "full":
        return None
    if raw in _MCP_TOOL_PRESETS:
        return _MCP_TOOL_PRESETS[raw]
    names = frozenset(part.strip() for part in raw.split(",") if part.strip())
    unknown = names - _ALL_MCP_TOOLS
    if unknown:
        raise ValueError(
            f"ROOTRECALL_MCP_TOOLS 含未知工具名: {sorted(unknown)}。"
            f"可用工具: {sorted(_ALL_MCP_TOOLS)};可用预设: {sorted(_MCP_TOOL_PRESETS)}"
        )
    return names


def _render_audit_card(it) -> str:
    """把一条 KnowledgeItem 渲染成体检用的溯源卡(大白话:这条记忆 + 它多可信 + 哪来的 + 还有效吗)。

    和 RecallHit.render() 的区别:recall 是按相关性挑几条给 LLM 看(精简、带 score);
    体检卡是给「审记忆库」用的 —— 一次把每条都摊开,重点看四个审计维度:
      ① 置信度 confidence   —— 这条结论我们自己有多大把握(0..1);
      ② 来源档 source_tier  —— 哪来的(委托 agent 最可信 / 工具检索最低);
      ③ 溯源 evidence+commit_sha —— 能不能追到具体代码行/commit(溯源弱的高置信条目要补);
      ④ 时效 valid_at/invalid_at —— 还有效吗,有没有被取代(bi-temporal,失效的标 STALE)。
    access_count 顺带给(被反复召回 = 该不该升级 mental_model 的信号)。
    """
    # 溯源行:有 evidence 就列 file:line(最多 3 条),没 evidence 就空 —— 体检时一眼看出「这条有没有锚到代码」
    if it.evidence:
        ev = "; ".join(f"{e.file}:{e.line}" if e.line else e.file for e in it.evidence[:3])
    else:
        ev = ""
    loc = f"  @{ev}" if ev else "  @无证据(file:line)"
    # 置信度 + 来源档凑一行:体检的核心信号 —— 高置信 + 低来源档(如 tool)要警惕,低置信 + 高来源档可补强
    conf = f"conf={it.confidence:.2f}" if it.confidence else "conf=—"
    tier = f"tier={it.source_tier}"
    sha = f"  sha={it.commit_sha[:8]}" if it.commit_sha else ""  # 有 commit 才标(溯源锚点;没有是体检发现的「溯源弱」信号之一)
    # bi-temporal:失效点 / 被取代 → 标 STALE,体检时这些是「该清理/已过期」的条目
    stale = ""
    if it.invalid_at is not None:
        stale = f"  STALE(invalid {it.invalid_at:%Y-%m-%d})"
    elif it.superseded_by:
        stale = f"  STALE(被 {it.superseded_by[:8]} 取代)"
    # 被纠正(不是失效/取代,但结论已被另一条推翻)→ 标 CORRECTED,体检时看出「这条别再用,看纠正者」
    if not stale and getattr(it, "corrected_by", None):
        stale = f"  CORRECTED(by {it.corrected_by[:8]})"
    # 记录时间 + 被召回次数:created_at 看新旧,access_count 看利用率(低置信却高 access = 待巩固)
    dt = f"  {it.created_at:%Y-%m-%d}" if it.created_at else ""
    acc = f"  hits={it.access_count}" if it.access_count else ""
    # 条目 id(截断 8 位):体检/纠正链要用它 —— memory_memorize(corrects=[...]) 要传「在 dump 输出里看到的 id」,
    # 不渲染 id = 闭环走不通(agent 拿不到被纠正条目 id,被逼去 grep SQLite)。与 sha/CORRECTED 同款对称。
    kid = f"  id={it.id[:8]}" if it.id else ""
    # 治理标签(Phase 3 A2:consolidate 五 pass 的产出):needs_review=未决矛盾 / merged_upstream=补丁已在上游
    # (conf 已打折)/ stale=长期没人翻。体检时一眼看出「这条卡在哪个治理状态」,不用逐条猜。
    tg = f"  [{','.join(it.tags)}]" if getattr(it, "tags", None) else ""
    return f"- [{it.kind}] {it.summary}{loc}  {conf} {tier}{sha}{dt}{acc}{kid}{tg}{stale}".rstrip()


def _honest_truncate(body: str, limit: int, *, how_to_refetch: str) -> str:
    """诚实截断(踩坑 #19 同源治理,2026-08-14):超长才截,且明说截了多少、怎么补取。

    旧病:图/git 类工具 body 一律 `[:8000]` 静默丢尾 —— agent 拿到被截的 JSON 不知道缺了
    东西,基于残缺结果下结论(和 memory_dump 当年吞一半条目同一个病)。修法照抄
    repo_overview / memory_dump 已验证的方子:截断时尾部拼 note,告诉 agent 截断了 +
    指一条补取路径(通常是「收窄参数重调」,如减小 top_n / depth / max_commits)。

    body 未超 limit → 原样返回(零开销,不加噪音)。
    how_to_refetch: 补取指引,写给 agent 看的指令性短语(如 "重调并减小 depth/top_n")。
    """
    if len(body) <= limit:
        return body
    note = (f"\n[截断:返回超 {limit} 字符,已截掉尾部 {len(body) - limit} 字符,"
            f"以上 JSON 可能不完整;{how_to_refetch}]")
    return body[: limit - len(note)] + note


_PLACEHOLDER_MARKERS = ("TBD", "待补", "占位", "待填", "placeholder", "内容回头补", "coming soon")


def _placeholder_reason(content: str) -> str | None:
    """占位报告判定(T24,2026-08-26 实测:空串拒了,TBD 模板照样落盘):返回拒写理由,
    None = 不像占位。两条判据:① 有效内容过短(<200 字符,真报告必有根因/证据/patch 路径/
    memorize id,撑得起这个量);② 占位标记行密度 ≥0.4(真报告偶引一句上游代码的 TODO 不
    误伤 —— TODO 故意不在标记表里,正是为了这个)。"""
    text = content.strip()
    if len(text) < 200:
        return f"有效内容仅 {len(text)} 字符(过短)"
    lines = [ln for ln in (raw.strip() for raw in text.splitlines()) if ln]
    hits = sum(1 for ln in lines if any(m in ln.lower() for m in _PLACEHOLDER_MARKERS))
    if lines and hits / len(lines) >= 0.4:
        return f"{hits}/{len(lines)} 行含占位标记(TBD/待补/占位等)"
    return None


def _retrieval_bundle():
    """懒构造 (embedder, store, reranker)——code_index 检索三件套(search_codebase 用)。

    reranker 可能为 None(provider=off)。2026-08-10 撤 code_nav @tool 层时从 tools/code_nav.py
    搬来内联(embedder/store/reranker 工厂自身带缓存,无需额外 lru_cache)。
    """
    from rootrecall.services.code_index.embed import create_embedder
    from rootrecall.services.code_index.retrieval import create_reranker
    from rootrecall.services.code_index.store import LanceDBStore

    cfg = get_app_config()
    embedder = create_embedder(cfg.code_index.embedding)
    from rootrecall.services.repos.registry import reanchor_data_path

    vs_cfg = getattr(cfg.code_index, "vector_store", None)
    vs_path = getattr(vs_cfg, "path", "data/code_index") if vs_cfg else "data/code_index"
    store = LanceDBStore(reanchor_data_path(vs_path))
    reranker = create_reranker(getattr(cfg.code_index, "reranker", None))
    return embedder, store, reranker


def build_server(codebase: str | None = None, *, host: str | None = None, port: int | None = None):
    """构造 FastMCP server,暴露十七个 RootRecall 工具给 coding agent(opencode/codex/claude code)。

    codebase 在此解析一次,烘焙进各工具闭包当**默认值**;memory_recall / memory_memorize /
    memory_dump / search_codebase / blast_radius / call_chain / repo_map / cross_version_diff / merge_eval 另接受 per-call `codebase` 参数覆盖此默认(多库:
    同一 server 进程可切多个仓),不传则用这里的默认 repo。
    server 名 "rootrecall" —— opencode 按 `<server>_<tool>` 给工具加前缀(如 rootrecall_search_codebase)。
    host/port:仅 streamable-http transport 用(FastMCP 在构造时吃这俩 → settings → uvicorn 监听;
       `run()` 不接收 host/port)。stdio 模式忽略。不传 → 用 FastMCP 默认(127.0.0.1:8000)。
    ROOTRECALL_MCP_TOOLS 环境变量可门控注册哪些工具(预设 minimal/research/full 或显式短名清单,
       见 _resolve_mcp_tools);不设置 = 17 个全量注册(向后兼容)。
    """
    from mcp.server.fastmcp import FastMCP

    from rootrecall.services.memory import get_memory_service

    repo = _resolve_codebase(codebase)
    # host/port 只在给定时透传给 FastMCP(stdio 模式用不上,但给了也无害)
    fastmcp_kwargs: dict = {}
    if host is not None:
        fastmcp_kwargs["host"] = host
    if port is not None:
        fastmcp_kwargs["port"] = port
    mcp = FastMCP("rootrecall", **fastmcp_kwargs)
    svc = get_memory_service()

    # 工具门控:enabled_tools=None 全量;否则只注册清单内的(未入选的不进 tools/list,
    # 模型看不见其 schema —— 上下文真省;对照 permission deny 的"看得见但调不了")。
    enabled_tools = _resolve_mcp_tools()

    def _tool(name: str):
        """门控版 @mcp.tool():工具在允许集 → 正常注册;不在 → 原样返回函数、不注册。"""

        def deco(fn):
            if enabled_tools is None or name in enabled_tools:
                return mcp.tool()(fn)
            return fn

        return deco

    # ── ① memory_recall:翻长期记忆(R1 已有,这里薄封一层 scope)────────────
    @_tool("memory_recall")
    async def memory_recall(query: str, top_k: int = 5, kind: str | None = None,
                            codebase: str | None = None) -> str:
        """Recall from RootRecall's long-term memory: historical bug lessons / codebase facts
        relevant to the query, each with file:line provenance + confidence + recency.

        Call this BEFORE localizing/patching to reuse prior root-causes/fixes for this codebase.
        kind: optional filter — "bug_lesson" returns only past patches/fixes (excludes
              codebase facts); omit for all kinds. Multiplies fetch then filters, so the kind
              filter won't starve results (absorbed the former patch_search tool).
        codebase: override which codebase's memory to recall from (default = this server's
              codebase). Pass when the bug you're investigating belongs to a different repo than
              the server's default; recall is scope-isolated so it never crosses codebases.
              NAMING: pass the PROJECT name (e.g. ``wpa``), never a version-line name
              (``wpa-v25``) — memory is scope-isolated, so a version-scoped label would lock
              lessons inside one version (v20 sessions could never recall v25 lessons). Version
              context belongs in summary/evidence; version-line names are for index/retrieval
              tools (search_codebase etc.).
              The shared ``general`` pool (domain knowledge) is ALWAYS searched alongside your
              codebase — hits from another pool are prefixed ``[pool]``, and an empty result lists
              the non-empty scopes so you can fix a wrong codebase in one retry.
        """
        # per-call codebase 覆盖(模板同 blast_radius 的 `codebase or repo`);不传 = 闭包默认 repo。
        active_repo = codebase or repo
        active_scope = Scope(owner="default", codebase=active_repo)
        # memory-only 检索(svc.search = recall 关掉 code/structural 两路 + 不 bump)。
        # 故意不调 svc.recall():那个会混进 code_index 的代码 chunk,而本工具的职责是翻「长期记忆」
        # (bug_lesson / codebase_fact),代码检索另有 search_codebase 工具 —— 混进来既是职责重叠
        # (踩坑#2 变体),也和本 docstring 矛盾,还会用无关 code chunk 稀释记忆信号。
        # 给了 kind → 多取再按 kind 过滤(留余量);否则按 top_k 直取。
        fetch_k = max(top_k * 3, top_k) if kind else top_k
        # 并查 general 池(2026-08-26 实测:领域知识记在 general、项目会话默认作用域各异,
        # 单池查询「查一个漏一个」——A2DP 一条在 bluez 一条在 general,demo 会话只查到一条)。
        # active==general 时不重复查;命中按 item_id 去重、按分合排,跨池命中加 [作用域] 前缀。
        scopes = [active_scope]
        if active_scope.codebase != "general":
            scopes.append(Scope(owner="default", codebase="general"))
        hits: list = []
        seen: set[str] = set()
        for s in scopes:
            for h in await svc.search(query, s, top_k=fetch_k):
                key = h.item_id or f"{h.summary}@{s.codebase}"
                if key in seen:
                    continue
                seen.add(key)
                hits.append(h)
        hits.sort(key=lambda h: h.score, reverse=True)
        if kind:
            hits = [h for h in hits if (h.kind or "") == kind][:top_k]
        if not hits:
            # 空池提示(2026-08-26 实测:agent 没传 codebase 探到服务器默认空池,连试两轮;
            # 列出非空作用域让它一次改对)。后端不支持 list_scopes → 静默跳过,不加提示。
            hint = ""
            try:
                avail = await svc.list_scopes()
            except Exception:  # noqa: BLE001 —— 提示是增强,后端没有这能力就不挡主链路
                avail = None
            if avail:
                hint = "非空作用域:" + "、".join(f"{cb}({n})" for cb, n in avail[:8]) + "。"
            tag = f", kind={kind}" if kind else ""
            return (f"No memory found for '{query}' (codebase={active_repo}{tag},已并查 general 池)。{hint}"
                    f"确认 codebase 传对了吗 —— 项目记忆传项目名(如 bluez),领域知识在 general;"
                    f"服务器默认作用域常常不是你要的那个池子。")
        tag = f", kind={kind}" if kind else ""
        shown = hits[:top_k]
        # 语义相关度警示(2026-08-26 标定:RRF 满分 ≠ 相关 —— 无关查询在小池也拿 0.0315;
        # 余弦才是信号,相关 0.64-0.92 / 无关 0.18-0.28,阈值 0.40 居中)。只标不删:低相关
        # 命中仍可见(诚实),但头牌低相关时明确劝退短路 —— 防「无关查询被当命中」的假秒答。
        top_sim = shown[0].sim if shown else None
        warn = ""
        if top_sim is not None and top_sim < _RECALL_SIM_WARN:
            warn = (f"⚠️ 最高命中的语义相关度 sim={top_sim:.2f} < {_RECALL_SIM_WARN} —— 大概率主题不符,"
                    f"按 miss 处理(走冷路径完整调研),别拿这些条目短路。\n")
        out = [f"{warn}Recalled {len(shown)} (by relevance, codebase={active_repo}{tag},并查 general 池):"]
        for h in shown:
            line = h.render()
            if h.repo and h.repo != active_repo:
                line = f"[{h.repo}] {line}"  # 跨池命中亮明住在哪(治「查一个漏一个」)
            if h.sim is not None and h.sim < _RECALL_SIM_WARN:
                line += f"  (低相关 {h.sim:.2f})"
            out.append(line)
        return "\n".join(out)

    # ── ② memory_memorize:写一条记忆(报告/补丁走 workflow 自动记,这是 ad-hoc 入口)──
    @_tool("memory_memorize")
    async def memory_memorize(kind: Literal["codebase_fact", "bug_lesson", "domain_knowledge"], summary: str,
                              file: str | None = None, line: int | None = None,
                              evidence: list[dict] | None = None,
                              root_cause: str = "",
                              fix_patch: str = "",
                              symptom: str = "",
                              blast_radius_files: list[str] | None = None,
                              commit_sha: str | None = None,
                              tags: list[str] | None = None,
                              corrects: list[str] | None = None,
                              verification: Literal["apply_only", "real_machine"] | None = None,
                              kind_detail: Literal["module", "symbol", "architecture", "domain"] | None = None,
                              confidence: float | None = None,
                              source_url: str | None = None,
                              codebase: str | None = None) -> str:
        """Write one knowledge item into RootRecall's long-term memory (cross-session reuse).

        kind: codebase_fact | bug_lesson | domain_knowledge. Prefer letting the bug_rca/patch_review
        flow auto-memorize; use this only for ad-hoc facts/lessons a delegate discovers on-site.
        domain_knowledge = domain/project knowledge (protocol semantics, wpa layer responsibilities) —
        the semantic-memory layer, distinct from codebase-fact (code-anchored) and bug-lesson
        (episode). It joins recall like any other kind (refutes misdiagnosis), but does NOT
        auto-promote to mental_model (domain knowledge is evergreen, not a "graduating" rule).

        For a patch/PR analysis (kind=bug_lesson): pass fix_patch (the unified diff). The item is then
        content-addressed by the PATCH text (not the summary), so re-memorizing the same patch MERGES
        (confidence bump) instead of duplicating. Pair with blast_radius_files + commit_sha + tags
        (e.g. ["patch_insight"]) so the lesson is searchable and provenance-traceable. Put your
        verdict (intent / correctness / merge recommendation) in summary + root_cause.
        evidence: list of provenance anchors, each ``{"file": <path>, "line": <int?>, "snippet": <str?>}``.
              Use this when a fact spans MULTIPLE file:line locations (e.g. an architecture fact that
              references the entry function, the dispatch table, and an event handler). The legacy
              ``file``+``line`` params cover the single-anchor case and are merged in if also passed;
              prefer ``evidence`` for architecture/codebase-fact memories and leave file/line for the
              single-anchor bug-lesson case.
        kind_detail: finer classification for codebase_fact / domain_knowledge —
              module | symbol | architecture | domain. Use ``architecture`` for onboarding-tour /
              structural facts; use ``domain`` for domain_knowledge. Ignored for bug_lesson.
        source_url: external provenance URL for domain_knowledge (the web source you researched the
              protocol/domain fact from). When set, the item's source_tier is ``imported`` (web-sourced);
              when unset, ``stated`` (a user's own technical note). Ignored for bug_lesson/codebase_fact
              (their provenance is commit_sha + evidence file:line, code-anchored).
        confidence: 0..1 override of the initial confidence (otherwise derived from source_tier:
              delegate = 0.5). Set only when you have a real reason to weigh this above/below the
              delegate default (e.g. a fact you inferred vs one you read directly).
        corrects: list of knowledge-item IDs that THIS item corrects/supersedes. Use when your new
              finding explicitly overturns a prior root-cause or conclusion — the old items stay
              (append-only preserved for audit) but get a ``corrected_by`` backlink and are demoted
              at retrieval time (recall scores them 0.3× lower). Pass the IDs you saw in
              ``memory_recall`` or ``memory_dump`` output. Leave empty for ordinary facts/lessons.
        verification (bug_lesson only, 2026-08-20 纪律硬化): declare the evidence level of this lesson.
              ``"apply_only"`` = patch applies cleanly but NOT yet confirmed on a real machine →
              the item is tagged ``unverified`` (recall renders ``(未真机验证)``) and confidence
              capped at 0.5. ``"real_machine"`` = user confirmed on a real machine → tagged
              ``verified_real_machine``. Re-memorizing the SAME patch with real_machine merges
              (content-addressed id) and replaces the tags — the upgrade path: record early with
              apply_only, upgrade after user confirmation. Omit = legacy behavior (no marker).
              Structural rule instead of "don't write until verified": an apply-only lesson honestly
              labeled beats a lost lesson — recall-first consumers see the caveat and weigh it.
        codebase: override which codebase's memory to write into (default = this server's codebase).
              Pass when the lesson belongs to a different repo than the server's default; the item is
              scoped (id namespaced + filtered) by this codebase, so it won't pollute others.
              NAMING: pass the PROJECT name (e.g. ``wpa``), never a version-line name (``wpa-v25``) —
              scope isolation means a version label locks the lesson inside that version (v25 sessions
              would recall nothing recorded under wpa-v20). Put the version in summary/commit_sha/
              evidence instead; version-line names are for index/retrieval tools.
              IGNORED for domain_knowledge: domain facts always land in the shared ``general`` pool
              (auto-redirected) so any session can recall them — project-scoped domain knowledge gets
              lost in one repo's pool (2026-08-26 field report: A2DP facts split across bluez + general,
              recall found one or the other, never both).
        """
        from rootrecall.services.memory.schema import Evidence, KnowledgeItem, SourceTier, make_id

        # per-call codebase 覆盖(模板同 blast_radius);不传 = 闭包默认 repo/scope。
        active_repo = codebase or repo
        # 领域知识统一入 general 池(2026-08-26 实测:A2DP 一条记 bluez、一条记 general,同主题
        # 裂成两池,recall 查一漏一)。这里强制归一,不信任调用方记性;原传值在输出里注明。
        cb_note = ""
        if kind == "domain_knowledge" and active_repo != "general":
            cb_note = f"(domain_knowledge 统一入 general 池,原传 codebase={active_repo} 已改写)"
            active_repo = "general"
        active_scope = Scope(owner="default", codebase=active_repo)
        blast_radius_files = blast_radius_files or []
        tags = tags or []
        corrects = corrects or []
        # 给了 fix_patch → id 按补丁内容算(对齐 ingest.py:415),同补丁重复 memorize 走合并而非新增。
        kid = make_id(active_scope, kind, fix_patch) if fix_patch else ""

        # evidence 合并:新 evidence(多锚点,list[dict])+ 旧 file/line(单锚点,向后兼容)。
        # 去重:同一 (file,line) 只留一条(架构事实常在 evidence 里重复列同一个入口)。
        ev_list: list[Evidence] = []
        seen_loc: set[tuple[str, int | None]] = set()
        for e in (evidence or []):
            if not isinstance(e, dict) or not e.get("file"):
                continue
            ef = str(e["file"])
            el = e.get("line")
            el = int(el) if isinstance(el, (int, str)) and str(el).strip().lstrip("-").isdigit() else None
            if (ef, el) in seen_loc:
                continue
            seen_loc.add((ef, el))
            ev_list.append(Evidence(file=ef, line=el, snippet=str(e.get("snippet") or "")))
        if file and (file, line) not in seen_loc:
            ev_list.append(Evidence(file=file, line=line))

        # 验证纪律(P2-1,结构化而非禁令):apply-only 的 bug_lesson 打 unverified 标 + 置信封顶
        # 0.5;真机确认后同补丁重提 real_machine(同 id 合并,新条 tags 替换旧的)即升级。
        tags = list(dict.fromkeys(tags or []))
        if verification == "apply_only":
            if "unverified" not in tags:
                tags.append("unverified")
            confidence = min(confidence, 0.5) if confidence is not None else 0.5
        elif verification == "real_machine":
            tags = [t for t in tags if t != "unverified"]
            if "verified_real_machine" not in tags:
                tags.append("verified_real_machine")

        item = KnowledgeItem(
            id=kid,
            kind=kind, repo=active_repo, scope=active_scope, summary=summary, root_cause=root_cause,
            symptom=symptom, fix_patch=fix_patch,
            # kind_detail codebase_fact/domain_knowledge 有意义(bug_lesson 不用);None → schema 默认(module)。
            kind_detail=kind_detail if (kind in ("codebase_fact", "domain_knowledge") and kind_detail) else "module",
            blast_radius_files=list(dict.fromkeys(blast_radius_files)),
            commit_sha=commit_sha, tags=tags,
            corrects=list(dict.fromkeys(corrects)),
            evidence=ev_list,
            source_url=source_url,
            # confidence 显式给(0..1)才覆盖;None → schema 按 source_tier 算默认(delegate=0.5)。
            confidence=confidence if confidence is not None else 0.5,
            # source_tier 分层:domain_knowledge 按 source_url 有无分(网调=imported / 用户笔记=stated);
            # bug/codebase_fact 维持 delegate(委托 agent 产出,最可信)。
            source="mcp",
            source_tier=(SourceTier.imported if source_url else SourceTier.stated)
            if kind == "domain_knowledge" else SourceTier.delegate,
        )
        n = await svc.memorize([item], active_scope)
        extra = f" corrects={len(corrects)}" if corrects else ""
        url_extra = f" source_url={source_url}" if source_url else ""
        return f"memorized id={item.id} kind={kind} codebase={active_repo} ({n} merged/added){extra}{url_extra}{cb_note}"

    # ── ②b memory_dump:把记忆库摊开做体检(浏览/审计,区别于 recall 的 query 式检索)──
    # recall 是「按 query 相关性挑几条」(得先知道问啥);memory_dump 是「一次把全量摊开看」——
    # 体检记忆库:「关于这个仓我们到底记了啥 / 每条多可信 / 哪来的 / 还有效吗」。这是可审计知识库的入口。
    # 包 MemoryService.list_items(已是契约,0 新服务代码),每条渲染成带溯源的体检卡(_render_audit_card)。
    @_tool("memory_dump")
    async def memory_dump(kind: str | None = None, include_invalid: bool = False,
                          codebase: str | None = None,
                          limit: int = 60, offset: int = 0) -> str:
        """Dump (browse/audit) RootRecall's long-term memory for a codebase — every knowledge item with
        its confidence + provenance + bi-temporal status. NOT a relevance search.

        The opposite of memory_recall (which finds a few items by query relevance): memory_dump lists
        ALL items so you can audit the knowledge base — "what do we actually know about this repo" /
        "how trustworthy is each memory" / "which are stale or superseded". Each item renders as a
        provenance card: summary / kind / confidence / source_tier / evidence file:line / commit_sha /
        valid_at / access_count, with stale (invalid_at / superseded_by) items flagged STALE.

        Use this for a memory health-check (what's high vs low confidence, what lacks provenance, what's
        stale, where there are unresolved conflicts between high-confidence items).
        kind:            optional filter — codebase_fact | bug_lesson | mental_model (omit = all).
        include_invalid: also show soft-deleted / superseded items (default False = active only).
        codebase:        override which codebase's memory to dump (default = this server's codebase).
              Pass the PROJECT name (e.g. ``wpa``), not a version-line name — memory scopes are
              project-level (same naming convention as memory_recall/memory_memorize).
        limit/offset:    pagination — the dump returns at most ``limit`` items starting at ``offset``
                  (default first 60). A health-check needs the WHOLE picture, so if there are more
                  items (header says "showing 1-60 of N, more → memory_dump(offset=60)"), PAGE THROUGH
                  the rest by bumping offset rather than auditing an incomplete slice.
        """
        active_repo = codebase or repo
        active_scope = Scope(owner="default", codebase=active_repo)
        items = await svc.list_items(active_scope, kind=kind, include_invalid=include_invalid)
        if not items:
            tag = f", kind={kind}" if kind else ""
            inv = ", include_invalid" if include_invalid else ""
            return f"No memory for codebase={active_repo}{tag}{inv}."
        total = len(items)
        # 分页:体检要全量,但单次返回过大撑爆上下文。默认 60 条/页,agent 按需翻页(offset += limit)。
        page = items[offset:offset + limit]
        tag = f", kind={kind}" if kind else ""
        header = (f"Memory dump: {total} items (codebase={active_repo}{tag}"
                  f"{', +invalid' if include_invalid else ''})")
        # 健康概要(Phase 3 A2:治理标签聚合,header 一行看全局):consolidate 五 pass 打的标签
        # 按类计数(needs_review=未决矛盾 / merged_upstream=补丁已在上游 / stale=长期没翻)。
        # agent 不翻页也能先拿到「库里有没有治理信号」的总量;0 个标签时这行省掉(不输出噪音)。
        tag_counts: dict[str, int] = {}
        for it in items:
            for t in getattr(it, "tags", None) or []:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        if tag_counts:
            summary_bits = " ".join(f"{t}={n}" for t, n in sorted(tag_counts.items()))
            header += f"  health: {summary_bits}"
        # 还有下一页 → 显式提示翻页(诚实信号,不静默截断:体检漏看一半会误判健康度)。
        if total > offset + limit:
            header += (f"  [showing {offset + 1}-{offset + len(page)} of {total},"
                       f" more → memory_dump(offset={offset + limit})]")
        elif offset > 0:
            header += f"  [showing {offset + 1}-{offset + len(page)} of {total}]"
        out = [header] + [_render_audit_card(it) for it in page]
        return "\n".join(out)[:12000]

    # ── ③ search_codebase:语义+符号检索(防幻觉:只回索引里真实存在的符号)──────
    @_tool("search_codebase")
    async def search_codebase(query: str, top_k: int = 5, codebase: str | None = None) -> str:
        """Semantic + symbol search over this codebase's index (BM25 + vector + RRF + rerank).

        Pass a CONCEPT / natural-language query (e.g. "p2p scan result routing", "radio work
        lifecycle free"), NOT a guessed file/function name. Returns ONLY symbols that REALLY EXIST
        in the indexed codebase — each with file:line + symbol + score + first line. Because the
        result comes straight from the actual index, you cannot be handed a hallucinated path.

        Cheaper + more precise than grepping the whole tree by hand. Needs the codebase indexed
        (`uv run rootrecall index <path> <name>`); returns a "not indexed" hint otherwise.
        codebase: override which codebase's index to search (default = this server's codebase).
              Pass when the code you're looking for lives in a different repo than the server's
              default; the index is table-per-repo, so each codebase is searched in isolation.
              Near-names are fuzzy-resolved against known codebases (list: `rootrecall baseline ls`).
        """
        from rootrecall.services.code_index.retrieval import retrieve
        # per-call codebase 覆盖(模板同 blast_radius);不传 = 闭包默认 repo。
        target, cb_note, known = _resolve_active_codebase(codebase or repo)
        if target is None:
            return cb_note
        try:
            embedder, store, reranker = _retrieval_bundle()  # 模块级检索单例(embedder/store/reranker)
        except Exception as e:  # noqa: BLE001 —— 依赖没装好给可操作错误串,不抛崩整个 server
            return f"search_codebase 初始化失败(检查 config.code_index / .env): {e}"

        try:
            if store.count(target) == 0:  # 表不存在或为空
                if known.get(target, set()) - {"index"}:  # 注册表/图里有它,只缺向量索引
                    return (f"代码库 '{target}' 已注册但向量索引是空的(建索引失败过或被清)。"
                            f"重建:`uv run rootrecall index <仓库路径> {target}`。")
                return (f"代码库 '{target}' 还没建索引(表空)。先建:"
                        f"`uv run rootrecall index <仓库路径> {target}`。")
        except Exception:
            return (f"代码库 '{target}' 还没建索引。先建:"
                    f"`uv run rootrecall index <仓库路径> {target}`。")

        try:
            result = retrieve(query, target, embedder, store, reranker, top_k=top_k)
        except Exception as e:  # noqa: BLE001
            return f"检索失败: {e}"

        if not result.hits:
            return f"{cb_note}未找到与 '{query}' 相关的代码(检索路径 {result.out_mode},codebase={target})。"
        out = [f"{cb_note}检索路径 {result.out_mode} · top-{len(result.hits)}(均为索引内真实符号,codebase={target})"]
        for h in result.hits:
            first = h.text.splitlines()[0][:120] if h.text.splitlines() else ""
            out.append(f"\n{h.file}:{h.start_line}-{h.end_line}  ({h.kind} {h.symbol})  score={h.score:.3f}\n  {first}")
        return "\n".join(out)

    # ── ⑤ blast_radius:改动影响面(结构图 BFS —— 改这些文件会波及谁)──────────
    # harness 转向:把 CodeGraph.impact_radius 暴露成工具,让 agent 改代码前查"动了这些会断哪"。
    @_tool("blast_radius")
    async def blast_radius(changed_files: list[str], codebase: str | None = None) -> str:
        """Structural blast-radius: given a set of changed files, return what else gets hit
        (callers / callees / dependents via code-graph BFS) — the "if I touch these, what breaks" view.

        Pass the file paths a patch/PR modifies. Graph-driven, no LLM. Needs the codebase graph built
        (`uv run rootrecall index <path> <name>`); returns a "not built" hint otherwise.
        File-level by nature — great for leaf/small modules, low discrimination for core hubs
        (hundreds of neighbors); when the result says so, switch to call_chain (symbol-level) for
        the specific function you're changing.
        codebase: override which codebase's graph (default = this server's codebase; near-names
              fuzzy-resolved, list: `rootrecall baseline ls`).
        """
        try:
            from rootrecall.services.code_index.code_graph import CodeGraph
        except Exception as e:  # noqa: BLE001 —— code-review-graph 未装给可操作提示
            return (f"blast_radius 不可用:结构图后端未装。装它: `uv sync --extra code-review-graph`\n  ({e})")
        if not changed_files:
            return "未传 changed_files(传会被改动的文件路径列表)。"
        target, cb_note, known = _resolve_active_codebase(codebase or repo)
        if target is None:
            return cb_note
        try:
            cg = CodeGraph.open(target)
            result = cg.impact_radius(list(changed_files))
        except FileNotFoundError:
            return _graph_missing_msg(target, known)
        except Exception as e:  # noqa: BLE001
            return f"算影响面失败({target}): {e}"
        import json
        body = json.dumps(result, ensure_ascii=False, default=str)
        n_nodes = len(result.get("impacted_nodes") or [])
        n_files = len(result.get("impacted_files") or [])
        # 波及面过大提示(2026-08-26 实测:player.c 这类 core 模块文件级 BFS 出数百节点 +
        # 8000 字符截断,对决策零鉴别力,agent 最终还是靠读代码)。此时明说「文件级不顶用,
        # 换符号级 call_chain 定点」,别让 agent 对着一坨截断 JSON 硬啃。
        big = bool(result.get("truncated")) or n_nodes > 50 or len(body) > 8000
        hint = ("⚠️ 波及面过大" + ("(图侧已截断) " if result.get("truncated") else "")
                + f"({n_nodes} 节点)——文件级视图对 core 模块无鉴别力(谁都连着谁)。"
                "要判断「改这个函数会断谁」:改用 call_chain(symbol=<函数名>, direction=\"callers\") "
                "定点查,或分批传 changed_files。\n") if big else ""
        return (f"{cb_note}blast-radius(codebase={target},输入 {len(changed_files)} 文件 → 波及 "
                f"{n_nodes} 节点 / {n_files} 文件):\n{hint}"
                f"{_honest_truncate(body, 8000, how_to_refetch='要完整波及面:分批传 changed_files(每次几个文件)重调')}")

    # ── ⑤b call_chain:符号中心的 N 跳调用链(仅 CALLS 边 + PageRank;P1.5 caller/callee 进适配层)
    # 和 blast_radius 互补:blast_radius = 文件种子·全边·「改这些会波及谁」(blast);
    # call_chain = 符号种子·仅 CALLS 边·「这个函数的调用上下文 + 谁结构上重要」(chain)。
    # bug-RCA / 调研里 agent 定位根因、判断改动影响时最想要的「调用链」视图;图驱动,零 LLM。
    @_tool("call_chain")
    async def call_chain(symbol: str, direction: str = "both", depth: int = 2,
                         top_n: int = 15, codebase: str | None = None) -> str:
        """Call chain for a function: who calls it / what it calls (N hops along CALL edges only),
        each node ranked by PageRank importance.

        Pass a function/method name (bare like ``wpa_supplicant_init`` or qualified
        ``wpa_supplicant.c::wpa_supplicant_init``). Returns the N-hop caller/callee subtree along CALL
        edges only, each node with file:line, hop count, and a PageRank score (a function called by many
        important functions scores higher). Use it to understand a function's call context when localizing
        a root cause or assessing a change — "how does execution reach here, and which callers matter".

        Complement to blast_radius: blast_radius is file-seed + all-edge "what breaks if I touch these";
        call_chain is symbol-seed + CALLS-only "who calls / is called by this function, ranked".
        direction: callers (who calls it) / callees (what it calls) / both (default).
        depth:     hop count (default 2, capped at 5 to bound large graphs).
        top_n:     max nodes per direction after sorting (hop asc, pagerank desc); default 15.
        codebase:  override which codebase's graph (default = this server's codebase; near-names
              fuzzy-resolved, list: `rootrecall baseline ls`).
        Needs the codebase graph built; returns a "not built" hint otherwise.
        """
        try:
            from rootrecall.services.code_index.code_graph import CodeGraph
        except Exception as e:  # noqa: BLE001 —— code-review-graph 未装给可操作提示
            return (f"call_chain 不可用:结构图后端未装。装它: `uv sync --extra code-review-graph`\n  ({e})")
        target, cb_note, known = _resolve_active_codebase(codebase or repo)
        if target is None:
            return cb_note
        try:
            cg = CodeGraph.open(target)
            result = cg.call_chain(symbol, direction=direction, depth=depth, top_n=top_n)
        except FileNotFoundError:
            return _graph_missing_msg(target, known)
        except ValueError as e:  # symbol 解析不到 / direction 非法 → 友好串,不抛
            return f"call_chain 没法算({target}, symbol={symbol}): {e}"
        except Exception as e:  # noqa: BLE001
            return f"算调用链失败({target}, symbol={symbol}): {e}"
        import json
        body = json.dumps(result, ensure_ascii=False, default=str)
        return (f"{cb_note}call-chain(codebase={target}, symbol={symbol}, direction={direction}, "
                f"depth={depth}):\n"
                f"{_honest_truncate(body, 8000, how_to_refetch='要完整链:减小 top_n/depth 或换 direction(callers/callees 单向)重调')}")

    # ── ⑤c cross_version_diff:跨版本对比(同仓两 git ref 间;git 为核,图可选富化)────
    # 回答「旧版本(base)→ 新版本(head),我关心的 concern 改了啥 / 修了没」:
    # base..head 提交门 + concern 的 git diff + (有图)触及函数 + git cherry 等价摘要。
    # 确定性事实,零 LLM(「修没修」判断归 agent)。比 blast_radius/call_chain 强:没图也能跑 git 核。
    @_tool("cross_version_diff")
    async def cross_version_diff(base_ref: str, head_ref: str, repo_path: str,
                                 concern_files: list[str] | None = None,
                                 concern_symbols: list[str] | None = None,
                                 top_commits: int = 30,
                                 codebase: str | None = None) -> str:
        """Cross-version diff between two git refs of the same repo: what changed base..head,
        especially around your concern. Returns intervening commits (deterministic gate),
        the concern's git diff (so you can read the fix), optional touched-functions (graph),
        and a git-cherry patch-equivalence summary. Deterministic, no LLM — the 'is it fixed /
        how to port' judgment is yours, using this output + search_codebase + call_chain.

        base_ref/head_ref: two git refs in the SAME repo (e.g. '5.50'/'5.85', or 'HEAD~5'/'HEAD').
        repo_path: absolute path to the repo working tree (cwd for git) — or just a registered
        codebase/repo name (resolved via the repo registry / index manifest). concern_files/symbols:
        scope to these (symbols resolved via the graph if available). top_commits: commit cap.
        codebase: override which codebase's graph is used for enrichment (default = server's;
              near-names fuzzy-resolved, list: `rootrecall baseline ls`).
        Needs only the git repo; graph is optional enrichment (runs git core even without it).
        """
        repo_path = _resolve_repo_path_arg(repo_path)
        from rootrecall.services.code_index.code_graph import CodeGraph
        from rootrecall.services.code_index.code_graph import cross_version_diff as _cvd
        # 图是可选富化:名字解析失败不挡 git 核 —— 保留原名照跑(开不到图自动降级)。
        target, cb_note, _known = _resolve_active_codebase(codebase or repo)
        if target is None:
            target, cb_note = (codebase or repo), ""
        graph = None
        try:
            graph = CodeGraph.open(target)
        except Exception:  # noqa: BLE001 —— FileNotFoundError/ImportError 都降级,不致命
            pass
        try:
            result = _cvd(base_ref, head_ref, repo_path=repo_path, concern_files=concern_files,
                          concern_symbols=concern_symbols, graph=graph, top_commits=top_commits)
        except ValueError as e:  # 坏 ref / 非 git 仓 / repo_path 无效 → 友好串,不抛
            return f"cross_version_diff 没法算(repo={repo_path}, {base_ref}..{head_ref}): {e}"
        except Exception as e:  # noqa: BLE001
            return f"跨版本对比失败(repo={repo_path}, {base_ref}..{head_ref}): {e}"
        import json
        body = json.dumps(result, ensure_ascii=False, default=str)
        return (f"{cb_note}cross-version-diff(repo={repo_path}, codebase={target}, "
                f"{base_ref}..{head_ref}):\n"
                f"{_honest_truncate(body, 8000, how_to_refetch='要完整 diff:传 concern_files/concern_symbols 收窄范围,或减小 top_commits 重调')}")

    # ── ⑤e merge_eval:上游 commit 合入评估(逐 commit 三态:已修/建议合/冲突;git 为核图可选)
    # 维护 fork 时,把上游一段 commit 范围逐个评估「该不该合入」:patch-id 等价(已修,git --cherry-mark)
    # + 能否干净 apply(冲突)+ 触及文件/函数(CRG 可选)。确定性地板;「相不相关」归 agent(CRG 查)。
    # 全程 local-git:上游须先 fetch 进本仓让 ref 可见(agent 跑 git remote add + fetch)。
    @_tool("merge_eval")
    async def merge_eval(upstream_base_ref: str, upstream_head_ref: str, fork_ref: str,
                         repo_path: str, concern_files: list[str] | None = None,
                         max_commits: int = 50, codebase: str | None = None) -> str:
        """Upstream-commit merge evaluation: for each commit in an upstream range, decide whether to
        backport it into the fork. Deterministic per-commit tri-state (no LLM): already_fixed (a git
        patch-id-equivalent commit already exists in the fork, via git --cherry-mark), recommend_merge
        (not in fork, applies cleanly), conflict (not in fork, apply fails), uncertain. Also returns
        touched files/functions. SHORT-CIRCUITS with a guidance note when fork and upstream share NO
        merge-base (squashed/independent lineage): both patch-id and merge-tree floors are unusable
        there — evaluate each commit semantically instead (backport-style: read the diff, check the
        fork's code for the same bug).

        This is the deterministic FLOOR — the 'is the fork actually affected / does it need this fix'
        relevance judgment is YOURS, using touched files/functions + search_codebase + call_chain
        ('can apply' != 'fork needs it'; a fork may lack the bug/feature entirely).

        Fully local-git. YOU must first fetch the upstream into the repo so the refs resolve:
        `git -C <repo> remote add upstream <url> && git -C <repo> fetch upstream --no-tags` (idempotent;
        your job, not this tool's). Conflict check is zero-touch (`git merge-tree --write-tree`, git
        2.38+): no checkout of fork_ref and no clean worktree needed — it merges in the object db.
        Only on git < 2.38 it falls back to `git apply --check` against the CURRENT worktree (then do
        checkout fork_ref + clean tree first; the fallback is flagged in the note).

        upstream_base_ref/upstream_head_ref: upstream commit range (two git refs in repo_path, e.g.
            last-sync-point and upstream/master). fork_ref: fork branch to compare against (e.g. release/eagle).
        repo_path: absolute path of the repo working tree (cwd for git) — or a registered
            codebase/repo name (resolved via the repo registry / index manifest). concern_files: scope
            to commits touching these files. max_commits: scan cap (default 50). codebase: graph for
            touched-function enrichment (optional; default = this server's codebase).
        Needs only the git repo; graph is optional enrichment (runs without it).
        """
        repo_path = _resolve_repo_path_arg(repo_path)
        from rootrecall.services.code_index.code_graph import CodeGraph
        from rootrecall.services.code_index.code_graph import merge_eval as _me
        # 图是可选富化:名字解析失败不挡 git 核 —— 保留原名照跑(同 cross_version_diff)。
        target, cb_note, _known = _resolve_active_codebase(codebase or repo)
        if target is None:
            target, cb_note = (codebase or repo), ""
        graph = None
        try:
            graph = CodeGraph.open(target)
        except Exception:  # noqa: BLE001 —— FileNotFoundError/ImportError 都降级,不致命
            pass
        try:
            result = _me(upstream_base_ref, upstream_head_ref, fork_ref=fork_ref, repo_path=repo_path,
                         concern_files=concern_files, max_commits=max_commits, graph=graph)
        except ValueError as e:  # 坏 ref / 非 git 仓 / repo_path 无效 → 友好串,不抛
            return (f"merge_eval 没法算(repo={repo_path}, fork={fork_ref}, "
                    f"{upstream_base_ref}..{upstream_head_ref}): {e}")
        except Exception as e:  # noqa: BLE001
            return (f"合入评估失败(repo={repo_path}, fork={fork_ref}, "
                    f"{upstream_base_ref}..{upstream_head_ref}): {e}")
        import json
        body = json.dumps(result, ensure_ascii=False, default=str)
        s = result.get("summary", {})
        note = result.get("note") or ""
        # note 提到正文首行(无共同祖先短路时整条价值就是这句指引,埋进 JSON 等于没说)
        note_line = (note + "\n") if note else ""
        return (f"{cb_note}merge-eval(repo={repo_path}, fork={fork_ref}, codebase={target}, "
                f"{upstream_base_ref}..{upstream_head_ref}): "
                f"total={s.get('total', 0)} | already_fixed={s.get('already_fixed', 0)} "
                f"| recommend_merge={s.get('recommend_merge', 0)} | conflict={s.get('conflict', 0)} "
                f"| uncertain={s.get('uncertain', 0)}\n"
                f"{note_line}"
                f"{_honest_truncate(body, 8000, how_to_refetch='要逐 commit 详情:传 concern_files 或缩小 ref 范围分批重调')}")

    # ── ⑤f when_introduced:bug 引入 commit 定位(SZZ 式;🟡#7,第 16 个 MCP 工具)──
    # 纯 git(零 LLM、零图依赖)——pickaxe(-S)或行历史(-L)出候选表,哪条真引入缺陷
    # 归 agent 语义裁决(确定性地板 + LLM 天花板,与 merge_eval 同分工)。
    @_tool("when_introduced")
    async def when_introduced(repo_path: str, symbol: str | None = None,
                              file: str | None = None, line: int | None = None,
                              line_end: int | None = None, max_commits: int = 20) -> str:
        """Find which commits introduced a bug's defective logic (SZZ-style), anchored at a
        symbol or file:line from your root-cause analysis. Deterministic candidate list, no LLM.

        Two anchor modes (pick ONE):
        - symbol → `git log -S <symbol>` pickaxe: commits whose diff ADDED/REMOVED that string.
          The introducing commit is usually the OLDEST one with added>0, removed==0; paired
          added/removed in between are usually refactors/moves, not introductions.
        - file + line (optional line_end) → `git log -L` line history: commits that touched that
          line range (rename-following, so line drift is handled). Line numbers = CURRENT worktree.

        Returns candidates newest-first: sha / date / author / subject / added / removed counts.
        Picking which candidate ACTUALLY introduced the defect (vs moved existing code) is YOUR
        semantic judgment — `git show <sha>` each: the introducing commit's message/diff often
        reveals the root cause's intent (a useful cross-check for your hypothesis).

        repo_path: absolute path of the repo working tree — or a registered codebase/repo name
            (resolved via the repo registry / index manifest). file: with symbol = pathspec narrowing
            (short symbols like "scan" hit a lot — always narrow); with line = REQUIRED
            repo-relative path. max_commits: candidate cap (default 20; oldest-introducer may be
            beyond cap — raise it and re-call). Searches the current checkout only (no --all).
        """
        repo_path = _resolve_repo_path_arg(repo_path)
        from rootrecall.services.code_index.code_graph import when_introduced as _wi
        try:
            result = _wi(repo_path, symbol=symbol, file=file, line=line,
                         line_end=line_end, max_commits=max_commits)
        except ValueError as e:  # 坏锚点 / 非 git 仓 / 文件不存在 → 友好串,不抛
            return f"when_introduced 没法算(repo={repo_path}): {e}"
        except Exception as e:  # noqa: BLE001
            return f"引入 commit 定位失败(repo={repo_path}): {e}"
        import json
        body = json.dumps(result, ensure_ascii=False, default=str)
        anchor = result.get("anchor", {})
        a = (f"symbol={anchor.get('symbol')}" if anchor.get("symbol")
             else f"{anchor.get('file')}:{anchor.get('line')}"
             f"{'-' + str(anchor['line_end']) if anchor.get('line_end') != anchor.get('line') else ''}")
        return (f"when-introduced(repo={repo_path}, mode={result.get('mode')}, {a}):"
                f" {len(result.get('commits', []))} candidates (newest-first)\n"
                f"{_honest_truncate(body, 8000, how_to_refetch='要更全的候选:加大 max_commits 重调;要更准:配 file 收窄 symbol 或改用 file+line 锚点')}")

    # ── ⑤d repo_map:PageRank 排名的全仓符号地图(Aider repomap 式;#38)──────────
    # 和 call_chain 互补:call_chain = 一个符号的调用上下文(手电筒照一条路);
    # repo_map = 全仓最重要符号俯瞰图(卫星图),委托前给 agent 全局视角 / 调研「关键模块」骨架。
    @_tool("repo_map")
    async def repo_map(map_tokens: int = 2048, codebase: str | None = None,
                       exclude_tests: bool = True, compact: bool = False) -> str:
        """Whole-repo symbol map ranked by PageRank importance (Aider-style repo map), packed into a token budget.

        Returns a bird's-eye view of which functions are structurally most central across the WHOLE repo
        (not one symbol's neighborhood — that's call_chain). Runs PageRank over the full call graph: a
        function called by many important functions ranks higher (= a core hub). Top symbols are greedily
        packed into ``map_tokens`` (default 2048), grouped by file into a tree. Use it to give yourself a
        global view before localizing a root cause, or as the 'key modules' skeleton for a research report.

        Complement to call_chain: call_chain is one symbol's call context (flashlight down one path);
        repo_map is the whole-repo importance overview (satellite map). Also distinct from hub_nodes
        (degree-based top-15 flat list) — repo_map is PageRank (centrality) based, larger, and tree-grouped.
        map_tokens:     token budget for the map (default 2048).
        exclude_tests:  drop test/emulator/generated-file symbols from the map (default True — bluez field
                  report: unfiltered PageRank had *-tester files crowding out real core modules;
                  pass False only when you deliberately want the test infra layout).
        compact:        return just the map tree + top-10 names instead of the full JSON body — roughly
                  halves the output for big repos when you only need the bird's-eye view.
        codebase:       override which codebase's graph (default = this server's codebase; near-names
              fuzzy-resolved, list: `rootrecall baseline ls`).
        Needs the codebase graph built; returns a 'not built' hint otherwise.
        """
        try:
            from rootrecall.services.code_index.code_graph import CodeGraph
        except Exception as e:  # noqa: BLE001 —— code-review-graph 未装给可操作提示
            return (f"repo_map 不可用:结构图后端未装。装它: `uv sync --extra code-review-graph`\n  ({e})")
        target, cb_note, known = _resolve_active_codebase(codebase or repo)
        if target is None:
            return cb_note
        try:
            cg = CodeGraph.open(target)
            result = cg.repo_map(map_tokens=map_tokens, exclude_tests=exclude_tests)
        except FileNotFoundError:
            return _graph_missing_msg(target, known)
        except Exception as e:  # noqa: BLE001
            return f"算仓库地图失败({target}): {e}"
        # compact:只要树 + top-10 名单(大仓省一半输出;JSON 全量给程序化消费才需要)
        excl_note = result.get("note") or ""
        if compact:
            top10 = ", ".join(t.get("qualified_name", "").split("::")[-1]
                              for t in result.get("top_symbols", [])[:10])
            tree = result.get("map_text", "")
            return (f"{cb_note}repo-map(codebase={target}, map_tokens={map_tokens}, "
                    f"exclude_tests={exclude_tests}, compact):"
                    f" {result.get('n_symbols', 0)} symbols / {result.get('n_files', 0)} files"
                    f"{' (truncated by budget)' if result.get('truncated') else ''}"
                    f"{('  ' + excl_note) if excl_note else ''}\n"
                    f"{_honest_truncate(tree, 6000, how_to_refetch='要更小的地图:减小 map_tokens 重调')}"
                    + (f"\ntop-10: {top10}" if top10 else ""))
        import json
        body = json.dumps(result, ensure_ascii=False, default=str)
        return (f"{cb_note}repo-map(codebase={target}, map_tokens={map_tokens}, exclude_tests={exclude_tests}):"
                f" {result.get('n_symbols', 0)} symbols / {result.get('n_files', 0)} files"
                f"{' (truncated by budget)' if result.get('truncated') else ''}\n"
                f"{_honest_truncate(body, 8000, how_to_refetch='要更小的地图:减小 map_tokens 重调(top_symbols 字段已含前 10 名摘要)')}")

    # ── ⑤e repo_overview:单仓架构总览(社区/模块边界 + hub/bridge 节点 + 耦合告警)──
    # onboarding 导览 skill 的主数据源。三个工具三种俯瞰,互补不打架:
    #   repo_overview = 卫星图看「城市怎么分区」(社区/模块边界 + 哪个路口是枢纽 hub
    #                   + 哪个是咽喉 bridge + 哪两区耦合太紧该报警);
    #   repo_map      = 看全城「最重要的 50 家店」(PageRank 排名符号);
    #   call_chain    = 手电筒照一条路(一个符号的调用上下文)。
    # 全是纯图查询(无 LLM),图驱动防幻觉 —— 讲「这仓分几大模块」靠社区检测,不是模型瞎编。
    @_tool("repo_overview")
    async def repo_overview(
        top_n: int = 15, max_communities: int = 30, codebase: str | None = None,
        exclude_tests: bool = True,
    ) -> str:
        """Single-repo architectural overview: module boundaries + hub/bridge nodes + coupling warnings.

        Wraps four CodeGraph methods in one call (pure graph queries, no LLM): communities (Leiden
        module boundaries), hub_nodes (highest in+out degree — most-depended-on cores), bridge_nodes
        (highest betweenness — architectural chokepoints), and architecture_overview (cross-community
        coupling edges + high-coupling >10-edge warnings). Returns them as sections of one dict so a
        newcomer-tour agent gets the whole structural snapshot in one tool call.

        Use this to answer "what does this codebase look like architecturally" / "what are the core
        modules and hubs" — the phase-1 structural view of an onboarding tour. Distinct from repo_map
        (PageRank symbol tree, which functions matter) and call_chain (one symbol's neighborhood):
        repo_overview is the module-coupling / community-layout view (how the modules are divided).

        Output ordering matters: hubs / bridges / warnings / cross-edges come FIRST, communities LAST.
        Communities are the bulkiest (each carries its member list) and the least individually
        important, so they sit at the truncation edge. On a large repo (hundreds of communities) only
        the largest `max_communities` are summarized (size + a few sample members, not the full member
        list) so the hubs/bridges that onboarding actually needs never get crowded out.
        top_n:           how many hub_nodes / bridge_nodes to return (default 15 each).
        max_communities: cap on how many communities to include, largest-first (default 30).
                         The header still reports the true total community count.
        exclude_tests:   drop test/emulator/generated-file nodes from hubs/bridges (default True —
                         bluez field report: mgmt-tester[474 in-edges] and ltmain.sh[degree 1475]
                         crowded out every real core; pass False only when you want the raw layout).
        codebase:        override which codebase's graph (default = this server's codebase;
                         near-names fuzzy-resolved, list: `rootrecall baseline ls`).
        Needs the codebase graph built; returns a 'not built' hint otherwise.
        """
        try:
            from rootrecall.services.code_index.code_graph import CodeGraph
        except Exception as e:  # noqa: BLE001 —— code-review-graph 未装给可操作提示
            return (f"repo_overview 不可用:结构图后端未装。装它: `uv sync --extra code-review-graph`\n  ({e})")
        target, cb_note, known = _resolve_active_codebase(codebase or repo)
        if target is None:
            return cb_note
        try:
            cg = CodeGraph.open(target)
            arch = cg.architecture_overview()         # {communities, cross_community_edges, warnings}
            # communities 复用 arch 已经取好的(architecture_overview 内部已调 get_communities,省一次调用)
            communities = arch.get("communities", [])
            hubs = cg.hub_nodes(top_n=top_n, exclude_tests=exclude_tests)          # 被依赖最多的核心枢纽
            bridges = cg.bridge_nodes(top_n=top_n, exclude_tests=exclude_tests)    # 架构瓶颈/咽喉(betweenness 最高)
        except FileNotFoundError:
            return _graph_missing_msg(target, known)
        except Exception as e:  # noqa: BLE001
            return f"算仓库架构总览失败({target}): {e}"
        import json

        n_total_comm = len(communities)   # 真实社区总数(不受 max_communities 影响,header 诚实报)
        # —— 大仓瘦身:社区是最 bulky 的(每个带 member 列表),也是单条最不重要的。
        # 只留最大的 max_communities 个,且把 members 压成 count + 几个样本(不堆全量 qn)。
        communities_sorted = sorted(
            communities, key=lambda c: c.get("size", len(c.get("members", []))), reverse=True
        )
        comm_capped = communities_sorted[:max_communities]
        communities_trimmed = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "level": c.get("level"),
                "cohesion": c.get("cohesion"),
                "size": c.get("size", len(c.get("members", []))),
                "dominant_language": c.get("dominant_language", ""),
                "description": c.get("description", ""),
                # 不堆全量 member(大社区几十上百个 qn 撑爆截断)→ 只给 count + 前 5 个样本。
                "member_count": len(c.get("members", [])),
                "sample_members": c.get("members", [])[:5],
            }
            for c in comm_capped
        ]
        # 跨社区边也可能上百上千条 → 截 top 20(按出现顺序,CRG 已统计);warnings 本就少不动。
        cross_edges = arch.get("cross_community_edges", [])[:20]
        warnings = arch.get("warnings", [])          # 高耦合(>10 边)社区对,list[str]

        # —— body 顺序:hub/bridge/warning/cross-edge 在前,communities 在末。
        # 这样即便末尾 communities 被截断,架构最关键的「核心枢纽/咽喉/耦合告警」也不会丢
        # (onboarding e2e 暴露:旧版 communities 撑满截断 → hub/bridge 取不到被迫绕路)。
        result = {
            "codebase": target,
            "hub_nodes": hubs,
            "bridge_nodes": bridges,
            "warnings": warnings,
            "cross_community_edges": cross_edges,
            "communities": communities_trimmed,
        }
        body = json.dumps(result, ensure_ascii=False, default=str)

        # 诚实截断(治静默丢):超长才截,且明说截在哪、怎么补取。
        LIMIT = 12000
        truncated = len(body) > LIMIT
        if truncated:
            # 末尾是 communities,截断丢的是社区清单(最不重要),给出补取路径。
            # 没有 communities 专用 MCP 工具 —— 指向调大 max_communities 重取 repo_overview。
            note = (f"\n[截断:返回超 {LIMIT} 字符,末尾 communities 可能不全;共 {n_total_comm} 社区,"
                    f"本调用含 {len(communities_trimmed)}。要更多社区:重调 repo_overview 加大 max_communities]")
            body = body[:LIMIT - len(note)] + note

        comm_suffix = f"(本调用含 {len(communities_trimmed)} / 共 {n_total_comm})" if n_total_comm > len(communities_trimmed) else ""
        return (f"{cb_note}repo-overview(codebase={target}, top_n={top_n}):"
                f" {n_total_comm} communities{(' ' + comm_suffix) if comm_suffix else ''}"
                f" / {len(hubs)} hubs / {len(bridges)} bridges"
                f"{f' / {len(warnings)} 高耦合告警' if warnings else ''}\n{body}")

    # ── ⑥ validate_patch:补丁能否干净 apply(执行硬门,零 LLM)────────────────
    # harness 转向:把 validate_patch 暴露成工具,agent 改完/拿到 PR diff 后过这道硬门再信。
    @_tool("validate_patch")
    async def validate_patch(patch: str, repo_path: str, worktree: bool = False) -> str:
        """Execution gate (non-LLM): does this unified-diff patch apply cleanly to the repo working tree?

        Runs `git apply --check` forward (strict → --3way → patch -p1 fallback) — a deterministic hard
        gate before trusting a patch. Returns applies + method + git diagnostic. Use it to confirm a
        patch/PR you're about to merge, or a fix you just wrote, actually fits the target repo.
        repo_path: absolute path of the repo working tree to check against — or a registered
        codebase/repo name (resolved via the repo registry / index manifest).

        worktree=True (2026-08-26): validate the repo's CURRENT UNCOMMITTED CHANGES instead of a
        supplied patch — captures ``git diff HEAD`` and reverse-``--check``s it against the working
        tree. The tree already holds your edits, so forward is meaningless there (context moved);
        reverse pass = the diff faithfully matches the tree state AND can be cleanly reverted. Call
        this right after editing, before export_patch; ``patch:`` is ignored in this mode. Known
        bounds (flagged in output): untracked NEW files aren't in ``git diff HEAD``; debian ``.pc/``
        build artifacts are noise (export_patch strips them, this only warns).
        """
        import subprocess
        from pathlib import Path

        from rootrecall.services.workspace.validate import validate_patch as _validate

        repo_path = _resolve_repo_path_arg(repo_path)
        if not Path(repo_path).is_dir():
            return f"repo_path 不是目录: {repo_path}"

        if worktree:
            def _git(args: list[str]) -> str:
                p = subprocess.run(["git", "-C", repo_path, *args],
                                   capture_output=True, text=True, timeout=60)
                if p.returncode != 0:
                    raise RuntimeError((p.stderr or p.stdout or "").strip()[-300:])
                return p.stdout

            try:
                diff = _git(["diff", "HEAD", "--no-color"])
            except (OSError, subprocess.SubprocessError, RuntimeError) as e:  # noqa: BLE001
                return f"worktree 验证执行失败(git 不可用/非 git 仓?): {e}"
            if not diff.strip():
                return ("❌ git diff HEAD 为空:工作树没有已跟踪文件的改动。要么你还没改,要么改的是"
                        "未跟踪新文件(git diff HEAD 不含 untracked),要么 repo_path 指错了树。")
            warns = []
            if any(ln.startswith("?? ") for ln in _git(["status", "--porcelain"]).splitlines()):
                warns.append("检出未跟踪新文件(不在本次验证的 diff 里;新文件本身无 context 可验)")
            if "diff --git a/.pc/" in diff:
                warns.append("diff 含 .pc/ 构建产物(debian quilt 垃圾;export_patch 会剔除,这里只提醒)")
            try:
                r = _validate(diff, None, reverse_dir=repo_path)  # forward 跳过:树已含改动,reverse 才有效
            except Exception as e:  # noqa: BLE001
                return f"worktree 验证执行失败: {e}"
            revert_ok = r.get("revert_ok")
            n = len(diff.splitlines())
            flag = ("✅ 工作树改动自洽(reverse --check 通过:diff 与树状态一致、可干净撤回)"
                    if revert_ok else "❌ reverse --check 失败:diff 与工作树实际状态对不上(改动没保存?半途手改?)")
            warn_line = ("\n⚠️ " + ";".join(warns)) if warns else ""
            log = (r.get("log") or "").strip()[-400:]
            return f"{flag}\nmode=worktree  diff={n} 行(git diff HEAD,含已暂存){warn_line}\n诊断:\n{log}"

        try:
            r = _validate(patch, forward_dir=repo_path)  # reverse_dir=None:forward 模式只 forward --check
        except Exception as e:  # noqa: BLE001
            return f"validate_patch 执行失败: {e}"
        applies = bool(r.get("verified"))
        method = r.get("forward_method")
        log = (r.get("log") or "").strip()[-600:]
        flag = "✅ 能干净 apply" if applies else "❌ apply 失败(路径/格式/context 不匹配)"
        return f"{flag}\nmethod={method}  applies={applies}\n诊断:\n{log}"

    # ── ⑦ export_patch:把补丁落盘成 .patch 文件(交付硬门 —— 聊天回复不算交付)────────
    # bug-RCA 跑完,agent 的改动若只在聊天里 = 没交付。这步把 git diff 写成磁盘文件,且自检
    # 触发时机是**用户开口**(要补丁/要去真机验)—— 迭代中间版不自动落盘(2026-08-19 措辞对齐:
    # 交付由用户触发,工具不是流水线步骤;落盘纪律靠 docstring + SKILL 双处声明)。
    # 非空(治"agent 改错树 / 假装改完"——纯 bash `git diff > file` 会静默吞掉空 diff,2026 调研:
    # deer-flow 用结构化 present_files tool + 事后交付验证,正是治这个)。格式 unified diff(git diff),
    # 对齐整条管线(validate 用 git apply / ingest 解析 unified diff / report 渲染 ```diff);不污染 repo
    # (无需建 commit —— format-patch 留生产级迭代)。落 data/bug_rca/<repo>.patch(最新一份快照,
    # 同 bug_rca workflow 约定;同仓重跑覆盖,历史在记忆库)。
    # apply 验证**不在这做** —— forward --check 对"已改过的树"必失败(见 validate.py:context 已变,
    # 反向 --check 才证必要);那是 validate_patch(第⑥步,对干净树)的活。export 只保证"有非空 diff 落盘"。
    @_tool("export_patch")
    async def export_patch(repo_path: str, out_dir: str = "data/bug_rca") -> str:
        """Finalize your fix as an on-disk .patch file — USER-TRIGGERED deliverable, not an iteration step.

        Call it ONLY when the user asks for the patch ("生成补丁" / wants to take it to a real
        machine for testing) — do NOT auto-export intermediate versions while iterating; chat-level
        validate feedback is enough mid-loop. Captures ALL your uncommitted changes in repo_path
        (``git add -A && git diff --cached``,
        including new files), writes the unified diff to ``<out_dir>/<repo-name>.patch``, and REFUSES
        to write an empty diff — catches "edited the wrong tree / changes not saved / gitignored",
        failures a bare ``git diff > file`` silently swallows. Run ``validate_patch`` first to confirm
        the diff applies; this tool only guarantees a non-empty patch lands on disk at the canonical path.

        repo_path: absolute path of the repo whose working tree holds your fix — or a registered
                   codebase/repo name (resolved via the repo registry / index manifest).
        out_dir:   output directory (default ``data/bug_rca`` = "latest snapshot" location, matching
                   the bug_rca workflow convention; created if missing).
        """
        import subprocess
        from pathlib import Path

        raw_repo_arg = repo_path  # 归档按 bug_id 要用「名字/原始路径」查注册表(解析前留底)
        repo_path = _resolve_repo_path_arg(repo_path)
        repo = Path(repo_path)
        if not repo.is_dir():
            return f"repo_path 不是目录: {repo_path}"

        def _git(args: list[str]) -> tuple[int, str, str]:
            p = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True, timeout=60,
            )
            return p.returncode, p.stdout, p.stderr

        try:
            rc, _, err = _git(["rev-parse", "--is-inside-work-tree"])
            if rc != 0:
                return f"repo_path 不是 git 工作树: {(err or '').strip()[-300:]}"
            # git add -A 再 diff --cached:含新增文件(对齐 bug_rca workflow 的 observe 约定)。
            # 副作用:会 stage repo_path 的改动(可 git reset 撤;agent 已在改其工作树,同量级)。
            _git(["add", "-A"])
            # quilt/dpkg-source 的 .pc/ 是 debian 源码仓工作树的构建产物,不是修复内容 ——
            # 混进补丁会把 26 行修复膨胀成 30 万行垃圾(bluez v20 e2e 实测),排除之。
            _git(["reset", "-q", "--", ".pc"])
            rc, diff, err = _git(["diff", "--cached"])
            if rc != 0:
                return f"git diff 失败: {(err or diff).strip()[-300:]}"
        except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001 —— git 不可用给可操作错误
            return f"export_patch 执行失败(git 不可用?): {e}"

        if not diff.strip():
            return ("❌ 空 diff:git 看不到你的改动。可能改错了树(repo_path 指错)、改动没保存、"
                    "或被 .gitignore 忽略。export_patch 不写空补丁 —— 回去确认你真的改对了文件。")

        # 名字输入用注册名、路径输入用目录名(前者对齐注册/索引/gc 命名,bug_id 归档可追溯)
        repo_name = raw_repo_arg if "/" not in raw_repo_arg else repo.name
        from rootrecall.services.repos.registry import reanchor_data_path

        out = reanchor_data_path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        patch_path = out / f"{repo_name}.patch"
        patch_path.write_text(diff, encoding="utf-8")
        n = len(diff.splitlines())
        archived = _archive_bug_copy(patch_path, raw_repo_arg)
        return (f"✅ 已落盘\npath={patch_path}\nlines={n}  (unified diff;apply 验证见 validate_patch)"
                + (f"\n归档:{archived}(按 bug_id,gc 回收仓后仍可追溯)" if archived else ""))

    # ── ⑨ export_report:把分析报告落盘成 .md 文件(交付硬门 —— 报告跟补丁一样要上盘)────
    # 跟 export_patch 对称:补丁内容是 git 生成的(工具自己 diff),报告内容是 agent 生成的(传 content)。
    # 触发时机同 export_patch:用户开口要报告才调,迭代中不自动写(见第⑦步注释)。
    # bug-RCA 跑完,agent 若只在聊天里吐报告 = 没交付(跟"只在聊天里说改好了"同理)。这步把报告写成磁盘文件,
    # 自检非空(治"agent 假装写报告 / 传空串糊弄")。落 data/bug_rca/<repo>-rca.md(对齐 orchestrator 的
    # render_report 约定;同仓重跑覆盖,历史在记忆库)。报告是**最终交付物**,排在 memorize 之后写 ——
    # 要含 memorize 返回的 id(证明教训已沉淀),才算完整闭环。
    @_tool("export_report")
    async def export_report(content: str, repo_path: str, out_dir: str = "data/bug_rca",
                            agents_md: bool = False, topic: str | None = None) -> str:
        """Finalize your analysis report as an on-disk .md file — USER-TRIGGERED deliverable (same
        bar as the patch; call it when the user asks for the report, typically after user-confirmed
        real-machine verification — do NOT auto-write it mid-iteration).

        Writes your markdown report to ``<out_dir>/<repo-name>[-<topic>]-rca.md`` and REFUSES
        empty/trivial content — catches "forgot to write a report / passed a placeholder". Write
        the patch first (``export_patch``, step ⑦) AND memorize the lesson (``memorize``, step ⑧)
        first, then write this report so it can cite the on-disk ``.patch`` path and the returned
        memorize id.

        content:   the full markdown report (root cause + evidence + patch summary + validate result +
                   patch path + memorize id).
        repo_path: absolute path of the repo (used only to derive the report filename).
        out_dir:   output directory (default ``data/bug_rca``; created if missing).
        topic:     short slug distinguishing THIS report's topic (e.g. ``connect-flow-compare``,
                   ``a2dp-protocol``, ``bug-1234``) — one repo usually produces MULTIPLE reports
                   (compare / domain-research / different bugs) and the bare ``<repo>-rca.md``
                   name made them overwrite each other (2026-08-26 field report: an A2DP report
                   silently replaced a connection-flow compare report). Omit = legacy filename.
        agents_md: ALSO write an AGENTS.md next to the report (``<repo_path>/AGENTS.md`` — INTO the
                   repo root). AGENTS.md is the agent-facing README convention (agents.md; opencode /
                   claude code / cursor read it natively) — onboarding/research findings become context
                   that ANY agent auto-loads next time it works in that repo. OPT-IN (default off —
                   never write files into the user's repo unasked); the skill passes it when the user
                   asked for it. Content is derived FROM your report: architecture overview + key
                   entry points + naming conventions + known pitfalls. Keep it LEAN (ETH Zurich 2026:
                   verbose AGENTS.md slows agents down) — a distilled digest, not a copy: ≤60 lines,
                   no evidence tables / no per-step narration / no report-only sections.
        """
        import re
        from pathlib import Path

        if not content or not content.strip():
            return ("❌ 空报告:没传内容(或只传空白)。报告跟补丁一样是交付物 —— 写好根因/证据/补丁要点/"
                    "validate 结果/patch 路径/memorize id 再调。export_report 不写空报告。")
        # 占位拦截(T24,2026-08-26 实测:「先落盘占位、内容回头补」的 TBD 模板非空、旧守卫
        # 拦不住,真落了盘):过短或占位标记密度高 → 拒。用户急着要「先有个文件」也不行 ——
        # 占位报告混进 data/bug_rca 会污染后续 ingest/归档。
        ph = _placeholder_reason(content)
        if ph:
            return (f"❌ 疑似占位报告({ph}):报告是交付物,要根因/证据/补丁要点/validate 结果/"
                    f"patch 路径/memorize id 的实质内容。「先落盘占位、内容回头补」不是报告 ——"
                    f"分析完成后再来调。")
        # 报告落盘不强依赖 git / repo 目录存在(内容自包含),只取 repo_path 的目录名做文件名;
        # 空路径兜底 "report",绝不因 repo_path 小瑕疵挡住报告上盘(交付物宁可落盘)。
        name = Path(repo_path).name if repo_path and repo_path.strip() else ""
        repo_name = name or "report"
        # 主题后缀(2026-08-26 实测:固定 <repo>-rca.md 让同仓多主题报告互相覆盖)。topic 缺省
        # 保持旧名(向后兼容);给了就 <repo>-<topic>-rca.md,空白/斜杠归一成连字符、截 48 字符。
        slug = ""
        if topic and topic.strip():
            slug = re.sub(r"[\s/\\]+", "-", topic.strip()).strip("-")[:48]
        fname = f"{repo_name}-{slug}-rca.md" if slug else f"{repo_name}-rca.md"
        from rootrecall.services.repos.registry import reanchor_data_path

        out = reanchor_data_path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        report_path = out / fname
        existed = report_path.exists()
        report_path.write_text(content, encoding="utf-8")
        archived = _archive_bug_copy(report_path, repo_path)
        n = len(content.splitlines())
        msg = f"✅ 已落盘\npath={report_path}\nlines={n}  (markdown 报告)"
        if existed:
            msg += "\n⚠️ 已覆盖同名文件(同主题重跑属正常幂等;这是不同主题的报告就传 topic 区分文件名)"
        if archived:
            msg += f"\n归档:{archived}(按 bug_id,gc 回收仓后仍可追溯)"
        # AGENTS.md 产出(#5,2026-08-17):报告同源数据蒸馏成「给 agent 看的 README」写进仓根。
        # 默认关 —— 不问自写入用户仓违背最小惊讶;skill 按用户显式要求才传 agents_md=True。
        # 不覆盖已有内容:仓里已有 AGENTS.md(手写或别的工具产)→ 拒写 + 提示,保护用户文件。
        if agents_md:
            target = Path(repo_path) / "AGENTS.md"
            if target.exists():
                return (msg + f"\n⚠️ AGENTS.md 未写:{target} 已存在(手写/其他工具产物,不覆盖;"
                        "要更新先人工确认再删它重跑)。")
            target.write_text(f"# AGENTS.md\n\n<!-- 由 RootRecall export_report 生成(蒸馏自同目录导览/调研报告);"
                              f"agent-facing README,保持精简。 -->\n\n{content}", encoding="utf-8")
            msg += f"\nAGENTS.md 已写:{target}({len(content.splitlines())} 行,蒸馏版)"
        return msg

    # ── ⑩ fetch_patch:PR 链接 → diff + meta(P-A 1a,取快递)────────────────────
    # 给一个 GitHub PR 链接,抓回 diff + title/body/changed_files/merge_commit_sha。opencode 能 curl,
    # 但这里带 token 鉴权(私有/限速)+ 失败重试 + 结构化拆包(踩坑#2 辩护:agent 通用 curl 不知 token/remotes)。
    @_tool("fetch_patch")
    async def fetch_patch(url: str) -> str:
        """Fetch a GitHub PR's diff + metadata (title/body/changed_files/merge_commit_sha).

        Give a PR URL (github.com/<owner>/<repo>/pull/<num>). Returns the unified diff plus PR metadata
        so you can then ``validate_patch`` / assess it. Uses GITHUB_TOKEN if set
        (private repos / rate limits). Network errors / 404 / non-GitHub URL → friendly error string.
        """
        from rootrecall.services.patch.fetcher import from_config

        try:
            art = await from_config().fetch(url)
        except Exception as e:  # noqa: BLE001 - 网络错/404/非 GitHub URL 给可操作串,不崩整个调用
            return f"fetch_patch 失败({url}): {e}"
        meta = (f"title: {art.title}\nmerge_commit_sha: {art.merge_commit_sha}\n"
                f"changed_files({len(art.changed_files)}): {', '.join(art.changed_files[:20])}")
        if art.body:
            meta += f"\nbody: {art.body[:500]}"
        return f"source={art.source_kind}  url={art.url}\n{meta}\n\n--- diff ---\n{art.diff}"

    # ── ⑪ ensure_repo:本地没有 → auto-clone(P-A 1a,借样机)────────────────────
    # 鉴定要一台"样机"(代码仓)。本地没有 → 按 config.patch.git.remotes 配的地址 clone。
    # 踩坑#2 辩护:opencode 会 git clone,但只去公网;用户的"自定义 git 连接"(内网镜像/SSH)它不知道。
    @_tool("ensure_repo")
    async def ensure_repo(name_or_url: str) -> str:
        """Resolve a codebase to a local path, auto-cloning if missing.

        Give a repo name, a git URL, or an existing local path. Resolution order: repo registry
        (``data/repos.yaml`` — registered baselines/bug checkouts hit instantly, no clone) →
        ``config.patch.git.remotes`` → treat as git URL; a fresh clone lands in ``data/repos/<name>``
        and is auto-registered. Returns the local absolute path; idempotent — won't re-clone.
        Use before ``validate_patch`` etc. when the repo isn't already local; note those tools
        now also accept a registered name directly as repo_path.
        """
        from rootrecall.services.repos.resolver import ensure_repo as _ensure

        try:
            path, cloned = _ensure(name_or_url)
        except Exception as e:  # noqa: BLE001 - clone 失败(认证/不存在/网络)给可操作串,不崩
            return f"ensure_repo 失败({name_or_url}): {e}"
        tag = "新 clone" if cloned else "命中本地(未 clone)"
        return f"✅ repo_path={path}  ({tag})"

    # ── ⑫ find_repo:注册表模糊查仓(P0 自然语言→自动开仓的第一环)────────────────
    # 用户话里是「项目+版本」(bluez 5.50.61),工具链要仓/索引 —— 这层解析从「问用户要绝对路径」
    # 挪进注册表:命中给候选(baseline 优先);没命中给基线清单 + 带安装根、bash 可原样跑的自动开仓命令。
    @_tool("find_repo")
    async def find_repo(project: str, version: str | None = None, role: str | None = None) -> str:
        """Find candidate repos in RootRecall's registry by project (+optional version).

        Structured lookup — parse the user's wording yourself first ("bluez 5.50.61" →
        project="bluez", version="5.50.61"). Fuzzy-matches registered names / branches /
        urls (``data/repos.yaml``), baseline first; each candidate lists role / path /
        index name / bug id / on-disk status. Other tools accept a candidate's NAME
        directly as repo_path (registry-resolved) — no absolute paths needed.
        role: optional filter — "baseline" | "ephemeral" | "unmanaged".

        Empty result = that version isn't provisioned yet: the reply lists registered
        baselines and a ready-to-run auto-provision command (``baseline checkout ... --index``:
        worktree from the baseline mirror + seeded incremental index, registered as
        ephemeral) — run it via bash instead of asking the user for paths. No baselines
        either → ask for the git URL, clone it under the codebases root and ``baseline add``.
        """
        from rootrecall.services.repos.registry import RepoRegistry, _install_root

        reg = RepoRegistry()
        hits = reg.find(project, version=version, role=role)
        # find() 的语义:精确命中任一条就不再走 loose 兜底;一条不精确则 loose(忽略版本)全上。
        # 对自动开仓这丢了关键信息 —— 「该版本已开仓」(直接用)vs「只有相近基线」(要开仓)是
        # 两个结论,这里重分类:exact = 版本真配上;related = 另查一次不带版本的项目候选。
        if version:
            v = version.lower()

            def _vmatch(r) -> bool:
                hay = f"{r.name} {r.branch or ''}".lower()
                hay_url = (r.url or "").rstrip("/").rsplit("/", 1)[-1].lower().removesuffix(".git")
                return v in hay or v in hay_url

            exact_hits = [r for r in hits if _vmatch(r)]
            pool = reg.find(project, role=role)  # 不带版本:把 exact 路径排除掉的相近仓捞回来
            exact_names = {r.name for r in exact_hits}
            related = [r for r in pool if r.name not in exact_names]
        else:
            exact_hits, related = hits, []
        desc = (f"project={project!r}"
                + (f" version={version!r}" if version else "")
                + (f" role={role!r}" if role else ""))

        def _render(r) -> str:
            disk = "on-disk" if r.exists_on_disk() else "⚠️ path missing"
            idx = f"  index={r.index_name}" if r.index_name != r.name else ""
            br = f"  @{r.branch}" if r.branch else ""
            bug = f"  bug={r.bug_id}" if r.bug_id else ""
            return f"- {r.name}  [{r.role}]  {r.path or 'no-path'}{idx}{br}{bug}  ({disk})"

        if exact_hits:
            out = [f"Matched {len(exact_hits)} repo(s) for {desc} (baseline first):"]
            out += [_render(r) for r in exact_hits]
            if related:
                out.append(f"Related (project matched, version {version} not in them — 开仓前别用错版本):")
                out += [_render(r) for r in related]
            out.append("候选的 name 直接当 repo_path / codebase 传给其他工具(注册名可解析,无需绝对路径)。")
            return "\n".join(out)

        baselines = [r for r in reg.list() if r.role == "baseline"]
        root = _install_root()
        if not baselines:
            return (f"No repo matched {desc}, and no baseline is registered either.\n"
                    f"Ask the user for the git URL, clone it under the codebases root"
                    f"(env ROOTRECALL_CODEBASES, default ~/codebases), then one command:\n"
                    f"  uv run --no-sync --project {root} rootrecall baseline add <clone路径>\n"
                    f"(登记基线 + 建索引一条龙;默认名=相对总目录路径倒序连 '-',如 v20/bluez → bluez-v20。\n"
                    f"只想要一次性样机不走生命周期,可用 ensure_repo 直接 clone。)")
        tag = version or "<tag或commit>"
        name = f"{project}-{version}" if version else f"{project}-<版本>"
        lines = [f"No repo matched {desc}"
                 + (f"(项目有相近仓,但都没有版本 {version})" if related else "") + ". Registered baselines:"]
        for r in baselines:
            lines.append(f"  - {r.name}  branch={r.branch or '-'}  url={r.url or '-'}")
        lines.append(
            f"Auto-provision (bash,跑之前把 <…> 占位换成实值): uv run --no-sync --project {root} "
            f"rootrecall baseline checkout {name} --from <基线名> --ref {tag} --bug <bug标识> --index\n"
            f"(worktree 秒开 + 播种基线索引增量建,一步就绪;登记 ephemeral,分析完 repo gc 回收)")
        return "\n".join(lines)

    return mcp


def main() -> None:
    """MCP server 入口(stdio 默认)。`rootrecall mcp serve` 或 `python -m rootrecall.tools.mcp_memory` 调。

    http(streamable-http)模式走 CLI `rootrecall mcp serve --transport http`(cmd_mcp 里建带 host/port 的 server)。
    """
    build_server().run()


if __name__ == "__main__":
    main()
