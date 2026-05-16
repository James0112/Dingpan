from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field

from src.auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    generate_one_time_token,
    get_optional_user,
    hash_one_time_token,
    hash_password,
    require_user,
    verify_password,
)
from src.config import load_settings
from src.database import connect, execute, fetch_all, fetch_one, init_db
from src.fetch_data import DataFetchError, fetch_market_data
from src.fetch_news import fetch_news
from src.analyze import AnalysisError, analyze_market_data
from src.mailer import MailerError, render_template, send_resend_email
from src.personalize import PersonalizedAnalysisError, generate_personalized_analysis
from src.push import (
    PushError,
    delete_push_subscription,
    get_user_push_subscriptions,
    has_user_push_subscription,
    send_push_to_user,
    upsert_push_subscription,
)
from src.render_report import (
    ANALYSIS_VERSION,
    analysis_result_to_json,
    analysis_result_from_json,
    build_report_context,
    market_data_to_json,
    market_data_from_json,
    news_list_to_json,
    news_list_from_json,
    personalized_analysis_from_json,
    personalized_analysis_to_json,
    user_profile_to_json,
)
from src.schemas import MarketData, UserProfile
from src.trading_calendar import (
    TradingCalendarError,
    fallback_latest_trade_date,
    get_latest_trade_date,
    get_today,
    is_calendar_stale,
    is_today_trading_day,
    load_trade_calendar,
)


settings = load_settings()
app = FastAPI(title="DingPan", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
static_dir = Path(__file__).resolve().parent / "static"
project_root = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
RATE_LIMIT_BUCKETS: dict[str, list[float]] = defaultdict(list)
VERIFY_EMAIL_TOKEN_TTL_HOURS = 24
RESET_PASSWORD_TOKEN_TTL_MINUTES = 60
EMAIL_RESEND_INTERVAL_SECONDS = 60


def _resolve_asset_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_root),
            text=True,
        ).strip()
    except Exception:
        return str(int((project_root / "app.py").stat().st_mtime))


ASSET_VERSION = _resolve_asset_version()


class RegisterPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class ForgotPasswordPayload(BaseModel):
    email: EmailStr


class ResetPasswordPayload(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class EmailPreferencesPayload(BaseModel):
    daily_email_enabled: bool


class ModelPreferencePayload(BaseModel):
    preferred_model: str = Field(min_length=1, max_length=32)


class UserProfilePayload(BaseModel):
    risk_preference: str = Field(default="", max_length=32)
    trading_style: str = Field(default="", max_length=32)
    focus_sectors: str = Field(default="", max_length=500)
    position_notes: str = Field(default="", max_length=2000)
    custom_notes: str = Field(default="", max_length=2000)


class SubscriptionCreatePayload(BaseModel):
    stock_code: str = Field(min_length=1, max_length=16)
    stock_name: str = Field(default="", max_length=64)
    cost_price: float = 0.0


class SubscriptionUpdatePayload(BaseModel):
    cost_price: Optional[float] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class PushSubscribePayload(BaseModel):
    endpoint: str
    keys: dict[str, str]


class AdminUserUpdatePayload(BaseModel):
    is_active: Optional[bool] = None
    email_verified: Optional[bool] = None
    daily_email_enabled: Optional[bool] = None


def _normalize_stock_code(stock_code: str) -> str:
    code = stock_code.strip().upper()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="stock_code is required")
    return code


def _set_session_cookie(response: Response, user_id: int) -> None:
    token = create_session_token(user_id, settings.jwt_secret)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.site_url.startswith("https://"),
        max_age=60 * 60 * 24 * 14,
        path="/",
    )


async def current_user(request: Request):
    return await require_user(request, settings.db_path, settings.jwt_secret)


async def verified_user(request: Request):
    user = await require_user(request, settings.db_path, settings.jwt_secret)
    if not user.email_verified_at:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Please verify your email first")
    return user


async def optional_user(request: Request):
    return await get_optional_user(request, settings.db_path, settings.jwt_secret)


async def page_user_or_redirect(request: Request):
    user = await get_optional_user(request, settings.db_path, settings.jwt_secret)
    if user is None:
        return None
    return user


async def admin_user(request: Request):
    user = await require_user(request, settings.db_path, settings.jwt_secret)
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def _preferred_app_name(request: Request) -> str:
    accept_language = request.headers.get("accept-language", "").lower()
    if "zh" in accept_language:
        return "盯盘侠"
    return "DingPan"


def _template_context(request: Request, **extra: object) -> dict[str, object]:
    return {"app_display_name": _preferred_app_name(request), "asset_version": ASSET_VERSION, **extra}


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    content_type = response.headers.get("content-type", "")

    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif path in {"/sw.js", "/manifest.webmanifest"}:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response


def _require_mailer_config() -> None:
    if not settings.resend_api_key or not settings.mail_from_auth:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Email service is not configured")


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_rate_limit(scope: str, key: str, *, max_attempts: int, window_seconds: int) -> None:
    now = datetime.now(timezone.utc).timestamp()
    bucket_key = f"{scope}:{key}"
    timestamps = [item for item in RATE_LIMIT_BUCKETS[bucket_key] if now - item < window_seconds]
    if len(timestamps) >= max_attempts:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests, please try again later")
    timestamps.append(now)
    RATE_LIMIT_BUCKETS[bucket_key] = timestamps


