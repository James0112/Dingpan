from __future__ import annotations

import json
from dataclasses import dataclass

from src.config import Settings
from src.providers.base import GenerateConfig
from src.providers.registry import get_provider


class ChatError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatReply:
    reply: str
    title: str


@dataclass(frozen=True)
class ChatOutput:
    result: ChatReply
    actual_provider: str
    actual_model_name: str
    provider_response_id: str


def generate_chat_reply(
    *,
    db_path: str,
    settings: Settings,
    model_id: str,
    conversation_type: str,
    stock_code: str,
    shared_context: str,
    messages: list[dict[str, str]],
) -> ChatOutput:
    provider = get_provider(db_path, settings, model_id)
    prompt = _build_prompt(
        conversation_type=conversation_type,
        stock_code=stock_code,
        shared_context=shared_context,
        messages=messages,
    )
    try:
        result = provider.generate(prompt, GenerateConfig(temperature=0.5, max_output_tokens=1600))
    except Exception as exc:
        raise ChatError(f"{model_id} chat failed: {exc}") from exc

    parsed = _parse_chat_response(result.text)
    return ChatOutput(
        result=parsed,
        actual_provider=result.actual_provider,
        actual_model_name=result.actual_model_name,
        provider_response_id=result.provider_response_id,
    )


def _build_prompt(
    *,
    conversation_type: str,
    stock_code: str,
    shared_context: str,
    messages: list[dict[str, str]],
) -> str:
    context_lines = [
        "你是盯盘侠里的股票分析助手。",
        "回答要求：直接、具体、可执行，不要空话，不要免责声明堆砌。",
        "输出必须是 JSON 对象，字段只有 title 和 reply。",
        "title: 8-18 个中文字符，适合作为会话标题。",
        "reply: 给用户的正式回复，允许分段，但不要使用 Markdown 标题。",
    ]
    if conversation_type == "stock":
        context_lines.append(f"当前是单只股票会话，股票代码：{stock_code}。")
        if shared_context.strip():
            context_lines.append("以下是该股票当前可用的共享分析上下文，请优先基于它回答。")
            context_lines.append(shared_context.strip())
    else:
        context_lines.append("当前是普通会话，不注入单只股票共享分析。")

    transcript_lines: list[str] = []
    for message in messages[-10:]:
        role = "用户" if message.get("role") == "user" else "助手"
        content = str(message.get("content") or "").strip()
        if content:
            transcript_lines.append(f"{role}: {content}")

    return "\n".join(
        [
            "\n".join(context_lines),
            "",
            "下面是最近对话：",
            "\n".join(transcript_lines) if transcript_lines else "用户: 你好",
            "",
            '请返回形如 {"title":"...", "reply":"..."} 的 JSON。',
        ]
    )


def _parse_chat_response(raw_text: str) -> ChatReply:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ChatError(f"Model returned invalid chat JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ChatError("Chat response must be a JSON object")

    reply = str(payload.get("reply") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not reply:
        raise ChatError("Chat response is missing reply")
    if not title:
        title = reply[:18]
    return ChatReply(reply=reply, title=title[:32])
