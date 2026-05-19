import asyncpg
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import json

DATABASE_URL = os.getenv("DATABASE_URL")

async def get_connection():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await get_connection()
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            subscription_until TIMESTAMP,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS vk_tokens (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
            token TEXT NOT NULL,
            name TEXT,
            is_active BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_checked TIMESTAMP,
            stats JSONB DEFAULT '{}'
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
            name TEXT,
            content TEXT,
            delay REAL DEFAULT 3.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id TEXT PRIMARY KEY,
            user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
            days INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Добавляем колонку created_at в templates, если её нет (миграция)
    await conn.execute('''
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                           WHERE table_name='templates' AND column_name='created_at') THEN
                ALTER TABLE templates ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
            END IF;
        END $$;
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

# ----- Tokens -----
async def add_vk_token(user_id: int, token: str, name: str):
    conn = await get_connection()
    await conn.execute('''
        INSERT INTO vk_tokens (user_id, token, name, is_active, last_checked) 
        VALUES ($1, $2, $3, FALSE, $4)
    ''', user_id, token, name, datetime.now())
    token_id = await conn.fetchval('SELECT currval(pg_get_serial_sequence(\'vk_tokens\', \'id\'))')
    await conn.close()
    return token_id

async def update_token_stats(token_id: int, sent: int = 0, errors: int = 0):
    conn = await get_connection()
    row = await conn.fetchrow('SELECT stats FROM vk_tokens WHERE id = $1', token_id)
    if row:
        stats = row['stats'] or {}
        stats['sent'] = stats.get('sent', 0) + sent
        stats['errors'] = stats.get('errors', 0) + errors
        stats['last_used'] = datetime.now().isoformat()
        await conn.execute('UPDATE vk_tokens SET stats = $1 WHERE id = $2', json.dumps(stats), token_id)
    await conn.close()

async def get_all_tokens(user_id: int) -> List[Tuple[int, str, str, bool]]:
    conn = await get_connection()
    rows = await conn.fetch('SELECT id, token, name, is_active FROM vk_tokens WHERE user_id = $1', user_id)
    await conn.close()
    return [(r['id'], r['token'], r['name'], r['is_active']) for r in rows]

async def set_active_token(user_id: int, token_id: int):
    conn = await get_connection()
    await conn.execute('UPDATE vk_tokens SET is_active = FALSE WHERE user_id = $1', user_id)
    await conn.execute('UPDATE vk_tokens SET is_active = TRUE WHERE id = $2 AND user_id = $1', token_id, user_id)
    await conn.close()

async def delete_token(user_id: int, token_id: int):
    conn = await get_connection()
    await conn.execute('DELETE FROM vk_tokens WHERE id = $1 AND user_id = $2', token_id, user_id)
    await conn.close()

async def get_active_token(user_id: int) -> Optional[Tuple[int, str, str]]:
    """Возвращает (id, token, name) активного токена или None"""
    conn = await get_connection()
    row = await conn.fetchrow('SELECT id, token, name FROM vk_tokens WHERE user_id = $1 AND is_active = TRUE', user_id)
    await conn.close()
    if row:
        return (row['id'], row['token'], row['name'])
    return None

async def get_all_user_stats(user_id: int) -> List[Dict]:
    conn = await get_connection()
    rows = await conn.fetch('SELECT id, name, stats, is_active FROM vk_tokens WHERE user_id = $1', user_id)
    await conn.close()
    result = []
    for r in rows:
        stats = r['stats'] or {}
        result.append({
            'id': r['id'],
            'name': r['name'],
            'sent': stats.get('sent', 0),
            'errors': stats.get('errors', 0),
            'last_used': stats.get('last_used', 'никогда'),
            'is_active': r['is_active']
        })
    return result

# ----- Templates -----
async def save_template(user_id: int, name: str, content: str, delay: float):
    conn = await get_connection()
    await conn.execute('''
        INSERT INTO templates (user_id, name, content, delay) VALUES ($1, $2, $3, $4)
    ''', user_id, name, content, delay)
    await conn.close()

async def get_templates(user_id: int) -> List[Tuple[int, str, str, float]]:
    conn = await get_connection()
    rows = await conn.fetch('SELECT id, name, content, delay FROM templates WHERE user_id = $1 ORDER BY created_at DESC', user_id)
    await conn.close()
    return [(r['id'], r['name'], r['content'], r['delay']) for r in rows]

async def delete_template(template_id: int, user_id: int):
    conn = await get_connection()
    await conn.execute('DELETE FROM templates WHERE id = $1 AND user_id = $2', template_id, user_id)
    await conn.close()

async def export_templates_json(user_id: int) -> List[Dict]:
    conn = await get_connection()
    rows = await conn.fetch('SELECT name, content, delay FROM templates WHERE user_id = $1', user_id)
    await conn.close()
    return [{'name': r['name'], 'content': r['content'], 'delay': r['delay']} for r in rows]

async def import_templates_json(user_id: int, data: List[Dict]):
    for item in data:
        await save_template(user_id, item['name'], item['content'], item['delay'])

# ----- Invoices -----
async def create_invoice(invoice_id: str, user_id: int, days: int):
    conn = await get_connection()
    await conn.execute('INSERT INTO invoices (invoice_id, user_id, days) VALUES ($1, $2, $3)', invoice_id, user_id, days)
    await conn.close()

async def get_pending_invoice(user_id: int) -> Optional[Tuple[str, int]]:
    conn = await get_connection()
    row = await conn.fetchrow('SELECT invoice_id, days FROM invoices WHERE user_id = $1 AND status = \'pending\' ORDER BY created_at DESC LIMIT 1', user_id)
    await conn.close()
    return (row['invoice_id'], row['days']) if row else None

async def mark_invoice_paid(invoice_id: str):
    conn = await get_connection()
    await conn.execute('UPDATE invoices SET status = \'paid\' WHERE invoice_id = $1', invoice_id)
    await conn.close()

# ----- Admin helpers -----
async def get_all_users() -> List[int]:
    conn = await get_connection()
    rows = await conn.fetch('SELECT telegram_id FROM users')
    await conn.close()
    return [r['telegram_id'] for r in rows]

async def get_bot_stats() -> Dict:
    conn = await get_connection()
    total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
    total_tokens = await conn.fetchval('SELECT COUNT(*) FROM vk_tokens')
    await conn.close()
    return {'users': total_users, 'tokens': total_tokens}