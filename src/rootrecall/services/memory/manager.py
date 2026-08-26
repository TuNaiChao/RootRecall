"""MemoryService 契约(照搬 deer-flow MemoryManager 的分层形状)+ 单例工厂 + 后端解析。

设计要点(对齐参考实现):
  - 分层 ABC(借 deer-flow agents/memory/manager.py:62):tier-1 抽象方法 memorize/recall
    每个后端必须实现;tier-2/3 带默认 raise NotImplementedError,后端按需覆盖 → 新后端
    增量可上(只实现核心,其余用默认)。
  - from_config classmethod:每个后端自己组装依赖(借 deer-flow from_config)。
  - 后端可换(借 oh-my-pi resolve.ts + deer-flow drop-in 扫描):丢一个 backends/<name>/
    文件夹(暴露 BACKEND_CLASS)+ 配置 memory.backend;<name> 也支持 'pkg.mod:Cls' 点路径。
  - 单例 get_memory_service()(double-checked lock,借 deer-flow get_memory_manager)。
  - 拒绝静默回退:后端名配错必须报错(记忆是持久状态,不能偷偷用别的)。

注:本类是纯 abc.ABC(不继承 pydantic BaseModel)。RootRecall 的 service 持有运行时句柄
(SQLite 连接 / embedder / reranker),不是"配置即字段";deer-flow 用 BaseModel 是因为它
要配置字段化 + 序列化,需求不同,不照搬。
"""

from __future__ import annotations

import abc
import importlib
import threading
from pathlib import Path
from typing import Any, ClassVar

from rootrecall.services.memory.schema import KnowledgeItem, RecallHit, Scope, SourceTier

# 后端文件夹约定(借 deer-flow backends/__init__.py drop-in 契约)。
_BACKENDS_DIR = Path(__file__).parent / "backends"
_BACKEND_CLASS_ATTR = "BACKEND_CLASS"  # 每个 backends/<name>/__init__.py 必须暴露这个


