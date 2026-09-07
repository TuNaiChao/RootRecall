"""代码理解服务 · 第四步:把 chunk + 向量存进向量库,并支持混合检索(P1.3 store.py)。

这一层干什么
------------
上一层 embed.py 把每个 chunk 变成了向量。这层负责把「chunk 元数据 + 向量」一起存进
**向量库**,并提供「混合检索」(BM25 全文 + 向量 ANN)。retrieval.py 在这层之上做
RRF 融合后的 cross-encoder 重排 + boosting + 降级。

选型:LanceDB 嵌入式向量库(详见 docs/p1-code-understanding-design.md §6 / §14)
------------------------------------------------------------------
- **嵌入式**:进程内、零 server,表就是文件目录(`data/code_index/<repo>/lancedb/`),
  随 git/rsync 跨机,符合 RootRecall「两台机零运维」。
- **table-per-repo**:每个代码仓库一张表(独立 LanceDB 目录),物理隔离免费——某仓库可
  单独重建 / rsync / 删除,互不影响(§14.2)。
- **原生 hybrid**:LanceDB 自带 BM25(Tantivy FTS)+ 向量 ANN + RRF 融合,不自搓胶水。

VectorStore 接口(留 Qdrant 扩展性,§14.3)
------------------------------------------
按 RootRecall provider 抽象哲学(模型工厂 / Embedder / Reranker),这里定义 `VectorStore`
Protocol,默认实现 `LanceDBStore`。**P1 不实现 QdrantStore**——只留接口口子;未来升级
触发器到了(常驻服务 / 千万级向量 / 多用户在线,§14.4)再加,config 一行切,上层不改。

LanceDB 0.34 真实 API(P1.3 调研 + live 冒烟核实,见 §6 决策 #8)
----------------------------------------------------------
- 建表用 **pyarrow schema**(生产优于 pydantic LanceModel);向量列 `pa.list_(pa.float32(), dim)`,
  **维度建表时定死**(改维度 = 新建表迁移,由 index.py 的 model_fingerprint 检测触发)。
- 建表后**立即建两个索引**(新 API `create_index(config=...)`,旧的 create_scalar_index/
  create_fts_index 已弃用):① `create_index("id", config=BTree())`——merge_insert 必须,
  否则撞 "unindexed rows > 10000";② `create_index("fts_text", config=FTS(stem=False,
  remove_stop_words=False, with_position=True))`——代码场景关键参数(`stem=False` 否则
  malloc 被 stem;`remove_stop_words=False` 否则 int/void/public/return 被当停用词删掉)。
- upsert 用 `merge_insert("id")` + 条件更新(`when_matched_update_all(where=...)`,
  content_hash 不变跳过)——增量利器。
- bulk 写后 `tbl.optimize()` 把新行折叠进索引,否则新行走 flat scan 慢路径。
- hybrid:`tbl.search(query_type="hybrid").vector(v).text(t).limit(n).rerank(RRFReranker(K=60))`。

对外提供
--------
- VectorStore(Protocol)、LanceDBStore:存储 + hybrid 检索句柄。
- CODE_FIELDS、make_schema(dim):pyarrow schema 定义(index.py / 测试复用)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pyarrow as pa

from rootrecall.services.code_index.chunker import CodeChunk

# ──────────────────────────────────────────────────────────────────────────
# §1 pyarrow schema
# ──────────────────────────────────────────────────────────────────────────

# 非向量列(CodeChunk 的可存储字段)。vector 列按维度动态加(见 make_schema)。
# 类型都是 Lance 原生;fts_text 必须是 string(不能 large_string),否则建不了 FTS 索引。
CODE_FIELDS: list[pa.Field] = [
    pa.field("id", pa.string()),  # 主键(merge_insert 的 join key)
    pa.field("symbol", pa.string()),  # 限定名
    pa.field("kind", pa.string()),  # function | method | class | module
    pa.field("file", pa.string()),  # 相对仓根路径
    pa.field("language", pa.string()),
    pa.field("start_line", pa.int32()),
    pa.field("end_line", pa.int32()),
    pa.field("text", pa.string()),  # 原始代码文本(read_function 直接用)
    pa.field("content_hash", pa.string()),  # text 的 sha256(增量 upsert 判变)
    pa.field("fts_text", pa.string()),  # BM25 词袋(建 FTS 索引)
]


def make_schema(dim: int) -> pa.Schema:
    """按向量维度生成完整 schema(向量列 = fixed_size_list<float32, dim>)。"""
    return pa.schema([*CODE_FIELDS, pa.field("vector", pa.list_(pa.float32(), dim))])


# ──────────────────────────────────────────────────────────────────────────
# §2 VectorStore 接口(留 Qdrant 扩展性)
# ──────────────────────────────────────────────────────────────────────────


class VectorStore(Protocol):
    """向量库接口。LanceDBStore 实现它;未来 QdrantStore 也实现它,上层 retrieval.py 不改。

    hybrid_search 只返回 RRF 融合后的候选(含 _relevance_score);cross-encoder 重排
    在 retrieval.py 做,本接口不绑 reranker provider(保持存储层与重排解耦)。
    """

    def upsert(self, repo: str, chunks: list[CodeChunk], vectors: np.ndarray) -> None:
        """批量 upsert(按 id;content_hash 不变的行跳过)。vectors 形状 (N, dim)。"""
        ...

    def hybrid_search(
        self,
        repo: str,
        query_vec: np.ndarray,
        fts_query: str,
        limit: int,
        where: str | None = None,
    ) -> list[dict[str, Any]]:
        """混合检索(BM25 + 向量 + RRF),返回最多 limit 条候选(字段 dict + _relevance_score)。"""
        ...


# ──────────────────────────────────────────────────────────────────────────
# §3 LanceDBStore:默认实现(table-per-repo)
# ──────────────────────────────────────────────────────────────────────────

# 每个 repo 的单表名。table-per-repo = 每个 repo 一个独立 LanceDB 目录 + 一张此表。
_TABLE = "chunks"


class LanceDBStore:
    """LanceDB 嵌入式向量库,table-per-repo。

    base_dir 下每个 repo 一个子目录(<base_dir>/<repo>/lancedb/),物理隔离。
    一个进程复用一个 LanceDBStore 实例(LanceDB 连接轻量、可并发读;写要串行,见 index.py)。
    """

    def __init__(self, base_dir: Path | str = "data/code_index", *, db_name: str = "lancedb"):
        self._base = Path(base_dir)
        self._db_name = db_name  # 全量重建时传 "lancedb_tmp" 写影子目录,成功后 swap
        self._base.mkdir(parents=True, exist_ok=True)
        self._tables: dict[str, Any] = {}  # repo -> table 句柄缓存

    def _repo_dir(self, repo: str) -> Path:
        return self._base / repo / self._db_name

    def _open_or_create(self, repo: str, dim: int | None = None) -> Any | None:
        """打开 repo 的表;不存在则用 dim 建表 + 建索引。dim=None 且表不存在时返回 None。"""
        import lancedb  # 局部导入:没装 lancedb 的环境也能 import 本模块的纯逻辑(make_schema 等)

        if repo in self._tables:
            return self._tables[repo]
        db = lancedb.connect(str(self._repo_dir(repo)))
        if _TABLE in db.table_names():
            tbl = db.open_table(_TABLE)
        else:
            if dim is None:
                return None  # 表不存在且没给维度:调用方应先 upsert
            tbl = db.create_table(_TABLE, schema=make_schema(dim))
            self._create_indexes(tbl)
        self._tables[repo] = tbl
        return tbl

    @staticmethod
    def _create_indexes(tbl: Any) -> None:
        """建两个必需索引(新 API create_index+config;旧 create_scalar/fts_index 已弃用)。"""
        from lancedb.index import FTS, BTree

        # ① id 的 BTree 标量索引:merge_insert 内部按 id join,没它大表撞 10000 行上限。
        tbl.create_index("id", config=BTree())
        # ② fts_text 的 FTS 索引(Tantivy BM25)。代码场景关键参数:
        #    stem=False(否则 malloc 等 token 被 stem 乱变)、
        #    remove_stop_words=False(否则 int/void/public/return 被当英文停用词删掉!)、
        #    with_position=True(预留 phrase query)。
        tbl.create_index(
            "fts_text",
            config=FTS(with_position=True, stem=False, remove_stop_words=False, ascii_folding=True),
        )

    # —— VectorStore 实现 ——

    def upsert(self, repo: str, chunks: list[CodeChunk], vectors: np.ndarray) -> None:
        """批量 upsert:按 id,只重写 content_hash 变了的行(条件更新,增量利器)。

        vectors 形状 (len(chunks), dim),与 chunks 顺序一一对应。首次写入按维度建表 + 建索引。
        """
        if not chunks:
            return
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] != len(chunks):
            raise ValueError(f"vectors 形状 {arr.shape} 与 chunks 数 {len(chunks)} 不匹配")
        tbl = self._open_or_create(repo, dim=int(arr.shape[1]))
        assert tbl is not None  # dim 已给,_open_or_create 必建表返回非 None;这行给类型检查器收窄类型

        rows = [
            {
                "id": ch.id,
                "symbol": ch.symbol,
                "kind": ch.kind,
                "file": ch.file,
                "language": ch.language,
                "start_line": ch.start_line,
                "end_line": ch.end_line,
                "text": ch.text,
                "content_hash": ch.content_hash,
                "fts_text": ch.fts_text,
                "vector": vec,
            }
            for ch, vec in zip(chunks, arr.tolist(), strict=True)
        ]
        # merge_insert:命中 id → 仅当 content_hash 变了才更新(where= 关键字传 SQL 谓词);
        # 未命中 → 插入。增量重建时没改的 chunk 一行都不重写。
        (tbl.merge_insert("id")
            .when_matched_update_all(where="target.content_hash <> source.content_hash")
            .when_not_matched_insert_all()
            .execute(rows))

    def fts_search(self, repo: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """BM25-only 检索(不走向量路)。表不存在返回 []。

        用途:① eval L2 stub 模式的确定性质量地板(哈希向量是噪声,掺进 RRF 会把
        BM25 的强信号搅浑 —— 质量门用纯 BM25,管道冒烟另走 hybrid);② 零 key
        降级检索的潜在底座(向量路不可用时仍有 BM25,镜像 memory 侧的降级设计)。
        """
        tbl = self._open_or_create(repo)
        if tbl is None:
            return []
        return tbl.search(query, query_type="fts", vector_column_name="vector",
                          fts_columns="fts_text").limit(limit).to_list()

    def optimize(self, repo: str) -> None:
        """把未索引的新行折叠进 FTS/向量索引。bulk upsert 后调一次,否则新行走 flat scan 慢路径。"""
        tbl = self._open_or_create(repo)
        if tbl is not None:
            tbl.optimize()

    def delete_by_file(self, repo: str, files: list[str] | set[str]) -> int:
        """删掉若干文件的全部 chunk 行(按 file 列匹配),返回删前行数。

        增量重建的两处用:① 文件从仓库消失(否则它的 chunk 永远阴魂不散);
        ② 重嵌某文件前先清旧行 —— chunk id 是「file:限定名」,符号改名/挪作用域会换 id,
        只靠 merge_insert 匹配不上旧行,会留重复内容的幽灵行。
        """
        files = [f for f in files if f]
        if not files:
            return 0
        tbl = self._open_or_create(repo)
        if tbl is None:
            return 0
        quoted = ", ".join("'" + f.replace("'", "''") + "'" for f in files)
        before = tbl.count_rows()
        tbl.delete(f"file IN ({quoted})")
        return before

    def hybrid_search(
        self,
        repo: str,
        query_vec: np.ndarray,
        fts_query: str,
        limit: int,
        where: str | None = None,
    ) -> list[dict[str, Any]]:
        """混合检索:BM25(FTS)+ 向量 ANN,RRF(k=60)融合,返回最多 limit 条候选。

        返回每条 = CodeChunk 字段 + _relevance_score(RRF 分,越大越相关)。
        cross-encoder 重排在 retrieval.py 做(本层不绑 reranker provider)。表不存在返回 []。
        """
        from lancedb.rerankers import RRFReranker

        tbl = self._open_or_create(repo)
        if tbl is None:
            return []

        q = (
            tbl.search(query_type="hybrid", vector_column_name="vector", fts_columns="fts_text")
            .vector(np.asarray(query_vec, dtype=np.float32).tolist())
            .text(fts_query)
            .limit(limit)
        )
        if where:
            q = q.where(where, prefilter=True)
        # RRF(K=60,Cormack 2009 标准)融合 BM25 + 向量两路排名;return_score="relevance" 只留 _relevance_score。
        return q.rerank(RRFReranker(K=60, return_score="relevance")).to_list()

    # —— 运维 ——

    def drop_repo(self, repo: str) -> None:
        """删整个 repo 的向量库(目录级删除,物理回收)。"""
        import shutil

        self._tables.pop(repo, None)
        d = self._repo_dir(repo)
        if d.exists():
            shutil.rmtree(d)

    def count(self, repo: str) -> int:
        """返回 repo 表行数(不存在返回 0)。"""
        tbl = self._open_or_create(repo)
        return 0 if tbl is None else tbl.count_rows()