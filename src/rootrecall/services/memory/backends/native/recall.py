"""native 后端 · 读路径(R1 backends/native/recall.py)。

多路召回 → RRF 融合 → (可选)rerank 精排 → 衰减/置信加权 → top-k(每条带溯源+置信+时效)。

四路(借 mnemopi polyphonic + 多路融合):
  - memory·BM25  :知识项库全文(store.search_bm25)
  - memory·vector:知识项库向量(store.search_vector,需 embedder)
  - code         :code_index 代码 chunk(现成 L1;可选,repo 未索引则跳过)
  - structural   :code-review-graph blast-radius(可选;未配 crg 则跳过)
RRF(K=60)融合各路排名 → reranker 精排(复用 code_index 的 reranker)→ 衰减×置信 → top-k。
命中的 memory 条顺手 bump_access(升级 mental_model 的依据)。
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

from rootrecall.services.memory.backends.native.store import MemoryStore
from rootrecall.services.memory.schema import KnowledgeItem, RecallHit, Scope

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF 常数(Cormack 2009;与 code_index retrieval 一致)
CORRECTED_PENALTY = 0.3  # 被纠正条目(corrected_by 非空)的检索降权因子(0.3 = 分数砍到 30%)


# ── KI → RecallHit(memory 路)──


def _ki_to_hit(ki: KnowledgeItem, score: float) -> RecallHit:
    return RecallHit(
        summary=ki.summary,
        score=score,
        source="memory",
        kind=ki.kind,
        repo=ki.repo,
        evidence=ki.evidence,
        confidence=ki.confidence,
        valid_at=ki.valid_at,
        created_at=ki.created_at,
        superseded_by=ki.superseded_by,
        corrected_by=ki.corrected_by,
        item_id=ki.id,
        tags=ki.tags,
    )


def _hit_key(h: RecallHit) -> str:
    """RRF 融合去重键:memory 路用 item_id;code/structural 路用 file+lines+summary。"""
    if h.item_id:
        return "m:" + h.item_id
    return f"c:{h.file}:{h.line_start}:{h.summary[:40]}"


def _rrf_fuse(ranked_lists: list[list[RecallHit]], k: int = RRF_K) -> list[RecallHit]:
    """多路排名 → RRF 融合:每条 score = Σ 1/(k+rank)。同 key 跨路累加(去重)。

    取每条首次出现那份的字段(保留最高排名路;字段并集是 backlog)。
    """
    scores: dict[str, float] = {}
    first: dict[str, RecallHit] = {}
    for ranked in ranked_lists:
        for rank, h in enumerate(ranked, start=1):
            key = _hit_key(h)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            first.setdefault(key, h)
    fused = sorted(first.values(), key=lambda h: scores[_hit_key(h)], reverse=True)
    for h in fused:
        h.score = scores[_hit_key(h)]
    return fused


def _apply_decay_confidence(hits: list[RecallHit], halflife_days: float, now: datetime) -> None:
    """原地把分乘上 衰减 exp(-age/halflife) × 置信加权(0.7+0.3·conf)。

    衰减只对带 valid_at 的(memory/报告类)生效;code/structural 无时效不衰减。
    (Weibull 留 backlog —— mnemopi 也未启用,生产实际跑 exp halflife。)
    """
    for h in hits:
        weight = 1.0
        if h.valid_at is not None:
            age_days = max(0.0, (now - h.valid_at).total_seconds() / 86400.0)
            weight *= math.exp(-age_days / max(halflife_days, 1e-6))
        weight *= 0.7 + 0.3 * (h.confidence or 0.0)
        # 被纠正条目(corrected_by 非空)额外降权:仍可见作参考,但排在纠正者后面。
        if h.corrected_by:
            weight *= CORRECTED_PENALTY
        h.score *= weight


def _code_voice(query: str, repo: str, code_bundle: Any, limit: int) -> list[RecallHit]:
    """调 code_index retrieve(现成 L1)。code_bundle=None 或表空 → 返 [](跳过该路)。

    code_bundle = (embedder, store, reranker) 三元组(镜像 tools/code_nav._retrieval_bundle)。
    """
    if code_bundle is None:
        return []
    try:
        embedder, store, reranker = code_bundle
    except (TypeError, ValueError):
        return []
    try:
        if store.count(repo) == 0:
            return []
    except Exception:  # noqa: BLE001
        return []
    try:
        from rootrecall.services.code_index.retrieval import retrieve

        res = retrieve(query, repo, embedder, store, reranker, top_k=limit)
    except Exception as e:  # noqa: BLE001 - code 路失败不阻断 memory 召回
        logger.warning("memory.recall: code_index 路失败,跳过: %s", e)
        return []
    out: list[RecallHit] = []
    for h in res.hits:
        out.append(RecallHit(
            summary=(h.text.splitlines()[0][:160] if h.text else h.symbol),
            score=h.score,
            source="code",
            repo=repo,
            file=h.file,
            line_start=h.start_line,
            line_end=h.end_line,
            snippet=(h.text[:200] if h.text else ""),
        ))
    return out


def recall(
    query: str,
    scope: Scope,
    *,
    store: MemoryStore,
    repo: str | None = None,
    top_k: int = 5,
    embedder: Any = None,
    reranker: Any = None,
    code_bundle: Any = None,
    structural: Any = None,
    halflife_days: float = 180.0,
    bump: bool = True,
) -> list[RecallHit]:
    """多路召回 + RRF + rerank + 衰减 → top-k RecallHit。

    各路可选:embedder=None 跳向量;code_bundle=None 跳代码路;structural=None 跳结构路。
    至少 memory·BM25 始终在(只要有 KI)。命中的 memory 条 bump_access(升级 mental_model 依据)。
    """
    now = datetime.now(UTC)
    cand = max(top_k * 4, 20)  # 每路多召一些喂 RRF(借 mnemopi/crg 宽召回)

    voices: list[list[RecallHit]] = []
    # memory·BM25(始终)
    voices.append([_ki_to_hit(ki, s) for ki, s in store.search_bm25(query, scope, repo=repo, limit=cand)])
    # memory·vector(需 embedder)—— 顺手记下每条的原始余弦(RRF 融合前),供工具层判语义相关度:
    # RRF 只看池内排名一致性,小池子里无关查询也满分;余弦才是「问的和记的是不是一回事」。
    qvec = None
    if embedder is not None:
        try:
            qvec = embedder.embed_query(query)
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.recall: 查询嵌向量失败,跳向量路: %s", e)
    sim_by_id: dict[str, float] = {}
    if qvec is not None:
        vhits = [_ki_to_hit(ki, s) for ki, s in store.search_vector(qvec, scope, repo=repo, limit=cand)]
        for h in vhits:
            if h.item_id:
                sim_by_id[h.item_id] = max(h.score, sim_by_id.get(h.item_id, -1.0))
        voices.append(vhits)
    # code_index(可选)
    if code_bundle is not None and repo:
        voices.append(_code_voice(query, repo, code_bundle, cand))
    # structural(可选;batch 5 的 StructuralBackend,duck-typed .blast_radius)
    if structural is not None and repo:
        try:
            voices.append(structural.blast_radius(query, repo=repo, limit=cand))
        except Exception as e:  # noqa: BLE001
            logger.warning("memory.recall: structural 路失败,跳过: %s", e)

    fused = _rrf_fuse(voices)
    if not fused:
        return []
    for h in fused:  # 语义相关度挂载(RRF 会覆盖 score;sim 保留向量路原始余弦)
        h.sim = sim_by_id.get(h.item_id or "")

    # 可选 rerank(复用 code_index 的 reranker):对融合后的 summary 精排,保留 2× 池子再衰减裁剪。
    if reranker is not None and len(fused) > 1:
        try:
            ranked = reranker.rerank(query, [h.summary for h in fused], top_n=min(len(fused), max(top_k * 2, top_k)))
            fused = [fused[idx] for idx, _s in ranked]
        except Exception as e:  # noqa: BLE001 - rerank 失败降级用融合序
            logger.warning("memory.recall: rerank 失败,降级融合序: %s", e)

    _apply_decay_confidence(fused, halflife_days, now)
    fused.sort(key=lambda h: h.score, reverse=True)  # 衰减/置信后重排(惩罚陈旧)
    top = fused[:top_k]

    if bump:  # 命中的 memory 条 bump_access
        for h in top:
            if h.item_id:
                try:
                    store.bump_access(h.item_id)
                except Exception:  # noqa: BLE001
                    pass
    return top
