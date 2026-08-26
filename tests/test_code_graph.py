"""code_graph.py 测试(R3.2)。

两类:
- 单元:CRG 缺 extra 时 _require_crg 抛清晰错;open() 对未建 db 抛 FileNotFoundError。
- 集成:小仓真跑 CRG full_build → 各查询返回结构正确(慢 ~1-2s,但验端到端)。

CRG 是可选 extra:没装时集成测自动 skip。
"""

from __future__ import annotations

import importlib.util

import pytest

from rootrecall.services.code_index.code_graph import CodeGraph, _require_crg


def _crg_installed() -> bool:
    return importlib.util.find_spec("code_review_graph") is not None


needs_crg = pytest.mark.skipif(not _crg_installed(), reason="需要 code-review-graph extra")


# ── 单元 ──────────────────────────────────────────────────────────────────


def test_require_crg_missing_raises(monkeypatch):
    """CRG 没装时 _require_crg 抛 ImportError 且带安装指引。"""
    import rootrecall.services.code_index.code_graph as cg

    real = cg.importlib.util.find_spec

    def fake(name, *args, **kwargs):
        return None if name == "code_review_graph" else real(name, *args, **kwargs)

    monkeypatch.setattr(cg.importlib.util, "find_spec", fake)
    with pytest.raises(ImportError, match="code-review-graph"):
        _require_crg()


@needs_crg
def test_open_missing_db_raises(tmp_path):
    """open() 对未建的 db 抛 FileNotFoundError(不默默建空图)。"""
    with pytest.raises(FileNotFoundError):
        CodeGraph.open("definitely_not_built_repo_xyz", base_dir=str(tmp_path))


# ── 集成(真跑 CRG full_build)────────────────────────────────────────────


@needs_crg
def test_build_and_query_small_repo(tmp_path):
    """小仓建图 + 各查询返回结构正确。

    造一个有调用/包含关系的小 .py 仓(跨文件 import + 函数互调 + 一个类),足够产出节点/边/社区。
    """
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return beta() + gamma()\n"
        "def beta():\n    return 1\n"
        "def gamma():\n    return delta()\n"
        "def delta():\n    return 0\n"
        "class Foo:\n    def method(self):\n        return alpha()\n"
    )
    (tmp_path / "b.py").write_text(
        "from a import alpha, beta\n"
        "def caller():\n    return alpha() + beta()\n"
    )

    cg = CodeGraph.build(tmp_path, "fixture", base_dir=str(tmp_path))

    # stats 有节点/边
    s = cg.stats()
    assert s["total_nodes"] > 0
    assert s["total_edges"] > 0

    # communities 是 list(小仓可能就几个,不强断言数量)
    assert isinstance(cg.communities(), list)

    # architecture_overview 三个键齐全
    ov = cg.architecture_overview()
    assert {"communities", "cross_community_edges", "warnings"} <= set(ov.keys())

    # hub/bridge 返回 list 且不超过 top_n
    hubs = cg.hub_nodes(top_n=5)
    assert isinstance(hubs, list) and len(hubs) <= 5
    if hubs:
        assert "total_degree" in hubs[0] and "qualified_name" in hubs[0]
    bridges = cg.bridge_nodes(top_n=5)
    assert isinstance(bridges, list) and len(bridges) <= 5

    # open() 能复用刚建的图(不重建)
    cg2 = CodeGraph.open("fixture", base_dir=str(tmp_path))
    assert cg2.stats()["total_nodes"] == s["total_nodes"]


