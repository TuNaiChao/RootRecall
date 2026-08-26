"""codebase 近义名容错(2026-08-25 实测教训:bluez-rca.md 报告)。

背景:记忆吃项目名(bluez,教训跨版本共享),索引/图吃注册名(bluez-v25)—— agent 从
记忆拿到 "bluez" 直接传工具,四连败后才摸到正名。修法三层,这里逐层锁行为:

① 纯函数 _match_codebase:精确 > 归一化(_↔-/大小写/空白) > 子串双向(唯一才自动采用);
② known_codebases:registry / index / graph 三源并集收集(带 marker 验证,半成品不算);
③ 工具层 _resolve_active_codebase + 六工具接线:唯一近义自动纠偏(输出头注明)、多个
   候选列举、全落空给本机已知清单、图缺失区分「在册没图」vs「完全未知」。
"""

from __future__ import annotations

import asyncio

from rootrecall.services.repos.registry import RepoRegistry, known_codebases
from rootrecall.tools.mcp_memory import (
    _graph_missing_msg,
    _match_codebase,
    _resolve_active_codebase,
    build_server,
)

KNOWN = {"bluez-v20", "bluez-v25", "bluez-upstream", "systemd"}


def _call(mcp, name: str, args: dict) -> str:
    """调一个工具,取回它的 str 结果(同 test_mcp_tools 的直调 helper)。"""
    blocks, _structured = asyncio.run(mcp.call_tool(name, args))
    return blocks[0].text


def _known(monkeypatch, mapping: dict[str, set[str]]) -> None:
    monkeypatch.setattr("rootrecall.services.repos.registry.known_codebases", lambda: mapping)


# ════════════════════════ ① _match_codebase 纯函数 ════════════════════════

def test_match_exact_passthrough():
    assert _match_codebase("bluez-v25", KNOWN) == ("bluez-v25", [])


def test_match_normalized_underscore_and_case():
    assert _match_codebase("bluez_v25", KNOWN)[0] == "bluez-v25"   # 下划线 ↔ 连字符
    assert _match_codebase(" Bluez-V25/ ", KNOWN)[0] == "bluez-v25"  # 大小写/空白/尾斜杠


def test_match_unique_substring_autouses():
    # 单基线机器:项目名唯一命中 → 自动采用
    assert _match_codebase("bluez", {"bluez-v25"}) == ("bluez-v25", ["bluez-v25"])
    # 反向包含:请求带版本后缀,唯一基线是它的前缀
    assert _match_codebase("bluez-v25-5.85", {"bluez-v25", "systemd"})[0] == "bluez-v25"


def test_match_multi_substring_lists_candidates():
    # 实测翻车现场:bluez 传给三基线机器 → 列候选让 agent 一次改对,不再误导去「建 bluez 索引」
    matched, subs = _match_codebase("bluez", KNOWN)
    assert matched is None
    assert subs == sorted(["bluez-v20", "bluez-v25", "bluez-upstream"])


def test_match_none_and_empty():
    assert _match_codebase("wpa_supplicant", KNOWN) == (None, [])
    assert _match_codebase("   ", KNOWN) == (None, [])


# ════════════════════════ ② known_codebases 三源收集 ════════════════════════

def test_known_codebases_three_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOTRECALL_HOME", str(tmp_path))  # data_root → tmp(索引/图目录)
    # 注册表源:conftest 已把 ROOTRECALL_REPOS_FILE 锚到本用例 tmp,直接登记即落 tmp
    RepoRegistry().register("reg-only", path=str(tmp_path / "somewhere"), role="baseline")
    # 索引源:带 index_manifest.json 才算;没清单的半成品目录不算
    idx = tmp_path / "code_index" / "idx-only"
    idx.mkdir(parents=True)
    (idx / "index_manifest.json").write_text("{}", encoding="utf-8")
    half = tmp_path / "code_index" / "half-built"
    half.mkdir()
    # 图源:带 graph.db 才算
    sg = tmp_path / "structgraph" / "graph-only"
    sg.mkdir(parents=True)
    (sg / "graph.db").write_text("", encoding="utf-8")

    out = known_codebases()
    assert out["reg-only"] == {"registry"}
    assert out["idx-only"] == {"index"}
    assert out["graph-only"] == {"graph"}
    assert "half-built" not in out


def test_known_codebases_survives_broken_sources(tmp_path, monkeypatch):
    """注册表文件写坏 → 跳过该源,索引/图两源照常(容错不挡主链路)。"""
    monkeypatch.setenv("ROOTRECALL_REPOS_FILE", str(tmp_path / "broken.yaml"))
    (tmp_path / "broken.yaml").write_text("::: not yaml [", encoding="utf-8")
    monkeypatch.setenv("ROOTRECALL_HOME", str(tmp_path))
    sg = tmp_path / "structgraph" / "only-graph"
    sg.mkdir(parents=True)
    (sg / "graph.db").write_text("", encoding="utf-8")

    assert known_codebases() == {"only-graph": {"graph"}}


