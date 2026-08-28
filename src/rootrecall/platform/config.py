"""Config: load config.yaml, resolve $ENV vars, expose AppConfig.

Mirrors deer-flow's declarative config + $ENV resolution (see deer-flow
config.example.yaml and deerflow.config.app_config).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_PATTERN = re.compile(r"^\$(\w+)$")

logger = logging.getLogger(__name__)


class ModelConfig(BaseModel):
    """One model entry. `extra='allow'` so provider-specific kwargs pass through."""

    model_config = ConfigDict(extra="allow")

    use: str
    name: str
    display_name: str | None = None
    description: str | None = None
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_vision: bool = False
    when_thinking_enabled: dict | None = None
    when_thinking_disabled: dict | None = None
    thinking: dict | None = None
    pricing: dict | None = None


class SandboxConfig(BaseModel):
    """沙箱 provider 与可调参数。extra='allow' 给未来 provider 的特有键留口子。"""

    model_config = ConfigDict(extra="allow")

    use: str = "rootrecall.platform.sandbox.provider:LocalSandboxProvider"  # provider 反射目标
    workspace: str = "data/sandbox/workspace"  # bash 默认 cwd、输出落点
    bash_command_timeout: float = 600.0  # 单条命令超时(秒),超时杀整组进程
    bash_output_max_chars: int = 20000  # bash 输出截断阈值
    read_file_output_max_chars: int = 50000  # read_file 输出截断阈值
    ls_output_max_chars: int = 20000  # ls 输出截断阈值


class EmbedderConfig(BaseModel):
    """embedding 配置(provider 抽象,见 services/code_index/embed.py)。

    两种 provider:
      - openai_compatible:远端 OpenAI 兼容 API(DashScope / SiliconFlow / OpenAI / vLLM),
        用 base_url + api_key + model,和调 chat 一样配 key。
      - sentence_transformers:本地(需 `uv sync --extra embedding-local`)。
    extra='allow' 给未来新 provider 的特有键留口子。
    """

    model_config = ConfigDict(extra="allow")

    provider: str = "openai_compatible"  # openai_compatible | sentence_transformers
    model: str = "text-embedding-v4"  # 远端模型名;本地模式填 HF 名(如 Qwen/Qwen3-Embedding-0.6B)
    # —— 远端 OpenAI 兼容 ——
    base_url: str | None = None  # DashScope: .../compatible-mode/v1;SiliconFlow: api.siliconflow.cn/v1
    api_key: str | None = None  # $DASHSCOPE_API_KEY 等($ENV 解析后填入)
    dimensions: int | None = None  # Qwen3 系列可调(64-2048);bge-m3 必须留 None(传了报 400)
    batch_limit: int = 10  # 远端每请求文本条数上限(DashScope v4=10)
    # —— 通用 ——
    normalize: bool = True  # 客户端 L2 归一化,保 cosine 一致
    # —— 本地 sentence_transformers ——
    max_seq_length: int = 8192  # ⚠️ 本地必须显式设(ST 默认 512 会静默截断长代码)
    device: str | None = None  # cpu / cuda / mps;None 让库自选
    batch_size: int = 16  # 本地批编码大小(CPU 按内存调)
    hf_endpoint: str | None = "https://hf-mirror.com"  # 本地下载镜像;None 不设
    query_instruction: str | None = "query"  # Qwen3 用 prompt_name="query";bge-m3 留 None


class LSPConfig(BaseModel):
    """L2 精确导航(clangd 经 multilspy)的配置(P1.5)。

    面向小白:这是"精确查谁调用谁"那层的旋钮——用什么 clangd、起它时加什么参数、
    超时多久、找不到结果重试几次。绝大多数情况用默认值即可,只在交叉编译/大仓时才调。
    extra='allow' 给将来加的旋钮(如索引就绪信号)留口子。
    """

    model_config = ConfigDict(extra="allow")

    clangd_path: str | None = None  # null = shutil.which("clangd") 自动找;或写绝对路径
    extra_args: list[str] = Field(default_factory=list)  # 追加 clangd flag(如交叉编译 --query-driver=...)
    start_timeout: float = 30.0  # 起 clangd + initialize 握手超时(秒)
    request_timeout: float = 15.0  # 单次 references/definition/hover 超时(秒)
    index_retry: int = 1  # 结果为空/偏少时重试次数(防后台索引没建完)
    index_retry_delay: float = 0.3  # 重试间隔(秒)
    compile_commands_dir: str | None = None  # null = clangd 自动从源文件向上找;或强制指定目录


class CodeIndexConfig(BaseModel):
    """代码理解服务配置(P1 分阶段搭建)。

    P1.2 先有 embedding;vector_store / retrieval / repo_map 字段随对应阶段补
    (见 docs/p1-code-understanding-design.md §9)。extra='allow' 让后续阶段子段能增量加。
    """

    model_config = ConfigDict(extra="allow")

    embedding: EmbedderConfig = Field(default_factory=EmbedderConfig)
    lsp: LSPConfig = Field(default_factory=LSPConfig)  # P1.5:L2 精确导航(clangd)


class NativeMemoryConfig(BaseModel):
    """native 后端的子旋钮(R1)。extra='allow' 给将来加的旋钮留口子。

    面向小白:native 后端 = "组合已有两个引擎当记忆底座"。这几个旋钮控制它怎么组合——
    要不要接结构图、要不要复用 code_index 的向量/重排、巩固参数多少。
    """

    model_config = ConfigDict(extra="allow")

    structural: str = "none"  # none(不接结构图)| crg(接 code-review-graph,需 --extra code-review-graph)
    embed: str = "code_index"  # code_index(复用 embedder 给 KI summary 算向量)| off(只 BM25)
    rerank: str = "code_index"  # code_index(复用 reranker 精排 recall)| off(只 RRF 融合)
    recall_top_k: int = 5  # recall 默认返回条数
    decay_halflife_days: float = 180.0  # 衰减半衰期(天):exp(-age/halflife);Weibull 留 backlog
    promote_access_count: int = 3  # 被召回≥N 次 → 升级 mental_model(Letta 3+ 规则)
    auto_consolidate: bool = True  # recall 命中达标条目 → 后台异步 consolidate 自转(对标 Cognee self-improving;见 service.recall)
    auto_index: bool = True  # 向量 ANN(sqlite-vec vec0):True→count>ann_threshold 切 KNN;False→纯 Python loop
    ann_threshold: int = 500  # N>此值切 vec0 KNN(N≤此走 loop 更快;benchmark 实测 1024 维交叉点 ~200-500)
    merge_step: float = 0.3  # 重提时 Bayes 置信度累加步长(mnemopi veracity)
    stale_after_days: float = 365.0  # B4:超过这么多天没人翻(last_recalled/created_at 取晚者)→ 打 stale 标签(只标不降权)
    merged_upstream_discount: float = 0.5  # B3:补丁已在上游(apply --reverse 过)→ confidence 打此折 + merged_upstream 标签


class MemoryConfig(BaseModel):
    """记忆核心配置(R1,★P3 差异化)。对应 config.yaml 的 memory: 段。

    后端可换(backend-swap):backend 名 = backends/<name>/ 文件夹名;也接受
    'pkg.mod:Cls' 点路径。v1 只实现 native(组合 code_index + code-review-graph)。
    """

    model_config = ConfigDict(extra="allow")

    backend: str = "native"  # native | mem0 | cognee | 'pkg.mod:Cls'
    store_path: str = "data/memory"  # native SQLite 落点
    native: NativeMemoryConfig = Field(default_factory=NativeMemoryConfig)


class OpencodeDelegateConfig(BaseModel):
    """opencode 委托后端子配置(v1 默认,本机已装 v1.18.3)。

    面向小白:opencode 是被委托的"外勤侦探"。这几个旋钮控制怎么请它——用哪个可执行文件、
    指定哪个模型/子 agent、推理多卖力(variant)、要不要自动批准工具、超时多久。
    对应 config.yaml 的 delegate.opencode 段。
    """

    model_config = ConfigDict(extra="allow")

    bin: str = "opencode"  # 可执行文件名;或绝对路径
    model: str | None = None  # null = opencode 自带默认 provider;或 "provider/model"
    agent: str | None = None  # null = opencode 默认 agent;或指定子 agent 名
    variant: str | None = None  # null = 默认;"high"|"max"|"minimal"(provider 推理档,对齐 T2L Medium)
    auto_approve: bool = True  # 无头必须(--auto):自动批准未显式拒绝的权限
    format: str = "json"  # json(NDJSON 事件流)| default(格式化文本)
    timeout: float = 600.0  # 委托总超时(秒)
    config: str | None = "config/opencode_rootrecall.json"  # RootRecall 自带 opencode 配置(agent+steps+permission);env OPENCODE_CONFIG 注入,与用户全局 provider/key 合并
    retry_max: int = 2  # 瞬时网络错(connection reset 等)重试次数 —— glm API 偶发 reset,delegate 自动 --continue 续 session 重试
    fallback_model: str | None = None  # 主模型重试仍瞬时错时换它(如 "uniontech-ai/deepseek-v4-flash":flash 非推理、快、reset 概率低)


class OmpDelegateConfig(BaseModel):
    """omp 委托后端子配置(备选,本机暂未装)。对应 delegate.omp 段。"""

    model_config = ConfigDict(extra="allow")

    bin: str = "omp"
    mode: str = "rpc"  # rpc(NDJSON 流)| print(-p 纯文本兜底)
    auto_approve: bool = True  # 等价 --yolo
    timeout: float = 600.0


class ClaudeDelegateConfig(BaseModel):
    """claude code 委托后端子配置(备选,需另装 claude CLI)。对应 delegate.claude 段。"""

    model_config = ConfigDict(extra="allow")

    bin: str = "claude"
    timeout: float = 600.0


class DelegateConfig(BaseModel):
    """委托层配置(R2,★P2 MVP)。对应 config.yaml 的 delegate: 段。

    面向小白:这一层控制"把 coding 活外包给谁"。backend 选 opencode(v1 默认)/omp/claude;
    下面三块是各自的旋钮。抽象接口 CodingAgentDelegate 从第一天起支持后端可换(三锁定决策 #2)。
    R3.1 #54-rework:多候选采样投票已弃,改「迭代 verify-refine 双循环」(max_localize/repair_loops
    控制每阶段最多自审重试几轮);2026-07-31 进一步移除 rerank/majority_voting(无 oracle + 模型
    近确定性 → 投票平凡白烧 token;现代 SOTA 转单轨迹+执行验证,即本 B 路线)。
    """

    model_config = ConfigDict(extra="allow")

    backend: str = "opencode"  # opencode(v1 默认)| omp | claude | 'pkg.mod:Cls'
    max_localize_loops: int = 2  # R3.1 B:localize verify-refine 最大轮数(iter0 + 最多重定位 K1-1 次)
    max_repair_loops: int = 2  # R3.1 B:repair verify-refine 最大轮数
    opencode: OpencodeDelegateConfig = Field(default_factory=OpencodeDelegateConfig)
    omp: OmpDelegateConfig = Field(default_factory=OmpDelegateConfig)
    claude: ClaudeDelegateConfig = Field(default_factory=ClaudeDelegateConfig)


class RuntimeTokenBudgetConfig(BaseModel):
    """runtime token 预算子配置(对应 token_budget.py 的 TokenBudgetConfig,R3.0 默认值同)。

    面向小白:控制 lead agent(深度调研那种长 agent)一轮跑下来最多烧多少 token——
    超软警告阈值给模型塞「快收尾」提示;超硬停阈值剥 tool_calls 让它自然停(不抛异常)。
    """

    model_config = ConfigDict(extra="allow")

    max_tokens: int = 1_000_000  # 总 token 上限(input+output)
    max_input_tokens: int | None = None  # 可选:单限 input
    max_output_tokens: int | None = None  # 可选:单限 output
    warn_threshold: float = 0.7  # 软警告占比
    hard_stop_threshold: float = 1.0  # 硬停占比


class RuntimeToolOutputConfig(BaseModel):
    """runtime 工具输出预算子配置(对应 tool_output.py 的 ToolOutputBudgetConfig)。

    面向小白:agent 调工具(grep/bash/read)返回几万行不能全塞上下文。超过 externalize_min_chars
    就把全文写到 outputs_dir,模型只看摘要 + 文件路径(需要细节自己 read_file 取)。
    """

    model_config = ConfigDict(extra="allow")

    externalize_min_chars: int = 30_000  # 超过 → 外化到磁盘 + synopsis 摘要
    outputs_dir: str = "data/runtime/tool-outputs"  # 外化落盘根目录


class RuntimeConfig(BaseModel):
    """agent 运行时 harness 配置(R3,对标 deer-flow harness)。对应 config.yaml 的 runtime: 段。

    面向小白:这一层控制 RootRecall 自己的 lead agent(长 agent)怎么跑——断点续跑用 sqlite/内存、
    token 预算上限、工具输出外化阈值。R3.0 只驱动 checkpointer(checkpoint.py 读 checkpoint_backend/path);
    token_budget/tool_output 子配置是声明式(R3.0 中间件用 dataclass 默认值,CLI/runtime 启动时再 wire)。
    设计见 docs/设计/runtime-harness-design.md。
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True  # runtime 总开关(R3.0 默认开)
    checkpoint_backend: str = "sqlite"  # memory(内存,测)| sqlite(持久,默认)| postgres(R5)
    checkpoint_path: str | None = None  # None → checkpoint.py 默认 data/runtime/checkpoint.sqlite
    token_budget: RuntimeTokenBudgetConfig = Field(default_factory=RuntimeTokenBudgetConfig)
    tool_output: RuntimeToolOutputConfig = Field(default_factory=RuntimeToolOutputConfig)


