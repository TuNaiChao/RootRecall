"""cmd_index 图系解耦测试(2026-09-02 路线图②)。

背景:此前零 key(没配 embedding key)在 embedder 创建处直接 `return 2`,结构图排在
向量索引之后被连坐 —— 而图系 4 工具(blast_radius / call_chain / repo_map /
repo_overview)全走 CodeGraph.open,零 embedder。修法:零 key 向量路诚实跳过但图照建
(rc 仍 2 提示向量未建);另加 `--graph-only` 显式正向开关(有 key 也可只用图)。
"""

import argparse

import pytest

from rootrecall.cli import cmd_index


def _ns(tmp_path, **kw):
    ns = dict(repo_path=str(tmp_path), repo_name="decouple_t", force=False, seed=None,
              no_graph=False, graph_only=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture
def graph_stub(monkeypatch):
    """打桩 CodeGraph.build / build_index / create_embedder,记录调用。"""
    calls = {"graph": 0, "vector": 0, "embedder": 0}

    def _fake_build(*a, **k):
        calls["graph"] += 1

    def _fake_build_index(*a, **k):
        calls["vector"] += 1
        return {"mode": "stub", "total_chunks": 0}

    monkeypatch.setattr("rootrecall.services.code_index.code_graph.CodeGraph.build", _fake_build)
    monkeypatch.setattr("rootrecall.services.code_index.index.build_index", _fake_build_index)
    return calls


# ── 1. 零 key:向量诚实跳过,图照建(不再连坐),rc=2 提示向量未建 ────────────
def test_zero_key_builds_graph_not_connected(tmp_path, monkeypatch, graph_stub, capsys):
    def _no_key(cfg):
        graph_stub["embedder"] += 1
        raise ValueError("远端 embedding 需要 api_key")

    monkeypatch.setattr("rootrecall.services.code_index.embed.create_embedder", _no_key)
    rc = cmd_index(_ns(tmp_path))
    assert rc == 2                       # 向量路没建成,调用方(baseline add)仍能感知
    assert graph_stub == {"graph": 1, "vector": 0, "embedder": 1}
    err = capsys.readouterr().err
    assert "--graph-only" in err          # 指路文案含第三条路
    assert "继续建结构图" in err


# ── 2. --graph-only:完全不碰 embedder(有 key 也不碰),只建图,rc=0 ─────────
def test_graph_only_skips_embedder_entirely(tmp_path, monkeypatch, graph_stub, capsys):
    monkeypatch.setattr("rootrecall.services.code_index.embed.create_embedder",
                        lambda cfg: pytest.fail("graph-only 不该创建 embedder"))
    rc = cmd_index(_ns(tmp_path, graph_only=True))
    assert rc == 0
    assert graph_stub == {"graph": 1, "vector": 0, "embedder": 0}
    assert "search_codebase" in capsys.readouterr().out


# ── 3. --graph-only 与 --no-graph 互斥:两开等于什么都不建 ───────────────────
def test_graph_only_conflicts_with_no_graph(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("rootrecall.services.code_index.embed.create_embedder",
                        lambda cfg: pytest.fail("互斥报错该在创建 embedder 之前"))
    rc = cmd_index(_ns(tmp_path, graph_only=True, no_graph=True))
    assert rc == 2
    assert "互斥" in capsys.readouterr().err


# ── 4. 全 key 回归:行为与旧版一致(向量+图都建,rc=0)───────────────────────
def test_full_key_unchanged(tmp_path, monkeypatch, graph_stub):
    class _Embedder:
        pass

    def _ok_embedder(cfg):
        graph_stub["embedder"] += 1
        return _Embedder()

    monkeypatch.setattr("rootrecall.services.code_index.embed.create_embedder", _ok_embedder)
    rc = cmd_index(_ns(tmp_path))
    assert rc == 0
    assert graph_stub == {"graph": 1, "vector": 1, "embedder": 1}
