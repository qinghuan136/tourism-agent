"""北京对话 RAG 离线评测：只读快照、真实候选与评分采集、离线阈值分析。

运行方式：python -m scripts.evaluate_conversation_rag snapshot|collect|analyze。
原始历史和向量缓存仅写入被 Git 忽略的 .tmp，业务数据库始终只读。
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from tourism_agent.infrastructure.database import DatabaseSettings, PostgresDatabase
from tourism_agent.providers.model import (
    ModelSettings,
    create_chat_model,
    create_embedding_model,
)
from tourism_agent.providers.reranker import RERANK_MODEL, create_qwen_reranker
from tourism_agent.repositories.planning import PlanningRepository
from tourism_agent.services.conversation_retrieval import _cosine_similarity
from tourism_agent.services.semantic_enhancement import (
    QUERY_ENHANCEMENT_PROMPT,
    SemanticEnhancementService,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "docs/beijing_conversation_rag_golden_dataset_v1.md"
MANIFEST = ROOT / "evals/beijing_rag_v1.json"
DEFAULT_OUTPUT = ROOT / ".tmp/rag-eval-beijing-v1"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    """每次调用后保存中间结果，进程失败可续跑且不重复支付已完成调用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def load_queries():
    """保留原始题目及标签，校对标签独立存放，并在评分前固定分组。"""
    manifest = read_json(MANIFEST)
    queries = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if not re.match(r"\| Q\d{3} \|", line):
            continue
        qid, query, labels, focus = [part.strip() for part in line.split("|")[1:5]]
        original = json.loads(labels.strip("`"))
        override = manifest["label_overrides"].get(qid, {})
        queries.append(
            {
                "qid": qid,
                "query": query,
                "original_labels": original,
                "labels": override.get("ids", original),
                "focus": focus,
                "audit_reason": override.get("reason", ""),
                "split": "calibration" if qid in manifest["calibration_ids"] else "validation",
            }
        )
    if len(queries) != 50 or len({q["qid"] for q in queries}) != 50:
        raise ValueError("黄金数据集应包含50个不重复问题，请检查Markdown表格")
    known = set(manifest["expected_chunk_ids"])
    if any(not set(q["labels"]) <= known for q in queries):
        raise ValueError("标注引用了快照范围外的Chunk")
    return queries


def evaluate(labels, results):
    """Recall按正例宏平均；Precision按实际返回总数微平均，空分母为None。"""
    positive = hit = negative = false_positive = relevant = returned = 0
    recalls = []
    for qid, gold in labels.items():
        selected = results[qid]
        correct = len(set(selected) & set(gold))
        returned += len(selected)
        relevant += correct
        if gold:
            positive += 1
            hit += bool(correct)
            recalls.append(correct / len(gold))
        else:
            negative += 1
            false_positive += bool(selected)
    return {
        "query_count": len(labels),
        "positive_count": positive,
        "negative_count": negative,
        "hit_count": hit,
        "hit": hit / positive if positive else None,
        "recall": sum(recalls) / positive if positive else None,
        "relevant_returned": relevant,
        "returned": returned,
        "precision_micro": relevant / returned if returned else None,
        "no_answer_false_positive_count": false_positive,
        "no_answer_false_positive_rate": false_positive / negative if negative else None,
    }


def select_results(candidates, vectors, threshold, dedup, limit=3):
    """复现生产顺序：先按分数过滤排序，再去重，最后截断；保留同分原始顺序。"""
    selected = []
    for item in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if item["score"] < threshold:
            continue
        cid = item["id"]
        if dedup is not None and any(
            _cosine_similarity(vectors[cid], vectors[other]) >= dedup for other in selected
        ):
            continue
        selected.append(cid)
        if len(selected) == limit:
            break
    return selected


