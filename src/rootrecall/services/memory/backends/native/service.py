"""native 后端 · MemoryService 实现(R1 backends/native/service.py)。

把 store/recall/memorize/consolidate/structural 组装成一个 MemoryService。
from_config(cfg) 按 config.memory 构造全部依赖:
  - store      :MemoryStore(SQLite,知识项库)
  - embedder   :复用 code_index 的 embedder(embed=off 则 None)
  - reranker   :复用 code_index 的 reranker(rerank=off 则 None)
  - code_bundle:code_index 三元组(代码路;repo 没索引自动跳过)
  - structural :Noop(默认)| Crg(structural=crg)
  - model      :memory_extractor 角色(报告抽取用;无 key 则 None,降级成只收直接 KI)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rootrecall.platform.config import AppConfig
from rootrecall.services.memory.backends.native.consolidate import consolidate as _consolidate
from rootrecall.services.memory.backends.native.memorize import memorize_items
from rootrecall.services.memory.backends.native.memorize import memorize_report as _memorize_report
from rootrecall.services.memory.backends.native.recall import recall as _recall
from rootrecall.services.memory.backends.native.store import MemoryStore
from rootrecall.services.memory.backends.native.structural import (
    CrgStructuralBackend,
    NoopStructuralBackend,
    StructuralBackend,
)
from rootrecall.services.memory.manager import MemoryService
from rootrecall.services.memory.schema import KnowledgeItem, RecallHit, Scope, SourceTier

logger = logging.getLogger(__name__)


def _code_index_bundle(cfg: AppConfig) -> tuple[Any, Any, Any] | None:
    """构造 code_index 的 (embedder, LanceDBStore, reranker) 三元组(镜像 code_nav._retrieval_bundle)。

    code 路可选:repo 没建索引时 retrieve 返 [],自动跳过。code_index 没配好 → 返 None(不阻断 memory)。
    """
    try:
        from rootrecall.services.code_index.embed import create_embedder
        from rootrecall.services.code_index.retrieval import create_reranker
        from rootrecall.services.code_index.store import LanceDBStore

        embedder = create_embedder(cfg.code_index.embedding)
        from rootrecall.services.repos.registry import reanchor_data_path

        vs = getattr(cfg.code_index, "vector_store", None)
        vs_path = getattr(vs, "path", "data/code_index") if vs else "data/code_index"
        store = LanceDBStore(reanchor_data_path(vs_path))
        reranker = create_reranker(getattr(cfg.code_index, "reranker", None))
        return (embedder, store, reranker)
    except Exception as e:  # noqa: BLE001 - code_index 没配好不阻断 memory(只少 code 路)
        logger.warning("native: code_index bundle 构造失败,recall 将不带 code 路: %s", e)
        return None


class NativeMemoryService(MemoryService):
    """组合 code_index(语义)+ code-review-graph(结构,可选)的 v1 记忆后端。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        embedder: Any,
        reranker: Any,
        code_bundle: tuple[Any, Any, Any] | None,
        structural: StructuralBackend,
        model: Any,
        native_cfg: Any,
    ):
        self._store = store
        self._embedder = embedder
        self._reranker = reranker
        self._code_bundle = code_bundle
        self._structural = structural
        self._model = model
        self._ncfg = native_cfg

    @classmethod
    def from_config(cls, cfg: AppConfig, **host_hooks: Any) -> NativeMemoryService:
        from rootrecall.services.repos.registry import reanchor_data_path

        mcfg = cfg.memory
        ncfg = mcfg.native
        store = MemoryStore(reanchor_data_path(mcfg.store_path),
                            auto_index=ncfg.auto_index, ann_threshold=ncfg.ann_threshold)

        # embedder / reranker / code_bundle 都复用 code_index(embed/rerank=off 则不用)
        embedder = reranker = code_bundle = None
        if ncfg.embed != "off" or ncfg.rerank != "off":
            code_bundle = _code_index_bundle(cfg)
            if code_bundle is not None:
                embedder = code_bundle[0] if ncfg.embed != "off" else None
                reranker = code_bundle[2] if ncfg.rerank != "off" else None

        # 结构路:none(默认 Noop)| crg(CRG 适配器)
        structural: StructuralBackend = NoopStructuralBackend()
        if ncfg.structural == "crg":
            try:
                structural = CrgStructuralBackend(repo_root=cfg.sandbox.workspace)
            except Exception as e:  # noqa: BLE001 - CRG 没装/没图 → 降级 Noop,不崩
                logger.warning("native: CRG 结构路启用失败,降级 Noop: %s", e)

        # memory_extractor 模型(报告抽取);无 key → None(memorize 仍可收直接 KI)
        model = None
        try:
            from rootrecall.platform.models import create_chat_model

            model = create_chat_model(role="memory_extractor", config=cfg)
        except Exception as e:  # noqa: BLE001
            logger.warning("native: memory_extractor 模型构造失败,memorize_report 将不可用: %s", e)

        return cls(
            store=store, embedder=embedder, reranker=reranker, code_bundle=code_bundle,
            structural=structural, model=model, native_cfg=ncfg,
        )

    # ── tier-1 ──

    async def memorize(self, items: list[KnowledgeItem], scope: Scope) -> int:
        for it in items:
            if it.scope == Scope():  # 调用方没填 scope → 用参数的
                it.scope = scope
        return memorize_items(items, store=self._store, embedder=self._embedder, step=self._ncfg.merge_step)

    async def recall(self, query: str, scope: Scope, *, top_k: int | None = None) -> list[RecallHit]:
        hits = _recall(
            query, scope, store=self._store, repo=scope.codebase,
            top_k=top_k or self._ncfg.recall_top_k,
            embedder=self._embedder, reranker=self._reranker,
            code_bundle=self._code_bundle, structural=self._structural,
            halflife_days=self._ncfg.decay_halflife_days,
        )
        # 自转(建议 D):命中了 memory 路条目(有 item_id,刚被 bump 过 access_count)→ 后台异步跑一次 consolidate。
        # consolidate 自己判 access_count 达不达标(没达标 promoted=0,只扫表不改,微秒级)。fire-and-forget 不拖慢 recall。
        # 不挂 search():search 明确 bump=False(无 access_count 信号,挂了空跑)。
        if self._ncfg.auto_consolidate and any(h.item_id for h in hits):
            asyncio.create_task(self._safe_consolidate(scope))
        return hits

    async def _safe_consolidate(self, scope: Scope) -> None:
        """后台巩固(fire-and-forget):失败只记日志,绝不影响 recall 主流程(consolidate 是优化,不是 recall 契约)。"""
        try:
            stats = await self.consolidate(scope)
            if stats.get("promoted"):
                logger.info("memory auto-consolidate(%s): 升级 %d 条 mental_model", scope.codebase, stats["promoted"])
        except Exception as e:  # noqa: BLE001
            logger.warning("memory auto-consolidate 失败(不影响 recall): %s", e)

    # ── tier-2 ──

    async def search(self, query: str, scope: Scope, *, top_k: int = 5, **kw: Any) -> list[RecallHit]:
        """memory 路(不混 code/structural):recall 关掉 code/structure 两路 + 不 bump。"""
        return _recall(
            query, scope, store=self._store, repo=scope.codebase, top_k=top_k,
            embedder=self._embedder, reranker=self._reranker,
            code_bundle=None, structural=None,
            halflife_days=self._ncfg.decay_halflife_days, bump=False,
        )

    async def get(self, item_id: str, scope: Scope) -> KnowledgeItem | None:
        return self._store.get(item_id)

    async def list_items(self, scope: Scope, *, kind: str | None = None, include_invalid: bool = False) -> list[KnowledgeItem]:
        return self._store.list_items(scope, kind=kind, include_invalid=include_invalid)

    async def list_scopes(self) -> list[tuple[str, int]] | None:
        return self._store.list_scopes()

    # ── tier-3 ──

    async def consolidate(self, scope: Scope, *, repo_path: str | None = None) -> dict[str, Any]:
        """巩固(五 pass)。repo_path 给得出才做 B3 补丁已合入检测(reverse-apply 需要 git 仓路径)。

        recall 的自动 consolidate(_safe_consolidate)不传 repo_path —— 自动路径不知道仓在哪,
        B3 只在显式 consolidate(CLI --repo-path / 手动)时做,避免乱猜路径误判。
        """
        return _consolidate(
            scope, store=self._store,
            promote_access_count=self._ncfg.promote_access_count,
            stale_after_days=getattr(self._ncfg, "stale_after_days", 365.0),
            merged_discount=getattr(self._ncfg, "merged_upstream_discount", 0.5),
            repo_path=repo_path,
        )

    async def invalidate(self, item_id: str, scope: Scope, *, reason: str = "") -> bool:
        return self._store.set_invalid(item_id)

    def close(self) -> None:
        self._store.close()

    # ── native 专属:从报告抽取写入(CLI memory add --from-report 用)──

    async def memorize_report(
        self,
        report_text: str,
        scope: Scope,
        *,
        repo: str | None = None,
        commit_sha: str | None = None,
        source: str = "",
        source_tier: SourceTier = SourceTier.inferred,
    ) -> int:
        if self._model is None:
            logger.warning("native.memorize_report: 无 memory_extractor 模型,跳过抽取。")
            return 0
        return _memorize_report(
            report_text, repo=repo or scope.codebase, scope=scope,
            store=self._store, model=self._model, embedder=self._embedder,
            commit_sha=commit_sha, source=source, source_tier=source_tier,
            step=self._ncfg.merge_step,
        )
