"""代码理解服务 · 第三步:把 chunk 变成向量(P1.2 embed.py)。

这一层干什么
------------
上一层 chunker.py 把代码切成了 chunk(每个函数/类/模块级一块)。要做"语义检索"
(搜"断连处理"能命中 disconnect_cb),得把每块代码变成一个**向量**(一串数字),让数学
上的"距离"代表"语义相似度"。这步叫 embedding(嵌入)。后面 store.py 把向量存进向量库;
检索时把查询也变成向量,算两向量距离找最近的就是答案。

选型:Qwen3-Embedding 系列(详见 docs/p1-code-understanding-design.md §5)
------------------------------------------------------------------
RootRecall 核心是代码检索,Qwen3-Embedding 把代码检索列为系列核心能力(通用模型如 bge-m3
代码非强项)。本文件支持两种**部署模式**(provider 抽象,像模型工厂那样 config 切换):

1. **远端 OpenAI 兼容(默认)** —— 调阿里云 DashScope `text-embedding-v4`(= Qwen3-Embedding
   全血版,质量优于本地 0.6B),或 SiliconFlow 的 bge-m3 / Qwen3-Embedding-0.6B。和现在调
   DeepSeek(chat)完全对称:base_url + api_key + model 配置驱动,复用 openai 库,**零新依赖、
   不下载模型、不装 torch**。
2. **本地 sentence-transformers(可选)** —— `uv sync --extra embedding-local` 装上后,离线 /
   数据不出本地。模型 ~1.2GB,CPU 推理较慢。

选定就锁死(换 provider/模型 = 向量空间变 = 要全量重嵌,见 fingerprint)。

本文件的几个设计点
------------------
1. **provider 抽象**:Embedder 是个 Protocol(接口);RemoteEmbedder(远端)/ LocalEmbedder
   (本地)都实现它;create_embedder(config) 按 config.embedding.provider 选。换模式只改 config。
2. **chunk_expansion 元数据头**:嵌入代码块前,拼一行注释格式的元数据
   (`# file: ... · symbol: ... · kind: ... · lang: ...`),让向量带出处信息(Anthropic
   Contextual Retrieval 的轻量版)。注释是代码一部分(模型训练见过),比 `file|symbol|kind`
   管道串更稳。查询端不加(查询没这些元数据)。
3. **批量分批(远端)**:远端 API 每请求有文本条数上限(DashScope v4=10),embed_chunks
   内部按 batch_limit 自动分批,逐批调 API 再拼回。
4. **维度锁(dimensions)**:Qwen3 系列可调输出维度(64-2048);bge-m3 不支持 dimensions 参数
   (传了报 400)。dimensions 配 None 则不传。存取必须同维度(进指纹)。
5. **客户端归一化(normalize)**:API 返回的向量未必归一化,显式做 L2 归一化,保证"点积 =
   cosine 相似度"(LanceDB 默认用 cosine)。进指纹。
6. **本地 max_seq_length 陷阱**:本地 sentence-transformers 默认 max_seq_length=512,会**静默
   截断**长代码。LocalEmbedder 加载后必须显式设(默认 8192)。进指纹。
7. **模型指纹**:`provider|model|[base_url]|dim|normalize`,index.py 存进索引清单,任一项变
   → 全量重建。

还没做(P1.2 范围外,记 .claude/memory/backlog-production-grade.md #8/#9)
------------------------------------------------------------------------
- 完整三态加载 + 失败冷却自愈(deer-flow tiktoken 模式);远端 API 的指数退避重试。
- 本地 ONNX int8 提速。

对外提供
--------
- Embedder(Protocol)、RemoteEmbedder、LocalEmbedder:三种粒度的 embedding 句柄。
- create_embedder(config):按 config 选实现。
- expand_chunk_text(chunk):给 chunk 拼元数据头(单独导出,方便测试/复用)。
"""

from __future__ import annotations

import os
from typing import Protocol

import numpy as np  # 向量运算(L2 归一化);远端模式它也是 lancedb/openai 链上的依赖,已在环境

from rootrecall.services.code_index.chunker import CodeChunk

# ──────────────────────────────────────────────────────────────────────────
# §1 常量 + chunk_expansion
# ──────────────────────────────────────────────────────────────────────────

