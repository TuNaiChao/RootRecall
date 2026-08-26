"""retrieval 重排池扩展 + 符号粒度先验测试(全离线:假 embedder/store/reranker)。

2026-08-18 L2 概念查询粒度错位改进的回归保护:
① reranker 拿全部候选重排(旧实现 top_n=top_k,gold 落 rank 6+ 永远看不见);
② 重排分 × 粒度先验(module 强降/私有·嵌套轻降/公共入口不动)再排序。
"""

from __future__ import annotations

import numpy as np

from rootrecall.services.code_index.retrieval import (
    _PRIOR_INTERNAL,
    _PRIOR_MODULE,
    _granularity_prior,
    retrieve,
)

# ── _granularity_prior:纯函数分档 ────────────────────────────────────────────


def test_prior_buckets():
    assert _granularity_prior("module", "x/y.py") == _PRIOR_MODULE  # module 块强降
    assert _granularity_prior("function", "_parse_bytes") == _PRIOR_INTERNAL  # 私有顶层函数
    assert _granularity_prior("function", "_extract_symbols.visit") == _PRIOR_INTERNAL  # 私有嵌套
    assert _granularity_prior("function", "create_embedder.get") == _PRIOR_INTERNAL  # 嵌套(无下划线)
    assert _granularity_prior("method", "RemoteReranker._build_body") == _PRIOR_INTERNAL  # 私有方法
    assert _granularity_prior("function", "parse_file") == 1.0  # 公共顶层函数不降
    assert _granularity_prior("class", "LanceDBStore") == 1.0  # 公共类不降
    assert _granularity_prior("method", "LanceDBStore.upsert") == 1.0  # 公共方法不降


# ── _testinfra_prior:路径级测试基建先验(2026-08-26 实测:bluez「连接流程」top-6
#    全是 emulator/android 外围,核心入口挤不进)──────────────────────────────────


def test_testinfra_prior_buckets():
    from rootrecall.services.code_index.retrieval import _PRIOR_TESTINFRA, _testinfra_prior

    assert _testinfra_prior("emulator/bthost.c") == _PRIOR_TESTINFRA       # 仿真目录
    assert _testinfra_prior("unit/test_foo.c") == _PRIOR_TESTINFRA         # test 目录段
    assert _testinfra_prior("src/mgmt-tester.c") == _PRIOR_TESTINFRA       # -tester 文件名
    assert _testinfra_prior("src/android_test.c") == _PRIOR_TESTINFRA      # _test 文件名
    assert _testinfra_prior("") == 1.0                                     # 空路径不降
    assert _testinfra_prior("src/device.c") == 1.0                         # 产品代码不降
    assert _testinfra_prior("android/gatt.c") == 1.0                       # 真构建变体不降(交给 rerank)
    assert _testinfra_prior("latest/greatest.c") == 1.0                    # 子串不算,按路径段判


def test_testinfra_prior_demotes_peripheral_in_retrieve():
    """rerank 同分下,emulator 外围符号被路径先验压下、src/ 核心入口顶上(实测翻车形状)。"""
    from rootrecall.services.code_index.retrieval import _PRIOR_TESTINFRA

    def cand_at(symbol: str, path: str) -> dict:
        c = _cand(symbol, "function")
        c["file"] = path
        c["id"] = f"{path}:{symbol}"
        return c

    cands = [
        cand_at("bthost_send_cmd", "emulator/bthost.c"),   # 外围,重排分高
        cand_at("device_connect_le", "src/device.c"),      # 核心入口,重排分低
    ]
    rr = _FixedReranker({"bthost_send_cmd": 0.60, "device_connect_le": 0.50})
    res = retrieve("connect flow", "repo", _FakeEmbedder(), _FakeStore(cands), rr, top_k=2)
    # 0.60×0.70=0.42 < 0.50×1.0 —— 核心入口反超(与粒度先验同乘法语义)
    assert res.hits[0].symbol == "device_connect_le"
    assert res.hits[1].symbol == "bthost_send_cmd"
    assert res.hits[1].score == 0.60 * _PRIOR_TESTINFRA


# ── retrieve:池扩满 + 先验重排 ───────────────────────────────────────────────


class _FakeEmbedder:
    """假 embedder:向量无关紧要(假 store 不真检索)。"""

    fingerprint = "fake"

    def embed_query(self, query: str) -> np.ndarray:
        return np.zeros(4, dtype=np.float32)


class _FakeStore:
    """假 store:固定候选列表,顺序即「hybrid 召回序」。"""

    def __init__(self, candidates: list[dict]):
        self._cands = candidates
        self.seen_limit = None

    def hybrid_search(self, repo, query_vec, fts_query, limit, where=None):
        self.seen_limit = limit
        return self._cands


