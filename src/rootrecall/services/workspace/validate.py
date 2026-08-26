"""补丁验证(R3.1 Tier 0;完整 6 步见 workspace-design.md §6)。

面向小白:opencode 改完 code/,RootRecall 用 git diff 观察出补丁。这个补丁能不能干净打到
原仓?本模块做 Tier 0 确定性验证(零 LLM):
  1. **forward --check**:补丁能干净 apply 到原仓吗?(R2 挂点:LLM 吐的 diff 行号错,apply 挂;
     观察出的 git diff 应该过 —— 过不了说明路径/格式有问题。)
  2. **reverse --check**:补丁能干净 revert 吗?(证必要:能撤回 = 真实改动,不是空补丁。)
都用 `--check`(dry-run,不改文件)。`--recount` 容忍行号小偏差。

编译 / F2P / P2P / 多候选 rerank(Agentless)推到「构建环境就绪」(F3:wpa/bluez build 是硬前提),
不在 Tier 0。R3.1 只做 Tier 0;R5 加 Tier 1(repro test)+ Tier 2(对抗审)。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def validate_patch(
    patch: str,
    forward_dir: Path | str | None,
    *,
    reverse_dir: Path | str | None = None,
    timeout: float = 60.0,
) -> dict:
    """验证 patch 能否干净 apply(Tier 0,纯 git --check,零 LLM)。

    参数:
      patch        观察出的 unified diff(git diff 产物)。
      forward_dir  正向验证目录(原仓 repo_root):补丁应能干净 apply 到这里。
                   None = 跳过 forward,只做 reverse(worktree 自洽验证用:树已含改动,
                   forward 对同一棵树必失败,reverse --check 才是有效方向)。
      reverse_dir  反向验证目录(workspace/code,已含改动):补丁应能干净 revert(证必要)。
                   None = 跳过 reverse 检查。
    返回 {verified, forward_method, revert_ok, log}:
      verified        forward --check 通过(含降级 --3way / patch -p1);forward_dir=None 时为 None。
      forward_method  strict | 3way | patch | empty | skipped(降级路径,反映补丁质量)。
      revert_ok       reverse --check 结果(None=没测)。
      log             各步输出尾(诊断)。
    """
    if not patch or not patch.strip():
        return {"verified": False, "forward_method": "empty", "revert_ok": None, "log": "patch 为空"}

    # 容错:agent 读/传补丁时常 rstrip 掉末尾换行、或带 CRLF → git apply 报"补丁损坏"误判。
    # 归一化(LF + 补末尾换行)再验。e2e 实证:flash 传补丁丢末尾 \n → "第 71 行损坏"。
    patch = patch.replace("\r\n", "\n").replace("\r", "\n")
    if not patch.endswith("\n"):
        patch += "\n"

    log: list[str] = []
    if forward_dir is not None:
        verified, method = _forward_check(patch, str(forward_dir), timeout, log)
    else:
        verified, method = None, "skipped"
        log.append("[forward 跳过:worktree 自洽验证,只做 reverse --check]")

    revert_ok: bool | None = None
    if reverse_dir is not None:
        rr = subprocess.run(
            ["git", "apply", "--recount", "--reverse", "--check"],
            input=patch, cwd=str(reverse_dir), capture_output=True, text=True, timeout=timeout,
        )
        revert_ok = rr.returncode == 0
        log.append(f"[reverse --check {'通过' if revert_ok else '失败'}] {rr.stderr.strip()[-200:]}")

    return {"verified": verified, "forward_method": method, "revert_ok": revert_ok, "log": "\n".join(log)}


def _forward_check(patch: str, target: str, timeout: float, log: list[str]) -> tuple[bool, str]:
    """forward --check:严格 → 降级 --3way → 降级 patch -p1(记降级路径)。"""
    # 1. 严格 git apply --check
    r = subprocess.run(
        ["git", "apply", "--recount", "--check"],
        input=patch, cwd=target, capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode == 0:
        log.append("[strict --check 通过]")
        return True, "strict"
    log.append(f"[strict 失败 rc={r.returncode}] {r.stderr.strip()[-300:]}")

    # 2. 降级:--3way(用三路合并,容忍 context 漂移)
    r3 = subprocess.run(
        ["git", "apply", "--recount", "--3way", "--check"],
        input=patch, cwd=target, capture_output=True, text=True, timeout=timeout,
    )
    if r3.returncode == 0:
        log.append("[--3way 降级通过(补丁 context 有漂移)]")
        return True, "3way"
    log.append(f"[--3way 失败] {r3.stderr.strip()[-300:]}")

    # 3. 再降级:patch -p1(非 git 的经典 patch,更宽松)
    rp = subprocess.run(
        ["patch", "-p1", "--dry-run"],
        input=patch, cwd=target, capture_output=True, text=True, timeout=timeout,
    )
    if rp.returncode == 0:
        log.append("[patch -p1 降级通过]")
        return True, "patch"
    log.append(f"[patch -p1 失败] {rp.stderr.strip()[-300:]}")
    return False, "failed"