class MCPConfig(BaseModel):
    """MCP server 对外暴露的 transport 配置(harness 转向 D0)。

    面向小白:RootRecall 当"工具+菜谱服务"给别的 coding agent(opencode 主 / codex / claude
    code)调。这层控制 MCP server 怎么对外"开门"——
      - stdio(默认):每次 agent 拉起一个子进程,1:1,本地单机最简,零配置(delegate 老路径用它)。
      - http(streamable-http):一个长期开着的 warm 进程,所有 agent 共用,省掉每修一个 bug
        都重启加载 ~1.2GB(sentence-transformers)的冷启动(= 解掉老 ③ 号 cold-boot 痛)。
    host/port 只有 http 模式用。CLI `rootrecall mcp serve --transport/--host/--port` 覆盖这里的默认值。
    """

    model_config = ConfigDict(extra="allow")

    transport: str = "stdio"  # stdio(默认,本地 1:1)| http(streamable-http,warm 多客户端)
    host: str = "127.0.0.1"  # http 绑定地址(本机;对外暴露改 0.0.0.0 + 鉴权留 R4/R5)
    port: int = 8765  # http 端口


class PatchGitConfig(BaseModel):
    """patch.git 子配置:P-A 的「auto-clone 代码仓」(1a)。

    面向小白:验货要一台"样机"(代码仓)。本地没有时,RootRecall 按这里配的地址
    自动 git clone 一台到 clone_dir。remotes 是你的"自定义连接"——比如公司内网镜像、或带 SSH key
    的私有仓地址;opencode 自己只会去公网 GitHub 拉,不知道你的内网镜像,所以这步归 RootRecall。
    """

    model_config = ConfigDict(extra="allow")

    clone_dir: str = "data/repos"  # auto-clone 落点(ensure_repo 命中这里就不再 clone,幂等)
    shallow: bool = True  # --depth 1 浅克隆(省时省空间;要完整历史改 false)
    remotes: dict[str, str] = Field(default_factory=dict)  # {仓库名: git url},如 {bluez: https://...}


