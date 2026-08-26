"""code_index · 结构图服务(R3.2):把 code-review-graph(CRG)包成深度调研用的「结构真相源」。

这一层干什么(面向小白)
------------------------
深度调研要回答"这系统怎么组成的、哪些是核心模块、哪里耦合过紧"。靠 LLM 瞎猜不靠谱,
得有**结构真相**——谁调用谁、谁是被大量依赖的枢纽、模块怎么聚类成社区。CRG(code-review-graph)
就是干这个的:tree-sitter 解析全仓 → SQLite 存"函数/类/调用/继承"图 → Leiden 社区检测 +
hub/bridge 分析。本文件把这套散在 CRG 三四个模块里的 API 包成几个调研直接能调的方法。

为什么自己包一层(而不让调研代码直接调 CRG)
- CRG 的建图分散成 full_build → detect_communities → store_communities 三步,查询又散在
  communities / analysis / graph 三个模块;调研代码不该每次重写这套编排。
- 统一错误处理:CRG 是可选 extra(`uv sync --extra code-review-graph`),没装时给清晰指引
  而不是一坨 import 报错。
- db 落点统一在 data/structgraph/<repo>/graph.db(RootRecall 自管,不污染 repo 目录)。

设计取舍
- **进程内 import**(免 MCP server、免 compile_commands;tree-sitter 即可)—— 与 memory 的
  structural.py 同源(都吃 code_review_graph 库),只是各用不同子模块:本文件用 incremental /
  communities / analysis;structural.py 用 tools.query 的 callers/callees。两者 db 路径目前各自
  独立(本文件走 data/structgraph/,structural 走 CRG 默认 .code-review-graph/),将来 memory
  recall 想复用同一张图再对齐路径(记 backlog)。
- **igraph 是 CRG 的可选 extra**:装了走 Leiden 社区(质量高),没装 CRG 内部静默降级成文件聚类
  (质量次但能用)。本层不强制要求 igraph。
- **非 git 仓也能建图**:full_build 优先 git ls-files,失败则回落到目录遍历(example/demo2/wpa
  不是 git 仓也能用)。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any


def default_graph_root() -> str:
    """默认 base_dir 的规范解析:与 CLI 建图落点同锚(ROOTRECALL_HOME 设置时迁家)。

    "data/structgraph" 字面量是 cwd 相对;ROOTRECALL_HOME 设置时 CLI 建图走 reanchor 迁家,
    而本类 build/update/open 的默认值若仍是 cwd 相对,同机两处找图会分裂(2026-08-25 工具层
    接线时发现)。单点在这里解析:env 未设时返回原串,行为与旧默认完全一致。
    """
    from rootrecall.services.repos.registry import reanchor_data_path

    return str(reanchor_data_path("data/structgraph"))

logger = logging.getLogger(__name__)


_SAFE_GIT_REF = re.compile(r"^[A-Za-z0-9_.~^/@{}\-]+$")  # 防 git ref 注入(同 CRG changes.py)
_GIT_TIMEOUT = int(os.environ.get("ROOTRECALL_GIT_TIMEOUT", "30"))  # 跨版本 git 命令超时(秒)


def _run_git(repo: Path, args: list[str], *, timeout: int = _GIT_TIMEOUT) -> str:
    """跑一条 git 命令(cwd=repo);非 0 退出 → 抛 ValueError(让工具层转友好串,绝不漏 traceback 给 agent)。

    面向小白:就是「在 repo 目录里跑一条 git —— 成了回 stdout,挂了抛 ValueError」的统一入口。
    cross_version_diff / merge_eval 共用(原先各自是闭包,提上来消重复)。
    """
    try:
        r = subprocess.run(["git", *args], capture_output=True, stdin=subprocess.DEVNULL,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=str(repo), timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"git 命令执行失败({' '.join(args[:2])}…): {exc}") from exc
    if r.returncode != 0:
        raise ValueError(f"git 失败(rc={r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


def _require_crg() -> None:
    """CRG 是否已装;没装给清晰指引(用 find_spec 探测,不触发真 import 报错)。"""
    if importlib.util.find_spec("code_review_graph") is None:
        raise ImportError(
            "CRG 结构图服务需要 code-review-graph。装它: uv sync --extra code-review-graph"
        )


def _current_head(repo: Path) -> str | None:
    """当前 git HEAD 的完整 sha;非 git 仓 / 空(无提交)仓 → None(增量没基准,走全量)。"""
    try:
        return _run_git(repo, ["rev-parse", "HEAD"]).strip() or None
    except ValueError:
        return None


def _changed_since(repo: Path, base_sha: str) -> list[str]:
    """自 base_sha 以来动过的文件清单(仓库相对路径,去重排序)。

    两条来源取并集,宁可多报不漏报(CRG incremental_update 内部还有 per-file hash 快筛,
    清单给宽了只是多几次 hash 比对,不会重复解析):
    - ``git diff --name-only <sha>``:工作树 vs 那次提交 —— 覆盖「建图后新提交 + 已暂存 + 未暂存」;
    - ``git ls-files --others``:未跟踪的新文件(git diff 看不见它们)。
    sha 不可解析(rebase 改历史等)→ _run_git 抛 ValueError,由调用方兜底全量重建。
    """
    diff = _run_git(repo, ["diff", "--name-only", "-z", base_sha, "--"]).split("\0")
    untracked = _run_git(repo, ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0")
    return sorted({p for p in (*diff, *untracked) if p})


def _pagerank(graph) -> dict:
    """CALLS 子图上的 PageRank —— 被越多重要函数调用 → 分越高,标识「结构上关键的函数」。

    分层降级取稳健(不强加 scipy 这种重依赖):
      1. 优先 ``nx.pagerank``(networkx 3.x 默认走 scipy 稀疏矩阵,大图快、省内存);
      2. scipy 没装(本机常见)→ 降级 ``_pagerank_python_pure``(自实现纯 python 幂迭代,
         直接吃邻接表、不建稠密矩阵,故**不 OOM**;大图慢但正确,小图瞬间)。
         —— 不再 import networkx 私有的 ``_pagerank_python``(下划线私有 API,静态分析报
         「未知导入符号」,且跨版本无保证),自实现等价算法,行为一致。

    两者都返 ``{node: score}``;无边(空图 / 种子孤立)→ ``{}``(调用方按 0.0 兜底)。
    """
    import networkx as nx

    if graph.number_of_edges() == 0:
        return {}
    try:
        return nx.pagerank(graph)
    except ModuleNotFoundError:
        return _pagerank_python_pure(graph)


def _pagerank_python_pure(graph, *, alpha: float = 0.85, max_iter: int = 200,
                          tol: float = 1.0e-6) -> dict:
    """纯 python PageRank(幂迭代)—— scipy 缺时的降级实现,不建稀疏/稠密矩阵(大图不 OOM)。

    标准 power iteration:dangling 节点(无出边的函数)的 rank 均匀回流给全网,保证
    概率和恒为 1。与 networkx ``pagerank`` 同算法、同语义(返 ``{node: score}``,和为 1),
    仅实现更朴素 —— 大图比 scipy 慢,但无重依赖、不 OOM,小图瞬间。

    参数(都有库默认,通常不用传):
      - ``alpha``:阻尼系数(默认 0.85,业界标准);
      - ``max_iter``:最大迭代轮(默认 200,足够收敛);
      - ``tol``:L1 收敛阈值(默认 1e-6)。
    """
    nodes = list(graph)
    n = len(nodes)
    if n == 0:
        return {}
    outdeg = {v: graph.out_degree(v) for v in nodes}  # 出度(dangling 节点 = 0)
    rank = {v: 1.0 / n for v in nodes}
    for _ in range(max_iter):
        dangling = sum(rank[v] for v in nodes if outdeg[v] == 0)  # dangling 总分,均匀回流全网
        new_rank = {}
        base = (1.0 - alpha) / n + alpha * dangling / n  # 每个节点的基础分(随机跳转 + dangling 回流)
        for v in nodes:
            s = base
            for u in graph.predecessors(v):  # u→v:u 把 rank/outdeg[u] 分给 v
                s += alpha * rank[u] / outdeg[u]
            new_rank[v] = s
        # L1 收敛:总分差 < tol 就停(不跑满 max_iter)
        if sum(abs(new_rank[v] - rank[v]) for v in nodes) < tol:
            rank = new_rank
            break
        rank = new_rank
    return rank


def _resolve_concern_files(graph, symbols: list[str]) -> set[str]:
    """把 concern 符号(bare 名或 qualified)解析成图里的文件路径集合。

    复用 call_chain 同款解析:精确匹配 > bare 名(``qn.split('::')[-1] == sym``)。
    解析不到的符号静默跳过(上层 note 汇总)。只调一次 ``_build_networkx_graph``,
    多符号共用,省去反复建图。
    """
    nxg = graph._store._build_networkx_graph()
    files: set[str] = set()
    qns: set[str] = set()
    for sym in symbols:
        if sym in nxg:
            qns.add(sym)
        else:
            hits = [n for n in nxg if str(n).split("::")[-1] == sym]
            if hits:
                qns.add(hits[0])  # 多个同名取首个(bare 名歧义上层 note 提示)
    for nd in graph._store._batch_get_nodes(qns):
        if nd.file_path:
            files.add(nd.file_path)
    return files


def cross_version_diff(base_ref: str, head_ref: str, *, repo_path: str,
                       concern_files: list[str] | None = None,
                       concern_symbols: list[str] | None = None,
                       graph: CodeGraph | None = None,
                       top_commits: int = 30, max_diff_chars: int = 8000) -> dict:
    """跨版本对比 —— 同一个代码仓的两个 git ref(tag/commit/branch)之间,回答
    「旧版本(base)→ 新版本(head)之间,我关心的 concern 改了啥 / 修了没」。

    给 agent 的**确定性事实**(零 LLM):「修没修 / 怎么移植」的判断由 agent 综合本工具产出 +
    search_codebase + call_chain 做出 —— 本工具只管把 git 层的事实喂准。这是 harness 转向后的
    分工:工具出事实,判断归 agent(对标 deer-flow「round1 确定性脚本、后续 LLM 解释」)。

    干啥(面向小白)
    ----------------
    想象你修一个 5.50 版本的 bug,想知道 5.85 有没有已经修了、怎么修的。本工具干三件事:
      1) 列出 base..head 之间的提交(尤其触及 concern 的)——「中间到底改了哪些 commit」
         (对标 spec 的确定性门 ``git patch-id``/``git cherry``:MVP 用 git log);
      2) 给 concern 涉及文件的 ``git diff`` 文本——「具体代码怎么变的」,供 agent 读修法;
      3) (有结构图时)把 diff 映射到函数级——「哪些函数被触及了」。
    再附一个 ``git cherry`` 摘要(head 相对 base 的净新补丁数)——「backport 等价」的快速信号。

    为什么用 git 而不是结构 diff 工具(difftastic 之类)
      - 跨版本对比的正主就是 ``git diff/log/cherry``:结构化、零依赖、确定性。
      - difftastic/SemanticDiff 是「查看器」(产 AST 可视化),不做函数级分类、不打补丁,
        对「给 agent 喂事实」不如 git 直接。结构图只做可选的函数级富化。

    base_ref / head_ref:同一仓库的两个 git ref(如 ``"5.50"`` / ``"5.85"``,或 ``"HEAD~5"``/``"HEAD"``)。
    repo_path:仓库工作树的**绝对路径**(跑 git 命令的 cwd)。
    concern_files / concern_symbols:只关心这部分;symbols 会在图里解析成文件(需 ``graph``)。
        都不给则不收窄(全量 commit 列表 + 跳过全量 diff,防回巨大 diff)。
    graph:可选的 CodeGraph(有才做 concern_symbols 解析 + touched_functions 富化)。
    top_commits:commit 列表上限(默认 30)。max_diff_chars:concern_diff 回显上限(默认 8000)。

    返回 ``{refs, commits, commits_truncated, patch_equivalence, concern_diff, touched_functions, note}``。
    失败(非 git 仓 / ref 不存在 / repo_path 无效)→ 抛 ``ValueError``,工具层转友好串。
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo_path 不是目录: {repo_path}")
    for ref in (base_ref, head_ref):
        if not _SAFE_GIT_REF.match(ref):
            raise ValueError(f"非法 git ref(只允许字母数字 _ . / ~ ^ @ {{ }} -): {ref!r}")

    def _git(args: list[str], *, timeout: int = _GIT_TIMEOUT) -> str:
        # 本地别名:绑定 repo,转发到模块级 _run_git(impl 集中,cross_version_diff / merge_eval 共用)。
        return _run_git(repo, args, timeout=timeout)

    # 1) 解析两 ref → sha(顺带验存在;不存在 git 报错 → 上面的 _git 转 ValueError)
    base_sha = _git(["rev-parse", "--verify", base_ref]).strip()
    head_sha = _git(["rev-parse", "--verify", head_ref]).strip()

    notes: list[str] = []

    # 2) concern 文件清单:显式给的 + symbols 在图里解析出的
    files: set[str] = set(concern_files or [])
    if concern_symbols and graph is not None:
        try:
            files |= _resolve_concern_files(graph, concern_symbols)
        except Exception as exc:  # noqa: BLE001 —— 图解析失败不致命,降级
            notes.append(f"concern_symbols 解析失败,已忽略: {exc}")
    elif concern_symbols and graph is None:
        notes.append("传了 concern_symbols 但没给 graph,无法解析成文件(给 graph 或改传 concern_files)。")

    pathspec = sorted(files) if files else None

    # 3) base..head 的提交(concern 收窄);确定性门(MVP 用 git log,逐 commit patch-id 留迭代)
    log_args = ["log", "--no-merges", "--format=%H%x1f%s", f"--max-count={top_commits}",
                f"{base_ref}..{head_ref}"]
    if pathspec:
        log_args += ["--", *pathspec]
    commits = []
    for line in _git(log_args).splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            commits.append({"sha": sha, "subject": subject})

    # 4) git cherry 摘要:head 相对 base 的净新 / 等价补丁数(backport 等价信号)
    new_in_head = equiv_in_base = 0
    try:
        for line in _git(["cherry", base_ref, head_ref]).splitlines():
            if line.startswith("+"):
                new_in_head += 1
            elif line.startswith("-"):
                equiv_in_base += 1
    except ValueError as exc:
        notes.append(f"git cherry 跳过: {exc}")
    patch_equivalence = {"new_in_head": new_in_head, "equivalent_in_base": equiv_in_base}

    # 5) concern 的 diff 文本(给 agent 读修法);没给 concern → 跳全量 diff(可能巨大)
    if pathspec:
        try:
            concern_diff = _git(["diff", f"{base_ref}..{head_ref}", "--", *pathspec],
                                timeout=max(_GIT_TIMEOUT, 60))[:max_diff_chars]
            if not concern_diff.strip():
                notes.append("concern 文件在 base..head 间无改动。")
        except ValueError as exc:
            concern_diff = ""
            notes.append(f"concern_diff 取失败: {exc}")
    else:
        concern_diff = ""
        notes.append("未给 concern,跳过全量 diff(要 diff 传 concern_files)。")

    # 6) touched_functions(有图才给):把 base..head diff 映射到函数节点
    touched = []
    if graph is not None:
        try:
            from code_review_graph.changes import map_changes_to_nodes, parse_git_diff_ranges
            ranges = parse_git_diff_ranges(str(repo), f"{base_ref}..{head_ref}")
            if pathspec:  # 只关心 concern 文件
                ranges = {f: rs for f, rs in ranges.items() if f in files}
            nodes = map_changes_to_nodes(graph._store, ranges)
            touched = [{"qualified_name": n.qualified_name, "file": n.file_path,
                        "line": n.line_start, "kind": n.kind} for n in nodes]
            notes.append("touched_functions 映射自 base..head diff 的 NEW(head)侧行号 —— "
                         "图须对应 head_ref 才行号对齐。")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"touched_functions 富化失败,已跳过: {exc}")

    return {
        "refs": {"base": base_ref, "head": head_ref, "base_sha": base_sha, "head_sha": head_sha},
        "commits": commits,
        "commits_truncated": len(commits) >= top_commits,
        "patch_equivalence": patch_equivalence,
        "concern_diff": concern_diff,
        "touched_functions": touched,
        "note": " | ".join(notes) if notes else "",
    }


