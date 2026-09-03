"""记忆 backfill 测试(路线图③,2026-09-03)。

场景:零 key 期间 memorize(embedder=None)写入的条目无向量、只走 BM25;配 key 后
`rootrecall memory backfill` 补嵌 —— 只更新向量列(不触发 Bayes/合并)、幂等可重跑;
**纯语义查询**(与存储摘要零共同 token)能从向量路命中。
"""

from __future__ import annotations

import asyncio
import tempfile

import numpy as np
import pytest

from rootrecall.platform.config import NativeMemoryConfig
from rootrecall.services.memory.backends.native.memorize import memorize_items
from rootrecall.services.memory.backends.native.recall import recall
from rootrecall.services.memory.backends.native.service import NativeMemoryService
from rootrecall.services.memory.backends.native.store import MemoryStore
from rootrecall.services.memory.backends.native.structural import NoopStructuralBackend
from rootrecall.services.memory.schema import Evidence, KnowledgeItem, Scope, SourceTier

VEC_DIM = 8

# 摘要含中文暗号,查询用**零共同 token 的英文**(纯语义:BM25 必 miss,只有向量路能中)
SUMMARY_HF = "车载免提通话链路的建立流程"      # 暗号:免提 → 基向量 0
SUMMARY_A2DP = "高保真音频分发信道参数协商"    # 暗号:音频 → 基向量 1
QUERY_HF = "handsfree call establishment"
QUERY_A2DP = "audio distribution channel"


class FakeEmbedder:
    """确定性「语义」:中英暗号映射同一基向量,模拟跨语言语义检索。"""

    _batch_limit = 2  # 小批次,验证分批提交路径

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(VEC_DIM, dtype=np.float32)
        if "免提" in text or "handsfree" in text:
            v[0] = 1.0
        if "音频" in text or "audio" in text:
            v[1] = 1.0
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        self.calls = getattr(self, "calls", 0) + 1
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, query: str) -> np.ndarray:
        return self._vec(query)


@pytest.fixture
def store():
    s = MemoryStore(tempfile.mkdtemp())
    yield s
    s.close()


@pytest.fixture
def scope():
    return Scope(codebase="wpa")


def _svc(store, embedder) -> NativeMemoryService:
    return NativeMemoryService(
        store=store, embedder=embedder, reranker=None, code_bundle=None,
        structural=NoopStructuralBackend(), model=None,
        native_cfg=NativeMemoryConfig(),
    )


def _memorize_zero_key(store, scope) -> list[KnowledgeItem]:
    """零 key 期间写入两条(无向量)。"""
    items = [
        KnowledgeItem(kind="codebase_fact", repo="wpa", scope=scope, summary=SUMMARY_HF,
                      evidence=[Evidence(file="hfp.c", line=10)],
                      source_tier=SourceTier.delegate),
        KnowledgeItem(kind="codebase_fact", repo="wpa", scope=scope, summary=SUMMARY_A2DP,
                      evidence=[Evidence(file="a2dp.c", line=20)],
                      source_tier=SourceTier.delegate),
    ]
    memorize_items(items, store=store, embedder=None)
    return items


# ── 1. 验收主链:零 key 记 N 条 → backfill → 纯语义查询从向量路命中 ──────────
def test_backfill_semantic_hit(store, scope):
    ids = [it.id for it in _memorize_zero_key(store, scope)]
    emb = FakeEmbedder()
    # 补嵌前:纯语义查询(零共同 token)BM25 miss、无向量 → 召回空
    assert recall(QUERY_HF, scope, store=store, embedder=emb, reranker=None) == []
    rep = asyncio.run(_svc(store, emb).backfill(scope))
    assert rep == {"pending": 2, "embedded": 2}
    hits = recall(QUERY_HF, scope, store=store, embedder=emb, reranker=None)
    assert hits and hits[0].item_id == ids[0], "免提问答应命中 handsfree 条"
    hits2 = recall(QUERY_A2DP, scope, store=store, embedder=emb, reranker=None)
    assert hits2 and hits2[0].item_id == ids[1], "音频问答应命中 A2DP 条"


# ── 2. 幂等:重复跑零变更(不重嵌、不动置信度/条数)──────────────────────────
def test_backfill_idempotent(store, scope):
    items = _memorize_zero_key(store, scope)

    emb = FakeEmbedder()
    svc = _svc(store, emb)
    asyncio.run(svc.backfill(scope))
    before = [(store.get(it.id).confidence, store.get(it.id).valid_at) for it in items]
    calls_before = emb.calls
    rep = asyncio.run(svc.backfill(scope))
    assert rep == {"pending": 0, "embedded": 0}
    assert emb.calls == calls_before            # 没再发嵌请求
    after = [(store.get(it.id).confidence, store.get(it.id).valid_at) for it in items]
    assert before == after                      # 只补向量列:置信度/时间戳都不动
    assert store.count(scope) == 2              # 不触发合并,条数不变


# ── 3. dry-run:只列不写 ────────────────────────────────────────────────────
def test_backfill_dry_run(store, scope):

    items = _memorize_zero_key(store, scope)
    svc = _svc(store, FakeEmbedder())
    rep = asyncio.run(svc.backfill(scope, dry_run=True))
    assert rep["pending"] == 2
    assert {i["id"] for i in rep["items"]} == {it.id[:8] for it in items}
    assert all(store.get(it.id).embedding is None for it in items)  # 没写库


# ── 4. 零 key 诚实报错 ──────────────────────────────────────────────────────
def test_backfill_no_embedder_raises(store, scope):

    with pytest.raises(ValueError, match="embedder 不可用"):
        asyncio.run(_svc(store, None).backfill(scope))
