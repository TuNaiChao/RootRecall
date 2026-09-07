"""native 后端 · 巩固 / 持续学习(consolidate.py)。

干什么(面向小白)
  记忆的"夜间整理"——趁没人翻笔记本时,把里面重复的、打架的、过期的、该升级的梳理一遍。
  对标 2026 业界共识:consolidation = keeps / merges / evicts(留 / 合 / 逐)三件事。

五个 pass(keeps + merges + evicts):
  ① 升级 mental_model(keeps):被召回≥N 次(access_count)的教训 → 升级为"稳定规则"(借 Letta 3+ 规则)。
  ② 矛盾检测(只标不裁):同主题不同结论的 active 对 → 打 needs_review 标签 + 进统计上报。
     不自动选边(谁是正确根因是语义判断,踩坑#11:apply 过≠根因对);留给 memory-health-check skill /
     agent / 人裁决。检测本身是确定性的(_same_subject + not _same_conclusion)。
  ③ 语义近邻去重候选(merges):同 scope+同 kind,embedding cosine 超阈值 → 报候选合并对。
     默认只报不自动合(自动合并语义上危险,可能误合近义不同 bug;宁漏不错)。
  ④ 补丁已合入检测(Phase 2,只标不删):bug_lesson 带 fix_patch + repo_path 给得出 → git apply
     --check --reverse 通过 = 改动已在树里 → 打 merged_upstream 标签 + confidence 打折。
     **不 set_invalid**:补丁合入 ≠ 教训错了(这个 bug 真实发生过,考古查询"X 时点在不在"要靠它);
     且 reverse-check 只证"改动在树里",可能是等价修复而非本补丁 → 留人在环(确认后可手动 invalidate)。
  ⑤ stale 检测(Phase 2,只标不降权):超过 stale_after_days 没人翻(last_recalled/created_at 取晚者)
     → 打 stale 标签。**不改 confidence**——recall 已有 exp 时间衰减打分,consolidate 再降就是双杀;
     标签供 memory-health-check 预警 + agent 提示"这条很久没人验证过了"。

为什么不物理删 / 不 Weibull 物理降级
  bi-temporal 铁律:永不物理删(能回答"这 bug 在 X 时点还在不在",审计可追溯)。召回排序的时效
  由 recall 的 exp 衰减管;consolidate 只打标签/打折 confidence,不动生命周期。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from rootrecall.services.memory.backends.native.memorize import _norm, _same_conclusion, _same_subject
from rootrecall.services.memory.backends.native.store import MemoryStore
from rootrecall.services.memory.schema import Scope

logger = logging.getLogger(__name__)

# 矛盾检测考虑的最低置信度(低于此值的条目不参与"打架"判定,噪声不值得报)。
_CONTRADICTION_MIN_CONFIDENCE = 0.5
# 语义近邻去重:cosine 超此阈值才算"疑似重复"。保守(0.92),宁漏不错——误合两个不同 bug 比留两条重复更糟。
_DUPLICATE_COSINE_THRESHOLD = 0.92
# 近邻预筛的每条候选数:cosine 阈值前的 top-K 截断(路线图⑤,O(n²)→O(n·K))。
_NEIGHBOR_TOP_K = 50
# stale 判定的兜底天数(config 没配时用)。
_DEFAULT_STALE_AFTER_DAYS = 365.0
# 补丁已合入的 confidence 折扣(config 没配时用)。
_DEFAULT_MERGED_DISCOUNT = 0.5


def consolidate(
    scope: Scope,
    *,
    store: MemoryStore,
    promote_access_count: int = 3,
    detect_contradictions: bool = True,
    detect_duplicates: bool = True,
    duplicate_threshold: float = _DUPLICATE_COSINE_THRESHOLD,
    detect_merged_upstream: bool = True,
    detect_stale: bool = True,
    stale_after_days: float = _DEFAULT_STALE_AFTER_DAYS,
    merged_discount: float = _DEFAULT_MERGED_DISCOUNT,
    repo_path: str | None = None,
    now=None,
) -> dict[str, Any]:
    """巩固 pass:升级 + 矛盾检测 + 语义去重 + 补丁已合入 + stale 检测。返回统计 dict。

    返回:``{scanned, promoted, contradictions, duplicate_clusters, merged_upstream, stale}``。
      - contradictions:发现的"同主题不同结论"矛盾对数(已打 needs_review 标签)。
      - duplicate_clusters:疑似语义重复的条目组数(只统计上报,不自动合)。
      - merged_upstream:补丁改动已在仓里的条数(打 merged_upstream 标签 + confidence 打折)。
      - stale:超过 stale_after_days 没人翻的条数(打 stale 标签,只标不降权)。

    domain_knowledge 不参与升级/矛盾/去重(evergreen + 天然语义近邻);bug_lesson/codebase_fact
    都参与 stale 检测(任何知识都可能过期);merged_upstream 只对带 fix_patch 的 bug_lesson 生效。

    repo_path:B3 需要(补丁 reverse-apply 检测的 git 仓路径);None → 跳过 B3(不是错误,
    service 层不知道仓路径时正常)。now:测试注入时间用;None → 当前 UTC 时间。

    各 pass 幂等:重复跑不会叠加标签/重复上报(set 去重 + 已标的不再加/已打过折的不再折)。
    """
    if now is None:
        from datetime import UTC, datetime
        now = datetime.now(UTC)
    stats: dict[str, Any] = {
        "scanned": 0, "promoted": 0, "contradictions": 0, "duplicate_clusters": 0,
        "merged_upstream": 0, "stale": 0,
    }
    items = store.list_items(scope)
    stats["scanned"] = len(items)

    # ① 升级 mental_model(keeps)。
    for it in items:
        if it.kind not in ("mental_model", "domain_knowledge") and it.access_count >= promote_access_count:
            store.set_kind(it.id, "mental_model")
            stats["promoted"] += 1

    # ② 矛盾检测(只标不裁):同主题不同结论的 active 高 conf 对 → 打 needs_review 标签。
    if detect_contradictions:
        stats["contradictions"] = _detect_contradictions(store, items)

    # ③ 语义近邻去重候选(只报不合):同 kind 高 cosine 对 → 进统计。
    if detect_duplicates:
        stats["duplicate_clusters"] = _detect_semantic_duplicates(items, duplicate_threshold)

    # ④ 补丁已合入(只标不删):reverse-apply 通过 → merged_upstream 标签 + confidence 打折。
    if detect_merged_upstream and repo_path:
        stats["merged_upstream"] = _detect_merged_upstream(store, items, repo_path=repo_path, discount=merged_discount)

    # ⑤ stale 检测(只标不降权):超 stale_after_days 没人翻 → stale 标签。
    if detect_stale:
        stats["stale"] = _detect_stale(store, items, stale_after_days=stale_after_days, now=now)

    logger.info(
        "memory.consolidate(%s): 扫 %d,升级 %d,矛盾对 %d,重复簇 %d,已合入 %d,过期 %d",
        scope.codebase, stats["scanned"], stats["promoted"], stats["contradictions"],
        stats["duplicate_clusters"], stats["merged_upstream"], stats["stale"],
    )
    return stats


def _add_tag(store: MemoryStore, item_id: str, tag: str) -> None:
    """打标签:先重读 DB 最新 tags 再合并写回(幂等:已有则不动)。

    为什么不直接用调用方手里的 it.tags:consolidate 五个 pass 共享同一份 list_items
    快照,pass ④ 打的 merged_upstream 不在 pass ⑤ 拿到的快照里——直接 [*it.tags, "stale"]
    整体覆盖写会把前面的标签洗掉(e2e 真踩:合并标签被过期 pass 覆盖丢失)。
    """
    fresh = store.get(item_id)
    if fresh is None or tag in fresh.tags:
        return
    store.set_tags(item_id, [*fresh.tags, tag])


def _detect_contradictions(store: MemoryStore, items: list) -> int:
    """扫 active 高 conf 条目,找"同主题不同结论"的矛盾对 → 打 needs_review 标签。返回矛盾对数。

    只标不裁:不自动选边谁对(语义判断,踩坑#11),打标签让 memory-health-check skill 聚焦提示裁决。
    _same_subject / _same_conclusion 是确定性 helper(memorize.py 复用),判定结果可复现。
    幂等:已带 needs_review 标签的条目不再重复加(集合去重)。

    2026-09-07 提速(路线图⑤):全对两两 O(n²) 改**桶式预分组**再组内两两 —— 与暴力法
    语义等价(_same_subject 的两条路各有自己的桶,跨桶对不可能同主题):
    - 桶一(仅 bug_lesson):norm(symptom) 精确相等 —— 同桶内 _same_subject 恒真,只判结论;
    - 桶二:首证据文件(bug_lesson)/ kind_detail+首证据文件(codebase_fact)—— 组内仍做
      ±5 行窗判定,且跳过"双方 symptom 都非空"的对(那路只看 symptom,不看证据)。
    """
    # 只在 codebase_fact / bug_lesson 里找矛盾(这两类有"同主题不同结论"语义);domain_knowledge / mental_model 跳过。
    candidates = [
        it for it in items
        if it.kind in ("codebase_fact", "bug_lesson")
        and it.confidence >= _CONTRADICTION_MIN_CONFIDENCE
    ]
    by_symptom: dict[tuple, list] = {}
    by_file: dict[tuple, list] = {}
    for it in candidates:
        if it.kind == "bug_lesson":
            if it.symptom:
                by_symptom.setdefault(_norm(it.symptom), []).append(it)
            if it.evidence:
                by_file.setdefault(("bug", it.evidence[0].file), []).append(it)
        elif it.evidence:  # codebase_fact:_same_subject 还要求 kind_detail 相等 → 进桶键
            by_file.setdefault(("fact", it.kind_detail, it.evidence[0].file), []).append(it)

    def _flag(a, b) -> None:
        if not _same_conclusion(a, b):
            flagged.add(a.id)
            flagged.add(b.id)

    flagged: set[str] = set()
    for bucket in by_symptom.values():  # 同 symptom 桶:_same_subject 恒真
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                _flag(a, b)
    for bucket in by_file.values():  # 同文件桶:仍要过 _same_subject(行窗/回退语义)
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                if not (a.symptom and b.symptom) and _same_subject(a, b):
                    _flag(a, b)
    for item_id in flagged:
        _add_tag(store, item_id, "needs_review")
    return len(flagged)


def _union_find(parent: dict[str, str], x: str) -> str:
    """并查集 find(带路径压缩)。parent 是 {id: 父id};根的父是自己。

    放模块级(不嵌在循环里)——ruff B023:循环内定义的闭包不绑定循环变量,模块级函数无此问题。
    """
    root = x
    while parent[root] != root:
        root = parent[root]
    # 路径压缩:把链上的点都指到根。
    while parent[x] != root:
        parent[x], x = root, parent[x]
    return root


def _count_duplicate_clusters(group: list, threshold: float) -> int:
    """一组同 kind 条目里,互相 embedding cosine≥threshold 的并成簇。返回 size≥2 的簇数。

    2026-09-07 提速(路线图⑤):全对两两 Python cosine 改**分块矩阵 top-K 预筛** ——
    归一化后点积即 cosine,每条只保留 top-K 近邻再判阈值,O(n²) 比较变 O(n·K) 进并查集。
    - 混合维度(换过 embedding model 的存量库)按维度分桶:跨维 cosine 恒 -1(旧 _cosine
      语义)不可能超阈值 → 直接不比;
    - 零向量归一化后保持零 → 与谁都不超阈值,同旧语义;
    - 极端情况:簇成员 > K 时预筛可能漏簇内远端边(每条只看 top-K)——「只报不合、宁漏
      不错」的本 pass 可接受;现实簇(真重复)远小于 K。
    与暴力版对拍一致的边界:簇 ≤ K 时结果逐簇相同(单测锁)。
    """
    import numpy as np

    by_dim: dict[int, list] = {}
    for it in group:
        by_dim.setdefault(len(it.embedding), []).append(it)
    parent = {it.id: it.id for it in group}
    for bucket in by_dim.values():
        n = len(bucket)
        if n < 2:
            continue
        m = np.asarray([it.embedding for it in bucket], dtype=np.float64)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        m = m / norms
        k = min(_NEIGHBOR_TOP_K, n)
        block = 512  # 分块控内存:sims 每次只有 block×n(n=10万 时约 200MB float64)
        for s in range(0, n, block):
            sims = m[s:s + block] @ m.T
            for r in range(sims.shape[0]):
                i = s + r
                sims[r, i] = -1.0  # 排除自身
                top = np.argpartition(-sims[r], k - 1)[:k]
                for j in top:
                    if sims[r, j] >= threshold:
                        ra, rb = _union_find(parent, bucket[i].id), _union_find(parent, bucket[j].id)
                        if ra != rb:
                            parent[ra] = rb
    # 按 root 聚合,数 size≥2 的簇。
    sizes: dict[str, int] = {}
    for it in group:
        sizes[_union_find(parent, it.id)] = sizes.get(_union_find(parent, it.id), 0) + 1
    return sum(1 for c in sizes.values() if c >= 2)


def _detect_semantic_duplicates(items: list, threshold: float) -> int:
    """扫同 kind 条目,找 embedding cosine 超阈值的疑似重复簇。返回簇数。

    只报不合(自动合并语义上危险:近义不同 bug 可能被误合;宁漏不错)。
    跳过 domain_knowledge(领域知识天然语义近邻,如多条都讲 4-way handshake 是正常的)。
    按 kind 分组(跨 kind 不比),每组用并查集并成簇,返回 size≥2 的簇数。
    """
    # 按 kind 分组(domain_knowledge 跳过);只看有 embedding 的。
    groups: dict[str, list] = {}
    for it in items:
        if it.kind == "domain_knowledge" or not it.embedding:
            continue
        groups.setdefault(it.kind, []).append(it)

    return sum(_count_duplicate_clusters(g, threshold) for g in groups.values() if len(g) >= 2)


def _reverse_applies(diff_text: str, repo_path: str) -> bool | None:
    """补丁能否对仓"反向 apply"(= 改动已经在树里)。True=已合入;False=没合入;None=判不了。

    姿势同 merge_eval 的 git apply --check(踩坑#15:归一化行尾,防 marshalling 假阴)。
    判不了(非 git 仓 / git 挂 / 补丁格式烂)返 None,调用方跳过不误判。
    """
    try:
        diff = diff_text.replace("\r\n", "\n").replace("\r", "\n")
        if not diff.endswith("\n"):
            diff += "\n"
        r = subprocess.run(
            ["git", "apply", "--check", "--reverse"],
            input=diff, cwd=str(repo_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        if r.returncode == 0:
            return True
        # returncode!=0 有两种:补丁没合入(正常态)or 仓状态特殊。区分不了就都当"没合入"——
        # 误报 merged 的代价(错降权)比漏报大,保守取 False。
        return False
    except (OSError, subprocess.SubprocessError):
        return None


def _detect_merged_upstream(store: MemoryStore, items: list, *, repo_path: str, discount: float) -> int:
    """B3:bug_lesson 带 fix_patch 的条目,git apply --check --reverse 通过 = 改动已在树里。

    只标不删:打 merged_upstream 标签 + confidence 打折(×discount)。**不 set_invalid**:
    补丁合入 ≠ 教训错了(考古"X 时点在不在"要靠它);且 reverse 只证"改动在树里",
    可能是等价修复 → 留人在环(用户/agent 确认后可手动 invalidate)。

    计数=当前态(含之前已标的;健康体检要的是"总共有几条已合入"),写入=幂等
    (已带标签的不再重复打折,防 confidence 被反复折到 0)。git 检查对已标条目也跳过
    (结论已知,省 subprocess)。
    """
    count = 0
    for it in items:
        if it.kind != "bug_lesson" or not it.fix_patch:
            continue
        if "merged_upstream" in it.tags:
            count += 1  # 之前已判过已合入:计入总数,跳过 git 检查和重复打折
            continue
        verdict = _reverse_applies(it.fix_patch, repo_path)
        if verdict is True:
            _add_tag(store, it.id, "merged_upstream")
            store.set_confidence(it.id, it.confidence * discount)
            count += 1
    return count


def _detect_stale(store: MemoryStore, items: list, *, stale_after_days: float, now) -> int:
    """B4(stale):超过 stale_after_days 没人翻的条目 → 打 stale 标签。返回条数。

    "最后活动"取 last_recalled(最近一次被召回)与 created_at(建卡)的较晚者 ——
    从没被召回过的卡,建卡那天就是它最后被"确认新鲜"的日子。
    只标不降权:recall 已有 exp 时间衰减打分,consolidate 再降就是双杀;标签供
    memory-health-check 预警 + agent 注入时提示"这条很久没人验证过了"。

    计数=当前态(含已标过的;重复跑统计稳定),写入=幂等(标签只加一次)。
    """
    count = 0
    for it in items:
        last = it.last_recalled or it.created_at
        age_days = (now - last).total_seconds() / 86400.0
        if age_days < stale_after_days:
            continue
        count += 1  # 匹配即计数(含已标的)
        _add_tag(store, it.id, "stale")
    return count
