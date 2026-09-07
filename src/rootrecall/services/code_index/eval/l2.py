"""L2 检索回归 eval(路线图④前半,2026-09-07):检索质量不被后续改动磨掉的回归网。

两种模式(入口 `rootrecall eval run --level retrieval --mode stub|live`):
- **stub(默认,进 CI)**:HashEmbedder(确定性哈希向量,零 API、秒级)+ rerank off。
  测的是检索**管道**回归(chunk 主键 / BM25 / 向量路 / RRF / 指标口径)和 oracle
  有效性(gold id 必须都在索引里),**不是**语义质量 —— 语义质量要 key,live 模式管。
  外加 memory_recall 侧断言(同一 stub embedder 播种 + 召回命中)。
- **live(有 key 的机器)**:真 embedder + 真 reranker 跑全指标,报告落 data/eval/
  带时间戳 JSON,并与上一份 diff —— 语义质量的跑分对比底座从这里出数。
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[3]  # …/src/rootrecall(索引根 = eval gold 路径契约)
_EVAL_SET = Path(__file__).resolve().parents[5] / "eval" / "sets" / "rootrecall.jsonl"

_HASH_DIM = 512  # 桶数太少会让哈希碰撞把向量路变均匀噪声、RRF 融合反被污染(64 维实测 hit@5 只有 0.18~0.36)


class HashEmbedder:
    """确定性特征哈希 embedder(L2 stub 模式专用)。

    token → 带符号哈希(桶位 + 符号,标准 feature hashing):公共 token 的贡献概率性
    抵消、稀有 token 主导,判别度远高于朴素计数哈希(后者让向量路退化为均匀噪声、
    RRF 大量平票,首版实测 hit@5 只有 0.18)。token 集 = 英文词(含 snake/camel 拆词)
    + CJK 二元组(tantivy 默认分词不吃中文,向量路是中文查询的唯一信号)。
    跨进程/跨机器稳定;语义质量仍为零 —— stub 测管道回归,质量归 live 模式。
    """

    @property
    def dim(self) -> int:
        return _HASH_DIM

    @property
    def fingerprint(self) -> str:
        return f"hash-stub|{_HASH_DIM}|v3-signed"

    def _tokens(self, text: str) -> list[str]:
        out: list[str] = []
        for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+", text.lower()):
            if re.match(r"^[\u4e00-\u9fff]+$", w):
                out += [w[i:i + 2] for i in range(len(w) - 1)] or [w]
            else:
                out += [p for p in w.split("_") if p] or [w]
        return out

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(_HASH_DIM, dtype=np.float32)
        for tok in self._tokens(text):
            b = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            h = int.from_bytes(b, "big")
            v[h % _HASH_DIM] += 1.0 if (h >> 62) & 1 else -1.0
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def embed_chunks(self, chunks: list) -> np.ndarray:
        if not chunks:
            return np.zeros((0, _HASH_DIM), dtype=np.float32)
        return np.stack([self._vec(c.text) for c in chunks])

    def embed_query(self, query: str) -> np.ndarray:
        return self._vec(query)

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, _HASH_DIM), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    def warm(self) -> None:
        pass


# stub 模式阈值(2026-09-07 首跑实测后按 measured−5pt 校准):
# - BM25 质量门:哈希向量是噪声、RRF 掺它会稀释 BM25 强信号 → 质量门跑纯 BM25。
#   注意纯 BM25 排不过短包装 chunk(cmd_mcp 压过 build_server)——L1≈0.62 是 BM25
#   单路的真实水平,生产路径靠 rerank+粒度先验拉满(L1 mrr 1.000 是 live 实测),
#   那是 live 模式的守土范围,stub 只挡「BM25 这一级就坏了」的粗回归。
# - hybrid 冒烟门(弱):整条生产管线(向量+BM25+RRF)不崩、分数掉太多也挡。
STUB_BM25_THRESHOLDS = {"hit@5": 0.45, "mrr": 0.28}
STUB_BM25_L1_HIT5 = 0.55
STUB_HYBRID_FLOOR_HIT5 = 0.40

# live 模式硬下限(语义质量地板;软指标看报告 diff):2026-09-07 真机实测
# (hit@5=0.857 / mrr=0.738 / L1 mrr=1.000,28 条)− ~5pt 裕量。
LIVE_THRESHOLDS = {"hit@5": 0.80, "mrr": 0.68, "L1_mrr": 0.92}

# ── memory_recall 侧断言的固定种子(与代码检索共用 stub embedder,确定性)────────
_MEMORY_SEED = [
    ("检索候选池扩满再交给 cross-encoder 重排,RRF 融合 BM25 与向量两路", "retrieval.py"),
    ("记忆向量检索走 sqlite-vec 的 vec0 虚拟表做 ANN,不可用时降级 Python loop cosine", "store.py"),
    ("hash stub eval 让检索管道回归零 key 进 CI,语义质量归 live 模式", "eval.py"),
    ("embedder factory reads config and picks remote or local provider", "embed.py"),
]
_MEMORY_CHECKS = [
    ("候选池怎么扩满 rerank", "检索候选池扩满"),
    ("RRF 融合在哪做", "RRF 融合"),
    ("which path does vector ANN take", "vec0"),
]


def run_l2_stub(eval_set: Path | str | None = None, repo_root: Path | str | None = None,
                base_dir: Path | str | None = None, *, force: bool = True) -> tuple[bool, str]:
    """零 key 确定性回归:临时建 stub 索引 → run_eval → 阈值断言 → 记忆召回断言。

    返回 (passed, report_text)。base_dir 不给 → 系统临时目录(跑完即删,不碰真库);
    给了且 force=False → 增量复用(自检测试第二次跑免重建,秒级)。
    """
    from rootrecall.services.code_index.eval.runner import load_eval_set, run_eval
    from rootrecall.services.code_index.index import build_index
    from rootrecall.services.code_index.store import LanceDBStore

    es_path = Path(eval_set) if eval_set else _EVAL_SET
    root = Path(repo_root) if repo_root else _REPO_ROOT
    if not es_path.exists():
        return False, f"❌ 评测集不存在:{es_path}(stub 模式需要源码树里的 eval/sets/)"
    if not root.exists():
        return False, f"❌ 索引根不存在:{root}"

    tmp_ctx = tempfile.TemporaryDirectory(prefix="rr-eval-stub-") if base_dir is None else None
    base = Path(base_dir) if base_dir else Path(tmp_ctx.__enter__())  # type: ignore[union-attr]
    try:
        emb = HashEmbedder()
        name = "rootrecall-evalstub"
        build_index(root, name, emb, base, force=force)
        store = LanceDBStore(base)
        es = load_eval_set(es_path)

        # oracle 有效性:gold id 不在索引里 = 该 query 被不公平记 0(继承 run_eval.py 的守卫)
        tbl = store._open_or_create(name)
        indexed = {r["id"] for r in tbl.to_arrow().to_pylist()} if tbl is not None else set()
        bad: list[tuple[str, list[str]]] = []
        for q in es:
            miss = [g for g in q.get("gold", []) if g not in indexed]
            if miss:
                bad.append((q["query"][:40], miss))
        if bad:
            lines = [f"❌ oracle 失效:{len(bad)} 条 query 的 gold 不在索引里(新增/改名的符号没同步 eval 集):"]
            lines += [f"   {q} → {m}" for q, m in bad]
            return False, "\n".join(lines)

        rep_bm25 = run_eval(es, name, emb, _BM25OnlyStore(store), reranker=None, top_k=5)
        rep_hybrid = run_eval(es, name, emb, store, reranker=None, top_k=5)

        fail_lines: list[str] = []
        ok_bm25, bm25_fails = _check_thresholds(rep_bm25, STUB_BM25_THRESHOLDS)
        l1 = (rep_bm25.get("by_tier", {}).get("L1") or {}).get("hit@5")
        if l1 is not None and l1 < STUB_BM25_L1_HIT5:
            miss = [r for r in rep_bm25["rows"] if r.get("tier") == "L1" and not r.get("hit@5")]
            bm25_fails.append(f"  ✘ L1 hit@5={round(l1, 2)} < {STUB_BM25_L1_HIT5}")
            bm25_fails += [f"     ✘ L1 miss: {r['query'][:40]}(top1={r['top5'][0][:50] if r['top5'] else '-'})"
                           for r in miss]
        fail_lines += ["[强门·BM25 质量地板]"] + bm25_fails if not ok_bm25 else []
        h_hit = rep_hybrid.get("overall", {}).get("hit@5")
        if h_hit is None or h_hit < STUB_HYBRID_FLOOR_HIT5 or rep_hybrid["n_errors"]:
            ok_hybrid = False
            fail_lines += [f"[弱门·hybrid 管道冒烟] ✘ hit@5={h_hit and round(h_hit, 3)} < {STUB_HYBRID_FLOOR_HIT5}"
                           f" 或 {rep_hybrid['n_errors']} 条异常 —— 生产管线破了(分词/融合/向量路)"]
        else:
            ok_hybrid = True

        ok_mem, mem_lines = _memory_check(base / "mem")
        passed = ok_bm25 and ok_hybrid and ok_mem and not rep_hybrid["n_empty_gold"]
        verdict = "✅ L2 stub 回归通过" if passed else "❌ L2 stub 回归失败"
        detail = "\n".join(filter(None, [
            f"[强门] BM25:{_fmt_overall(rep_bm25)}",
            f"[弱门] hybrid:{_fmt_overall(rep_hybrid)}",
            *fail_lines, *mem_lines,
        ]))
        return passed, f"{verdict}(零 key 管道回归;语义质量跑分用 --mode live)\n{detail}"
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def run_l2_live(eval_set: Path | str | None = None, repo: str = "rootrecall",
                top_k: int = 5, out_root: Path | str | None = None) -> tuple[bool, str]:
    """真 key 跑分:真 embedder + reranker 全指标;报告落 data/eval/ 并与上一份 diff。

    前置:`rootrecall index src/rootrecall rootrecall` 已建真索引(索引根契约同 stub)。
    返回 (passed, report_text);passed 按 LIVE_THRESHOLDS 硬下限判。
    """
    from rootrecall.platform.config import get_app_config
    from rootrecall.services.code_index.embed import create_embedder
    from rootrecall.services.code_index.retrieval import create_reranker
    from rootrecall.services.code_index.store import LanceDBStore
    from rootrecall.services.repos.registry import reanchor_data_path

    from .runner import format_report, load_eval_set, run_eval

    es_path = Path(eval_set) if eval_set else _EVAL_SET
    cfg = get_app_config()
    emb = create_embedder(cfg.code_index.embedding)
    rr = create_reranker(getattr(cfg.code_index, "reranker", None))
    base = reanchor_data_path(getattr(getattr(cfg.code_index, "vector_store", None), "path", "data/code_index"))
    store = LanceDBStore(base)
    if store.count(repo) == 0:
        return False, (f"❌ 索引不存在:{base / repo}(先 `uv run rootrecall index src/rootrecall {repo}`)"
                       f" —— 索引根=src/rootrecall 是 eval gold 路径契约)")

    es = load_eval_set(es_path)
    rep = run_eval(es, repo, emb, store, reranker=rr, top_k=top_k)

    out_dir = Path(out_root) if out_root else reanchor_data_path("data/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{ts}-retrieval.json"
    out.write_text(_dump_report(rep), encoding="utf-8")

    ok, fail_lines = _check_thresholds(rep, LIVE_THRESHOLDS, l1_floor=LIVE_THRESHOLDS["L1_mrr"])
    diff = _diff_previous(out_dir, out, rep)
    verdict = "✅ L2 live 跑分通过" if ok else "❌ L2 live 跑分低于硬下限"
    return ok, "\n".join(filter(None, [f"{verdict}(报告:{out})", format_report(rep), diff, *fail_lines]))


# ── 内部 ────────────────────────────────────────────────────────────────────


class _BM25OnlyStore:
    """把 hybrid_search 降级为纯 BM25 的 duck-type 包装(stub 强门用)。

    run_eval 只碰 store.hybrid_search —— 换成 fts_search 即可让质量地板不受 stub
    向量噪声干扰;生产 hybrid 管道另跑一遍真 store(弱门冒烟)。
    """

    def __init__(self, store):
        self._s = store

    def hybrid_search(self, repo, query_vec, fts_query, limit, where=None):
        return self._s.fts_search(repo, fts_query, limit=limit)


def _fmt_overall(rep: dict) -> str:
    o = rep.get("overall", {})
    by = rep.get("by_tier", {})
    tiers = " · ".join(f"{t}: hit@5={round(m.get('hit@5', 0), 2)} mrr={round(m.get('mrr', 0), 2)}"
                       for t, m in sorted(by.items()))
    return (f"{rep['n_ok']}/{rep['n_total']} ok · hit@5={round(o.get('hit@5', 0), 3)}"
            f" mrr={round(o.get('mrr', 0), 3)} · {tiers}")


def _memory_check(mem_dir: Path) -> tuple[bool, list[str]]:
    """播种固定记忆 + stub 向量,断言 recall 的 BM25/向量路命中预期条目(top-1)。"""
    from rootrecall.services.memory.backends.native.memorize import memorize_items
    from rootrecall.services.memory.backends.native.recall import recall
    from rootrecall.services.memory.backends.native.store import MemoryStore
    from rootrecall.services.memory.schema import Evidence, KnowledgeItem, Scope, SourceTier

    mem_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(str(mem_dir))
    scope = Scope(codebase="rootrecall")
    emb = HashEmbedder()
    items = [KnowledgeItem(kind="codebase_fact", repo="rootrecall", scope=scope, summary=s,
                           evidence=[Evidence(file=f, line=1)], source_tier=SourceTier.inferred)
             for s, f in _MEMORY_SEED]
    memorize_items(items, store=store, embedder=emb)
    lines: list[str] = []
    ok = True
    for query, expect in _MEMORY_CHECKS:
        hits = recall(query, scope, store=store, embedder=emb, reranker=None, top_k=3)
        if hits and expect in hits[0].summary:
            lines.append(f"  memory_recall ✔ '{query}' → top1 命中「{hits[0].summary[:30]}…」")
        else:
            ok = False
            got = hits[0].summary[:30] if hits else "(空)"
            lines.append(f"  memory_recall ✘ '{query}' 期望含「{expect}」,实得「{got}…」")
    store.close()
    return ok, ["memory_recall 侧:"] + lines


def _check_thresholds(rep: dict, thresholds: dict[str, float], *, l1_floor: float | None = None) -> tuple[bool, list[str]]:
    overall = rep.get("overall", {})
    fails: list[str] = []
    for k, floor in thresholds.items():
        if k == "L1_mrr":
            continue
        v = overall.get(k)
        if v is None or v < floor:
            fails.append(f"  ✘ overall {k}={v and round(v, 3)} < 下限 {floor}")
    if l1_floor is not None:
        l1 = (rep.get("by_tier", {}).get("L1") or {}).get("mrr")
        if l1 is None or l1 < l1_floor:
            fails.append(f"  ✘ L1 mrr={(l1 is not None) and round(l1, 3)} < 下限 {l1_floor}")
    if rep.get("n_errors"):
        fails.append(f"  ✘ {rep['n_errors']} 条 query 检索异常(管道破了)")
    if rep.get("n_empty_gold"):
        fails.append(f"  ✘ {rep['n_empty_gold']} 条 query gold 为空")
    return not fails, (["代码检索侧:"] + fails if fails else [])


def _dump_report(rep: dict[str, Any]) -> str:
    import json

    return json.dumps(rep, ensure_ascii=False, indent=1, default=str)


def _diff_previous(out_dir: Path, cur_file: Path, cur: dict) -> str:
    """与 data/eval/ 里上一份 retrieval 报告 diff(overall 指标 ±,涨跌一眼见)。"""
    import json

    prevs = sorted(p for p in out_dir.glob("*-retrieval.json") if p != cur_file)
    if not prevs:
        return "(没有历史报告可 diff —— 这是第一份基线)"
    prev = json.loads(prevs[-1].read_text(encoding="utf-8")).get("overall", {})
    cur_o = cur.get("overall", {})
    parts = []
    for k in ("hit@5", "mrr", "ndcg@5", "recall@5"):
        if k in cur_o:
            d = cur_o[k] - prev.get(k, cur_o[k])
            arrow = "↑" if d > 1e-9 else ("↓" if d < -1e-9 else "=")
            parts.append(f"{k} {round(cur_o[k], 3)}({arrow}{abs(d):+.3f})" if arrow != "=" else f"{k} {round(cur_o[k], 3)}(=)")
    return f"对比上一份({prevs[-1].name}):" + " ".join(parts)