def tune_threshold(labels, scored):
    """只接收校准集；约束优先，不能靠高阈值清空正例来提高Precision。"""
    baseline = evaluate(
        labels, {qid: select_results(rows, {}, 0, None) for qid, rows in scored.items()}
    )

    def measure(threshold):
        result = {qid: select_results(rows, {}, threshold, None) for qid, rows in scored.items()}
        return {"threshold": threshold, **evaluate(labels, result)}

    def choose(rows):
        feasible = [
            r for r in rows if r["hit"] >= 0.9 and (r["no_answer_false_positive_rate"] or 0) <= 0.2
        ]
        eligible = feasible or [r for r in rows if r["hit"] >= min(0.9, baseline["hit"])]
        best = max(
            eligible,
            key=lambda r: (
                r["precision_micro"] or 0,
                r["hit"],
                -(r["no_answer_false_positive_rate"] or 0),
                -r["threshold"],
            ),
        )
        return best, bool(feasible)

    coarse = [measure(i / 20) for i in range(21)]
    best, _ = choose(coarse)
    center = round(best["threshold"] * 100)
    thresholds = sorted(
        {r["threshold"] for r in coarse}
        | {i / 100 for i in range(max(0, center - 5), min(100, center + 5) + 1)}
    )
    sweep = [measure(t) for t in thresholds]
    best, feasible = choose(sweep)
    return best["threshold"], feasible, sweep


def read_corpus():
    """明确限制用户/Trip，读取数据库真实ID与向量；不执行迁移或更新。"""
    manifest = read_json(MANIFEST)
    with psycopg.connect(
        **DatabaseSettings().connection_parameters(), connect_timeout=8, row_factory=dict_row
    ) as connection:
        connection.read_only = True
        rows = connection.execute(
            """SELECT c.id, c.exchange_id, c.user_message_id, c.assistant_message_id,
                      c.retrieval_text, c.retrieval_text_sha256, c.created_at,
                      c.embedding_model, c.enhancement_model, c.enhancement_version,
                      c.embedding::text AS embedding
               FROM tourism_agent.conversation_rag_chunks c
               JOIN tourism_agent.trips t ON t.id=c.trip_id
               WHERE c.trip_id=%s AND t.user_id=%s AND t.archived_at IS NULL
               ORDER BY c.id""",
            (manifest["trip_id"], manifest["user_id"]),
        ).fetchall()
        for row in rows:
            row["embedding"] = json.loads(row["embedding"])
            row["exchange_id"] = str(row["exchange_id"])
            row["created_at"] = row["created_at"].isoformat()
        if [r["id"] for r in rows] != manifest["expected_chunk_ids"]:
            raise ValueError("当前语料与冻结的41条ID不一致；请单独建立新评测快照")
        if any(len(r["embedding"]) != 1024 for r in rows):
            raise ValueError("快照必须全部为1024维向量")
        version = connection.execute("SELECT version() AS v").fetchone()["v"]
        vector = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()["extversion"]
    return {"chunks": rows, "postgres": version, "pgvector": vector}


def snapshot(output):
    corpus = read_corpus()
    settings = ModelSettings()
    queries = load_queries()
    configuration = {
        "manifest": read_json(MANIFEST),
        "queries": queries,
        "chat_model": settings.model_name,
        "embedding_model": "qwen3.7-text-embedding",
        "dimensions": 1024,
        "rerank_model": RERANK_MODEL,
        "enhancement_prompt_sha256": digest(QUERY_ENHANCEMENT_PROMPT),
        "endpoint_signature": digest([settings.base_url, settings.rerank_url]),
        "corpus_sha256": digest(corpus),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in [
                "src/tourism_agent/repositories/planning.py",
                "src/tourism_agent/providers/model.py",
                "src/tourism_agent/providers/reranker.py",
                "src/tourism_agent/services/semantic_enhancement.py",
                "src/tourism_agent/services/conversation_retrieval.py",
            ]
        },
    }
    fingerprint = digest(configuration)
    path = output / "snapshot.json"
    if path.exists():
        if read_json(path)["fingerprint"] != fingerprint:
            raise ValueError("语料/配置/标注已变更，不能复用旧缓存；请使用新 --output 目录")
        print("已核对现有快照，指纹一致", flush=True)
        return read_json(path)
    value = {
        "created_at": datetime.now(UTC).isoformat(),
        "fingerprint": fingerprint,
        "configuration": configuration,
        "corpus": corpus,
    }
    write_json(path, value)
    print(f"已冻结{len(corpus['chunks'])}条Chunk、{len(queries)}个Query；{fingerprint}", flush=True)
    return value


