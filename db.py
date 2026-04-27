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

CARD_NUMBER = "0000 0000 0000 0000"  # ← замінити на реальний номер картки

TRIAL_DAYS = 2
PRICE_SMALL = 250   # до 15 брендів
PRICE_BIG   = 380   # більше 15 брендів
BRAND_LIMIT = 15    # межа тарифів


def calc_amount(brands_count: int) -> int:
    return PRICE_SMALL if brands_count <= BRAND_LIMIT else PRICE_BIG


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
                    telegram_id BIGINT PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    trial_used  BOOLEAN DEFAULT FALSE,
                    created_at  TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT REFERENCES users(telegram_id),
                    brands      TEXT[],
                    amount      INT,
                    expires_at  TIMESTAMP,
                    active      BOOLEAN DEFAULT FALSE,
                    created_at  TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT REFERENCES users(telegram_id),
                    brands      TEXT[],
                    amount      INT,
                    status      TEXT DEFAULT 'pending',
                    created_at  TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
    logger.info("DB initialized")


def upsert_user(telegram_id: int, username: str, first_name: str) -> dict:
    """Створює або оновлює користувача. Повертає запис."""
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
    """Запускає пробний період. Повертає дату закінчення."""
    expires = datetime.now() + timedelta(days=TRIAL_DAYS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Деактивуємо попередні
            cur.execute("UPDATE subscriptions SET active = FALSE WHERE telegram_id = %s", (telegram_id,))
            cur.execute("""
                INSERT INTO subscriptions (telegram_id, brands, amount, expires_at, active)
                VALUES (%s, %s, 0, %s, TRUE)
            """, (telegram_id, brands, expires))
            cur.execute("UPDATE users SET trial_used = TRUE WHERE telegram_id = %s", (telegram_id,))
        conn.commit()
    return expires


def get_active_subscription(telegram_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM subscriptions
                WHERE telegram_id = %s AND active = TRUE AND expires_at > NOW()
                ORDER BY expires_at DESC LIMIT 1
            """, (telegram_id,))
            return cur.fetchone()


def create_payment(telegram_id: int, brands: list, amount: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO payments (telegram_id, brands, amount, status)
                VALUES (%s, %s, %s, 'pending') RETURNING id
            """, (telegram_id, brands, amount))
            payment_id = cur.fetchone()["id"]
        conn.commit()
    return payment_id


def activate_subscription(telegram_id: int) -> bool:
    """Адмін активує підписку. Бере останній pending payment."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM payments
                WHERE telegram_id = %s AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
            """, (telegram_id,))
            payment = cur.fetchone()
            if not payment:
                return False

            cur.execute("UPDATE subscriptions SET active = FALSE WHERE telegram_id = %s", (telegram_id,))
            cur.execute("""
                INSERT INTO subscriptions (telegram_id, brands, amount, expires_at, active)
                VALUES (%s, %s, %s, %s, TRUE)
            """, (
                telegram_id,
                payment["brands"],
                payment["amount"],
                datetime.now() + timedelta(days=30)
            ))
            cur.execute("UPDATE payments SET status = 'paid' WHERE id = %s", (payment["id"],))
        conn.commit()
    return True


def get_all_brands() -> list[str]:
    """Повертає унікальний список брендів з таблиці Curtain Price, відсортований."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT supplier FROM "Curtain Price"
                WHERE supplier IS NOT NULL
                ORDER BY supplier
            """)
            return [row["supplier"] for row in cur.fetchall()]


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


def get_pending_payments():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, u.username, u.first_name
                FROM payments p
                JOIN users u ON u.telegram_id = p.telegram_id
                WHERE p.status = 'pending'
                ORDER BY p.created_at DESC
            """)
            return cur.fetchall()


def get_all_users_with_subs():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.telegram_id, u.username, u.first_name, u.trial_used,
                       s.brands, s.amount, s.expires_at, s.active
                FROM users u
                LEFT JOIN subscriptions s ON s.telegram_id = u.telegram_id
                    AND s.active = TRUE AND s.expires_at > NOW()
                ORDER BY u.created_at DESC
            """)
            return cur.fetchall()