@needs_crg
def test_impact_radius_path_resolution(tmp_path):
    """blast_radius 路径容错:CRG 存路径带 repo_root 前缀,agent 给仓库相对路径也要能命中。

    蓝芷(bluez)实测踩到:index 用相对 repo_root → 图存 ``<tmp>/a.py``,agent 喂 ``a.py``
    会静默返空。_resolve_file_paths 用后缀匹配兜底。这个测固化该行为。
    """
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return beta()\n"
        "def beta():\n    return 1\n"
    )
    (tmp_path / "b.py").write_text(
        "from a import alpha\n"  # 跨文件调用 → a.py 改动会波及 b.py
        "def caller():\n    return alpha()\n"
    )
    cg = CodeGraph.build(tmp_path, "fixture_ir", base_dir=str(tmp_path))

    # 仓库相对路径(只是 basename)→ 后缀解析到 <tmp>/a.py,非空
    br_rel = cg.impact_radius(["a.py"])
    assert len(br_rel["changed_nodes"]) > 0, "相对路径 a.py 应解析到图里的 <tmp>/a.py"

    # 精确的全路径(图存的格式)→ 也命中(精确分支)
    br_abs = cg.impact_radius([str(tmp_path / "a.py")])
    assert len(br_abs["changed_nodes"]) > 0

    # 两种喂法命中同一批 changed 节点(changed_nodes 是 GraphNode 对象,属性访问)
    qn = lambda nodes: {getattr(n, "qualified_name", None) for n in nodes}  # noqa: E731
    assert qn(br_rel["changed_nodes"]) == qn(br_abs["changed_nodes"])

    # 不存在的文件 → 如实返空(不假装命中)
    br_none = cg.impact_radius(["does_not_exist.c"])
    assert br_none["changed_nodes"] == [] and br_none["total_impacted"] == 0


@needs_crg
def test_analyze_changes_and_community_ids(tmp_path):
    """analyze_changes(改动文件+行范围 → risk/changed_functions)+ community_ids_for(符号→社区)。P-A 1b 用。

    给 changed_ranges(从 PR diff hunk 算的形态),不靠 git diff —— 跟 1b 实际用法一致。
    """
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return beta()\n"
        "def beta():\n    return 1\n"
        "def gamma():\n    return alpha()\n"
    )
    cg = CodeGraph.build(tmp_path, "fixture_ac", base_dir=str(tmp_path))

    # analyze_changes:改动 a.py 的 1-6 行(覆盖 alpha/beta/gamma)→ CRG 映射到这些函数节点。
    ac = cg.analyze_changes(["a.py"], changed_ranges={"a.py": [(1, 6)]})
    assert isinstance(ac, dict)
    # CRG analyze_changes 的返回键(risk_score/changed_functions/affected_flows/review_priorities)。
    assert "risk_score" in ac and "changed_functions" in ac
    assert isinstance(ac["risk_score"], float)
    assert isinstance(ac["changed_functions"], list)

    # community_ids_for:CRG 的 qualified_name 是「绝对路径::符号」格式(如 .../a.py::alpha)。
    # 1b 实际用法:qn 来自 analyze_changes 的 changed_functions(格式一致),再查社区按 module 分桶。
    qns = [f["qualified_name"] for f in ac.get("changed_functions", []) if f.get("qualified_name")]
    assert qns, "analyze_changes 应映射到 a.py 的函数(alpha/beta/gamma)"
    cmap = cg.community_ids_for(qns)
    assert isinstance(cmap, dict)
    assert all(qn in cmap for qn in qns)  # 查询的 qn 都是 key