def candidate_choice(queries, records):
    """候选数仅依据校准集决定，验证集不参与任何选参。"""
    labels = {q["qid"]: q["labels"] for q in queries if q["split"] == "calibration"}
    metrics = {
        n: evaluate(
            labels, {qid: [c["id"] for c in records[qid]["candidates"][:n]] for qid in labels}
        )
        for n in (5, 10, 20)
    }
    eligible = [
        n
        for n in metrics
        if metrics[n]["recall"] >= 0.95 and metrics[n]["recall"] >= metrics[20]["recall"] - 0.01
    ]
    return min(eligible) if eligible else 20, metrics


async def collect(output):
    frozen = snapshot(output)
    queries = frozen["configuration"]["queries"]
    manifest = frozen["configuration"]["manifest"]
    exchange_map = {c["exchange_id"]: c for c in frozen["corpus"]["chunks"]}
    settings = ModelSettings()
    model = create_chat_model(settings)
    embeddings = create_embedding_model(settings)
    enhancer = SemanticEnhancementService(model, model_name=settings.model_name)
    reranker = create_qwen_reranker(settings)
    database = PostgresDatabase(DatabaseSettings())
    records = {}
    semaphore = asyncio.Semaphore(2)
    await database.open()
    repository = PlanningRepository(database)

    async def retrieve(q):
        async with semaphore:
            path = output / "queries" / (q["qid"] + ".json")
            row = read_json(path) if path.exists() else {"qid": q["qid"], "query": q["query"]}
            if "enhanced_query" not in row:
                row["enhanced_query"] = await asyncio.wait_for(
                    enhancer.enhance_query(
                        query=q["query"],
                        current_user_input=q["query"],
                        task_goal=q["query"],
                        recent_conversation=(),
                    ),
                    timeout=120,
                )
                write_json(path, row)
            if "embedding" not in row:
                row["embedding"] = await asyncio.wait_for(
                    embeddings.aembed_query(row["enhanced_query"]), timeout=90
                )
                if len(row["embedding"]) != 1024:
                    raise ValueError("查询Embedding不是1024维")
                write_json(path, row)
            if "candidates" not in row:
                candidates = await repository.search_conversation_chunks(
                    UUID(manifest["user_id"]), UUID(manifest["trip_id"]), row["embedding"], 20, []
                )
                row["candidates"] = []
                for c in candidates:
                    original = exchange_map[str(c.exchange_id)]
                    if original["retrieval_text"] != c.retrieval_text:
                        raise ValueError("采集期间Chunk文本已改变，请使用新快照")
                    row["candidates"].append({"id": original["id"], "similarity": c.similarity})
                write_json(path, row)
            records[q["qid"]] = row
            print(f"召回完成 {q['qid']}", flush=True)

    async def score(q, candidate_k):
        async with semaphore:
            row = records[q["qid"]]
            if "scored" not in row:
                by_id = {c["id"]: c for c in frozen["corpus"]["chunks"]}
                selected = row["candidates"][:candidate_k]
                scores = await asyncio.wait_for(
                    reranker.rerank(
                        query=row["enhanced_query"],
                        documents=[by_id[c["id"]]["retrieval_text"] for c in selected],
                    ),
                    timeout=120,
                )
                row["scored"] = [{**c, "score": s} for c, s in zip(selected, scores, strict=True)]
                row["candidate_k"] = candidate_k
                row["scored_at"] = datetime.now(UTC).isoformat()
                write_json(output / "queries" / (q["qid"] + ".json"), row)
            elif row["candidate_k"] != candidate_k:
                raise ValueError("缓存候选数不一致，请使用新输出目录")
            print(f"重排完成 {q['qid']}", flush=True)

    try:
        await asyncio.gather(*(retrieve(q) for q in queries))
        candidate_k, candidate_metrics = candidate_choice(queries, records)
        print(f"校准集选定 candidate_k={candidate_k}；Recall={candidate_metrics}", flush=True)
        await asyncio.gather(*(score(q, candidate_k) for q in queries))
        if digest(read_corpus()) != frozen["configuration"]["corpus_sha256"]:
            raise ValueError("采集过程中语料发生变化，本次结果不能用于报告")
        write_json(
            output / "collection_complete.json",
            {
                "candidate_k": candidate_k,
                "completed_at": datetime.now(UTC).isoformat(),
                "fingerprint": frozen["fingerprint"],
            },
        )
    finally:
        await reranker.aclose()
        await database.close()