def merge_eval(upstream_base_ref: str, upstream_head_ref: str, *,
               fork_ref: str, repo_path: str,
               concern_files: list[str] | None = None,
               max_commits: int = 50, graph: CodeGraph | None = None) -> dict:
    """上游 commit 合入评估 —— 把上游仓一段 commit 范围逐个拿来问「该不该合入 fork」,
    给 agent **确定性事实**(零 LLM):逐 commit 的「已修 / 能不能干净打上 / 触及哪些文件函数」。
    「相不相关 / 该不该合」的最终判断由 agent 综合本工具产出 + search_codebase + call_chain 做出
    (确定性地板 + LLM 天花板,对标 VeriPort/PortGPT)。

    干啥(面向小白)
    ----------------
    你维护一个 fork(如 wpa 的内部分支 release/eagle),上游不停出修复/安全补丁,你想知道哪些该
    backport 过来。本工具对上游每个 commit 给三态(基于 git 确定性事实):
      - ``already_fixed``(已修):fork 里已经有等价补丁了(git patch-id 命中)→ 不用再合;
      - ``recommend_merge``(建议合):fork 没这补丁,且能干净 apply 到 fork → 候选(待 agent 查相关性);
      - ``conflict``(冲突):fork 没这补丁,且 apply 冲突 → 需人工解冲突;
      - ``uncertain``:apply 检查没跑成(如 worktree 不干净 / commit 无父),拿不准。
    「已修」靠 git patch-id(``git log --cherry-mark``)—— 对 diff 算哈希,对空白 / commit message 免疫,
    是 backport 检测的黄金机制(上游 commit 被 backport 时改了补丁文本才会漏检,那时靠 agent 语义判断)。

    为什么这样分(确定性地板 + LLM 天花板)
      - patch-id 等价 + apply --check 是 git 原生的确定性事实(零 LLM、可复现)→ 地板;
      - 「能 apply」不等于「fork 真需要这个补丁」(fork 可能根本没那个 bug / 功能)→ 相关性判断归 agent。

    upstream_base_ref / upstream_head_ref:上游 commit 范围两端(同一仓库的 git ref,如上次同步点
        到 ``upstream/master`` 最新)。须先把上游 fetch 进本仓(``git remote add upstream <url> &&
        git fetch upstream``)让这些 ref 可见 —— agent 的活,本工具不做。
    fork_ref:fork 侧对照分支(如 ``release/eagle``)。两用:① patch-id 等价比对的一方;
        ② apply 检查的基准(见下 caveat)。
    repo_path:仓库工作树**绝对路径**(跑 git 的 cwd)。
    concern_files:只评估触及这些文件的上游 commit(收窄;不给则范围内全量)。
    max_commits:逐 commit 扫描上限(默认 50,防巨大 range 烧时间)。
    graph:可选 CodeGraph(有才做 touched_functions 富化;per-commit CRG 映射较慢,超阈值自动跳过)。

    ⚠️ apply 检查的实现(2026-08-17 升级,原 backlog #60):优先 ``git merge-tree --write-tree
    <fork_ref> <commit>`` 零 touch 判冲突 —— 不依赖当前 worktree 状态(不用先 checkout fork_ref、
    worktree 脏也不影响结果),返回码 0=干净 / 1=冲突(树对象写进对象库,不碰工作树/索引;两边都
    不动被 merge 的 ref)。git < 2.38 或 merge-tree 失败 → 回退老路:commit diff 跑 ``git apply
    --check`` 对当前 worktree(那时才需要 checkout + 干净树),回退会在 note 里说明。

    返回 ``{repo, fork_ref, upstream_range, commits:[...], summary:{...}, note}``。
    失败(非 git 仓 / ref 不存在 / repo_path 无效)→ 抛 ``ValueError``,工具层转友好串。
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo_path 不是目录: {repo_path}")
    for ref in (upstream_base_ref, upstream_head_ref, fork_ref):
        if not _SAFE_GIT_REF.match(ref):
            raise ValueError(f"非法 git ref(只允许字母数字 _ . / ~ ^ @ {{ }} -): {ref!r}")

    # merge-tree 探测(一次性,循环外):git ≥ 2.38 支持 `merge-tree --write-tree`;老 git 收到
    # --write-tree 直接 usage 报错(rc≠0 且 stderr 带 usage)。探测用已验证存在的 fork_ref 对
    # 自身 merge(恒干净),rc=0 → 可用;否则记 note + 回退 apply --check 老路。
    try:
        mt_probe = subprocess.run(["git", "merge-tree", "--write-tree", fork_ref, fork_ref],
                                  cwd=str(repo), capture_output=True, text=True, timeout=_GIT_TIMEOUT)
        mt_available = mt_probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        mt_available = False

    # 解析三 ref → sha(顺带验存在;不存在 git 报错 → _run_git 转 ValueError)
    base_sha = _run_git(repo, ["rev-parse", "--verify", upstream_base_ref]).strip()
    head_sha = _run_git(repo, ["rev-parse", "--verify", upstream_head_ref]).strip()
    fork_sha = _run_git(repo, ["rev-parse", "--verify", fork_ref]).strip()

    # 无共同祖先前置短路(2026-08-26 实测:deepin fork 是 squash 独立血统)—— merge-tree 对
    # 无关历史直接拒绝(rc=128「拒绝合并无关的历史」),patch-id 对称差也失去参照,逐 commit
    # 全落 uncertain,零信号还烧 max_commits 轮。提前一句话告诉 agent 地板不可用、改走语义
    # 评估,别让它对着 5/5 uncertain 猜工具是不是坏了。
    try:
        mb = _run_git(repo, ["merge-base", fork_sha, head_sha]).strip()
    except ValueError:
        mb = ""
    if not mb:
        return {"repo": str(repo), "fork_ref": fork_ref,
                "upstream_range": f"{upstream_base_ref}..{upstream_head_ref}",
                "commits": [], "summary": {"total": 0},
                "note": "fork 与上游无共同祖先(squashed/独立血统)—— patch-id 等价与 merge-tree "
                        "地板均不可用,本工具到此为止。请直接逐 commit 语义评估:git show 读 diff + "
                        "对照 fork 对应代码判断「fork 有没有这 bug/要不要这修」,标准参照 backport "
                        "工作流(能干净 apply 只说明打得上,不说明该打)。"}

    notes: list[str] = []
    if not mt_available:
        notes.append("merge-tree 不可用(git < 2.38?),apply 检查回退 git apply --check 对当前 "
                     "worktree —— 结果依赖 checkout 状态,三态可能失真。")
    pathspec = sorted(concern_files) if concern_files else None

    # 1) 上游范围内的 commit(upstream_base..upstream_head,concern 收窄)
    log_args = ["log", "--no-merges", "--format=%H%x1f%s", f"--max-count={max_commits}",
                f"{upstream_base_ref}..{upstream_head_ref}"]
    if pathspec:
        log_args += ["--", *pathspec]
    raw_commits: list[tuple[str, str]] = []
    for line in _run_git(repo, log_args).splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            raw_commits.append((sha, subject))

    if not raw_commits:
        return {"repo": str(repo), "fork_ref": fork_ref,
                "upstream_range": f"{upstream_base_ref}..{upstream_head_ref}",
                "commits": [], "summary": {"total": 0},
                "note": "上游范围内无 commit(或被 concern_files 收窄过滤光)。"}

    commit_shas = [sha for sha, _ in raw_commits]

    # 2) 逐 commit 的「已修」判定:git --cherry-pick 内部按 patch-id 把 fork 已有等价的 commit 剔除。
    #    故 shown(fork...upstream 对称差的 upstream 侧)= 还没等价进 fork 的候选;范围内不在 shown
    #    里的 commit = 已修(被 cherry 逻辑剔除,或本就可达于 fork)。比逐个算 patch-id 高效(只走对称差,
    #    不扫 fork 全史)。实证(git 2.x):`=` 标记不会出现 —— 等价 commit 直接被对称差剔除,故取反集。
    shown: set[str] = set()
    try:
        out = _run_git(repo, ["log", "--no-merges", "--cherry-pick", "--right-only",
                              "--format=%H", f"{fork_ref}...{upstream_head_ref}"])
        shown = {ln.strip() for ln in out.splitlines() if ln.strip()}
    except ValueError as exc:
        notes.append(f"patch-id 等价判定(--cherry-pick)跳过: {exc}")
    equiv = {sha for sha in commit_shas if sha not in shown}

    # 3) touched_functions 富化(per-commit CRG 映射慢):commit 少才做,多则只给 touched_files
    touch_funcs_cap = 12
    do_touch_funcs = graph is not None and len(raw_commits) <= touch_funcs_cap
    if graph is not None and not do_touch_funcs:
        notes.append(f"commit 数 > {touch_funcs_cap},touched_functions 富化跳过(仍给 touched_files)。")

    commits_out: list[dict] = []
    for sha, subject in raw_commits:
        equivalent_in_fork = sha in equiv

        # apply 检查(优先 merge-tree,回退 apply --check):
        #   merge-tree --write-tree <fork_ref> <commit> 在对象库里试合并(不碰 worktree/索引),
        #   rc=0 干净 / rc=1 冲突 —— 三态不再依赖「先 checkout fork_ref + worktree 干净」的调用姿势。
        #   merge-tree 不可用(老 git)或跑挂 → 走老路:commit diff 跑 git apply --recount --check 对
        #   当前 worktree(归一化防踩坑#15 末尾换行);再挂 → None(uncertain)。
        applies_cleanly: bool | None
        if mt_available:
            try:
                mt = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only",
                                     fork_ref, sha],
                                    cwd=str(repo), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT)
                if mt.returncode in (0, 1):
                    applies_cleanly = mt.returncode == 0
                else:
                    applies_cleanly = None  # rc>1 = 真错误(如 ref 解析挂),拿不准
            except (OSError, subprocess.SubprocessError):
                applies_cleanly = None
        else:
            applies_cleanly = None
            try:
                diff = _run_git(repo, ["diff", "--no-color", f"{sha}^", sha])
                diff = diff.replace("\r\n", "\n").replace("\r", "\n")
                if not diff.endswith("\n"):
                    diff += "\n"
                ar = subprocess.run(["git", "apply", "--recount", "--check"], input=diff,
                                    cwd=str(repo), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=60)
                applies_cleanly = ar.returncode == 0
            except (ValueError, subprocess.SubprocessError):
                applies_cleanly = None  # commit 无父 / git 挂 → 拿不准,标 uncertain

        # touched_files(git diff-tree 廉价、无 header 污染)
        try:
            names = _run_git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", sha])
            touched_files = [ln for ln in names.splitlines() if ln.strip()]
        except ValueError:
            touched_files = []

        # touched_functions(可选 CRG 富化);显式 graph is not None 让类型检查收窄(同 cross_version_diff)
        touched_functions: list[dict] = []
        if do_touch_funcs and graph is not None:
            try:
                from code_review_graph.changes import map_changes_to_nodes, parse_git_diff_ranges
                ranges = parse_git_diff_ranges(str(repo), f"{sha}^..{sha}")
                if pathspec:
                    ranges = {f: rs for f, rs in ranges.items() if f in set(pathspec)}
                nodes = map_changes_to_nodes(graph._store, ranges)
                touched_functions = [{"qualified_name": n.qualified_name, "file": n.file_path,
                                      "line": n.line_start, "kind": n.kind} for n in nodes]
            except Exception:  # noqa: BLE001 —— 富化失败不致命,只缺这块
                pass

        # 三态判定:已修优先(确定性高);次看 apply 结果
        if equivalent_in_fork:
            state = "already_fixed"
        elif applies_cleanly is None:
            state = "uncertain"
        elif applies_cleanly:
            state = "recommend_merge"
        else:
            state = "conflict"

        commits_out.append({
            "sha": sha, "subject": subject,
            "equivalent_in_fork": equivalent_in_fork,
            "applies_cleanly": applies_cleanly,
            "touched_files": touched_files,
            "touched_functions": touched_functions,
            "state": state,
        })

    summary: dict[str, int] = {"total": len(commits_out)}
    for st in ("already_fixed", "recommend_merge", "conflict", "uncertain"):
        summary[st] = sum(1 for c in commits_out if c["state"] == st)

    return {
        "repo": str(repo), "fork_ref": fork_ref,
        "upstream_range": f"{upstream_base_ref}..{upstream_head_ref}",
        "fork_sha": fork_sha, "upstream_base_sha": base_sha, "upstream_head_sha": head_sha,
        "commits": commits_out, "summary": summary,
        "note": " | ".join(notes) if notes else "",
    }


def when_introduced(repo_path: str, *, symbol: str | None = None,
                    file: str | None = None, line: int | None = None,
                    line_end: int | None = None, max_commits: int = 20) -> dict:
    """bug 引入 commit 定位(SZZ 式)——「这个 bug 是哪个 commit 带进来的?」。

    给 agent **确定性候选**(零 LLM),哪条真引入归 agent 语义裁决(确定性地板 + LLM 天花板,
    与 merge_eval 同分工;业界路线 = SZZ 出候选 + agent 裁决)。

    干啥(面向小白)
    ----------------
    根因锚定到某符号或某行后,问「这段缺陷逻辑是哪次改动带进来的」。两种锚定模式(二选一):
      - **pickaxe(symbol,可配 file 收窄)**:`git log -S <symbol>` 找 diff 里**增/删过该字符串**
        的 commit —— 适合「根因锚在函数/标识符名上」(引入 commit 通常就是第一次 ADD 该符号的那条);
      - **行历史(file + line[, line_end])**:`git log -L <l>,<l>:<file>` 追这一行(段)的
        演化史 —— 适合「根因锚在 file:line 上」(-L 自带改名跟随,行号漂移不怕)。
    返回候选表(时间倒序),每条带 sha/日期/作者/subject + added/removed 计数:
      - pickaxe 模式:added/removed = 该 commit diff 中**含 symbol** 的 +/- 行数
        (引入 commit 通常 added>0、removed==0;中间 added/removed 成对的多是重构搬移);
      - 行历史模式:added/removed = 该 commit 在所追行段上的 +/- 行数。

    候选只是地板:哪条真正引入**缺陷逻辑**(而非把既有逻辑换个位置)是语义判断 ——
    逐条 `git show <sha>` 读 message + diff 裁决;引入 commit 的 message/diff 常直接暴露
    根因意图,是假设循环的一路辅助证据。

    repo_path:仓库工作树**绝对路径**(跑 git 的 cwd)。
    symbol:pickaxe 字符串(函数名/标识符;**只查当前 checkout 分支**,不带 --all 防多分支重复)。
    file:symbol 模式下作 pathspec 收窄(常见短名如 "scan" 会命中一片,务必配 file);
          行历史模式下是**仓相对路径**,必填(与 line 配套)。
    line / line_end:行历史的行号(段),line_end 缺省 = line(单行)。行号按**当前工作树**。
    max_commits:候选封顶(默认 20,时间倒序取最新 N 条)。

    失败(非 git 仓 / 锚点形态不对 / 文件不存在)→ 抛 ``ValueError``,工具层转友好串。
    返回 ``{repo, mode, anchor, commits:[...], note}``。
    """
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo_path 不是目录: {repo_path}")
    # 锚点形态校验:pickaxe(symbol[,file])或行历史(file+line)二选一;file 单独给=全文件史太噪,拒。
    if symbol and line:
        raise ValueError("symbol 与 line 二选一:symbol 走 pickaxe(-S),file+line 走行历史(-L)")
    if not symbol and not (file and line):
        raise ValueError("缺锚点:传 symbol(pickaxe;短名配 file 收窄)或 file+line(行历史)")
    if line and not file:
        raise ValueError("line 需要 file 配套(行历史的锚是 file:line)")

    # 记录分隔 \x1e 开头 + 字段分隔 \x1f;--patch/--no-color 让 diff 跟在头后面,
    # 一趟 git 同时拿元数据和 +/- 计数(不用逐 commit 再 git show)。
    fmt = "%x1e%H%x1f%aI%x1f%an%x1f%s"
    if symbol:
        mode = "pickaxe"
        # -S 紧贴值(-S<sym>)防「-」开头被当选项;file 作 pathspec 收窄
        args = ["log", "--no-merges", f"-S{symbol}", f"--max-count={max_commits}",
                "--patch", "--no-color", f"--format={fmt}"]
        if file:
            args += ["--", file]
    else:
        mode = "line_history"
        end = line_end or line
        # -L 紧贴值(-L<l>,<end>:<file>);-L 输出自带所追行段的 diff,无需 --patch
        args = ["log", "--no-merges", f"-L{line},{end}:{file}",
                f"--max-count={max_commits}", "--no-color", f"--format={fmt}"]

    out = _run_git(repo, args)

    commits: list[dict] = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, diff = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        added = removed = 0
        for ln in diff.splitlines():
            if ln.startswith(("+++", "---", "diff ", "@@")):
                continue  # diff 头不是内容行
            if ln.startswith("+") and (mode == "line_history" or symbol in ln):
                added += 1
            elif ln.startswith("-") and (mode == "line_history" or symbol in ln):
                removed += 1
        commits.append({"sha": parts[0], "date": parts[1], "author": parts[2],
                        "subject": parts[3], "added": added, "removed": removed})

    if not commits:
        return {"repo": str(repo), "mode": mode,
                "anchor": {"symbol": symbol, "file": file, "line": line,
                           "line_end": line_end or line},
                "commits": [],
                "note": "没找到触及该锚点的 commit(字符串在历史中从未增删 / 行段无演化记录)。"
                        "换个锚点重试:相邻行 / 函数名 / 更稳定的标识符。"}

    notes = ["候选按时间倒序;哪条真正引入缺陷(而非重构搬移既有逻辑)是语义判断,归调用方:"
             "逐条 git show <sha> 读 message+diff——引入 commit 通常是列表末尾(最老)added>0 且 "
             "removed==0 的那条,中间 added/removed 成对的常是重构/搬移;引入 commit 的 "
             "message/diff 常直接暴露缺陷意图(根因的辅助证据)。"
             "只查了当前 checkout 分支(不带 --all)。"]
    if len(commits) >= max_commits:
        notes.append(f"命中 {max_commits} 条封顶,更老的引入 commit 可能没列出——加大 max_commits 重调。")
    return {"repo": str(repo), "mode": mode,
            "anchor": {"symbol": symbol, "file": file, "line": line,
                       "line_end": line_end or line},
            "commits": commits, "note": " | ".join(notes)}


def _render_repomap_tree(files: dict, meta: dict, scores: dict) -> str:
    """把选中的符号按文件分组,渲染成 Aider 式的「仓库地图」树。

    面向小白:想象给一栋大楼画「重要房间分布图」—— 每个文件是一层楼,楼里最重要的房间
    (PageRank 高的函数)排前面。树形缩进让人(和 LLM)一眼看清「哪些函数在哪、谁重要」。

    - ``files``: ``{文件路径: [符号 qualified_name 列表]}``(由 repo_map 按 PageRank 降序填好)。
    - ``meta`` / ``scores``: 给每个符号补 kind / 行号 / 分数(来自 CRG 节点 + PageRank)。
    - CRG 的 file_path / qualified_name 存的是**绝对路径**(如 ``/home/.../wpa/wpa_cli.c::wpa_cli_cmd``),
      直接显示会让全图被重复的绝对路径前缀淹没(费 token、没法读)。故这里做两件压缩:
      ① 文件头剥「全仓公共路径前缀」显示相对路径;② 符号行去掉开头的路径前缀,只留 ``Class::symbol``。
    - 文件按「楼里最高分符号」降序排(核心模块的文件在前);楼内符号再按分降序排。
    - 返回多行文本,用 ├──/└── 树连接符,文件之间空行分隔。
    """
    import os

    paths = list(files.keys())
    if not paths:  # 没符号塞进预算 → 空地图(repo_map 在 scores 空或预算太小一个都装不下时会传空)
        return ""
    # 全仓公共路径前缀:多文件取 commonpath,单文件取其所在目录;算不出(跨盘等)→ 空 → 退化 basename
    try:
        prefix = os.path.commonpath(paths) if len(paths) > 1 else os.path.dirname(paths[0])
    except ValueError:
        prefix = ""

    def _rel(p: str) -> str:
        """绝对路径 → 剥公共前缀的相对路径;剥不干净(前缀非真祖先)→ basename 兜底。"""
        if prefix:
            r = os.path.relpath(p, prefix)
            if r and not r.startswith(".."):
                return r
        return os.path.basename(p)

    def _sym(qn: str, path: str) -> str:
        """qn 形如 ``<path>::<Class>::<symbol>``;剥掉开头的 ``<path>::`` 留 ``Class::symbol`` 这段可读名。"""
        tag = path + "::"
        return qn[len(tag):] if qn.startswith(tag) else qn.split("::")[-1]

    # 文件排序键 = 该文件里最高分符号的 PageRank(核心文件排前);并列时按路径稳定排
    def _file_top_score(path: str) -> float:
        syms = files.get(path) or []
        return max((scores.get(s, 0.0) for s in syms), default=0.0)

    out_lines: list[str] = []
    for path in sorted(files, key=lambda p: (-_file_top_score(p), p)):
        syms = sorted(files[path], key=lambda s: -scores.get(s, 0.0))
        out_lines.append(_rel(path))
        for i, qn in enumerate(syms):
            nd = meta.get(qn)
            kind = getattr(nd, "kind", "?") if nd else "?"
            lineno = getattr(nd, "line_start", "?") if nd else "?"
            branch = "└──" if i == len(syms) - 1 else "├──"
            out_lines.append(f"{branch} {_sym(qn, path)} ({kind}) L{lineno} pr={scores.get(qn, 0.0):.3f}")
        out_lines.append("")  # 文件之间空行分隔,增强可读性
    return "\n".join(out_lines).rstrip()


class CodeGraph:
    """一个代码仓的结构图句柄(建一次,查多次)。

    典型用法:
        cg = CodeGraph.build(repo_root="/path/to/wpa", repo_name="wpa")  # 建图(慢,一次性)
        overview = cg.architecture_overview()   # 社区清单 + 跨社区耦合告警
        hubs = cg.hub_nodes(top_n=15)           # 高连接枢纽(被大量调用 / 大量调用)
        bridges = cg.bridge_nodes(top_n=15)     # betweenness 瓶颈(断了多社区失联)
    """

    def __init__(self, store, repo_name: str):
        # store 是 code_review_graph.graph.GraphStore 实例。这里不写死类型注解,避免缺 extra 时
        # 本文件 import 就崩(查询方法内部才 import CRG,与 structural.py 一致的做法)。
        self._store = store
        self.repo_name = repo_name

    # ── 建图 ──────────────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        repo_root: str | Path,
        repo_name: str,
        *,
        base_dir: str | None = None,
        min_community_size: int = 2,
    ) -> CodeGraph:
        """建图(一次性,慢):解析全仓 → 存节点/边 → Leiden 社区检测 → 持久化。

        返回建好、可直接查询的 CodeGraph。db 落 <base_dir>/<repo_name>/graph.db。
        建完顺手把当前 git HEAD 记进同目录 ``built_head`` 快照 —— update() 靠它算
        「上次建图以来改了哪些文件」(非 git 仓没快照,update() 每次都退回全量)。
        """
        base_dir = base_dir or default_graph_root()
        _require_crg()
        from code_review_graph.communities import detect_communities, store_communities
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import full_build

        db_path = Path(base_dir) / repo_name / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = GraphStore(db_path)

        logger.info("CRG full_build 开始: %s → %s", repo_root, db_path)
        build_stats = full_build(Path(repo_root), store)
        logger.info("CRG 建图完成: %s", build_stats)

        # 社区检测 + 持久化:overview / hub / bridge 读节点的 community_id,必须先 store_communities
        communities = detect_communities(store, min_size=min_community_size)
        stored = store_communities(store, communities)
        logger.info("CRG 社区: 检测 %d 个,持久化 %d 个", len(communities), stored)

        head = _current_head(Path(repo_root))
        if head:
            (db_path.parent / "built_head").write_text(head + "\n", encoding="utf-8")

        return cls(store, repo_name)

    @classmethod
    def update(
        cls,
        repo_root: str | Path,
        repo_name: str,
        *,
        base_dir: str | None = None,
        min_community_size: int = 2,
    ) -> tuple[CodeGraph, dict]:
        """增量刷新已有结构图:只重解析「上次建图/更新以来动过 + 未跟踪新增」的文件。

        接 CRG incremental machinery:`incremental_update` 重解析改动文件及其依赖文件
        (内部还有 per-file sha256 快筛,清单给宽不亏),`incremental_detect_communities`
        在改动不触及现有社区时直接跳过、触及才全量重检测。补丁打进工作区或合入后,
        重跑 `rootrecall index` 就走到这里 —— 向量索引本就按 manifest 增量,结构图
        以前是整个跳过(--force 才全量重建,图会静默变陈旧),现在改为增量刷新。

        兜底链(任何一步拿不准 → 全量重建,宁可贵不错):
        图不存在 / built_head 快照缺失(旧版建的图、或非 git 仓)/ 非 git 仓 /
        快照 commit 不可解析(rebase 改过历史)→ build()。

        返回 (CodeGraph, 摘要 dict):摘要 mode ∈ incremental | noop | full_rebuild;
        incremental 再带 files_updated / changed_files / dependent_files / communities。
        """
        base_dir = base_dir or default_graph_root()
        db_path = Path(base_dir) / repo_name / "graph.db"
        marker = Path(base_dir) / repo_name / "built_head"
        repo = Path(repo_root)

        def _full(reason: str) -> tuple[CodeGraph, dict]:
            logger.info("CRG 增量刷新退回全量重建(%s): %s", reason, repo)
            g = cls.build(repo, repo_name, base_dir=base_dir, min_community_size=min_community_size)
            return g, {"mode": "full_rebuild", "reason": reason}

        if not db_path.exists() or not marker.exists():
            return _full("图或 built_head 快照不存在" if db_path.exists() else "图未建")

        head_new = _current_head(repo)
        if head_new is None:
            return _full("非 git 仓,无 diff 基准")

        try:
            changed = _changed_since(repo, marker.read_text(encoding="utf-8").strip())
        except ValueError as e:
            return _full(f"基线 commit 不可解析: {e}")

        if not changed:
            marker.write_text(head_new + "\n", encoding="utf-8")  # 推进快照(空提交等场景)
            g = cls.open(repo_name, base_dir=base_dir)
            return g, {"mode": "noop", "changed_files": 0}

        from code_review_graph.communities import incremental_detect_communities
        from code_review_graph.graph import GraphStore
        from code_review_graph.incremental import incremental_update

        store = GraphStore(db_path)
        stats = incremental_update(repo, store, changed_files=changed)
        n_comm = incremental_detect_communities(store, changed, min_size=min_community_size)
        marker.write_text(head_new + "\n", encoding="utf-8")
        logger.info("CRG 增量刷新完成: %s,社区重存 %d 个", stats, n_comm)
        return cls(store, repo_name), {
            "mode": "incremental", **stats, "communities": n_comm,
        }

    @classmethod
    def open(cls, repo_name: str, *, base_dir: str | None = None) -> CodeGraph:
        """打开已建好的图(不重建,省去 full_build)。db 不存在 → FileNotFoundError。"""
        base_dir = base_dir or default_graph_root()
        _require_crg()
        from code_review_graph.graph import GraphStore

        db_path = Path(base_dir) / repo_name / "graph.db"
        if not db_path.exists():
            raise FileNotFoundError(f"结构图未建,先 CodeGraph.build(...): {db_path}")
        return cls(GraphStore(db_path), repo_name)

    # ── 查询(薄封装 CRG 分析 API)────────────────────────────────────────

    def architecture_overview(self) -> dict:
        """架构总览:社区清单 + 跨社区耦合边 + 高耦合告警(>10 条边的社区对)。

        报告「系统架构」章节的数据源(图驱动,非 LLM 瞎编)。
        """
        from code_review_graph.communities import get_architecture_overview

        return get_architecture_overview(self._store)

    def communities(self) -> list[dict]:
        """社区清单(≈ 模块边界):每个社区 = 一组被 Leiden 聚到一起的结构节点。"""
        from code_review_graph.communities import get_communities

        return get_communities(self._store)

    def hub_nodes(self, top_n: int = 15, *, exclude_tests: bool = True) -> list[dict]:
        """hub 节点(in+out 度最高):核心函数 / 被大量依赖的枢纽。报告「结构风险」用。

        exclude_tests(默认开):过滤测试/仿真/生成文件路径的节点 —— 实测(2026-08-26 bluez)
        不过滤时 hub 榜被 mgmt-tester(474 入边)、ltmain.sh(度 1475)霸屏,真正的核心入口
        全被挤出 top_n。实现:超采样 top_n×8 再过滤截断(CRG 的度数是全图预算好的,多取便宜)。
        """
        from code_review_graph.analysis import find_hub_nodes

        from rootrecall.services.code_index.noisepaths import is_noise_path

        fetch_n = max(top_n * 8, top_n + 40) if exclude_tests else top_n
        hubs = find_hub_nodes(self._store, top_n=fetch_n)
        if exclude_tests:
            hubs = [h for h in hubs if not is_noise_path(h.get("file") or "")]
        return hubs[:top_n]

    def bridge_nodes(self, top_n: int = 15, *, exclude_tests: bool = True) -> list[dict]:
        """bridge 节点(betweenness 最高):多处最短路径必经的瓶颈,断了多社区失联。

        exclude_tests(默认开):同 hub_nodes —— 测试文件的调用量虚高会顶出假瓶颈。
        """
        from code_review_graph.analysis import find_bridge_nodes

        from rootrecall.services.code_index.noisepaths import is_noise_path

        fetch_n = max(top_n * 8, top_n + 40) if exclude_tests else top_n
        bridges = find_bridge_nodes(self._store, top_n=fetch_n)
        if exclude_tests:
            bridges = [b for b in bridges if not is_noise_path(b.get("file") or "")]
        return bridges[:top_n]

    def impact_radius(self, changed_files: list[str]) -> dict:
        """改动影响面(BFS):给定一批改动文件,返回受波及的节点/文件/边(blast-radius)。

        路径容错:CRG 存的文件路径带 repo_root 前缀(如 ``code-test/v25/bluez/src/...``),
        而 agent / search_codebase / git diff 通常给仓库相对路径(``src/...``)。直接喂相对路径,
        get_nodes_by_file 的精确匹配会落空 → blast_radius 静默返空(蓝芋试出)。这里先把输入
        解析成图里真实存的路径(精确 > 后缀),再算影响面。深度/节点上限用 CRG 默认。
        """
        return self._store.get_impact_radius(self._resolve_file_paths(changed_files))

    def _resolve_file_paths(self, files: list[str]) -> list[str]:
        """把 agent 给的文件路径解析成图里真实存的路径(精确命中 > 后缀兜底 > 原样)。

        CRG 存路径带 repo_root 前缀;agent 常给仓库相对路径。后缀兜底用「存的路径以 /<输入>
        结尾」匹配,处理前缀差异(绝对/相对/prefixed 三种都收敛)。多义(短名撞多个文件)→ 全收
        (宁多勿漏,blast 面本就该宽)。解析不到 → 原样喂下层(让它如实返空,而非假装命中)。
        """
        if not files:
            return files
        try:
            stored = set(self._store.get_all_files())
        except Exception:  # noqa: BLE001 —— 拿不到文件清单就退回原样,不挡查询
            return files
        resolved: list[str] = []
        for f in files:
            if f in stored:  # 精确命中(图就这个格式)
                resolved.append(f)
                continue
            hits = [x for x in stored if x.endswith("/" + f)]  # 后缀兜底(剥 repo_root 前缀)
            resolved.extend(hits if hits else [f])
        # 去重保序
        seen: set[str] = set()
        return [x for x in resolved if not (x in seen or seen.add(x))]

    # ── P1.5 caller/callee 调用链(首次请进适配层,填 __init__.py 的「延后」)─────

    def call_chain(self, symbol: str, *, direction: str = "both",
                   depth: int = 2, top_n: int = 15) -> dict:
        """符号中心的 N 跳调用链(沿 CALLS 边)+ PageRank 重要度。

        给一个函数名,回答「谁调用它 / 它调用谁,N 跳之内,哪些结构上重要」——
        bug-RCA / 调研时定位根因、判断改动影响最想要的「调用链」视图。

        和 impact_radius(blast_radius)的分工:
          - impact_radius = 文件种子 + 全边类型 + 「波及面」(我改这些文件 → 谁受波及);
          - call_chain    = 符号种子 + 仅 CALLS 边 + 「调用链 + 重要度」(这个函数的调用上下文)。

        symbol:函数/方法名。bare 名(如 wpa_supplicant_init)或 qualified
               (如 wpa_supplicant.c::wpa_supplicant_init)都行,内部解析到图节点。
        direction:"callers"(谁调它,沿 CALLS 逆边)/ "callees"(它调谁,沿 CALLS 正边)/
                  "both"(默认,两边都给)。
        depth:跳数(默认 2,封顶 5 防大图节点爆炸)。
        top_n:每个方向返回的节点上限(按「跳数升序 → PageRank 降序」排后取),默认 15。

        返回 ``{symbol, resolved, direction, depth, callers:[...], callees:[...],
        truncated, note}``,每个节点是 ``{qualified_name, file, line, kind, hop, pagerank}``。
        symbol 解析不到节点 → 抛 ValueError(工具层转友好串)。

        实现全在 networkx 层(复用 store 的缓存全图,只过滤 CALLS 边),不逐边 SQL,大图友好。
        PageRank 在 CALLS-only 子图上跑 —— 被越多重要函数调用 → 分越高,标识「结构上关键的函数」。
        """
        import networkx as nx

        if direction not in ("callers", "callees", "both"):
            raise ValueError(f"direction 需为 callers / callees / both,收到 {direction!r}")
        depth = max(1, min(int(depth), 5))  # 封顶 5 防大图爆炸;至少 1 跳

        # 1) 解析符号 → qualified_name(精确 > bare 名 > 子串兜底)
        nxg = self._store._build_networkx_graph()
        if symbol in nxg:
            seed, note = symbol, ""
        else:
            bare_hits = [n for n in nxg if str(n).split("::")[-1] == symbol]
            if len(bare_hits) == 1:
                seed, note = bare_hits[0], f"resolved bare name '{symbol}' → '{bare_hits[0]}'"
            elif len(bare_hits) > 1:
                seed = bare_hits[0]
                note = (f"bare name '{symbol}' 有 {len(bare_hits)} 个匹配,取首个 '{seed}';"
                        f"其余: {', '.join(bare_hits[1:5])}")
            else:
                sub_hits = [n for n in nxg if symbol in str(n)]
                if len(sub_hits) == 1:
                    seed, note = sub_hits[0], f"resolved by substring '{symbol}' → '{sub_hits[0]}'"
                elif len(sub_hits) > 1:
                    seed = sub_hits[0]
                    note = f"substring '{symbol}' 有 {len(sub_hits)} 个匹配,取首个 '{seed}'"
                else:
                    raise ValueError(
                        f"符号 '{symbol}' 在图里找不到(试 bare 名或 qualified path/file.c::func)"
                    )

        # 2) CALLS-only 子图(复用缓存全图,只留 kind=CALLS 的边;节点随之)
        calls = nx.DiGraph()
        calls.add_edges_from(
            (u, v) for u, v, d in nxg.edges(data=True) if d.get("kind") == "CALLS"
        )

        # 种子可能只在别的边类型里出现(没有 CALLS 边)→ 不在 calls 子图 → 无调用关系,返空链
        if seed not in calls:
            return {"symbol": symbol, "resolved": seed, "direction": direction, "depth": depth,
                    "callers": [], "callees": [], "truncated": False,
                    "note": (note + " | " if note else "") + f"'{seed}' 无 CALLS 边(不被调也不调谁)"}

        # 3) PageRank 在 CALLS 子图上(被越多重要函数调用 → 分越高);分层降级见 _pagerank
        scores: dict[str, float] = _pagerank(calls)

        # 4) N 跳有界 BFS(自写,避开 nx.ancestors/descendants 的无界 transitive 爆炸)
        def _bfs(neighbors_fn, start: str) -> list[tuple[str, int]]:
            # neighbors_fn:calls.successors(callees 正向)/ calls.predecessors(callers 逆向)
            seen: dict[str, int] = {start: 0}
            frontier = [start]
            for hop in range(1, depth + 1):
                nxt = []
                for node in frontier:
                    for nb in neighbors_fn(node):
                        if nb not in seen:
                            seen[nb] = hop
                            nxt.append(nb)
                frontier = nxt
                if not frontier:
                    break
            return [(n, h) for n, h in seen.items() if n != start]  # 丢种子本身

        callers_raw = _bfs(calls.predecessors, seed) if direction in ("callers", "both") else []
        callees_raw = _bfs(calls.successors, seed) if direction in ("callees", "both") else []

        # 5) enrich 节点元数据(批量查 file/line/kind;_batch_get_nodes 自带 SQLite 变量数分批)
        all_qns = {n for n, _ in callers_raw} | {n for n, _ in callees_raw}
        meta = {nd.qualified_name: nd for nd in self._store._batch_get_nodes(all_qns)}

        # 6) 组装:每方向按(跳数升序 → PageRank 降序)排,截 top_n
        def _build(rows: list[tuple[str, int]]) -> tuple[list[dict], bool]:
            ordered = sorted(rows, key=lambda nh: (nh[1], -scores.get(nh[0], 0.0)))
            truncated = len(ordered) > top_n
            out = []
            for n, h in ordered[:top_n]:
                nd = meta.get(n)
                out.append({
                    "qualified_name": n,
                    "file": getattr(nd, "file_path", None),
                    "line": getattr(nd, "line_start", None),
                    "kind": getattr(nd, "kind", None),
                    "hop": h,
                    "pagerank": round(scores.get(n, 0.0), 6),
                })
            return out, truncated

        callers, trunc_c = _build(callers_raw)
        callees, trunc_x = _build(callees_raw)
        return {"symbol": symbol, "resolved": seed, "direction": direction, "depth": depth,
                "callers": callers, "callees": callees, "truncated": trunc_c or trunc_x, "note": note}

    # ── #38 repo-map:PageRank 排名的全仓符号地图(Aider repomap 式)──────────────
    def repo_map(self, *, map_tokens: int = 2048, exclude_tests: bool = True) -> dict:
        """全仓 PageRank 排名的符号地图(Aider repomap 式),塞进 token 预算。

        给 agent 一张「**这个仓里结构上最重要的函数是哪些**」的全局地图 —— 不聚焦某个符号
        (那是 call_chain 的活),而是俯瞰全仓:整张调用图上跑一次 PageRank,被越多重要函数
        调用的函数分越高(= 核心枢纽),按分降序贪心填进 token 预算,按文件分组渲染成树。

        面向小白:call_chain 是「顺着这个函数的调用关系往上下游走 N 跳」(手电筒照一条路);
        repo_map 是「站高处俯瞰整座城,标出最重要的地标」(卫星图)。bug-RCA 委托前给 delegate
        这张图当全局视角,或深度调研时当「关键模块」骨架。

        ``exclude_tests``(默认开):测试/仿真/生成文件的符号不进地图 —— 测试文件的调用量
        虚高会让 PageRank 前 50 名里一半是 *-tester(实测 bluez),挤掉真正的核心模块。

        算法(对标 Aider repomap,但**复用 CRG 已抽好的 CALLS 边 + RootRecall 已有的 _pagerank**,
        不另抄 tags.scm):
          1) ``_build_networkx_graph()`` 拿整图(缓存,同 call_chain)→ 只留 CALLS 边的子图;
          2) 整图 ``_pagerank``(同 call_chain 用的那个,分层降级)→ 每个符号一个 centrality 分;
          3) 按分降序排全部符号 → 逐个估算占多少 token(len//4)→ 贪心填到 ``map_tokens`` 截止;
          4) 选中符号按文件分组 → ``_render_repomap_tree`` 渲染成树。

        ``map_tokens``:地图 token 预算(默认 2048;Aider 默认 1k,这里给大点更适合当 delegate 上下文)。
        返回 ``{repo, map_text, n_symbols, n_files, map_tokens_budget, map_tokens_used, truncated,
        top_symbols, note}``。``top_symbols`` 是结构化 top-10(带 file + pagerank 分,给程序化消费);
        ``map_text`` 是给人/LLM 读的树。无 CALLS 边(空图 / 全孤立)→ 空地图 + note,不抛。
        """
        import networkx as nx

        from rootrecall.services.code_index.noisepaths import is_noise_path

        nxg = self._store._build_networkx_graph()  # 缓存整图(同 call_chain:403)
        # CALLS-only 子图:同 call_chain:427-430 的构造,一字不改(call 边是高信号子集)
        calls = nx.DiGraph()
        calls.add_edges_from((u, v) for u, v, d in nxg.edges(data=True) if d.get("kind") == "CALLS")
        scores: dict[str, float] = _pagerank(calls)  # 现成,分层降级(同 call_chain:439)
        if not scores:  # 无 CALLS 边(空图 / 全孤立)→ 算不出排名,返空地图(不抛,工具层正常展示)
            return {"repo": self.repo_name, "map_text": "", "n_symbols": 0, "n_files": 0,
                    "map_tokens_budget": map_tokens, "map_tokens_used": 0, "truncated": False,
                    "top_symbols": [], "note": "调用图无 CALLS 边(空仓 / 全孤立符号),算不出排名。"}

        ranked = sorted(scores, key=lambda n: -scores[n])  # PageRank 降序
        # 批量富化 file/line/kind(_batch_get_nodes 自带 SQLite 变量数分批,大图安全)
        meta = {nd.qualified_name: nd for nd in self._store._batch_get_nodes(set(ranked))}

        chosen: list[tuple[str, Any, float]] = []  # (qualified_name, node, score)
        tokens = 0
        files: dict[str, list[str]] = {}
        rankable = 0  # 有元数据、能进地图的符号总数(truncated 判定用)
        excluded = 0  # 被噪声过滤掉的符号数(诚实信号:榜单少了谁要可见)
        for qn in ranked:
            nd = meta.get(qn)
            if nd is None or not getattr(nd, "file_path", None):
                continue
            fpath = nd.file_path
            if exclude_tests and is_noise_path(fpath):
                excluded += 1
                continue  # 不进 rankable:它们本来就不该出现在这张地图上
            rankable += 1
            # 显示名 = 剥路径前缀后的可读名(同渲染器 _sym 的剥法);token 估算用它才贴近实际渲染,
            # 不会因 CRG 的绝对路径前缀把估算撑爆 → 早停少装(踩坑:估算用全长 qn 会虚高 ~2.5x)。
            disp = qn[len(fpath) + 2:] if qn.startswith(fpath + "::") else qn.split("::")[-1]
            kind = getattr(nd, "kind", "?")
            lineno = getattr(nd, "line_start", "?")
            est = len(f"{disp} ({kind}) L{lineno} pr={scores[qn]:.3f}") // 4 + 1
            if fpath not in files:  # 新文件首次出现:再加「文件头行 + 空行分隔」的 token 成本
                est += len(os.path.basename(fpath)) // 4 + 2
            if tokens + est > map_tokens:
                break  # 超预算停(贪心:PageRank 高的先进,故截掉的都是相对不重要的)
            chosen.append((qn, nd, scores[qn]))
            tokens += est
            files.setdefault(fpath, []).append(qn)

        map_text = _render_repomap_tree(files, meta, scores)
        note = f"已过滤 {excluded} 个测试/仿真/生成文件符号(exclude_tests=True)" if excluded else ""
        return {
            "repo": self.repo_name,
            "map_text": map_text,
            "n_symbols": len(chosen),
            "n_files": len(files),
            "map_tokens_budget": map_tokens,
            "map_tokens_used": tokens,
            "truncated": len(chosen) < rankable,  # 有能排的符号但被预算截了
            "top_symbols": [{"qualified_name": qn, "file": nd.file_path,
                             "pagerank": round(sc, 6)} for qn, nd, sc in chosen[:10]],
            "note": note,
        }

    # ── P-A 1b 批量聚合用的改动分析(扩 wrap CRG changes.py,R4.1.2)──────────────

    def analyze_changes(self, changed_files: list[str], *,
                        changed_ranges: dict[str, list[tuple[int, int]]] | None = None,
                        repo_root: str | None = None, base: str = "HEAD~1",
                        include_churn: bool = False) -> dict:
        """改动分析(批量 PR 聚合用):一批改动文件 → 风险分 + 改动函数 + 受影响流 + 测试缺口 + 复审优先级。

        wrap CRG `analyze_changes`(changes.py):六因子 ``risk_score``(flow 参与 / 社区跨越 / 测试覆盖 /
        SECURITY_KEYWORDS 名字命中 +0.20 / 调用方数 / 改动频率)+ ``changed_functions``(每函数带 risk)+
        ``affected_flows`` + ``test_gaps`` + ``review_priorities``(top-10 by risk)。图里没有的文件 → 空结果,不崩。
        给每条 PR 算一个 risk_score 用于安全分层(高风险/security 子集才送 LLM 深 CWE 分类,省 token)。

        changed_ranges:``{file: [(start,end),...]}`` 行范围(从 PR diff 的 hunk 算)。给了直接用;
            没给 + 给了 repo_root → CRG 跑 ``git diff <base>`` 自己解(本机非 git 仓或想用 PR diff 时传这个)。
        """
        from code_review_graph.changes import analyze_changes as _crg_analyze

        return _crg_analyze(self._store, changed_files, changed_ranges=changed_ranges,
                            repo_root=repo_root, base=base, include_churn=include_churn)

    def community_ids_for(self, qualified_names: list[str]) -> dict[str, int | None]:
        """批量查「符号 → 社区(module)」映射(批量 PR 聚合按 module 分桶用)。

        wrap CRG ``GraphStore.get_community_ids_by_qualified_names``(graph.py,批量 450)。返
        ``{qualified_name: community_id}``;community_id 相同的符号归同一模块/社区。图缺或符号不在图 → 该项 None。
        """
        if not qualified_names:
            return {}
        return self._store.get_community_ids_by_qualified_names(qualified_names)

    def stats(self) -> dict:
        """图统计(节点/边数等)给报告元数据用。GraphStats 形状以 CRG 版本为准,这里宽容转 dict。"""
        import dataclasses

        s = self._store.get_stats()
        # is_dataclass 对「实例」和「类」都返 True;排除类(只要实例),asdict 才接受。
        if dataclasses.is_dataclass(s) and not isinstance(s, type):
            return dataclasses.asdict(s)
        return {"raw": str(s)}

