"""baseline add 端到端:一条命令 = 登记 baseline(git url/branch 自动读)+ 建索引。

覆盖(CLI 瘦身,2026-08-21):总目录倒序自动命名(v20/bluez → bluez-v20、systemd → systemd)、
--name 覆盖、总目录外退目录名并提示、非 git 仓拒绝、索引真实落地(假 embedder)、幂等重跑
upsert 不重嵌、baseline ls 复用 repo 家族。
隔离:ROOTRECALL_HOME→tmp(数据落点)、ROOTRECALL_REPOS_FILE→tmp(conftest autouse)、chdir tmp。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import subprocess

import numpy as np
import pytest

import rootrecall.services.code_index.embed as embed_mod
from rootrecall.cli import main as cli_main

_EMBEDS = {"n": 0}


class _CountingEmbedder:
    """假 embedder:确定性向量 + 全局计数(幂等断言:重跑零重嵌)。"""

    @property
    def fingerprint(self) -> str:
        return "fake-embedder-v1"

    def embed_chunks(self, chunks):
        _EMBEDS["n"] += len(chunks)
        return np.stack([self._vec(c.id) for c in chunks])

    def embed_query(self, query: str) -> np.ndarray:
        return self._vec(query)

    @staticmethod
    def _vec(key: str) -> np.ndarray:
        h = hashlib.sha256(key.encode()).digest()
        v = np.frombuffer(h[:8], dtype=np.uint8).astype(np.float32)
        return v / (np.linalg.norm(v) + 1e-9)


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _mk_repo(path, url: str = "https://example.com/rem.git"):
    """造一个小 git 仓(1 个 .c + origin remote + main 分支)。"""
    path.mkdir(parents=True)
    (path / "a.c").write_text("int fa(void) { return 1; }\n", encoding="utf-8")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    if url:
        _git(path, "remote", "add", "origin", url)
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(embed_mod, "create_embedder", lambda cfg: _CountingEmbedder())
    monkeypatch.setenv("ROOTRECALL_HOME", str(tmp_path / "home"))  # 数据落点绝对化,chdir 无副作用
    cb = tmp_path / "codebases"
    monkeypatch.setenv("ROOTRECALL_CODEBASES", str(cb))
    _EMBEDS["n"] = 0
    return {"cb": cb, "tmp": tmp_path, "home": tmp_path / "home"}


def _run(*argv: str):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        rc = cli_main(list(argv))
    return rc, out.getvalue() + err.getvalue()


def test_nested_and_direct_autonaming(env):
    """v20/bluez → bluez-v20(倒序连 '-');systemd → systemd;登记字段与索引落地全对。"""
    from rootrecall.services.repos.registry import RepoRegistry

    v20 = _mk_repo(env["cb"] / "v20" / "bluez")
    systemd = _mk_repo(env["cb"] / "systemd")

    rc, out = _run("baseline", "add", str(v20), "--no-graph")
    assert rc == 0, out
    assert "bluez-v20" in out and "role=baseline" in out

    rc, out2 = _run("baseline", "add", str(systemd), "--no-graph")
    assert rc == 0, out2

    reg = RepoRegistry()
    for name in ("bluez-v20", "systemd"):
        rec = reg.get(name)
        assert rec is not None, name
        assert rec.role == "baseline"
        assert rec.url == "https://example.com/rem.git"  # git remote 自动读
        assert rec.branch == "main"                      # 当前分支自动读
        assert rec.index_name == name                    # 注册名=索引名,单一命名
    # 索引真实落地:向量库在新家(ROOTRECALL_HOME 剥 data/ 前缀语义)
    vec = env["home"] / "code_index"
    assert (vec / "bluez-v20").exists() and (vec / "systemd").exists()
    assert _EMBEDS["n"] > 0


def test_baseline_ls_reuses_repo_family(env):
    """baseline ls 是 repo ls 的换名转发 —— 列出刚建的基线。"""
    v20 = _mk_repo(env["cb"] / "v20" / "bluez")
    assert _run("baseline", "add", str(v20), "--no-graph")[0] == 0
    rc, out = _run("baseline", "ls")
    assert rc == 0
    assert "bluez-v20" in out and "baseline" in out


def test_name_override_and_outside_root(env):
    """--name 覆盖默认名;总目录外退目录名并提示。"""
    outside = _mk_repo(env["tmp"] / "elsewhere" / "bluez")

    rc, out = _run("baseline", "add", str(outside), "--no-graph")
    assert rc == 0
    assert "bluez" in out and "不在总目录" in out  # 退目录名 + 提示

    rc, out = _run("baseline", "add", str(outside), "--name", "bluez-custom", "--no-graph")
    assert rc == 0
    from rootrecall.services.repos.registry import RepoRegistry

    assert RepoRegistry().get("bluez-custom") is not None


def test_not_git_or_missing_rejected(env):
    """非 git 仓拒绝(rc=2,指路);路径不存在 rc=1;两者都不登记。"""
    plain = env["tmp"] / "plain"
    plain.mkdir()
    (plain / "a.c").write_text("int x;\n", encoding="utf-8")

    rc, out = _run("baseline", "add", str(plain))
    assert rc == 2 and "不是 git 仓" in out

    rc, _ = _run("baseline", "add", str(env["tmp"] / "nope"))
    assert rc == 1

    from rootrecall.services.repos.registry import RepoRegistry

    assert RepoRegistry().list() == []


def test_idempotent_rerun_upsert_no_reembed(env):
    """重跑同名 baseline add = upsert 登记(提示已存在)+ 索引零重嵌。"""
    v20 = _mk_repo(env["cb"] / "v20" / "bluez")
    assert _run("baseline", "add", str(v20), "--no-graph")[0] == 0
    first = _EMBEDS["n"]
    assert first > 0

    rc, out = _run("baseline", "add", str(v20), "--no-graph")
    assert rc == 0
    assert "已存在" in out
    assert _EMBEDS["n"] == first  # manifest 无变化 → 零重嵌


def test_stale_rootrecall_home_friendly_error(env, monkeypatch):
    """跨机拷 .env 的坑:ROOTRECALL_HOME 指向不可建路径(旧机家目录/文件占位)→
    友好指路报错(rc=1),不再甩深层 PermissionError traceback。"""
    blocker = env["tmp"] / "home-file"
    blocker.write_text("占位文件", encoding="utf-8")
    monkeypatch.setenv("ROOTRECALL_HOME", str(blocker / "share" / "rootrecall"))

    v20 = _mk_repo(env["cb"] / "v20" / "bluez")
    rc, out = _run("baseline", "add", str(v20), "--no-graph")
    assert rc == 1
    assert "ROOTRECALL_HOME" in out and "别的机器" in out
    assert "Traceback" not in out
