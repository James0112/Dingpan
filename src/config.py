from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.schedule import normalize_clock_time


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
ENV_FILE = BASE_DIR / ".env"


@dataclass(frozen=True)
class Settings:
    model_id: str = "gemini"
    stock_code: str = "603212"
    stock_name: str = ""
    cost_price: float = 0.0
    jwt_secret: str = "change-me"
    site_url: str = "http://127.0.0.1:8000"
    db_path: str = str(BASE_DIR / "data" / "dingpan.db")
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_claims_email: str | None = None
    model_name: str = "gemini-3-flash-preview"
    fallback_model_name: str = "gemini-2.5-flash-lite"
    timezone_name: str = "Asia/Shanghai"
    max_news_items: int = 8
    news_lookback_hours: int = 48
    enable_grounding: bool = False
    disable_proxy: bool = True
    dry_run: bool = False
    output_dir: Path = OUTPUT_DIR
    template_path: Path = TEMPLATE_DIR / "email_template.html"
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str = "https://subapi.233clouds.com/v1"
    openai_reasoning_effort: str = "xhigh"
    receiver_emails: tuple[str, ...] = ()
    resend_api_key: str | None = None
    mail_from_auth: str | None = None
    mail_from_reports: str | None = None
    report_schedule_timezone: str = "Asia/Shanghai"
    report_generate_time: str = "07:40:00"
    report_email_time: str = "07:50:00"
    report_push_time: str = "07:55:00"
    generate_target_timeout_seconds: int = 180


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_or_default(key: str, default: str) -> str:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return value


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    items = []
    for chunk in value.replace(";", ",").split(","):
        item = chunk.strip()
        if item:
            items.append(item)
    return tuple(items)


def load_dotenv_file() -> None:
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def load_settings() -> Settings:
    load_dotenv_file()
    return Settings(
        model_id=_env_or_default("MODEL_ID", "gemini"),
        stock_code=_env_or_default("STOCK_CODE", "603212"),
        stock_name=os.getenv("STOCK_NAME", ""),
        cost_price=float(_env_or_default("COST_PRICE", "0")),
        jwt_secret=_env_or_default("JWT_SECRET", "change-me"),
        site_url=_env_or_default("SITE_URL", "http://127.0.0.1:8000"),
        db_path=_env_or_default("DB_PATH", str(BASE_DIR / "data" / "dingpan.db")),
        vapid_public_key=os.getenv("VAPID_PUBLIC_KEY"),
        vapid_private_key=os.getenv("VAPID_PRIVATE_KEY"),
        vapid_claims_email=os.getenv("VAPID_CLAIMS_EMAIL"),
        model_name=_env_or_default("GEMINI_MODEL", "gemini-3-flash-preview"),
        fallback_model_name=_env_or_default("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite"),
        timezone_name=_env_or_default("TZ_NAME", "Asia/Shanghai"),
        max_news_items=int(_env_or_default("MAX_NEWS_ITEMS", "8")),
        news_lookback_hours=int(_env_or_default("NEWS_LOOKBACK_HOURS", "48")),
        enable_grounding=_parse_bool(os.getenv("ENABLE_GROUNDING"), False),
        disable_proxy=_parse_bool(os.getenv("DISABLE_PROXY"), True),
        dry_run=_parse_bool(os.getenv("DRY_RUN"), False),
        output_dir=OUTPUT_DIR,
        template_path=TEMPLATE_DIR / "email_template.html",
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_base_url=_env_or_default("OPENAI_BASE_URL", "https://subapi.233clouds.com/v1"),
        openai_reasoning_effort=_env_or_default("OPENAI_REASONING_EFFORT", "xhigh"),
        receiver_emails=_parse_csv(os.getenv("RECEIVER_EMAIL")),
        resend_api_key=os.getenv("RESEND_API_KEY"),
        mail_from_auth=os.getenv("MAIL_FROM_AUTH"),
        mail_from_reports=os.getenv("MAIL_FROM_REPORTS"),
        report_schedule_timezone=_env_or_default("REPORT_TIMEZONE", _env_or_default("TZ_NAME", "Asia/Shanghai")),
        report_generate_time=normalize_clock_time(_env_or_default("REPORT_GENERATE_TIME", "07:40:00")),
        report_email_time=normalize_clock_time(_env_or_default("REPORT_EMAIL_TIME", "07:50:00")),
        report_push_time=normalize_clock_time(_env_or_default("REPORT_PUSH_TIME", "07:55:00")),
        generate_target_timeout_seconds=int(_env_or_default("GENERATE_TARGET_TIMEOUT_SECONDS", "180")),
    )