# ════════════════════════ ③ _resolve_active_codebase / _graph_missing_msg ════════════════════════

def test_resolve_exact_silent(monkeypatch):
    _known(monkeypatch, {"bluez-v25": {"index"}})
    name, note, _ = _resolve_active_codebase("bluez-v25")
    assert (name, note) == ("bluez-v25", "")  # 精确命中零注记(老行为零变化)


def test_resolve_auto_with_note(monkeypatch):
    _known(monkeypatch, {"bluez-v25": {"graph"}})
    name, note, _ = _resolve_active_codebase("bluez_v25")
    assert name == "bluez-v25"
    assert "近义解析为" in note and "bluez_v25" in note


def test_resolve_multi_lists_candidates(monkeypatch):
    _known(monkeypatch, {"bluez-v20": {"index"}, "bluez-v25": {"index"}, "bluez-upstream": {"index"}})
    name, msg, _ = _resolve_active_codebase("bluez")
    assert name is None
    assert "匹配到多个" in msg and "bluez-v25" in msg and "baseline ls" in msg


def test_resolve_unknown_lists_known(monkeypatch):
    _known(monkeypatch, {"bluez-v25": {"index"}})
    name, msg, _ = _resolve_active_codebase("wpa_supplicant")
    assert name is None
    assert "没有叫" in msg and "bluez-v25" in msg


def test_resolve_no_codebases_at_all(monkeypatch):
    _known(monkeypatch, {})
    name, msg, _ = _resolve_active_codebase("whatever")
    assert name is None
    assert "baseline add" in msg


def test_graph_missing_msg_distinguishes():
    # 在册(注册表/索引)但没图 → 指路重建 index;完全未知 → 指路 baseline add
    assert "已注册/有索引" in _graph_missing_msg("x", {"x": {"registry", "index"}})
    assert "已注册/有索引" in _graph_missing_msg("x", {"x": {"index"}})
    assert "baseline add" in _graph_missing_msg("x", {})


# ════════════════════════ ④ 工具层接线(build_server 直调)════════════════════════

def test_repo_map_autocorrected_name_visible(monkeypatch):
    """唯一近义命中 → 工具照跑,输出头注明纠偏(agent 可见自己被纠到哪个库)。"""
    import rootrecall.services.code_index.code_graph as cg_mod

    class _FakeGraph:
        def repo_map(self, *, map_tokens: int = 2048, exclude_tests: bool = True):  # noqa: ANN001
            return {"n_symbols": 2, "n_files": 1, "map_text": "f.c", "truncated": False,
                    "top_symbols": [], "note": ""}

    monkeypatch.setattr(cg_mod.CodeGraph, "open", lambda target, **kw: _FakeGraph())
    _known(monkeypatch, {"bluez-v25": {"graph"}})
    mcp = build_server()
    out = _call(mcp, "repo_map", {"codebase": "bluez_v25"})
    assert "近义解析为 'bluez-v25'" in out, out
    assert "codebase=bluez-v25" in out, out
    assert "2 symbols / 1 files" in out, out


def test_blast_radius_multi_candidate_error(monkeypatch):
    """多个近义 → 不猜,列候选;不再误导 agent 去「建 bluez 索引」。"""
    _known(monkeypatch, {"bluez-v20": {"graph"}, "bluez-v25": {"graph"}})
    mcp = build_server()
    out = _call(mcp, "blast_radius", {"changed_files": ["src/x.c"], "codebase": "bluez"})
    assert "匹配到多个" in out and "bluez-v20" in out and "bluez-v25" in out, out
    assert "baseline ls" in out, out


def test_search_codebase_unknown_lists_known(monkeypatch):
    """完全未知名 → 列本机已知清单(不再叫 agent 去建一个可能已存在的索引)。"""
    _known(monkeypatch, {"bluez-v25": {"index"}})
    mcp = build_server()
    out = _call(mcp, "search_codebase", {"query": "p2p scan", "codebase": "wpa"})
    assert "没有叫 'wpa'" in out and "bluez-v25" in out, out


def test_repo_overview_registered_but_no_graph(monkeypatch):
    """「存在但没建图」vs「不存在」区分报错:注册在册只缺图 → 指路重建 index。"""
    import rootrecall.services.code_index.code_graph as cg_mod

    def _missing(target, *, base_dir="data/structgraph"):
        raise FileNotFoundError(f"结构图未建,先 CodeGraph.build(...): {base_dir}/{target}/graph.db")

    monkeypatch.setattr(cg_mod.CodeGraph, "open", _missing)
    _known(monkeypatch, {"bluez-v25": {"registry", "index"}})
    mcp = build_server()
    out = _call(mcp, "repo_overview", {"codebase": "bluez-v25"})
    assert "已注册/有索引" in out and "结构图未建" in out, out
    assert "baseline add" not in out, out  # 不是「从未见过这个库」的指路