class _FixedReranker:
    """假 reranker:按预设分数表返回,记录收到的 top_n(验池扩满)。"""

    def __init__(self, scores: dict[str, float]):
        self._scores = scores  # symbol -> 分数
        self.seen_top_n = None

    def rerank(self, query, documents, top_n):
        self.seen_top_n = top_n
        # documents 是 fts_text;用分数表(symbol 名在 fts_text 里)按序返回全部
        out = []
        for sym, s in self._scores.items():
            for i, d in enumerate(documents):
                if sym in d:
                    out.append((i, s))
                    break
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:top_n]


def _cand(symbol: str, kind: str) -> dict:
    return {
        "id": f"x.py:{symbol}", "symbol": symbol, "kind": kind, "file": "x.py",
        "start_line": 1, "end_line": 2, "text": f"def {symbol}(): pass",
        "fts_text": f"{symbol} some words", "_relevance_score": 0.5,
    }


def test_pool_expanded_and_prior_reorders():
    """① reranker 拿到全部候选(top_n=6,不是 top_k);② module/私有被先验压下,
    公共入口顶上来;③ score=先验调整分,extra['rerank_score'] 留原始分。"""
    cands = [
        _cand("x/y.py", "module"),          # 0.60 强噪声:module 压头(真实事故形状)
        _cand("_parse_bytes", "function"),  # 0.55 私有 helper
        _cand("parse_file", "function"),    # 0.50 公共入口(gold)
        _cand("Parser", "class"),           # 0.45 公共类
        _cand("outer.local_fn", "function"),  # 0.40 嵌套函数
        _cand("other", "function"),         # 0.30 路人
    ]
    rr = _FixedReranker({
        "x/y.py": 0.60, "_parse_bytes": 0.55, "parse_file": 0.50,
        "Parser": 0.45, "outer.local_fn": 0.40, "other": 0.30,
    })
    res = retrieve("q", "repo", _FakeEmbedder(), _FakeStore(cands), rr, top_k=5)

    assert rr.seen_top_n == 6  # 池扩满:全部候选都给了 reranker(不是 top_k=5)
    assert res.out_mode == "hybrid+rerank"
    order = [h.symbol for h in res.hits]
    # 先验调整后:parse_file 0.50×1.0 > Parser 0.45×1.0 > _parse_bytes 0.55×0.80=0.44
    #   > module 0.60×0.65=0.39 > local_fn 0.40×0.80=0.32 > other 0.30
    assert order[0] == "parse_file"          # 公共入口顶到第一
    assert order[1] == "Parser"              # 公共类(先验 1.0)压过私有 helper(0.44)
    assert order[2] == "_parse_bytes"        # 私有降而不剔,还在前排
    assert "x/y.py" not in order[:3]         # module 压头被治(0.39 掉到第 4)
    hit0 = res.hits[0]
    assert hit0.score == 0.50 and hit0.extra["rerank_score"] == 0.50  # 公共入口先验=1,原分=终分
    hit_mod = next(h for h in res.hits if h.symbol == "x/y.py")
    assert hit_mod.score < hit_mod.extra["rerank_score"]  # 终分 < 原分(先验生效,可观测)


def test_prior_off_is_pure_rerank_order():
    """apply_prior=False(消融开关):排序完全按重排原始分,module 回到第一。"""
    cands = [
        _cand("x/y.py", "module"), _cand("parse_file", "function"),
    ]
    rr = _FixedReranker({"x/y.py": 0.60, "parse_file": 0.50})
    res = retrieve("q", "repo", _FakeEmbedder(), _FakeStore(cands), rr, top_k=2, apply_prior=False)
    assert [h.symbol for h in res.hits] == ["x/y.py", "parse_file"]


def test_gold_beyond_top_k_now_reachable():
    """真实事故形状(Q4):gold 排重排第 9 —— 旧实现 top_n=top_k=5 直接看不见它;
    池扩满(看得见)+ module 先验(压噪声)→ gold 顶到第一。"""
    cands = [_cand(f"m{i}.py", "module") for i in range(8)] + [_cand("real_entry", "function")]
    scores = {f"m{i}.py": 0.50 - i * 0.001 for i in range(8)}
    scores["real_entry"] = 0.45  # 重排原始序排第 9(>5,旧实现永远丢)
    rr = _FixedReranker(scores)
    res = retrieve("q", "repo", _FakeEmbedder(), _FakeStore(cands), rr, top_k=5)
    assert rr.seen_top_n == 9  # gold 确实进入了重排视野
    assert res.hits[0].symbol == "real_entry"  # 0.45×1.0 > 0.50×0.65=0.325:先验把它顶到第一
