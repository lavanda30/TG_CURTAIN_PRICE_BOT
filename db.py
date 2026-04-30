"""
db.py — робота з PostgreSQL (Railway)
Змінна середовища: DATABASE_URL
"""
import os
import logging
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

TRIAL_DAYS = 2

# Бренди-виключення (не показувати в списку)
EXCLUDED_BRANDS = {"NoName", "NO NAME", "прайс 01.10.2025"}


def get_conn():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL не встановлено")
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id         BIGINT PRIMARY KEY,
                    username            TEXT,
                    first_name          TEXT,
                    trial_used          BOOLEAN DEFAULT FALSE,
                    purchase_requested  BOOLEAN DEFAULT FALSE,
                    purchase_q1         TEXT,
                    purchase_q2         TEXT,
                    created_at          TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id               SERIAL PRIMARY KEY,
                    telegram_id      BIGINT REFERENCES users(telegram_id),
                    brands           TEXT[],
                    expires_at       TIMESTAMP,
                    active           BOOLEAN DEFAULT FALSE,
                    manually_active  BOOLEAN DEFAULT FALSE,
                    created_at       TIMESTAMP DEFAULT NOW()
                );
            """)

            # Міграція: додаємо колонки якщо ще нема
            for col_def in [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS purchase_requested BOOLEAN DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS purchase_q1 TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS purchase_q2 TEXT",
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS manually_active BOOLEAN DEFAULT FALSE",
            ]:
                try:
                    cur.execute(col_def)
                except Exception:
                    pass

        conn.commit()
    logger.info("DB initialized")


def upsert_user(telegram_id: int, username: str, first_name: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (telegram_id, username, first_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
                RETURNING *
            """, (telegram_id, username, first_name))
            result = cur.fetchone()
        conn.commit()
    return result


def get_user(telegram_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            return cur.fetchone()


def start_trial(telegram_id: int, brands: list) -> datetime:
    """Запускає пробний доступ на TRIAL_DAYS днів."""
    expires = datetime.now() + timedelta(days=TRIAL_DAYS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE telegram_id = %s", (telegram_id,))
            row = cur.fetchone()
            username = row["username"] if row else None
            cur.execute("UPDATE subscriptions SET active = FALSE WHERE telegram_id = %s", (telegram_id,))
            cur.execute("""
                INSERT INTO subscriptions (telegram_id, brands, expires_at, active, username)
                VALUES (%s, %s, %s, TRUE, %s)
            """, (telegram_id, brands, expires, username))
            cur.execute("UPDATE users SET trial_used = TRUE WHERE telegram_id = %s", (telegram_id,))
        conn.commit()
    return expires


def update_trial_brands(telegram_id: int, brands: list):
    """Оновлює список брендів в активній підписці (під час триалу)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE subscriptions SET brands = %s
                WHERE telegram_id = %s AND active = TRUE AND expires_at > NOW()
            """, (brands, telegram_id))
        conn.commit()


def get_active_subscription(telegram_id: int):
    """
    Повертає активну підписку.
    Активна = (active=TRUE AND expires_at > NOW()) АБО manually_active=TRUE.
    Якщо manually_active — expires_at не перевіряємо (безстроково).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM subscriptions
                WHERE telegram_id = %s
                  AND active = TRUE
                  AND (manually_active = TRUE OR expires_at > NOW())
                ORDER BY expires_at DESC LIMIT 1
            """, (telegram_id,))
            return cur.fetchone()


def save_purchase_request(telegram_id: int, q1: str, q2: str):
    """Зберігає відповіді на питання при запиті на придбання."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users SET purchase_q1 = %s, purchase_q2 = %s
                WHERE telegram_id = %s
            """, (q1, q2, telegram_id))
        conn.commit()


def set_purchase_requested_flag(telegram_id: int):
    """Ставить purchase_requested=True БЕЗ деактивації підписки."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET purchase_requested = TRUE WHERE telegram_id = %s",
                (telegram_id,)
            )
        conn.commit()



def mark_purchase_requested(telegram_id: int):
    """Позначає, що юзер зробив запит на придбання (закриває доступ)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Деактивуємо підписку і ставимо прапорець
            cur.execute("UPDATE subscriptions SET active = FALSE WHERE telegram_id = %s", (telegram_id,))
            cur.execute("UPDATE users SET purchase_requested = TRUE WHERE telegram_id = %s", (telegram_id,))
        conn.commit()


def get_last_brands(telegram_id: int) -> list:
    """Повертає бренди з останньої підписки юзера (навіть неактивної)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT brands FROM subscriptions
                WHERE telegram_id = %s
                ORDER BY created_at DESC LIMIT 1
            """, (telegram_id,))
            row = cur.fetchone()
    return list(row["brands"]) if row and row["brands"] else []


def get_all_brands() -> list[str]:
    """Повертає унікальний список брендів, виключаючи EXCLUDED_BRANDS."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT supplier FROM "Curtain Price"
                WHERE supplier IS NOT NULL
                ORDER BY supplier
            """)
            all_b = [row["supplier"] for row in cur.fetchall()]

    # Фільтруємо виключення (точне збігання та часткове для прайсу)
    result = []
    for b in all_b:
        b_lower = b.strip().lower()
        skip = False
        for excl in EXCLUDED_BRANDS:
            if excl.lower() in b_lower or b_lower in excl.lower():
                skip = True
                break
        if not skip:
            result.append(b)
    return result


def get_prices_for_brands(brands: list) -> dict:
    """Повертає dict {supplier: [rows]} для обраних брендів."""
    if not brands:
        return {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM "Curtain Price"
                WHERE supplier = ANY(%s)
                ORDER BY supplier, sku
            """, (brands,))
            rows = cur.fetchall()

    result = {}
    for row in rows:
        supplier = row["supplier"]
        result.setdefault(supplier, []).append(dict(row))
    return result


def get_all_users_with_subs():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.telegram_id, u.username, u.first_name, u.trial_used,
                       u.purchase_requested,
                       s.brands, s.expires_at, s.active
                FROM users u
                LEFT JOIN LATERAL (
                    SELECT brands, expires_at, active
                    FROM subscriptions
                    WHERE telegram_id = u.telegram_id
                    ORDER BY created_at DESC LIMIT 1
                ) s ON TRUE
                ORDER BY u.created_at DESC
            """)
            return cur.fetchall()
