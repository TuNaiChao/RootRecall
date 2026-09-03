"""RootRecall CLI 入口(`uv run rootrecall ...`)。

日常面(--help 可见,2026-08-21 瘦身后):
  rootrecall baseline add <路径>   一条命令建基线:登记(role=baseline,git url/branch 自动读)+ 建索引
  rootrecall baseline sync [名…]  基线同步:fetch→ff→增量刷索引(缺省=全部基线)
  rootrecall baseline checkout …  从基线取指定版本的一次性检出(worktree,登记 ephemeral)
  rootrecall baseline ls          列出全部受管仓
  rootrecall here [--codebase X]  bug/工作目录轻标记(默认检索库)
  rootrecall install --global     opencode 全局注册四件套(全机一次)

进阶面(隐藏但可用,自动化/排障用,全量参考见 docs/cli.md):
  index / repo … / memory … / mcp serve / models / lsp
  (systemd 定时任务调 repo sync、opencode 拉起 mcp serve、排障用 memory/lsp —— 只藏不删。)
"""


from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from rootrecall.platform.config import _default_config_path, get_app_config


def cmd_models(args) -> int:
    """列出 config.yaml 中配置的模型与角色路由。"""
    cfg = get_app_config()
    if not cfg.models:
        print("(config.yaml 中未配置任何模型)")
        return 1
    for m in cfg.models:
        caps = []
        if m.supports_thinking:
            caps.append("thinking")
        if m.supports_vision:
            caps.append("vision")
        cap_str = f"  [{', '.join(caps)}]" if caps else ""
        src = "  (来自 opencode 宿主)" if (m.display_name or "").startswith("opencode:") else ""
        print(f"- {m.name:20} {m.use:45}{cap_str}{src}")
    if cfg.model_roles:
        print("\nroles:")
        for role, target in cfg.model_roles.items():
            print(f"  {role:16} -> {target}")
    return 0


def cmd_opencode_models(args) -> int:
    """探测宿主 opencode 的 chat 模型(url+key 复用;key 只验存在,不显示值)。"""
    from rootrecall.platform.opencode_bridge import adopt_opencode_models, discover_opencode_models

    if args.adopt:
        try:
            print(adopt_opencode_models(args.adopt, _default_config_path()))
        except ValueError as e:
            print(f"❌ {e}")
            return 1
        print("重跑 `rootrecall models` 应能看到派生条目(名称 opencode-<provider>-<model>);"
              "要用它就把 model_roles 指过去(如 default: opencode-xxx)。")
        return 0

    r = discover_opencode_models()
    for note in r["notes"]:
        print(f"· {note}")
    if not r["models"]:
        print("宿主里没有可派生的 chat 模型(要求:~/.config/opencode/opencode.json 的 provider "
              "显式写了 baseURL 且列了 models;key 在 ~/.local/share/opencode/auth.json,type=api)。")
        if r["providers_no_url"]:
            print(f"· 配了但没写 baseURL 的 provider(不派生):{', '.join(r['providers_no_url'])}")
        return 1
    print(f"发现 {len(r['models'])} 个可派生 chat 模型(采纳后 key 运行时从宿主读,不落盘):")
    for m in r["models"]:
        key = "key✔" if m["has_key"] else "key✘(auth.json 里没有,采纳了也调不通)"
        print(f"  {m['provider']}/{m['model']:32} {key}  {m['base_url']}")
    if r["providers_no_url"]:
        print(f"· 配了但没写 baseURL 的 provider(不派生):{', '.join(r['providers_no_url'])}")
    print("\n采纳(写进 config.yaml 末尾的 models_from_opencode 段,可多个):")
    print(f"  uv run rootrecall opencode-models --adopt {r['models'][0]['provider']}/{r['models'][0]['model']}")
    return 0


def _vector_status(embedder) -> str:
    """图路异常提示里向量路的状态一句:零 key / --graph-only 时不谎报「已就绪」。"""
    if embedder is not None:
        return "向量索引已就绪(search_codebase 可用)"
    return "向量索引未建(零 key / --graph-only;search_codebase 不可用)"


