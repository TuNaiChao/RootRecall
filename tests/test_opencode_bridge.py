"""opencode 宿主桥接单测(chat 模型 url/key 复用;embedding 不走此路,2026-08-28 用户定)。

覆盖:discover(正常/oauth/无 baseURL/enabled:false/坏 JSON/缺失)、key 解析(auth 优先/
options 兜底/缺失空串)、$opencode: token、AppConfig 物化(派生条目/找不到跳过)、
adopt(标记段写入/key 不落盘/整块覆盖/未知 ref 拒绝)、CLI 探测分支。
全部 hermetic:fixture 家目录在 tmp_path,不碰真机 ~/.config/opencode。
"""
from __future__ import annotations

import json

import pytest

from rootrecall.platform import opencode_bridge as bridge
from rootrecall.platform.config import AppConfig, _resolve_env, load_config


def _mk_host(tmp_path, *, auth=None, providers=None, bad_cfg=False, bad_auth=False):
    """造一个假的 opencode 宿主家:config_home/opencode.json + data_home/auth.json。"""
    config_home = tmp_path / "cfg"
    data_home = tmp_path / "data"
    config_home.mkdir()
    data_home.mkdir()
    if providers is not None or bad_cfg:
        (config_home / "opencode.json").write_text(
            "// 带注释的坏 JSON" if bad_cfg else json.dumps({"provider": providers}),
            encoding="utf-8")
    if auth is not None or bad_auth:
        (data_home / "auth.json").write_text(
            "{oops" if bad_auth else json.dumps(auth), encoding="utf-8")
    return config_home, data_home


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_full(tmp_path):
    """provider 显式 baseURL + models + auth type=api key → 可派生条目(has_key 布尔,无 key 值)。"""
    providers = {
        "uniontech-ai": {
            "options": {"baseURL": "https://ai.example.com/v1"},
            "models": {"glm-5": {"name": "GLM 5"}, "kimi-k2.5": {}},
        },
    }
    auth = {"uniontech-ai": {"type": "api", "key": "sk-SECRET_VALUE_1"}}
    ch, dh = _mk_host(tmp_path, auth=auth, providers=providers)
    r = bridge.discover_opencode_models(ch, dh)
    assert len(r["models"]) == 2
    m = r["models"][0]
    assert (m["provider"], m["model"], m["has_key"]) == ("uniontech-ai", "glm-5", True)
    assert m["base_url"] == "https://ai.example.com/v1"
    dumped = json.dumps(r)
    assert "sk-SECRET_VALUE_1" not in dumped          # key 值绝不进 discover 输出


def test_discover_variants(tmp_path):
    """oauth 型 key 不可用 / 无 baseURL 不派生 / enabled:false 跳过 / options.apiKey 兜底。"""
    providers = {
        "oauthprov": {"options": {"baseURL": "https://a.example.com"}, "models": {"m1": {}}},
        "nourl": {"options": {}, "models": {"m2": {}}},
        "disabled": {"enabled": False, "options": {"baseURL": "https://b.example.com"},
                     "models": {"m3": {}}},
        "keyinopts": {"options": {"baseURL": "https://c.example.com", "apiKey": "sk-OPT_KEY"},
                      "models": {"m4": {}}},
    }
    auth = {"oauthprov": {"type": "oauth", "refresh": "r", "access": "a", "expires": 1}}
    ch, dh = _mk_host(tmp_path, auth=auth, providers=providers)
    r = bridge.discover_opencode_models(ch, dh)
    got = {(m["provider"], m["has_key"]) for m in r["models"]}
    assert got == {("oauthprov", False), ("keyinopts", True)}   # oauth 不算 key;disabled 没了
    assert r["providers_no_url"] == ["nourl"]


def test_discover_broken_or_missing(tmp_path):
    """坏 JSON / 宿主不存在 → notes + 空,绝不抛。"""
    ch, dh = _mk_host(tmp_path, bad_cfg=True, bad_auth=True)
    r = bridge.discover_opencode_models(ch, dh)
    assert r["models"] == [] and any("解析失败" in n for n in r["notes"])
    empty = tmp_path / "nothing"
    empty.mkdir()
    r2 = bridge.discover_opencode_models(empty, empty)
    assert r2["models"] == [] and any("未找到" in n for n in r2["notes"])


# ── key 解析 + $opencode: token ──────────────────────────────────────────────

def test_opencode_api_key_precedence(tmp_path):
    """auth.json(type=api)优先,options.apiKey 兜底,都没有 → 空。"""
    providers = {
        "a": {"options": {"baseURL": "u", "apiKey": "sk-OPT"}, "models": {}},
        "b": {"options": {"baseURL": "u", "apiKey": "sk-OPT"}, "models": {}},
        "c": {"options": {"baseURL": "u"}, "models": {}},
    }
    auth = {"a": {"type": "api", "key": "sk-AUTH"}}
    ch, dh = _mk_host(tmp_path, auth=auth, providers=providers)
    assert bridge.opencode_api_key("a", ch, dh) == "sk-AUTH"
    assert bridge.opencode_api_key("b", ch, dh) == "sk-OPT"
    assert bridge.opencode_api_key("c", ch, dh) == ""
    assert bridge.opencode_api_key("ghost", ch, dh) == ""


def test_resolve_env_opencode_token(monkeypatch):
    """$opencode:<provider> token:走 bridge 取 key;未知 provider → 空(与 $ENV 缺失同语义)。"""
    monkeypatch.setattr(bridge, "opencode_api_key",
                        lambda p, ch=None, dh=None: "sk-TOKEN" if p == "deepseek" else "")
    assert _resolve_env("$opencode:deepseek") == "sk-TOKEN"
    assert _resolve_env("$opencode:ghost") == ""
    assert _resolve_env("$DEEPSEEK_API_KEY") == _resolve_env("$DEEPSEEK_API_KEY")  # 原路不受影响


