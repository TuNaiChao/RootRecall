"""harness 转向 D0/D1:MCP 工具 blast_radius + validate_patch + export_patch + export_report 单测。

不真起 transport —— 用 FastMCP.call_tool(name, dict) 直接调工具闭包(call_tool 返回
([TextContent,...], structured),取第一个 TextContent.text 拿工具返回的 str)。验包装逻辑
+ 优雅降级(图未建/后端未装不抛,validate 路径不存在/garbage 补丁不抛)。
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from rootrecall.tools.mcp_memory import build_server


def _call(mcp, name: str, args: dict) -> str:
    """调一个工具,取回它的 str 结果。"""
    blocks, _structured = asyncio.run(mcp.call_tool(name, args))
    return blocks[0].text


def _git_repo(path) -> None:
    """建个真 git 仓 + 一个 commit(validate_patch 的 git apply --check 要在 git 仓里跑)。"""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "f.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


# ════════════════════════ validate_patch 工具 ════════════════════════

def test_validate_patch_not_a_dir():
    """repo_path 不存在 → 友好提示,不抛。"""
    mcp = build_server()
    out = _call(mcp, "validate_patch", {"patch": "x", "repo_path": "/no/such/dir/xyz_abc"})
    assert "不是目录" in out


def test_validate_patch_applies_clean(tmp_path):
    """真 git 仓 + 合法 forward 补丁 → applies=True。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    # 改一行 → git diff 出 forward 补丁 → checkout 还原:补丁对该仓(HEAD 态)应干净 apply
    (repo / "f.c").write_text("int main(void){return 1;}\n", encoding="utf-8")
    diff = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "-C", str(repo), "checkout", "--", "f.c"], check=True)
    assert diff.strip(), "测试夹具:没生成 diff"

    mcp = build_server()
    out = _call(mcp, "validate_patch", {"patch": diff, "repo_path": str(repo)})
    assert "applies=True" in out, out
    assert "✅" in out