def analyze(output):
    """只读取缓存进行阈值扫描；阈值选择完成后才汇总验证集。"""
    frozen = read_json(output / "snapshot.json")
    completed = read_json(output / "collection_complete.json")
    if completed["fingerprint"] != frozen["fingerprint"]:
        raise ValueError("采集记录与快照不一致")
    queries = frozen["configuration"]["queries"]
    records = {q["qid"]: read_json(output / "queries" / (q["qid"] + ".json")) for q in queries}
    vectors = {c["id"]: c["embedding"] for c in frozen["corpus"]["chunks"]}
    calibration = {q["qid"]: q["labels"] for q in queries if q["split"] == "calibration"}
    scored = {qid: records[qid]["scored"] for qid in calibration}
    threshold, feasible, sweep = tune_threshold(calibration, scored)
    parameters = {
        "candidate_k": completed["candidate_k"],
        "threshold": threshold,
        "dedup": 0.98,
        "limit": 3,
        "calibration_targets_met": feasible,
    }
    write_json(output / "selected_parameters.json", parameters)
    variants = {"vector_top3": {}, "rerank_t0": {}, "rerank_tuned": {}, "complete": {}}
    for q in queries:
        qid = q["qid"]
        variants["vector_top3"][qid] = [c["id"] for c in records[qid]["candidates"][:3]]
        for name, t, d in [
            ("rerank_t0", 0, None),
            ("rerank_tuned", threshold, None),
            ("complete", threshold, 0.98),
        ]:
            variants[name][qid] = select_results(records[qid]["scored"], vectors, t, d)
    metrics = {}
    for label_field in ("labels", "original_labels"):
        metrics[label_field] = {}
        for split in ("calibration", "validation", "all"):
            labels = {
                q["qid"]: q[label_field] for q in queries if split == "all" or q["split"] == split
            }
            metrics[label_field][split] = {
                **{
                    f"vector_recall_{n}": evaluate(
                        labels,
                        {qid: [c["id"] for c in records[qid]["candidates"][:n]] for qid in labels},
                    )
                    for n in (5, 10, 20)
                },
                **{name: evaluate(labels, result) for name, result in variants.items()},
            }
    write_json(output / "metrics.json", {"parameters": parameters, "metrics": metrics})
    write_json(output / "threshold_sweep.json", sweep)
    details = []
    for q in queries:
        selected = variants["complete"][q["qid"]]
        details.append(
            {
                **q,
                "enhanced_query": records[q["qid"]]["enhanced_query"],
                "selected": selected,
                "correct": sorted(set(selected) & set(q["labels"])),
                "without_dedup": variants["rerank_tuned"][q["qid"]],
                "scored": records[q["qid"]]["scored"],
            }
        )
    write_json(output / "details.json", details)
    with (output / "threshold_sweep.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sweep[0]))
        writer.writeheader()
        writer.writerows(sweep)
    write_report(output, frozen, parameters, metrics, sweep, details)
    print(
        json.dumps(
            {"parameters": parameters, "reviewed": metrics["labels"]}, ensure_ascii=False, indent=2
        ),
        flush=True,
    )


def percent(value):
    return "N/A" if value is None else f"{value:.1%}"


def write_report(output, frozen, parameters, metrics, sweep, details):
    """报告、评分和标注审计可公开审阅；原文快照和向量继续留在本地缓存。"""
    public = ROOT / "evals/results/beijing_v1"
    public.mkdir(parents=True, exist_ok=True)
    configuration = frozen["configuration"]
    metadata = {
        key: configuration[key]
        for key in (
            "chat_model",
            "embedding_model",
            "dimensions",
            "rerank_model",
            "enhancement_prompt_sha256",
            "corpus_sha256",
            "source_sha256",
        )
    }
    metadata.update(
        {
            "snapshot_time": frozen["created_at"],
            "snapshot_fingerprint": frozen["fingerprint"],
            "pgvector": frozen["corpus"]["pgvector"],
            "postgres": frozen["corpus"]["postgres"],
        }
    )
    write_json(
        public / "metrics.json",
        {"metadata": metadata, "parameters": parameters, "metrics": metrics},
    )
    write_json(public / "query_results.json", details)
    write_json(
        public / "chunk_mapping.json",
        [
            {
                key: c[key]
                for key in (
                    "id",
                    "exchange_id",
                    "user_message_id",
                    "assistant_message_id",
                    "retrieval_text_sha256",
                    "created_at",
                    "enhancement_model",
                )
            }
            for c in frozen["corpus"]["chunks"]
        ],
    )
    (public / "threshold_sweep.csv").write_bytes((output / "threshold_sweep.csv").read_bytes())
    calibrated = metrics["labels"]["calibration"]
    validation = metrics["labels"]["validation"]
    final = validation["complete"]
    names = {
        "vector_top3": "仅向量Top-3",
        "rerank_t0": "重排，阈值0，无去重",
        "rerank_tuned": "重排，选定阈值，无去重",
        "complete": "重排，选定阈值，去重0.98",
    }
    lines = [
        "# 北京对话 RAG 实测报告 v1",
        "",
        (f"快照时间：{frozen['created_at']}。数据库实际41个Chunk，50个问题；"
        "所有模型分数来自真实调用，阈值扫描仅使用已缓存分数。"),
        "",
        "## 1. 结果与参数",
        "",
        (f"校准集选择候选数 **{parameters['candidate_k']}**、阈值 **{parameters['threshold']:.2f}**，"
        "最终最多返回3条，语义去重阈值保留0.98。未修改线上配置。"),
        "",
        (f"完整链路验证集：Hit@3 **{percent(final['hit'])}**，微平均Precision "
        f"**{percent(final['precision_micro'])}**，无答案误召回 "
        f"**{final['no_answer_false_positive_count']}/{final['negative_count']}**。"),
        "",
        (
            "校准集同时达到预设Hit≥90%、无答案误召回≤20%的目标。"
            if parameters["calibration_targets_met"]
            else "**校准集无法同时达到Hit≥90%、无答案误召回≤20%的目标。选定值仅是保留正例命中的折中，"
            "不能视为合格的拒答阈值。**"
        ),
        "",
        "## 2. 评测条件",
        "",
        f"- 查询增强：`{configuration['chat_model']}`，复用生产Prompt；每题只增强一次并缓存。",
        "- query、current_user_input和task_goal均为该题原始Query；不注入数据库最新对话。",
        "- 原始Chunk向量不重算；查询Embedding为`qwen3.7-text-embedding`，1024维。",
        ("- 真实调用生产Repository，在当前user_id/trip_id作用域按pgvector余弦距离排序；"
        "exclude_exchange_ids为空。未增加混合检索。"),
        "- Reranker：`qwen3.7-text-rerank`；复用现有DashScope配置及生产客户端，输入增强Query和候选文本。",
        "- 语义去重复用生产余弦函数，按分数降序保留，`score >= threshold`，去重后再Top-3。",
        (f"- 校对后校准集：{calibrated['complete']['positive_count']}有答案 / "
        f"{calibrated['complete']['negative_count']}无答案；验证集："
        f"{final['positive_count']}有答案 / {final['negative_count']}无答案。"),
        "- 本轮衡量历史证据的检索质量，不衡量LLM回答、外部旅行事实真伪或行程是否已保存。",
        "",
        "## 3. 候选召回率",
        "",
        "Recall按每个有答案问题的相关Chunk覆盖比例取宏平均，无答案问题不进入分母。",
        "",
        "| 候选数N | 校准集Recall@N | 验证集Recall@N |",
        "| --- | --- | --- |",
    ]
    for n in (5, 10, 20):
        lines.append(
            f"| {n} | {percent(calibrated[f'vector_recall_{n}']['recall'])} | "
            f"{percent(validation[f'vector_recall_{n}']['recall'])} |"
        )
    lines += [
        "",
        ("候选数只根据校准集选择：取Recall≥95%、且相对Top-20下降不超过1个百分点的最小N；"
        "没有满足条件的N时保留20。验证集未参与选参。"),
        "",
        "## 4. 最终返回质量",
        "",
        ("Precision采用总相关返回数/总实际返回数；返回空集合不贡献分母，全部为空记N/A。"
        "Hit@3只计算有答案问题；无答案误召回率是无答案问题中仍返回内容的比例。"),
        "",
        "| 集合 | 链路 | Hit@3 | 微平均Precision | 无答案误召回 | 返回片段数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for split, title in (("calibration", "校准"), ("validation", "验证")):
        for name, label in names.items():
            m = metrics["labels"][split][name]
            lines.append(
                f"| {title} | {label} | {percent(m['hit'])} | "
                f"{percent(m['precision_micro'])} | {m['no_answer_false_positive_count']}/"
                f"{m['negative_count']} ({percent(m['no_answer_false_positive_rate'])}) | "
                f"{m['returned']} |"
            )
    lines += [
        "",
        "## 5. 阈值扫描（仅校准集）",
        "",
        configuration["manifest"]["selection_rule"],
        "",
        "| threshold | Hit@3 | 微平均Precision | 无答案误召回 | 返回片段数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in sweep:
        lines.append(
            f"| {row['threshold']:.2f} | {percent(row['hit'])} | "
            f"{percent(row['precision_micro'])} | {row['no_answer_false_positive_count']}/"
            f"{row['negative_count']} | {row['returned']} |"
        )
    changed = [d for d in details if d["selected"] != d["without_dedup"]]
    lines += [
        "",
        "## 6. 去重检查",
        "",
        f"固定0.98去重阈值使 **{len(changed)}/50** 个问题的最终返回发生变化。",
        "",
        "| Query | 去重前 | 去重后 | 校对相关ID |",
        "| --- | --- | --- | --- |",
    ]
    for d in changed:
        lines.append(f"| {d['qid']} | {d['without_dedup']} | {d['selected']} | {d['labels']} |")
    if not changed:
        lines.append("| 无 | — | — | — |")
    lines += [
        "",
        "## 7. 原始标注与校对标注的敏感性",
        "",
        ("原Markdown保持不变，校对在观察模型分数之前完成。相同输出分别用两套标签计分，"
        "下表差异来自标签，不代表模型升级带来的提升。"),
        "",
        "| 标注 | 集合 | Hit@3 | 微平均Precision | 无答案误召回 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for field, label in (("original_labels", "原始"), ("labels", "校对")):
        for split, title in (("calibration", "校准"), ("validation", "验证")):
            m = metrics[field][split]["complete"]
            lines.append(
                f"| {label} | {title} | {percent(m['hit'])} | "
                f"{percent(m['precision_micro'])} | {m['no_answer_false_positive_count']}/"
                f"{m['negative_count']} |"
            )
    lines += [
        "",
        "标注变更明细：",
        "",
        "| Query | 原ID | 校对ID | 理由 |",
        "| --- | --- | --- | --- |",
    ]
    for d in details:
        if d["audit_reason"]:
            lines.append(
                f"| {d['qid']} | {d['original_labels']} | {d['labels']} | {d['audit_reason']} |"
            )
    lines += [
        "",
        "## 8. 失败案例",
        "",
        "以下列出完整链路的正例全未命中与无答案误召回。部分相关结果混入的错误可在完整评分明细核查。",
        "",
        "| Query | 集合 | 原查询 | 返回ID及分数 | 正确ID |",
        "| --- | --- | --- | --- | --- |",
    ]
    failures = [
        d
        for d in details
        if (d["labels"] and not d["correct"]) or (not d["labels"] and d["selected"])
    ]
    for d in failures:
        scores = {c["id"]: c["score"] for c in d["scored"]}
        selected = ", ".join(f"{cid}:{scores[cid]:.4f}" for cid in d["selected"]) or "空"
        lines.append(f"| {d['qid']} | {d['split']} | {d['query']} | {selected} | {d['labels']} |")
    if not failures:
        lines.append("| 无 | — | — | — | — |")
    lines += ["", "## 9. 局限与复现", ""]
    lines += [f"- {note}" for note in configuration["manifest"]["scope_notes"]]
    lines += [
        ("- 同一北京旅行、仅50题，不足以证明对其他用户/城市/输入分布泛化。验证集无答案仅4题，"
        "每错1题误召回率就增加25个百分点，不能将0/4解释为真实错误率为0。"),
        "- 原有Chunk语义增强可能压缩、复述甚至混淆事实；本次不重新索引，也没有用线上最近历史做答案泄漏。",
        "- 分数不等于概率；阈值是当前模型、语料与输入设置下的初始候选值。",
        "",
        "运行命令（项目根目录，真实采集会产生模型费用）：",
        "",
        "```powershell",
        ".\\.venv\\Scripts\\python.exe -m scripts.evaluate_conversation_rag snapshot",
        ".\\.venv\\Scripts\\python.exe -m scripts.evaluate_conversation_rag collect",
        ".\\.venv\\Scripts\\python.exe -m scripts.evaluate_conversation_rag analyze",
        "```",
        "",
        "采集成功后重复运行analyze不访问数据库或模型。语料/标注/模型配置变化需要使用新的`--output`目录。",
        "",
        "结果文件：",
        "",
        "- [机器可读指标](../evals/results/beijing_v1/metrics.json)",
        "- [50题完整结果与候选分数](../evals/results/beijing_v1/query_results.json)",
        "- [阈值扫描CSV](../evals/results/beijing_v1/threshold_sweep.csv)",
        "- [Chunk ID映射](../evals/results/beijing_v1/chunk_mapping.json)",
        "- [校对标注与预设选参规则](../evals/beijing_rag_v1.json)",
        "",
        f"快照SHA-256：`{frozen['fingerprint']}`。本地完整快照/查询向量缓存位于`.tmp/rag-eval-beijing-v1/`。",
    ]
    (ROOT / "docs/beijing_conversation_rag_evaluation_v1.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["snapshot", "collect", "analyze"])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.stage == "snapshot":
        snapshot(args.output)
    elif args.stage == "collect":
        loop_factory = asyncio.SelectorEventLoop if os.name == "nt" else None
        asyncio.run(collect(args.output), loop_factory=loop_factory)
    else:
        analyze(args.output)


if __name__ == "__main__":
    main()