def cmd_index(args) -> int:
    """为一个代码仓库建/更新索引 —— 向量索引 + 结构图一次到位(P1 代码理解服务)。

    用法:rootrecall index <repo_path> [repo_name]
    例:rootrecall index <repo>
       rootrecall index ~/src/bluez bluez --force
    没给 repo_name → 用 repo_path 的目录名。⚠️ repo_name 必须和 config.code_index.repo
    (search_code 查的表名)一致,否则 search_code 会查空表。

    默认建两样(代码情报工具全都要预建):
    - 向量索引 → search_codebase(BM25 + 向量 + RRF + rerank)
    - 结构图   → blast_radius / call_chain / repo_map / repo_overview(CRG tree-sitter 解析 + Leiden 社区)
    `--no-graph` 只建向量索引(快);`--graph-only` 反向:只建结构图,不碰 embedder 与向量索引
    (零 key 可用,图系 4 工具不依赖向量)。零 key(没配 embedding key)时向量路诚实跳过、
    **结构图照建不再连坐**(等价自动走 --graph-only,rc=2 提示向量未建);CRG(code-review-graph
    extra)没装则自动跳过结构图并提示,不挡向量索引。已建的结构图默认**增量刷新**(补丁打进/
    合入后重跑本命令即可,只重解析改动文件;拿不准的场合自动退回全量),`--force` 才强制全量重建。
    """
    import shutil
    from pathlib import Path

    from rootrecall.services.code_index.embed import create_embedder
    from rootrecall.services.code_index.index import build_index

    cfg = get_app_config()
    repo_path = Path(args.repo_path)
    if not repo_path.exists():
        print(f"错误:路径不存在: {repo_path}", file=sys.stderr)
        return 1
    repo_name = args.repo_name or repo_path.resolve().name
    from rootrecall.services.repos.registry import reanchor_data_path

    vs_path = reanchor_data_path(
        getattr(getattr(cfg.code_index, "vector_store", None), "path", "data/code_index"))

    graph_only = bool(getattr(args, "graph_only", False))  # checkout --index 等旧调用方不传此参
    if graph_only and args.no_graph:
        print("错误:--graph-only(只建图)与 --no-graph(只建向量)互斥,两开等于什么都不建。",
              file=sys.stderr)
        return 2

    # ── 1)embedder 前置判定:--graph-only 显式跳过;零 key 诚实降级为只建图 ──
    # (2026-09-02 图解耦:此前零 key 在这里 return 2 短路,结构图排在后面被连坐 —— 图系
    #  4 工具零 embedder,不该陪葬。zero_key_rc 让调用方仍知道向量路没建成。)
    zero_key_rc = 0
    embedder = None
    if graph_only:
        print("向量索引:--graph-only 跳过(embedder 与向量索引都不碰;search_codebase 将不可用)。")
    else:
        try:
            embedder = create_embedder(cfg.code_index.embedding)
        except ValueError as e:  # 零 key(远端档没配 api_key)→ 指三条路,图照建不甩栈
            zero_key_rc = 2
            print(f"⚠️ 向量索引跳过:{e}\n"
                  f"  零 key 三条路:① .env 配上远端 key 后重跑同名命令增量补建;\n"
                  f"  ② 最小模式:config.yaml 把 embedding.provider 切 sentence_transformers\n"
                  f"  (先 `uv sync --extra embedding-local`,模型走 hf-mirror 本地下载),并把 reranker.provider 设 off;\n"
                  f"  ③ 只用结构图:`--graph-only`。\n"
                  f"  本次继续建结构图(blast_radius / call_chain / repo_map / repo_overview 不依赖向量)。"
                  f"详见 docs/configuration.md「最小模式」。",
                  file=sys.stderr)

    # ── 0)播种(F5):小版本索引从同线基线索引拷贝起步,增量只重嵌差异文件 ──────
    # 场景:v20 的 5.50.61 出 bug,索引名 bluez-v20-5.50.61;它和基线 bluez-v20 绝大多数
    # 文件相同 → 拷贝基线索引(向量库+manifest)再走增量,只有差异文件重新 embed
    # (远端 embedding 按 token 计费,这条能省下 95%+ 的费用和时间)。幂等:目标已存在不拷。
    # 向量播种只在向量路要建时(零 key / --graph-only 拷了也没 embedder 增量);图播种不受影响。
    if args.seed:
        seed_vec, target_vec = Path(vs_path) / args.seed, Path(vs_path) / repo_name
        seed_sg = reanchor_data_path("data/structgraph") / args.seed
        target_sg = reanchor_data_path("data/structgraph") / repo_name
        if embedder is not None:
            if target_vec.exists():
                print(f"播种跳过:{target_vec} 已存在(--seed 只在目标索引不存在时拷贝)。")
            elif seed_vec.exists():
                shutil.copytree(seed_vec, target_vec)
                print(f"已播种向量索引:{args.seed} → {repo_name}(增量只重嵌差异文件)。")
            else:
                print(f"⚠️ 播种源不存在:{args.seed} —— 走正常全量建索引。", file=sys.stderr)
        if not target_sg.exists() and seed_sg.exists():
            shutil.copytree(seed_sg, target_sg)
            print(f"已播种结构图:{args.seed} → {repo_name}(增量刷新只重解析改动文件)。")

    # ── 1b)向量索引(search_codebase 用;零 key / --graph-only 时跳过)────
    if embedder is not None:
        stats = build_index(repo_path, repo_name, embedder, vs_path, force=args.force)
        n = stats.get("indexed", stats.get("total_chunks", "?"))
        print(f"向量索引完成 [{stats.get('mode')}]:{repo_name}  {n} chunk  "
              f"commit={(stats.get('repo_commit') or '-')[:10]}")

    # ── 2)结构图(blast_radius / call_chain / repo_map / repo_overview 用;可选,失败不致命)──
    # 没这条,降级提示「先建 rootrecall index」会把人引向只建了向量索引、结构图仍缺的死路。
    if args.no_graph:
        print("结构图:--no-graph 跳过(blast_radius / call_chain / repo_map / repo_overview 不可用)。")
        return zero_key_rc

    from rootrecall.services.code_index.code_graph import CodeGraph  # CRG 可选 extra,放函数内 lazy import

    graph_dir = reanchor_data_path("data/structgraph") / repo_name
    db_path = graph_dir / "graph.db"
    if db_path.exists() and not args.force:
        # 图已建 → 增量刷新(而非旧版的整个跳过:图会静默陈旧,--force 才重建太钝)。
        # 向量索引上面本就按 manifest 增量;这里补齐结构图的增量路:只重解析
        # built_head 快照以来改动 + 未跟踪新增的文件,社区按需重检测,拿不准自动退全量。
        try:
            _g, summary = CodeGraph.update(repo_root=str(repo_path), repo_name=repo_name,
                                           base_dir=str(graph_dir.parent))
            mode = summary.get("mode")
            if mode == "incremental":
                print(f"结构图已增量刷新:{db_path}"
                      f"(重解析 {summary.get('files_updated', len(summary.get('changed_files', [])))} 个改动文件"
                      f" + {len(summary.get('dependent_files', []))} 个依赖文件,"
                      f"社区重存 {summary.get('communities', 0)} 个;--force 可全量重建)。")
            elif mode == "noop":
                print(f"结构图无改动,跳过:{db_path}(--force 可全量重建)。")
            else:
                print(f"结构图已全量重建({summary.get('reason', '兜底')}):{db_path}。")
        except ImportError as e:  # 与下方全量路径同款:CRG 没装不挡向量索引
            print(f"结构图跳过:CRG 未装({e})。装它:`uv sync --extra code-review-graph`。\n"
                  f"  {_vector_status(embedder)};blast_radius / call_chain / repo_map / repo_overview 暂不可用。",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001 —— 刷新失败不致命:旧图还在,工具按旧图答
            print(f"结构图增量刷新失败(非致命,沿用旧图):{e}\n"
                  f"  可 `rootrecall index <repo> <name> --force` 全量重建。", file=sys.stderr)
        return zero_key_rc
    if args.force and graph_dir.exists():
        shutil.rmtree(graph_dir)  # --force 清旧图,免 stale 节点混进新图
    print(f"结构图建图中(CRG tree-sitter 解析全仓,大仓需几分钟):{repo_path} …")
    try:
        CodeGraph.build(repo_root=str(repo_path), repo_name=repo_name,
                        base_dir=str(graph_dir.parent))
        print(f"结构图建好:{db_path}(blast_radius / call_chain / repo_map / repo_overview 可用"
              f"{';search_codebase 不可用(向量索引未建)' if embedder is None else ''})。")
    except ImportError as e:  # CRG(code-review-graph extra)没装 → 提示装,不挡向量索引
        print(f"结构图跳过:CRG 未装({e})。装它:`uv sync --extra code-review-graph`。\n"
              f"  {_vector_status(embedder)};blast_radius / call_chain / repo_map / repo_overview 暂不可用。",
              file=sys.stderr)
    except Exception as e:  # noqa: BLE001 —— 建图失败不致命;向量路状态如实说,零 key 不谎报「已就绪」
        print(f"结构图建图失败(非致命,{_vector_status(embedder)}):{e}\n"
              f"  blast_radius / call_chain / repo_map / repo_overview 暂不可用。",
              file=sys.stderr)
    return zero_key_rc


def cmd_lsp(args) -> int:
    """L2 精确导航(clangd/LSP)子命令:health 自检 / refs 冒烟。

      rootrecall lsp health [repo_root]   检测 clangd + compile_commands 是否就位
      rootrecall lsp refs <file> <line> <col> [repo_root]   冒烟:直接打一次 references
    """
    from pathlib import Path

    from rootrecall.services.code_index.lsp import get_lsp_server, lsp_health

    cfg = get_app_config()
    repo = Path(args.repo_root).resolve() if args.repo_root else Path(cfg.sandbox.workspace).resolve()

    if args.lsp_cmd == "health":
        h = lsp_health(str(repo))
        print(h.render())
        return 0 if h.ok else 1

    if args.lsp_cmd == "refs":
        fpath = Path(args.file)
        if not fpath.is_file():
            print(f"错误:文件不存在: {args.file}", file=sys.stderr)
            return 1
        try:
            rel = fpath.resolve().relative_to(repo)
        except ValueError:
            rel = fpath
        try:
            sync = get_lsp_server(str(repo))
        except Exception as e:
            print(f"错误:启动 clangd 失败: {e}\n  先跑 `uv run rootrecall lsp health`。", file=sys.stderr)
            return 1
        with sync.open_file(str(rel)):
            locs = sync.request_references(str(rel), args.line - 1, args.col - 1)
        if not locs:
            print(f"(无 references:{args.file}:{args.line}:{args.col})")
            return 0
        for loc in locs:
            uri = loc.get("uri", "")
            rng = loc.get("range", {}).get("start", {})
            p = uri[7:] if uri.startswith("file://") else uri
            print(f"{p}:{rng.get('line', 0) + 1}:{rng.get('character', 0) + 1}")
        return 0

    print(f"(未知 lsp 子命令: {args.lsp_cmd})", file=sys.stderr)
    return 1


def _cmd_memory_ingest(args, scope, repo) -> int:
    """`rootrecall memory ingest <path>` —— 摄取文档 → 记忆(R3.4)。

      rootrecall memory ingest <报告.md> [--kind auto|report|patch] [--source-tier imported|stated|inferred]
      rootrecall memory ingest <补丁.patch> [--commit-sha SHA]
    按扩展名自动分流:.md/.txt/.pdf → 报告路(extract + memorize,长文自动分块);
    .patch/.diff → 补丁路(PatchIngestPipeline,retrieve-then-summarize)。
    """
    import asyncio
    from pathlib import Path

    from rootrecall.services.memory.ingest import ingest_document
    from rootrecall.services.memory.schema import SourceTier

    p = Path(args.path)
    if not p.exists():
        print(f"错误:文件不存在: {p}", file=sys.stderr)
        return 1
    tier_map = {"imported": SourceTier.imported, "stated": SourceTier.stated, "inferred": SourceTier.inferred}
    stier = tier_map.get(args.source_tier or "imported", SourceTier.imported)
    try:
        stats = asyncio.run(ingest_document(
            p, scope=scope, repo=repo, source_tier=stier,
            commit_sha=args.commit_sha, kind=args.kind,
        ))
    except NotImplementedError as e:
        print(f"(该路径暂未实现: {e})", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - CLI 顶层兜底
        print(f"ingest 出错:{e}", file=sys.stderr)
        return 1

    route = stats.get("route")
    if route == "patch":
        print(f"补丁摄取:{p.name} → 产 {stats.get('items_produced', 0)} 条 → 写入 {stats.get('wrote', 0)} 条(scope={repo})。")
    else:
        warn = f"  ⚠️ {stats['warn']}" if stats.get("warn") else ""
        print(f"报告摄取:{p.name} → {stats.get('chunks', 0)} 块 → 写入 {stats.get('wrote', 0)} 条(scope={repo})。{warn}")
    return 0


def cmd_memory(args) -> int:
    """记忆核心子命令(R1):recall 翻记忆 / add 记一条(或从报告抽)/ ingest 摄取文档补丁 / list / consolidate / invalidate。

      rootrecall memory recall "<query>" [--top-k N] [--repo X]
      rootrecall memory add --kind bug_lesson --summary "..." [--file F --line L] [--root-cause "..."]
      rootrecall memory add --from-report <报告.md> [--commit-sha SHA]
      rootrecall memory ingest <文档或补丁> [--kind auto|report|patch] [--source-tier imported]  # R3.4
      rootrecall memory list [--kind K] [--include-invalid]
      rootrecall memory consolidate          # 巩固:升级 mental_model
      rootrecall memory invalidate <id>
    """
    import asyncio
    from pathlib import Path

    from rootrecall.services.memory import get_memory_service
    from rootrecall.services.memory.schema import Scope

    cfg = get_app_config()
    repo = args.repo or getattr(cfg.code_index, "repo", None) or Path(cfg.sandbox.workspace).name
    scope = Scope(owner="default", codebase=repo)
    svc = get_memory_service()
    sub = args.memory_cmd

    if sub == "recall":
        hits = asyncio.run(svc.recall(args.query, scope, top_k=args.top_k))
        if not hits:
            print(f"(记忆里没找到与 '{args.query}' 相关的历史教训/事实)")
            return 0
        print(f"检索到 {len(hits)} 条(按相关度降序):")
        for h in hits:
            print(h.render())
        return 0

    if sub == "add":
        if args.from_report:
            text = Path(args.from_report).read_text(encoding="utf-8")
            try:
                n = asyncio.run(svc.memorize_report(
                    text, scope, repo=repo, commit_sha=args.commit_sha, source=args.from_report))
            except NotImplementedError as e:
                print(f"错误:当前 memory 后端不支持从报告抽取: {e}", file=sys.stderr)
                return 2
            print(f"从报告抽出并写入 {n} 条知识项(scope={repo})。")
            return 0

        from rootrecall.services.memory.schema import Evidence, KnowledgeItem, SourceTier

        if not args.summary:
            print("错误:直接记一条需要 --summary(或用 --from-report 从报告抽)。", file=sys.stderr)
            return 2
        ev = [Evidence(file=args.file, line=args.line)] if args.file else []
        # domain_knowledge 的 source_tier 按 source_url 有无分(网调=imported / 用户笔记=stated);
        # bug/codebase_fact 维持 stated(CLI 直记,人/报告陈述)。
        if args.kind == "domain_knowledge":
            cli_tier = SourceTier.imported if args.source_url else SourceTier.stated
        else:
            cli_tier = SourceTier.stated
        item = KnowledgeItem(
            kind=args.kind, repo=repo, scope=scope, summary=args.summary,
            root_cause=args.root_cause or "", detail=args.detail or "", evidence=ev,
            source_url=args.source_url,
            source="cli", source_tier=cli_tier,
        )
        n = asyncio.run(svc.memorize([item], scope))
        print(f"已记入(id={item.id}, kind={args.kind}, 合并/新增 {n} 条)。")
        return 0

    if sub == "ingest":
        # 摄取外部文档(bug 报告/调研报告 .md/.txt/.pdf 或补丁 .patch/.diff)→ 记忆(R3.4)。
        return _cmd_memory_ingest(args, scope, repo)

    if sub == "list":
        items = asyncio.run(svc.list_items(scope, kind=args.kind, include_invalid=args.include_invalid))
        if not items:
            print(f"(scope={repo} 无知识项)")
            return 0
        for it in items:
            flag = "" if it.active else "  [失效]"
            ev = f" @{it.evidence[0].file}:{it.evidence[0].line}" if it.evidence else ""
            print(f"- [{it.kind}] {it.summary[:80]}{ev}  conf={it.confidence:.2f} acc={it.access_count}{flag}  ({it.id})")
        return 0

    if sub == "consolidate":
        stats = asyncio.run(svc.consolidate(scope, repo_path=args.repo_path))
        print(
            f"巩固完成:扫 {stats.get('scanned', 0)},升级 mental_model {stats.get('promoted', 0)},"
            f"矛盾对 {stats.get('contradictions', 0)},重复簇 {stats.get('duplicate_clusters', 0)},"
            f"已合入上游 {stats.get('merged_upstream', 0)},过期 {stats.get('stale', 0)}。"
        )
        return 0

    if sub == "invalidate":
        ok = asyncio.run(svc.invalidate(args.id, scope, reason=args.reason or ""))
        print(f"{'已失效' if ok else '未找到/已失效'}: {args.id}")
        return 0 if ok else 1

    if sub == "backfill":
        try:
            rep = asyncio.run(svc.backfill(scope, dry_run=args.dry_run))
        except (AttributeError, NotImplementedError):
            print("错误:当前 memory 后端不支持 backfill。", file=sys.stderr)
            return 2
        except ValueError as e:  # 零 key → 指路,不甩栈(dry-run 也走这:embedder 不可用连列都列不了)
            print(f"错误:{e}", file=sys.stderr)
            return 2
        if args.dry_run:
            print(f"待补嵌 {rep['pending']} 条(active 且无向量):")
            for it in rep["items"]:
                print(f"  {it['id']}  {it['summary']}")
            return 0
        print(f"backfill 完成:待补 {rep['pending']} 条,已补嵌 {rep['embedded']} 条"
              f"(只更新向量列,不触发置信度/合并;重复跑零变更)。")
        return 0

    print(f"(未知 memory 子命令: {sub})", file=sys.stderr)
    return 1


def cmd_mcp(args) -> int:
    """启动 MCP server(把 RootRecall 能力做成工具给 coding agent 调;stdio 或 http)。

    需 `uv sync --extra mcp`。transport:
      - stdio(默认):agent 拉起子进程 1:1 接入(delegate 老路径 / 本地单机最简)。
      - http:warm 长进程,多 agent 共用,省每 bug 重启加载 ~1.2GB 的 cold-boot(③)。
        先 `rootrecall mcp serve --transport http` 跑起来,再把 opencode/codex 指向
        http://<host>:<port>/mcp。
    """
    import os as _os

    from rootrecall.platform.config import _default_config_path, get_app_config
    from rootrecall.tools.mcp_memory import build_server

    # MCP server 被 opencode 拉起时 cwd 通常是 workspace/code(≠ RootRecall 根)→ config 里相对
    # data/ 路径(memory SQLite / code_index LanceDB)会写进 workspace(污染补丁 + 记忆不持久)。
    # chdir 到 RootRecall 根(config.yaml 所在),让相对路径解析回正轨。MCP server 是独立进程,
    # 工具都用绝对路径/名查(log_path 绝对、codebase 走 env、index 走 repo 表名),不依赖 cwd。
    _os.chdir(_default_config_path().parent.parent)

    # transport 优先级:CLI 标志 > config.mcp.transport > 默认 stdio。http 模式要把 host/port
    # 焊进 FastMCP 构造(run() 不收 host/port,见 mcp_memory.build_server 注释)。
    mcp_cfg = get_app_config().mcp
    transport = (getattr(args, "transport", None) or mcp_cfg.transport).lower()
    http_mode = transport in ("http", "streamable-http", "streamable_http")
    build_kwargs: dict = {}
    if http_mode:
        build_kwargs["host"] = args.host or mcp_cfg.host
        build_kwargs["port"] = args.port or mcp_cfg.port

    try:
        server = build_server(codebase=args.codebase, **build_kwargs)
    except ImportError as e:
        print(f"错误:MCP 依赖未装。装它: uv sync --extra mcp\n  ({e})", file=sys.stderr)
        return 2

    if http_mode:
        print(f"RootRecall MCP(streamable-http)→ http://{build_kwargs['host']}:{build_kwargs['port']}/mcp"
              f"  (Ctrl-C 停;opencode/codex 指过来即可)", file=sys.stderr)
        server.run(transport="streamable-http")  # 阻塞:uvicorn 服务
    else:
        server.run()  # 阻塞:stdio 循环
    return 0


def cmd_repo(args) -> int:
    """仓库注册表子命令(F1 repo registry):ls / register / rm / resolve / checkout / gc / sync。

    注册表(data/repos.yaml)把「索引名 ↔ 仓库路径 ↔ 角色 ↔ 生命周期」串起来:
      rootrecall repo ls                                  列出全部受管仓(角色/路径/索引名)
      rootrecall repo register <名> --path <p> [--url U] [--role baseline|ephemeral|unmanaged]
                  [--branch B] [--bug ID] [--codebase 索引名]                  登记/更新(upsert)
      rootrecall repo rm <名>                             移除记录(只删记录不删盘上文件)
      rootrecall repo resolve <名或路径>                  反查本地绝对路径(注册表→索引清单→data/repos)
      rootrecall repo checkout <名> --from <基线> --ref <tag> [--bug B] [--index]
                                                          开一次性检出(worktree);--index 顺手播种建索引
      rootrecall repo gc [--dry-run] [--name N] [--prune-orphans]               回收过期 ephemeral
      rootrecall repo sync [名…] [--analyze <fork名>]                            基线同步+上游三态报告
    baseline = 共享基线(永久保留,repo sync 定时更新);ephemeral = 某 bug 的一次性检出
    (repo gc 级联清理);unmanaged = ensure_repo 顺手 clone 的样机/手动 index 未声明角色的仓。
    """
    from rootrecall.services.repos.registry import RepoRegistry, resolve_repo_path

    reg = RepoRegistry()

    if args.repo_cmd == "ls":
        recs = reg.list()
        if not recs:
            print("(注册表为空 —— rootrecall baseline add <代码仓路径> 一条命令建基线,或 ensure_repo/index 自动登记)")
            return 0
        print(f"{'名字':24} {'角色':11} {'路径':36} 索引/分支/bug")
        for r in recs:
            extra = r.index_name if r.index_name != r.name else ""
            extra += f" @{r.branch}" if r.branch else ""
            extra += f" bug={r.bug_id}" if r.bug_id else ""
            warn = "" if r.exists_on_disk() else "  ⚠️ 路径不在盘上"
            print(f"{r.name:24} {r.role:11} {r.path:36} {extra}{warn}")
        return 0

    if args.repo_cmd == "register":
        if not args.path and not args.url and not reg.get(args.name):
            print("错误:新登记需要 --path 或 --url 之一(已有记录可省略,仅改角色等字段)。", file=sys.stderr)
            return 2
        # role 缺省 = 保留现值(新记录才落 unmanaged)—— registry.register 统一处理
        rec = reg.register(
            args.name, path=args.path, url=args.url, role=args.role,
            branch=args.branch, bug_id=args.bug, codebase=args.codebase, note=args.note,
        )
        print(f"已登记: {rec.name}  role={rec.role}  path={rec.path or '-'}"
              f"{f' url={rec.url}' if rec.url else ''}{f' @{rec.branch}' if rec.branch else ''}")
        return 0

    if args.repo_cmd == "rm":
        rec = reg.remove(args.name)
        print(f"已移除记录: {rec.name}(盘上文件未动,要删盘上文件用 repo gc / 手动 rm)"
              if rec else f"未找到记录: {args.name}")
        return 0 if rec else 1

    if args.repo_cmd == "resolve":
        path, source = resolve_repo_path(args.name_or_path)
        if path is None:
            print(f"❌ 解析失败({source})", file=sys.stderr)
            return 1
        print(f"repo_path={path}  (来源: {source})")
        return 0

    if args.repo_cmd == "checkout":
        # 从基线(baseline 注册记录或其 bare 镜像)开一个一次性检出:worktree 共享对象库,
        # 秒级创建;登记为 ephemeral(bug 分析完 repo gc 级联回收)。
        from pathlib import Path

        from rootrecall.services.repos.mirror import add_worktree, ensure_mirror, worktrees_root

        base = reg.get(args.from_repo)
        if base is None:
            print(f"错误:基线未注册 —— 先 `rootrecall repo register {args.from_repo} --url <git地址> "
                  f"--role baseline --branch <分支>`。", file=sys.stderr)
            return 2
        url = base.url or (Path(base.mirror) if base.mirror else None)
        if url is None:
            print(f"错误:基线 {args.from_repo} 既无 url 也无 mirror,没法开检出。", file=sys.stderr)
            return 2
        mirror, new_clone = ensure_mirror(args.from_repo, str(url))
        if base.mirror != str(mirror):  # 首次开检出 → 把镜像落点回填基线记录
            reg.register(args.from_repo, mirror=str(mirror))
        dest = Path(args.dest) if args.dest else worktrees_root() / args.name
        wt, new_wt = add_worktree(mirror, args.ref, dest)
        ref_desc = args.ref if args.ref else (base.branch or "HEAD")
        reg.register(args.name, path=str(wt), role="ephemeral", from_repo=args.from_repo,
                     branch=ref_desc, bug_id=args.bug, mirror=str(mirror))
        print(f"✅ 检出就绪:{wt}  ({'新建 worktree' if new_wt else '已有,复用'};镜像 {'新 clone' if new_clone else '复用'} {mirror})")
        tail = "" if args.index else f";建索引: uv run rootrecall index {wt} {args.name}"
        print(f"   已登记 ephemeral(role=ephemeral, from={args.from_repo}"
              f"{f', bug={args.bug}' if args.bug else ''}){tail}")
        if args.index:
            # 自动开仓收尾一步:播种基线索引后增量建(P0)。路径经 reanchor(ROOTRECALL_HOME
            # 设置时 data/ 相对路径改锚新家;未设 = 现状)。cmd_index 其余相对落点已同款处理,
            # chdir 锚安装根保留作兜底(agent 常在 bug 目录里跑这条命令)。
            import os as _os

            from rootrecall.services.repos.registry import _install_root, reanchor_data_path

            vs_root = reanchor_data_path(
                getattr(getattr(get_app_config().code_index, "vector_store", None),
                        "path", "data/code_index"))
            seed = base.index_name if (vs_root / base.index_name).exists() else None
            prev_cwd = _os.getcwd()
            try:
                _os.chdir(_install_root())
                print(f"— --index:建索引 {args.name}(seed={seed or '无 → 全量'})…")
                cmd_index(argparse.Namespace(repo_path=str(wt), repo_name=args.name,
                                             force=False, no_graph=False, seed=seed))
            except Exception as e:  # noqa: BLE001 —— 索引失败不撤销检出(它已就绪),诚实提示手动补
                print(f"⚠️ --index 建索引未完成:{e}\n"
                      f"   检出可用;稍后补跑: uv run rootrecall index {wt} {args.name}", file=sys.stderr)
            finally:
                _os.chdir(prev_cwd)
        return 0

    if args.repo_cmd == "gc":
        from rootrecall.services.repos.registry import gc_ephemeral

        rep = gc_ephemeral(max_age_days=args.max_age_days, dry_run=args.dry_run,
                           names=[args.name] if args.name else None)
        verb = "将删(dry-run)" if rep["dry_run"] else "已删"
        for item in rep["removed"]:
            casc = " + ".join(str(c) for c in item["cascades"])
            print(f"🗑  {verb}:{item['name']}(age={item['age_days']}d{f', bug={item['bug']}' if item['bug'] else ''})→ {casc}")
        for item in rep["kept_young"]:
            print(f"⏳ 保留(未到期):{item['name']}(age={item['age_days']}d;点名强删加 --name)")
        if rep["orphan_indexes"]:
            print("⚠️ 孤儿索引(manifest 记的源仓路径已消失;要删手动 rm 或加 --prune-orphans):")
            for p in rep["orphan_indexes"]:
                print(f"   {p}")
            if args.prune_orphans and not args.dry_run:
                import shutil
                for p in rep["orphan_indexes"]:
                    shutil.rmtree(p, ignore_errors=True)
                    print(f"🗑  已删孤儿索引:{p}")
        for p in rep.get("legacy_indexes", []):
            print(f"ℹ️ 未登记索引(老清单无 repo_path,不删;对源仓重跑 index 即纳入管理):{p}")
        if not rep["removed"] and not rep["kept_young"] and not rep["orphan_indexes"] and not rep.get("legacy_indexes"):
            print("(没有可回收的 ephemeral 仓,也没有孤儿索引)")
        return 0

    if args.repo_cmd == "sync":
        # 基线仓定时更新:fetch --prune → 检出 ff 跟进 → 增量刷索引 →(可选)上游三态分析报告。
        # 幂等,给 systemd timer / cron 反复跑(cron/systemd 样例见 deploy/)。
        from rootrecall.services.repos.mirror import sync_repo
        from rootrecall.services.repos.registry import RepoRegistry

        reg = RepoRegistry()
        names = args.names or [r.name for r in reg.list() if r.role == "baseline"]
        if not names:
            print("(没有 baseline 注册仓可同步;先 repo register <名> --url <地址> --role baseline)")
            return 0
        if args.analyze_agent and not args.analyze:
            print("错误:--analyze-agent 要与 --analyze <FORK名> 同用(它复核的就是三态报告)。", file=sys.stderr)
            return 2
        if args.ingest_report and not args.analyze:
            print("错误:--ingest-report 要与 --analyze <FORK名> 同用(入记忆的就是三态报告)。", file=sys.stderr)
            return 2

        embedder = None
        if not args.no_index:
            try:
                from rootrecall.services.code_index.embed import create_embedder
                embedder = create_embedder(get_app_config().code_index.embedding)
            except Exception as e:  # noqa: BLE001 —— key 缺/网络不可达:同步不因此中断,只跳过索引刷新
                print(f"⚠️ embedder 不可用({e}),本次跳过索引刷新(要强制跳过加 --no-index)", file=sys.stderr)

        rc = 0
        for n in names:
            try:
                r = sync_repo(n, analyze_fork=args.analyze, analyze_agent=args.analyze_agent,
                              ingest_report=args.ingest_report,
                              refresh_index=not args.no_index, embedder=embedder, registry=reg)
            except Exception as e:  # noqa: BLE001 —— 单仓失败不挡其余仓
                print(f"❌ {n}: {e}", file=sys.stderr)
                rc = 1
                continue
            if "skipped" in r:
                print(f"⏭  {n}: {r['skipped']}")
                continue
            n_new = len(r.get("new_commits") or [])
            ff = "" if "fast_forwarded" not in r else ("  ✅ ff 跟进" if r["fast_forwarded"] else "  ⚠️ 不能 ff,HEAD 未动")
            print(f"🔄 {n}: 新 commit {n_new}{ff}"
                  + (f"  索引[{(r.get('index') or {}).get('mode') if isinstance(r.get('index'), dict) else r.get('index')}]" if r.get("index") else ""))
            for c in (r.get("new_commits") or [])[:10]:
                print(f"    {c}")
            if len(r.get("new_commits") or []) > 10:
                print(f"    …共 {n_new} 条")
            if r.get("note"):
                print(f"    注:{r['note']}")
            if r.get("analysis"):
                a = r["analysis"]
                if a.get("error"):
                    print(f"    ⚠️ 分析未成:{a['error']}")
                else:
                    s = a.get("summary", {})
                    print(f"    📊 三态分析:{a['range']} → total={s.get('total', 0)} "
                          f"already_fixed={s.get('already_fixed', 0)} recommend_merge={s.get('recommend_merge', 0)} "
                          f"conflict={s.get('conflict', 0)}  报告:{a['report']}")
                    if a.get("agent_review"):
                        print(f"    🤖 agent 复核:{a['agent_review']}")
                    if a.get("ingest"):
                        print(f"    🧠 报告入记忆:{a['ingest']}")
        return rc

    print(f"(未知 repo 子命令: {args.repo_cmd})", file=sys.stderr)
    return 1


def _git_out(repo: Path, *git_args: str) -> str | None:
    """在 repo 里跑一条 git 命令,成功返回 stdout(去尾换行),失败/没装 git 返回 None。"""
    import subprocess

    try:
        r = subprocess.run(["git", "-C", str(repo), *git_args],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _baseline_default_name(path: Path) -> tuple[str, str | None]:
    """基线默认名 = 相对总目录(ROOTRECALL_CODEBASES,quickstart 建,默认 ~/codebases)的
    路径**倒序**连 '-':v20/bluez → bluez-v20、upstream/bluez → bluez-upstream、systemd → systemd。

    不在总目录下 → 退回目录名,并带提示(建议 --name 或挪进总目录);路径就是总目录本身 → 空名 + 错误说明。
    """
    import os

    root = Path(os.environ.get("ROOTRECALL_CODEBASES") or Path.home() / "codebases").resolve()
    p = path.resolve()
    try:
        rel = p.relative_to(root)
    except ValueError:
        return p.name, f"路径不在总目录({root})下,默认名只取末级目录名;建议 --name 显式给,或把仓挪进总目录"
    parts = rel.parts
    if not parts:
        return "", f"路径就是总目录本身({root});要指向里面的具体代码仓"
    return "-".join(reversed(parts)), None


def cmd_baseline_add(args) -> int:
    """一条命令建基线:登记(role=baseline,url/branch 从 git 自动读)+ 建索引(向量+结构图)。

      rootrecall baseline add ~/codebases/v20/bluez    → 基线名 bluez-v20
      rootrecall baseline add ~/codebases/v25/bluez    → 基线名 bluez-v25
      rootrecall baseline add ~/codebases/systemd      → 基线名 systemd
    默认名规则见 _baseline_default_name;--name 覆盖。基线必须是 git 仓(取版本 checkout /
    同步 sync 都依赖 git 对象库与 remote)。幂等:重跑 = upsert 登记 + 索引增量刷新。
    """
    import os as _os

    from rootrecall.services.repos.registry import RepoRegistry, _install_root

    p = Path(args.path).expanduser()
    if not p.is_dir():
        print(f"错误:目录不存在:{p}", file=sys.stderr)
        return 1
    if _git_out(p, "rev-parse", "--is-inside-work-tree") is None:
        print(f"错误:{p} 不是 git 仓 —— 基线需要 git(取版本 checkout / 同步 sync 都依赖它)。\n"
              f"  先把源码 git clone 进总目录再 add;只想建索引不进基线体系,用进阶命令: "
              f"rootrecall index <路径> <索引名>(见 docs/cli.md)", file=sys.stderr)
        return 2

    name, warn = _baseline_default_name(p)
    if args.name:
        name, warn = args.name, None
    if not name:
        print(f"错误:{warn}", file=sys.stderr)
        return 2

    # git 元数据自动读:remote(origin 优先,退第一个)/ 当前分支(detached 时退 tag/短 sha)
    url = _git_out(p, "remote", "get-url", "origin")
    if url is None:
        first = _git_out(p, "remote")
        if first:
            url = _git_out(p, "remote", "get-url", first.splitlines()[0])
    branch = _git_out(p, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":  # detached:精确 tag 优先,退短 sha
        branch = _git_out(p, "describe", "--tags", "--exact-match", "HEAD") \
            or _git_out(p, "rev-parse", "--short", "HEAD")

    reg = RepoRegistry()
    existing = reg.get(name)
    reg.register(name, path=str(p.resolve()), url=url, role="baseline",
                 branch=branch, codebase=name)
    print(f"✅ 基线登记:{name}  role=baseline{'(已存在,upsert 增量刷新)' if existing else ''}"
          f"  path={p.resolve()}")
    print(f"   url={url or '-'}  branch={branch or '-'}")
    if url is None:
        print("   ⚠️ 没检测到 git remote —— baseline sync 将不可用;补:rootrecall repo register "
              f"{name} --url <git地址>(upsert,其余字段不动)", file=sys.stderr)
    if warn:
        print(f"   ⚠️ {warn}")
    print("— 建索引(向量+结构图,大仓分钟级)…")
    # 与 repo checkout --index 同款守卫:调用方可能在任意 cwd(如 agent 在 bug 目录跑),
    # 把相对 data/ 落点锚回安装根(ROOTRECALL_HOME 设了时 reanchor 已给绝对路径,chdir 无副作用)。
    prev_cwd = _os.getcwd()
    try:
        _os.chdir(_install_root())
        rc = cmd_index(argparse.Namespace(repo_path=str(p), repo_name=name,
                                          force=args.force, no_graph=args.no_graph,
                                          graph_only=args.graph_only, seed=None))
    finally:
        _os.chdir(prev_cwd)
    if rc == 0:
        print(f"基线就绪。之后:`baseline sync` 同步最新+增量刷索引;"
              f"`baseline checkout <新名> --from {name} --ref <tag> --bug <bug号> --index` 取指定版本;"
              f"任意 bug 目录 `opencode` 直接问(项目+版本会自动从基线开检出)。")
    else:
        print(f"⚠️ 登记已完成,但向量索引未建(rc={rc};多半是缺 embedding key)——上面若已打印"
              f"「结构图建好」则图系 4 工具(blast_radius / call_chain / repo_map / repo_overview)"
              f"已可用;补 key 后重跑 `baseline add {p}` 同名增量补建向量,登记不会重复。", file=sys.stderr)
    return rc


def cmd_install(args) -> int:
    """opencode 全局注册 / 卸载(F2):四件套装进 ~/.config/opencode,全机一次。

      rootrecall install --global           装:skills 软链 + mcp.rootrecall + rootrecall-* agent 块 + AGENTS.md 路由段
      rootrecall install --global --uninstall  卸:只摘自己写的东西(别人的配置绝不动)
    装完之后任意目录 `opencode` 免接线(@ 点名 rootrecall-* subagent 也可用);bug 目录里跑
    `rootrecall here` 补默认检索库标记。
    幂等可重跑(git pull 升级后重跑一次同步路由表/agent 块)。
    """
    from rootrecall.services.install import install_global, uninstall_global

    if args.uninstall:
        r = uninstall_global()
        print(f"已卸载全局注册({r['config_home']}):")
        print(f"  skills: 摘除 {len(r['skills_removed'])} 个软链 {r['skills_removed']}")
        print(f"  mcp: {r['mcp']}")
        print(f"  agents: {r['agents']}")
        print(f"  AGENTS.md: {r['agents_md']}")
        return 0

    r = install_global()
    print(f"✅ 已全局注册({r['config_home']};重跑同步升级,卸载加 --uninstall):")
    print(f"  skills: {len(r['skills'])} 个软链 -> {r['skills']}")
    print(f"  mcp: {r['mcp']}")
    print(f"  agents: {r['agents']}")
    print(f"  AGENTS.md: {r['agents_md']}")
    print("之后任意目录 `opencode` 免接线直接问;bug 目录可跑 `rootrecall here --codebase <索引名>` 定默认检索库。")
    return 0


def cmd_here(args) -> int:
    """在 bug/工作目录现场做轻量标记(F2,配合全局注册 = 每目录唯一要跑的命令)。

      rootrecall here [--codebase <索引名>]
    写 `.rootrecall.yaml`(人/agent 可读标记)+ 项目 opencode.json(锚 MCP + 默认检索库),
    不是 git 仓则 git init。已有自己的 opencode.json(不含 rootrecall)→ 备份 .bak 后跳过不覆盖。
    """
    from rootrecall.services.install import here

    r = here(codebase=args.codebase)
    print(f"✅ 已标记({r['project']}):")
    if r.get("git_init"):
        print("  git init(opencode 项目发现沿 git 根)")
    print(f"  标记文件: {r['marker']}")
    print(f"  opencode.json: {r['opencode_json']}")
    print("cd 该目录 && opencode —— 默认界面直接提问,agent 按 AGENTS.md 路由表自动载入 skill。")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 把 .env 读进环境变量;必须在任何 config/$VAR 解析之前
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="rootrecall",
        description="RootRecall —— 代码仓基线一条龙(baseline)+ bug 目录标记(here)+ opencode 接线(install);"
                    "进阶命令隐藏不删(repo/memory/index/mcp/models/lsp),全量参考 docs/cli.md")
    sub = parser.add_subparsers(dest="cmd")

    # ── 日常面:baseline 家族(用户唯一高频入口,2026-08-21 CLI 瘦身)────────────
    sub_base = sub.add_parser("baseline", help="代码仓基线一条龙:add 登记+建索引 / sync 同步 / checkout 取版本 / ls")
    sub_base_sub = sub_base.add_subparsers(dest="baseline_cmd", required=True)
    b_add = sub_base_sub.add_parser("add", help="一条命令:登记基线(role=baseline,git url/branch 自动读)+ 建索引")
    b_add.add_argument("path", help="代码仓路径(如 ~/codebases/v20/bluez → 基线 bluez-v20)")
    b_add.add_argument("--name", default=None,
                       help="基线名(默认=相对总目录 ROOTRECALL_CODEBASES 的路径倒序连 '-';systemd → systemd)")
    b_add.add_argument("--force", action="store_true", help="索引强制全量重建")
    b_add.add_argument("--no-graph", action="store_true", help="只建向量索引,不建结构图(快)")
    b_add.add_argument("--graph-only", action="store_true",
                       help="只建结构图,不碰 embedder 与向量索引(零 key 可用)")
    b_add.set_defaults(func=cmd_baseline_add)
    b_sync = sub_base_sub.add_parser("sync", help="基线同步:fetch→ff→增量刷索引→(可选)上游三态报告;缺省=全部基线")
    b_sync.add_argument("names", nargs="*", help="要同步的基线名(缺省=全部 baseline)")
    b_sync.add_argument("--analyze", default=None, metavar="FORK名",
                        help="对哪个注册仓做上游三态分析(如 --analyze bluez-v20;fork 须有本地检出)")
    b_sync.add_argument("--analyze-agent", action="store_true",
                        help="三态报告后再跑 headless opencode 复核「该不该合」并追加进报告(需与 --analyze 同用)")
    b_sync.add_argument("--ingest-report", action="store_true",
                        help="把三态报告摄取进记忆库(需与 --analyze 同用)")
    b_sync.add_argument("--no-index", action="store_true", help="跳过索引刷新(没配 embedding key 时)")
    b_sync.set_defaults(func=cmd_repo, repo_cmd="sync")
    b_co = sub_base_sub.add_parser("checkout", help="从基线取指定版本的一次性检出(worktree 秒开,登记 ephemeral)")
    b_co.add_argument("name", help="检出注册名(=索引名,如 bluez-v20-5.50.61)")
    b_co.add_argument("--from", dest="from_repo", required=True, help="基线注册名(baseline add 登记的)")
    b_co.add_argument("--ref", required=True, help="检出的 ref(分支/tag/commit,如 5.50.61-deepin1)")
    b_co.add_argument("--bug", default=None, help="关联 bug 标识(gc 报告里给人看)")
    b_co.add_argument("--dest", default=None, help="落点(默认 data/worktrees/<name>)")
    b_co.add_argument("--index", action="store_true",
                      help="开仓顺手建索引:播种基线索引后增量建(embedder 不可用则诚实跳过,不挡检出)")
    b_co.set_defaults(func=cmd_repo, repo_cmd="checkout")
    b_ls = sub_base_sub.add_parser("ls", help="列出全部受管仓(基线/检出/未管理)")
    b_ls.set_defaults(func=cmd_repo, repo_cmd="ls")

    # ── 日常面:here / install ────────────────────────────────────────────────
    sub_install = sub.add_parser("install", help="opencode 全局注册/卸载(skills+mcp+agent块+AGENTS.md 四件套,全机一次)")
    sub_install.add_argument("--global", dest="global_", action="store_true", help="装进 ~/.config/opencode(唯一模式)")
    sub_install.add_argument("--uninstall", action="store_true", help="卸载全局注册(只摘自己写的)")
    sub_install.set_defaults(func=cmd_install)

    sub_here = sub.add_parser("here", help="在当前 bug/工作目录做轻量标记(.rootrecall.yaml + 项目 opencode.json)")
    sub_here.add_argument("--codebase", default=None, help="该目录会话的默认检索索引名(免每次显式传)")
    sub_here.set_defaults(func=cmd_here)

    # ── 进阶面:不进 --help 展示但完全可用(自动化/排障;systemd 调 repo sync、opencode 拉起 mcp serve)──
    sub_models = sub.add_parser("models", help="[进阶] 列出 config.yaml 中配置的模型")
    sub_models.set_defaults(func=cmd_models)

    sub_oc = sub.add_parser(
        "opencode-models",
        help="探测宿主 opencode 的 chat 模型(url+key 复用,key 不落盘);--adopt 写进 config")
    sub_oc.add_argument("--adopt", nargs="+", metavar="PROVIDER/MODEL",
                        help="采纳指定模型(可多个;整块覆盖语义,重跑=刷新全集)")
    sub_oc.set_defaults(func=cmd_opencode_models)

    sub_index = sub.add_parser("index", help="[进阶] 为仓库建索引(向量索引 + 结构图,一次到位)")
    sub_index.add_argument("repo_path", help="仓库根目录路径")
    sub_index.add_argument("repo_name", nargs="?", default=None,
                           help="索引名(默认取目录名;须与 code_index.repo 一致)")
    sub_index.add_argument("--force", action="store_true", help="强制全量重建(向量索引 + 结构图)")
    sub_index.add_argument("--seed", default=None, metavar="已有索引名",
                           help="从同线基线索引播种(拷贝向量库+结构图再增量,只重嵌差异文件;省时省钱)")
    sub_index.add_argument("--no-graph", action="store_true",
                           help="只建向量索引,不建结构图(快;blast_radius/call_chain/repo_map/repo_overview 将不可用)")
    sub_index.add_argument("--graph-only", action="store_true",
                           help="只建结构图,不碰 embedder 与向量索引(零 key 可用;search_codebase 将不可用)")
    sub_index.set_defaults(func=cmd_index)

    sub_repo = sub.add_parser("repo", help="[进阶] 仓库注册表:ls/register/rm/resolve + checkout/gc/sync")
    sub_repo_sub = sub_repo.add_subparsers(dest="repo_cmd", required=True)
    r_ls = sub_repo_sub.add_parser("ls", help="列出全部受管仓(角色/路径/索引名)")
    r_ls.set_defaults(func=cmd_repo, repo_cmd="ls")
    r_reg = sub_repo_sub.add_parser("register", help="登记/更新一个仓(upsert:未传字段保留现值)")
    r_reg.add_argument("name", help="注册名(约定=索引名,如 bluez-v20 / bluez-v20-5.50.61)")
    r_reg.add_argument("--path", default=None, help="工作树绝对路径")
    r_reg.add_argument("--url", default=None, help="git remote(clone/sync 用)")
    r_reg.add_argument("--role", default=None, choices=["baseline", "ephemeral", "unmanaged"],
                       help="baseline=共享基线(永久+sync)| ephemeral=一次性 bug 检出(gc 清)| unmanaged"
                            "(缺省=保留现值;新记录默认 unmanaged)")
    r_reg.add_argument("--branch", default=None, help="锁定的分支/tag")
    r_reg.add_argument("--bug", default=None, help="ephemeral:关联 bug 标识")
    r_reg.add_argument("--codebase", default=None, help="检索索引名(与 name 不同才填)")
    r_reg.add_argument("--note", default=None)
    r_reg.set_defaults(func=cmd_repo, repo_cmd="register")
    r_rm = sub_repo_sub.add_parser("rm", help="移除记录(只删记录,不删盘上文件)")
    r_rm.add_argument("name")
    r_rm.set_defaults(func=cmd_repo, repo_cmd="rm")
    r_res = sub_repo_sub.add_parser("resolve", help="名字/路径 → 本地绝对路径反查")
    r_res.add_argument("name_or_path")
    r_res.set_defaults(func=cmd_repo, repo_cmd="resolve")
    r_co = sub_repo_sub.add_parser("checkout", help="从基线开一次性检出(git worktree 共享对象库,登记 ephemeral)")
    r_co.add_argument("name", help="检出注册名(=索引名,如 bluez-v20-5.50.61)")
    r_co.add_argument("--from", dest="from_repo", required=True, help="基线注册名(须已 register --role baseline --url)")
    r_co.add_argument("--ref", required=True, help="检出的 ref(分支/tag/commit,如 5.50.61)")
    r_co.add_argument("--bug", default=None, help="关联 bug 标识(gc 报告里给人看)")
    r_co.add_argument("--dest", default=None, help="落点(默认 data/worktrees/<name>)")
    r_co.add_argument("--index", action="store_true",
                      help="开仓顺手建索引:播种基线索引后增量建(embedder 不可用则诚实跳过,不挡检出)")
    r_co.set_defaults(func=cmd_repo, repo_cmd="checkout")
    r_gc = sub_repo_sub.add_parser("gc", help="回收过期 ephemeral 仓(级联:worktree+向量索引+结构图+记录;记忆不删)")
    r_gc.add_argument("--max-age-days", type=int, default=14, help="ephemeral 到期天数(默认 14)")
    r_gc.add_argument("--dry-run", action="store_true", help="只列要删什么,不动手")
    r_gc.add_argument("--name", default=None, help="点名强删某个(忽略年龄)")
    r_gc.add_argument("--prune-orphans", action="store_true", help="顺带删孤儿索引(源仓已不在盘上的)")
    r_gc.set_defaults(func=cmd_repo, repo_cmd="gc")
    r_sync = sub_repo_sub.add_parser("sync", help="基线仓同步:fetch→ff→增量刷索引→(可选)上游三态分析报告")
    r_sync.add_argument("names", nargs="*", help="要同步的基线名(缺省=全部 baseline)")
    r_sync.add_argument("--analyze", default=None, metavar="FORK名",
                        help="对哪个注册仓做上游三态分析(如 --analyze bluez-v20;fork 须有本地检出)")
    r_sync.add_argument("--analyze-agent", action="store_true",
                        help="三态报告后再跑 headless opencode 复核「该不该合」并追加进报告"
                             "(需与 --analyze 同用;opencode 不在/失败诚实退纯三态)")
    r_sync.add_argument("--ingest-report", action="store_true",
                        help="把三态报告(含 agent 复核)摄取进记忆库,recall 可带出「上次评估为什么没合」"
                             "(需与 --analyze 同用;codebase 取项目名,如 bluez-v20 → bluez)")
    r_sync.add_argument("--no-index", action="store_true", help="跳过索引刷新(没配 embedding key 时)")
    r_sync.set_defaults(func=cmd_repo, repo_cmd="sync")

    sub_lsp = sub.add_parser("lsp", help="[进阶] L2 精确导航(clangd):health 自检 / refs 冒烟")
    sub_lsp_sub = sub_lsp.add_subparsers(dest="lsp_cmd", required=True)
    sub_lsp_health = sub_lsp_sub.add_parser("health", help="检测 clangd + compile_commands 是否就位")
    sub_lsp_health.add_argument("repo_root", nargs="?", default=None, help="仓库根(默认 workspace)")
    sub_lsp_refs = sub_lsp_sub.add_parser("refs", help="冒烟:打一次 references")
    sub_lsp_refs.add_argument("file", help="文件路径")
    sub_lsp_refs.add_argument("line", type=int, help="行号(1-based)")
    sub_lsp_refs.add_argument("col", type=int, help="列号(1-based)")
    sub_lsp_refs.add_argument("repo_root", nargs="?", default=None, help="仓库根(默认 workspace)")
    sub_lsp.set_defaults(func=cmd_lsp)

    sub_memory = sub.add_parser("memory", help="[进阶] 记忆核心:recall/add/ingest/list/consolidate/invalidate/backfill")
    sub_memory_sub = sub_memory.add_subparsers(dest="memory_cmd", required=True)
    m_recall = sub_memory_sub.add_parser("recall", help="翻记忆(多路召回)")
    m_recall.add_argument("query", help="自然语言查询")
    m_recall.add_argument("--top-k", type=int, default=5)
    m_recall.add_argument("--repo", default=None, help="代码库(默认 config.code_index.repo)")
    m_add = sub_memory_sub.add_parser("add", help="记一条(或 --from-report 从报告抽)")
    m_add.add_argument("--kind", default="bug_lesson", choices=["bug_lesson", "codebase_fact", "domain_knowledge"])
    m_add.add_argument("--summary", default=None, help="一句话摘要(直接记时必填)")
    m_add.add_argument("--root-cause", default="")
    m_add.add_argument("--detail", default="")
    m_add.add_argument("--file", default=None)
    m_add.add_argument("--line", type=int, default=None)
    m_add.add_argument("--source-url", default=None, help="外部溯源 URL(domain_knowledge 网调知识用)")
    m_add.add_argument("--from-report", default=None, help="从报告文件抽(走 LLM extract)")
    m_add.add_argument("--commit-sha", default=None)
    m_add.add_argument("--repo", default=None)
    m_ingest = sub_memory_sub.add_parser("ingest", help="摄取文档(bug 报告/调研报告/补丁)→ 记忆(R3.4)")
    m_ingest.add_argument("path", help="文档路径(.md/.txt/.pdf/.patch/.diff)")
    m_ingest.add_argument("--kind", default="auto", choices=["auto", "report", "patch"],
                          help="auto=按扩展名判定(补丁走 retrieve-then-summarize,报告走 extract)")
    m_ingest.add_argument("--source-tier", default="imported", choices=["imported", "stated", "inferred"],
                          help="来源可信度(默认 imported)")
    m_ingest.add_argument("--commit-sha", default=None)
    m_ingest.add_argument("--repo", default=None)
    m_list = sub_memory_sub.add_parser("list", help="列知识项")
    m_list.add_argument("--kind", default=None)
    m_list.add_argument("--include-invalid", action="store_true")
    m_list.add_argument("--repo", default=None)
    m_consol = sub_memory_sub.add_parser("consolidate", help="巩固(升级/矛盾/去重/已合入/过期 五 pass)")
    m_consol.add_argument("--repo-path", default=None, help="git 仓绝对路径(给了才做『补丁已合入上游』检测)")
    m_consol.add_argument("--repo", default=None)
    m_inv = sub_memory_sub.add_parser("invalidate", help="失效一条")
    m_inv.add_argument("id")
    m_inv.add_argument("--reason", default="")
    m_inv.add_argument("--repo", default=None)
    m_bf = sub_memory_sub.add_parser(
        "backfill", help="补嵌零 key 期间写入的记忆(只补向量列,不触发合并;幂等可重跑)")
    m_bf.add_argument("--dry-run", action="store_true", help="只列待补条目,不真嵌")
    m_bf.add_argument("--repo", default=None)
    sub_memory.set_defaults(func=cmd_memory)

    sub_mcp = sub.add_parser("mcp", help="[进阶] MCP server(把 RootRecall 能力做成工具给 coding agent 调)")
    sub_mcp_sub = sub_mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_serve = sub_mcp_sub.add_parser("serve", help="启动 MCP server(stdio 默认;--transport http 起 warm 长进程)")
    mcp_serve.add_argument("--codebase", default=None, help="查哪个代码库的索引/记忆(= 建索引时的 name);默认 config.code_index.repo")
    mcp_serve.add_argument("--transport", default=None, choices=["stdio", "http"], help="stdio(默认,子进程 1:1)| http(streamable-http,warm 多客户端,解 cold-boot)")
    mcp_serve.add_argument("--host", default=None, help="http 绑定地址(默认 127.0.0.1;config.mcp.host)")
    mcp_serve.add_argument("--port", type=int, default=None, help="http 端口(默认 8765;config.mcp.port)")
    sub_mcp.set_defaults(func=cmd_mcp)

    # 进阶命令不进展示:argparse 的 help=SUPPRESS 对子命令在 py3.12 会打出字面量、usage 仍列
    # 全名,故注册完后手动从选择列表摘掉(命令本身保留 —— systemd 调 repo sync、opencode 拉起 mcp serve)。
    _visible = ("baseline", "install", "here")
    sub._choices_actions = [a for a in sub._choices_actions if a.dest in _visible]
    sub.metavar = "{" + ",".join(_visible) + "}"

    args = parser.parse_args(argv)
    if getattr(args, "func", None):
        try:
            return args.func(args)
        except OSError as e:
            # 跨机拷 .env 的坑:ROOTRECALL_HOME 指向别的机器的家目录 → 数据落盘时深层 PermissionError。
            # 只在出错路径与 ROOTRECALL_HOME 相关时给指路信息,其余 OSError 原样抛(不吞真 bug)。
            import os

            fn = str(getattr(e, "filename", "") or "")
            rh = os.environ.get("ROOTRECALL_HOME", "")
            if rh and fn and (rh.startswith(fn) or fn.startswith(rh.rstrip("/"))):
                print(f"错误:路径不可访问({fn})。\n"
                      f"  疑点:.env 里的 ROOTRECALL_HOME={rh} 指向本机不存在/无权限的路径"
                      f"(多半是整份拷了别的机器的 .env)—— 改成本机路径,或删掉该行用仓内 data/,重跑。",
                      file=sys.stderr)
                return 1
            raise
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