class PatchConfig(BaseModel):
    """P-A 补丁/PR 分析配置(harness 转向后第二条 tool+skill 线,1a)。对应 config.yaml 的 patch: 段。

    面向小白:bug-RCA 是"从 bug 找补丁";P-A 反过来——"给个补丁/PR,验它对不对、该不该合"。
    这层只管一件后勤:缺代码仓时自动 clone(patch.git)。
    验证封顶 = apply,**不跑测试、不复现**(系统软件测试/构建环境太重,用户定);所以结论是
    "apply 过、看着靠谱",非"包对",最终拍板靠人/真机。build_check 试编译门 2026-08-10 撤
    (踩坑#14:构建信号歧义 + opencode 能 make)。
    """

    model_config = ConfigDict(extra="allow")

    git: PatchGitConfig = Field(default_factory=PatchGitConfig)


class AppConfig(BaseModel):
    """顶层配置(models / model_roles / tools / sandbox)。"""

    model_config = ConfigDict(extra="allow")

    models: list[ModelConfig] = Field(default_factory=list)
    model_roles: dict[str, str] = Field(default_factory=dict)
    # opencode 宿主桥接(2026-08-28):[{provider, model, name?}] —— load 时从宿主现读
    # base_url/key 物化成 models 里的派生条目(key 不落盘;embedding 不从宿主读,用户定)。
    models_from_opencode: list[dict] = Field(default_factory=list)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)  # 沙箱 provider + 参数
    code_index: CodeIndexConfig = Field(default_factory=CodeIndexConfig)  # 代码理解服务(P1)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)  # 记忆核心(R1,P3 差异化)
    delegate: DelegateConfig = Field(default_factory=DelegateConfig)  # 委托层(R2,P2 MVP)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)  # agent 运行时 harness(R3)
    mcp: MCPConfig = Field(default_factory=MCPConfig)  # MCP 对外 transport(harness 转向 D0)
    patch: PatchConfig = Field(default_factory=PatchConfig)  # 补丁/PR 分析(P-A 1a:auto-clone)

    # YAML 会把"键下面只有注释"的空段(如 config.yaml 现在的 tools:)解析成 None;
    # 而 pydantic 把显式 None 当成"有值"而非"用默认值",会校验失败。
    # 这里在赋值前把 None 强转成空集合,让空段也能正常解析。
    @field_validator("models", mode="before")
    @classmethod
    def _coerce_none_to_list(cls, v: Any) -> Any:
        return v if v is not None else []

    @field_validator("model_roles", mode="before")
    @classmethod
    def _coerce_none_to_dict(cls, v: Any) -> Any:
        return v if v is not None else {}

    @field_validator("models_from_opencode", mode="before")
    @classmethod
    def _coerce_none_to_oc_list(cls, v: Any) -> Any:
        return v if v is not None else []

    @model_validator(mode="after")
    def _materialize_opencode_models(self) -> AppConfig:
        """把 models_from_opencode 的 {provider, model, name?} 物化成真正的 chat 模型条目。

        base_url/key 都从宿主**现读**(key 不落盘的核心:磁盘上只有 provider/model/name)。
        宿主里找不到(provider 没显式 baseURL / 没装 opencode)→ 记 warning + 跳过该条,
        绝不崩 config 加载(机器没有 opencode 时段还在,是警告不是错误)。显式 models
        优先于派生条目(重名时 get_model 先命中显式的 —— 它排在前面)。
        """
        if not self.models_from_opencode:
            return self
        from rootrecall.platform.opencode_bridge import discover_opencode_models, opencode_api_key

        found = {(m["provider"], m["model"]): m
                 for m in discover_opencode_models()["models"]}
        for ref in self.models_from_opencode:
            provider = str(ref.get("provider", "")).strip()
            model = str(ref.get("model", "")).strip()
            hit = found.get((provider, model))
            if not hit:
                logger.warning(
                    "models_from_opencode 条目 %s/%s 在宿主 opencode 配置里找不到"
                    "(要求显式 baseURL),已跳过 —— 跑 `rootrecall opencode-models` 看可选项",
                    provider, model)
                continue
            self.models.append(ModelConfig(
                use="langchain_openai:ChatOpenAI",
                name=str(ref.get("name") or f"opencode-{provider}-{model}"),
                display_name=f"opencode:{provider}/{model}",
                model=model,
                base_url=hit["base_url"],
                api_key=opencode_api_key(provider),
            ))
        return self

    def get_model(self, name: str) -> ModelConfig | None:
        for m in self.models:
            if m.name == name:
                return m
        return None


