import asyncpg
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple   # <-- добавили Tuple

DATABASE_URL = os.getenv("DATABASE_URL")

async def get_connection():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await get_connection()
    # Таблица users
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            username TEXT,
            first_name TEXT,
            subscription_until TIMESTAMP,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Таблица для нескольких токенов
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_vk_tokens (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
            token TEXT NOT NULL,
            name TEXT DEFAULT 'Аккаунт VK',
            is_active BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_checked TIMESTAMP,
            stats JSONB DEFAULT '{}'
        )
    ''')
    # Миграция старых токенов (если были в users.vk_token)
    await conn.execute('''
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='vk_token') THEN
                INSERT INTO user_vk_tokens (user_id, token, name, is_active)
                SELECT telegram_id, vk_token, 'Аккаунт VK', TRUE
                FROM users WHERE vk_token IS NOT NULL AND vk_token != ''
                ON CONFLICT DO NOTHING;
                ALTER TABLE users DROP COLUMN vk_token;
            END IF;
        END $$;
    ''')
    # Таблица mailings
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS mailings (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            total_recipients INTEGER,
            sent_success INTEGER,
            sent_error INTEGER,
            total_time_seconds REAL,
            message_text TEXT,
            vk_account_name TEXT
        )
    ''')
    # Таблица templates
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            name TEXT,
            content TEXT,
            delay REAL DEFAULT 3.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Таблица invoices
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id SERIAL PRIMARY KEY,
            invoice_id TEXT UNIQUE,
            user_id BIGINT,
            amount REAL,
            currency TEXT,
            days INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    await conn.close()

# ----- Users -----
async def add_user(telegram_id: int, username: str = None, first_name: str = None):
    conn = await get_connection()
    await conn.execute('''
        INSERT INTO users (telegram_id, username, first_name) VALUES ($1, $2, $3)
        ON CONFLICT (telegram_id) DO UPDATE 
        SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
    ''', telegram_id, username, first_name)
    await conn.close()

async def get_user(telegram_id: int) -> Optional[Dict]:
    conn = await get_connection()
    row = await conn.fetchrow('SELECT telegram_id, username, first_name, subscription_until, joined_at FROM users WHERE telegram_id = $1', telegram_id)
    await conn.close()
    return dict(row) if row else None

async def get_user_subscription(telegram_id: int) -> Optional[datetime]:
    conn = await get_connection()
    val = await conn.fetchval('SELECT subscription_until FROM users WHERE telegram_id = $1', telegram_id)
    await conn.close()
    return val

async def set_subscription(telegram_id: int, days: float):
    until = datetime.now() + timedelta(days=days)
    conn = await get_connection()
    await conn.execute('UPDATE users SET subscription_until = $1 WHERE telegram_id = $2', until, telegram_id)
    await conn.close()

async def revoke_subscription(telegram_id: int):
    conn = await get_connection()
    await conn.execute('UPDATE users SET subscription_until = NULL WHERE telegram_id = $1', telegram_id)
    await conn.close()

async def get_all_users() -> List[int]:
    conn = await get_connection()
    rows = await conn.fetch('SELECT telegram_id FROM users')
    await conn.close()
    return [r['telegram_id'] for r in rows]

async def get_bot_stats() -> Dict:
    conn = await get_connection()
    total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
    total_mailings = await conn.fetchval('SELECT COUNT(*) FROM mailings')
    total_sent = await conn.fetchval('SELECT COALESCE(SUM(sent_success),0) FROM mailings')
    total_errors = await conn.fetchval('SELECT COALESCE(SUM(sent_error),0) FROM mailings')
    await conn.close()
    return {'users': total_users, 'mailings': total_mailings, 'sent': total_sent, 'errors': total_errors}

async def save_mailing_stats(user_id: int, total: int, success: int, error: int, total_time: float, message: str, vk_name: str):
    conn = await get_connection()
    await conn.execute('''
        INSERT INTO mailings (user_id, started_at, finished_at, total_recipients, sent_success, sent_error, total_time_seconds, message_text, vk_account_name)
        VALUES ($1, $2, $2, $3, $4, $5, $6, $7, $8)
    ''', user_id, datetime.now(), total, success, error, total_time, message[:500], vk_name)
    await conn.close()

async def get_mailing_stats(limit=20) -> List[Dict]:
    conn = await get_connection()
    rows = await conn.fetch('''
        SELECT id, user_id, started_at, total_recipients, sent_success, sent_error, total_time_seconds, vk_account_name
        FROM mailings ORDER BY started_at DESC LIMIT $1
    ''', limit)
    await conn.close()
    return [dict(r) for r in rows]

async def get_user_mailing_stats(user_id: int, limit=10) -> List[Dict]:
    conn = await get_connection()
    rows = await conn.fetch('''
        SELECT started_at, total_recipients, sent_success, sent_error, total_time_seconds, vk_account_name
        FROM mailings WHERE user_id = $1 ORDER BY started_at DESC LIMIT $2
    ''', user_id, limit)
    await conn.close()
    return [dict(r) for r in rows]

