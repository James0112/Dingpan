from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field

from src.auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    get_optional_user,
    hash_password,
    require_user,
    verify_password,
)
from src.config import load_settings
from src.database import execute, fetch_all, fetch_one, init_db
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
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


class RegisterPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


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


class PushPreferencesPayload(BaseModel):
    daily_push_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    push_timezone: str = Field(min_length=1, max_length=64)


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


async def optional_user(request: Request):
    return await get_optional_user(request, settings.db_path, settings.jwt_secret)


async def page_user_or_redirect(request: Request):
    user = await get_optional_user(request, settings.db_path, settings.jwt_secret)
    if user is None:
        return None
    return user


def _preferred_app_name(request: Request) -> str:
    accept_language = request.headers.get("accept-language", "").lower()
    if "zh" in accept_language:
        return "盯盘侠"
    return "DingPan"


@app.on_event("startup")
async def startup_event() -> None:
    await init_db(settings.db_path)


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(static_dir / "sw.js", media_type="application/javascript")


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
        headers={"Vary": "Accept-Language"},
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "title": "盯盘侠",
            "user": None,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "login.html", {"title": "登录", "user": None})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, user=Depends(optional_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "register.html", {"title": "注册", "user": None})


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
        {
            "title": "Dashboard",
            "user": user,
            "subscriptions": subscriptions,
            "models": models,
            "push_enabled": push_enabled,
            "push_configured": bool(settings.vapid_public_key and settings.vapid_private_key and settings.vapid_claims_email),
            "daily_push_time": user.daily_push_time,
            "push_timezone": user.push_timezone,
        },
    )


@app.get("/report/{stock_code}/latest", response_class=HTMLResponse)
async def report_latest_page(request: Request, stock_code: str, user=Depends(page_user_or_redirect)):
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
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
        {
            "title": "报告页",
            "user": user,
            **context,
        },
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
        {
            "title": "设置",
            "user": user,
            "models": models,
        },
    )


@app.post("/api/auth/register")
async def register(payload: RegisterPayload):
    existing = await fetch_one(
        settings.db_path,
        "SELECT id FROM users WHERE email = ?",
        (payload.email.lower(),),
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user_id = await execute(
        settings.db_path,
        """
        INSERT INTO users (email, password_hash, preferred_model, points_balance)
        VALUES (?, ?, 'gemini', 0)
        """,
        (payload.email.lower(), hash_password(payload.password)),
    )
    response = JSONResponse({"ok": True, "user_id": user_id})
    _set_session_cookie(response, user_id)
    return response


@app.post("/api/auth/login")
async def login(payload: LoginPayload):
    row = await fetch_one(
        settings.db_path,
        """
        SELECT id, password_hash
        FROM users
        WHERE email = ? AND is_active = 1
        """,
        (payload.email.lower(),),
    )
    if row is None or not verify_password(payload.password, str(row["password_hash"])):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    response = JSONResponse({"ok": True, "user_id": int(row["id"])})
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
        "preferred_model": user.preferred_model,
        "points_balance": user.points_balance,
        "daily_push_time": user.daily_push_time,
        "push_timezone": user.push_timezone,
        "last_daily_push_trade_date": user.last_daily_push_trade_date,
    }


@app.get("/api/subscriptions")
async def list_subscriptions(user=Depends(current_user)):
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
async def create_subscription(payload: SubscriptionCreatePayload, user=Depends(current_user)):
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
async def update_subscription(subscription_id: int, payload: SubscriptionUpdatePayload, user=Depends(current_user)):
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
async def delete_subscription(subscription_id: int, user=Depends(current_user)):
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
async def get_vapid_key(user=Depends(current_user)):
    if not settings.vapid_public_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Web Push is not configured")
    return {"public_key": settings.vapid_public_key}


@app.get("/api/push/status")
async def push_status(user=Depends(current_user)):
    return {
        "configured": bool(settings.vapid_public_key and settings.vapid_private_key and settings.vapid_claims_email),
        "enabled": await has_user_push_subscription(settings.db_path, user_id=user.id),
        "count": len(await get_user_push_subscriptions(settings.db_path, user_id=user.id)),
    }


@app.post("/api/push/subscribe")
async def subscribe_push(request: Request, payload: PushSubscribePayload, user=Depends(current_user)):
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
async def unsubscribe_push(payload: dict[str, str], user=Depends(current_user)):
    endpoint = payload.get("endpoint", "").strip()
    if not endpoint:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="endpoint is required")
    await delete_push_subscription(settings.db_path, user_id=user.id, endpoint=endpoint)
    return {"ok": True}


@app.post("/api/push/preferences")
async def update_push_preferences(payload: PushPreferencesPayload, user=Depends(current_user)):
    hour_text, minute_text = payload.daily_push_time.split(":")
    hour = int(hour_text)
    minute = int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid daily_push_time")
    await execute(
        settings.db_path,
        """
        UPDATE users
        SET daily_push_time = ?, push_timezone = ?
        WHERE id = ?
        """,
        (payload.daily_push_time, payload.push_timezone.strip(), user.id),
    )
    return {"ok": True}


@app.post("/api/push/test")
async def send_test_push(user=Depends(current_user)):
    try:
        delivered = await send_push_to_user(
            settings.db_path,
            user_id=user.id,
            payload={
                "title": "盯盘侠测试推送",
                "body": f"这是发送到 {user.email} 的测试通知，当前设定时间为 {user.daily_push_time} {user.push_timezone}。",
                "url": "/dashboard",
            },
            public_key=settings.vapid_public_key,
            private_key=settings.vapid_private_key,
            claims_email=settings.vapid_claims_email,
        )
    except PushError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "delivered": delivered}
