from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import subprocess

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
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
from src.mailer import MailerError, render_template, send_resend_email
from src.push import (
    PushError,
    delete_push_subscription,
    get_user_push_subscriptions,
    has_user_push_subscription,
    send_push_to_user,
    upsert_push_subscription,
)
from src.render_report import (
    analysis_result_from_json,
    build_report_context,
    market_data_from_json,
    news_list_from_json,
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


class SubscriptionCreatePayload(BaseModel):
    stock_code: str = Field(min_length=1, max_length=16)
    stock_name: str = Field(default="", max_length=64)
    cost_price: float = 0.0
    model_id: str = Field(default="gemini", max_length=32)


class SubscriptionUpdatePayload(BaseModel):
    cost_price: float | None = None
    model_id: str | None = Field(default=None, max_length=32)
    sort_order: int | None = None
    is_active: bool | None = None


class PushSubscribePayload(BaseModel):
    endpoint: str
    keys: dict[str, str]


class AdminUserUpdatePayload(BaseModel):
    is_active: bool | None = None
    email_verified: bool | None = None
    daily_email_enabled: bool | None = None


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
    subscription_rows = await fetch_all(
        settings.db_path,
        """
        SELECT
            s.id,
            s.stock_code,
            s.stock_name,
            s.cost_price,
            s.model_id,
            s.is_active,
            s.sort_order,
            s.created_at,
            ac.trade_date AS latest_trade_date,
            ac.market_data_json,
            ac.analysis_json,
            ac.status AS cache_status
        FROM subscriptions s
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
        if row["market_data_json"] and row["analysis_json"]:
            market_data = market_data_from_json(str(row["market_data_json"]))
            analysis = analysis_result_from_json(str(row["analysis_json"]))
            item["latest_close_price"] = market_data.snapshot.close_price
            item["latest_change_pct"] = market_data.snapshot.change_pct
            item["latest_summary"] = analysis.executive_summary
            item["latest_trade_date"] = str(row["latest_trade_date"])
            if float(row["cost_price"]) > 0:
                item["latest_pnl_pct"] = ((market_data.snapshot.close_price - float(row["cost_price"])) / float(row["cost_price"])) * 100
            else:
                item["latest_pnl_pct"] = None
        else:
            item["latest_close_price"] = None
            item["latest_change_pct"] = None
            item["latest_summary"] = ""
            item["latest_pnl_pct"] = None
        subscriptions.append(item)
    models = await fetch_all(
        settings.db_path,
        """
        SELECT model_id, display_name, points_per_call
        FROM model_pricing
        WHERE is_active = 1
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
        SELECT trade_date
        FROM analysis_cache
        WHERE stock_code = ? AND model_id = ? AND status = 'success'
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
            },
        )
    return RedirectResponse(url=f"/report/{stock_code}/{latest_row['trade_date']}", status_code=status.HTTP_302_FOUND)


@app.get("/report/{stock_code}/{trade_date}", response_class=HTMLResponse)
async def report_page(request: Request, stock_code: str, trade_date: str, user=Depends(page_user_or_redirect)):
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
        SELECT market_data_json, analysis_json, news_json, trade_date, model_id
        FROM analysis_cache
        WHERE stock_code = ? AND trade_date = ? AND model_id = ? AND status = 'success'
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
        _template_context(request, title="报告", user=user, **context),
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user=Depends(page_user_or_redirect)):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    models = await fetch_all(
        settings.db_path,
        """
        SELECT model_id, display_name, points_per_call
        FROM model_pricing
        ORDER BY sort_order ASC, model_id ASC
        """,
    )
    return templates.TemplateResponse(
        request,
        "settings_placeholder.html",
        _template_context(request, title="我的", user=user, models=models),
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
async def update_model_preference(payload: ModelPreferencePayload, user=Depends(current_user)):
    model_id = payload.preferred_model.strip()
    row = await fetch_one(
        settings.db_path,
        """
        SELECT model_id, display_name
        FROM model_pricing
        WHERE model_id = ? AND is_active = 1
        """,
        (model_id,),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected model is not available")
    await execute(
        settings.db_path,
        "UPDATE users SET preferred_model = ? WHERE id = ?",
        (model_id, user.id),
    )
    return {
        "ok": True,
        "message": "默认模型已更新。",
        "preferred_model": str(row["model_id"]),
        "display_name": str(row["display_name"]),
    }


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
    try:
        subscription_id = await execute(
            settings.db_path,
            """
            INSERT INTO subscriptions (user_id, stock_code, stock_name, cost_price, model_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user.id, stock_code, stock_name, payload.cost_price, payload.model_id),
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
    next_model_id = payload.model_id if payload.model_id is not None else str(row["model_id"])
    next_sort_order = payload.sort_order if payload.sort_order is not None else int(row["sort_order"])
    next_is_active = int(payload.is_active) if payload.is_active is not None else int(row["is_active"])
    await execute(
        settings.db_path,
        """
        UPDATE subscriptions
        SET cost_price = ?, model_id = ?, sort_order = ?, is_active = ?
        WHERE id = ? AND user_id = ?
        """,
        (next_cost_price, next_model_id, next_sort_order, next_is_active, subscription_id, user.id),
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
