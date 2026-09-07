"""consolidate 提速的对拍与性能测试(路线图⑤,2026-09-07)。

- 对拍:桶式预分组(矛盾 pass)与分块矩阵 top-K 预筛(近邻 pass)对照**暴力参考实现**
  (旧版逐对逻辑,复用同一批 _same_subject/_same_conclusion/cosine 语义)——构造的边界例
  覆盖 symptom 路 / 证据回退路 / 混合路径 / 行窗内外 / kind_detail / 混合维度 / 零向量 / 无向量。
- 性能:合成 5000 条 consolidate() 全程 <10s(验收线)。
"""

from __future__ import annotations

import random
import tempfile
import time

import numpy as np

from rootrecall.services.memory.backends.native.consolidate import (
    _CONTRADICTION_MIN_CONFIDENCE,
    _DUPLICATE_COSINE_THRESHOLD,
    _count_duplicate_clusters,
    _detect_contradictions,
)
from rootrecall.services.memory.backends.native.memorize import _same_conclusion, _same_subject
from rootrecall.services.memory.backends.native.store import MemoryStore
from rootrecall.services.memory.schema import Evidence, KnowledgeItem, Scope, SourceTier

SCOPE = Scope(codebase="perf")


def _ki(summary, *, kind="bug_lesson", symptom="", root_cause="", file=None, line=None,
        embedding=None, kind_detail="module", confidence=0.8):
    return KnowledgeItem(
        kind=kind, repo="perf", scope=SCOPE, summary=summary, symptom=symptom,
        root_cause=root_cause, kind_detail=kind_detail, confidence=confidence,
        evidence=[Evidence(file=file or "f.c", line=line if line is not None else 1)],
        source_tier=SourceTier.delegate, embedding=embedding,
    )


def _store_with(items):
    s = MemoryStore(tempfile.mkdtemp())
    s.upsert(items)
    return s


# ── 暴力参考(旧版逐对逻辑;对拍基准)────────────────────────────────────────

def _brute_flagged(items):
    cands = [it for it in items
             if it.kind in ("codebase_fact", "bug_lesson")
             and it.confidence >= _CONTRADICTION_MIN_CONFIDENCE]
    flagged = set()
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            if _same_subject(a, b) and not _same_conclusion(a, b):
                flagged |= {a.id, b.id}
    return flagged


def _brute_cos(a, b):
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if va.shape[0] != vb.shape[0] or va.shape[0] == 0:
        return -1.0
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _brute_clusters(group, threshold):
    parent = {it.id: it.id for it in group}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(group):
        for b in group[i + 1:]:
            if _brute_cos(a.embedding, b.embedding) >= threshold:
                ra, rb = find(a.id), find(b.id)
                if ra != rb:
                    parent[ra] = rb
    clusters: dict[str, set] = {}
    for it in group:
        clusters.setdefault(find(it.id), set()).add(it.id)
    return {frozenset(c) for c in clusters.values() if len(c) >= 2}


# ── 1. 矛盾 pass 对拍:桶式预分组 == 暴力两两 ───────────────────────────────