def test_validate_patch_garbage(tmp_path):
    """garbage 补丁 → 三条降级路径全挂 → applies=False。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    mcp = build_server()
    out = _call(mcp, "validate_patch", {"patch": "this is not a diff at all", "repo_path": str(repo)})
    assert "applies=False" in out, out


# ════════════════════════ blast_radius 工具 ═════════════════════════

def test_blast_radius_empty_input():
    """没传 changed_files → 提示,不查图。"""
    mcp = build_server()
    out = _call(mcp, "blast_radius", {"changed_files": []})
    assert "未传 changed_files" in out


def _known_nonempty(monkeypatch):
    """让近义解析层确定走「没有叫 X」分支:本机已知集固定为一个无关名(防真机 data 漏进测试)。"""
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"some-other-cb": {"index"}})


def test_blast_radius_not_built(monkeypatch):
    """图未建(或 code-review-graph 后端未装)→ 优雅返回提示串,绝不漏 traceback。"""
    _known_nonempty(monkeypatch)
    mcp = build_server()
    out = _call(mcp, "blast_radius",
                {"changed_files": ["src/x.c"], "codebase": "nonexistent_xyz_repo_42"})
    assert "Traceback" not in out, out
    # 友好提示之一:图未建 / 后端未装 / 失败 / 近义容错(没有叫 / 匹配到多个)
    assert any(k in out for k in ("未建", "不可用", "失败", "没有叫", "匹配到多个")), out


# ════════════════════════ call_chain 工具 ══════════════════════════

def test_call_chain_not_built(monkeypatch):
    """图未建(或 CRG 后端未装)→ 优雅返回提示串,绝不漏 traceback(策略同 blast_radius)。"""
    _known_nonempty(monkeypatch)
    mcp = build_server()
    out = _call(mcp, "call_chain",
                {"symbol": "some_function", "codebase": "nonexistent_xyz_repo_42"})
    assert "Traceback" not in out, out
    # 友好提示之一:图未建 / 后端未装 / 失败 / 近义容错(没有叫 / 匹配到多个)
    assert any(k in out for k in ("未建", "不可用", "失败", "没有叫", "匹配到多个")), out


def test_call_chain_bad_direction(monkeypatch):
    """非法 direction → CodeGraph.call_chain 抛 ValueError → 工具转友好串,不抛 traceback。

    monkeypatch CodeGraph.open 返一个 call_chain 必抛 ValueError 的假图,直测工具的 ValueError 兜底
    (不靠真图,hermetic;真图缺失时 direction 校验根本到不了,故必须注入)。
    """
    import rootrecall.services.code_index.code_graph as cg_mod

    class _FakeGraph:
        def call_chain(self, *a, **kw):  # noqa: ANN002,ANN003 —— 假对象,签名宽松
            raise ValueError("direction 需为 callers / callees / both,收到 'sideways'")

    # 替掉 classmethod open:经类访问的普通函数不绑 cls,CodeGraph.open(target) → 假图。
    # (**kw:工具层现在传 base_dir=reanchor 路径;known_codebases 认得该名,过近义解析层)
    monkeypatch.setattr(cg_mod.CodeGraph, "open", lambda target, **kw: _FakeGraph())
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"fake_cb": {"graph"}})
    mcp = build_server()
    out = _call(mcp, "call_chain", {"symbol": "foo", "direction": "sideways", "codebase": "fake_cb"})
    assert "Traceback" not in out, out
    assert "没法算" in out, out  # ValueError 被工具兜底成友好串


# ════════════════════════ repo_map 工具(#38)════════════════════════

def test_repo_map_not_built(monkeypatch):
    """图未建(或 CRG 后端未装)→ 优雅返回提示串,绝不漏 traceback(策略同 call_chain)。"""
    _known_nonempty(monkeypatch)
    mcp = build_server()
    out = _call(mcp, "repo_map", {"codebase": "nonexistent_xyz_repo_42"})
    assert "Traceback" not in out, out
    assert any(k in out for k in ("未建", "不可用", "失败", "没有叫", "匹配到多个")), out


def test_repo_map_success_via_fake_graph(monkeypatch):
    """happy path:假图 repo_map 返固定 dict → 工具格式化「N symbols / M files」+ body;map_tokens 透传。

    monkeypatch CodeGraph.open 返假图(不靠真图,hermetic):直测工具壳的格式化 + map_tokens/per-call codebase 透传。
    """
    import rootrecall.services.code_index.code_graph as cg_mod

    seen: dict = {}

    class _FakeGraph:
        def repo_map(self, *, map_tokens: int = 2048, exclude_tests: bool = True):  # noqa: ANN002
            seen["map_tokens"] = map_tokens
            seen["exclude_tests"] = exclude_tests
            return {"repo": "fake", "map_text": "f.c\n└── main (function) L1 pr=0.500",
                    "n_symbols": 1, "n_files": 1, "map_tokens_budget": map_tokens,
                    "map_tokens_used": 8, "truncated": False,
                    "top_symbols": [{"qualified_name": "f.c::main", "file": "f.c", "pagerank": 0.5}],
                    "note": ""}

    monkeypatch.setattr(cg_mod.CodeGraph, "open", lambda target, **kw: _FakeGraph())
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"fake_cb": {"graph"}})
    mcp = build_server()
    out = _call(mcp, "repo_map", {"map_tokens": 512, "codebase": "fake_cb"})
    assert "Traceback" not in out, out
    assert "1 symbols / 1 files" in out, out
    assert "f.c::main" in out  # body(json)含 top_symbols
    assert seen["map_tokens"] == 512  # map_tokens 透传到 repo_map


# ════════════════════════ 诚实截断(踩坑 #19 同源治理,2026-08-14)════════════════════════
# 旧病:图/git 类工具 body 一律 [:8000] 静默丢尾。修法:_honest_truncate —— 超长才截,
# 尾部明说截了多少 + 怎么补取。两条锁行为:helper 本体(短不截/长截+note)+ 工具壳集成。

def test_honest_truncate_short_body_passthrough():
    """未超限 → 原样返回,零 note 零噪音(绝大多数调用走这条,不能白加一行提示)。"""
    from rootrecall.tools.mcp_memory import _honest_truncate

    body = '{"k": "v"}'
    out = _honest_truncate(body, 8000, how_to_refetch="重调")
    assert out == body


def test_repo_map_truncation_note_via_fake_graph(monkeypatch):
    """超长 body → 截断 + 尾部 note(明说截了多少字符 + 补取路径),不再静默丢尾。

    假图塞一个 >8000 字符的 map_text 触发截断;断言 note 出现且总长被钳在限内。
    """
    import rootrecall.services.code_index.code_graph as cg_mod

    class _BigGraph:
        def repo_map(self, *, map_tokens: int = 2048, exclude_tests: bool = True):  # noqa: ANN002
            return {"repo": "fake", "map_text": "x" * 9000,
                    "n_symbols": 900, "n_files": 9, "map_tokens_budget": map_tokens,
                    "map_tokens_used": 9000, "truncated": True,
                    "top_symbols": [{"qualified_name": "f.c::main", "file": "f.c", "pagerank": 0.5}],
                    "note": ""}

    monkeypatch.setattr(cg_mod.CodeGraph, "open", lambda target, **kw: _BigGraph())
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"fake_cb": {"graph"}})
    mcp = build_server()
    out = _call(mcp, "repo_map", {"codebase": "fake_cb"})
    assert "Traceback" not in out, out
    assert "[截断" in out and "减小 map_tokens" in out, out  # note:截断事实 + 补取路径
    # 总长被钳在限内(header 一行 + body 8000)
    assert len(out) <= 8000 + 200, len(out)

def test_repo_map_compact_and_exclude_passthrough(monkeypatch):
    """compact=True:只出 header + map_text 树 + top-10 名单(不吐全量 JSON);exclude_tests 透传到图。

    P2(2026-08-26 实测):大仓 repo_map 全量 JSON 8000+ 字符密度低 —— map_text 本身就是
    紧凑形态;exclude_tests 治 *-tester/生成文件霸榜(mgmt-tester 474 入边、ltmain.sh 度 1475)。
    """
    import rootrecall.services.code_index.code_graph as cg_mod

    seen: dict = {}

    class _FakeGraph:
        def repo_map(self, *, map_tokens: int = 2048, exclude_tests: bool = True):  # noqa: ANN001
            seen["map_tokens"] = map_tokens
            seen["exclude_tests"] = exclude_tests
            return {"repo": "fake", "map_text": "src/core.c\n└── core_entry (function) L10 pr=0.500",
                    "n_symbols": 12, "n_files": 3, "map_tokens_budget": map_tokens,
                    "map_tokens_used": 40, "truncated": False,
                    "top_symbols": [{"qualified_name": "src/core.c::core_entry", "file": "src/core.c", "pagerank": 0.5},
                                    {"qualified_name": "test/mgmt-tester.c::main", "file": "test/mgmt-tester.c", "pagerank": 0.4}],
                    "note": "已过滤 88 个测试/仿真/生成文件符号(exclude_tests=True)"}

    monkeypatch.setattr(cg_mod.CodeGraph, "open", lambda target, **kw: _FakeGraph())
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"fake_cb": {"graph"}})
    mcp = build_server()
    out = _call(mcp, "repo_map", {"map_tokens": 512, "codebase": "fake_cb", "compact": True, "exclude_tests": False})
    assert seen["exclude_tests"] is False          # 开关透传到图层
    assert "map_text" not in out and '"top_symbols"' not in out, out  # 无 JSON 全量
    assert "src/core.c" in out and "core_entry" in out                    # 树 + top-10 在
    assert "top-10: core_entry" in out, out
    assert "已过滤 88 个" in out, out                                      # 过滤量诚实可见


# ════════════════════════ repo_overview 工具(#14,onboarding skill 主数据源)════════════════════════

def test_repo_overview_not_built(monkeypatch):
    """图未建(或 CRG 后端未装)→ 优雅返回提示串,绝不漏 traceback(策略同 repo_map)。"""
    _known_nonempty(monkeypatch)
    mcp = build_server()
    out = _call(mcp, "repo_overview", {"codebase": "nonexistent_xyz_repo_42"})
    assert "Traceback" not in out, out
    # 友好提示之一:图未建 / 后端未装 / 失败 / 近义容错(没有叫 / 匹配到多个)
    assert any(k in out for k in ("未建", "不可用", "失败", "没有叫", "匹配到多个")), out


def test_repo_overview_success_via_fake_graph(monkeypatch):
    """happy path:假图三方法返固定 dict → 工具聚合+格式化 header + body;top_n 透传到 hub/bridge。

    monkeypatch CodeGraph.open 返假图(不靠真图,hermetic):直测工具壳的「四方法聚合成一个 dict」
    + header 格式化(communities/hubs/bridges/告警计数)+ top_n 透传。注意:工具用 arch['communities']
    取社区(architecture_overview 内部已调 get_communities),故假图不单写 communities() 方法。
    warnings 用 list[str] 匹配真实 CRG(communities.py:1079-1082 拼 "High coupling ..." 串)。
    """
    import rootrecall.services.code_index.code_graph as cg_mod

    seen: dict = {}

    class _FakeGraph:
        def architecture_overview(self):
            return {"communities": [{"id": 0, "name": "core", "members": ["main"], "cohesion": 0.8},
                                    {"id": 1, "name": "util", "members": ["helper"], "cohesion": 0.7}],
                    "cross_community_edges": [{"source_community": 0, "target_community": 1}],
                    "warnings": ["High coupling (12 edges) between 'core' and 'util'"]}

        def hub_nodes(self, *, top_n: int = 15, exclude_tests: bool = True):
            seen["top_n"] = top_n
            seen["hub_exclude_tests"] = exclude_tests
            return [{"name": "main", "qualified_name": "f.c::main", "kind": "function",
                     "file": "f.c", "in_degree": 5, "out_degree": 3, "total_degree": 8, "community_id": 0}]

        def bridge_nodes(self, *, top_n: int = 15, exclude_tests: bool = True):
            return [{"name": "bridge_x", "qualified_name": "g.c::bridge_x",
                     "betweenness": 0.9, "community_id": 0}]

    monkeypatch.setattr(cg_mod.CodeGraph, "open", lambda target, **kw: _FakeGraph())
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"fake_cb": {"graph"}})
    mcp = build_server()
    out = _call(mcp, "repo_overview", {"top_n": 8, "codebase": "fake_cb"})
    assert "Traceback" not in out, out
    assert "2 communities / 1 hubs / 1 bridges" in out, out       # header 计数
    assert "1 高耦合告警" in out, out                              # warnings 非空 → 告警计数
    assert "f.c::main" in out                                     # body(json)含 hub
    assert seen["top_n"] == 8                                     # top_n 透传到 hub_nodes
    assert seen.get("hub_exclude_tests") is True                  # exclude_tests 默认开、透传到图


def test_repo_overview_large_repo_caps_communities_and_keeps_hubs(monkeypatch):
    """大仓(社区爆量)治截断:onboarding e2e 暴露 wpa 746 社区撑满 8000 截断 → hub/bridge 取不到。

    3 个硬要求:① 社区只留 max_communities 个最大的(header 仍诚实报真实总数);② members 压成
    member_count + 样本(不堆全量 qn);③ hub/bridge/warnings 排在 communities 前 —— 即便末尾社区被
    截断,架构最关键的枢纽/咽喉/告警也不丢;④ 截断有显式 note(诚实信号,不静默丢)。
    """
    import rootrecall.services.code_index.code_graph as cg_mod

    # 50 个社区,每个塞 100 个 member qn → 模拟大仓 bulky communities(不压会爆截断)。
    # 用长 name + 长 description 让即便压成 member_count+5样本后,30 个社区仍超 12000 → 触发截断路径。
    many_communities = [
        {"id": i, "name": f"module_{i}_very_long_name_for_bloat",
         "size": 100 - i, "cohesion": 0.5,
         "description": f"detailed module description {i} padded out to be long " * 6,
         "members": [f"f.c::symbol_{i}_{j}" for j in range(100)]}
        for i in range(50)
    ]

    class _FakeGraph:
        def architecture_overview(self):
            return {"communities": many_communities,
                    "cross_community_edges": [{"source_community": 0, "target_community": 1}] * 100,
                    "warnings": ["High coupling (12 edges) between 'module_0' and 'module_1'"]}

        def hub_nodes(self, *, top_n: int = 15, exclude_tests: bool = True):
            return [{"name": "main", "qualified_name": "f.c::main", "total_degree": 8}]

        def bridge_nodes(self, *, top_n: int = 15, exclude_tests: bool = True):
            return [{"name": "bridge_x", "qualified_name": "g.c::bridge_x", "betweenness": 0.9}]

    monkeypatch.setattr(cg_mod.CodeGraph, "open", lambda target, **kw: _FakeGraph())
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"big_cb": {"graph"}})
    mcp = build_server()
    # max_communities=30:50 个社区取前 30 个,每个长 name+description → 压成 member_count+样本后仍超 12000。
    out = _call(mcp, "repo_overview", {"max_communities": 30, "codebase": "big_cb"})
    assert "Traceback" not in out, out

    # ① header 诚实报真实总数 50(不受 max_communities 影响),标「本调用含 30 / 共 50」。
    assert "50 communities" in out and "本调用含 30 / 共 50" in out, out
    # ② body 里社区被 cap 到 30(50 取前 30 个);members 压成 member_count + 样本(不堆全量 qn)。
    assert '"member_count": 100' in out, out
    assert "sample_members" in out, out
    # ③ hub/bridge 在 body 的 "communities" 键前(关键:即便末尾截断,枢纽/咽喉不丢)。
    # 注:header 里有 "communities" 字样(如 "50 communities"),只比较 body 里的键 "communities":
    assert out.index("f.c::main") < out.index('"communities"'), out
    # ④ 截断有显式 note(30 社区×长描述超 12000 → 触发;诚实给补取路径,不静默丢)。
    assert "[截断" in out, out
    assert "加大 max_communities" in out, out   # note 指向重调 repo_overview 加大上限(无 communities 专用工具)



# ════════════════════════ export_patch 工具 ════════════════════════

def test_export_patch_not_a_dir():
    """repo_path 不存在 → 友好提示,不抛。"""
    mcp = build_server()
    out = _call(mcp, "export_patch", {"repo_path": "/no/such/dir/xyz_abc"})
    assert "不是目录" in out


def test_export_patch_empty_diff(tmp_path):
    """git 仓但工作树无改动 → 空 diff → 拒绝写(治改错树 / 没保存)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)  # 干净 commit,工作树无改动
    mcp = build_server()
    out = _call(mcp, "export_patch",
                {"repo_path": str(repo), "out_dir": str(tmp_path / "out")})
    assert "空 diff" in out, out
    assert "已落盘" not in out  # 拒绝写空补丁


