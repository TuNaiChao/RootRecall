"""仓库注册表(repo registry)—— 把「索引名 ↔ 仓库路径 ↔ 角色 ↔ 生命周期」串起来。

为什么需要它(面向小白)
------------------------
在此之前,`rootrecall index <路径> <索引名>` 建完索引后,**索引名和仓库路径的映射只存在
于人脑和对话上下文**:manifest 只记 commit + 文件 sha256,不记路径;MCP 工具要仓库工作树
时全靠 agent 每次显式传绝对路径,compare skill 甚至专门教 agent「打不开就问用户要路径」。

注册表就是补上这一环的本地清单(`data/repos.yaml`,随机型走不进 git):

- **索引名 → 仓库路径反查**:MCP 工具 / skill 拿名字就能解析出路径,不再问用户。
- **角色(role)驱动的生命周期**:`baseline`(共享基线,永久保留 + 定时同步)vs
  `ephemeral`(某个 bug 的一次性检出,分析完 `repo gc` 级联清理)vs `unmanaged`
  (ensure_repo 顺手 clone 的样机、手动 index 未声明角色的仓 —— gc 不碰)。
- **bug 关联**:ephemeral 记 `bug_id` / `from_repo`(从哪条基线开的 worktree),
  gc 时连索引带结构图一起回收,教训留在记忆库(记忆本来就该跨版本沉淀)。

设计对齐 Android repo manifest 的 revision/role 思想 + Serena 的「全局配置 + 每项目数据」
分层;文件即真相(plain YAML),人可读可手改,坏了删掉重建即可(索引/镜像都还在)。
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

ROLE_BASELINE = "baseline"
ROLE_EPHEMERAL = "ephemeral"
ROLE_UNMANAGED = "unmanaged"
_ROLES = (ROLE_BASELINE, ROLE_EPHEMERAL, ROLE_UNMANAGED)

# 注册表落点:env 覆盖 > data 根下 repos.yaml。锚到安装根(与 data/ 其余同源),
# 不随调用方 cwd 漂(MCP server 虽 chdir 到根,CLI 可能从任意目录调)。
_ENV_REGISTRY = "ROOTRECALL_REPOS_FILE"

# data 落点根:env 覆盖 > <安装根>/data。设 ROOTRECALL_HOME 可把全部数据(注册表/镜像/
# worktree/索引/结构图/记忆/报告)迁出仓库克隆 —— git pull 升级不碰数据,多任务并行也不挤。
_ENV_DATA_HOME = "ROOTRECALL_HOME"


def data_root() -> Path:
    """data 落点根:``ROOTRECALL_HOME``(非空)优先,否则 ``<安装根>/data``。

    单一真相 —— registry / mirror / resolver / memory / 检索 / 交付物全部经它取 data 子目录,
    测试 monkeypatch 本模块 ``_install_root`` 即可整体改锚(未设 env 时回落走它)。
    """
    p = (os.environ.get(_ENV_DATA_HOME) or "").strip()
    return Path(p).expanduser() if p else _install_root() / "data"


def reanchor_data_path(p: str | Path) -> Path:
    """config/默认参数里 ``data/`` 前缀的**相对**路径,在 ROOTRECALL_HOME 设置时改锚到新家
    (去掉 ``data/`` 段,如 ``data/memory`` → ``$HOME/memory``);其余一律原样 —— **env 未设时
    零行为变化**(这是向后兼容的关键:不设就完全等于现状,老接线/老测试不受扰)。

    绝对路径 / 不带 ``data/`` 前缀的相对路径(用户显式自定)不搬,尊重用户选择。
    """
    s = str(p)
    if not (os.environ.get(_ENV_DATA_HOME) or "").strip():
        return Path(p)
    if Path(s).is_absolute() or not s.startswith("data/"):
        return Path(p)
    return data_root() / s[len("data/"):]


def registry_path() -> Path:
    if p := os.environ.get(_ENV_REGISTRY):
        return Path(p).expanduser()
    return data_root() / "repos.yaml"


@dataclass
class RepoRecord:
    """一个本地受管仓库/检出。字段全部可选(除 name),人可手改 YAML 补齐。"""

    name: str                       # 注册名(= 索引名约定;唯一键)
    path: str = ""                  # 工作树绝对路径(baseline 常驻检出 / ephemeral worktree)
    url: str | None = None          # git remote(再 clone / sync fetch 用)
    role: str = ROLE_UNMANAGED      # baseline | ephemeral | unmanaged
    branch: str | None = None       # 锁定的分支/tag(baseline 基线跟随;ephemeral 锁小版本)
    mirror: str | None = None       # bare 镜像路径(repo checkout/sync 体系;普通注册为空)
    from_repo: str | None = None    # ephemeral:从哪条基线开的(bare 仓名)
    bug_id: str | None = None       # ephemeral:关联 bug 标识(gc 报告里给人看)
    codebase: str | None = None     # 检索索引名(与 name 不同才填;None = 同 name)
    created_at: str | None = None   # ISO 日期(gc 判龄用;手写 '2026-08-19' 即可)
    last_synced_at: str | None = None  # repo sync 最后成功时间(报告用)
    synced_sha: str | None = None   # repo sync 上次对账的上游 sha(下次 --analyze 的范围底)
    note: str | None = None

    @property
    def index_name(self) -> str:
        """检索工具用的索引名(默认 = name;显式 codebase 覆盖)。"""
        return self.codebase or self.name

    def exists_on_disk(self) -> bool:
        return bool(self.path) and Path(self.path).is_dir()


class RepoRegistry:
    """data/repos.yaml 的读写门面。load 惰性、save 原子写(tmp + replace),并发安全够单机用。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else registry_path()
        self._records: dict[str, RepoRecord] | None = None

    # ── 读写 ────────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, RepoRecord]:
        if self._records is not None:
            return self._records
        records: dict[str, RepoRecord] = {}
        if self.path.exists():
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            for name, ent in (raw.get("repos") or {}).items():
                if not isinstance(ent, dict):
                    continue
                known = {f.name for f in fields(RepoRecord)}
                ent.pop("name", None)  # save 落盘带了 name;构造参数已显式给,防重复
                ent = {k: v for k, v in ent.items() if k in known}
                rec = RepoRecord(name=str(name), **ent)
                if rec.role not in _ROLES:
                    rec.role = ROLE_UNMANAGED
                records[rec.name] = rec
        self._records = records
        return records

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"repos": {n: asdict(r) for n, r in sorted(self._load().items())}}
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── CRUD ────────────────────────────────────────────────────────────────

    def register(
        self, name: str, *, path: str | None = None, url: str | None = None,
        role: str | None = None, branch: str | None = None, mirror: str | None = None,
        from_repo: str | None = None, bug_id: str | None = None, codebase: str | None = None,
        created_at: str | None = None, note: str | None = None,
        last_synced_at: str | None = None, synced_sha: str | None = None,
        save: bool = True,
    ) -> RepoRecord:
        """登记/更新一条(upsert):已有同名记录的未传字段保留现值(改 role/path 不丢 url)。

        role=None = 保留现值(新记录才落 unmanaged)—— 防止补字段时把 ephemeral/baseline
        悄悄降级成 unmanaged(gc/sync 就看不见它了)。
        """
        recs = self._load()
        old = recs.get(name)
        if role is None:
            role = old.role if old else ROLE_UNMANAGED
        if role not in _ROLES:
            raise ValueError(f"role 必须是 {'/'.join(_ROLES)} 之一,拿到: {role!r}")
        rec = RepoRecord(
            name=name,
            path=str(path) if path else (old.path if old else ""),
            url=url if url is not None else (old.url if old else None),
            role=role,
            branch=branch if branch is not None else (old.branch if old else None),
            mirror=mirror if mirror is not None else (old.mirror if old else None),
            from_repo=from_repo if from_repo is not None else (old.from_repo if old else None),
            bug_id=bug_id if bug_id is not None else (old.bug_id if old else None),
            codebase=codebase if codebase is not None else (old.codebase if old else None),
            created_at=created_at or (old.created_at if old else _today()),
            last_synced_at=last_synced_at or (old.last_synced_at if old else None),
            synced_sha=synced_sha or (old.synced_sha if old else None),
            note=note if note is not None else (old.note if old else None),
        )
        recs[name] = rec
        if save:
            self.save()
        return rec

    def get(self, name: str) -> RepoRecord | None:
        return self._load().get(name)

    def remove(self, name: str, *, save: bool = True) -> RepoRecord | None:
        rec = self._load().pop(name, None)
        if rec is not None and save:
            self.save()
        return rec

    def list(self) -> list[RepoRecord]:
        return sorted(self._load().values(), key=lambda r: (r.role, r.name))

    # ── 模糊查找(agent 用:「bluez 5.50.61」→ 候选仓)────────────────────────

    def find(self, project: str, version: str | None = None, *, role: str | None = None) -> list[RepoRecord]:
        """按 项目名(+可选版本号) 模糊匹配注册名/分支/url。

        规则(宽松优先,宁可多给候选让 agent 挑):
          - project 记号(按 -_. 切词)至少一个出现在 name(或 url 末段)里;有 version 时
            version 串还须出现在 name/branch 里;
          - 两者都裸匹配不上时,退化为「project 子串包含」,仍按精确度排序。
        """
        p_tokens = {t for t in re.split(r"[-_.]", project.lower()) if len(t) >= 2}
        v = (version or "").lower().strip()

        def exact(rec: RepoRecord) -> bool:
            hay = f"{rec.name} {rec.branch or ''}".lower()
            hay_url = (rec.url or "").rstrip("/").rsplit("/", 1)[-1].lower().removesuffix(".git")
            has_p = any(t in rec.name.lower() or t in hay_url for t in p_tokens)
            return has_p and (not v or v in hay or v in hay_url)

        def loose(rec: RepoRecord) -> bool:
            hay = f"{rec.name} {rec.branch or ''} {rec.url or ''}".lower()
            return project.lower() in hay

        pool = [r for r in self._load().values() if role is None or r.role == role]
        hits = [r for r in pool if exact(r)] or [r for r in pool if loose(r)]
        return sorted(hits, key=lambda r: (r.role != ROLE_BASELINE, r.name))


