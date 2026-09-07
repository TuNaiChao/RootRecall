"""L2 检索回归 eval 的 harness 自检(路线图④验收:改坏一处,分数必须掉)。

- test_stub_green:零 key stub 门全绿(BM25 强门 + hybrid 冒烟 + memory_recall 断言 + oracle 有效)。
- test_sabotage_fts:故意把 FTS 查询换成查不到的噪声词 —— BM25 强门必须变红
  (仍绿 = 回归网失明,比没有更糟);索引复用增量路,秒级。
- test_hash_embedder_deterministic:哈希向量跨实例确定、不同文本可区分(harness 的前提)。
"""

from __future__ import annotations

import numpy as np
import pytest

from rootrecall.services.code_index.eval.l2 import HashEmbedder, run_l2_stub


@pytest.fixture(scope="module")
def stub_base(tmp_path_factory):
    return tmp_path_factory.mktemp("rr-l2-stub")


def test_stub_green(stub_base):
    ok, text = run_l2_stub(base_dir=stub_base)
    assert ok, text


def test_sabotage_fts_breaks_gate(stub_base, monkeypatch):
    """harness 自检:FTS 查询构造被打坏(查询变噪声词)→ stub 门必须失败。"""
    from rootrecall.services.code_index import store as store_mod

    orig = store_mod.LanceDBStore.fts_search

    def mangled(self, repo, query, limit=20):
        return orig(self, repo, "qqqqzzqq 不存在token", limit=limit)

    monkeypatch.setattr(store_mod.LanceDBStore, "fts_search", mangled)
    ok, text = run_l2_stub(base_dir=stub_base, force=False)
    assert not ok, "FTS 被打坏后 harness 仍绿 —— 回归网失明"
    assert "BM25" in text


def test_hash_embedder_deterministic():
    a, b = HashEmbedder(), HashEmbedder()
    v1 = a.embed_query("create_embedder 工厂")
    v2 = b.embed_query("create_embedder 工厂")
    assert np.allclose(v1, v2)  # 跨实例确定(CI 可复现的前提)
    v3 = a.embed_query("完全不同的另一句话")
    cos = float(np.dot(v1, v3) / (np.linalg.norm(v1) * np.linalg.norm(v3)))
    assert cos < 0.99  # 不同文本可区分(512 桶带符号哈希下碰撞余弦远小于 1)
    assert a.fingerprint == b.fingerprint
