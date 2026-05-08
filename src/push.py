from __future__ import annotations

from dataclasses import dataclass
import json

from src.database import connect, execute, fetch_all, fetch_one

try:
    from pywebpush import WebPushException, webpush
except ModuleNotFoundError:  # pragma: no cover - dependency optional during local setup
    WebPushException = RuntimeError
    webpush = None


class PushError(RuntimeError):
    pass


@dataclass(frozen=True)
class PushKeys:
    public_key: str
    private_key: str
    claims_email: str


@dataclass(frozen=True)
class DuePushUser:
    user_id: int
    email: str
    last_daily_push_trade_date: str


def _require_keys(public_key: str | None, private_key: str | None, claims_email: str | None) -> PushKeys:
    if webpush is None:
        raise PushError("pywebpush is not installed. Run `pip install -r requirements.txt` in the project virtualenv.")
    if not public_key or not private_key or not claims_email:
        raise PushError("Web Push is not configured. Set VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, and VAPID_CLAIMS_EMAIL.")
    return PushKeys(public_key=public_key, private_key=private_key, claims_email=claims_email)


async def upsert_push_subscription(
    db_path: str,
    *,
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str,
) -> None:
    conn = await connect(db_path)
    try:
        await conn.execute(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                user_id=excluded.user_id,
                p256dh=excluded.p256dh,
                auth=excluded.auth,
                user_agent=excluded.user_agent
            """,
            (user_id, endpoint, p256dh, auth, user_agent),
        )
        await conn.commit()
    finally:
        await conn.close()


async def delete_push_subscription(db_path: str, *, user_id: int, endpoint: str) -> None:
    await execute(
        db_path,
        "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
        (user_id, endpoint),
    )


async def get_user_push_subscriptions(db_path: str, *, user_id: int) -> list[dict[str, str]]:
    rows = await fetch_all(
        db_path,
        """
        SELECT endpoint, p256dh, auth, user_agent
        FROM push_subscriptions
        WHERE user_id = ?
        ORDER BY created_at DESC
        """,
        (user_id,),
    )
    return [
        {
            "endpoint": str(row["endpoint"]),
            "p256dh": str(row["p256dh"]),
            "auth": str(row["auth"]),
            "user_agent": str(row["user_agent"]),
        }
        for row in rows
    ]


async def has_user_push_subscription(db_path: str, *, user_id: int) -> bool:
    row = await fetch_one(
        db_path,
        "SELECT 1 FROM push_subscriptions WHERE user_id = ? LIMIT 1",
        (user_id,),
    )
    return row is not None


async def send_push_message(
    db_path: str,
    *,
    endpoint: str,
    payload: str,
    public_key: str | None,
    private_key: str | None,
    claims_email: str | None,
) -> None:
    keys = _require_keys(public_key, private_key, claims_email)
    row = await fetch_one(
        db_path,
        """
        SELECT endpoint, p256dh, auth
        FROM push_subscriptions
        WHERE endpoint = ?
        """,
        (endpoint,),
    )
    if row is None:
        raise PushError("Push subscription not found")
    subscription_info = {
        "endpoint": str(row["endpoint"]),
        "keys": {
            "p256dh": str(row["p256dh"]),
            "auth": str(row["auth"]),
        },
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=keys.private_key,
            vapid_claims={"sub": f"mailto:{keys.claims_email}"},
        )
    except WebPushException as exc:
        raise PushError(str(exc)) from exc


async def send_push_to_user(
    db_path: str,
    *,
    user_id: int,
    payload: dict[str, str],
    public_key: str | None,
    private_key: str | None,
    claims_email: str | None,
) -> int:
    subscriptions = await get_user_push_subscriptions(db_path, user_id=user_id)
    if not subscriptions:
        raise PushError("No push subscriptions found for this user")

    payload_json = json.dumps(payload, ensure_ascii=False)
    delivered = 0
    for subscription in subscriptions:
        try:
            await send_push_message(
                db_path,
                endpoint=subscription["endpoint"],
                payload=payload_json,
                public_key=public_key,
                private_key=private_key,
                claims_email=claims_email,
            )
            delivered += 1
        except PushError as exc:
            message = str(exc)
            if "410" in message or "404" in message:
                await delete_push_subscription(db_path, user_id=user_id, endpoint=subscription["endpoint"])
            else:
                raise
    if delivered == 0:
        raise PushError("Push send failed for all subscriptions")
    return delivered


async def load_due_push_users(db_path: str, *, trade_date: str) -> list[DuePushUser]:
    rows = await fetch_all(
        db_path,
        """
        SELECT DISTINCT
            u.id,
            u.email,
            u.last_daily_push_trade_date
        FROM users u
        INNER JOIN push_subscriptions ps ON ps.user_id = u.id
        INNER JOIN subscriptions s ON s.user_id = u.id AND s.is_active = 1
        INNER JOIN analysis_cache ac
            ON ac.stock_code = s.stock_code
            AND ac.model_id = s.model_id
            AND ac.trade_date = ?
            AND ac.status = 'success'
        WHERE u.is_active = 1
        """,
        (trade_date,),
    )
    due_users: list[DuePushUser] = []
    for row in rows:
        last_trade_date = str(row["last_daily_push_trade_date"])
        if last_trade_date == trade_date:
            continue
        due_users.append(
            DuePushUser(
                user_id=int(row["id"]),
                email=str(row["email"]),
                last_daily_push_trade_date=last_trade_date,
            )
        )
    return due_users


async def load_user_latest_report_target(db_path: str, *, user_id: int, trade_date: str) -> dict[str, str] | None:
    row = await fetch_one(
        db_path,
        """
        SELECT s.stock_code, s.stock_name, s.model_id
        FROM subscriptions s
        INNER JOIN analysis_cache ac
            ON ac.stock_code = s.stock_code
            AND ac.model_id = s.model_id
            AND ac.trade_date = ?
            AND ac.status = 'success'
        WHERE s.user_id = ? AND s.is_active = 1
        ORDER BY s.sort_order ASC, s.id ASC
        LIMIT 1
        """,
        (trade_date, user_id),
    )
    if row is None:
        return None
    return {
        "stock_code": str(row["stock_code"]),
        "stock_name": str(row["stock_name"]),
        "model_id": str(row["model_id"]),
    }


async def mark_daily_push_sent(db_path: str, *, user_id: int, trade_date: str, sent_at_iso: str) -> None:
    await execute(
        db_path,
        """
        UPDATE users
        SET last_daily_push_trade_date = ?, last_daily_push_sent_at = ?
        WHERE id = ?
        """,
        (trade_date, sent_at_iso, user_id),
    )
