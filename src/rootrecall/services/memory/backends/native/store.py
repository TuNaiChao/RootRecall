"""native 后端 · SQLite 知识项库(R1 backends/native/store.py)。

这是什么
--------
记忆核心的"物理存储"。把 KnowledgeItem(一条知识)存进 SQLite,并提供两路检索:
  - BM25 全文检索(FTS5):按"关键词"找 summary/detail/root_cause。
  - 向量检索(cosine):按"意思"找 —— 需要 KnowledgeItem 带 embedding(memorize 时算)。
两路是 recall.py 多路融合里的"memory 路";recall 还会混 code_index(代码路)+ crg(结构路)。

为什么用 SQLite(不另开 LanceDB)
  - KI ≠ 代码 chunk:需要关系操作(按 scope/kind 过滤、冲突软删 superseded_by IS NULL、
    access_count 累加),SQLite 最合适;LanceDB 留给 code_index 的代码 chunk,不造第三套检索栈。
  - 同类参考实现全是 SQLite:deer-flow deermem(纯 BM25 零向量)、mnemopi beam、code-review-graph graph.db。
  - 向量:存 float32 blob。**渐进式 ANN(建议 A)** —— count(scope)>ann_threshold(默认 500)时
    search_vector 切 sqlite-vec vec0 KNN(C 扩展,partition_key 按 owner+codebase 隔离);否则 Python
    逐行 cosine(benchmark 实测:N<200 loop 更快,N>500 vec0 快 2-4×)。双路径阈值切换,加载失败降级纯 loop。

bi-temporal(借 graphiti):矛盾/失效时设 invalid_at + superseded_by(软删),永不物理删除 ——
能回答"这个 bug 在 X 时点还存不存在"(系统考古关键)。检索默认只看 active 的。

并发:WAL + busy_timeout(借 deermem/crg);写用进程内 Lock 串行,读可并发。

dumb CRUD:本文件只存/取/查;智能(合并/冲突/巩固)在 memorize.py / consolidate.py。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rootrecall.services.memory.backends.native.tokenize import segment as _segment
from rootrecall.services.memory.schema import Evidence, KnowledgeItem, Scope, SourceTier

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

# sqlite-vec 虚拟表名(向量 ANN;建议 A)。延迟建表 —— 首次写带 embedding 的 KI 时按探测维度建。
_VEC_TABLE = "ki_vec"
_DEFAULT_ANN_THRESHOLD = 500  # count(scope) 超此 → search_vector 切 vec0 KNN(benchmark 交叉点 ~200-500)

# 知识项的所有列(单表查询用 _KI_COLS;多表 JOIN 用 _cols("ki") 加别名前缀)。
_KI_FIELD_LIST = [
    "id", "kind", "repo", "owner", "codebase", "summary", "detail", "symptom", "root_cause", "fix_patch",
    "blast_radius_files", "kind_detail", "commit_sha", "evidence", "source", "source_tier", "confidence",
    "access_count", "last_recalled", "valid_at", "invalid_at", "created_at", "related", "tags",
    "superseded_by", "corrected_by", "source_url", "embedding", "updated_at",
]
_KI_COLS = ", ".join(_KI_FIELD_LIST)


def _cols(alias: str) -> str:
    """带表别名前缀的列清单(JOIN 时避免歧义),如 _cols('ki') → 'ki.id, ki.kind, ...'。"""
    return ", ".join(f"{alias}.{c}" for c in _KI_FIELD_LIST)


# ──────────────────────────────────────────────────────────────────────────
# §1 建库 DDL(表 + 索引 + FTS5 external-content + 同步触发器)
# ──────────────────────────────────────────────────────────────────────────

_SCHEMA = """
-- 知识项主表。rowid 是 SQLite 隐式整数主键(FTS5 用它做 content_rowid 映射)。
CREATE TABLE IF NOT EXISTS knowledge_items (
    id                 TEXT PRIMARY KEY,   -- 稳定 id(sha256(scope+kind+content_key)[:16])
    kind               TEXT NOT NULL,      -- codebase_fact | bug_lesson | mental_model
    repo               TEXT NOT NULL,
    owner              TEXT NOT NULL,      -- 租户(scope 一半)
    codebase           TEXT NOT NULL,      -- 租户(scope 一半)
    summary            TEXT NOT NULL,      -- 人读摘要(检索+注入核心)
    detail             TEXT NOT NULL DEFAULT '',
    symptom            TEXT NOT NULL DEFAULT '',   -- bug_lesson
    root_cause         TEXT NOT NULL DEFAULT '',   -- bug_lesson
    fix_patch          TEXT NOT NULL DEFAULT '',   -- bug_lesson
    blast_radius_files TEXT NOT NULL DEFAULT '[]',  -- JSON
    kind_detail        TEXT NOT NULL DEFAULT 'module',  -- codebase_fact
    commit_sha         TEXT,
    evidence           TEXT NOT NULL DEFAULT '[]',  -- JSON [Evidence]
    source             TEXT NOT NULL DEFAULT '',
    source_tier        TEXT NOT NULL DEFAULT 'unknown',  -- SourceTier.value
    confidence         REAL NOT NULL DEFAULT 0,
    access_count       INTEGER NOT NULL DEFAULT 0,  -- 被召回次数(升级 mental_model 依据)
    last_recalled      TEXT,
    valid_at           TEXT NOT NULL,      -- bi-temporal:在真起点
    invalid_at         TEXT,               -- bi-temporal:失效点(NULL=仍有效)
    created_at         TEXT NOT NULL,
    related            TEXT NOT NULL DEFAULT '[]',  -- JSON [ki_id]
    tags               TEXT NOT NULL DEFAULT '[]',  -- JSON [str]
    superseded_by      TEXT,               -- 被哪条取代(NULL=当前版本)
    corrected_by       TEXT,               -- 被哪条纠正(NULL=未被纠正;不影响 active,检索降权用)
    source_url         TEXT,               -- 外部溯源 URL(domain_knowledge 网调知识锚主源;bug/codebase 通常 NULL)
    embedding          BLOB,               -- float32 向量(NULL=没算)
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ki_scope  ON knowledge_items(owner, codebase);
CREATE INDEX IF NOT EXISTS idx_ki_active ON knowledge_items(codebase, invalid_at, superseded_by);
CREATE INDEX IF NOT EXISTS idx_ki_kind   ON knowledge_items(kind);

-- FTS5 全文索引(standalone:文本自存,upsert() 在 Python 里同步 —— tokenize.py 的 CJK 分词需要)。
-- 为什么不再是 external-content + 触发器:触发器在 SQL 层,调不了 Python 分词;standalone 表由
-- upsert() 同事务 delete-then-insert 维护(文本列只有 upsert 一处写,不会漏同步)。
-- 存的是分词后的文本(中文段 jieba 切开空格连回)→ unicode61 按空格切 = 一词一 token。
-- 老库(external-content)在 __init__ migration 里检测并重建,见 _migrate_fts_standalone。
CREATE VIRTUAL TABLE IF NOT EXISTS ki_fts USING fts5(
    summary, detail, root_cause,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS ki_meta(key TEXT PRIMARY KEY, value TEXT);
"""


# ──────────────────────────────────────────────────────────────────────────
# §2 行 ↔ KnowledgeItem 序列化
# ──────────────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _vec_to_blob(vec: list[float] | None) -> bytes | None:
    """向量 → float32 bytes(numpy)。None/空 → None(没算向量的 KI)。"""
    if not vec:
        return None
    import numpy as np

    return np.asarray(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: Any) -> list[float] | None:
    """float32 bytes → list[float]。None → None。"""
    if blob is None:
        return None
    import numpy as np

    return np.frombuffer(bytes(blob), dtype=np.float32).tolist()


def _ki_to_row(ki: KnowledgeItem) -> dict[str, Any]:
    """KnowledgeItem → 可绑定到 SQL 的 dict(list/dict 字段 JSON 编码,时间 ISO 编码)。"""
    return {
        "id": ki.id,
        "kind": ki.kind,
        "repo": ki.repo,
        "owner": ki.scope.owner,
        "codebase": ki.scope.codebase,
        "summary": ki.summary,
        "detail": ki.detail,
        "symptom": ki.symptom,
        "root_cause": ki.root_cause,
        "fix_patch": ki.fix_patch,
        "blast_radius_files": json.dumps(ki.blast_radius_files, ensure_ascii=False),
        "kind_detail": ki.kind_detail,
        "commit_sha": ki.commit_sha,
        "evidence": json.dumps([e.model_dump() for e in ki.evidence], ensure_ascii=False),
        "source": ki.source,
        "source_tier": ki.source_tier.value,
        "confidence": ki.confidence,
        "access_count": ki.access_count,
        "last_recalled": ki.last_recalled.isoformat() if ki.last_recalled else None,
        "valid_at": ki.valid_at.isoformat(),
        "invalid_at": ki.invalid_at.isoformat() if ki.invalid_at else None,
        "created_at": ki.created_at.isoformat(),
        "related": json.dumps(ki.related, ensure_ascii=False),
        "tags": json.dumps(ki.tags, ensure_ascii=False),
        "superseded_by": ki.superseded_by,
        "corrected_by": ki.corrected_by,
        "source_url": ki.source_url,
        "embedding": _vec_to_blob(ki.embedding),
        "updated_at": _utcnow_iso(),
    }


def _row_to_ki(row: sqlite3.Row | dict[str, Any]) -> KnowledgeItem:
    """SQL 行 → KnowledgeItem。row 用 [key] 取值(sqlite3.Row 与 dict 都支持)。"""
    g = row.__getitem__
    last = g("last_recalled")
    return KnowledgeItem(
        id=g("id"),
        kind=g("kind"),
        repo=g("repo"),
        scope=Scope(owner=g("owner"), codebase=g("codebase")),
        summary=g("summary"),
        detail=g("detail") or "",
        symptom=g("symptom") or "",
        root_cause=g("root_cause") or "",
        fix_patch=g("fix_patch") or "",
        blast_radius_files=json.loads(g("blast_radius_files") or "[]"),
        kind_detail=g("kind_detail") or "module",
        commit_sha=g("commit_sha"),
        evidence=[Evidence(**e) for e in json.loads(g("evidence") or "[]")],
        source=g("source") or "",
        source_tier=SourceTier(g("source_tier") or "unknown"),
        confidence=float(g("confidence") or 0.0),
        access_count=int(g("access_count") or 0),
        last_recalled=datetime.fromisoformat(last) if last else None,
        valid_at=datetime.fromisoformat(g("valid_at")),
        invalid_at=datetime.fromisoformat(g("invalid_at")) if g("invalid_at") else None,
        created_at=datetime.fromisoformat(g("created_at")),
        related=json.loads(g("related") or "[]"),
        tags=json.loads(g("tags") or "[]"),
        superseded_by=g("superseded_by"),
        corrected_by=g("corrected_by"),
        source_url=g("source_url"),
        embedding=_blob_to_vec(g("embedding")),
    )


def _fts_query(query: str) -> str:
    """查询 → 分词 → FTS5「OR-of-terms」(每个 term 引号转义,避 *, : 被当语法)。

    为什么用 OR 不用短语/AND:BM25 在多路召回里当"撒大网"角色(宽召回,rerank/向量再精筛)。
    OR 不会因为某个 term(尤其 CJK)不匹配而整体返空 —— 漏一个 term 也能把命中的召回来。

    CJK(Phase 3):查询先过 _segment(中文段 jieba 切开)再 split —— 索引侧(upsert 入
    standalone FTS)存的就是分词后的文本,两侧同一分词器,"扫描"就能命中"阻塞所有站点扫描"。
    jieba 没装 → _segment 原样返回,退回 unicode61 行为(英文/混合查询不受影响)。
    """
    terms = [t for t in _segment(query or "").split() if t]
    if not terms:
        return ""

    def _q(t: str) -> str:
        return '"' + t.replace('"', '""') + '"'  # 引号转义:字面量 term,不当 FTS 语法

    return " OR ".join(_q(t) for t in terms)


def _scope_filter(scope: Scope, repo: str | None = None, *, alias: str | None = None) -> tuple[list[str], list[Any]]:
    """生成 scope 过滤子句(owner+codebase[+repo]),alias 给多表 JOIN 加前缀。"""
    p = f"{alias}." if alias else ""
    clauses = [f"{p}owner=?", f"{p}codebase=?"]
    params: list[Any] = [scope.owner, scope.codebase]
    if repo:
        clauses.append(f"{p}repo=?")
        params.append(repo)
    return clauses, params


# ──────────────────────────────────────────────────────────────────────────
# §3 MemoryStore:存储 + 两路检索
# ──────────────────────────────────────────────────────────────────────────


class MemoryStore:
    """SQLite 知识项库(canonical 数据 + FTS5 + 向量 blob)。

    线程:单连接 check_same_thread=False + 写锁;LangGraph 多线程读安全、写串行。
    """

    _UPSERT = f"""
    INSERT INTO knowledge_items ({_KI_COLS})
    VALUES (@id,@kind,@repo,@owner,@codebase,@summary,@detail,@symptom,@root_cause,@fix_patch,
            @blast_radius_files,@kind_detail,@commit_sha,@evidence,@source,@source_tier,@confidence,
            @access_count,@last_recalled,@valid_at,@invalid_at,@created_at,@related,@tags,
            @superseded_by,@corrected_by,@source_url,@embedding,@updated_at)
    ON CONFLICT(id) DO UPDATE SET
        kind=excluded.kind, repo=excluded.repo, summary=excluded.summary, detail=excluded.detail,
        symptom=excluded.symptom, root_cause=excluded.root_cause, fix_patch=excluded.fix_patch,
        blast_radius_files=excluded.blast_radius_files, kind_detail=excluded.kind_detail,
        commit_sha=excluded.commit_sha, evidence=excluded.evidence, source=excluded.source,
        source_tier=excluded.source_tier, confidence=excluded.confidence,
        access_count=excluded.access_count, last_recalled=excluded.last_recalled,
        valid_at=excluded.valid_at, invalid_at=excluded.invalid_at,
        related=excluded.related, tags=excluded.tags, superseded_by=excluded.superseded_by,
        corrected_by=excluded.corrected_by, source_url=excluded.source_url,
        embedding=excluded.embedding, updated_at=excluded.updated_at
    """  # created_at/owner/codebase 不在 SET —— upsert 保持原创建时间与租户身份

    def __init__(
        self,
        store_path: str | Path,
        *,
        db_name: str = "memory.db",
        auto_index: bool = True,
        ann_threshold: int = _DEFAULT_ANN_THRESHOLD,
    ):
        self._path = Path(store_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._db_file = self._path / db_name
        self._wl = threading.Lock()  # 写串行(WAL 下读可并发)
        # isolation_level=None → 自动提交模式,事务由我们显式 BEGIN/COMMIT 控制(借 crg)。
        self._conn = sqlite3.connect(str(self._db_file), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute("INSERT OR IGNORE INTO ki_meta(key,value) VALUES ('schema_version', ?)", (str(_SCHEMA_VERSION),))
        # ── 轻量迁移:给已建的老库补 corrected_by 列(2026-08-13 纠正链)──
        # CREATE TABLE IF NOT EXISTS 不改已建表结构 → 手动 ALTER 补列。幂等:列在就不加(PRAGMA table_info 查)。
        existing_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(knowledge_items)")}
        if "corrected_by" not in existing_cols:
            self._conn.execute("ALTER TABLE knowledge_items ADD COLUMN corrected_by TEXT")
        # ── 给老库补 source_url 列(2026-08-13 domain_knowledge 领域知识溯源)──
        if "source_url" not in existing_cols:
            self._conn.execute("ALTER TABLE knowledge_items ADD COLUMN source_url TEXT")

        # ── FTS standalone 化 migration(Phase 3 CJK 分词):老库的 external-content FTS
        #    (SQL 触发器同步)重建为 standalone(Python 侧 upsert 同步 + jieba 分词)。
        #    幂等:ki_meta 里 'fts_standalone' 标记位在就跳过;失败不崩(FTS 重建是增强,记忆数据在主表)。
        self._migrate_fts_standalone()

        # ── sqlite-vec 加载(建议 A:向量 ANN 加速)──
        # 绝不崩:加载/建表失败 → _vec_ok=False → search_vector 走纯 loop(记忆是核心,向量加速是优化)。
        self._auto_index = auto_index
        self._ann_threshold = ann_threshold
        self._vec_ok = False            # sqlite-vec 是否加载成功
        self._vec_dim: int | None = None  # vec0 表维度(None=还没建表);建表后记进 ki_meta('vec_dim')
        if auto_index:
            try:
                import sqlite_vec

                self._conn.enable_load_extension(True)
                self._conn.load_extension(sqlite_vec.loadable_path())
                self._vec_ok = True
                # 冷启动恢复:若库已有 vec0 表(之前建过),从 ki_meta 读回 dim
                stored = self._conn.execute("SELECT value FROM ki_meta WHERE key='vec_dim'").fetchone()
                self._vec_dim = int(stored["value"]) if stored else None
            except Exception as e:  # noqa: BLE001 - sqlite-vec 没装/加载失败不阻断 memory(只少 ANN 加速)
                logger.warning("memory store: sqlite-vec 加载失败,降级纯 loop 向量检索: %s", e)
                self._vec_ok = False

    # —— 写 ——

    def _migrate_fts_standalone(self) -> None:
        """把老库的 external-content FTS(触发器同步)重建为 standalone(Python 同步 + 分词)。

        检测:sqlite_master 里 ki_fts 的建表 SQL 含 'content=' → 是老结构。
        重建:DROP 触发器 + FTS 表 → 按新 _SCHEMA 重建 → 全量重灌(主表文本过 _segment)。
        幂等:ki_meta 'fts_standalone'=1 标记(新建库 _SCHEMA 已是 standalone,建表后直接打标)。
        失败不崩:FTS 是检索增强,记忆数据全在主表;重建失败 → 记 warning,BM25 返空,
        向量路照常(优雅降级,镜像 sqlite-vec 加载失败的处理)。
        """
        marked = self._conn.execute("SELECT value FROM ki_meta WHERE key='fts_standalone'").fetchone()
        if marked:
            return
        try:
            ddl = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='ki_fts'"
            ).fetchone()
            is_old = ddl is not None and "content=" in (ddl["sql"] or "")
            if is_old:
                with self._wl:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._conn.execute("DROP TRIGGER IF EXISTS ki_fts_ai")
                        self._conn.execute("DROP TRIGGER IF EXISTS ki_fts_ad")
                        self._conn.execute("DROP TRIGGER IF EXISTS ki_fts_au")
                        self._conn.execute("DROP TABLE ki_fts")
                        # 重建 standalone ki_fts。不用 executescript(它会隐式 COMMIT 打断外层
                        # BEGIN IMMEDIATE,把 DROP 和重建拆成两个自提交事务)—— 单条 CREATE 等价。
                        self._conn.execute(
                            "CREATE VIRTUAL TABLE IF NOT EXISTS ki_fts USING fts5("
                            "summary, detail, root_cause, tokenize='unicode61 remove_diacritics 2')"
                        )
                        self._conn.execute("COMMIT")
                    except BaseException:
                        self._conn.execute("ROLLBACK")
                        raise
            # 全量重灌:主表每行文本 → 分词 → 插 standalone FTS(rowid 对齐主表)。
            with self._wl:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for r in self._conn.execute(
                        "SELECT rowid, summary, detail, root_cause FROM knowledge_items"
                    ).fetchall():
                        self._conn.execute(
                            "INSERT INTO ki_fts(rowid, summary, detail, root_cause) VALUES (?,?,?,?)",
                            (r["rowid"], _segment(r["summary"] or ""), _segment(r["detail"] or ""),
                             _segment(r["root_cause"] or "")),
                        )
                    self._conn.execute(
                        "INSERT OR REPLACE INTO ki_meta(key,value) VALUES ('fts_standalone','1')"
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
            if is_old:
                logger.info("memory store: FTS 已迁移 standalone + CJK 分词重灌")
        except Exception as e:  # noqa: BLE001 - FTS 重建失败不崩记忆(主表数据完好,BM25 降级)
            logger.warning("memory store: FTS standalone 迁移失败,BM25 可能返空: %s", e)

    def _fts_sync(self, rows: list[dict[str, Any]]) -> None:
        """把 upsert 的行同步进 standalone FTS(与主表同事务):文本先分词再入索引。

        delete-then-insert:rowid 按 id 回查主表(ON CONFLICT 原地更新 rowid 稳定,
        executemany 不返回 rowid → 回查,镜像 _vec_upsert 的映射姿势)。
        """
        if not rows:
            return
        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        rid_by_id = {
            r["id"]: r["rowid"]
            for r in self._conn.execute(
                f"SELECT id, rowid FROM knowledge_items WHERE id IN ({ph})", ids
            ).fetchall()
        }
        for r in rows:
            rid = rid_by_id.get(r["id"])
            if rid is None:
                continue
            self._conn.execute("DELETE FROM ki_fts WHERE rowid=?", (rid,))
            self._conn.execute(
                "INSERT INTO ki_fts(rowid, summary, detail, root_cause) VALUES (?,?,?,?)",
                (rid, _segment(r["summary"] or ""), _segment(r["detail"] or ""),
                 _segment(r["root_cause"] or "")),
            )

    def upsert(self, items: list[KnowledgeItem]) -> int:
        """批量 upsert(按 id;存在则更新除 created_at/owner/codebase 外字段)。返回条数。

        ON CONFLICT 原地更新 → rowid 稳定 → FTS rowid 映射不乱。整批一个 BEGIN IMMEDIATE 事务。
        带 embedding 的行同事务双写 vec0 表(建议 A);全部行同事务同步 standalone FTS(Phase 3 CJK)。
        """
        if not items:
            return 0
        rows = [_ki_to_row(it) for it in items]
        with self._wl:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(self._UPSERT, rows)
                self._vec_upsert(rows)  # 同事务双写 vec0(失败不阻断主表,降级 loop)
                self._fts_sync(rows)    # 同事务同步 standalone FTS(分词后文本)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    # —— sqlite-vec ANN(建议 A)——

    def _ensure_vec_table(self, dim: int) -> bool:
        """首次写向量时按探测维度建 vec0 表(partition_key=owner+codebase)。

        返 True=表就绪(本次或之前建好,且维度一致);False=不可用(未加载/维度冲突/建表失败)→ 降级 loop。
        镜像 code_index store.py:_open_or_create(repo, dim) 的"探测维度再建表"模式。
        维度冲突(配置换 model 改 dim)→ 不重建(重建=运维 reindex),返 False 降级 loop。
        """
        if not self._vec_ok:
            return False
        if self._vec_dim is None:
            try:
                # distance_metric=cosine:distance = 1 - cosine_sim(实测转换误差<1e-7)。
                # 不用默认 L2(L2 distance ≠ cosine,需归一化双源,复杂)。cosine metric 直接对齐 loop 语义。
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE} USING vec0("
                    f"embedding float[{dim}] distance_metric=cosine, owner TEXT, codebase TEXT)"
                )
                self._conn.execute("INSERT OR REPLACE INTO ki_meta(key,value) VALUES ('vec_dim', ?)", (str(dim),))
                self._vec_dim = dim
            except Exception as e:  # noqa: BLE001 - 建表失败降级,不崩
                logger.warning("memory store: vec0 建表失败,降级 loop: %s", e)
                self._vec_ok = False
                return False
        return self._vec_dim == dim

    def _vec_upsert(self, rows: list[dict[str, Any]]) -> None:
        """把带 embedding 的行同步进 vec0 表(与主表同事务)。

        vec0 虚拟表无 ON CONFLICT → delete-then-insert 幂等(按 rowid)。
        rowid 稳定(ON CONFLICT 原地更新不挪),但 executemany 不返回 rowid → 按 id 回查映射。
        失败静默降级(只写主表)—— 记忆是核心,vec0 是加速层,绝不阻断 upsert。
        """
        if not self._auto_index or not self._vec_ok:
            return
        import numpy as np

        # 探测维度建表(用第一条带非零 embedding 的行);全无 embedding → 跳过
        dim: int | None = None
        for r in rows:
            if r["embedding"]:
                dim = np.frombuffer(r["embedding"], dtype=np.float32).shape[0]
                break
        if dim is None:
            return  # 这批没向量 → 不碰 vec0
        if not self._ensure_vec_table(dim):
            return  # 表不可用(维度冲突/建表失败)→ 降级,主表已写

        try:
            # 收集要写 vec0 的行:有 embedding 且非全零(cosine metric 下全零向量 distance 未定义,会崩;
            # 零向量仍进主表 embedding BLOB,loop 路 cosine 自然算出 sim≈0 排末尾,不崩)。
            def _is_zero(b: bytes) -> bool:
                return not np.frombuffer(b, dtype=np.float32).any()

            to_write = [r for r in rows if r["embedding"] and not _is_zero(r["embedding"])]
            if not to_write:
                return
            # 按 id 回查 rowid(executemany ON CONFLICT 不返回 rowid;rowid 稳定)
            ids = [r["id"] for r in to_write]
            ph = ",".join("?" * len(ids))
            rowid_map = dict(self._conn.execute(
                f"SELECT id, rowid FROM knowledge_items WHERE id IN ({ph})", ids
            ).fetchall())
            vec_rows = [
                (rowid_map[r["id"]], r["embedding"], r["owner"], r["codebase"])
                for r in to_write if r["id"] in rowid_map
            ]
            if not vec_rows:
                return
            # delete-then-insert(vec0 无 ON CONFLICT;executemany 对 vec0 实测可用)
            self._conn.executemany(f"DELETE FROM {_VEC_TABLE} WHERE rowid=?", [(vr[0],) for vr in vec_rows])
            self._conn.executemany(
                f"INSERT INTO {_VEC_TABLE}(rowid, embedding, owner, codebase) VALUES (?,?,?,?)",
                vec_rows,
            )
        except Exception as e:  # noqa: BLE001 - vec0 写失败降级,主表事务继续 COMMIT
            logger.warning("memory store: vec0 双写失败,降级(下次 search 走 loop): %s", e)

    def _resolve_id(self, item_id: str) -> str | None:
        """把 agent 传来的 id 解析成 DB 里的完整 16 位 id。

        背景:dump/recall 的溯源卡渲染 id 截断成 8 位(防刷屏),agent 据此调
        mark_corrected/corrects 或 memory invalidate 时传的是 8 位前缀;DB 里却是 16 位。
        精确匹配失败 → 先按前缀解析:恰好 1 条前缀命中 → 用它的完整 id;0 条或 >1 条(歧义)→ 返回 None。
        内部调用方(bump_access/consolidate)传的是完整 id,精确匹配直接中,不走前缀。
        """
        if not item_id:
            return None
        # 快路:精确匹配(DB id 是 16 位;内部调用方传完整 id 直接中)。
        exists = self._conn.execute("SELECT 1 FROM knowledge_items WHERE id=? LIMIT 1", (item_id,)).fetchone()
        if exists:
            return item_id
        # 慢路:前缀匹配(agent 传 dump 里看到的 8 位 id)。前缀歧义(>1 条)→ 拒绝,宁漏不错。
        rows = self._conn.execute(
            "SELECT id FROM knowledge_items WHERE id LIKE ? || '%' LIMIT 2", (item_id,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"] if isinstance(rows[0], dict) else rows[0][0]
        return None

    def bump_access(self, item_id: str) -> None:
        """被召回命中:access_count+1, last_recalled=now(升级 mental_model 的依据)。

        只动这两列 → 不触发 FTS 重排(AFTER UPDATE OF summary,detail,root_cause)。
        """
        with self._wl:
            self._conn.execute(
                "UPDATE knowledge_items SET access_count=access_count+1, last_recalled=?, updated_at=? WHERE id=?",
                (_utcnow_iso(), _utcnow_iso(), item_id),
            )

    def set_invalid(self, item_id: str, *, superseded_by: str | None = None, invalid_at: datetime | None = None) -> bool:
        """bi-temporal 软删:设 invalid_at(+可选 superseded_by)。返回是否真的改了。

        item_id 接受完整 id 或 dump 里看到的 8 位前缀(经 _resolve_id 解析);前缀歧义(>1 条)→ 不改。
        """
        ts = (invalid_at or datetime.now(UTC)).isoformat()
        with self._wl:
            full = self._resolve_id(item_id)
            if full is None:
                return False
            cur = self._conn.execute(
                "UPDATE knowledge_items SET invalid_at=?, superseded_by=COALESCE(?, superseded_by), updated_at=? "
                "WHERE id=? AND invalid_at IS NULL",
                (ts, superseded_by, _utcnow_iso(), full),
            )
        return cur.rowcount > 0

    def mark_corrected(self, item_id: str, *, corrected_by: str) -> bool:
        """标记一条 KI 被另一条纠正(corrected_by = 纠正者 id)。

        与 set_invalid 的区别:不设 invalid_at(条目仍 active,仍可检索/体检可见),
        只标纠正关系让检索侧降权(recall 的 _apply_decay_confidence 对 corrected_by 非空的条目额外降权)。
        幂等:已设过 corrected_by 的条目不再覆盖(WHERE corrected_by IS NULL)。

        item_id 接受完整 id 或 dump 里看到的 8 位前缀(经 _resolve_id 解析);前缀歧义(>1 条)→ 不改。
        """
        with self._wl:
            full = self._resolve_id(item_id)
            if full is None:
                return False
            cur = self._conn.execute(
                "UPDATE knowledge_items SET corrected_by=?, updated_at=? WHERE id=? AND corrected_by IS NULL",
                (corrected_by, _utcnow_iso(), full),
            )
        return cur.rowcount > 0

    def set_kind(self, item_id: str, kind: str) -> bool:
        """改 kind(consolidate 升级 mental_model 用)。"""
        with self._wl:
            cur = self._conn.execute("UPDATE knowledge_items SET kind=?, updated_at=? WHERE id=?", (kind, _utcnow_iso(), item_id))
        return cur.rowcount > 0

    def set_confidence(self, item_id: str, confidence: float) -> None:
        with self._wl:
            self._conn.execute("UPDATE knowledge_items SET confidence=?, updated_at=? WHERE id=?", (confidence, _utcnow_iso(), item_id))

    def set_tags(self, item_id: str, tags: list[str]) -> None:
        """覆写 tags 列(consolidate 打 needs_review 标签用;JSON 序列化)。

        调用方负责去重(传进来前用集合合并已有标签 + 新标签),这里只做持久化。
        """
        with self._wl:
            self._conn.execute(
                "UPDATE knowledge_items SET tags=?, updated_at=? WHERE id=?",
                (json.dumps(tags, ensure_ascii=False), _utcnow_iso(), item_id),
            )

    # —— 读 ——

    def get(self, item_id: str) -> KnowledgeItem | None:
        """按 id 取一条(含已失效的)。"""
        row = self._conn.execute(f"SELECT {_KI_COLS} FROM knowledge_items WHERE id=?", (item_id,)).fetchone()
        return _row_to_ki(row) if row else None

    def list_items(self, scope: Scope, *, repo: str | None = None, kind: str | None = None, include_invalid: bool = False) -> list[KnowledgeItem]:
        """列某 scope 的知识项(可按 repo/kind 过滤;默认只看 active)。"""
        clauses, params = _scope_filter(scope, repo)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if not include_invalid:
            clauses.append("invalid_at IS NULL AND superseded_by IS NULL")
        sql = f"SELECT {_KI_COLS} FROM knowledge_items WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC"
        return [_row_to_ki(r) for r in self._conn.execute(sql, params).fetchall()]

    def count(self, scope: Scope, *, include_invalid: bool = False) -> int:
        clauses, params = _scope_filter(scope)
        if not include_invalid:
            clauses.append("invalid_at IS NULL AND superseded_by IS NULL")
        return int(self._conn.execute(f"SELECT COUNT(*) FROM knowledge_items WHERE {' AND '.join(clauses)}", params).fetchone()[0])

    def list_scopes(self) -> list[tuple[str, int]]:
        """非空作用域清单 [(codebase, 条数)] 条数降序 —— recall 空池提示用(2026-08-26 实测:
        agent 没传 codebase 探默认空池连试两轮;列出非空池让它一次改对)。"""
        rows = self._conn.execute(
            "SELECT codebase, COUNT(*) AS c FROM knowledge_items GROUP BY codebase ORDER BY c DESC"
        ).fetchall()
        return [(r["codebase"], int(r["c"])) for r in rows]

    def search_bm25(self, query: str, scope: Scope, *, repo: str | None = None, limit: int = 20) -> list[tuple[KnowledgeItem, float]]:
        """BM25 全文检索(FTS5):返回 [(item, score)],score 越大越相关(bm25 取负归一)。

        无词/查询异常 → 返 [](不抛;recall 走其他路)。
        """
        fq = _fts_query(query)
        if not fq:
            return []
        clauses, params = _scope_filter(scope, repo, alias="ki")
        sql = (
            f"SELECT {_cols('ki')}, -bm25(ki_fts) AS score "
            "FROM ki_fts JOIN knowledge_items ki ON ki.rowid = ki_fts.rowid "
            f"WHERE ki_fts MATCH ? AND {' AND '.join(clauses)} "
            "AND ki.invalid_at IS NULL "
            # R3.5+(2026-08-06):不再过滤 superseded_by —— 旧版本重见天日作参考,靠 recall decay 排"最新为主"。
            # 仅手动 invalidate(invalid_at 非空,错 fact)仍隐藏。list/count(管理视图)仍 active-only。
            "ORDER BY score DESC LIMIT ?"
        )
        try:
            rows = self._conn.execute(sql, [fq, *params, limit]).fetchall()
        except sqlite3.OperationalError:
            return []  # FTS 查询语法异常(极端输入)→ 不崩 recall
        return [(_row_to_ki(r), float(r["score"])) for r in rows]

    def search_vector(self, query_vec: Any, scope: Scope, *, repo: str | None = None, limit: int = 20) -> list[tuple[KnowledgeItem, float]]:
        """向量检索(cosine):返回 [(item, cosine)],越大越相关。

        渐进式 ANN(建议 A):count(scope)>ann_threshold 且 vec0 可用 → sqlite-vec KNN(C 扩展,
        partition_key 按 owner+codebase 隔离);否则 Python 逐行 cosine(benchmark:N<200 loop 更快)。
        vec0 查询异常/维度不符 → 自动降级 loop。契约不变,recall.py 无感。
        """
        if query_vec is None:
            return []
        # 分流:超阈值 + vec0 可用 → KNN;否则现状 loop
        if self._should_use_ann(scope):
            hits = self._search_vec0(query_vec, scope, repo=repo, limit=limit)
            if hits is not None:  # None=KNN 异常应降级
                return hits
        return self._search_vec_loop(query_vec, scope, repo=repo, limit=limit)

    def _should_use_ann(self, scope: Scope) -> bool:
        """是否走 vec0 KNN:auto_index 开 + vec0 加载成功 + 表已建 + count(scope)>阈值。"""
        return (
            self._auto_index
            and self._vec_ok
            and self._vec_dim is not None
            and self.count(scope) > self._ann_threshold
        )

    def _search_vec_loop(self, query_vec: Any, scope: Scope, *, repo: str | None, limit: int) -> list[tuple[KnowledgeItem, float]]:
        """纯 Python 逐行 cosine(小规模快路径 + vec0 降级兜底)。

        scope 内所有带向量的 active 项 load 出来算 cosine(O(N);几百条无感)。维度不匹配的项跳过。
        """
        import numpy as np

        clauses, params = _scope_filter(scope, repo)
        # R3.5+(2026-08-06):不再过滤 superseded_by(旧版本可召回作参考);只排除手动 invalidate。
        clauses += ["embedding IS NOT NULL", "invalid_at IS NULL"]
        rows = self._conn.execute(f"SELECT {_KI_COLS} FROM knowledge_items WHERE {' AND '.join(clauses)}", params).fetchall()
        if not rows:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        qn = float(np.linalg.norm(q)) + 1e-12
        scored: list[tuple[KnowledgeItem, float]] = []
        for r in rows:
            v = np.frombuffer(bytes(r["embedding"]), dtype=np.float32)
            if v.shape[0] != q.shape[0]:
                continue  # 维度不匹配 → 跳过
            sim = float(np.dot(q, v) / (qn * (float(np.linalg.norm(v)) + 1e-12)))
            scored.append((_row_to_ki(r), sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def _search_vec0(
        self, query_vec: Any, scope: Scope, *, repo: str | None, limit: int
    ) -> list[tuple[KnowledgeItem, float]] | None:
        """vec0 KNN + 回主表取 KI + active/repo 过滤。返 None=异常(应降级 loop)。

        distance(cosine distance,越小越近)→ sim = 1 - distance(cosine similarity,越大越近)。
        partition_key=owner+codebase 硬隔离 KNN;active(invalid_at)与 repo 只能 KNN 后过滤
        (vec0 不支持非 partition_key 的 WHERE)→ over_fetch=limit×4 补漏名额(对齐 recall.py cand)。
        """
        import numpy as np

        try:
            q = np.asarray(query_vec, dtype=np.float32)
            if q.shape[0] != self._vec_dim:
                return None  # 查询维度与表维度不符 → 降级 loop(loop 逐行校验跳过)
            if not q.any():
                return None  # 全零查询向量:cosine 未定义 → 降级 loop(loop 算出 sim≈0 自然返低分)
            # KNN:多取 4× 喂后面的 active/repo 过滤(repo 过滤更易漏,故 repo 时才放大)
            over_fetch = limit * 4 if repo else limit * 2
            knn = self._conn.execute(
                f"SELECT rowid, distance FROM {_VEC_TABLE} "
                "WHERE embedding MATCH ? AND k = ? AND owner = ? AND codebase = ? ORDER BY distance",
                [q.tobytes(), over_fetch, scope.owner, scope.codebase],
            ).fetchall()
            if not knn:
                return []
            rowids = [r["rowid"] for r in knn]
            dist_map = {r["rowid"]: float(r["distance"]) for r in knn}
            # 回主表取 KI + active 过滤 + repo 过滤(vec0 不支持这些 WHERE)
            ph = ",".join("?" * len(rowids))
            clauses = [f"rowid IN ({ph})", "invalid_at IS NULL"]
            params: list[Any] = list(rowids)
            if repo:
                clauses.append("repo = ?")
                params.append(repo)
            main_rows = self._conn.execute(
                f"SELECT rowid, {_KI_COLS} FROM knowledge_items WHERE {' AND '.join(clauses)}", params
            ).fetchall()
            scored = [(_row_to_ki(mr), 1.0 - dist_map[mr["rowid"]]) for mr in main_rows]
            scored.sort(key=lambda x: x[1], reverse=True)  # sim 越大越相关
            return scored[:limit]
        except Exception as e:  # noqa: BLE001 - KNN 异常降级 loop
            logger.warning("memory store: vec0 KNN 失败,降级 loop: %s", e)
            return None

    def close(self) -> None:
        self._conn.close()