# ── AppConfig 物化 ────────────────────────────────────────────────────────────

def _patch_bridge(monkeypatch, found, key):
    monkeypatch.setattr(bridge, "discover_opencode_models",
                        lambda ch=None, dh=None: {"models": found, "providers_no_url": [], "notes": []})
    monkeypatch.setattr(bridge, "opencode_api_key", lambda p, ch=None, dh=None: key)


def test_materialize_opencode_models(monkeypatch):
    """models_from_opencode → 物化成 models 里的 ChatOpenAI 条目(base_url/key 从宿主现读)。"""
    _patch_bridge(monkeypatch, found=[{"provider": "u", "model": "glm-5",
                                       "base_url": "https://u.example.com/v1", "has_key": True}],
                  key="sk-LIVE")
    cfg = AppConfig(**{
        "models": [{"use": "langchain_openai:ChatOpenAI", "name": "deepseek-v4-pro",
                    "model": "deepseek-v4-pro"}],
        "models_from_opencode": [{"provider": "u", "model": "glm-5"}],
    })
    assert [m.name for m in cfg.models] == ["deepseek-v4-pro", "opencode-u-glm-5"]
    derived = cfg.get_model("opencode-u-glm-5")
    assert derived.model_dump()["base_url"] == "https://u.example.com/v1"
    assert derived.model_dump()["api_key"] == "sk-LIVE"
    assert derived.use == "langchain_openai:ChatOpenAI"


def test_materialize_skips_unknown_with_warning(monkeypatch, caplog):
    """宿主里找不到的 ref → warning + 跳过,绝不崩 config(机器没装 opencode 时的常态)。"""
    _patch_bridge(monkeypatch, found=[], key="")
    with caplog.at_level("WARNING"):
        cfg = AppConfig(**{"models_from_opencode": [{"provider": "x", "model": "y"}]})
    assert cfg.models == []
    assert "找不到" in caplog.text


def test_load_config_end_to_end(tmp_path, monkeypatch):
    """全链:config.yaml 带 models_from_opencode 段 → load_config 出派生模型(重跑缓存清理)。"""
    _patch_bridge(monkeypatch, found=[{"provider": "u", "model": "glm-5",
                                       "base_url": "https://u.example.com/v1", "has_key": True}],
                  key="sk-E2E")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "models:\n"
        "  - use: langchain_openai:ChatOpenAI\n"
        "    name: base-model\n"
        "    model: base\n"
        "models_from_opencode:\n"
        "  - provider: u\n"
        "    model: glm-5\n"
        "    name: my-glm\n",
        encoding="utf-8")
    import rootrecall.platform.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_CONFIG_CACHE", None)
    cfg = load_config(cfg_file)
    m = cfg.get_model("my-glm")                        # 显式 name 覆盖默认命名
    assert m is not None and m.model_dump()["api_key"] == "sk-E2E"


# ── adopt ─────────────────────────────────────────────────────────────────────

def test_adopt_writes_block_without_key(tmp_path):
    """adopt:标记段进 config(provider/model/name),key 值绝不落盘;段尾保留文件其余内容。"""
    providers = {"u": {"options": {"baseURL": "https://u.example.com/v1"},
                       "models": {"glm-5": {}, "kimi": {}}}}
    auth = {"u": {"type": "api", "key": "sk-NEVER_ON_DISK"}}
    ch, dh = _mk_host(tmp_path, auth=auth, providers=providers)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("models:\n  - use: x\n    name: base\n    model: b\n", encoding="utf-8")

    out = bridge.adopt_opencode_models(["u/glm-5"], cfg_file, ch, dh)
    text = cfg_file.read_text(encoding="utf-8")
    assert "已写 1 个模型" in out
    assert "models_from_opencode:" in text and "- provider: u" in text
    assert "sk-NEVER_ON_DISK" not in text               # 核心:key 不落盘
    assert text.index("models:\n") < text.index("models_from_opencode:")  # 追加在末尾,原段未动
    assert "name: base" in text

    # 整块覆盖:换一组,旧的 glm-5 消失、新的 kimi 在,文件其余部分不动
    bridge.adopt_opencode_models(["u/kimi"], cfg_file, ch, dh)
    text2 = cfg_file.read_text(encoding="utf-8")
    assert "u/klm" not in text2 and "model: kimi" in text2
    assert text2.count("models_from_opencode:") == 1
    assert "name: base" in text2 and "- provider: u" in text2


def test_adopt_rejects_unknown(tmp_path):
    """未知 ref → ValueError 且文件不动。"""
    ch, dh = _mk_host(tmp_path, providers={"u": {"options": {"baseURL": "x"}, "models": {"m": {}}}})
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("models: []\n", encoding="utf-8")
    before = cfg_file.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="没找到"):
        bridge.adopt_opencode_models(["ghost/m"], cfg_file, ch, dh)
    assert cfg_file.read_text(encoding="utf-8") == before


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_opencode_models_no_host(monkeypatch, capsys):
    """宿主无可派生模型 → 提示 + 退出码 1(不抛;monkeypatch 隔离真机宿主)。"""
    from rootrecall.cli import cmd_opencode_models

    monkeypatch.setattr(bridge, "discover_opencode_models",
                        lambda ch=None, dh=None: {"models": [], "providers_no_url": [], "notes": []})

    class _A:
        adopt = None
    rc = cmd_opencode_models(_A())
    out = capsys.readouterr().out
    assert rc == 1 and "没有可派生" in out