@needs_crg
def test_call_chain_small_repo(tmp_path):
    """call_chain:符号中心的 N 跳调用链(仅 CALLS 边)+ PageRank 重要度(P1.5 caller/callee 进适配层)。

    同款小仓 fixture(alpha→beta/gamma,gamma→delta,Foo.method→alpha,b.caller→alpha/beta):
    解析符号 → 建 CALLS 子图 → PageRank → 双向有界 BFS → enrich 节点 → 组装截断。
    callers/callees 可能为空(CRG 对 Python 的 CALLS 边提取视解析器而定),故断言结构 + 字段齐全,
    不强断言非空(空则覆盖「种子无 CALLS 边」分支,也是合法返回)。
    """
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return beta() + gamma()\n"
        "def beta():\n    return 1\n"
        "def gamma():\n    return delta()\n"
        "def delta():\n    return 0\n"
        "class Foo:\n    def method(self):\n        return alpha()\n"
    )
    (tmp_path / "b.py").write_text(
        "from a import alpha, beta\n"
        "def caller():\n    return alpha() + beta()\n"
    )
    cg = CodeGraph.build(tmp_path, "fixture_cc", base_dir=str(tmp_path))

    res = cg.call_chain("alpha", direction="both", depth=2, top_n=10)
    # 顶层结构齐全:键都在,callers/callees 是 list(可能空但不能缺),resolved 非空(符号解析到了)
    assert res["symbol"] == "alpha"
    assert res["resolved"], "alpha 应解析到图节点"
    assert res["direction"] == "both" and res["depth"] == 2
    assert isinstance(res["callers"], list) and isinstance(res["callees"], list)
    assert isinstance(res["truncated"], bool) and "note" in res
    # 每个节点字段齐全(qualified_name/file/line/kind/hop/pagerank);hop ∈ [1, depth]
    for side in ("callers", "callees"):
        for nd in res[side]:
            assert nd["qualified_name"]
            assert all(k in nd for k in ("file", "line", "kind", "hop", "pagerank"))
            assert 1 <= nd["hop"] <= 2
            assert isinstance(nd["pagerank"], float)
    # 不存在的符号 → ValueError(工具层据此转友好串)
    with pytest.raises(ValueError):
        cg.call_chain("no_such_function_zzz")


@needs_crg
def test_call_chain_bad_direction(tmp_path):
    """非法 direction → ValueError(call_chain 的输入校验;工具层兜底的来源)。"""
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    cg = CodeGraph.build(tmp_path, "fixture_dir", base_dir=str(tmp_path))
    with pytest.raises(ValueError, match="direction"):
        cg.call_chain("alpha", direction="sideways")


# ── exclude_tests(P2,2026-08-26 实测:mgmt-tester 474 入边、ltmain.sh 度 1475 霸榜)──


@needs_crg
def test_hub_and_repomap_exclude_tests(tmp_path):
    """exclude_tests(默认开):test/ 目录符号不进 hub 榜与 repo_map;关掉则回全量(开关真通)。

    夹具:test/ 里造一个高 degree 噪声中心(20 个测试函数全调它 + 它反调 20 个),
    src/ 里是低 degree 的真核心 —— 不过滤时噪声中心必登榜首,过滤后应消失。
    """
    core = tmp_path / "src"
    core.mkdir()
    (core / "core.py").write_text(
        "def core_entry():\n    return helper_a() + helper_b()\n"
        "def helper_a():\n    return 1\n"
        "def helper_b():\n    return 2\n"
    )
    tdir = tmp_path / "test"
    tdir.mkdir()
    callers = "".join(f"def t{i}():\n    return noise_center()\n" for i in range(20))
    callees = "".join(f"    noise_leaf_{i}()\n" for i in range(20))
    (tdir / "test_noise.py").write_text(
        "def noise_center():\n" + callees + callers
    )
    cg = CodeGraph.build(tmp_path, "fixture_excl", base_dir=str(tmp_path))

    # 关掉开关:噪声中心(test/)登上 hub 榜首(度数 40,远超 core_entry 的 3)
    raw_hubs = cg.hub_nodes(top_n=5, exclude_tests=False)
    assert any("/test/" in (h.get("file") or "") for h in raw_hubs), raw_hubs

    # 默认开:test/ 符号全部消失,剩 src/ 的真核心
    hubs = cg.hub_nodes(top_n=5)
    assert hubs and all("/test/" not in (h.get("file") or "") for h in hubs), hubs
    assert any("core" in (h.get("file") or "") for h in hubs), hubs

    # repo_map 同理:默认 map_text 无 test_noise;关掉则出现;note 诚实报过滤量
    m = cg.repo_map(map_tokens=2048)
    assert "test_noise" not in (m.get("map_text") or "") and "过滤" in (m.get("note") or ""), m["note"]
    m_raw = cg.repo_map(map_tokens=2048, exclude_tests=False)
    assert "test_noise" in (m_raw.get("map_text") or "") and m_raw.get("note") == ""


# ── repo_map(PageRank 排名全仓符号地图,#38)────────────────────────────────


