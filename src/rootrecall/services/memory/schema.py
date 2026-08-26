"""记忆核心 · 数据模型(R1 schema.py)。

这是什么
--------
记忆核心存的"一条知识"长什么样。普通 coding agent 每开新会话都失忆;RootRecall 把
"这个库长啥样 / 之前哪些 bug 怎么修的"沉淀成可检索、带溯源、能持续学习的记忆。
一条记忆就是一个 KnowledgeItem(下面定义)。

四类知识项(用 kind 区分,同一张表存):
  - codebase_fact   :P1 代码仓调研产出 —— "这个模块/符号/架构是干啥的、关键设计"。
  - bug_lesson      :P2 bug-RCA 产出 —— "这个 bug 根因是啥、怎么修的、影响面多大"。
  - mental_model    :巩固升级出的"稳定规则" —— 反复出现(被召回≥N 次)的教训固化成规律。
  - domain_knowledge:领域/项目知识 —— 协议语义、wpa 各层职责这类"领域常理"
                    (类比 agent memory 的 semantic memory 语义记忆)。和前三类的区别:
                    ① 锚 source_url 溯源(网调来的)而非 file:line;② 不自动升级 mental_model
                    (领域常理 evergreen,不像 bug 教训会"毕业"成程序性规则);③ 进 recall 后
                    给 bug-RCA 多一层证伪依据(治踩坑#11:误诊成显眼日志行时,协议语义能纠偏)。

为什么这么设计(对齐参考实现)
  - bi-temporal(valid_at/invalid_at):借 graphiti —— 矛盾的旧知识"失效"而非"删除",
    能回答"这个 bug 在 X 时点还存不存在"(系统考古的关键)。
  - 溯源 commit_sha + evidence[file:line]:每条结论都能追到具体代码状态/行(报告签名)。
  - source_tier + confidence:借 mnemopi veracity —— 不同来源可信度不同(委托 agent
    产出最可信,工具检索最低),合并时按档加权。
  - access_count:借 Letta —— 被引用次数比单纯时间更能判断该不该"升级为长期"。

对应:deer-flow MemoryManager 的 fact 字段 + mnemopi ConsolidatedFact + graphiti EntityEdge。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ──────────────────────────────────────────────────────────────────────────
# §1 来源可信度档(借 mnemopi VERACITY_WEIGHTS)
# ──────────────────────────────────────────────────────────────────────────


class SourceTier(StrEnum):
    """知识项来自哪里,决定合并时的"可信度权重"。

    类比:同样一句话,"委托侦探当面查到的"比"我瞎猜的"可信。合并重复知识时,
    按这个档给权重(mnemopi 的 stated=1.0 / inferred=0.7 / tool=0.5 那套)。
    """

    delegate = "delegate"  # 委托 agent(omp/opencode)产出 —— 最可信
    stated = "stated"  # 人/报告明确陈述 —— 最可信
    inferred = "inferred"  # LLM 推断 —— 中等
    imported = "imported"  # 外部导入 —— 中等
    unknown = "unknown"  # 未知来源 —— 中等偏高(保守)
    tool = "tool"  # 工具/检索得来 —— 最低(可能是噪声)


# 各档权重(用于 Bayes 置信度累加,见 consolidate.py)。借 mnemopi VERACITY_WEIGHTS。
TIER_WEIGHT: dict[SourceTier, float] = {
    SourceTier.delegate: 1.0,
    SourceTier.stated: 1.0,
    SourceTier.inferred: 0.7,
    SourceTier.imported: 0.6,
    SourceTier.unknown: 0.8,
    SourceTier.tool: 0.5,
}


# ──────────────────────────────────────────────────────────────────────────
# §2 Scope:租户隔离(谁管的哪个库)
# ──────────────────────────────────────────────────────────────────────────


class Scope(BaseModel):
    """记忆空间隔离 = (owner, codebase)。

    类比:每个侦探有自己的笔记本,还按"案件"分册。owner=谁,codebase=哪个库。
    v1 单机单 owner(default);R4 多人时按这个字段隔离互不串。
    """

    owner: str = "default"
    codebase: str = "default"


# ──────────────────────────────────────────────────────────────────────────
# §3 证据(报告签名:每条结论锚 file:line + 原文)
# ──────────────────────────────────────────────────────────────────────────


class Evidence(BaseModel):
    """一条证据 = 代码位置 + 原文片段。

    为什么必须有:bug-RCA 报告的"签名"就是每条结论都锚到 file:line 并引用原文,
    否则结论无法核验。记忆里的知识项沿用这条纪律,注入提示词时模型才敢用。
    """

    file: str  # 相对仓根路径(如 wpa_supplicant/scan.c)
    line: int | None = None  # 行号(1-based;None 表示整文件级)
    snippet: str = ""  # 原文片段(短)


# ──────────────────────────────────────────────────────────────────────────
# §4 KnowledgeItem:一条知识(记忆的基本单元)
# ──────────────────────────────────────────────────────────────────────────


def _content_key(text: str) -> str:
    """归一化文本做去重键(借 mnemopi/deer-flow:压空白 + casefold)。

    同一根因的不同次报告,summary 文本可能小有出入,归一化后落到同一个 key →
    合并时被识别为"重复"而不是"新增"。
    """
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def make_id(scope: Scope, kind: str, summary: str) -> str:
    """知识项稳定 id = sha256(scope+kind+content_key)[:16]。

    稳定 id 的意义:同一根因重复 memorize → 同 id → 走"合并/加权"而非"新增",
    这正是持续学习(去重)的基础。id 由内容决定,不是随机 uuid。
    """
    raw = f"{scope.owner}/{scope.codebase}:{kind}:{_content_key(summary)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class KnowledgeItem(BaseModel):
    """一条记忆(知识项)。四类 kind 共用此模型,domain 字段按需填。

    生命周期:memorize 写入 → recall 命中(access_count++)→ consolidate 巩固(去重/
    衰减/升级)→ 必要时 invalidate(软删)。永不物理删除(审计可追溯)。
    """

    # —— 身份 ——
    id: str = ""  # 稳定 id(空则按 scope+kind+summary 自动算,见 _ensure_id)
    kind: Literal["codebase_fact", "bug_lesson", "mental_model", "domain_knowledge"]
    repo: str  # 代码库标识(如 wpa_supplicant)
    scope: Scope = Field(default_factory=Scope)
    summary: str  # 人读摘要(检索 + 注入提示词用,核心字段)

    # —— 展开内容(按 kind 用)——
    detail: str = ""  # 正文展开(模块说明 / 根因详述 …)
    # bug_lesson 专用
    symptom: str = ""  # 现象
    root_cause: str = ""  # 根因
    fix_patch: str = ""  # 补丁文本 / 引用
    blast_radius_files: list[str] = Field(default_factory=list)  # 影响面文件
    # codebase_fact / domain_knowledge 用
    kind_detail: Literal["module", "symbol", "architecture", "domain"] = "module"

    # —— 溯源 + 证据 ——
    commit_sha: str | None = None  # ★ 溯源到具体 commit(记忆"保质期"锚点)
    evidence: list[Evidence] = Field(default_factory=list)
    source_url: str | None = None  # 外部溯源 URL(domain_knowledge 用:网调来的协议知识锚主源;
    #   bug/codebase_fact 通常 None —— 它们的溯源靠 commit_sha + evidence file:line,是代码锚点)。
    source: str = ""  # 产生它的 report_id / workflow 名
    source_tier: SourceTier = SourceTier.unknown

    # —— 置信度 + 持续学习信号 ——
    confidence: float = 0.0  # 0..1(初始 = tier_weight·0.5;重提 Bayes 累加,见 consolidate)
    access_count: int = 0  # 被召回命中次数(升级 mental_model 的依据)
    last_recalled: datetime | None = None

    # —— bi-temporal(graphiti 思路):矛盾时"失效"而非"删除" ——
    valid_at: datetime = Field(default_factory=_utcnow)  # 这条知识"在真"的起点
    invalid_at: datetime | None = None  # 失效点(被取代/补丁已合入);None=仍有效
    created_at: datetime = Field(default_factory=_utcnow)  # 我们何时记录下它

    # —— 图边 / 软删 ——
    related: list[str] = Field(default_factory=list)  # 关联知识项 id(同模块/同符号/历史 bug)
    tags: list[str] = Field(default_factory=list)  # 自由标签(如 "type:root-cause")
    superseded_by: str | None = None  # 被哪条 id 取代(None=当前版本)

    # —— 纠正链(2026-08-13 补「纠正关系」闭环)——
    # corrects:新条(corrector)说「我纠正了哪些旧条」—— transit 字段,写入时消费掉(回填旧条
    #         的 corrected_by),不入库(查询/检索/体检读的是旧条上的 corrected_by 反向链,不是这个)。
    # corrected_by:旧条(corrected)上「我被哪条纠正了」—— 持久化(检索降权 + 体检可见)。
    #   与 superseded_by 的区别:superseded_by 绑定 active(设了 = 从 active 视图消失);
    #   corrected_by 不影响 active(被纠正 ≠ 失效,条目仍可检索/体检可见,只是检索降权)。
    #   场景:bug 根因被推翻(A 派"abort-failure"是错的,B 派"scan-only 竞态"纠正它)→
    #         B.corrects=[A.id],写入时自动回填 A.corrected_by=B.id → recall 时 A 被 0.3× 降权排后面。
    corrects: list[str] = Field(default_factory=list)  # 写入时指令:我纠正了这些旧条(transit,不入库)
    corrected_by: str | None = None  # 被哪条 id 纠正(None=未被纠正;持久化,检索降权用)

    # —— 向量(native 后端写入时算;recall 走 cosine)——
    embedding: list[float] | None = None

    @model_validator(mode="after")
    def _ensure_id(self) -> KnowledgeItem:
        """id 留空时按内容自动算(稳定 id)。"""
        if not self.id:
            self.id = make_id(self.scope, self.kind, self.summary)
        return self

    @property
    def active(self) -> bool:
        """是否当前有效(未失效且未被取代)。检索默认只看 active 的。"""
        return self.invalid_at is None and self.superseded_by is None


# ──────────────────────────────────────────────────────────────────────────
# §5 RecallHit:recall 的一条结果
# ──────────────────────────────────────────────────────────────────────────


class RecallHit(BaseModel):
    """recall 返回的一条命中。source 标它来自哪一路检索。

    三路(融合后统一成 RecallHit):
      - memory     :从知识项库命中(差异化核心 —— 历史教训/事实)
      - code       :从 code_index 命中(代码 chunk,现成 L1)
      - structural :从 code-review-graph 命中(blast-radius / 结构,可选)
    每条带 溯源 + 置信度 + 时效,注入提示词时模型知道可信度与新鲜度。
    """

    summary: str  # 给模型看的人读文本
    score: float  # 融合 + 衰减后的最终分(越大越相关)
    source: Literal["memory", "code", "structural"] = "memory"
    kind: str = ""  # knowledge item kind(memory 路才有意义)
    repo: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0
    valid_at: datetime | None = None
    created_at: datetime | None = None  # 写入时间(让消费方判新旧、偏好最新;对标 mem0 v3 时序排序)
    superseded_by: str | None = None  # 非空 = 这是被取代的旧版本(R3.5+ 仍可召回作参考;手动 invalidate 走 invalid_at 不在此列)
    corrected_by: str | None = None  # 非空 = 这条被另一条纠正了(检索降权;仍可见作参考,不同于 superseded_by)
    item_id: str | None = None  # 命中的 KI id(memory 路;code/structural 路为 None)
    tags: list[str] = Field(default_factory=list)  # 透传 KI 标签(渲染「未真机验证」等纪律标记用)
    # code/structural 路的定位字段(memory 路用 evidence)
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    snippet: str = ""

    def render(self) -> str:
        """渲染成给 LLM 看的一行(带溯源 + 置信度 + 时效)。"""
        if self.evidence:
            ev = "; ".join(f"{e.file}:{e.line}" if e.line else e.file for e in self.evidence[:3])
        elif self.file:
            ev = f"{self.file}:{self.line_start}-{self.line_end}" if self.line_end else f"{self.file}"
        else:
            ev = ""
        loc = f"  @{ev}" if ev else ""
        conf = f"  conf={self.confidence:.2f}" if self.confidence else ""
        # R3.5+(2026-08-06):显写入日期 + 旧版本标记,让模型判新鲜度、偏好最新(对标 mem0 v3 时序排序)。
        dt = f"  {self.created_at:%Y-%m-%d}" if self.created_at else ""
        old = "  (旧版本)" if self.superseded_by else ""
        corrected = "  (已被纠正)" if self.corrected_by else ""
        tag = f"[{self.source}]" if self.source != "memory" else ""
        # memory 路带 item_id 时输出(截断 8 位)—— 纠正链要用:memory_memorize(corrects=[...]) 要传
        # 「在 recall 输出里看到的 id」。code/structural 路 item_id=None,不渲染(避免 id=None 噪声)。
        kid = f"  id={self.item_id[:8]}" if self.item_id else ""
        # 验证纪律标记(P2-1):apply-only 记的 bug_lesson 带 unverified 标 —— 召回时显式亮出来,
        # 后续会话拿先验前先知道「这条没过真机」;真机确认后重提同补丁(verification=real_machine)
        # 换掉标记。位置学 corrected:跟在结论后面,不进 summary 污染内容键。
        unv = "  (未真机验证)" if "unverified" in self.tags else ""
        # 自由标签(2026-08-26 实测教训:短路判定要「主题对不对得上」,tags 是最快的主题域信号)。
        # 纪律标记有专属渲染(unv/kid),不重复进 tags 列表。
        vis_tags = [t for t in self.tags if t not in ("unverified", "verified_real_machine")][:4]
        tg = f"  tags={'|'.join(vis_tags)}" if vis_tags else ""
        return f"- {tag}{self.summary}{loc}{conf}{dt}{old}{corrected}{unv}{kid}{tg}".rstrip()