def test_contradiction_bucketing_equivalent():
    rng = random.Random(7)
    items = [
        # symptom 路:同 symptom 不同根因 → 矛盾
        _ki("s1", symptom="P2P 扫描挂起", root_cause="rc-a", file="scan.c", line=100),
        _ki("s1b", symptom="P2P 扫描挂起", root_cause="rc-b", file="other.c", line=1),
        # 同 symptom 同根因 → 不矛盾
        _ki("s2", symptom="连接断开", root_cause="same", file="a.c"),
        _ki("s2b", symptom="连接断开", root_cause="same", file="b.c"),
        # 证据回退路:双空 symptom,同文件 ±5 行,不同根因 → 矛盾
        _ki("s3", symptom="", root_cause="x", file="m.c", line=10),
        _ki("s3b", symptom="", root_cause="y", file="m.c", line=14),
        # 同文件但行距 30(>5)→ 不矛盾
        _ki("s4", symptom="", root_cause="x", file="m.c", line=10),
        _ki("s4b", symptom="", root_cause="y", file="m.c", line=40),
        # 混合路:a 有 symptom、b 没有,同文件邻近,不同结论 → 矛盾(_same_subject 走证据回退)
        _ki("s5", symptom="有现象", root_cause="p", file="n.c", line=50),
        _ki("s5b", symptom="", root_cause="q", file="n.c", line=52),
        # 双方 symptom 都非空但不同,同文件邻近 → 不矛盾(symptom 路只看 symptom)
        _ki("s6", symptom="现象甲", root_cause="p", file="n.c", line=60),
        _ki("s6b", symptom="现象乙", root_cause="q", file="n.c", line=62),
        # codebase_fact:同 kind_detail + 同文件邻近 + 不同 summary → 矛盾
        _ki("f1", kind="codebase_fact", kind_detail="symbol", file="z.c", line=5),
        _ki("f1b", kind="codebase_fact", kind_detail="symbol", file="z.c", line=8),
        # kind_detail 不同 → 不矛盾
        _ki("f2", kind="codebase_fact", kind_detail="architecture", file="z.c", line=5),
        # 低置信 → 不参与
        _ki("low", symptom="低置信同款", root_cause="a", file="l.c", confidence=0.3),
        _ki("lowb", symptom="低置信同款", root_cause="b", file="l.c", confidence=0.3),
    ]
    # 随机打乱顺序入桶(桶式实现不依赖输入序)
    rng.shuffle(items)
    store = _store_with(items)
    new_n = _detect_contradictions(store, [store.get(it.id) for it in items])
    expect = _brute_flagged(items)
    after = [store.get(it.id) for it in items]
    got = {it.id for it in after if "needs_review" in it.tags}
    assert got == expect, f"桶式与暴力不一致:多 {got - expect} 少 {expect - got}"
    assert new_n == len(expect)
    assert new_n > 0  # 上面的构造里至少有 4 组矛盾


# ── 2. 近邻 pass 对拍:分块 top-K == 暴力两两(簇 ≤ K)─────────────────────

def test_duplicate_clusters_equivalent():
    rng = random.Random(11)
    dim = 16
    items = []
    # 三个真重复簇(随机单位向量 + 微扰,cos > 0.99)
    for c in range(3):
        base = list(rng.gauss(0, 1) for _ in range(dim))
        for j in range(4):
            v = np.asarray(base) + rng.gauss(0, 0.01) * np.asarray(base)
            items.append(_ki(f"dup-{c}-{j}", embedding=(v / np.linalg.norm(v)).tolist()))
    # 随机孤立项(cos ~ ±0.25,远低于 0.92)
    for i in range(30):
        v = np.asarray([rng.gauss(0, 1) for _ in range(dim)])
        items.append(_ki(f"rand-{i}", embedding=(v / np.linalg.norm(v)).tolist()))
    # 混合维度(换模型的存量)与零向量、无向量:都不该出边
    items.append(_ki("dim8", embedding=[0.1] * 8))
    items.append(_ki("zero", embedding=[0.0] * dim))
    items.append(_ki("novec", embedding=None))

    group = [it for it in items if it.embedding]
    new_n = _count_duplicate_clusters(group, _DUPLICATE_COSINE_THRESHOLD)
    expect = _brute_clusters(group, _DUPLICATE_COSINE_THRESHOLD)
    assert new_n == len(expect) == 3


# ── 3. 性能:合成 5000 条 consolidate 全程 <10s(验收线)────────────────────

def test_consolidate_5000_under_10s():
    rng = random.Random(42)
    dim = 16
    items = []
    for i in range(5000):
        kind = "bug_lesson" if i % 3 else "codebase_fact"
        v = np.asarray([rng.gauss(0, 1) for _ in range(dim)])
        items.append(_ki(
            f"item-{i}", kind=kind, symptom=f"symptom-{i}", root_cause=f"rc-{i % 97}",
            file=f"file_{i % 50}.c", line=rng.randrange(1, 4000),
            embedding=(v / np.linalg.norm(v)).tolist(),
            kind_detail=["module", "symbol", "architecture"][i % 3],
        ))
    # 埋 5 个重复簇(同向量 ×6)
    for c in range(5):
        v = [rng.gauss(0, 1) for _ in range(dim)]
        nrm = (v / np.linalg.norm(v)).tolist()
        for j in range(6):
            items.append(_ki(f"dup-{c}-{j}", embedding=nrm))
    store = _store_with(items)
    from rootrecall.services.memory.backends.native.consolidate import consolidate

    t0 = time.monotonic()
    stats = consolidate(SCOPE, store=store)
    dt = time.monotonic() - t0
    assert stats["scanned"] == 5030
    assert stats["duplicate_clusters"] >= 5  # 埋的簇都报出来
    assert dt < 10.0, f"consolidate 5030 条花了 {dt:.1f}s(验收线 10s)"
