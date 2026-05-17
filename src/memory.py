from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from src.config import Settings
from src.database import fetch_one
from src.providers import GenerateConfig, get_provider


logger = logging.getLogger("dingpan")
MEMORY_CONTEXT_TYPE = "memory"
MEMORY_USER_MESSAGE_THRESHOLD = 5


@dataclass(frozen=True)
class MemoryContext:
    summary: str
    last_message_id: int
    model_id: str
    updated_from: str
    updated_at: str


async def load_stock_memory_context(*, db_path: str, user_id: int, stock_code: str) -> MemoryContext:
    row = await fetch_one(
        db_path,
        """
        SELECT content_json
        FROM user_context
        WHERE user_id = ? AND stock_code = ? AND context_type = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id, stock_code, MEMORY_CONTEXT_TYPE),
    )
    if row is None:
        return MemoryContext(summary="", last_message_id=0, model_id="", updated_from="", updated_at="")
    return _memory_context_from_json(str(row["content_json"] or ""))


def update_stock_memory_context_sync(
    *,
    db_path: str,
    settings: Settings,
    user_id: int,
    stock_code: str,
    model_id: str,
    conversation_id: int,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        existing_row = conn.execute(
            """
            SELECT id, content_json
            FROM user_context
            WHERE user_id = ? AND stock_code = ? AND context_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, stock_code, MEMORY_CONTEXT_TYPE),
        ).fetchone()
        existing = (
            _memory_context_from_json(str(existing_row["content_json"] or ""))
            if existing_row is not None
            else MemoryContext(summary="", last_message_id=0, model_id="", updated_from="", updated_at="")
        )

        new_message_rows = conn.execute(
            """
            SELECT id, role, content
            FROM messages
            WHERE conversation_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (conversation_id, existing.last_message_id),
        ).fetchall()
        if not new_message_rows:
            return

        new_user_message_count = sum(1 for row in new_message_rows if str(row["role"]) == "user")
        if new_user_message_count < MEMORY_USER_MESSAGE_THRESHOLD:
            logger.info(
                "chat_memory_skip threshold_not_met user_id=%s stock_code=%s conversation_id=%s user_messages=%s threshold=%s last_message_id=%s",
                user_id,
                stock_code,
                conversation_id,
                new_user_message_count,
                MEMORY_USER_MESSAGE_THRESHOLD,
                existing.last_message_id,
            )
            return

        transcript_lines: list[str] = []
        last_message_id = existing.last_message_id
        for row in new_message_rows:
            role = "用户" if str(row["role"]) == "user" else "助手"
            content = str(row["content"] or "").strip()
            if content:
                transcript_lines.append(f"{role}: {content}")
            last_message_id = max(last_message_id, int(row["id"] or 0))

        provider = get_provider(db_path, settings, model_id)
        prompt = _build_memory_prompt(existing.summary, "\n".join(transcript_lines))
        provider_result = provider.generate(prompt, GenerateConfig(temperature=0.2, max_output_tokens=400))
        next_summary = _parse_memory_response(provider_result.text, existing.summary)
        payload = {
            "summary": next_summary,
            "last_message_id": last_message_id,
            "model_id": model_id,
            "updated_from": "chat",
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        raw_payload = json.dumps(payload, ensure_ascii=False)

        conn.execute(
            """
            INSERT INTO user_context (user_id, stock_code, context_type, content_json, trade_date, created_at)
            VALUES (?, ?, ?, ?, '', CURRENT_TIMESTAMP)
            """,
            (user_id, stock_code, MEMORY_CONTEXT_TYPE, raw_payload),
        )
        inserted_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            DELETE FROM user_context
            WHERE user_id = ? AND stock_code = ? AND context_type = ? AND id <> ?
            """,
            (user_id, stock_code, MEMORY_CONTEXT_TYPE, inserted_id),
        )
        conn.commit()
        logger.info(
            "chat_memory_update user_id=%s stock_code=%s conversation_id=%s user_messages=%s old_len=%s new_len=%s changed=%s last_message_id=%s model_id=%s provider=%s",
            user_id,
            stock_code,
            conversation_id,
            new_user_message_count,
            len(existing.summary),
            len(next_summary),
            int(next_summary != existing.summary),
            last_message_id,
            model_id,
            provider_result.actual_provider,
        )
    except Exception:
        logger.exception(
            "chat_memory_update_failed user_id=%s stock_code=%s conversation_id=%s model_id=%s",
            user_id,
            stock_code,
            conversation_id,
            model_id,
        )
    finally:
        conn.close()


def _build_memory_prompt(existing_summary: str, recent_transcript: str) -> str:
    return f"""你是盯盘侠的记忆管理器，负责维护“某个用户对某只股票的长期有效背景摘要”。

## 当前记忆（可能为空）
{existing_summary or "（暂无）"}

## 最近新增对话
{recent_transcript or "（暂无）"}

## 任务
请基于当前记忆和最近新增对话，输出一段更新后的用户记忆摘要。

## 硬约束
1. 只记录用户相关、跨日期仍有价值的信息，例如：持仓态度、风险边界、关注点、操作倾向、明确表达过的关键价位判断。
2. 禁止写入当天市场结论、技术结论、新闻事件、共享分析内容；这些信息会过期，不属于长期记忆。
3. 不要编造用户仓位比例、收益率、持仓时长或成本价，除非用户在对话里明确说过。
4. 控制在 200 字以内，写成一段自然语言，不要分点。
5. 如果最近对话没有新增任何有价值的用户信息，原样返回当前记忆。
6. 只返回 JSON：{{"summary":"..."}}，不要返回其他文字。
"""


def _parse_memory_response(raw_text: str, existing_summary: str) -> str:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return existing_summary
    if not isinstance(payload, dict):
        return existing_summary
    summary = str(payload.get("summary") or "").strip()
    if not summary and existing_summary:
        return existing_summary
    return summary[:200]


def _memory_context_from_json(raw: str) -> MemoryContext:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return MemoryContext(
        summary=str(payload.get("summary") or "").strip(),
        last_message_id=int(payload.get("last_message_id") or 0),
        model_id=str(payload.get("model_id") or "").strip(),
        updated_from=str(payload.get("updated_from") or "").strip(),
        updated_at=str(payload.get("updated_at") or "").strip(),
    )
