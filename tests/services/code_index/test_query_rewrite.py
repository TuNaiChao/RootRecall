"""查询改写(⑧ HyDE-lite)单测:默认旁路 / 开启改写+缓存 / 失败降级。"""

from __future__ import annotations

from types import SimpleNamespace

from rootrecall.services.code_index.retrieval import _REWRITE_CACHE, _rewrite_query, rewrite_enabled


class _StubModel:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return SimpleNamespace(content=self._text)


def test_default_off_passthrough(monkeypatch):
    monkeypatch.delenv("ROOTRECALL_QUERY_REWRITE", raising=False)
    assert not rewrite_enabled()
    monkeypatch.setattr("rootrecall.platform.models.create_chat_model",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("默认关不该碰模型")))
    assert _rewrite_query("为什么断开") == "为什么断开"


def test_rewrite_on_with_cache(monkeypatch):
    monkeypatch.setenv("ROOTRECALL_QUERY_REWRITE", "1")
    assert rewrite_enabled()
    _REWRITE_CACHE.clear()
    model = _StubModel("bluetooth disconnect reason a2dp_abort")
    monkeypatch.setattr("rootrecall.platform.models.create_chat_model", lambda *a, **k: model)
    assert _rewrite_query("蓝牙为什么断开") == "bluetooth disconnect reason a2dp_abort"
    assert _rewrite_query("蓝牙为什么断开") == "bluetooth disconnect reason a2dp_abort"
    assert model.calls == 1  # 进程内缓存:同查询第二次不再调 LLM
    _REWRITE_CACHE.clear()


def test_rewrite_failure_falls_back(monkeypatch):
    monkeypatch.setenv("ROOTRECALL_QUERY_REWRITE", "true")

    def _boom(*a, **k):
        raise RuntimeError("no api key")
    monkeypatch.setattr("rootrecall.platform.models.create_chat_model", _boom)
    assert _rewrite_query("蓝牙为什么断开") == "蓝牙为什么断开"  # 零 key → 原查询照常检索


def test_rewrite_chatty_model_guard(monkeypatch):
    monkeypatch.setenv("ROOTRECALL_QUERY_REWRITE", "1")
    _REWRITE_CACHE.clear()
    model = _StubModel("好的,以下是改写结果:" + "啰嗦" * 400)
    monkeypatch.setattr("rootrecall.platform.models.create_chat_model", lambda *a, **k: model)
    assert _rewrite_query("蓝牙为什么断开") == "蓝牙为什么断开"  # 话痨防御:超长改写丢弃
    _REWRITE_CACHE.clear()