# ── 名字 → 本地路径反查(ensure_repo / MCP repo_path 参数共用)────────────────


def resolve_repo_path(
    name_or_path: str, *, registry: RepoRegistry | None = None,
    code_index_dir: Path | str | None = None, clone_dir: Path | str | None = None,
) -> tuple[Path | None, str]:
    """把「名字或路径」解析成本地工作树,返回 (绝对路径, 命中来源)。

    解析链(先专后泛,全部零网络):
      1. 路径样输入(绝对或含 /)且目录存在 → 直接用(与 ensure_repo 第 1 步同判)。
      2. 注册表命中(data/repos.yaml)且 path 在盘上 → 用注册表。
      3. 索引清单反查:data/code_index/<名>/index_manifest.json 的 repo_path(F1 起记录)。
      4. ensure_repo 老落点:data/repos/<短名> 已存在 → 用它(兼容旧 auto-clone)。
    都没有 → (None, 说明),调用方决定问用户 / ensure_repo clone / 报错。
    """
    p = Path(name_or_path).expanduser()
    looks_like_path = p.is_absolute() or ("/" in name_or_path) or ("\\" in name_or_path)
    if looks_like_path and p.is_dir():
        return p.resolve(), "local-path"

    name = name_or_path.strip()
    reg = registry or RepoRegistry()
    if (rec := reg.get(name)) and rec.exists_on_disk():
        return Path(rec.path).resolve(), f"registry[{rec.role}]"

    idx_dir = Path(code_index_dir) if code_index_dir else _default_index_dir()
    mf = idx_dir / name / "index_manifest.json"
    if mf.exists():
        try:
            import json
            data = json.loads(mf.read_text(encoding="utf-8"))
            if rp := data.get("repo_path"):
                if Path(rp).is_dir():
                    return Path(rp).resolve(), "index-manifest"
        except (json.JSONDecodeError, OSError):
            pass

    from rootrecall.services.repos.resolver import repo_name

    cd = Path(clone_dir) if clone_dir else _default_clone_dir()
    dest = cd / repo_name(name)
    if dest.is_dir():
        return dest.resolve(), "clone-dir"

    return None, f"未注册且不在本地:{name}(先 rootrecall repo register / ensure_repo / rootrecall index)"