def test_export_patch_writes_file(tmp_path):
    """git 仓 + 有未提交改动 → 写 <out_dir>/<repo-name>.patch(unified diff,非空)。"""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_repo(repo)
    (repo / "f.c").write_text("int main(void){return 1;}\n", encoding="utf-8")  # 未提交改动
    out_dir = tmp_path / "out"
    mcp = build_server()
    out = _call(mcp, "export_patch",
                {"repo_path": str(repo), "out_dir": str(out_dir)})
    assert "已落盘" in out, out
    patch_file = out_dir / "myrepo.patch"  # 命名 = repo 目录名
    assert patch_file.is_file(), f"没写到 {patch_file}"
    content = patch_file.read_text(encoding="utf-8")
    assert "diff --git" in content, "落盘的不是 unified diff"
    assert "return 1" in content, "diff 没含改动"


def test_export_patch_excludes_quilt_pc(tmp_path):
    """debian/quilt 源码仓:.pc/ 构建产物不进补丁(否则 26 行修复混成 30 万行垃圾)。"""
    repo = tmp_path / "debian-repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / "f.c").write_text("int main(void){return 1;}\n", encoding="utf-8")      # 真修复
    junk = repo / ".pc" / "some.patch" / "src"                                       # quilt 构建产物
    junk.mkdir(parents=True)
    (junk / "f.c").write_text("quilt artifact\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    mcp = build_server()
    out = _call(mcp, "export_patch",
                {"repo_path": str(repo), "out_dir": str(out_dir)})
    assert "已落盘" in out, out
    content = (out_dir / "debian-repo.patch").read_text(encoding="utf-8")
    assert "return 1" in content, "真修复没进补丁"
    assert ".pc/" not in content, "quilt 构建产物混进了补丁"


# ════════════════════════ export_report 工具 ════════════════════════

def test_export_report_empty(tmp_path):
    """空内容(或纯空白)→ 拒绝写(治 agent 假装写报告 / 传空串糊弄)。"""
    mcp = build_server()
    out = _call(mcp, "export_report",
                {"content": "   \n  ", "repo_path": str(tmp_path / "repo"),
                 "out_dir": str(tmp_path / "out")})
    assert "空报告" in out, out
    assert "已落盘" not in out  # 拒绝写空报告


def test_export_report_writes_file(tmp_path):
    """有内容 → 写 <out_dir>/<repo-name>-rca.md(内容逐字一致;repo 目录不存在也能取名)。"""
    repo = tmp_path / "myrepo"  # 故意不 mkdir:export_report 不依赖 repo 目录存在,只取目录名
    out_dir = tmp_path / "out"
    report_md = ("# 根因\n\nradio work 泄漏:abort 失败分支不释放 p2p_scan_work。\n\n"
                 "patch: data/bug_rca/myrepo.patch\nmemorize id=abc")
    mcp = build_server()
    out = _call(mcp, "export_report",
                {"content": report_md, "repo_path": str(repo),
                 "out_dir": str(out_dir)})
    assert "已落盘" in out, out
    report_file = out_dir / "myrepo-rca.md"  # 命名 = <repo 目录名>-rca.md
    assert report_file.is_file(), f"没写到 {report_file}"
    assert report_file.read_text(encoding="utf-8") == report_md


def test_export_report_topic_filename(tmp_path):
    """topic 参数:<repo>-<topic>-rca.md,治同仓多主题报告互相覆盖(2026-08-26 实测:A2DP
    报告盖掉连接流程对比报告)。同 topic 重跑 = 幂等覆盖并注明;不传 topic = 旧文件名不变。"""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    out_dir = tmp_path / "out"
    mcp = build_server()
    # ① 两个主题 → 两个文件,互不覆盖
    _call(mcp, "export_report", {"content": "# 对比\n", "repo_path": str(repo),
                                 "out_dir": str(out_dir), "topic": "connect-flow-compare"})
    out2 = _call(mcp, "export_report", {"content": "# A2DP\n", "repo_path": str(repo),
                                        "out_dir": str(out_dir), "topic": "a2dp protocol"})
    assert (out_dir / "myrepo-connect-flow-compare-rca.md").is_file()
    # topic 里的空白归一成连字符
    assert (out_dir / "myrepo-a2dp-protocol-rca.md").is_file(), out2
    assert (out_dir / "myrepo-connect-flow-compare-rca.md").read_text(encoding="utf-8") == "# 对比\n"
    # ② 同 topic 重跑 → 覆盖 + 注明(幂等,正常)
    out3 = _call(mcp, "export_report", {"content": "# 对比 v2\n", "repo_path": str(repo),
                                        "out_dir": str(out_dir), "topic": "connect-flow-compare"})
    assert "已覆盖同名文件" in out3, out3
    assert (out_dir / "myrepo-connect-flow-compare-rca.md").read_text(encoding="utf-8") == "# 对比 v2\n"
    # ③ 不传 topic → 旧命名(向后兼容)
    out4 = _call(mcp, "export_report", {"content": "# 旧式\n", "repo_path": str(repo),
                                        "out_dir": str(out_dir)})
    assert "myrepo-rca.md" in out4, out4
    assert (out_dir / "myrepo-rca.md").is_file()


def test_export_report_agents_md_opt_in(tmp_path):
    """#5 AGENTS.md 产出:默认关(不传 → 仓根无 AGENTS.md,不问自写用户仓);传 agents_md=True → 写仓根。

    两层行为:① 默认 off —— repo_root 连 AGENTS.md 都不碰(最小惊讶);② opt-in —— 写
    <repo_path>/AGENTS.md(带生成头注释),已有 AGENTS.md 不覆盖(保护手写/别的工具产物)。
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    out_dir = tmp_path / "out"
    report_md = "# 导览\n\n模块 A 是核心入口。\n"
    mcp = build_server()
    # ① 默认关:不写 AGENTS.md
    out = _call(mcp, "export_report",
                {"content": report_md, "repo_path": str(repo), "out_dir": str(out_dir)})
    assert "已落盘" in out and "AGENTS.md" not in out, out
    assert not (repo / "AGENTS.md").exists()
    # ② opt-in:写仓根
    out2 = _call(mcp, "export_report",
                 {"content": report_md, "repo_path": str(repo),
                  "out_dir": str(out_dir), "agents_md": True})
    assert "AGENTS.md 已写" in out2, out2
    agents = repo / "AGENTS.md"
    assert agents.is_file()
    body = agents.read_text(encoding="utf-8")
    assert body.startswith("# AGENTS.md") and "RootRecall export_report 生成" in body
    assert "模块 A 是核心入口" in body  # 同源内容
    # ③ 已有不覆盖
    out3 = _call(mcp, "export_report",
                 {"content": report_md, "repo_path": str(repo),
                  "out_dir": str(out_dir), "agents_md": True})
    assert "未写" in out3 and "已存在" in out3, out3


# ════════════════════════ memory_recall kind 过滤(patch_search 已并入 recall)════════════════════════

def test_memory_recall_kind_filter():
    """memory_recall 加 kind 参数(原 patch_search 并入):不崩 + kind 标签生效。

    kind 过滤逻辑(recall 多取再按 kind 过滤)在此验证不崩;命中路径由 recall 自身测试覆盖。
    """
    mcp = build_server()
    out_all = _call(mcp, "memory_recall", {"query": "bluetooth connection", "top_k": 3})
    assert "codebase=" in out_all  # 命中列表或空提示都带 codebase
    out_lesson = _call(mcp, "memory_recall", {"query": "bluetooth connection", "top_k": 3, "kind": "bug_lesson"})
    assert "kind=bug_lesson" in out_lesson  # kind 过滤生效(命中列表与空提示都带 kind 标签)


# ════════════════════════ per-call codebase(多库:同 server 进程切仓)═══════════════════════


class _FakeMemSvc:
    """记录 scope 的假 MemoryService —— 让 recall/memorize 的 per-call codebase 测试不碰真 db / 网络。

    build_server() 内 `from rootrecall.services.memory import get_memory_service` 在调用时读模块属性,
    monkeypatch 替掉它即可注入本假对象(绕开真单例)。
    """

    def __init__(self):
        self.recall_scopes: list = []
        self.search_scopes: list = []
        self.memorize_scopes: list = []
        self.memorize_items: list = []              # 记录传入的 KI(验 corrects 等字段透传)
        self.list_items_calls: list = []          # memory_dump 用(记录每次调用的 scope/kind/include_invalid)
        self.list_items_return: list = []         # 注入返回值(默认空 → 工具走空提示分支)
        self.search_returns: dict = {}            # 按 codebase 注入 search 返回(union 测试用;缺省 [])
        self.list_scopes_return: list | None = None  # list_scopes 注入(None = 后端不支持)

    async def recall(self, query, scope, *, top_k=None):  # noqa: ANN001 —— 假对象,签名宽松
        self.recall_scopes.append(scope)
        return []  # 无命中 → memory_recall 走空提示分支(仍回显 codebase)

    async def search(self, query, scope, *, top_k=5, **kw):  # noqa: ANN001 —— memory-only 路(memory_recall 工具用)
        self.search_scopes.append(scope)
        return list(self.search_returns.get(scope.codebase, []))

    async def list_scopes(self):  # noqa: ANN001 —— recall 空池提示用
        return self.list_scopes_return

    async def memorize(self, items, scope):  # noqa: ANN001
        self.memorize_scopes.append(scope)
        self.memorize_items.extend(items)
        return len(items)

    async def list_items(self, scope, *, kind=None, include_invalid=False):  # noqa: ANN001 —— memory_dump 用
        self.list_items_calls.append((scope, kind, include_invalid))
        return self.list_items_return


def test_search_codebase_per_call_codebase(monkeypatch):
    """search_codebase 传 codebase → 真去查那个仓:提示里回显 per-call 名(非闭包默认)。

    monkeypatch known_codebases 把该名放进已知集 —— 绕过近义容错层(那层对未知名会
    提前返回「没有叫 X」,到不了索引检查),直测 per-call 覆盖语义本身。
    """
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases",
                        lambda: {"nonexistent_xyz_cb_42": {"index"}})
    mcp = build_server()
    out = _call(mcp, "search_codebase",
                {"query": "p2p scan routing", "codebase": "nonexistent_xyz_cb_42"})
    assert "Traceback" not in out, out
    # per-call 生效:返回串是传入的 codebase 名,不是闭包默认的 repo(cwd/config 名)
    assert "nonexistent_xyz_cb_42" in out, out
    assert any(k in out for k in ("还没建索引", "未找到")), out


def test_memory_recall_per_call_codebase(monkeypatch):
    """memory_recall 传 codebase → search 用对应 scope(空结果回显 per-call 名 + scope 记录双证)。

    同时确认走的是 memory-only 的 search(不混 code chunk),不是混合检索的 recall ——
    见 memory-design-review-2026-08-12:memory_recall 职责是翻长期记忆,代码检索另有 search_codebase。
    注:工具会额外并查一次 general 池(2026-08-26 起),所以 search_scopes[0] 才是 per-call 名。
    """
    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_recall",
                {"query": "bluetooth disconnect", "codebase": "nonexistent_xyz_cb_42"})
    assert "codebase=nonexistent_xyz_cb_42" in out, out
    assert fake.search_scopes, "memory-only search 没被调(应走 svc.search 不是 svc.recall)"
    assert not fake.recall_scopes, "memory_recall 不该走混合检索 svc.recall(会返 code chunk)"
    assert fake.search_scopes[0].codebase == "nonexistent_xyz_cb_42"
    assert fake.search_scopes[-1].codebase == "general"  # 并查 general 池(T11 修复)


def test_memory_recall_union_general_pool(monkeypatch):
    """recall 并查 general 池:两池命中都出、按 item_id 去重、跨池命中带 [池名] 前缀。

    2026-08-26 实测教训(A2DP 裂池):bluez 池一条 + general 池一条,单池查询「查一个漏一个」。
    """
    from rootrecall.services.memory.schema import RecallHit

    def hit(summary: str, score: float, repo: str, item_id: str) -> RecallHit:
        return RecallHit(summary=summary, score=score, kind="domain_knowledge",
                         repo=repo, item_id=item_id, confidence=0.9, tags=["a2dp", "bluetooth"])

    fake = _FakeMemSvc()
    # 同一条知识裂在两池(id 相同 → 去重只出一次);general 另有一条独有
    dup = hit("A2DP 高级音频分发(裂池重复条)", 0.9, "bluez", "abc12345")
    only_general = hit("A2DP AVDTP PSM 0x0019", 0.8, "general", "def67890")
    fake.search_returns = {"bluez": [dup], "general": [dup, only_general]}
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_recall", {"query": "A2DP 蓝牙", "codebase": "bluez", "top_k": 5})
    assert "A2DP 高级音频分发" in out and "AVDTP" in out, out          # 两池命中都在
    assert out.count("A2DP 高级音频分发(裂池重复条)") == 1, out        # 同 id 去重
    assert "[general] " in out, out                                    # 跨池命中亮明池子
    assert "tags=a2dp" in out, out                                     # 主题域标签可见(短路判定提速)
    assert fake.search_scopes[0].codebase == "bluez" and fake.search_scopes[-1].codebase == "general"


def test_memory_recall_low_sim_warning(monkeypatch):
    """头牌语义相关度低于阈值 → 头部警示劝退短路;低分条目带(低相关)标记 —— 只标不删。

    标定(text-embedding-v4,2026-08-26 远端):相关 0.64-0.92 / 无关 0.18-0.28,阈值 0.40。
    RRF 分不可用:小池子里无关查询也拿满分(0.0315 vs 0.0318),只有余弦承载语义。
    """
    from rootrecall.services.memory.schema import RecallHit

    def hit(summary: str, score: float, sim: float | None) -> RecallHit:
        return RecallHit(summary=summary, score=score, kind="domain_knowledge",
                         repo="general", item_id=summary[:8], confidence=0.9, sim=sim)

    fake = _FakeMemSvc()
    fake.search_returns = {"general": [hit("量子纠缠无关条目", 0.03, 0.22), hit("勉强沾边条目", 0.02, 0.35)]}
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_recall", {"query": "量子纠缠调度器", "top_k": 3})
    assert "sim=0.22" in out and "按 miss 处理" in out, out      # 头牌低相关 → 劝退短路
    assert "(低相关 0.22)" in out and "(低相关 0.35)" in out, out  # 低分条目标记(仍可见,不删)

    fake2 = _FakeMemSvc()
    fake2.search_returns = {"general": [hit("A2DP 相关条目", 0.03, 0.85)]}
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake2)
    mcp2 = build_server()  # svc 在建 server 时捕获,换 fake 必须重建
    out2 = _call(mcp2, "memory_recall", {"query": "A2DP 蓝牙", "top_k": 3})
    assert "⚠️" not in out2 and "低相关" not in out2, out2         # 高相关:零警示零标记


def test_memory_recall_miss_lists_scopes(monkeypatch):
    """recall 空结果 → 列非空作用域,agent 一次改对 codebase(治默认空池盲试,2026-08-26 实测)。"""
    fake = _FakeMemSvc()
    fake.list_scopes_return = [("bluez", 6), ("general", 1)]
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_recall", {"query": "a2dp protocol"})
    assert "No memory found" in out, out
    assert "非空作用域:bluez(6)、general(1)" in out, out
    assert "已并查 general" in out, out


def test_memory_memorize_per_call_codebase(monkeypatch):
    """memory_memorize 传 codebase → 写入用对应 scope(返回串回显 + scope 记录双证,不碰真 db)。"""
    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_memorize", {
        "kind": "bug_lesson", "summary": "per-call codebase probe",
        "codebase": "nonexistent_xyz_cb_42",
    })
    assert "codebase=nonexistent_xyz_cb_42" in out, out
    assert fake.memorize_scopes, "memorize 没被调"
    assert fake.memorize_scopes[-1].codebase == "nonexistent_xyz_cb_42"


def test_memory_memorize_domain_knowledge_forced_general(monkeypatch):
    """domain_knowledge 强制入 general 池:传了别的 codebase 也被改写,输出注明原传值。

    2026-08-26 实测教训:同一条 A2DP 知识一条记 bluez、一条记 general,recall 查一漏一 ——
    写侧归一(全进 general)+ 读侧并查(memory_recall union)双向堵。
    """
    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_memorize", {
        "kind": "domain_knowledge", "summary": "A2DP 跑在 AVDTP 上,PSM 0x0019",
        "codebase": "bluez", "source_url": "https://www.bluetooth.com/specifications/a2dp/",
    })
    assert "codebase=general" in out, out
    assert "统一入 general" in out and "bluez" in out, out  # 注明原传值
    assert fake.memorize_scopes[-1].codebase == "general"
    assert fake.memorize_items[-1].scope.codebase == "general"


def test_memory_memorize_with_corrects(monkeypatch):
    """memory_memorize 传 corrects → KI 带 corrects 字段(纠正在场)+ 返回串回显 corrects=N。

    验纠正链入口:agent 显式声明「这条纠正了哪些旧条目」→ 工具把 corrects 填进 KI →
    memorize_items 写入时自动回填旧条的 corrected_by(这步在 native store 层测,见 test_memory_native)。
    本测验工具透传;不碰真 db(假 svc 记录传入的 KI)。
    """
    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_memorize", {
        "kind": "bug_lesson",
        "summary": "真因覆盖竞态(纠正先前误诊)",
        "root_cause": "scan-only 覆盖竞态",
        "corrects": ["abc123def4567890", "def4567890123456"],
    })
    assert "corrects=2" in out, out                             # 返回串回显纠正了 2 条
    assert fake.memorize_items, "memorize 没被调"
    ki = fake.memorize_items[-1]
    assert "abc123def4567890" in ki.corrects                     # corrects 透传到 KI
    assert len(ki.corrects) == 2


def test_memory_memorize_multi_evidence(monkeypatch):
    """memory_memorize 传 evidence(多锚点 list[dict])→ KI.evidence 多条 + 去重 + 旧 file/line 合并。

    治 onboarding e2e 暴露的缺口:架构级事实涉及多 file:line(入口+派发表+事件回调),但旧工具只接单
    file/line → onboarding 记的架构事实 evidence=[] 空(SKILL step7 写 evidence=[<file:line+片段>] 形状,
    工具却收不下)。现加 evidence 参数:list[dict] 每条 {file,line?,snippet?},去重,与旧 file/line 合并。
    """
    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_memorize", {
        "kind": "codebase_fact", "summary": "wpa 连接主流程架构",
        "kind_detail": "architecture",
        "confidence": 0.85,
        "evidence": [
            {"file": "wpa_supplicant.c", "line": 1931, "snippet": "wpa_supplicant_associate"},
            {"file": "driver_nl80211.c", "line": 5000, "snippet": "wpa_drv_associate"},
            # 重复锚点(同 file+line)→ 去重,不进两次。
            {"file": "wpa_supplicant.c", "line": 1931, "snippet": "dup"},
            # 缺 file 的脏条目 → 跳过(不崩)。
            {"line": 99},
            # line 是字符串数字 → 解析成 int。
            {"file": "events.c", "line": "3000"},
        ],
        # 旧单锚点参数仍可用,且与 evidence 合并(向后兼容)。
        "file": "ctrl_iface.c", "line": 100,
    })
    assert "memorized id=" in out, out
    assert fake.memorize_items, "memorize 没被调"
    ki = fake.memorize_items[-1]
    # 4 条:evidence 里 3 个合法去重后(wpa:1931/drv:5000/events:3000)+ 旧 file/line(ctrl:100)。
    locs = [(e.file, e.line) for e in ki.evidence]
    assert ("wpa_supplicant.c", 1931) in locs
    assert ("driver_nl80211.c", 5000) in locs
    assert ("events.c", 3000) in locs            # 字符串 line 解析成 int
    assert ("ctrl_iface.c", 100) in locs         # 旧 file/line 合并进来
    assert len(ki.evidence) == 4                 # 去重后 4 条(同锚点不重复)
    # snippet 透传。
    assert any(e.snippet == "wpa_supplicant_associate" for e in ki.evidence)
    # kind_detail/confidence 透传(onboarding 要记 architecture 级事实 + 显式置信度)。
    assert ki.kind_detail == "architecture"
    assert abs(ki.confidence - 0.85) < 1e-6


def test_memory_memorize_domain_knowledge_with_url(monkeypatch):
    """domain_knowledge + source_url(网调)→ KI 带 source_url + source_tier=imported + kind_detail=domain。

    验领域知识溯源分层:网调来的协议知识(source_url 非空)落 imported 档(外部导入,weight 0.6),
    区别于用户笔记(stated)和委托 agent 产出(delegate)。这是 domain-research skill 的核心入口。
    """
    from rootrecall.services.memory.schema import SourceTier

    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_memorize", {
        "kind": "domain_knowledge",
        "summary": "蓝牙 L2CAP 支持面向连接和面向无连接两种信道",
        "kind_detail": "domain",
        "source_url": "https://www.bluetooth.com/specifications/specs/core-54/",
        "confidence": 0.85,
        "codebase": "bluez",
    })
    assert "memorized id=" in out, out
    assert "kind=domain_knowledge" in out, out
    assert "source_url=https://www.bluetooth.com/specifications/specs/core-54/" in out, out
    assert fake.memorize_items, "memorize 没被调"
    ki = fake.memorize_items[-1]
    assert ki.kind == "domain_knowledge"
    assert ki.kind_detail == "domain"                       # domain_knowledge 的 kind_detail 透传(不再被默认成 module)
    assert ki.source_url == "https://www.bluetooth.com/specifications/specs/core-54/"
    assert ki.source_tier == SourceTier.imported            # 有 source_url → imported(网调分层)


def test_memory_memorize_domain_knowledge_user_note(monkeypatch):
    """domain_knowledge 无 source_url(用户笔记)→ source_tier=stated + source_url=None。

    验用户笔记路径:用户直接给的技术笔记(非网调)落 stated 档(人陈述,weight 1.0),
    source_url 留空。和网调(imported)区分,体现溯源分层。
    """
    from rootrecall.services.memory.schema import SourceTier

    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_memorize", {
        "kind": "domain_knowledge",
        "summary": "wpa 4-way handshake: PMK 派生 PTK,前两步 ANCE/ANCE+MIC 握手",
        "codebase": "wpa",
    })
    assert "memorized id=" in out, out
    assert "kind=domain_knowledge" in out, out
    assert "source_url=" not in out, out                    # 无 source_url 时返回串不回显
    ki = fake.memorize_items[-1]
    assert ki.kind == "domain_knowledge"
    assert ki.source_url is None                            # 用户笔记无网调 URL
    assert ki.source_tier == SourceTier.stated              # 无 source_url → stated(用户笔记分层)
    assert ki.kind_detail == "module"                       # 不传 kind_detail → 默认 module(domain_knowledge 允许)


# ════════════════════════ memory_dump 工具(第 15;记忆库体检入口)═════════════════════════

def test_memory_dump_empty(monkeypatch):
    """memory_dump 空库 → 友好提示串(回显 codebase),不抛 traceback、不碰真 db。

    包 MemoryService.list_items(已是契约);空返回 → 工具走空提示分支。hermetic:假 svc 返 []。
    """
    fake = _FakeMemSvc()  # list_items_return 默认 [] → 空提示分支
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_dump", {"codebase": "nonexistent_xyz_cb_42"})
    assert "Traceback" not in out, out
    assert "nonexistent_xyz_cb_42" in out, out          # 回显 per-call codebase
    assert fake.list_items_calls, "list_items 没被调"
    # per-call codebase 透传到 scope
    assert fake.list_items_calls[-1][0].codebase == "nonexistent_xyz_cb_42"


def test_memory_dump_renders_audit_cards(monkeypatch):
    """memory_dump 有条目 → 每条渲染成溯源卡(confidence/tier/evidence/sha/STALE 信号透传)+ header 计数。

    hermetic:假 svc 注入 2 条 KnowledgeItem(一条高 conf 带 evidence/sha,一条低 conf 无证据),
    断言 header「2 items」+ 两条 summary + 审计字段都进串。
    """
    from rootrecall.services.memory.schema import Evidence, KnowledgeItem, Scope, SourceTier

    scope = Scope(owner="default", codebase="bluez")
    item_hi = KnowledgeItem(
        kind="bug_lesson", repo="bluez", scope=scope,
        summary="sdp 缓冲区溢出根因", confidence=0.9,
        source_tier=SourceTier.delegate, commit_sha="abcdef1234",
        evidence=[Evidence(file="lib/sdp.c", line=1222, snippet="memcpy")],
        access_count=3,
    )
    item_lo = KnowledgeItem(
        kind="codebase_fact", repo="bluez", scope=scope,
        summary="连接流程入口 device_add_connection", confidence=0.2,
        source_tier=SourceTier.tool,  # 无 evidence 无 sha → 溯源弱信号(@无证据)
    )
    fake = _FakeMemSvc()
    fake.list_items_return = [item_hi, item_lo]
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_dump", {"codebase": "bluez"})
    assert "Traceback" not in out, out
    assert "2 items" in out, out                              # header 计数
    assert "sdp 缓冲区溢出根因" in out, out                  # 高置信条 summary
    assert "连接流程入口" in out, out                         # 低置信条 summary
    assert "conf=0.90" in out, out                            # 置信度透传
    assert "conf=0.20" in out, out
    assert "tier=delegate" in out and "tier=tool" in out, out  # 来源档透传
    assert "sha=abcdef12" in out, out                         # commit_sha 截断 8 位
    assert "lib/sdp.c:1222" in out, out                       # evidence file:line
    assert "@无证据" in out, out                              # 溯源弱信号(无 evidence 的条目标出)
    # id 渲染(截断 8 位):体检/纠正链要用 —— memory_memorize(corrects=[...]) 要传 dump 里看到的 id,
    # 不渲染 id = 闭环走不通(e2e 暴露:agent 拿不到被纠正条目 id 被逼 grep SQLite)。
    assert f"id={item_hi.id[:8]}" in out, out
    assert f"id={item_lo.id[:8]}" in out, out
    assert "health:" not in out, out  # 无 tags 的库不输出健康概要行(不添噪音)
    """Phase 3 A2:治理标签(consolidate 五 pass 的产出)进体检 —— 溯源卡渲染 [tags] + header 健康概要。

    hermetic:假 svc 注入带 tags 的条目(needs_review / merged_upstream+stale),断言
    ① 卡上有 [needs_review] 等(逐条治理状态);② header 「health:」行聚合计数(全局一眼看)。
    无 tags 的库不输出 health 行(不添噪音)—— 由 test_memory_dump_renders_audit_cards 覆盖
    (那个测试的条目无 tags,断言 'health:' 不在串,见下)。
    """
    from rootrecall.services.memory.schema import KnowledgeItem, Scope, SourceTier

    scope = Scope(owner="default", codebase="bluez")
    a = KnowledgeItem(
        kind="bug_lesson", repo="bluez", scope=scope, summary="矛盾甲:根因是 A",
        confidence=0.8, source_tier=SourceTier.delegate, tags=["needs_review"],
    )
    b = KnowledgeItem(
        kind="bug_lesson", repo="bluez", scope=scope, summary="已合上游的旧教训",
        confidence=0.25, source_tier=SourceTier.delegate, tags=["merged_upstream", "stale"],
    )
    fake = _FakeMemSvc()
    fake.list_items_return = [a, b]
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    out = _call(mcp, "memory_dump", {"codebase": "bluez"})
    assert "Traceback" not in out, out
    assert "[needs_review]" in out, out                                  # 卡上治理标签
    assert "[merged_upstream,stale]" in out, out                         # 多标签逗号连
    assert "health:" in out and "needs_review=1" in out, out             # header 健康概要
    assert "merged_upstream=1" in out and "stale=1" in out, out


def test_memory_dump_pagination(monkeypatch):
    """memory_dump 超 limit → 翻页提示「showing X-Y of N, more → offset=」;offset 翻页拿后续。

    e2e 暴露:体检要全量,旧 [:8000] 截断静默吞一半条目逼 agent recall 补捞。改 limit/offset
    分页 + 显式翻页提示(诚实信号)。本测:65 条(>默认 limit=60)→ 第一页提示 more + offset=60;
    offset=60 第二页拿余下 5 条,header 仍带总数 65。
    """
    from rootrecall.services.memory.schema import KnowledgeItem, Scope
    scope = Scope(owner="default", codebase="big")
    items = [KnowledgeItem(kind="codebase_fact", repo="big", scope=scope,
                           summary=f"fact-{i:03d}", confidence=0.5) for i in range(65)]
    fake = _FakeMemSvc()
    fake.list_items_return = items
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    # 第一页:总数 65,提示还有更多 + 下次 offset=60
    p1 = _call(mcp, "memory_dump", {"codebase": "big"})
    assert "65 items" in p1, p1
    assert "showing 1-60 of 65" in p1, p1
    assert "offset=60" in p1, p1              # 翻页提示
    assert "fact-000" in p1 and "fact-059" in p1   # 第一页内容
    assert "fact-064" not in p1               # 第二页的没出现在第一页
    # 第二页:offset=60 拿余下 5 条,无 more 提示(拿完了)
    p2 = _call(mcp, "memory_dump", {"codebase": "big", "offset": 60})
    assert "65 items" in p2, p2               # header 仍带总数
    assert "showing 61-65 of 65" in p2, p2
    assert "more" not in p2.lower() or "offset=" not in p2  # 拿完无翻页提示
    assert "fact-060" in p2 and "fact-064" in p2


# ════════════════════════ cross_version_diff 工具 ═════════════════════════

def test_cross_version_diff_bad_ref(tmp_path):
    """非法 ref(含 ';' → 过不了 _SAFE_GIT_REF)→ ValueError → 工具转友好串,不抛 traceback。

    不需 git:regex 校验在 rev-parse 之前;tmp_path 是合法目录即可(repo_path is_dir 检查过)。
    """
    mcp = build_server()
    out = _call(mcp, "cross_version_diff",
                {"base_ref": "a;b", "head_ref": "HEAD", "repo_path": str(tmp_path)})
    assert "Traceback" not in out, out
    assert "没法算" in out, out  # ValueError 被工具兜底成友好串


def test_cross_version_diff_not_a_repo(tmp_path):
    """repo_path 是合法目录但非 git 仓 → git rev-parse 失败 → ValueError → 友好串,不抛。

    需 git(无 git skip):没装 git 时是另一条路径(OSError),语义不同,跳过保持断言精度。
    """
    import shutil
    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    mcp = build_server()
    out = _call(mcp, "cross_version_diff",
                {"base_ref": "HEAD~1", "head_ref": "HEAD", "repo_path": str(tmp_path)})
    assert "Traceback" not in out, out
    assert "没法算" in out, out  # 非 git 仓 → rev-parse 失败 → ValueError → "没法算"


# ════════════════════════ when_introduced 工具(🟡#7,第 16 个 MCP 工具)════════════════════════

def _mk_introduced_repo(base):
    """造一个 3-commit 小仓:c1 引入 bug 函数 → c2 只重构搬行(不动该函数)→ c3 加无关代码。

    金标:pickaxe 锚 bug_func 只该命中 c1(added>0, removed==0);
    行历史锚 c1 那一行也只该命中 c1。—— 验证「候选表含引入 commit + added/removed 计数语义」。
    """
    d = base / "repo"
    d.mkdir()
    def git(*a):
        # -c 传身份/关 gpgsign(别碰全局 git config;仓里提交顺序即时间序,不另造日期)
        subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *a], check=True, capture_output=True)
    git("init", "-q", "--initial-branch=main")
    (d / "mod.c").write_text("int bug_func(void) { return 1; }  // c1: 缺 range check,引入缺陷\n")
    git("add", "mod.c")
    git("commit", "-qm", "c1: add bug_func")
    # c2:改同文件别的函数(不动 bug_func)——pickaxe + pathspec 下不该出现
    (d / "mod.c").write_text(
        "int other(void) { return 2; }\nint bug_func(void) { return 1; }  // c1: 缺 range check,引入缺陷\n")
    git("add", "mod.c")
    git("commit", "-qm", "c2: add other")
    # c3:再加无关文件
    (d / "x.c").write_text("int x;\n")
    git("add", "x.c")
    git("commit", "-qm", "c3: add x")
    return d


def test_when_introduced_pickaxe_finds_introducer(tmp_path):
    """pickaxe(symbol+file):候选表含引入 commit c1,且 added>0 / removed==0;c2/c3 不在(没动该符号)。"""
    import shutil
    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    d = _mk_introduced_repo(tmp_path)
    mcp = build_server()
    out = _call(mcp, "when_introduced",
                {"repo_path": str(d), "symbol": "bug_func", "file": "mod.c"})
    assert "Traceback" not in out, out
    assert "1 candidates" in out, out            # 只命中 c1
    assert "c1: add bug_func" in out, out        # 引入 commit 在表里
    assert '"added": 1' in out and '"removed": 0' in out, out  # 计数语义:纯引入
    assert "语义判断" in out, out                # note:裁决归 agent(确定性地板+LLM 天花板)


def test_when_introduced_line_history_finds_introducer(tmp_path):
    """行历史(file+line):锚 bug_func 定义行(当前工作树行 2,c2 挪过)→ 只命中 c1(rename/挪行跟随)。"""
    import shutil
    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    d = _mk_introduced_repo(tmp_path)
    mcp = build_server()
    out = _call(mcp, "when_introduced",
                {"repo_path": str(d), "file": "mod.c", "line": 2})
    assert "Traceback" not in out, out
    assert "mode=line_history" in out, out
    assert "c1: add bug_func" in out, out
    assert "c2: add other" not in out, out       # c2 在行上方插入,不算触及该行段


def test_when_introduced_bad_anchor(tmp_path):
    """两锚点同给(symbol+line)→ ValueError → 工具转友好串「没法算」,不抛 traceback。"""
    mcp = build_server()
    out = _call(mcp, "when_introduced",
                {"repo_path": str(tmp_path), "symbol": "x", "line": 1})
    assert "Traceback" not in out, out
    assert "没法算" in out, out


# ════════════════════════ merge_eval 工具(低优 #1)════════════════════════

def test_merge_eval_bad_ref(tmp_path):
    """非法 ref(含 ';' 过不了 _SAFE_GIT_REF)→ ValueError → 工具转友好串,不抛 traceback。"""
    mcp = build_server()
    out = _call(mcp, "merge_eval",
                {"upstream_base_ref": "a;b", "upstream_head_ref": "HEAD",
                 "fork_ref": "HEAD", "repo_path": str(tmp_path)})
    assert "Traceback" not in out, out
    assert "没法算" in out, out


def test_merge_eval_not_a_repo(tmp_path):
    """repo_path 合法目录但非 git 仓 → rev-parse 失败 → ValueError → 友好串,不抛。"""
    import shutil
    if not shutil.which("git"):
        pytest.skip("git 不在 PATH")
    mcp = build_server()
    out = _call(mcp, "merge_eval",
                {"upstream_base_ref": "HEAD~1", "upstream_head_ref": "HEAD",
                 "fork_ref": "HEAD", "repo_path": str(tmp_path)})
    assert "Traceback" not in out, out
    assert "没法算" in out, out


def test_merge_eval_success_via_fake(monkeypatch):
    """happy path:monkeypatch merge_eval 返固定 dict → 工具格式化「total=N | 三态计数」+ body;
    fork_ref/max_commits 透传。

    monkeypatch 模块级 merge_eval(工具内 `from ... import merge_eval as _me` 每次调用重读属性 → 拿到假函数,
    hermetic 不靠真 git 仓)。CodeGraph.open('fake_cb') 会 FileNotFoundError → 工具 try/except → graph=None,假函数忽略 graph。
    """
    import rootrecall.services.code_index.code_graph as cg_mod

    seen: dict = {}

    def fake_me(upstream_base_ref, upstream_head_ref, *, fork_ref, repo_path,  # noqa: ANN001
                concern_files=None, max_commits=50, graph=None):
        seen.update(fork_ref=fork_ref, max_commits=max_commits, repo_path=repo_path,
                    concern_files=concern_files)
        return {"repo": repo_path, "fork_ref": fork_ref,
                "upstream_range": f"{upstream_base_ref}..{upstream_head_ref}",
                "commits": [{"sha": "abc123", "subject": "fix: harden foo",
                             "equivalent_in_fork": False, "applies_cleanly": True,
                             "touched_files": ["a.c"], "touched_functions": [],
                             "state": "recommend_merge"}],
                "summary": {"total": 1, "already_fixed": 0, "recommend_merge": 1,
                            "conflict": 0, "uncertain": 0},
                "note": ""}

    monkeypatch.setattr(cg_mod, "merge_eval", fake_me)
    mcp = build_server()
    out = _call(mcp, "merge_eval",
                {"upstream_base_ref": "v1.0", "upstream_head_ref": "v1.1",
                 "fork_ref": "release/eagle", "repo_path": "/tmp/repo",
                 "max_commits": 7, "concern_files": ["a.c"], "codebase": "fake_cb"})
    assert "Traceback" not in out, out
    assert "total=1" in out, out
    assert "recommend_merge=1" in out, out
    assert "abc123" in out, out  # body(json)含 commit sha
    # 参数透传
    assert seen["fork_ref"] == "release/eagle"
    assert seen["max_commits"] == 7
    assert seen["concern_files"] == ["a.c"]


# ════════════════════════ 工具门控(ROOTRECALL_MCP_TOOLS)════════════════════════

def _tool_names(mcp) -> set[str]:
    """server 实际注册了哪些工具(list_tools = 模型能看见的 tools/list)。"""
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_mcp_tools_default_registers_all_17(monkeypatch):
    """未设置 env → 17 个全注册(向后兼容:现有接线零影响)。"""
    monkeypatch.delenv("ROOTRECALL_MCP_TOOLS", raising=False)
    assert len(_tool_names(build_server())) == 17


def test_mcp_tools_preset_minimal(monkeypatch):
    """minimal 预设 → 只注册 8 个(find_repo 开仓查表+记忆3+search+硬门3);情报/PR 类不进 tools/list。"""
    monkeypatch.setenv("ROOTRECALL_MCP_TOOLS", "minimal")
    assert _tool_names(build_server()) == {
        "memory_recall", "memory_memorize", "memory_dump", "find_repo",
        "search_codebase", "validate_patch", "export_patch", "export_report",
    }


def test_mcp_tools_explicit_list(monkeypatch):
    """显式逗号清单(带空格容错)→ 只注册列出的;被裁工具不在 tools/list 里 = 模型看不见 schema。"""
    monkeypatch.setenv("ROOTRECALL_MCP_TOOLS", "memory_recall, validate_patch")
    assert _tool_names(build_server()) == {"memory_recall", "validate_patch"}


def test_mcp_tools_unknown_name_fails_loud(monkeypatch):
    """拼错名 → 启动即 ValueError(附可用名清单),不静默给个错误子集。"""
    monkeypatch.setenv("ROOTRECALL_MCP_TOOLS", "memory_recallx")
    with pytest.raises(ValueError, match="未知工具名"):
        build_server()


# ════════════════════════ find_repo 工具(P0 自动开仓第一环)════════════════════════

def test_find_repo_hit_baseline_first(tmp_path):
    """同项目 baseline + 该版本 ephemeral → 版本精确命中(ephemeral)主列,baseline 进 Related。"""
    from rootrecall.services.repos.registry import RepoRegistry

    reg = RepoRegistry()
    (tmp_path / "wt").mkdir()
    reg.register("bluez-v25", path=str(tmp_path), url="https://example.com/bluez.git",
                 role="baseline", branch="master")
    reg.register("bluez-v25-5.50.61", path=str(tmp_path / "wt"), role="ephemeral",
                 from_repo="bluez-v25", branch="5.50.61", bug_id="B-9")

    mcp = build_server()
    out = _call(mcp, "find_repo", {"project": "bluez", "version": "5.50.61"})
    assert "Matched 1" in out and "bluez-v25-5.50.61" in out and "[ephemeral]" in out
    assert "bug=B-9" in out and "on-disk" in out
    assert "Related" in out and "[baseline]" in out  # 相近基线单列,不冒充该版本命中


def test_find_repo_miss_with_baseline_gives_provision_command():
    """有基线没该版本 → 回基线清单 + 带安装根、含 --index 的自动开仓命令(不问用户)。"""
    from rootrecall.services.repos.registry import RepoRegistry

    RepoRegistry().register("bluez-v20", url="https://example.com/bluez.git",
                            role="baseline", branch="master")

    mcp = build_server()
    out = _call(mcp, "find_repo", {"project": "bluez", "version": "5.50.61"})
    assert "No repo matched" in out and "bluez-v20" in out
    assert "baseline checkout" in out and "--index" in out and "--ref 5.50.61" in out
    assert "project" in out  # 命令带 --project <安装根>,bash 可原样跑


def test_find_repo_miss_no_baseline_asks_for_url():
    """连基线都没有 → 引导要 git 地址、clone 进总目录后 baseline add(或 ensure_repo),不给无法执行的命令。"""
    mcp = build_server()
    out = _call(mcp, "find_repo", {"project": "bluez"})
    assert "No repo matched" in out and "baseline add" in out and "git URL" in out
    assert "baseline checkout" not in out  # 没基线时开仓命令无从执行,不该给


# ════════════════════ verification 纪律硬化(P2-1)════════════════════

def test_memory_memorize_verification_apply_only(monkeypatch):
    """verification=apply_only → KI 带 unverified 标 + 置信封顶 0.5(先验不冒充结论)。"""
    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    _call(mcp, "memory_memorize", {
        "kind": "bug_lesson", "summary": "probe apply-only",
        "fix_patch": "diff --git a/x b/x\n", "verification": "apply_only", "confidence": 0.95})
    ki = fake.memorize_items[-1]
    assert "unverified" in ki.tags
    assert ki.confidence == 0.5, "apply-only 置信必须封顶 0.5,给了 0.95 也要压下来"


def test_memory_memorize_verification_real_machine(monkeypatch):
    """verification=real_machine → verified_real_machine 标;显式带进来的 unverified 被摘掉(升级路径)。"""
    fake = _FakeMemSvc()
    monkeypatch.setattr("rootrecall.services.memory.get_memory_service", lambda: fake)
    mcp = build_server()
    _call(mcp, "memory_memorize", {
        "kind": "bug_lesson", "summary": "probe real-machine",
        "fix_patch": "diff --git a/x b/x\n", "verification": "real_machine",
        "tags": ["patch_insight", "unverified"]})
    ki = fake.memorize_items[-1]
    assert "verified_real_machine" in ki.tags and "unverified" not in ki.tags


def test_recall_hit_renders_unverified_marker():
    """RecallHit 渲染:unverified 标 → 「(未真机验证)」显式可见;无标不渲染(零噪声)。"""
    from rootrecall.services.memory.schema import RecallHit

    h = RecallHit(summary="某 bug 教训", score=1.0, tags=["patch_insight", "unverified"])
    assert "(未真机验证)" in h.render()
    h2 = RecallHit(summary="某 bug 教训", score=1.0, tags=["verified_real_machine"])
    assert "未真机验证" not in h2.render()


# ════════════════════ 交付物按 bug_id 归档(P2-2)════════════════════

def test_export_patch_archives_by_bug_id(tmp_path, monkeypatch):
    """注册了 bug_id 的 ephemeral → 补丁双写:平铺「最新一份」不变 + <bug_id>/ 归档副本。"""
    monkeypatch.chdir(tmp_path)  # data/bug_rca 相对落点进 tmp
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / "f.c").write_text("int main(void){return 1;}\n", encoding="utf-8")  # 制造非空 diff
    from rootrecall.services.repos.registry import RepoRegistry

    RepoRegistry().register("buggy-1.0", path=str(repo), role="ephemeral", bug_id="B-42")
    mcp = build_server()
    out = _call(mcp, "export_patch", {"repo_path": "buggy-1.0"})
    assert "归档" in out and "B-42" in out, out
    assert (tmp_path / "data" / "bug_rca" / "buggy-1.0.patch").exists()      # 平铺最新
    assert (tmp_path / "data" / "bug_rca" / "B-42" / "buggy-1.0.patch").exists()  # 按 bug 归档


def test_export_patch_without_bug_id_skips_archive(tmp_path, monkeypatch):
    """记录没有 bug_id(或压根没注册)→ 只写平铺,不归档、不报错。"""
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git_repo(repo)
    (repo / "f.c").write_text("int main(void){return 2;}\n", encoding="utf-8")
    mcp = build_server()
    out = _call(mcp, "export_patch", {"repo_path": str(repo)})
    assert "归档" not in out
    assert (tmp_path / "data" / "bug_rca" / "repo2.patch").exists()
    assert not (tmp_path / "data" / "bug_rca" / "B-42").exists()
