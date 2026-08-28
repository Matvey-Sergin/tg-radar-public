import argparse
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from ai_engine import AIEngine
from database import Database
from it_radar_rules import first_money_score, normalize_wtp, reachability_score, trend_from_dates
from logging_utils import log_event
from quality_rules import clip_text, insight_quality_grade, pain_rejection_reason, source_key
from runtime_settings import setting, speed_label
from text_encoding import repair_json_strings


ai = AIEngine()
MIN_VALID_ROWS = setting("BUTCHER_MIN_VALID_ROWS")
MIN_CLUSTER_SIZE = 2
BATCH_LIMIT = setting("BUTCHER_BATCH_LIMIT")
EVIDENCE_CHARS = setting("BUTCHER_EVIDENCE_CHARS")
CONTEXT_CHARS = setting("BUTCHER_CONTEXT_CHARS")
EMPTY_SLEEP = setting("BUTCHER_EMPTY_SLEEP")
NOT_ENOUGH_SLEEP = setting("BUTCHER_NOT_ENOUGH_SLEEP")
INVALID_RESPONSE_SLEEP = setting("BUTCHER_INVALID_RESPONSE_SLEEP")
CYCLE_SLEEP = setting("BUTCHER_CYCLE_SLEEP")


def parse_json_result(raw):
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return repair_json_strings(data) if isinstance(data, dict) else {}


STOP_WORDS = {
    "котор", "чтобы", "через", "из-за", "этого", "этот", "такая", "такой", "нужн",
    "проблем", "невозмож", "разработ", "человек", "сервис", "систем", "использ",
}


def _topic_tokens(value):
    words = re.findall(r"[a-zа-яё0-9+#.]{4,}", str(value or "").lower())
    return {word[:7] for word in words if word[:5] not in STOP_WORDS}


def coherent_cluster_rows(core_problem, rows):
    """Remove accidental LLM outliers before accepting a cluster."""
    if len(rows) < 2:
        return []
    core = _topic_tokens(core_problem)
    topics = [_topic_tokens(f"{row.get('problem_title')} {row.get('pain_summary')} {row.get('category')}") for row in rows]
    categories = [str(row.get("category") or "").lower().strip() for row in rows]
    coherent = []
    for index, tokens in enumerate(topics):
        shares_core = bool(tokens & core)
        shares_peer = any(tokens & other for peer, other in enumerate(topics) if peer != index)
        shares_category = any(categories[index] and categories[index] == other for peer, other in enumerate(categories) if peer != index)
        if shares_core and (shares_peer or shares_category):
            coherent.append(rows[index])
    return coherent if len(coherent) >= 2 else []


def cluster_is_coherent(core_problem, rows):
    return len(coherent_cluster_rows(core_problem, rows)) == len(rows) and len(rows) >= 2


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row(row):
    keys = [
        "id", "trace_id", "problem_title", "category", "wtp_signal", "evidence_quote",
        "evidence_context", "source_title", "source_username", "confidence", "raw_llm_json",
        "chat_id", "message_id", "author_id", "author_anonymized_id", "profession_segment",
        "pain_summary", "desired_outcome", "current_workaround", "frequency",
        "estimated_time_cost", "estimated_money_cost", "urgency", "repeatability",
        "manual_work_indicator", "existing_tool_failure", "solution_seeking_indicator",
        "explicit_wtp_indicator", "potential_wtp", "pain_score", "msg_at", "quality_score",
    ]
    return dict(zip(keys, row))


LOAD_COLUMNS = """
id, trace_id, problem_title, category, wtp_signal, evidence_quote,
evidence_context, source_title, source_username, confidence, raw_llm_json,
chat_id, message_id, author_id, author_anonymized_id, profession_segment,
pain_summary, desired_outcome, current_workaround, frequency,
estimated_time_cost, estimated_money_cost, urgency, repeatability,
manual_work_indicator, existing_tool_failure, solution_seeking_indicator,
explicit_wtp_indicator, potential_wtp, pain_score, msg_at, quality_score
"""


async def reject_pain(row, reason, status="Rejected"):
    await Database.execute(
        """INSERT INTO trash_bin (original_pain_id,category,problem_title,reason,raw_llm_dump,trace_id)
           VALUES (?,?,?,?,?,?)""",
        (row["id"], row.get("category"), row.get("problem_title"), reason,
         row.get("raw_llm_json"), row.get("trace_id")),
    )
    await Database.execute(
        "UPDATE pains SET butcher_status=?, rejection_reason=? WHERE id=?",
        (status, reason, row["id"]),
    )