# 默认远端模型(DashScope 百炼):text-embedding-v4 = Qwen3-Embedding 全血版。
DEFAULT_REMOTE_MODEL = "text-embedding-v4"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 默认本地模型(sentence-transformers):Qwen3-Embedding-0.6B(0.6B / CPU fast / 32K)。
DEFAULT_LOCAL_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_MAX_SEQ_LENGTH = 8192  # 本地:显式设!ST 默认 512 会静默截断
DEFAULT_BATCH_SIZE = 16  # 本地批编码大小
DEFAULT_BATCH_LIMIT = 10  # 远端每请求文本条数上限(DashScope v4=10)
# 远端单条嵌入输入字符预算:provider 有单条 token 上限(text-embedding-v4 实测:密代码 ~12k 字符过、
# 16k 死 → 8192 token;oui 表类可更密,取 8000 字符在**任何分词密度**下都安全)。超长 chunk 只截断
# 送嵌的文本,LanceDB 存的 text 仍是全文,BM25(fts_text)也不受影响。0 = 不截断(自建 vLLM 无限长可用)
DEFAULT_MAX_INPUT_CHARS = 8000

# 各语言的行注释前缀(给 chunk_expansion 拼元数据头用)。未知语言兜底用 #。
_COMMENT_PREFIX: dict[str, str] = {"python": "#", "c": "//", "cpp": "//"}


def _comment_prefix(language: str) -> str:
    """按语言取行注释符(未知语言兜底用 #)。"""
    return _COMMENT_PREFIX.get(language, "#")


def expand_chunk_text(chunk: CodeChunk) -> str:
    """把 chunk 的元数据拼成一行注释,加在代码前,供 embedding 用(远端/本地通用)。

    为什么加:embedding 默认只看代码字面,看不到它的出处。加一行注释元数据当上下文锚点,
    向量就带上了文件/符号信息(Anthropic Contextual Retrieval 轻量版,不调 LLM)。
    为什么用注释格式(而非管道串):注释是代码的一部分,模型训练见过,分布内更稳。
    查询端不加(查询没有这些元数据)。
    """
    prefix = _comment_prefix(chunk.language)
    header = f"{prefix} file: {chunk.file} · symbol: {chunk.symbol} · kind: {chunk.kind} · lang: {chunk.language}"
    return f"{header}\n{chunk.text}"


def _normalize(arr: np.ndarray) -> np.ndarray:
    """L2 归一化:每条向量除以自己的 L2 范数,使"点积 = cosine 相似度"。

    1D(单条)和 2D(批量)都处理;范数为 0(全零向量,理论不出现)的维度不除,防除零。
    """
    if arr.ndim == 1:
        norm = np.linalg.norm(arr)
        return arr / norm if norm > 0 else arr
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


# ──────────────────────────────────────────────────────────────────────────
# §2 Embedder 接口(Protocol)
# ──────────────────────────────────────────────────────────────────────────


class Embedder(Protocol):
    """embedding 句柄的接口。RemoteEmbedder / LocalEmbedder 都实现它(结构子类型)。

    一个进程复用一个实例即可(远端复用 HTTP 连接;本地避免反复加载模型)。
    """

    @property
    def dim(self) -> int:
        """向量维度(建库要用,必须和 LanceDB 表维度一致)。"""
        ...

    @property
    def fingerprint(self) -> str:
        """模型指纹:index.py 存进索引清单,变更 → 全量重建。"""
        ...

    def embed_chunks(self, chunks: list[CodeChunk]) -> np.ndarray:
        """批编码一批 chunk → (N, dim) 矩阵(已拼 chunk_expansion 头)。"""
        ...

    def embed_query(self, query: str) -> np.ndarray:
        """编码单条查询 → (dim,) 向量(不加任何头)。"""
        ...

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """批编码一批纯文本 → (N, dim) 矩阵(内部按 batch_limit 分批;不加任何头)。

        与 embed_chunks 的区别:入参是裸文本,不做 chunk 头拼接 —— 记忆 backfill
        (零 key 期间写入的条目补嵌)等非代码场景用。
        """
        ...

    def warm(self) -> None:
        """预热:触发首次连接/加载,避免首次检索被惰性初始化卡一下。"""
        ...


# ──────────────────────────────────────────────────────────────────────────
# §3 RemoteEmbedder:远端 OpenAI 兼容(默认)
# ──────────────────────────────────────────────────────────────────────────