def _install_root() -> Path:
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _default_index_dir() -> Path:
    return data_root() / "code_index"


def _default_clone_dir() -> Path:
    return data_root() / "repos"


def known_codebases() -> dict[str, set[str]]:
    """本机全部已知代码库名 → 证据来源集({"registry","index","graph"} 的子集)。

    三源并集:注册表(baseline add / checkout 落的名)+ 向量索引清单
    (data/code_index/<名>/index_manifest.json)+ 结构图目录(data/structgraph/<名>/graph.db)。
    MCP 工具层拿它做 codebase 近义名容错(名字纠偏 / 候选列举 / 「没建索引 vs 没建图」
    区分报错)。任一源缺失/损坏 → 跳过该源,绝不让列名单这件事挡了工具主链路。
    """
    out: dict[str, set[str]] = {}

    def _add(name: str, src: str) -> None:
        if name and name not in (".", ".."):
            out.setdefault(name, set()).add(src)

    try:
        for rec in RepoRegistry().list():
            _add(rec.name, "registry")
    except Exception:  # noqa: BLE001 —— repos.yaml 坏 → 只剩索引/图两源,照样列
        pass
    for root, marker, src in (
        (_default_index_dir(), "index_manifest.json", "index"),
        (data_root() / "structgraph", "graph.db", "graph"),
    ):
        try:
            if root.is_dir():
                for sub in root.iterdir():
                    if (sub / marker).exists():
                        _add(sub.name, src)
        except OSError:
            pass
    return out


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


