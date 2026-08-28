"""opencode 宿主配置桥接 —— chat 模型的 url/key 自动读宿主,RootRecall 不再维护第二套 key。

为什么(2026-08-28 用户定)
------------------------
RootRecall 是 opencode 生态的 MCP server(`install --global` 本就假定宿主在场);宿主已经
配好的 chat 模型(url + key),memory_extractor / 调研 workflow 这些 chat 消费方可以直接
复用。**边界**:embedding/reranker **不从 opencode 读**(2026-08-28 用户明确)—— 宿主配的
chat provider 多半没有 /embeddings 端点,向量空间也锁死 provider,embedding 保持 .env 显式
配置。

读什么(全部只读,绝不写宿主文件)
--------------------------------
| 要素   | 位置                                                    | 说明 |
|--------|---------------------------------------------------------|------|
| key    | ``~/.local/share/opencode/auth.json``                    | ``{<provider>: {type:"api", key:...}}``;``oauth``/``wellknown`` 是宿主会话凭证,调不了 API,跳过 |
| url    | ``~/.config/opencode/opencode.json`` 的 provider 块      | ``provider.<id>.options.baseURL`` + ``models`` 清单 |

安全纪律(与本仓既有约定一致):
  - **key 永不落盘**:采纳(adopt)写进 config.yaml 的只有 provider/model/name;key 在
    load_config 物化派生条目时从宿主**运行时读取**;
  - **key 永不打印**:discover 输出只有 has_key 布尔;
  - **防御式解析**:两个 JSON 都是宿主内部约定(版本会变),解析失败 → 当没配、回退
    .env,load_config 绝不因宿主配置坏了而崩;
  - **显式优先**:本桥接只在用户显式写 ``models_from_opencode:``(或手写
    ``api_key: $opencode:<provider>``)时生效,不劫持既有 ``$ENV`` 配置。

v1 约定:只认**显式写了 baseURL** 的 provider(没写 URL 的知名 provider 依赖宿主内置
注册表,不在文件里,后续需要再补端点映射);派生走 OpenAI 兼容(ChatOpenAI),anthropic
原生等非兼容端点不适用(遇到再显式配)。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_PREFIX = "$opencode:"


def _opencode_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "opencode"


def _opencode_data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def _load_json(path: Path) -> dict | None:
    """防御式读 JSON:不存在/解析失败(宿主可能写 JSONC/手改坏)→ None,绝不抛。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_auth(data_home: Path) -> dict:
    """auth.json 的 {provider: {type, key}} 表;坏文件当空。"""
    raw = _load_json(data_home / "auth.json")
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for provider, info in raw.items():
        if isinstance(info, dict) and info.get("type") == "api" and info.get("key"):
            out[provider] = info["key"]
    return out


def _read_providers(config_home: Path) -> dict:
    """opencode.json 的 provider 块;坏文件/无块当空。"""
    raw = _load_json(config_home / "opencode.json")
    if not isinstance(raw, dict):
        return {}
    providers = raw.get("provider")
    return providers if isinstance(providers, dict) else {}


def discover_opencode_models(config_home: Path | None = None,
                             data_home: Path | None = None) -> dict:
    """枚举宿主里可派生的 chat 模型(**不含 key 值,只带 has_key 布尔 —— 打印安全**)。

    返回 {models: [{provider, model, name, base_url, has_key}], providers_no_url: [...],
    notes: [...]}。只收:显式 baseURL + enabled 不为 false + models 清单里显式列了的模型;
    auth.json 里 type=api 的 key(或 options.apiKey 兜底)。
    """
    config_home = config_home or _opencode_config_home()
    data_home = data_home or _opencode_data_home()
    notes: list[str] = []
    cfg_json = config_home / "opencode.json"
    auth_json = data_home / "auth.json"
    if not cfg_json.exists():
        notes.append(f"未找到 {cfg_json}(宿主没配 provider 或未装 opencode)")
        return {"models": [], "providers_no_url": [], "notes": notes}
    if cfg_json.exists() and _load_json(cfg_json) is None:
        notes.append("opencode.json 解析失败(可能是 JSONC/手改坏)→ 只按可解析部分处理")
    if auth_json.exists() and _load_json(auth_json) is None:
        notes.append("auth.json 解析失败 → key 一律按无处理")

    auth = _read_auth(data_home)
    models_out: list[dict] = []
    no_url: list[str] = []
    for provider, blk in _read_providers(config_home).items():
        if not isinstance(blk, dict):
            continue
        if blk.get("enabled") is False:
            continue
        options = blk.get("options") or {}
        base_url = options.get("baseURL") or options.get("base_url")
        has_key = bool(auth.get(provider) or options.get("apiKey"))
        if not base_url:
            # 显式 baseURL 之外的一律不派生(v1 约定,见模块 docstring)
            no_url.append(provider)
            continue
        entries = blk.get("models") or {}
        if not isinstance(entries, dict) or not entries:
            continue  # provider 配了但没列模型(走宿主内置注册表,文件里没有)
        for model_id, minfo in entries.items():
            models_out.append({
                "provider": provider,
                "model": model_id,
                "name": (minfo.get("name") if isinstance(minfo, dict) else None) or model_id,
                "base_url": base_url,
                "has_key": has_key,
            })
    return {"models": models_out, "providers_no_url": sorted(no_url), "notes": notes}