class RemoteEmbedder:
    """远端 OpenAI 兼容 embedding(DashScope / SiliconFlow / OpenAI / 自建 vLLM 通用)。

    复用 openai 库(langchain-openai 的依赖,已在环境),零新依赖、不下载模型、不装 torch。
    base_url + api_key + model 配置驱动,换平台只改 config。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = DEFAULT_REMOTE_MODEL,
        dimensions: int | None = None,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        normalize: bool = True,
        max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    ):
        from openai import OpenAI  # 局部导入:远端模式才需要,也让 import 错误更定位

        if not api_key:
            raise ValueError("远端 embedding 需要 api_key(在 .env 配 DASHSCOPE_API_KEY / SILICONFLOW_API_KEY 等)。")
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._base_url = base_url
        self._model = model
        self._dimensions = dimensions  # None 则不传(Qwen3 系列可设;bge-m3 必须不设,否则 400)
        self._batch_limit = batch_limit
        self._normalize = normalize
        self._max_input_chars = max_input_chars
        self._dim_cache: int | None = None  # 首次 embed 后缓存维度

    def _raw_embed(self, texts: list[str]) -> list[list[float]]:
        """调一次 embeddings API(调用方保证 len(texts) <= batch_limit)。"""
        kwargs: dict = {"model": self._model, "input": texts}
        if self._dimensions is not None:
            kwargs["dimensions"] = self._dimensions
        resp = self._client.embeddings.create(**kwargs)
        # API 返回的 data 带 index,稳妥起见按 index 排序还原输入顺序
        return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]

    @property
    def dim(self) -> int:
        # 首次访问时发一条探测请求拿维度(花一条 embedding 的钱,极少),之后缓存
        if self._dim_cache is None:
            self._dim_cache = len(self._raw_embed(["dim probe"])[0])
        return self._dim_cache

    @property
    def fingerprint(self) -> str:
        # base_url 进指纹:不同平台的同名模型向量不互通(本地 Qwen3-0.6B ≠ DashScope v4)
        # tc(截断预算)进指纹:预算变 = 超长 chunk 的向量变,不混
        return (f"openai_compatible|{self._model}|{self._base_url}|{self.dim}"
                f"|{'l2' if self._normalize else 'raw'}|tc{self._max_input_chars}")

    def embed_chunks(self, chunks: list[CodeChunk]) -> np.ndarray:
        texts = [expand_chunk_text(c) for c in chunks]
        # 超长输入防御(踩坑实录:bluez v20 的大文件 chunk 展开后超 DashScope 单条 8192 token 上限,
        # 整批 400 打死索引):只截断**送嵌入**的文本,LanceDB 里存的 text 仍是全文(read_function 不受影响)。
        m = self._max_input_chars
        if m and any(len(t) > m for t in texts):
            texts = [t[:m] for t in texts]
        vecs: list[list[float]] = []
        # 按 batch_limit 分批(远端 API 每请求文本条数有上限),逐批调、拼回
        for i in range(0, len(texts), self._batch_limit):
            vecs.extend(self._raw_embed(texts[i : i + self._batch_limit]))
        arr = np.asarray(vecs, dtype=np.float32)
        self._dim_cache = arr.shape[1]
        return _normalize(arr) if self._normalize else arr

    def embed_query(self, query: str) -> np.ndarray:
        arr = np.asarray(self._raw_embed([query])[0], dtype=np.float32)
        self._dim_cache = arr.shape[0]
        return _normalize(arr) if self._normalize else arr

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.asarray([], dtype=np.float32)
        m = self._max_input_chars  # 与 embed_chunks 同款超长防御(单条超限整批 400)
        if m:
            texts = [t[:m] for t in texts]
        vecs: list[list[float]] = []
        for i in range(0, len(texts), self._batch_limit):
            vecs.extend(self._raw_embed(texts[i : i + self._batch_limit]))
        arr = np.asarray(vecs, dtype=np.float32)
        self._dim_cache = arr.shape[1]
        return _normalize(arr) if self._normalize else arr

    def warm(self) -> None:
        """预热:发一条探测请求,建立 HTTP 连接 + 触发鉴权(顺便缓存 dim)。"""
        _ = self.dim


# ──────────────────────────────────────────────────────────────────────────
# §4 LocalEmbedder:本地 sentence-transformers(可选)
# ──────────────────────────────────────────────────────────────────────────


class LocalEmbedder:
    """本地 embedding(sentence-transformers)。需 `uv sync --extra embedding-local`。

    适合离线 / 数据不出本地的场景。模型 ~1.2GB,CPU 推理较慢(批约十几秒)。
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_LOCAL_MODEL,
        max_seq_length: int = DEFAULT_MAX_SEQ_LENGTH,
        device: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        normalize: bool = True,
        query_instruction: str | None = "query",
        hf_endpoint: str | None = "https://hf-mirror.com",
    ):
        try:
            from sentence_transformers import SentenceTransformer  # pyright: ignore[reportMissingImports]
        except ImportError as e:  # 没装 optional 依赖时给清晰指引
            raise ImportError("本地 embedding 需要 sentence-transformers。装它: uv sync --extra embedding-local(会拉 torch ~800MB+)") from e

        self._model_name = model
        self._max_seq_length = max_seq_length
        self._normalize = normalize
        self._batch_size = batch_size
        self._query_instruction = query_instruction

        # 国内下载排雷:加载前把镜像写进环境变量(ST / transformers 底层都读 HF_ENDPOINT)。
        if hf_endpoint:
            os.environ["HF_ENDPOINT"] = hf_endpoint

        self._model = SentenceTransformer(model, device=device)
        # ⚠️ 关键陷阱:显式设 max_seq_length,否则默认 512 会静默截断长代码
        self._model.max_seq_length = max_seq_length

    @property
    def dim(self) -> int:
        return self._model.get_sentence_embedding_dimension()

    @property
    def fingerprint(self) -> str:
        return f"sentence_transformers|{self._model_name}|{self._max_seq_length}|{self.dim}|{'l2' if self._normalize else 'raw'}"

    def embed_chunks(self, chunks: list[CodeChunk]) -> np.ndarray:
        texts = [expand_chunk_text(c) for c in chunks]
        return self._model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=self._normalize,
            show_progress_bar=True,
        )

    def embed_query(self, query: str) -> np.ndarray:
        kwargs: dict = {"normalize_embeddings": self._normalize}
        if self._query_instruction:
            kwargs["prompt_name"] = self._query_instruction  # Qwen3 查询端 prompt
        return self._model.encode([query], **kwargs)[0]

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.asarray([], dtype=np.float32)
        return self._model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=self._normalize,
            show_progress_bar=False,
        )

    def warm(self) -> None:
        self.embed_query("warmup")


