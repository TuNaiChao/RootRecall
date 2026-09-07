"""记忆冷启动播种(路线图⑥,2026-09-07)。

问题:新仓接入时记忆库是空的,recall-first 无从短路,agent 每次都从零读码。
seed 播一批「低置信起步卡」填这个真空:

- **full 档**(默认):CodeGraph 社区(Leiden 模块边界)出模块清单 → 每模块一次轻 LLM
  (title 便宜角色)写一条 codebase_fact(职责 / 入口);**evidence 带真 file:line**
  (从图节点取,LLM 只负责措辞,不负责编锚点)。
- **--light 档**:不建图也可用 —— 摄取 README / CHANGELOG 成 domain_knowledge
  (入 general 共享池,同 domain-research 约定)。

全标 ``source_tier: inferred``(权重 0.7、初始置信 0.35):真结论经 Bayes 自然接管 ——
agent 读码坐实后以 delegate 档(0.95)重提同主题,置信一票压过播种卡;错了也不污染,
正确姿势是 memorize 新卡,不是信 seed。幂等:同 summary → 同 id → 已存在直接跳过,
重复跑零变更。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from rootrecall.services.memory.backends.native.memorize import _init_confidence
from rootrecall.services.memory.backends.native.store import MemoryStore
from rootrecall.services.memory.schema import Evidence, KnowledgeItem, Scope, SourceTier

logger = logging.getLogger(__name__)

_PROMPT_MODULE = """你在给代码库写冷启动记忆。下面是一个模块(社区检测聚类)的真实成员样本(文件::符号,带行号):

{members}

