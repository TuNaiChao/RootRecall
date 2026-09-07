"""memory seed 冷启动播种测试(路线图⑥,2026-09-07)。

验收口径:seed 后 recall「X 模块在哪」命中,且条目 tier=inferred 可辨(体检卡能识别
低置信种子卡);幂等(重复跑零变更);图未建 / 零 key / 缺 README → 诚实报错不甩栈。
"""

from __future__ import annotations

import asyncio
import tempfile
from types import SimpleNamespace

import pytest

from rootrecall.platform.config import NativeMemoryConfig
from rootrecall.services.memory.backends.native.recall import recall
from rootrecall.services.memory.backends.native.service import NativeMemoryService
from rootrecall.services.memory.backends.native.store import MemoryStore
from rootrecall.services.memory.backends.native.structural import NoopStructuralBackend
from rootrecall.services.memory.schema import Scope


class _StubModel:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=self._text)


@pytest.fixture
def store():
    s = MemoryStore(tempfile.mkdtemp())
    yield s
    s.close()


def _svc(store) -> NativeMemoryService:
    return NativeMemoryService(
        store=store, embedder=None, reranker=None, code_bundle=None,
        structural=NoopStructuralBackend(), model=None,
        native_cfg=NativeMemoryConfig(),
    )


class _StubGraph:
    """两个社区:一个 3 成员(播)、一个 1 成员(孤点,跳过)。镜像 architecture_overview 的形状。"""

    def architecture_overview(self):
        return {"communities": [
            {"id": 1, "size": 3, "members": [
                "/repo/src/scan.c::scan_start", "/repo/src/scan.c::scan_stop", "/repo/src/peer.c::peer_add"]},
            {"id": 2, "size": 1, "members": ["/repo/src/misc.c::helper"]},
        ]}

    class _store:  # noqa: N801
        @staticmethod
        def _batch_get_nodes(qns):
            return [{"file_path": "/repo/src/scan.c", "name": "scan_start", "line_start": 120},
                    {"file_path": "/repo/src/peer.c", "name": "peer_add", "line_start": 45}]

    _store = _store()


def test_seed_full_writes_inferred_facts(store, monkeypatch):
    monkeypatch.setattr("rootrecall.services.code_index.code_graph.CodeGraph.open",
                        lambda name: _StubGraph())
    model = _StubModel('{"summary": "扫描模块负责 P2P 设备发现与扫描调度,入口 scan_start", '
                       '"entry": "scan_start @ src/scan.c:120"}')
    monkeypatch.setattr("rootrecall.platform.models.create_chat_model", lambda *a, **k: model)
    scope = Scope(codebase="wpa")

    rep = asyncio.run(_svc(store).seed(scope, codebase="wpa", repo_path="/repo"))
    assert rep["seeded"] == 1 and rep["modules"] == 2  # 孤点社区跳过但仍计入模块清单
    (it,) = store.list_items(scope)
    assert it.kind == "codebase_fact" and it.source_tier == "inferred"   # 体检卡可辨
    assert it.evidence[0].file == "src/scan.c" and it.evidence[0].line == 120  # 真 file:line,已剥 repo 前缀
    assert it.confidence == pytest.approx(0.35)  # inferred 0.7 × 0.5 低置信

    # 验收:recall「X 模块在哪」命中(零 key,走 BM25 路)
    hits = recall("扫描模块在哪", scope, store=store, embedder=None, reranker=None)
    assert hits and hits[0].item_id == it.id

    # 幂等:重复跑零变更
    rep2 = asyncio.run(_svc(store).seed(scope, codebase="wpa", repo_path="/repo"))
    assert rep2["seeded"] == 0 and store.count(scope) == 1


def test_seed_light_reads_readme(store, tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text(
        "# demo\n本项目是蓝牙协议栈的参考实现,含 L2CAP 与 SDP 两个核心模块。\n", encoding="utf-8")
    model = _StubModel('[{"summary": "demo 是蓝牙协议栈参考实现", "detail": "含 L2CAP/SDP"}, '
                       '{"summary": "核心模块:L2CAP 与 SDP", "detail": ""}]')
    monkeypatch.setattr("rootrecall.platform.models.create_chat_model", lambda *a, **k: model)
    svc = _svc(store)

    rep = asyncio.run(svc.seed(Scope(codebase="wpa"), codebase="wpa",
                               repo_path=str(tmp_path), light=True))
    assert rep["seeded"] == 2
    gitems = store.list_items(Scope(codebase="general"))  # domain_knowledge 入共享池
    assert len(gitems) == 2 and all(i.kind == "domain_knowledge" for i in gitems)
    assert all(i.source_tier == "inferred" for i in gitems)

    rep2 = asyncio.run(svc.seed(Scope(codebase="wpa"), codebase="wpa",
                                repo_path=str(tmp_path), light=True))
    assert rep2["seeded"] == 0  # 幂等


def test_seed_honest_errors(store, monkeypatch, tmp_path):
    svc = _svc(store)
    # 图未建 → 指路 --graph-only / --light
    def _no_graph(name):
        raise FileNotFoundError("结构图未建")
    monkeypatch.setattr("rootrecall.services.code_index.code_graph.CodeGraph.open", _no_graph)
    with pytest.raises(ValueError, match="--graph-only"):
        asyncio.run(svc.seed(Scope(codebase="wpa"), codebase="wpa"))
    # --light 缺 repo_path → 明说
    with pytest.raises(ValueError, match="--repo-path"):
        asyncio.run(svc.seed(Scope(codebase="wpa"), codebase="wpa", light=True))
    # 轻 LLM 不可用(零 key)→ 指路(light 路先查 README 再建模型,先放个 README)
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")

    def _boom(*a, **k):
        raise RuntimeError("no api key")
    monkeypatch.setattr("rootrecall.platform.models.create_chat_model", _boom)
    with pytest.raises(ValueError, match="轻 LLM"):
        asyncio.run(svc.seed(Scope(codebase="wpa"), codebase="wpa", repo_path=str(tmp_path), light=True))