# ----- Templates -----
async def save_template(user_id: int, name: str, content: str, delay: float = 3.0):
    conn = await get_connection()
    await conn.execute('INSERT INTO templates (user_id, name, content, delay) VALUES ($1, $2, $3, $4)', user_id, name, content, delay)
    await conn.close()

async def get_templates(user_id: int) -> List[Dict]:
    conn = await get_connection()
    rows = await conn.fetch('SELECT id, name, content, delay FROM templates WHERE user_id = $1 ORDER BY created_at DESC', user_id)
    await conn.close()
    return [dict(r) for r in rows]

async def delete_template(template_id: int, user_id: int):
    conn = await get_connection()
    await conn.execute('DELETE FROM templates WHERE id = $1 AND user_id = $2', template_id, user_id)
    await conn.close()

async def get_template_by_id(template_id: int, user_id: int) -> Optional[Dict]:
    conn = await get_connection()
    row = await conn.fetchrow('SELECT id, name, content, delay FROM templates WHERE id = $1 AND user_id = $2', template_id, user_id)
    await conn.close()
    return dict(row) if row else None

# ----- Invoices -----
async def create_invoice_db(invoice_id: str, user_id: int, amount: float, currency: str, days: int):
    conn = await get_connection()
    await conn.execute('INSERT INTO invoices (invoice_id, user_id, amount, currency, days) VALUES ($1, $2, $3, $4, $5)', invoice_id, user_id, amount, currency, days)
    await conn.close()

async def get_pending_invoice(user_id: int) -> Optional[Dict]:
    conn = await get_connection()
    row = await conn.fetchrow('SELECT invoice_id, days FROM invoices WHERE user_id = $1 AND status = $2 ORDER BY created_at DESC LIMIT 1', user_id, 'pending')
    await conn.close()
    return dict(row) if row else None

async def mark_invoice_paid(invoice_id: str, days: int):
    conn = await get_connection()
    await conn.execute('UPDATE invoices SET status = $1, completed_at = $2 WHERE invoice_id = $3', 'paid', datetime.now(), invoice_id)
    await conn.close()

# ----- Множественные токены -----
async def add_vk_token(user_id: int, token: str, name: str = None):
    conn = await get_connection()
    await conn.execute('''
        INSERT INTO user_vk_tokens (user_id, token, name, is_active) 
        VALUES ($1, $2, COALESCE($3, 'Аккаунт VK'), FALSE)
    ''', user_id, token, name)
    await conn.close()

async def add_multiple_vk_tokens(user_id: int, tokens: List[tuple]):
    if not tokens:
        return
    conn = await get_connection()
    for token, name in tokens:
        await conn.execute('''
            INSERT INTO user_vk_tokens (user_id, token, name, is_active) 
            VALUES ($1, $2, $3, FALSE)
        ''', user_id, token, name)
    await conn.close()

async def get_user_tokens(user_id: int) -> List[Dict]:
    conn = await get_connection()
    rows = await conn.fetch('''
        SELECT id, token, name, is_active, created_at, stats 
        FROM user_vk_tokens WHERE user_id = $1 ORDER BY created_at
    ''', user_id)
    await conn.close()
    return [dict(r) for r in rows]

async def set_active_token(user_id: int, token_id: int):
    conn = await get_connection()
    await conn.execute('UPDATE user_vk_tokens SET is_active = FALSE WHERE user_id = $1', user_id)
    await conn.execute('UPDATE user_vk_tokens SET is_active = TRUE WHERE id = $2 AND user_id = $1', user_id, token_id)
    await conn.close()

async def delete_token(user_id: int, token_id: int):
    conn = await get_connection()
    await conn.execute('DELETE FROM user_vk_tokens WHERE id = $1 AND user_id = $2', token_id, user_id)
    await conn.close()

async def update_token_stats(user_id: int, token_id: int, stats: dict):
    conn = await get_connection()
    await conn.execute('UPDATE user_vk_tokens SET stats = $1, last_checked = $2 WHERE id = $3 AND user_id = $4',
                       stats, datetime.now(), token_id, user_id)
    await conn.close()

async def get_active_token(user_id: int) -> Optional[Dict]:
    conn = await get_connection()
    row = await conn.fetchrow('SELECT id, token, name, stats FROM user_vk_tokens WHERE user_id = $1 AND is_active = TRUE', user_id)
    await conn.close()
    return dict(row) if row else None

async def validate_and_clean_tokens(user_id: int, check_func) -> Tuple[int, int]:
    """Проверяет все токены пользователя, удаляет невалидные. check_func - асинхронная функция проверки токена"""
    tokens = await get_user_tokens(user_id)
    deleted = 0
    for t in tokens:
        try:
            await check_func(t['token'])
        except Exception:
            await delete_token(user_id, t['id'])
            deleted += 1
    remaining = len(tokens) - deleted
    return deleted, remaining