def test_render_repomap_tree_format():
    """_render_repomap_tree:纯函数,按文件分组 + PageRank 降序 + 树连接符(不需 CRG,快,恒跑)。

    造两个文件各俩符号 + 假分:验「文件按楼内最高分降序」「楼内按分降序」「树连接符 + pr= 分数格式」。
    """
    from types import SimpleNamespace

    from rootrecall.services.code_index.code_graph import _render_repomap_tree

    meta = {
        "a.py::alpha": SimpleNamespace(kind="function", line_start=1),
        "a.py::alpha2": SimpleNamespace(kind="function", line_start=10),
        "b.py::beta": SimpleNamespace(kind="function", line_start=5),
        "b.py::beta2": SimpleNamespace(kind="method", line_start=20),
    }
    # b.py 的 beta(0.3)是全仓最高 → b.py 段应排在 a.py 前
    scores = {"a.py::alpha": 0.10, "a.py::alpha2": 0.05, "b.py::beta": 0.30, "b.py::beta2": 0.20}
    files = {"a.py": ["a.py::alpha", "a.py::alpha2"], "b.py": ["b.py::beta", "b.py::beta2"]}
    out = _render_repomap_tree(files, meta, scores)
    lines = out.splitlines()

    def _idx_containing(sub: str) -> int:
        # 符号行带 ├── 前缀 + kind/L/pr 后缀,不是裸名 → 用包含匹配找行号
        for i, ln in enumerate(lines):
            if sub in ln:
                return i
        raise AssertionError(f"{sub!r} 不在输出: {lines}")

    # 文件段顺序:文件头是裸路径精确行;最高分符号所在的文件在前(b.py 0.30 > a.py 0.10)
    assert lines.index("b.py") < lines.index("a.py")
    # 路径前缀已剥:符号行只留 Class::symbol(beta/beta2),不再带 "b.py::"
    assert "b.py::" not in out and "a.py::" not in out
    # 楼内符号按分降序:beta(0.30) 在 beta2(0.20) 前(尾随空格防 beta 命中 beta2)
    assert _idx_containing("beta (") < _idx_containing("beta2 (")
    # 末符号 └──、其余 ├──;分数格式 pr=0.300 / pr=0.200
    assert "├── beta (function) L5 pr=0.300" in out
    assert "└── beta2 (method) L20 pr=0.200" in out


@needs_crg
def test_repo_map_small_repo(tmp_path):
    """repo_map:小仓整图 PageRank → 按文件分组树 + token 预算贪心裁剪(#38)。

    同 call_chain 测的小仓(alpha→beta/gamma,gamma→delta,Foo.method→alpha,b.caller→alpha/beta)。
    验结构齐全 + token 预算生效(小预算装的符号 ≤ 大预算)+ PageRank 分是 float。CALLS 边提取
    视 CRG 解析器而定,可能为空(空则覆盖「无 CALLS 边」分支,合法),故按 n_symbols 分支断言。
    """
    (tmp_path / "a.py").write_text(
        "def alpha():\n    return beta() + gamma()\n"
        "def beta():\n    return 1\n"
        "def gamma():\n    return delta()\n"
        "def delta():\n    return 0\n"
        "class Foo:\n    def method(self):\n        return alpha()\n"
    )
    (tmp_path / "b.py").write_text(
        "from a import alpha, beta\n"
        "def caller():\n    return alpha() + beta()\n"
    )
    cg = CodeGraph.build(tmp_path, "fixture_rm", base_dir=str(tmp_path))

    big = cg.repo_map(map_tokens=2048)
    # 顶层结构齐全
    assert big["repo"] == "fixture_rm"
    for k in ("map_text", "n_symbols", "n_files", "map_tokens_budget",
              "map_tokens_used", "truncated", "top_symbols", "note"):
        assert k in big, f"缺键 {k}"
    assert big["map_tokens_budget"] == 2048
    assert isinstance(big["truncated"], bool)

    if big["n_symbols"] > 0:  # 有 CALLS 边 → 验排名输出
        assert big["map_text"], "有符号就应有地图文本"
        assert big["n_files"] >= 1
        assert "pr=" in big["map_text"]  # 渲染了分数
        assert big["map_tokens_used"] <= big["map_tokens_budget"]  # 贪心不超预算
        assert len(big["top_symbols"]) <= 10
        for s in big["top_symbols"]:
            assert all(k in s for k in ("qualified_name", "file", "pagerank"))
            assert isinstance(s["pagerank"], float)
        # 小预算装的符号不多于大预算(预算生效);装不下全部 → truncated=True
        small = cg.repo_map(map_tokens=15)
        assert small["n_symbols"] <= big["n_symbols"]
        if small["n_symbols"] < big["n_symbols"]:
            assert small["truncated"] is True
    else:  # 无 CALLS 边(空地图分支)
        assert big["map_text"] == ""
        assert "CALLS" in big["note"]