async def _fetch_runnable_model(model_id: str):
    row = await fetch_one(
        settings.db_path,
        """
        SELECT model_id, display_name, points_per_call
        FROM model_pricing
        WHERE model_id = ? AND is_active = 1 AND is_runnable = 1
        """,
        (model_id,),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected model is not available")
    return row


def _build_default_user_profile() -> UserProfile:
    return UserProfile(
        risk_preference="",
        trading_style="",
        focus_sectors=[],
        position_notes="",
        custom_notes="",
        context_version=0,
    )


def _normalize_focus_sectors(raw_value: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_value.replace("，", ",").replace("、", ",").split(","):
        value = item.strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(value)
    return normalized[:12]


def _user_profile_from_row(row) -> UserProfile:
    if row is None:
        return _build_default_user_profile()
    try:
        focus_sectors = json.loads(str(row["focus_sectors_json"] or "[]"))
    except json.JSONDecodeError:
        focus_sectors = []
    return UserProfile(
        risk_preference=str(row["risk_preference"] or ""),
        trading_style=str(row["trading_style"] or ""),
        focus_sectors=[str(item) for item in focus_sectors if str(item).strip()],
        position_notes=str(row["position_notes"] or ""),
        custom_notes=str(row["custom_notes"] or ""),
        context_version=int(row["context_version"] or 0),
    )


async def _load_user_profile(user_id: int) -> UserProfile:
    row = await fetch_one(
        settings.db_path,
        """
        SELECT risk_preference, trading_style, focus_sectors_json, position_notes, custom_notes, context_version
        FROM user_profiles
        WHERE user_id = ?
        """,
        (user_id,),
    )
    return _user_profile_from_row(row)


def _load_user_profile_sync(user_id: int) -> UserProfile:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT risk_preference, trading_style, focus_sectors_json, position_notes, custom_notes, context_version
            FROM user_profiles
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return _user_profile_from_row(row)


def _resolve_generation_trade_date() -> date:
    today = get_today(settings.timezone_name)
    try:
        trade_dates = load_trade_calendar()
        if is_calendar_stale(today, trade_dates):
            raise TradingCalendarError("Trade calendar is stale")
        if not is_today_trading_day(today, trade_dates):
            return get_latest_trade_date(today, trade_dates)
        return get_latest_trade_date(today, trade_dates)
    except TradingCalendarError:
        return fallback_latest_trade_date(today)


def _upsert_analysis_cache_sync(
    *,
    stock_code: str,
    stock_name: str,
    trade_date_value: date,
    model_id: str,
    status_value: str,
    error_message: str,
    market_data_json: str,
    analysis_json: str,
    news_json: str,
    actual_provider: str,
    actual_model_name: str,
    provider_response_id: str,
) -> None:
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            """
            INSERT INTO analysis_cache (
                stock_code, stock_name, trade_date, model_id, analysis_version,
                status, error_message, market_data_json, analysis_json, news_json,
                actual_provider, actual_model_name, provider_response_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_code, trade_date, model_id) DO UPDATE SET
                stock_name=excluded.stock_name,
                analysis_version=excluded.analysis_version,
                status=excluded.status,
                error_message=excluded.error_message,
                market_data_json=excluded.market_data_json,
                analysis_json=excluded.analysis_json,
                news_json=excluded.news_json,
                actual_provider=excluded.actual_provider,
                actual_model_name=excluded.actual_model_name,
                provider_response_id=excluded.provider_response_id,
                created_at=CURRENT_TIMESTAMP
            """,
            (
                stock_code,
                stock_name,
                trade_date_value.isoformat(),
                model_id,
                ANALYSIS_VERSION,
                status_value,
                error_message,
                market_data_json,
                analysis_json,
                news_json,
                actual_provider,
                actual_model_name,
                provider_response_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _upsert_personalized_analysis_sync(
    *,
    user_id: int,
    stock_code: str,
    trade_date: str,
    model_id: str,
    status_value: str,
    error_message: str,
    result_json: str,
    context_snapshot_json: str,
    context_version: int,
    actual_provider: str,
    actual_model_name: str,
    provider_response_id: str,
) -> None:
    conn = sqlite3.connect(settings.db_path)
    try:
        payload = (
            status_value,
            error_message,
            result_json,
            context_snapshot_json,
            context_version,
            actual_provider,
            actual_model_name,
            provider_response_id,
        )
        cursor = conn.execute(
            """
            UPDATE personalized_analysis
            SET
                status = ?,
                error_message = ?,
                result_json = ?,
                context_snapshot_json = ?,
                context_version = ?,
                actual_provider = ?,
                actual_model_name = ?,
                provider_response_id = ?,
                created_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND stock_code = ? AND trade_date = ? AND model_id = ?
            """,
            payload
            + (
                user_id,
                stock_code,
                trade_date,
                model_id,
            ),
        )
        if cursor.rowcount == 0:
            try:
                conn.execute(
                    """
                    INSERT INTO personalized_analysis (
                        user_id, stock_code, trade_date, model_id, status, error_message,
                        result_json, context_snapshot_json, context_version, points_consumed,
                        actual_provider, actual_model_name, provider_response_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        user_id,
                        stock_code,
                        trade_date,
                        model_id,
                        status_value,
                        error_message,
                        result_json,
                        context_snapshot_json,
                        context_version,
                        actual_provider,
                        actual_model_name,
                        provider_response_id,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute(
                    """
                    UPDATE personalized_analysis
                    SET
                        model_id = ?,
                        status = ?,
                        error_message = ?,
                        result_json = ?,
                        context_snapshot_json = ?,
                        context_version = ?,
                        actual_provider = ?,
                        actual_model_name = ?,
                        provider_response_id = ?,
                        created_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND stock_code = ? AND trade_date = ?
                    """,
                    (
                        model_id,
                        status_value,
                        error_message,
                        result_json,
                        context_snapshot_json,
                        context_version,
                        actual_provider,
                        actual_model_name,
                        provider_response_id,
                        user_id,
                        stock_code,
                        trade_date,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _generate_personalized_analysis_for_user_sync(
    user_id: int,
    stock_code: str,
    trade_date: str,
    model_id: str,
    cost_price: float,
) -> None:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cache_row = conn.execute(
            """
            SELECT market_data_json, analysis_json, status
            FROM analysis_cache
            WHERE stock_code = ? AND trade_date = ? AND model_id = ?
            """,
            (stock_code, trade_date, model_id),
        ).fetchone()
    finally:
        conn.close()

    if cache_row is None or str(cache_row["status"]) != "success":
        _upsert_personalized_analysis_sync(
            user_id=user_id,
            stock_code=stock_code,
            trade_date=trade_date,
            model_id=model_id,
            status_value="failed",
            error_message="Shared analysis is not ready",
            result_json="",
            context_snapshot_json="",
            context_version=0,
            actual_provider="",
            actual_model_name="",
            provider_response_id="",
        )
        return

    profile = _load_user_profile_sync(user_id)
    shared_market_data = market_data_from_json(str(cache_row["market_data_json"]))
    market_data = MarketData(
        stock_code=shared_market_data.stock_code,
        stock_name=shared_market_data.stock_name,
        cost_price=cost_price,
        latest_trade_date=shared_market_data.latest_trade_date,
        snapshot=shared_market_data.snapshot,
        indicators=shared_market_data.indicators,
        fund_flow=shared_market_data.fund_flow,
        recent_5d_summary=shared_market_data.recent_5d_summary,
    )
    shared_analysis = analysis_result_from_json(str(cache_row["analysis_json"]))
    try:
        output = generate_personalized_analysis(
            model_id,
            market_data,
            shared_analysis,
            profile,
            db_path=settings.db_path,
            settings=settings,
        )
        _upsert_personalized_analysis_sync(
            user_id=user_id,
            stock_code=stock_code,
            trade_date=trade_date,
            model_id=model_id,
            status_value="success",
            error_message="",
            result_json=personalized_analysis_to_json(output.result),
            context_snapshot_json=user_profile_to_json(profile),
            context_version=profile.context_version,
            actual_provider=output.actual_provider,
            actual_model_name=output.actual_model_name,
            provider_response_id=output.provider_response_id,
        )
    except PersonalizedAnalysisError as exc:
        _upsert_personalized_analysis_sync(
            user_id=user_id,
            stock_code=stock_code,
            trade_date=trade_date,
            model_id=model_id,
            status_value="failed",
            error_message=str(exc),
            result_json="",
            context_snapshot_json=user_profile_to_json(profile),
            context_version=profile.context_version,
            actual_provider="",
            actual_model_name="",
            provider_response_id="",
        )


def _profile_summary_text(profile: UserProfile) -> str:
    parts: list[str] = []
    if profile.risk_preference:
        parts.append(profile.risk_preference)
    if profile.trading_style:
        parts.append(profile.trading_style)
    if profile.focus_sectors:
        parts.append(" / ".join(profile.focus_sectors[:3]))
    return " · ".join(parts)


def _personalized_dashboard_status(state: str) -> tuple[str, str]:
    if state == "ready":
        return ("个性化已就绪", "已合并你的投资画像与持仓成本。")
    if state == "failed":
        return ("个性化生成失败", "可以进入报告页重试，或先修改画像。")
    if state == "stale":
        return ("画像已更新", "当前个性化建议需要按最新画像重新生成。")
    if state == "generating":
        return ("个性化生成中", "共享分析可先查看，个性化建议稍后补齐。")
    return ("等待个性化生成", "已有共享分析，首次个性化建议会自动生成。")


def _shared_dashboard_status(state: str, *, current_trade_date: str, latest_trade_date: str | None) -> tuple[str, str]:
    if state == "ready":
        return ("当前模型共享分析已就绪", f"当前模型的共享分析已经切到 {current_trade_date}。")
    if state == "failed":
        return ("当前模型共享分析失败", f"{current_trade_date} 的共享分析生成失败，请稍后重试。")
    if latest_trade_date and latest_trade_date != current_trade_date:
        return ("当前模型共享分析生成中", f"卡片内容还是 {latest_trade_date} 的旧报告，{current_trade_date} 的新共享分析仍在生成。")
    return ("当前模型共享分析生成中", f"{current_trade_date} 的共享分析仍在生成，完成后个性化建议会继续补齐。")


def _personalized_version_note(current_version: int, applied_version: int) -> str:
    if applied_version <= 0:
        return f"当前画像 v{current_version}，尚未应用到这份个性化建议。"
    if applied_version == current_version:
        return f"当前画像 v{current_version} 已应用。"
    return f"当前画像 v{current_version}，这份建议仍停留在 v{applied_version}。"


async def _load_personalized_analysis_state(
    *,
    user_id: int,
    stock_code: str,
    trade_date: str,
    model_id: str,
    profile: UserProfile,
) -> dict[str, object]:
    row = await fetch_one(
        settings.db_path,
        """
        SELECT status, error_message, result_json, context_version
        FROM personalized_analysis
        WHERE user_id = ? AND stock_code = ? AND trade_date = ? AND model_id = ?
        """,
        (user_id, stock_code, trade_date, model_id),
    )
    if row is None:
        return {"state": "missing", "result": None, "error_message": "", "applied_context_version": 0}

    row_version = int(row["context_version"] or 0)
    if row_version != profile.context_version:
        return {"state": "stale", "result": None, "error_message": "", "applied_context_version": row_version}

    state = str(row["status"] or "pending")
    if state == "success" and str(row["result_json"] or "").strip():
        return {
            "state": "ready",
            "result": personalized_analysis_from_json(str(row["result_json"])),
            "error_message": "",
            "applied_context_version": row_version,
        }
    if state == "failed":
        return {
            "state": "failed",
            "result": None,
            "error_message": str(row["error_message"] or ""),
            "applied_context_version": row_version,
        }
    return {"state": "generating", "result": None, "error_message": "", "applied_context_version": row_version}


async def _queue_personalized_generation(
    background_tasks: BackgroundTasks,
    *,
    user_id: int,
    stock_code: str,
    trade_date: str,
    model_id: str,
    cost_price: float,
    profile: UserProfile,
) -> None:
    conn = await connect(settings.db_path)
    try:
        context_snapshot_json = user_profile_to_json(profile)
        cursor = await conn.execute(
            """
            UPDATE personalized_analysis
            SET
                status = 'pending',
                error_message = '',
                result_json = '',
                context_snapshot_json = ?,
                context_version = ?,
                actual_provider = '',
                actual_model_name = '',
                provider_response_id = '',
                created_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND stock_code = ? AND trade_date = ? AND model_id = ?
            """,
            (
                context_snapshot_json,
                profile.context_version,
                user_id,
                stock_code,
                trade_date,
                model_id,
            ),
        )
        if cursor.rowcount == 0:
            try:
                await conn.execute(
                    """
                    INSERT INTO personalized_analysis (
                        user_id, stock_code, trade_date, model_id, status, error_message,
                        result_json, context_snapshot_json, context_version, points_consumed,
                        actual_provider, actual_model_name, provider_response_id, created_at
                    )
                    VALUES (?, ?, ?, ?, 'pending', '', '', ?, ?, 0, '', '', '', CURRENT_TIMESTAMP)
                    """,
                    (
                        user_id,
                        stock_code,
                        trade_date,
                        model_id,
                        context_snapshot_json,
                        profile.context_version,
                    ),
                )
            except sqlite3.IntegrityError:
                await conn.execute(
                    """
                    UPDATE personalized_analysis
                    SET
                        model_id = ?,
                        status = 'pending',
                        error_message = '',
                        result_json = '',
                        context_snapshot_json = ?,
                        context_version = ?,
                        actual_provider = '',
                        actual_model_name = '',
                        provider_response_id = '',
                        created_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND stock_code = ? AND trade_date = ?
                    """,
                    (
                        model_id,
                        context_snapshot_json,
                        profile.context_version,
                        user_id,
                        stock_code,
                        trade_date,
                    ),
                )
        await conn.commit()
    finally:
        await conn.close()
    background_tasks.add_task(
        _generate_personalized_analysis_for_user_sync,
        user_id,
        stock_code,
        trade_date,
        model_id,
        cost_price,
    )


def _generate_shared_analysis_for_user_sync(user_id: int, model_id: str) -> None:
    trade_date_value = _resolve_generation_trade_date()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT stock_code, stock_name
            FROM subscriptions
            WHERE user_id = ? AND is_active = 1 AND model_id = ?
            ORDER BY stock_code ASC
            """,
            (user_id, model_id),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        stock_code = str(row["stock_code"])
        stock_name = str(row["stock_name"] or stock_code)
        try:
            market_data = fetch_market_data(
                stock_code=stock_code,
                stock_name=stock_name,
                cost_price=0.0,
                latest_trade_date=trade_date_value,
            )
            news_list = fetch_news(
                stock_code=stock_code,
                latest_trade_date=market_data.latest_trade_date,
                lookback_hours=settings.news_lookback_hours,
                max_items=settings.max_news_items,
            )
            analyze_output = analyze_market_data(
                model_id,
                market_data,
                news_list,
                db_path=settings.db_path,
                settings=settings,
            )
            _upsert_analysis_cache_sync(
                stock_code=stock_code,
                stock_name=market_data.stock_name,
                trade_date_value=market_data.latest_trade_date,
                model_id=model_id,
                status_value="success",
                error_message="",
                market_data_json=market_data_to_json(market_data),
                analysis_json=analysis_result_to_json(analyze_output.analysis),
                news_json=news_list_to_json(news_list),
                actual_provider=analyze_output.actual_provider,
                actual_model_name=analyze_output.actual_model_name,
                provider_response_id=analyze_output.provider_response_id,
            )
        except (DataFetchError, AnalysisError, ValueError) as exc:
            _upsert_analysis_cache_sync(
                stock_code=stock_code,
                stock_name=stock_name,
                trade_date_value=trade_date_value,
                model_id=model_id,
                status_value="failed",
                error_message=str(exc),
                market_data_json="{}",
                analysis_json="{}",
                news_json="[]",
                actual_provider="",
                actual_model_name="",
                provider_response_id="",
            )


async def _model_generation_status(user_id: int, model_id: str) -> dict[str, object]:
    trade_date_value = _resolve_generation_trade_date().isoformat()
    targets = await fetch_all(
        settings.db_path,
        """
        SELECT stock_code
        FROM subscriptions
        WHERE user_id = ? AND is_active = 1 AND model_id = ?
        ORDER BY stock_code ASC
        """,
        (user_id, model_id),
    )
    if not targets:
        return {"state": "ready", "trade_date": trade_date_value, "total": 0, "ready": 0, "failed": 0, "missing": 0}

    ready_count = 0
    failed_count = 0
    missing_count = 0
    for row in targets:
        cache_row = await fetch_one(
            settings.db_path,
            """
            SELECT status
            FROM analysis_cache
            WHERE stock_code = ? AND trade_date = ? AND model_id = ?
            LIMIT 1
            """,
            (str(row["stock_code"]), trade_date_value, model_id),
        )
        if cache_row is None:
            missing_count += 1
            continue
        if str(cache_row["status"]) == "success":
            ready_count += 1
        elif str(cache_row["status"]) == "failed":
            failed_count += 1
        else:
            missing_count += 1

    state = "ready"
    if missing_count > 0:
        state = "generating"
    elif failed_count > 0:
        state = "failed"
    return {
        "state": state,
        "trade_date": trade_date_value,
        "total": len(targets),
        "ready": ready_count,
        "failed": failed_count,
        "missing": missing_count,
    }


async def _email_send_allowed(user_id: int, token_type: str, *, cooldown_seconds: int = EMAIL_RESEND_INTERVAL_SECONDS) -> bool:
    row = await fetch_one(
        settings.db_path,
        """
        SELECT created_at
        FROM email_tokens
        WHERE user_id = ? AND token_type = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id, token_type),
    )
    if row is None or not row["created_at"]:
        return True
    created_at = datetime.fromisoformat(str(row["created_at"]).replace(" ", "T"))
    return (datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)).total_seconds() >= cooldown_seconds


async def _issue_email_token(user_id: int, token_type: str, expires_at: datetime) -> str:
    token = generate_one_time_token()
    token_hash = hash_one_time_token(token)
    conn = await connect(settings.db_path)
    try:
        await conn.execute(
            "UPDATE email_tokens SET used_at = ? WHERE user_id = ? AND token_type = ? AND used_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), user_id, token_type),
        )
        await conn.execute(
            """
            INSERT INTO email_tokens (user_id, token_hash, token_type, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, token_hash, token_type, expires_at.isoformat()),
        )
        await conn.commit()
    finally:
        await conn.close()
    return token


async def _consume_email_token(token: str, token_type: str) -> int | None:
    token_hash = hash_one_time_token(token)
    conn = await connect(settings.db_path)
    try:
        cursor = await conn.execute(
            """
            SELECT id, user_id, expires_at, used_at
            FROM email_tokens
            WHERE token_hash = ? AND token_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (token_hash, token_type),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row["used_at"]:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at < datetime.now(timezone.utc):
            return None
        await conn.execute(
            "UPDATE email_tokens SET used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), int(row["id"])),
        )
        await conn.commit()
        return int(row["user_id"])
    finally:
        await conn.close()


async def _send_verification_email(user_id: int, email: str) -> None:
    _require_mailer_config()
    allowed = await _email_send_allowed(user_id, "verify_email")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Verification email was sent recently")
    token = await _issue_email_token(
        user_id,
        "verify_email",
        datetime.now(timezone.utc) + timedelta(hours=VERIFY_EMAIL_TOKEN_TTL_HOURS),
    )
    verify_url = f"{settings.site_url.rstrip('/')}/auth/verify-email?token={token}"
    html = render_template(
        "email_verify.html",
        {"verify_url": verify_url, "expires_hours": VERIFY_EMAIL_TOKEN_TTL_HOURS},
    )
    text = (
        f"请验证您的盯盘侠邮箱：{verify_url}\n"
        f"该链接 {VERIFY_EMAIL_TOKEN_TTL_HOURS} 小时内有效。"
    )
    try:
        await run_in_threadpool(
            send_resend_email,
            api_key=settings.resend_api_key,
            from_email=settings.mail_from_auth,
            to_email=email,
            subject="验证您的盯盘侠邮箱",
            html=html,
            text=text,
        )
    except MailerError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


async def _send_password_reset_email(user_id: int, email: str) -> None:
    _require_mailer_config()
    allowed = await _email_send_allowed(user_id, "reset_password")
    if not allowed:
        return
    token = await _issue_email_token(
        user_id,
        "reset_password",
        datetime.now(timezone.utc) + timedelta(minutes=RESET_PASSWORD_TOKEN_TTL_MINUTES),
    )
    reset_url = f"{settings.site_url.rstrip('/')}/reset-password?token={token}"
    html = render_template(
        "email_reset_password.html",
        {"reset_url": reset_url, "expires_minutes": RESET_PASSWORD_TOKEN_TTL_MINUTES},
    )
    text = (
        f"请通过以下链接重置您的盯盘侠密码：{reset_url}\n"
        f"该链接 {RESET_PASSWORD_TOKEN_TTL_MINUTES} 分钟内有效。"
    )
    try:
        await run_in_threadpool(
            send_resend_email,
            api_key=settings.resend_api_key,
            from_email=settings.mail_from_auth,
            to_email=email,
            subject="重置您的盯盘侠密码",
            html=html,
            text=text,
        )
    except MailerError:
        return


@app.on_event("startup")
async def startup_event() -> None:
    await init_db(settings.db_path)


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(
        static_dir / "sw.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/manifest.webmanifest")
async def web_manifest(request: Request) -> JSONResponse:
    app_name = _preferred_app_name(request)
    return JSONResponse(
        {
            "id": "/",
            "name": app_name,
            "short_name": app_name,
            "description": "多用户股票日报与 Web Push 提醒服务。",
            "lang": "zh-CN" if app_name == "盯盘侠" else "en",
            "dir": "ltr",
            "start_url": "/dashboard",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#0a0a1a",
            "theme_color": "#0f3460",
            "icons": [
                {
                    "src": "/static/icons/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/static/icons/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        media_type="application/manifest+json",
        headers={
            "Vary": "Accept-Language",
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request,
        "index.html",
        _template_context(request, title="盯盘侠", user=None),
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "login.html", _template_context(request, title="登录", user=None))


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "register.html", _template_context(request, title="注册", user=None))


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request, user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/settings", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "forgot_password.html", _template_context(request, title="忘记密码", user=None))


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = "", user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/settings", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request,
        "reset_password.html",
        _template_context(request, title="重置密码", user=None, token=token),
    )


@app.get("/auth/verify-email", response_class=HTMLResponse)
async def verify_email_page(request: Request, token: str = ""):
    success = False
    if token:
        user_id = await _consume_email_token(token, "verify_email")
        if user_id is not None:
            await execute(
                settings.db_path,
                "UPDATE users SET email_verified_at = ? WHERE id = ? AND email_verified_at = ''",
                (datetime.now(timezone.utc).isoformat(), user_id),
            )
            success = True
    return templates.TemplateResponse(
        request,
        "auth_verify_result.html",
        _template_context(request, title="邮箱验证", user=None, success=success),
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request, user=Depends(page_user_or_redirect)):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    profile = await _load_user_profile(user.id)
    profile_summary = _profile_summary_text(profile)
    current_trade_date = _resolve_generation_trade_date().isoformat()
    subscription_rows = await fetch_all(
        settings.db_path,
        """
        SELECT
            s.id,
            s.stock_code,
            s.stock_name,
            s.cost_price,
            s.model_id,
            mp.display_name AS model_display_name,
            s.is_active,
            s.sort_order,
            s.created_at,
            ac.trade_date AS latest_trade_date,
            ac.market_data_json,
            ac.analysis_json,
            ac.status AS cache_status
        FROM subscriptions s
        LEFT JOIN model_pricing mp
            ON mp.model_id = s.model_id
        LEFT JOIN analysis_cache ac
            ON ac.stock_code = s.stock_code
            AND ac.model_id = s.model_id
            AND ac.status = 'success'
            AND ac.trade_date = (
                SELECT MAX(ac2.trade_date)
                FROM analysis_cache ac2
                WHERE ac2.stock_code = s.stock_code
                  AND ac2.model_id = s.model_id
                  AND ac2.status = 'success'
            )
        WHERE s.user_id = ?
        ORDER BY s.sort_order ASC, s.id ASC
        """,
        (user.id,),
    )
    subscriptions = []
    for row in subscription_rows:
        item = dict(row)
        shared_cache_row = await fetch_one(
            settings.db_path,
            """
            SELECT status
            FROM analysis_cache
            WHERE stock_code = ? AND trade_date = ? AND model_id = ?
            LIMIT 1
            """,
            (str(row["stock_code"]), current_trade_date, str(row["model_id"])),
        )
        if row["market_data_json"] and row["analysis_json"]:
            market_data = market_data_from_json(str(row["market_data_json"]))
            analysis = analysis_result_from_json(str(row["analysis_json"]))
            personalized_state = await _load_personalized_analysis_state(
                user_id=user.id,
                stock_code=str(row["stock_code"]),
                trade_date=str(row["latest_trade_date"]),
                model_id=str(row["model_id"]),
                profile=profile,
            )
            status_label, status_copy = _personalized_dashboard_status(str(personalized_state["state"]))
            item["latest_close_price"] = market_data.snapshot.close_price
            item["latest_change_pct"] = market_data.snapshot.change_pct
            item["latest_summary"] = analysis.executive_summary
            item["latest_trade_date"] = str(row["latest_trade_date"])
            shared_state = "generating"
            if shared_cache_row is not None:
                shared_state = "ready" if str(shared_cache_row["status"]) == "success" else "failed"
            shared_status_label, shared_status_copy = _shared_dashboard_status(
                shared_state,
                current_trade_date=current_trade_date,
                latest_trade_date=item["latest_trade_date"],
            )
            item["shared_state"] = shared_state
            item["shared_status_label"] = shared_status_label
            item["shared_status_copy"] = shared_status_copy
            item["current_trade_date"] = current_trade_date
            item["personalized_state"] = str(personalized_state["state"])
            item["personalized_status_label"] = status_label
            item["personalized_status_copy"] = status_copy
            item["profile_summary"] = profile_summary
            item["personalized_version_note"] = _personalized_version_note(
                profile.context_version,
                int(personalized_state["applied_context_version"]),
            )
            if float(row["cost_price"]) > 0:
                item["latest_pnl_pct"] = ((market_data.snapshot.close_price - float(row["cost_price"])) / float(row["cost_price"])) * 100
            else:
                item["latest_pnl_pct"] = None
        else:
            item["latest_close_price"] = None
            item["latest_change_pct"] = None
            item["latest_summary"] = ""
            item["latest_pnl_pct"] = None
            shared_state = "generating"
            if shared_cache_row is not None:
                shared_state = "ready" if str(shared_cache_row["status"]) == "success" else "failed"
            shared_status_label, shared_status_copy = _shared_dashboard_status(
                shared_state,
                current_trade_date=current_trade_date,
                latest_trade_date=None,
            )
            item["shared_state"] = shared_state
            item["shared_status_label"] = shared_status_label
            item["shared_status_copy"] = shared_status_copy
            item["current_trade_date"] = current_trade_date
            item["personalized_state"] = "waiting_shared"
            item["personalized_status_label"] = "等待共享分析"
            item["personalized_status_copy"] = "共享分析生成后，这里会继续补个性化建议状态。"
            item["profile_summary"] = profile_summary
            item["personalized_version_note"] = _personalized_version_note(profile.context_version, 0)
        subscriptions.append(item)
    models = await fetch_all(
        settings.db_path,
        """
        SELECT model_id, display_name, points_per_call
        FROM model_pricing
        WHERE is_active = 1 AND is_runnable = 1
        ORDER BY sort_order ASC, model_id ASC
        """,
    )
    push_enabled = await has_user_push_subscription(settings.db_path, user_id=user.id)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _template_context(
            request,
            title="自选",
            user=user,
            subscriptions=subscriptions,
            models=models,
            push_enabled=push_enabled,
            push_configured=bool(settings.vapid_public_key and settings.vapid_private_key and settings.vapid_claims_email),
            report_schedule_timezone=settings.report_schedule_timezone,
            report_generate_time=settings.report_generate_time,
            report_email_time=settings.report_email_time,
            report_push_time=settings.report_push_time,
        ),
    )


@app.get("/report/{stock_code}/latest", response_class=HTMLResponse)
async def report_latest_page(request: Request, stock_code: str, user=Depends(page_user_or_redirect)):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    if not user.email_verified_at:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    subscription = await fetch_one(
        settings.db_path,
        """
        SELECT stock_code, stock_name, cost_price, model_id
        FROM subscriptions
        WHERE user_id = ? AND stock_code = ?
        ORDER BY id ASC
        """,
        (user.id, stock_code),
    )
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    latest_row = await fetch_one(
        settings.db_path,
        """
        SELECT trade_date, status
        FROM analysis_cache
        WHERE stock_code = ? AND model_id = ?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (stock_code, str(subscription["model_id"])),
    )
    if latest_row is None:
        return templates.TemplateResponse(
            request,
            "report_empty.html",
            {
                "title": "报告页",
                "user": user,
                "stock_code": stock_code,
                "model_id": str(subscription["model_id"]),
                "cache_status": "missing",
                "trade_date": None,
            },
        )
    if str(latest_row["status"]) == "failed":
        return templates.TemplateResponse(
            request,
            "report_empty.html",
            {
                "title": "报告页",
                "user": user,
                "stock_code": stock_code,
                "model_id": str(subscription["model_id"]),
                "cache_status": "failed",
                "trade_date": str(latest_row["trade_date"]),
            },
        )
    return RedirectResponse(url=f"/report/{stock_code}/{latest_row['trade_date']}", status_code=status.HTTP_302_FOUND)


@app.get("/report/{stock_code}/{trade_date}", response_class=HTMLResponse)
async def report_page(
    request: Request,
    stock_code: str,
    trade_date: str,
    background_tasks: BackgroundTasks,
    user=Depends(page_user_or_redirect),
):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    if not user.email_verified_at:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    subscription = await fetch_one(
        settings.db_path,
        """
        SELECT stock_code, stock_name, cost_price, model_id
        FROM subscriptions
        WHERE user_id = ? AND stock_code = ?
        ORDER BY id ASC
        """,
        (user.id, stock_code),
    )
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    cache_row = await fetch_one(
        settings.db_path,
        """
        SELECT market_data_json, analysis_json, news_json, trade_date, model_id, status
        FROM analysis_cache
        WHERE stock_code = ? AND trade_date = ? AND model_id = ?
        """,
        (stock_code, trade_date, str(subscription["model_id"])),
    )
    if cache_row is None:
        return templates.TemplateResponse(
            request,
            "report_empty.html",
            {
                "title": "报告页",
                "user": user,
                "stock_code": stock_code,
                "model_id": str(subscription["model_id"]),
                "cache_status": "missing",
                "trade_date": trade_date,
            },
        )
    if str(cache_row["status"]) != "success":
        return templates.TemplateResponse(
            request,
            "report_empty.html",
            {
                "title": "报告页",
                "user": user,
                "stock_code": stock_code,
                "model_id": str(subscription["model_id"]),
                "cache_status": "failed",
                "trade_date": trade_date,
            },
        )
    previous_row = await fetch_one(
        settings.db_path,
        """
        SELECT MAX(trade_date) AS trade_date
        FROM analysis_cache
        WHERE stock_code = ? AND model_id = ? AND status = 'success' AND trade_date < ?
        """,
        (stock_code, str(subscription["model_id"]), trade_date),
    )
    next_row = await fetch_one(
        settings.db_path,
        """
        SELECT MIN(trade_date) AS trade_date
        FROM analysis_cache
        WHERE stock_code = ? AND model_id = ? AND status = 'success' AND trade_date > ?
        """,
        (stock_code, str(subscription["model_id"]), trade_date),
    )
    market_data = market_data_from_json(str(cache_row["market_data_json"]))
    analysis = analysis_result_from_json(str(cache_row["analysis_json"]))
    news_list = news_list_from_json(str(cache_row["news_json"]))
    profile = await _load_user_profile(user.id)
    personalized_state = await _load_personalized_analysis_state(
        user_id=user.id,
        stock_code=stock_code,
        trade_date=trade_date,
        model_id=str(subscription["model_id"]),
        profile=profile,
    )
    if personalized_state["state"] in {"missing", "stale"}:
        await _queue_personalized_generation(
            background_tasks,
            user_id=user.id,
            stock_code=stock_code,
            trade_date=trade_date,
            model_id=str(subscription["model_id"]),
            cost_price=float(subscription["cost_price"]),
            profile=profile,
        )
        personalized_state = {
            "state": "generating",
            "result": None,
            "error_message": "",
            "applied_context_version": int(personalized_state["applied_context_version"]),
        }
    context = build_report_context(
        market_data,
        analysis,
        news_list,
        cost_price=float(subscription["cost_price"]),
        model_id=str(cache_row["model_id"]),
        previous_trade_date=str(previous_row["trade_date"]) if previous_row and previous_row["trade_date"] else None,
        next_trade_date=str(next_row["trade_date"]) if next_row and next_row["trade_date"] else None,
    )
    return templates.TemplateResponse(
        request,
        "report.html",
        _template_context(
            request,
            title="报告",
            user=user,
            personalized=personalized_state["result"],
            personalized_state=str(personalized_state["state"]),
            personalized_error_message=str(personalized_state["error_message"]),
            personalized_applied_context_version=int(personalized_state["applied_context_version"]),
            profile=profile,
            **context,
        ),
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user=Depends(page_user_or_redirect)):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    profile = await _load_user_profile(user.id)
    models = await fetch_all(
        settings.db_path,
        """
        SELECT model_id, display_name, points_per_call
        FROM model_pricing
        WHERE is_active = 1 AND is_runnable = 1
        ORDER BY sort_order ASC, model_id ASC
        """,
    )
    return templates.TemplateResponse(
        request,
        "settings_placeholder.html",
        _template_context(request, title="我的", user=user, models=models, profile=profile),
    )


@app.get("/settings/profile", response_class=HTMLResponse)
async def profile_settings_page(request: Request, user=Depends(page_user_or_redirect)):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    profile = await _load_user_profile(user.id)
    return templates.TemplateResponse(
        request,
        "profile_settings.html",
        _template_context(request, title="投资画像", user=user, profile=profile),
    )


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request, user=Depends(admin_user)):
    rows = await fetch_all(
        settings.db_path,
        """
        SELECT
            u.id,
            u.email,
            u.is_active,
            u.email_verified_at,
            u.daily_email_enabled,
            u.preferred_model,
            u.points_balance,
            u.created_at,
            COUNT(DISTINCT s.id) AS subscription_count,
            COUNT(DISTINCT ps.id) AS push_device_count,
            MAX(ed.sent_at) AS last_email_sent_at
        FROM users u
        LEFT JOIN subscriptions s
            ON s.user_id = u.id AND s.is_active = 1
        LEFT JOIN push_subscriptions ps
            ON ps.user_id = u.id
        LEFT JOIN email_deliveries ed
            ON ed.user_id = u.id AND ed.delivery_type = 'daily_report'
        GROUP BY
            u.id,
            u.email,
            u.is_active,
            u.email_verified_at,
            u.daily_email_enabled,
            u.preferred_model,
            u.points_balance,
            u.created_at
        ORDER BY u.created_at DESC, u.id DESC
        """,
    )
    users = [dict(row) for row in rows]
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        _template_context(request, title="账户管理", user=user, managed_users=users),
    )


@app.post("/api/auth/register")
async def register(request: Request, payload: RegisterPayload):
    _require_mailer_config()
    _check_rate_limit("register-ip", _client_host(request), max_attempts=5, window_seconds=60 * 10)
    existing = await fetch_one(
        settings.db_path,
        "SELECT id, email_verified_at FROM users WHERE email = ?",
        (payload.email.lower(),),
    )
    if existing is None:
        user_id = await execute(
            settings.db_path,
            """
            INSERT INTO users (email, password_hash, preferred_model, points_balance)
            VALUES (?, ?, 'gemini', 0)
            """,
            (payload.email.lower(), hash_password(payload.password)),
        )
        await _send_verification_email(user_id, payload.email.lower())
    elif not str(existing["email_verified_at"]):
        await _send_verification_email(int(existing["id"]), payload.email.lower())
    return {
        "ok": True,
        "message": "如果该邮箱可注册，验证邮件已经发送。请先验证邮箱后再登录。",
    }


@app.post("/api/auth/login")
async def login(request: Request, payload: LoginPayload):
    _check_rate_limit("login-ip", _client_host(request), max_attempts=10, window_seconds=60 * 10)
    row = await fetch_one(
        settings.db_path,
        """
        SELECT id, password_hash, email_verified_at
        FROM users
        WHERE email = ? AND is_active = 1
        """,
        (payload.email.lower(),),
    )
    if row is None or not verify_password(payload.password, str(row["password_hash"])):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    response = JSONResponse({"ok": True, "user_id": int(row["id"]), "email_verified": bool(str(row["email_verified_at"]))})
    _set_session_cookie(response, int(row["id"]))
    return response


@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/me")
async def auth_me(user=Depends(current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "is_admin": user.is_admin,
        "preferred_model": user.preferred_model,
        "points_balance": user.points_balance,
        "email_verified": bool(user.email_verified_at),
        "email_verified_at": user.email_verified_at,
        "daily_email_enabled": user.daily_email_enabled,
        "daily_push_time": user.daily_push_time,
        "push_timezone": user.push_timezone,
        "last_daily_push_trade_date": user.last_daily_push_trade_date,
        "report_schedule_timezone": settings.report_schedule_timezone,
        "report_generate_time": settings.report_generate_time,
        "report_email_time": settings.report_email_time,
        "report_push_time": settings.report_push_time,
    }


@app.post("/api/auth/resend-verification")
async def resend_verification(user=Depends(current_user)):
    if user.email_verified_at:
        return {"ok": True, "message": "邮箱已验证。"}
    await _send_verification_email(user.id, user.email)
    return {"ok": True, "message": "验证邮件已重新发送。"}


@app.post("/api/auth/forgot-password")
async def forgot_password(request: Request, payload: ForgotPasswordPayload):
    _check_rate_limit("forgot-ip", _client_host(request), max_attempts=5, window_seconds=60 * 10)
    row = await fetch_one(
        settings.db_path,
        "SELECT id, email FROM users WHERE email = ? AND is_active = 1",
        (payload.email.lower(),),
    )
    if row is not None:
        await _send_password_reset_email(int(row["id"]), str(row["email"]))
    return {"ok": True, "message": "如果该邮箱已注册，您将收到重置密码邮件。"}


@app.post("/api/auth/reset-password")
async def reset_password(payload: ResetPasswordPayload):
    user_id = await _consume_email_token(payload.token, "reset_password")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset link is invalid or expired")
    await execute(
        settings.db_path,
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(payload.password), user_id),
    )
    return {"ok": True, "message": "密码已更新，请使用新密码登录。"}


@app.post("/api/auth/change-password")
async def change_password(payload: ChangePasswordPayload, user=Depends(current_user)):
    row = await fetch_one(
        settings.db_path,
        "SELECT password_hash FROM users WHERE id = ? AND is_active = 1",
        (user.id,),
    )
    if row is None or not verify_password(payload.current_password, str(row["password_hash"])):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    await execute(
        settings.db_path,
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(payload.new_password), user.id),
    )
    return {"ok": True, "message": "密码已更新。"}


@app.post("/api/email/preferences")
async def update_email_preferences(payload: EmailPreferencesPayload, user=Depends(current_user)):
    await execute(
        settings.db_path,
        "UPDATE users SET daily_email_enabled = ? WHERE id = ?",
        (1 if payload.daily_email_enabled and user.email_verified_at else 0, user.id),
    )
    if payload.daily_email_enabled and not user.email_verified_at:
        return {"ok": True, "message": "请先验证邮箱，验证后才能开启每日报告邮件。", "daily_email_enabled": False}
    return {"ok": True, "message": "每日报告邮件设置已更新。", "daily_email_enabled": payload.daily_email_enabled}


@app.post("/api/user/preferences/model")
async def update_model_preference(
    payload: ModelPreferencePayload,
    background_tasks: BackgroundTasks,
    user=Depends(current_user),
):
    model_id = payload.preferred_model.strip()
    row = await _fetch_runnable_model(model_id)
    await execute(
        settings.db_path,
        "UPDATE users SET preferred_model = ? WHERE id = ?",
        (model_id, user.id),
    )
    await execute(
        settings.db_path,
        "UPDATE subscriptions SET model_id = ? WHERE user_id = ?",
        (model_id, user.id),
    )
    background_tasks.add_task(_generate_shared_analysis_for_user_sync, user.id, model_id)
    status_payload = await _model_generation_status(user.id, model_id)
    return {
        "ok": True,
        "message": "分析模型已更新，正在生成对应报告。",
        "preferred_model": str(row["model_id"]),
        "display_name": str(row["display_name"]),
        "generation_status": status_payload,
    }


@app.get("/api/user/preferences/model/status")
async def model_preference_status(user=Depends(current_user)):
    status_payload = await _model_generation_status(user.id, user.preferred_model)
    return {"ok": True, "preferred_model": user.preferred_model, **status_payload}


@app.get("/api/user/profile")
async def user_profile_detail(user=Depends(current_user)):
    profile = await _load_user_profile(user.id)
    return {
        "ok": True,
        "risk_preference": profile.risk_preference,
        "trading_style": profile.trading_style,
        "focus_sectors": profile.focus_sectors,
        "position_notes": profile.position_notes,
        "custom_notes": profile.custom_notes,
        "context_version": profile.context_version,
    }


@app.put("/api/user/profile")
async def update_user_profile(payload: UserProfilePayload, user=Depends(current_user)):
    current_profile = await _load_user_profile(user.id)
    next_focus_sectors = _normalize_focus_sectors(payload.focus_sectors)
    next_risk_preference = payload.risk_preference.strip()
    next_trading_style = payload.trading_style.strip()
    next_position_notes = payload.position_notes.strip()
    next_custom_notes = payload.custom_notes.strip()
    changed = any(
        [
            next_risk_preference != current_profile.risk_preference,
            next_trading_style != current_profile.trading_style,
            next_focus_sectors != current_profile.focus_sectors,
            next_position_notes != current_profile.position_notes,
            next_custom_notes != current_profile.custom_notes,
        ]
    )
    next_context_version = current_profile.context_version + 1 if changed else current_profile.context_version
    await execute(
        settings.db_path,
        """
        INSERT INTO user_profiles (
            user_id, risk_preference, trading_style, focus_sectors_json,
            position_notes, custom_notes, context_version, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            risk_preference=excluded.risk_preference,
            trading_style=excluded.trading_style,
            focus_sectors_json=excluded.focus_sectors_json,
            position_notes=excluded.position_notes,
            custom_notes=excluded.custom_notes,
            context_version=excluded.context_version,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            user.id,
            next_risk_preference,
            next_trading_style,
            json.dumps(next_focus_sectors, ensure_ascii=False),
            next_position_notes,
            next_custom_notes,
            next_context_version,
        ),
    )
    return {
        "ok": True,
        "message": "投资画像已更新。",
        "risk_preference": next_risk_preference,
        "trading_style": next_trading_style,
        "focus_sectors": next_focus_sectors,
        "position_notes": next_position_notes,
        "custom_notes": next_custom_notes,
        "context_version": next_context_version,
        "changed": changed,
    }


@app.get("/api/report/{stock_code}/{trade_date}/personalized-status")
async def personalized_report_status(stock_code: str, trade_date: str, user=Depends(verified_user)):
    subscription = await fetch_one(
        settings.db_path,
        """
        SELECT stock_code, cost_price, model_id
        FROM subscriptions
        WHERE user_id = ? AND stock_code = ?
        ORDER BY id ASC
        """,
        (user.id, stock_code),
    )
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    profile = await _load_user_profile(user.id)
    state_payload = await _load_personalized_analysis_state(
        user_id=user.id,
        stock_code=stock_code,
        trade_date=trade_date,
        model_id=str(subscription["model_id"]),
        profile=profile,
    )
    state_value = str(state_payload["state"])
    if state_value in {"missing", "stale"}:
        state_value = "generating"
    return {
        "ok": True,
        "state": state_value,
        "error_message": state_payload["error_message"],
        "report_url": f"/report/{stock_code}/{trade_date}",
        "context_version": profile.context_version,
        "applied_context_version": int(state_payload["applied_context_version"]),
    }


@app.post("/api/report/{stock_code}/{trade_date}/personalized-generate")
async def personalized_report_generate(
    stock_code: str,
    trade_date: str,
    background_tasks: BackgroundTasks,
    user=Depends(verified_user),
):
    subscription = await fetch_one(
        settings.db_path,
        """
        SELECT stock_code, cost_price, model_id
        FROM subscriptions
        WHERE user_id = ? AND stock_code = ?
        ORDER BY id ASC
        """,
        (user.id, stock_code),
    )
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    profile = await _load_user_profile(user.id)
    await _queue_personalized_generation(
        background_tasks,
        user_id=user.id,
        stock_code=stock_code,
        trade_date=trade_date,
        model_id=str(subscription["model_id"]),
        cost_price=float(subscription["cost_price"]),
        profile=profile,
    )
    return {"ok": True, "state": "generating", "message": "正在重新生成个性化建议。"}


@app.get("/api/admin/users")
async def admin_list_users(user=Depends(admin_user)):
    rows = await fetch_all(
        settings.db_path,
        """
        SELECT
            u.id,
            u.email,
            u.is_active,
            u.email_verified_at,
            u.daily_email_enabled,
            u.preferred_model,
            u.points_balance,
            u.created_at,
            COUNT(DISTINCT s.id) AS subscription_count,
            COUNT(DISTINCT ps.id) AS push_device_count,
            MAX(ed.sent_at) AS last_email_sent_at
        FROM users u
        LEFT JOIN subscriptions s
            ON s.user_id = u.id AND s.is_active = 1
        LEFT JOIN push_subscriptions ps
            ON ps.user_id = u.id
        LEFT JOIN email_deliveries ed
            ON ed.user_id = u.id AND ed.delivery_type = 'daily_report'
        GROUP BY
            u.id,
            u.email,
            u.is_active,
            u.email_verified_at,
            u.daily_email_enabled,
            u.preferred_model,
            u.points_balance,
            u.created_at
        ORDER BY u.created_at DESC, u.id DESC
        """,
    )
    return {"items": [dict(row) for row in rows]}


@app.patch("/api/admin/users/{managed_user_id}")
async def admin_update_user(managed_user_id: int, payload: AdminUserUpdatePayload, user=Depends(admin_user)):
    row = await fetch_one(
        settings.db_path,
        """
        SELECT id, email, is_active, email_verified_at, daily_email_enabled
        FROM users
        WHERE id = ?
        """,
        (managed_user_id,),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if int(row["id"]) == user.id and payload.is_active is False:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot deactivate current admin account")

    next_is_active = int(payload.is_active) if payload.is_active is not None else int(row["is_active"])
    next_email_verified_at = str(row["email_verified_at"])
    if payload.email_verified is not None:
        next_email_verified_at = datetime.now(timezone.utc).isoformat() if payload.email_verified else ""

    if payload.daily_email_enabled is None:
        next_daily_email_enabled = int(row["daily_email_enabled"])
    else:
        next_daily_email_enabled = 1 if payload.daily_email_enabled and next_email_verified_at else 0

    await execute(
        settings.db_path,
        """
        UPDATE users
        SET is_active = ?, email_verified_at = ?, daily_email_enabled = ?
        WHERE id = ?
        """,
        (next_is_active, next_email_verified_at, next_daily_email_enabled, managed_user_id),
    )
    return {
        "ok": True,
        "user": {
            "id": managed_user_id,
            "is_active": bool(next_is_active),
            "email_verified_at": next_email_verified_at,
            "daily_email_enabled": bool(next_daily_email_enabled),
        },
    }


@app.delete("/api/admin/users/{managed_user_id}")
async def admin_delete_user(managed_user_id: int, user=Depends(admin_user)):
    row = await fetch_one(
        settings.db_path,
        """
        SELECT id, email
        FROM users
        WHERE id = ?
        """,
        (managed_user_id,),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if int(row["id"]) == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete current admin account")
    await execute(
        settings.db_path,
        "DELETE FROM users WHERE id = ?",
        (managed_user_id,),
    )
    return {"ok": True, "id": managed_user_id, "email": str(row["email"])}


@app.get("/api/subscriptions")
async def list_subscriptions(user=Depends(verified_user)):
    rows = await fetch_all(
        settings.db_path,
        """
        SELECT id, stock_code, stock_name, cost_price, model_id, is_active, sort_order, created_at
        FROM subscriptions
        WHERE user_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (user.id,),
    )
    return {"items": [dict(row) for row in rows]}


@app.post("/api/subscriptions")
async def create_subscription(payload: SubscriptionCreatePayload, user=Depends(verified_user)):
    stock_code = _normalize_stock_code(payload.stock_code)
    stock_name = payload.stock_name.strip() or stock_code
    await _fetch_runnable_model(user.preferred_model)
    try:
        subscription_id = await execute(
            settings.db_path,
            """
            INSERT INTO subscriptions (user_id, stock_code, stock_name, cost_price, model_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user.id, stock_code, stock_name, payload.cost_price, user.preferred_model),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Failed to create subscription: {exc}") from exc
    return {"ok": True, "id": subscription_id}


@app.put("/api/subscriptions/{subscription_id}")
async def update_subscription(subscription_id: int, payload: SubscriptionUpdatePayload, user=Depends(verified_user)):
    row = await fetch_one(
        settings.db_path,
        "SELECT id, cost_price, model_id, sort_order, is_active FROM subscriptions WHERE id = ? AND user_id = ?",
        (subscription_id, user.id),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    next_cost_price = payload.cost_price if payload.cost_price is not None else float(row["cost_price"])
    next_sort_order = payload.sort_order if payload.sort_order is not None else int(row["sort_order"])
    next_is_active = int(payload.is_active) if payload.is_active is not None else int(row["is_active"])
    await execute(
        settings.db_path,
        """
        UPDATE subscriptions
        SET cost_price = ?, sort_order = ?, is_active = ?
        WHERE id = ? AND user_id = ?
        """,
        (next_cost_price, next_sort_order, next_is_active, subscription_id, user.id),
    )
    return {"ok": True}


@app.delete("/api/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: int, user=Depends(verified_user)):
    row = await fetch_one(
        settings.db_path,
        "SELECT id FROM subscriptions WHERE id = ? AND user_id = ?",
        (subscription_id, user.id),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    await execute(
        settings.db_path,
        "DELETE FROM subscriptions WHERE id = ? AND user_id = ?",
        (subscription_id, user.id),
    )
    return {"ok": True}


@app.get("/api/push/vapid-key")
async def get_vapid_key(user=Depends(verified_user)):
    if not settings.vapid_public_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Web Push is not configured")
    return {"public_key": settings.vapid_public_key}


@app.get("/api/push/status")
async def push_status(user=Depends(verified_user)):
    return {
        "configured": bool(settings.vapid_public_key and settings.vapid_private_key and settings.vapid_claims_email),
        "enabled": await has_user_push_subscription(settings.db_path, user_id=user.id),
        "count": len(await get_user_push_subscriptions(settings.db_path, user_id=user.id)),
    }


@app.post("/api/push/subscribe")
async def subscribe_push(request: Request, payload: PushSubscribePayload, user=Depends(verified_user)):
    if not (settings.vapid_public_key and settings.vapid_private_key and settings.vapid_claims_email):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Web Push is not configured")
    p256dh = payload.keys.get("p256dh", "").strip()
    auth = payload.keys.get("auth", "").strip()
    endpoint = payload.endpoint.strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid subscription payload")
    await upsert_push_subscription(
        settings.db_path,
        user_id=user.id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=request.headers.get("user-agent", ""),
    )
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def unsubscribe_push(payload: dict[str, str], user=Depends(verified_user)):
    endpoint = payload.get("endpoint", "").strip()
    if not endpoint:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="endpoint is required")
    await delete_push_subscription(settings.db_path, user_id=user.id, endpoint=endpoint)
    return {"ok": True}


@app.post("/api/push/preferences")
async def update_push_preferences(user=Depends(verified_user)):
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Push delivery time is managed by system configuration.",
    )


@app.post("/api/push/test")
async def send_test_push(user=Depends(verified_user)):
    try:
        delivered = await send_push_to_user(
            settings.db_path,
            user_id=user.id,
            payload={
                "title": "盯盘侠测试通知",
                "body": (
                    f"这是发送到 {user.email} 的测试通知。"
                    f"系统定时推送时间为 {settings.report_push_time} {settings.report_schedule_timezone}，点击可返回面板。"
                ),
                "tag": "dingpan-test-notification",
                "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                "renotify": True,
                "url": "/dashboard",
            },
            public_key=settings.vapid_public_key,
            private_key=settings.vapid_private_key,
            claims_email=settings.vapid_claims_email,
        )
    except PushError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "delivered": delivered}