# ──────────────────────────────────────────────────────────────────────────
# §5 工厂:按 config 选实现
# ──────────────────────────────────────────────────────────────────────────


def create_embedder(cfg) -> Embedder:
    """按 EmbedderConfig(或 dict)的 provider 字段选 RemoteEmbedder / LocalEmbedder。

    cfg 通常来自 get_app_config().code_index.embedding;也接受 dict(方便测试)。
    """

    # 兼容 dict / pydantic model 两种取值方式
    def get(key, default=None):
        if isinstance(cfg, dict):
            return cfg.get(key, default)
        return getattr(cfg, key, default)

    provider = get("provider", "openai_compatible")
    normalize = get("normalize", True)

    if provider == "openai_compatible":
        return RemoteEmbedder(
            base_url=get("base_url") or DEFAULT_BASE_URL,
            api_key=get("api_key") or "",
            model=get("model", DEFAULT_REMOTE_MODEL),
            dimensions=get("dimensions"),
            batch_limit=get("batch_limit", DEFAULT_BATCH_LIMIT),
            normalize=normalize,
            max_input_chars=get("max_input_chars", DEFAULT_MAX_INPUT_CHARS),
        )
    if provider == "sentence_transformers":
        return LocalEmbedder(
            model=get("model", DEFAULT_LOCAL_MODEL),
            max_seq_length=get("max_seq_length", DEFAULT_MAX_SEQ_LENGTH),
            device=get("device"),
            batch_size=get("batch_size", DEFAULT_BATCH_SIZE),
            normalize=normalize,
            query_instruction=get("query_instruction", "query"),
            hf_endpoint=get("hf_endpoint", "https://hf-mirror.com"),
        )
    raise ValueError(f"未知 embedding provider: {provider}(支持:openai_compatible / sentence_transformers)")