def _resolve_env(value):
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("$opencode:"):
            # 宿主桥接 token(2026-08-28):$opencode:<provider> → 运行时读宿主 auth.json,
            # key 不落盘、不打印;缺 → ""(与 $ENV 缺失同语义)。embedding 不走此路(用户定)。
            from rootrecall.platform.opencode_bridge import opencode_api_key

            return opencode_api_key(s[len("$opencode:"):].strip())
        m = _ENV_PATTERN.match(s)
        if m:
            return os.environ.get(m.group(1), "")
    return value


def _walk_resolve(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _walk_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_resolve(v) for v in obj]
    return _resolve_env(obj)


_CONFIG_CACHE: AppConfig | None = None


def _default_config_path() -> Path:
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "config" / "config.yaml"
        if candidate.exists():
            return candidate
    return Path.cwd() / "config" / "config.yaml"


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and parse config.yaml into AppConfig (with $ENV resolved)."""
    global _CONFIG_CACHE
    # 加载 .env 到 os.environ —— $ENV 解析依赖它。放这里(而不只 cli.main)保证所有走
    # config 的入口(CLI / MCP / 测试 / 直接调用 / workflow)都有 env,不漏 key。
    from dotenv import load_dotenv

    load_dotenv()
    p = Path(path) if path else _default_config_path()
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = AppConfig(**_walk_resolve(raw))
    _CONFIG_CACHE = cfg
    return cfg


def get_app_config() -> AppConfig:
    """Cached access to the parsed config."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        load_config()
    assert _CONFIG_CACHE is not None  # load_config() 已给 _CONFIG_CACHE 赋值;这行给类型检查器收窄类型
    return _CONFIG_CACHE