class MemoryService(abc.ABC):
    """记忆核心契约(分层,借 deer-flow MemoryManager)。

    类比:这是"笔记本"的标准接口 —— 不管后端是自家的 SQLite(native)还是外接的
    mem0/cognee,对外都只认 memorize(记)和 recall(翻)这两个核心动作。
    """

    # 声明能力(借 deer-flow supports_search 不变量):须与 search 是否真覆盖一致。
    supports_search: ClassVar[bool] = True

    # ── tier-1:每个后端必须实现 ──
    @abc.abstractmethod
    async def memorize(self, items: list[KnowledgeItem], scope: Scope) -> int:
        """记:把知识项写入记忆(重复 id → 合并/加权,不新增)。返回写入/更新的条数。"""

    @abc.abstractmethod
    async def recall(self, query: str, scope: Scope, *, top_k: int = 5) -> list[RecallHit]:
        """翻:按 query 多路召回(语义+结构)→ 融合 → top-k,每条带溯源+置信度+时效。"""

    # ── tier-2:管理(默认未实现,后端按需覆盖)──
    async def search(self, query: str, scope: Scope, *, top_k: int = 5, **kw: Any) -> list[RecallHit]:
        """语义检索(等价于 recall 的 memory 路,不混 code/structural)。"""
        raise NotImplementedError(f"{type(self).__name__} 未实现 search")

    async def get(self, item_id: str, scope: Scope) -> KnowledgeItem | None:
        """按 id 取一条(含已失效的)。"""
        raise NotImplementedError(f"{type(self).__name__} 未实现 get")

    async def list_items(self, scope: Scope, *, kind: str | None = None, include_invalid: bool = False) -> list[KnowledgeItem]:
        """列某 scope 的知识项(可按 kind 过滤、可含已失效)。"""
        raise NotImplementedError(f"{type(self).__name__} 未实现 list_items")

    async def list_scopes(self) -> list[tuple[str, int]] | None:
        """非空作用域及条数(可选能力;后端不支持 → None)。recall 空池提示用,治
        「没传 codebase 探默认空池」的盲试(2026-08-26 实测)。"""
        return None

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
        """从一份报告文本抽取知识项并写入(extract + memorize)。需 LLM;后端不支持则 raise。"""
        raise NotImplementedError(f"{type(self).__name__} 未实现 memorize_report")

    # ── tier-3:可选钩子 ──
    async def consolidate(self, scope: Scope, *, repo_path: str | None = None) -> dict[str, Any]:
        """巩固:升级/矛盾检测/去重/补丁已合入/stale(借 mnemopi + 2026 业界 keeps/merges/evicts)。

        repo_path 可选:给得出才做"补丁已合入上游"检测(git reverse-apply 需要);None 跳过该 pass。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 consolidate")

    async def invalidate(self, item_id: str, scope: Scope, *, reason: str = "") -> bool:
        """显式失效一条(如补丁已在上游合入)。bi-temporal 软删,不物理删。"""
        raise NotImplementedError(f"{type(self).__name__} 未实现 invalidate")

    def close(self) -> None:  # noqa: B027 - 可选覆盖钩子(默认 no-op;有后台线程/连接的后端才覆盖)
        """释放资源(默认空;有后台线程/连接的后端覆盖)。"""

    @classmethod
    @abc.abstractmethod
    def from_config(cls, cfg: Any, **host_hooks: Any) -> MemoryService:
        """每个后端自己组装依赖(借 deer-flow from_config)。cfg = AppConfig。"""


# ──────────────────────────────────────────────────────────────────────────
# 后端解析:drop-in 文件夹扫描 + 'pkg.mod:Cls' 点路径(借 deer-flow resolver)
# ──────────────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type[MemoryService]] | None = None
_REGISTRY_LOCK = threading.Lock()
_SERVICE: MemoryService | None = None
_SERVICE_LOCK = threading.Lock()


def discover_backends() -> dict[str, type[MemoryService]]:
    """扫 backends/<name>/__init__.py 找 BACKEND_CLASS(借 deer-flow drop-in 扫描)。

    可选后端缺依赖(如 mem0/cognee 未装)→ import 失败静默跳过,不崩核心;
    已装的进 registry。结果缓存(配合 reset_memory_service 清)。
    """
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    registry: dict[str, type[MemoryService]] = {}
    if _BACKENDS_DIR.is_dir():
        for entry in sorted(_BACKENDS_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            if not (entry / "__init__.py").is_file():
                continue
            dotted = f"rootrecall.services.memory.backends.{entry.name}"
            try:
                mod = importlib.import_module(dotted)
            except Exception:  # noqa: BLE001 - 可选后端缺依赖 → 跳过,不崩核心
                continue
            cls = getattr(mod, _BACKEND_CLASS_ATTR, None)
            if isinstance(cls, type) and issubclass(cls, MemoryService):
                registry[entry.name] = cls
    _REGISTRY = registry
    return registry


def resolve_backend_class(name: str) -> type[MemoryService]:
    """解析后端类:① 已扫描的短名(backends/<name>/);② 'pkg.mod:Cls' 点路径。

    借 deer-flow resolver,且拒绝静默回退 —— 记忆是持久状态,配错必须报错。
    """
    registry = discover_backends()
    if name in registry:
        return registry[name]
    if ":" in name:  # 点路径(与模型工厂 use: 同形 'pkg.mod:Cls')
        from rootrecall.platform.reflection import resolve_class

        return resolve_class(name, MemoryService)
    available = ", ".join(sorted(registry)) or "(无;先在 backends/ 加文件夹)"
    raise ValueError(
        f"未知 memory 后端 {name!r}(可用:{available});或在 backends/ 加文件夹,或用 'pkg.mod:Cls' 点路径。"
    )


def get_memory_service(config: Any = None) -> MemoryService:
    """单例(double-checked lock,借 deer-flow get_memory_manager)。

    首次调用按 cfg.memory.backend 解析后端类并 from_config 构造;之后复用。
    config 可注入(测试用);默认 get_app_config()。
    """
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None:
            return _SERVICE
        from rootrecall.platform.config import get_app_config

        cfg = config or get_app_config()
        mcfg = getattr(cfg, "memory", None)
        name = getattr(mcfg, "backend", "native") if mcfg else "native"
        cls = resolve_backend_class(name)
        _SERVICE = cls.from_config(cfg)
        return _SERVICE


def reset_memory_service() -> None:
    """清单例 + registry 缓存(测试切后端 / 配置重载时用)。"""
    global _SERVICE, _REGISTRY
    with _SERVICE_LOCK:
        _SERVICE = None
    with _REGISTRY_LOCK:
        _REGISTRY = None