# ── gc:ephemeral 生命周期回收(F3)─────────────────────────────────────────────


def gc_ephemeral(
    *,
    max_age_days: int = 14,
    dry_run: bool = False,
    names: list[str] | None = None,
    registry: RepoRegistry | None = None,
    code_index_dir: Path | str | None = None,
    structgraph_dir: Path | str | None = None,
    today: str | None = None,
) -> dict:
    """回收过期 ephemeral 仓:**级联删**工作树 + 它的向量索引 + 结构图 + 注册记录。

    - 判龄:record.created_at(ISO 日期)距今 > max_age_days;缺 created_at 视为 0 天(不删,
      保守 —— 只能靠 names 点名强删)。
    - baseline / unmanaged 一律不碰(共享基线是永久资产;unmanaged 是 ensure_repo 样机)。
    - 级联:worktree(remove_worktree,带镜像簿记清理;没镜像就 rmtree)→ data/code_index/
      <index_name> → data/structgraph/<index_name> → 注册记录。**记忆不删** —— bug 教训
      本来就该跨版本沉淀。
    - 顺带产出孤儿索引报告(code_index 下既没注册、manifest repo_path 又不在盘上的目录),
      只报告不动手;要动手删传 prune_orphans(单独 CLI 旗标,不与 gc 混)。
    """
    import datetime
    import shutil as _shutil

    from rootrecall.services.repos import mirror as _mirror_mod

    reg = registry or RepoRegistry()
    today = today or _today()
    idx_root = Path(code_index_dir) if code_index_dir else _default_index_dir()
    sg_root = Path(structgraph_dir) if structgraph_dir else data_root() / "structgraph"

    def _age_days(rec: RepoRecord) -> int | None:
        if not rec.created_at:
            return 0
        try:
            return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(rec.created_at)).days
        except ValueError:
            return 0  # 手改坏日期 → 当新仓,不删

    report: dict = {"removed": [], "kept_young": [], "orphan_indexes": [], "dry_run": dry_run}

    for rec in reg.list():
        if rec.role != ROLE_EPHEMERAL:
            continue
        if names is not None and rec.name not in names:
            continue
        age = _age_days(rec)
        if names is None and (age is None or age < max_age_days):
            report["kept_young"].append({"name": rec.name, "age_days": age})
            continue

        cascades: list[str] = []
        wt = Path(rec.path) if rec.path else None
        mp = Path(rec.mirror) if rec.mirror else None
        if not dry_run:
            if wt is not None and str(wt) != "":
                _mirror_mod.remove_worktree(wt, mirror=mp if mp and mp.is_dir() else None)
                cascades.append(f"worktree:{wt}")
            for d in (idx_root / rec.index_name, sg_root / rec.index_name):
                if d.exists():
                    _shutil.rmtree(d, ignore_errors=True)
                    cascades.append(f"index:{d}")
            reg.remove(rec.name)
        else:
            cascades.append(f"worktree:{wt}" if wt else "worktree:-")
            cascades += [f"index:{d}" for d in (idx_root / rec.index_name, sg_root / rec.index_name) if d.exists()]
        report["removed"].append({"name": rec.name, "bug": rec.bug_id, "age_days": age, "cascades": cascades})

    # 孤儿索引报告:只报「manifest 记了 repo_path 且该路径已不在盘上」的(可安全删)。
    # F1 之前的老索引没有 repo_path —— 源仓多半还在(只是没登记),删了误伤,单列 legacy 只提示;
    # 要纳入管理:对源仓重跑一次 index(manifest 即记 repo_path)。
    if idx_root.is_dir():
        registered = {r.index_name for r in reg.list()}
        for d in sorted(idx_root.iterdir()):
            if not d.is_dir() or d.name in registered:
                continue
            mf = d / "index_manifest.json"
            if not mf.exists():
                continue  # 从没建过索引的空目录,不噪声
            try:
                rp = (yaml.safe_load(mf.read_text(encoding="utf-8")) or {}).get("repo_path")
            except Exception:  # noqa: BLE001 —— 坏 manifest 视作源不明 → legacy,不删
                rp = None
            if rp is None:
                report.setdefault("legacy_indexes", []).append(str(d))
            elif not Path(rp).is_dir():
                report["orphan_indexes"].append(str(d))
    report.setdefault("legacy_indexes", [])
    return report