@needs_crg
def test_repo_map_no_calls_empty(tmp_path):
    """无调用边的仓(单函数返常量)→ 期 CALLS 子图空 → 空地图 + note;不抛是硬要求。

    单函数 lonely 无调用 → CALLS 子图无边 → PageRank 返空 → 走空地图分支。若 CRG 意外造了边
    (n_symbols≠0),也接受 —— 只验「不抛 + 结构齐全」契约,不强绑死空分支。
    """
    (tmp_path / "solo.py").write_text("def lonely():\n    return 42\n")
    cg = CodeGraph.build(tmp_path, "fixture_empty", base_dir=str(tmp_path))
    res = cg.repo_map()  # 不抛即硬通过
    assert isinstance(res, dict) and "map_text" in res
    if res["n_symbols"] == 0:  # 走了空地图分支才验其契约
        assert res["map_text"] == ""
        assert "CALLS" in res["note"]


# ── cross_version_diff(模块级函数,feature 2b)──────────────────────────────


def test_cross_version_diff_small_git_repo(tmp_path):
    """cross_version_diff:同一 git 仓两 ref 间 —— base..head 提交 + concern diff(纯 git,不需 CRG);
    有 CRG/图时再验 touched_functions 富化。git 不在 PATH 则 skip。

    建 tmp git 仓:commit1 加 a.py(v1),commit2 改 a.py(v2)。cross_version_diff("HEAD~1","HEAD")。
    """
    import os
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def g(args):
        subprocess.run(["git", *args], cwd=str(tmp_path), env=env, check=True,
                       capture_output=True, text=True)

    g(["init", "-q"])
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "v1: add alpha"])
    (tmp_path / "a.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "v2: change alpha"])

    from rootrecall.services.code_index.code_graph import cross_version_diff

    # 纯 git 核(无图):refs / commits / concern_diff / patch_equivalence
    res = cross_version_diff("HEAD~1", "HEAD", repo_path=str(tmp_path),
                             concern_files=["a.py"])
    assert res["refs"]["base_sha"] and res["refs"]["head_sha"]
    assert res["refs"]["base_sha"] != res["refs"]["head_sha"]
    assert len(res["commits"]) == 1, res["commits"]
    assert "v2" in res["commits"][0]["subject"]
    assert res["concern_diff"], "应有 a.py 的 diff"
    assert "return 2" in res["concern_diff"]  # 改动行(+    return 2)
    assert {"new_in_head", "equivalent_in_base"} <= set(res["patch_equivalence"])

    # 没给 concern → 跳全量 diff(防回巨大 diff)+ note 提示
    res_full = cross_version_diff("HEAD~1", "HEAD", repo_path=str(tmp_path))
    assert res_full["concern_diff"] == ""
    assert "跳过全量 diff" in res_full["note"]

    # 有 CRG + 图:touched_functions 富化(alpha 在 a.py 且被 base..head diff 触及)
    if not _crg_installed():
        return  # 没装 CRG,git 核部分已验完,富化跳过(不 fail)
    cg = CodeGraph.build(tmp_path, "fixture_cvd", base_dir=str(tmp_path))
    res2 = cross_version_diff("HEAD~1", "HEAD", repo_path=str(tmp_path),
                              concern_files=["a.py"], graph=cg)
    assert isinstance(res2["touched_functions"], list)
    # 路径格式/CRG 解析容错:非空才断言结构 + note 含映射说明
    if res2["touched_functions"]:
        assert "qualified_name" in res2["touched_functions"][0]
        assert "touched_functions 映射" in res2["note"]


# ── merge_eval(上游 commit 合入评估,低优 #1)───────────────────────────────────


def test_merge_eval_three_states(tmp_path):
    """merge_eval:上游一段 commit 逐个评估三态(已修/建议合/冲突)。

    造 hermetic git 仓:main 共祖 → upstream 三 commit(U1 改 a.py / U2 加 c.py / U3 改 b.py)
    → fork 从 main 起:cherry-pick U1(patch-id 等价 → U1 已修)+ 改 b.py beta 成 50(U3 想改 1→99 上下文对不上 → 冲突)。
    期:U1=already_fixed · U2=recommend_merge(新文件干净 apply)· U3=conflict。
    apply 检查对当前 worktree,故全程留在 fork 分支上。git 不在 PATH 则 skip。
    """
    import os
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_EDITOR": "true"}

    def g(args):
        subprocess.run(["git", *args], cwd=str(tmp_path), env=env, check=True,
                       capture_output=True, text=True)

    # 共祖 main:a.py(alpha=1) + b.py(beta=1)
    g(["init", "-q"])
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def beta():\n    return 1\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "base"])
    g(["branch", "-m", "main"])  # 钳制基线分支名(跨 git 版本默认分支名不一)

    # upstream:U1(a.py alpha→2)/ U2(加 c.py gamma)/ U3(b.py beta→99)
    g(["checkout", "-q", "-b", "upstream"])
    (tmp_path / "a.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "U1 alpha returns 2"])
    (tmp_path / "c.py").write_text("def gamma():\n    return 0\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "U2 add gamma"])
    (tmp_path / "b.py").write_text("def beta():\n    return 99\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "U3 beta returns 99"])

    # fork:从 main 起,cherry-pick U1(upstream~2)+ 改 b.py beta→50(与 U3 冲突)
    g(["checkout", "-q", "main"])
    g(["checkout", "-q", "-b", "fork"])
    g(["cherry-pick", "upstream~2"])  # U1 → patch-id 等价
    (tmp_path / "b.py").write_text("def beta():\n    return 50\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "F2 beta returns 50"])
    # worktree 现在在 fork(already checked out)—— merge_eval 的 apply 检查对它

    from rootrecall.services.code_index.code_graph import merge_eval
    res = merge_eval("main", "upstream", fork_ref="fork", repo_path=str(tmp_path))

    # 顶层结构
    assert res["fork_ref"] == "fork"
    assert res["upstream_range"] == "main..upstream"
    assert res["summary"]["total"] == 3
    by_subj = {c["subject"]: c for c in res["commits"]}

    # U1 cherry-pick 过 → patch-id 等价 → already_fixed
    u1 = next(v for k, v in by_subj.items() if k.startswith("U1"))
    assert u1["equivalent_in_fork"] is True
    assert u1["state"] == "already_fixed"

    # U2 新文件 c.py,fork 没动 c.py → 干净 apply → recommend_merge
    u2 = next(v for k, v in by_subj.items() if k.startswith("U2"))
    assert u2["equivalent_in_fork"] is False
    assert u2["applies_cleanly"] is True
    assert u2["state"] == "recommend_merge"
    assert "c.py" in u2["touched_files"]

    # U3 改 b.py beta 1→99,fork 已把 beta 改 50 → 上下文冲突 → conflict
    u3 = next(v for k, v in by_subj.items() if k.startswith("U3"))
    assert u3["equivalent_in_fork"] is False
    assert u3["applies_cleanly"] is False
    assert u3["state"] == "conflict"
    assert "b.py" in u3["touched_files"]

    # summary 计数
    assert res["summary"]["already_fixed"] == 1
    assert res["summary"]["recommend_merge"] == 1
    assert res["summary"]["conflict"] == 1

    # CRG 可用时:touched_functions 富化是 list(结构对齐 cross_version_diff 测,宽松不断非空)
    if _crg_installed():
        cg = CodeGraph.build(tmp_path, "fixture_me", base_dir=str(tmp_path))
        res2 = merge_eval("main", "upstream", fork_ref="fork", repo_path=str(tmp_path), graph=cg)
        assert all(isinstance(c["touched_functions"], list) for c in res2["commits"])


def test_merge_eval_dirty_worktree_zero_touch(tmp_path):
    """merge-tree 升级(#6/backlog #60)核心回归:脏 worktree + 不 checkout fork,三态依然正确。

    老路(apply --check 对当前 worktree)在这种姿势下必失真——补丁对不上脏文件 → 全判 conflict。
    新路(merge-tree --write-tree 在对象库试合并)不受 worktree 状态影响。
    git < 2.38 无 merge-tree → 回退老路(此时该姿势本来就失真),skip 保断言精度。
    """
    import os
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    # merge-tree --write-tree 探测:git ≥ 2.38 才有(老 git 收到 --write-tree 报 usage)
    probe = subprocess.run(["git", "merge-tree", "--write-tree", "HEAD", "HEAD"],
                           capture_output=True, text=True, cwd=str(tmp_path))
    # tmp_path 非 git 仓时 HEAD 解析失败是 rc≠0 但 stderr 不同;先建仓再探,直接在下面建完探
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_EDITOR": "true"}

    def g(args):
        subprocess.run(["git", *args], cwd=str(tmp_path), env=env, check=True,
                       capture_output=True, text=True)

    g(["init", "-q"])
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "base"])
    g(["branch", "-m", "main"])
    # upstream 加新文件 c.py(fork 没动它 → 三方合并必干净)
    g(["checkout", "-q", "-b", "upstream"])
    (tmp_path / "c.py").write_text("def gamma():\n    return 0\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "U2 add gamma"])
    # fork:从 main 建(不 checkout 过去 —— 模拟「agent 没切 fork」的懒姿势;U2 不进 fork)
    g(["checkout", "-q", "main"])
    g(["branch", "fork"])
    # 关键姿势:停 main + 把 a.py 改脏(老路必失真:U2 的 diff 对脏树照样能 apply,
    # 但若 commit 触及 a.py 则必挂;这里用干净 c.py 隔离变量——失真与否看 merge-tree 是否被 worktree 干扰)
    (tmp_path / "a.py").write_text("def alpha():\n    return 999  # DIRTY\n", encoding="utf-8")
    # merge-tree 可用性探测(仓已建):老 git → skip
    probe = subprocess.run(["git", "merge-tree", "--write-tree", "main", "main"],
                           capture_output=True, text=True, cwd=str(tmp_path))
    if probe.returncode != 0:
        pytest.skip(f"git 无 merge-tree --write-tree(<2.38): {probe.stderr[:80]}")

    from rootrecall.services.code_index.code_graph import merge_eval
    res = merge_eval("main", "upstream", fork_ref="fork", repo_path=str(tmp_path))
    # U2 加 c.py 与 fork(main 态,没动 c.py)三方合并干净 → recommend_merge;
    # 脏 a.py 不影响(worktree 状态与对象库合并无关)
    assert res["summary"]["recommend_merge"] == 1, res
    assert res["summary"]["conflict"] == 0, res
    assert "merge-tree 不可用" not in res["note"]  # 走的是新路,没回退


def test_merge_eval_empty_range(tmp_path):
    """upstream_base..upstream_head 无 commit → 空结果 + 提示。"""
    import os
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def g(args):
        subprocess.run(["git", *args], cwd=str(tmp_path), env=env, check=True,
                       capture_output=True, text=True)
    g(["init", "-q"])
    (tmp_path / "x.py").write_text("def f():\n    return 0\n", encoding="utf-8")
    g(["add", "-A"])
    g(["commit", "-q", "-m", "base"])
    g(["branch", "-m", "main"])

    from rootrecall.services.code_index.code_graph import merge_eval
    res = merge_eval("main", "main", fork_ref="main", repo_path=str(tmp_path))
    assert res["summary"]["total"] == 0
    assert res["commits"] == []


def test_merge_eval_not_a_repo(tmp_path):
    """非 git 仓目录 → ValueError(工具层据此转友好串)。"""
    from rootrecall.services.code_index.code_graph import merge_eval
    empty = tmp_path / "notarepo"
    empty.mkdir()
    with pytest.raises(ValueError):
        merge_eval("a", "b", fork_ref="c", repo_path=str(empty))


# ── 增量刷新(D:接 CRG incremental machinery)─────────────────────────────


def _git(repo, *args):
    """在 repo 里跑一条 git;失败即炸(测试夹具,不吞错)。坑#21:一律 git -C,不 cd。"""
    import subprocess
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com",
                    "-c", "user.name=t", *args], check=True, capture_output=True, text=True)


def _make_repo(repo):
    """两个 .py 文件的小仓:跨文件 import + 函数互调,足够产出节点/边/社区。"""
    repo.mkdir(parents=True)
    (repo / "a.py").write_text(
        "def alpha():\n    return beta() + gamma()\n"
        "def beta():\n    return 1\n"
        "def gamma():\n    return 0\n"
    )
    (repo / "b.py").write_text(
        "from a import alpha\n"
        "def caller():\n    return alpha()\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "-m", "main")


@needs_crg
def test_update_full_rebuild_when_no_db(tmp_path):
    """图未建 → update() 兜底走全量 build,返回 mode=full_rebuild。"""
    repo = tmp_path / "repo"
    _make_repo(repo)
    graphs = tmp_path / "graphs"

    cg, summary = CodeGraph.update(repo, "t", base_dir=str(graphs))
    assert summary["mode"] == "full_rebuild"
    assert cg.stats()["total_nodes"] > 0
    # 全量建完应留 built_head 快照(增量下次才有基准)
    assert (graphs / "t" / "built_head").exists()


@needs_crg
def test_update_incremental_after_edit(tmp_path):
    """建图后改文件 + 加未跟踪文件 → update() 只增量重解析,新符号进图、旧节点不丢。"""
    repo = tmp_path / "repo"
    _make_repo(repo)
    graphs = tmp_path / "graphs"  # 图放仓外:db/快照别混进 git 未跟踪清单

    cg = CodeGraph.build(repo, "t", base_dir=str(graphs))
    n0 = cg.stats()["total_nodes"]

    # 改动面 = 已跟踪文件的修改 + 未跟踪新文件(两条来源都该进增量清单)
    (repo / "a.py").write_text(
        "def alpha():\n    return beta() + gamma()\n"
        "def beta():\n    return 1\n"
        "def gamma():\n    return 0\n"
        "def epsilon():\n    return alpha()\n"
    )
    (repo / "c.py").write_text("from a import beta\ndef zeta():\n    return beta()\n")

    cg2, summary = CodeGraph.update(repo, "t", base_dir=str(graphs))
    assert summary["mode"] == "incremental"
    assert summary["files_updated"] >= 2  # a.py(修改)+ c.py(未跟踪)
    # 新函数带来新节点(增量解析真的发生了,不是空跑)
    assert cg2.stats()["total_nodes"] > n0


@needs_crg
def test_update_noop_when_untouched(tmp_path):
    """建图后没动过 → update() 是 noop,节点数不变。"""
    repo = tmp_path / "repo"
    _make_repo(repo)
    graphs = tmp_path / "graphs"

    cg = CodeGraph.build(repo, "t", base_dir=str(graphs))
    cg2, summary = CodeGraph.update(repo, "t", base_dir=str(graphs))
    assert summary["mode"] == "noop"
    assert cg2.stats()["total_nodes"] == cg.stats()["total_nodes"]


@needs_crg
def test_update_non_git_falls_back_to_full(tmp_path):
    """非 git 仓:build 不写快照(无 HEAD)→ update() 退回全量重建(增量没基准)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def alpha():\n    return 0\n")
    graphs = tmp_path / "graphs"

    CodeGraph.build(repo, "t", base_dir=str(graphs))
    assert not (graphs / "t" / "built_head").exists()  # 非 git 仓本就不写快照

    _, summary = CodeGraph.update(repo, "t", base_dir=str(graphs))
    assert summary["mode"] == "full_rebuild"