def _json_list(value):
    if isinstance(value, list):
        return value
    return [] if value in (None, "") else [str(value)]


async def _all_insight_rows(insight_id):
    rows = await Database.fetchall(f"SELECT {LOAD_COLUMNS} FROM pains WHERE insight_id=? ORDER BY msg_at DESC", (insight_id,))
    return [_row(row) for row in rows]


async def upsert_insight(cluster, source_rows):
    core_problem = str(cluster.get("core_problem") or "").strip()
    existing = await Database.fetchone(
        "SELECT id FROM verified_insights WHERE lower(core_problem)=lower(?) LIMIT 1", (core_problem,)
    )
    if existing:
        insight_id = existing[0]
    else:
        insight_id = await Database.execute(
            "INSERT INTO verified_insights (core_problem,created_at,updated_at) VALUES (?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (core_problem,),
        )

    for row in source_rows:
        await Database.execute(
            "UPDATE pains SET butcher_status='Processed', insight_id=?, rejection_reason=NULL WHERE id=?",
            (insight_id, row["id"]),
        )

    all_rows = await _all_insight_rows(insight_id)
    authors = {row.get("author_anonymized_id") for row in all_rows if row.get("author_anonymized_id")}
    chats = {source_key(row) for row in all_rows if source_key(row)}
    dates = [row.get("msg_at") for row in all_rows if row.get("msg_at")]
    now = datetime.now(timezone.utc)
    recent_signals = sum(
        bool((dt := _parse_date(value)) and now - dt <= timedelta(days=30)) for value in dates
    )
    chat_ids = sorted({int(row["chat_id"]) for row in all_rows if row.get("chat_id") is not None})
    chat_meta = []
    if chat_ids:
        placeholders = ",".join("?" for _ in chat_ids)
        chat_meta = await Database.fetchall(
            f"SELECT quality_score,member_count,activity FROM target_chats WHERE chat_id IN ({placeholders})",
            tuple(chat_ids),
        )
    source_quality = (
        sum(float(row[0] or 0) for row in chat_meta) / len(chat_meta)
        if chat_meta else sum(float(row.get("quality_score") or 0) for row in all_rows) / max(len(all_rows), 1)
    )
    active_audience = sum(int(row[1] or 0) for row in chat_meta)
    source_activity = sum(float(row[2] or 0) for row in chat_meta) / len(chat_meta) if chat_meta else 0
    intent_count = sum(bool(row.get("solution_seeking_indicator") or row.get("explicit_wtp_indicator")) for row in all_rows)
    reach = reachability_score(
        len(chats), len(authors), recent_signals, source_quality,
        intent_count / max(len(all_rows), 1), active_audience, source_activity,
    )
    base_pain = round(sum(int(row.get("pain_score") or 0) for row in all_rows) / max(len(all_rows), 1))
    corroboration = min(15, max(0, len(authors) - 1) * 3 + max(0, len(chats) - 1) * 3)
    final_pain = min(100, base_pain + corroboration)
    wtp_values = [normalize_wtp(row.get("potential_wtp") or row.get("wtp_signal")) for row in all_rows]
    wtp = max(wtp_values or ["LOW"], key=lambda item: {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[item])
    manual_48h = bool(cluster.get("manual_48h"))
    first_money = first_money_score(final_pain, wtp, reach, manual_48h, recent_signals, len(authors))
    quality_grade, _ = insight_quality_grade(core_problem, cluster.get("business_impact") or "", all_rows)
    examples = [
        {"quote": clip_text(row.get("evidence_quote") or "", 500),
         "chat": row.get("source_title") or row.get("source_username"), "date": row.get("msg_at")}
        for row in all_rows[:8]
    ]
    date_values = sorted((_parse_date(value) for value in dates), key=lambda value: value or now)
    date_values = [value for value in date_values if value]
    description = cluster.get("description") or cluster.get("business_impact") or ""
    await Database.execute(
        """UPDATE verified_insights SET
        core_problem=?, description=?, business_impact=?, category=?, weight=?, wtp_score=?,
        evidence_quotes=?, quality_grade=?, source_count=?, source_chats=?, icp=?, unique_authors=?,
        unique_chats=?, total_messages=?, date_from=?, date_to=?, example_messages=?, current_workaround=?,
        frequency=?, pain_score=?, reachability_score=?, confidence=?, why_business_pain=?,
        why_not_business_pain=?, manual_mvp=?, manual_48h=?, price_hypothesis=?, next_50_source=?,
        next_experiment=?, offer=?, first_customer_experiment=?, acquisition_source=?, trend=?,
        recent_signals=?, first_money_score=?, main_risk=?, cluster_json=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?""",
        (
            core_problem, description, cluster.get("business_impact"), cluster.get("category"), len(all_rows), wtp,
            json.dumps(examples, ensure_ascii=False), quality_grade, len(chats),
            json.dumps(sorted(chats), ensure_ascii=False), cluster.get("icp"), len(authors), len(chats), len(all_rows),
            date_values[0].isoformat() if date_values else None, date_values[-1].isoformat() if date_values else None,
            json.dumps(examples, ensure_ascii=False), cluster.get("current_workaround"), cluster.get("frequency"),
            final_pain, reach, float(cluster.get("confidence") or 0), cluster.get("why_business_pain"),
            cluster.get("why_not_business_pain"), cluster.get("manual_mvp"), int(manual_48h),
            cluster.get("price_hypothesis"), cluster.get("next_50_source"), cluster.get("next_experiment"),
            cluster.get("offer"), cluster.get("first_customer_experiment"), cluster.get("acquisition_source"),
            trend_from_dates(dates), recent_signals, first_money, cluster.get("main_risk"),
            json.dumps(cluster, ensure_ascii=False), insight_id,
        ),
    )
    return insight_id


def _parse_date(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def build_cluster_prompt(rows):
    payload = []
    for row in rows:
        payload.append({
            "id": row["id"], "chat": row.get("source_title") or row.get("source_username"),
            "author": row.get("author_anonymized_id"), "date": row.get("msg_at"),
            "segment": row.get("profession_segment"), "summary": row.get("pain_summary") or row.get("problem_title"),
            "category": row.get("category"), "wtp": row.get("potential_wtp") or row.get("wtp_signal"),
            "workaround": row.get("current_workaround"), "frequency": row.get("frequency"),
            "evidence": clip_text(row.get("evidence_quote") or "", EVIDENCE_CHARS),
            "context": clip_text(row.get("evidence_context") or "", CONTEXT_CHARS),
        })
    prompt = (
        "Ты evidence-first аналитик IT-болей. Объедини только действительно одну и ту же конкретную повторяющуюся "
        "проблему. Не создавай широкие кластеры вроде 'разработчикам сложно программировать'. Источник истины — evidence. "
        "Не считай несколько сообщений одного автора независимыми подтверждениями. Не предлагай AI/SaaS/бот по умолчанию. "
        "Кластер требует минимум 2 source_ids; предпочтительны разные авторы и чаты. Не выдумывай деньги/время. "
        "Manual MVP — конкретный результат, который можно вручную отдать, а не технология. Если данных нет — null.\n"
        "Верни STRICT JSON {\"clusters\":[{\"core_problem\":str,\"description\":str,\"category\":str,"
        "\"icp\":str,\"source_ids\":[int],\"business_impact\":str|null,\"current_workaround\":str|null,"
        "\"frequency\":str|null,\"why_business_pain\":str,\"why_not_business_pain\":str,"
        "\"manual_mvp\":str|null,\"manual_48h\":bool,\"offer\":str|null,\"price_hypothesis\":str|null,"
        "\"first_customer_experiment\":str,\"acquisition_source\":str,\"next_50_source\":str,"
        "\"next_experiment\":str,\"main_risk\":str,\"confidence\":float}],"
        "\"rejected\":[{\"source_id\":int,\"reason\":str}]}\nDATA:\n" + json.dumps(payload, ensure_ascii=False)
    )
    return prompt


async def process_pending_batch():
    pending_raw = await Database.fetchall(
        f"SELECT {LOAD_COLUMNS} FROM pains WHERE butcher_status='Pending' ORDER BY pain_score DESC,id LIMIT ?",
        (BATCH_LIMIT,),
    )
    if not pending_raw:
        return {"pending": 0, "clusters": 0, "rejected": 0, "low_signal": 0}
    pending = [_row(row) for row in pending_raw]
    support_raw = await Database.fetchall(
        f"SELECT {LOAD_COLUMNS} FROM pains WHERE butcher_status='LowSignal' AND insight_id IS NULL ORDER BY pain_score DESC,id DESC LIMIT ?",
        (BATCH_LIMIT,),
    )
    rows_by_id = {row["id"]: row for row in pending + [_row(row) for row in support_raw]}
    valid = []
    rejected_count = 0
    for row in rows_by_id.values():
        reason = pain_rejection_reason(row.get("problem_title"), row.get("evidence_quote"),
                                      row.get("evidence_context"), row.get("source_title"))
        if reason:
            await reject_pain(row, reason)
            rejected_count += 1
        else:
            valid.append(row)
    if len(valid) < MIN_CLUSTER_SIZE:
        for row in pending:
            if row["id"] in rows_by_id:
                await Database.execute("UPDATE pains SET butcher_status='LowSignal',rejection_reason=? WHERE id=?",
                                       ("waiting for independent matching evidence", row["id"]))
        return {"pending": len(pending), "clusters": 0, "rejected": rejected_count, "low_signal": len(pending)}

    raw, _ = await ai.chat([{"role": "user", "content": build_cluster_prompt(valid)}],
                           model="llama-3.1-8b-instant", json_mode=True)
    data = parse_json_result(raw)
    if not data:
        return {"pending": len(pending), "clusters": 0, "rejected": rejected_count, "error": "invalid LLM response"}
    processed = set()
    for item in data.get("rejected", []):
        source_id = as_int(item.get("source_id"))
        if source_id in rows_by_id:
            await reject_pain(rows_by_id[source_id], item.get("reason") or "LLM rejected false pain")
            processed.add(source_id)
            rejected_count += 1

    cluster_count = 0
    for cluster in data.get("clusters", []):
        source_ids = []
        for value in cluster.get("source_ids", []):
            source_id = as_int(value)
            if source_id in rows_by_id and source_id not in source_ids and source_id not in processed:
                source_ids.append(source_id)
        core_problem = str(cluster.get("core_problem") or "").strip()
        if len(source_ids) < MIN_CLUSTER_SIZE or len(core_problem) < 18:
            continue
        proposed_rows = [rows_by_id[source_id] for source_id in source_ids]
        source_rows = coherent_cluster_rows(core_problem, proposed_rows)
        if len(source_rows) < MIN_CLUSTER_SIZE:
            log_event("BUTCHER", "CLUSTER_REJECT", "Cluster mixed semantically different pains",
                      core_problem=core_problem, source_ids=source_ids)
            continue
        if len(source_rows) < len(proposed_rows):
            log_event("BUTCHER", "CLUSTER_PRUNE", "Removed semantically unrelated cluster sources",
                      core_problem=core_problem, removed=len(proposed_rows) - len(source_rows))
        source_ids = [row["id"] for row in source_rows]
        independent_authors = {row.get("author_anonymized_id") for row in source_rows if row.get("author_anonymized_id")}
        if len(independent_authors) < 2:
            log_event("BUTCHER", "CLUSTER_REJECT", "Cluster lacks two independent affected authors",
                      core_problem=core_problem, known_authors=len(independent_authors))
            continue
        grade, reason = insight_quality_grade(core_problem, cluster.get("business_impact") or "", source_rows)
        if grade == "Trash":
            for row in source_rows:
                await reject_pain(row, reason or "cluster failed quality guardrails")
                processed.add(row["id"])
                rejected_count += 1
            continue
        await upsert_insight(cluster, source_rows)
        processed.update(source_ids)
        cluster_count += 1

    low_signal = 0
    for row in pending:
        if row["id"] not in processed:
            await Database.execute("UPDATE pains SET butcher_status='LowSignal',rejection_reason=? WHERE id=?",
                                   ("waiting for independent matching evidence", row["id"]))
            low_signal += 1
    return {"pending": len(pending), "clusters": cluster_count, "rejected": rejected_count, "low_signal": low_signal}


async def run_butcher(run_once=False):
    await Database.init_schema()
    log_event("BUTCHER", "START", "Starting IT pain clusterer", speed=speed_label())
    while True:
        result = await process_pending_batch()
        log_event("BUTCHER", "CYCLE_DONE", "IT clustering cycle finished", **result)
        if run_once:
            return result
        if not result.get("pending"):
            await asyncio.sleep(EMPTY_SLEEP)
        elif result.get("error"):
            await asyncio.sleep(INVALID_RESPONSE_SLEEP)
        elif not result.get("clusters"):
            await asyncio.sleep(NOT_ENOUGH_SLEEP)
        else:
            await asyncio.sleep(CYCLE_SLEEP)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cluster structured IT pain signals")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run_butcher(run_once=args.once))
    if args.once:
        print(json.dumps(result, ensure_ascii=False, indent=2))