写一条中文 codebase_fact,严格输出一行 JSON(不要多余文字):
{{"summary": "<这个模块负责什么,一句话,点名最可能的入口符号>", "entry": "<入口符号名 + 所在文件:行>"}}
要求:只用成员样本里真实出现过的文件与符号,不确定的别写。"""


def _cheap_model():
    """title 便宜角色(seed 专用:一次一短句,不值得上 pro)。失败 → ValueError 指路。"""
    from rootrecall.platform.models import create_chat_model

    try:
        return create_chat_model(role="title")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"轻 LLM(title 角色)不可用,seed 写不了卡:{e}(配好 key 再跑;--light 也需要 LLM)") from e


def _invoke_json(model, prompt: str) -> Any | None:
    """调一次模型,从回复里抠第一个 JSON 对象/数组;解析失败返 None(调用方跳过)。"""
    try:
        resp = model.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001 —— 单条失败跳过不阻断整轮 seed
        logger.warning("memory.seed: LLM 输出解析失败,跳过一条:%s", e)
        return None


def _node_field(n: Any, key: str, default: Any = None) -> Any:
    """CRG 节点双形态取字段:GraphNode 对象(attr)或 dict(测试桩)。"""
    if isinstance(n, dict):
        return n.get(key, default)
    return getattr(n, key, default)


def _relativize(file_path: str, repo_root: str | None) -> str:
    """图节点存的是带 repo_root 前缀的绝对路径;能对上就剥成仓内相对路径(记忆可移植)。"""
    p = (file_path or "").strip()
    if repo_root:
        root = str(repo_root).rstrip("/")
        if p.startswith(root + "/"):
            return p[len(root) + 1:]
    return p


def seed_from_graph(codebase: str, *, scope: Scope, store: MemoryStore,
                    max_modules: int = 12, repo_path: str | None = None,
                    embedder=None) -> dict[str, Any]:
    """full 档:社区 → 轻 LLM → codebase_fact(inferred)。返回 {modules, seeded, skipped, no_vector}。"""
    from rootrecall.services.memory.backends.native.memorize import _embed_items
    from rootrecall.services.memory.schema import make_id

    try:
        from rootrecall.services.code_index.code_graph import CodeGraph
    except Exception as e:  # noqa: BLE001 —— CRG 未装
        raise ValueError(f"结构图后端未装:{e}。装它:`uv sync --extra code-review-graph`;"
                         f"或用 --light(不需要图)。") from e
    try:
        cg = CodeGraph.open(codebase)
    except FileNotFoundError as e:
        raise ValueError(f"代码库 '{codebase}' 的结构图未建:{e}。先 "
                     f"`uv run rootrecall index <仓库路径> {codebase} --graph-only`,或用 --light。") from e

    # 走 architecture_overview():其 communities 带 members 全量清单(裸 communities() 的
    # sample_members 实测为空,真机抓出)。
    communities = sorted(cg.architecture_overview().get("communities", []),
                         key=lambda c: c.get("size") or c.get("member_count") or 0, reverse=True)
    model = _cheap_model()
    items: list[KnowledgeItem] = []
    skipped = 0
    # 锚点级幂等:真 LLM 两次措辞不同 → summary id 不同,纯 id 判重会重复播。
    # 以「本 scope 已 seed 卡的首证据文件」为锚 —— 同一模块(社区首文件稳定)已播过就跳过。
    seeded_files = {it.evidence[0].file for it in store.list_items(scope)
                    if (it.source or "").startswith("memory-seed") and it.evidence}
    for comm in communities[:max_modules]:
        members = comm.get("members") or comm.get("sample_members") or []
        if len(members) < 2:
            continue  # 孤点社区(1 个成员)没有「模块职责」可言
        root = repo_path or _root_of(members)
        # 行号从图节点取(社区清单只有 qualified name);拿前几个成员即可。
        # CRG 返回 GraphNode 对象(真机),测试桩给 dict —— _node_field 双形态兼容。
        try:
            nodes = cg._store._batch_get_nodes(members[:8])  # noqa: SLF001 —— 真 file:line 锚点只能从图节点拿
        except Exception:  # noqa: BLE001 —— 拿不到节点就退样本名(仍真实,只是没行号)
            nodes = []
        node_list = [n for n in (nodes or []) if _node_field(n, "file_path")]
        lines = [f"- {_relativize(_node_field(n, 'file_path'), root)}::{_node_field(n, 'name')}  L{_node_field(n, 'line_start')}"
                 for n in node_list] or [f"- {m}" for m in members[:8]]
        out = _invoke_json(model, _PROMPT_MODULE.format(members="\n".join(lines)))
        if not isinstance(out, dict) or not out.get("summary"):
            skipped += 1
            continue
        ev_file = _relativize(_node_field(node_list[0], "file_path"), root) if node_list else None
        if ev_file and ev_file in seeded_files:
            skipped += 1  # 锚点级幂等:该模块(首文件)已播过
            continue
        item = KnowledgeItem(
            kind="codebase_fact", repo=codebase, scope=scope,
            summary=str(out["summary"]).strip(), detail=str(out.get("entry") or "").strip(),
            kind_detail="module", source_tier=SourceTier.inferred,
            evidence=[Evidence(file=ev_file, line=_node_field(node_list[0], "line_start") or 1)]
            if node_list else [],
            source="memory-seed",
        )
        item.id = make_id(scope, item.kind, item.summary)
        item.confidence = _init_confidence(item.source_tier)
        if store.get(item.id) is not None:
            skipped += 1  # id 级幂等:同 summary 同 id(桩/确定性场景)
            continue
        if ev_file:
            seeded_files.add(ev_file)
        items.append(item)
    if items:
        _embed_items(items, embedder)  # 零 key → 跳过(配 key 后 memory backfill 补)
        store.upsert(items)
    return {"modules": len(communities[:max_modules]), "seeded": len(items),
            "skipped": skipped, "no_vector": embedder is None and bool(items)}


def seed_light(repo_path: str, *, repo: str, scope: Scope, store: MemoryStore,
               embedder=None) -> dict[str, Any]:
    """--light 档:README / CHANGELOG → domain_knowledge(general 池)。返回 {seeded, skipped}。

    不需要图 / 不需要索引;仍需轻 LLM(提炼也是生成)。scope 由调用方传 general 池。
    """
    from rootrecall.services.memory.backends.native.memorize import _embed_items
    from rootrecall.services.memory.schema import make_id

    root = Path(repo_path)
    docs = []
    for name in ("README.md", "README", "CHANGELOG.md"):
        f = root / name
        if f.exists():
            docs.append(f"### {name}\n" + f.read_text(encoding="utf-8", errors="replace")[:8000])
    if not docs:
        raise ValueError(f"{root} 下没有 README/CHANGELOG 可摄取(--light 的原料)。")

    model = _cheap_model()
    prompt = ("你在给代码库写冷启动记忆。下面是它的 README/CHANGELOG:\n\n" + "\n\n".join(docs)
              + "\n\n提炼 3-5 条中文 domain_knowledge(这个项目是什么 / 关键概念 / 模块构成 / 版本线,"
                "只写文档明确支持的)。严格输出 JSON 数组,每项 "
                '{"summary": "<一句话>", "detail": "<展开一两句>"}')
    out = _invoke_json(model, prompt)
    items: list[KnowledgeItem] = []
    skipped = 0
    for row in out if isinstance(out, list) else []:
        if not isinstance(row, dict) or not row.get("summary"):
            skipped += 1
            continue
        it = KnowledgeItem(
            kind="domain_knowledge", repo=repo, scope=scope,
            summary=str(row["summary"]).strip(), detail=str(row.get("detail") or "").strip(),
            source_tier=SourceTier.inferred, source="memory-seed-light",
        )
        it.id = make_id(scope, it.kind, it.summary)
        it.confidence = _init_confidence(it.source_tier)
        if store.get(it.id) is not None:
            skipped += 1
            continue
        items.append(it)
    if items:
        _embed_items(items, embedder)  # 零 key → 跳过(配 key 后 memory backfill 补)
        store.upsert(items)
    return {"seeded": len(items), "skipped": skipped}


def _root_of(members: list[str]) -> str | None:
    """从 qualified name(绝对路径::符号)猜 repo_root(最长公共目录前缀的上一级)。"""
    try:
        paths = [m.split("::")[0] for m in members if "::" in m]
        if not paths:
            return None
        prefix = Path(paths[0])
        for p in paths[1:]:
            while prefix != Path(prefix.anchor) and not str(p).startswith(str(prefix)):
                prefix = prefix.parent
        return str(prefix) if prefix != Path(prefix.anchor) else None
    except Exception:  # noqa: BLE001
        return None