def opencode_api_key(provider: str, config_home: Path | None = None,
                     data_home: Path | None = None) -> str:
    """取宿主某 provider 的 API key(auth.json type=api 优先,options.apiKey 兜底)。

    给两处用:``$opencode:<provider>`` token 解析(config._resolve_env)、
    models_from_opencode 物化(AppConfig 校验器)。缺 → ""(与 $ENV 缺失同语义,
    下游报 auth 错;本函数绝不抛 —— config 加载路径上不能因宿主问题崩)。
    """
    config_home = config_home or _opencode_config_home()
    data_home = data_home or _opencode_data_home()
    key = _read_auth(data_home).get(provider)
    if not key:
        blk = _read_providers(config_home).get(provider)
        if isinstance(blk, dict):
            options = blk.get("options") or {}
            key = options.get("apiKey")
    return key if isinstance(key, str) else ""


# ── 采纳:把选定模型写进 config.yaml 末尾的 models_from_opencode 段 ──────────────

_ADOPT_MARKER = "# ── models_from_opencode(rootrecall opencode-models --adopt 生成;" \
                 "key 不落盘,运行时从宿主读取)──"


def adopt_opencode_models(refs: list[str], config_path: Path,
                          config_home: Path | None = None,
                          data_home: Path | None = None) -> str:
    """把 ``provider/model`` 列表写成 config.yaml 末尾的标记段(**整块覆盖语义**,重跑=刷新全集)。

    段内只有 provider/model/name —— **key 永不落盘**(load_config 物化时从宿主现读)。
    段是独立顶层键,文本追加/整块替换,不动文件其余部分(config.yaml 注释丰富,禁 yaml 重排)。
    refs 里的每一项都必须能 discover 到(显式 baseURL),否则报错不动文件。
    """
    found = {(m["provider"], m["model"]): m
             for m in discover_opencode_models(config_home, data_home)["models"]}
    chosen: list[dict] = []
    for ref in refs:
        provider, _, model = ref.partition("/")
        hit = found.get((provider, model))
        if not hit:
            raise ValueError(f"宿主里没找到可派生的 {ref!r}(要求:opencode.json 显式 baseURL + "
                             f"models 清单里有该模型;跑 `rootrecall opencode-models` 看可选项)")
        chosen.append(hit)

    text = config_path.read_text(encoding="utf-8")
    lines = [_ADOPT_MARKER, "models_from_opencode:"]
    for m in chosen:
        lines += [f"  - provider: {m['provider']}",
                  f"    model: {m['model']}",
                  f"    name: opencode-{m['provider']}-{m['model']}"]
    block = "\n".join(lines) + "\n"

    if _ADOPT_MARKER in text:
        pre, _, rest = text.partition(_ADOPT_MARKER)
        # 整块替换:标记行 → models_from_opencode 键行 → 段体(缩进行),到下一个顶层键
        # (行首非空白非注释)或 EOF 为止都算本段,整段换新。键行本身要跳过(它顶格但属于本段)。
        body_lines = rest.splitlines()
        end = len(body_lines)
        for i, ln in enumerate(body_lines[1:], start=1):
            if i == 1 and ln.strip() == "models_from_opencode:":
                continue
            if ln and not ln[0].isspace() and not ln.startswith("#"):
                end = i
                break
        tail = "\n".join(body_lines[end:]).lstrip("\n")
        text = pre + block + (tail + "\n" if tail else "")
        if not text.endswith("\n"):
            text += "\n"
    else:
        text = text.rstrip("\n") + "\n\n" + block
    config_path.write_text(text, encoding="utf-8")

    # 自检:落盘内容绝不含宿主 key 明文(防御回归 —— adopt 路径未来若变,这条兜底)
    written = config_path.read_text(encoding="utf-8")
    auth = _read_auth(data_home or _opencode_data_home())
    leaked = [p for p, k in auth.items() if k and k in written]
    if leaked:
        raise RuntimeError(f"采纳段疑似写入 key 明文(providers={leaked})—— 已落盘但请立即检查")
    return f"已写 {len(chosen)} 个模型到 {config_path}(models_from_opencode 标记段,整块覆盖语义)"
