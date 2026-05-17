import os
import re
import json
import io
import time
import random
import asyncio
import logging
import sqlite3
import requests
import base64
import ssl
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram import F
from vk_api import VkApi
from vk_api.exceptions import ApiError

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip().lstrip('@')) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip('@').isdigit()]
ANTICAPTCHA_KEY = os.getenv("ANTICAPTCHA_KEY", "")
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api/"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========================== БАЗА ДАННЫХ ==========================
DB_PATH = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        subscription_until TIMESTAMP,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS vk_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        token TEXT NOT NULL,
        name TEXT,
        is_active INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_checked TIMESTAMP,
        stats TEXT DEFAULT '{}'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        content TEXT,
        delay REAL DEFAULT 3.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS invoices (
        invoice_id TEXT PRIMARY KEY,
        user_id INTEGER,
        days INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def add_user(telegram_id, username=None, first_name=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)',
              (telegram_id, username, first_name))
    c.execute('UPDATE users SET username=?, first_name=? WHERE telegram_id=?',
              (username, first_name, telegram_id))
    conn.commit()
    conn.close()

def get_user_subscription(telegram_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT subscription_until FROM users WHERE telegram_id=?', (telegram_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return datetime.fromisoformat(row[0])
    return None

def set_subscription(telegram_id, days):
    until = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE users SET subscription_until=? WHERE telegram_id=?', (until.isoformat(), telegram_id))
    conn.commit()
    conn.close()

# ---------- Токены ----------
def add_vk_token(user_id, token, name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO vk_tokens (user_id, token, name, is_active, last_checked) VALUES (?, ?, ?, 0, ?)',
              (user_id, token, name, datetime.now().isoformat()))
    token_id = c.lastrowid
    c.execute('UPDATE vk_tokens SET stats = ? WHERE id=?', ('{}', token_id))
    conn.commit()
    conn.close()
    return token_id

def update_token_stats(token_id, sent=0, errors=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT stats FROM vk_tokens WHERE id=?', (token_id,))
    row = c.fetchone()
    if row:
        stats = json.loads(row[0]) if row[0] else {}
        stats['sent'] = stats.get('sent', 0) + sent
        stats['errors'] = stats.get('errors', 0) + errors
        stats['last_used'] = datetime.now().isoformat()
        c.execute('UPDATE vk_tokens SET stats=? WHERE id=?', (json.dumps(stats), token_id))
    conn.commit()
    conn.close()

def get_token_stats(token_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT stats FROM vk_tokens WHERE id=?', (token_id,))
    row = c.fetchone()
    conn.close()
    return json.loads(row[0]) if row and row[0] else {}

def get_all_tokens(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, token, name, is_active FROM vk_tokens WHERE user_id=?', (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def set_active_token(user_id, token_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE vk_tokens SET is_active=0 WHERE user_id=?', (user_id,))
    c.execute('UPDATE vk_tokens SET is_active=1 WHERE id=? AND user_id=?', (token_id, user_id))
    conn.commit()
    conn.close()

def delete_token(user_id, token_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM vk_tokens WHERE id=? AND user_id=?', (token_id, user_id))
    conn.commit()
    conn.close()

def get_active_token(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, token, name FROM vk_tokens WHERE user_id=? AND is_active=1', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_user_stats(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, stats, is_active FROM vk_tokens WHERE user_id=?', (user_id,))
    rows = c.fetchall()
    conn.close()
    result = []
    for token_id, name, stats_json, is_active in rows:
        stats = json.loads(stats_json) if stats_json else {}
        result.append({
            'id': token_id,
            'name': name,
            'sent': stats.get('sent', 0),
            'errors': stats.get('errors', 0),
            'last_used': stats.get('last_used', 'никогда'),
            'is_active': is_active
        })
    return result

# ---------- Шаблоны ----------
def save_template(user_id, name, content, delay):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO templates (user_id, name, content, delay) VALUES (?, ?, ?, ?)',
              (user_id, name, content, delay))
    conn.commit()
    conn.close()

def get_templates(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, content, delay FROM templates WHERE user_id=? ORDER BY created_at DESC', (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_template(template_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM templates WHERE id=? AND user_id=?', (template_id, user_id))
    conn.commit()
    conn.close()

def export_templates_json(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT name, content, delay FROM templates WHERE user_id=?', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{'name': r[0], 'content': r[1], 'delay': r[2]} for r in rows]

def import_templates_json(user_id, data):
    for item in data:
        save_template(user_id, item['name'], item['content'], item['delay'])

# ---------- Оплата ----------
def create_invoice(invoice_id, user_id, days):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO invoices (invoice_id, user_id, days) VALUES (?, ?, ?)', (invoice_id, user_id, days))
    conn.commit()
    conn.close()

def get_pending_invoice(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT invoice_id, days FROM invoices WHERE user_id=? AND status="pending" ORDER BY created_at DESC LIMIT 1', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def mark_invoice_paid(invoice_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE invoices SET status="paid" WHERE invoice_id=?', (invoice_id,))
    conn.commit()
    conn.close()

# ========================== VK РАБОТА (С ПОДДЕРЖКОЙ КАПЧИ) ==========================
class SSLDisabledHTTPAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = ssl._create_unverified_context()
        return super().init_poolmanager(*args, **kwargs)

def create_vk_session(token):
    session = requests.Session()
    session.mount('https://', SSLDisabledHTTPAdapter())
    return VkApi(token=token, api_version='5.131', session=session)

def solve_captcha(captcha_url):
    if not ANTICAPTCHA_KEY:
        raise RuntimeError("ANTICAPTCHA_KEY не задан")
    submit_url = "http://2captcha.com/in.php"
    result_url = "http://2captcha.com/res.php"
    with requests.Session() as session:
        img_data = requests.get(captcha_url).content
        img_b64 = base64.b64encode(img_data).decode('utf-8')
        resp = session.post(submit_url, data={
            "key": ANTICAPTCHA_KEY,
            "method": "base64",
            "body": img_b64,
            "json": 1
        })
        if resp.status_code != 200 or resp.json().get("status") != 1:
            raise RuntimeError(f"Ошибка отправки капчи: {resp.text}")
        captcha_id = resp.json()["request"]
        for _ in range(15):
            time.sleep(2)
            resp = session.get(result_url, params={
                "key": ANTICAPTCHA_KEY,
                "action": "get",
                "id": captcha_id,
                "json": 1
            })
            if resp.status_code == 200 and resp.json().get("status") == 1:
                return resp.json()["request"]
        raise RuntimeError("Время ожидания капчи истекло.")

def validate_vk_token(token):
    vk_session = create_vk_session(token)
    vk = vk_session.get_api()
    try:
        info = vk.account.getProfileInfo()
        try:
            phone = vk.account.getPhone().get('phone', 'не указан')
        except:
            phone = 'нет доступа'
        # Дополнительная проверка прав на чтение диалогов
        vk.messages.getConversations(count=1)
        return True, info['first_name'], info['last_name'], phone, info['id']
    except ApiError as e:
        if e.code == 5:
            return False, "Токен невалиден (ошибка авторизации)", None, None, None
        elif e.code == 30:
            return False, "Аккаунт удалён или заблокирован", None, None, None
        elif e.code == 200:
            return False, "Нет прав на отправку сообщений (нужен scope messages)", None, None, None
        elif e.code == 14:
            return False, "Требуется капча", None, None, None
        else:
            return False, f"Ошибка VK {e.code}: {e.error.get('error_msg', 'Неизвестная ошибка')}", None, None, None
    except Exception as e:
        return False, str(e), None, None, None

def get_friends(token):
    """Получает только личные диалоги (друзей) с правом отправки"""
    vk_session = create_vk_session(token)
    vk = vk_session.get_api()
    try:
        convs = vk.messages.getConversations(count=200)
        dialogs = []
        for item in convs['items']:
            peer = item['conversation']['peer']
            if peer['type'] != 'user':
                continue
            can_write = item['conversation'].get('can_write', {}).get('allowed', False)
            if not can_write:
                continue
            dialogs.append({
                'peer_id': peer['id'],
                'name': f"User {peer['id']}"
            })
        return dialogs
    except ApiError as e:
        if e.code == 14:
            captcha_key = solve_captcha(e.captcha_img)
            # Повторный запрос с капчей (упрощённо)
            raise Exception("Капча была решена, повторите запрос")
        else:
            raise e

def send_vk_message(token, peer_id, text):
    vk_session = create_vk_session(token)
    vk = vk_session.get_api()
    text = re.sub(r'(https?://)', r'\1 ', text)
    random_id = random.randint(1, 2**31-1)
    while True:
        try:
            vk.messages.send(peer_id=peer_id, message=text, random_id=random_id)
            return True, None
        except ApiError as e:
            if e.code == 14:
                captcha_key = solve_captcha(e.captcha_img)
                try:
                    vk.messages.send(peer_id=peer_id, message=text, random_id=random_id,
                                     captcha_sid=e.captcha_sid, captcha_key=captcha_key)
                    return True, None
                except ApiError as e2:
                    return False, f"Капча не решена: {e2}"
            elif e.code in (901, 913, 917, 902):
                return False, f"Ошибка {e.code}"
            else:
                return False, str(e)
        except Exception as e:
            return False, str(e)

# ========================== CRYPTOBOT ==========================
async def create_crypto_invoice(user_id, days, amount):
    if not CRYPTOBOT_TOKEN:
        return None
    description = f"Subscription {days}d {amount} USDT"
    params = {"amount": amount, "asset": "USDT", "description": description, "user_id": user_id}
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        resp = requests.post(CRYPTOBOT_API_URL + "createInvoice", json=params, headers=headers)
        data = resp.json()
        if data.get("ok"):
            invoice_id = str(data["result"]["invoice_id"])
            create_invoice(invoice_id, user_id, days)
            return data["result"]["pay_url"]
    except:
        return None

async def check_payment(user_id):
    inv = get_pending_invoice(user_id)
    if not inv:
        return False
    invoice_id, days = inv
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        resp = requests.post(CRYPTOBOT_API_URL + "getInvoices", json={"invoice_ids": invoice_id}, headers=headers)
        data = resp.json()
        if data.get("ok") and data["result"]["items"] and data["result"]["items"][0]["status"] == "paid":
            mark_invoice_paid(invoice_id)
            set_subscription(user_id, days)
            return True
    except:
        pass
    return False

# ========================== FSM ==========================
class BotStates(StatesGroup):
    waiting_token = State()
    waiting_mass_tokens = State()
    waiting_phone = State()
    waiting_password = State()
    waiting_newsletter_text = State()
    waiting_delay = State()
    waiting_template_name = State()
    waiting_template_content = State()
    waiting_template_delay = State()
    waiting_import = State()
    admin_broadcast = State()
    admin_user_id = State()
    admin_days = State()

# ========================== ПРОВЕРКА ПОДПИСКИ ==========================
async def has_subscription(user_id):
    if user_id in ADMIN_IDS:
        return True
    sub = get_user_subscription(user_id)
    return sub and sub > datetime.now()

# ========================== КЛАВИАТУРЫ ==========================
def main_menu(uid):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=" Рассылка друзьям", callback_data="start_mailing", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text=" Добавить токен", callback_data="add_token", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text=" Массовое добавление", callback_data="mass_add_tokens", icon_custom_emoji_id="5472096095280572227", style="default")],
        [InlineKeyboardButton(text=" Войти по номеру", callback_data="phone_login", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text=" Мои шаблоны", callback_data="my_templates", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text=" Статистика аккаунтов", callback_data="account_stats", icon_custom_emoji_id="5278753302023004775", style="primary")],
        [InlineKeyboardButton(text=" Мой профиль", callback_data="my_profile", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text=" Подписка", callback_data="buy_sub", icon_custom_emoji_id="5195058841988914267", style="success")],
    ])
    if uid in ADMIN_IDS:
        kb.inline_keyboard.append([InlineKeyboardButton(text="👑 Админ", callback_data="admin_panel", style="danger")])
    return kb

def back_button(callback_data="back_to_main"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data, style="default")]])

# ========================== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    add_user(uid, message.from_user.username, message.from_user.first_name)
    welcome = (
        f"<tg-emoji emoji-id='5278611606756942667'></tg-emoji> <b>VK Рассыльщик</b>\n\n"
        f"Отправляй сообщения только друзьям.\n"
        f"💰 Купи подписку для доступа к функциям."
    )
    await message.answer(welcome, parse_mode="HTML", reply_markup=main_menu(uid))

# ---- Добавление одного токена ----
@dp.callback_query(lambda c: c.data == "add_token")
async def add_token_prompt(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Отправьте <b>токен VK</b> (scope: messages).\nПолучить можно: https://vkhost.github.io",
        parse_mode="HTML", reply_markup=None)
    await state.set_state(BotStates.waiting_token)

@dp.message(BotStates.waiting_token)
async def process_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if not token:
        await message.answer("❌ Токен не может быть пустым.")
        return
    msg = await message.answer("🔄 Проверка токена...")
    valid, first_name, last_name, phone, vk_id = validate_vk_token(token)
    if not valid:
        await msg.delete()
        await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка: {first_name}", parse_mode="HTML")
        return
    name = f"{first_name} {last_name}"
    add_vk_token(message.from_user.id, token, name)
    await msg.delete()
    await message.answer(
        f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Аккаунт добавлен\n"
        f"👤 {name}\n🤙 {phone}\n🆔 {vk_id}\n\n✅ Токен валиден",
        parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ---- Массовое добавление токенов ----
@dp.callback_query(lambda c: c.data == "mass_add_tokens")
async def mass_add_prompt(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "📦 Отправьте список токенов в формате:\n\n`токен1 | Название1`\n`токен2 | Название2`\n\nПример:\nvk1... | Мой основной\nvk2...\n\nТокены будут проверены и добавлены.",
        parse_mode="Markdown", reply_markup=None)
    await state.set_state(BotStates.waiting_mass_tokens)

@dp.message(BotStates.waiting_mass_tokens)
async def process_mass_tokens(message: Message, state: FSMContext):
    lines = message.text.strip().split('\n')
    added = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split('|')
        token = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else None
        valid, fn, ln, phone, vid = validate_vk_token(token)
        if not valid:
            await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка для {token[:10]}...: {fn}", parse_mode="HTML")
            continue
        if not name:
            name = f"{fn} {ln}"
        add_vk_token(message.from_user.id, token, name)
        added += 1
    await message.answer(f"✅ Добавлено {added} аккаунтов.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ---- Вход по номеру телефона ----
@dp.callback_query(lambda c: c.data == "phone_login")
async def phone_login_start(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("📱 Введите номер телефона в формате +71234567890:", reply_markup=None)
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def phone_login_get_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith('+'):
        await message.answer("❌ Номер должен начинаться с +")
        return
    await state.update_data(login=phone)
    await message.answer("🔑 Введите пароль от аккаунта VK:", reply_markup=None)
    await state.set_state(BotStates.waiting_password)

@dp.message(BotStates.waiting_password)
async def phone_login_get_password(message: Message, state: FSMContext):
    password = message.text
    data = await state.get_data()
    login = data.get('login')
    await message.answer("🔄 Авторизация...")
    try:
        vk_session = VkApi(login=login, password=password, api_version='5.131')
        vk_session.auth(token_only=True)
        token = vk_session.token['access_token']
        valid, fn, ln, phone, vid = validate_vk_token(token)
        if not valid:
            await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка: {fn}", parse_mode="HTML")
            await state.clear()
            return
        name = f"{fn} {ln}"
        add_vk_token(message.from_user.id, token, name)
        await message.answer(
            f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Аккаунт добавлен через номер телефона\n"
            f"👤 {name}\n🤙 {phone}\n🆔 {vid}",
            parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
        await state.clear()
    except ApiError as e:
        await message.answer(f"❌ Ошибка VK: {e}")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()

# ---- Статистика аккаунтов ----
@dp.callback_query(lambda c: c.data == "account_stats")
async def show_account_stats(callback: CallbackQuery):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    stats = get_all_user_stats(callback.from_user.id)
    if not stats:
        await callback.message.edit_text("📭 Нет добавленных аккаунтов.", reply_markup=back_button())
        return
    text = "<tg-emoji emoji-id='5278753302023004775'></tg-emoji> <b>Статистика аккаунтов VK</b>\n\n"
    for s in stats:
        status = "✅" if s['is_active'] else "⚪"
        text += f"{status} <b>{s['name']}</b>\n   ✅ Отправлено: {s['sent']}\n   ❌ Ошибок: {s['errors']}\n   🕒 Последний раз: {s['last_used']}\n\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())

# ---- Шаблоны с экспортом/импортом ----
@dp.callback_query(lambda c: c.data == "my_templates")
async def templates_menu(callback: CallbackQuery):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    uid = callback.from_user.id
    tpls = get_templates(uid)
    if not tpls:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать", callback_data="create_template", style="primary")],
            [InlineKeyboardButton(text="📥 Импорт", callback_data="import_templates", style="default")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")]
        ])
        await callback.message.edit_text("📭 Нет шаблонов", reply_markup=kb)
        return
    text = "📋 <b>Ваши шаблоны</b>\n\n"
    for t in tpls:
        text += f"🔹 <b>{t[1]}</b> — <code>{t[2][:40]}...</code> (⏱{t[3]}с)\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="create_template", style="success")],
        [InlineKeyboardButton(text="📤 Экспорт", callback_data="export_templates", style="default")],
        [InlineKeyboardButton(text="📥 Импорт", callback_data="import_templates", style="default")],
        [InlineKeyboardButton(text="❌ Удалить", callback_data="delete_template", style="danger")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "create_template")
async def create_template(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📝 Введите название шаблона:", reply_markup=None)
    await state.set_state(BotStates.waiting_template_name)

@dp.message(BotStates.waiting_template_name)
async def get_tpl_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Название не может быть пустым.")
        return
    await state.update_data(tpl_name=name)
    await message.answer("✏️ Введите текст шаблона (можно HTML):", reply_markup=None)
    await state.set_state(BotStates.waiting_template_content)

@dp.message(BotStates.waiting_template_content)
async def get_tpl_content(message: Message, state: FSMContext):
    content = message.text
    await state.update_data(tpl_content=content)
    await message.answer("⏱️ Введите задержку (сек):", reply_markup=None)
    await state.set_state(BotStates.waiting_template_delay)

@dp.message(BotStates.waiting_template_delay)
async def get_tpl_delay(message: Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    name = data.get('tpl_name')
    content = data.get('tpl_content')
    save_template(message.from_user.id, name, content, delay)
    await message.answer(f"✅ Шаблон <b>{name}</b> сохранён!", parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(lambda c: c.data == "export_templates")
async def export_templates_handler(callback: CallbackQuery):
    uid = callback.from_user.id
    data = export_templates_json(uid)
    if not data:
        await callback.answer("Нет шаблонов для экспорта", show_alert=True)
        return
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    file = io.BytesIO(json_str.encode('utf-8'))
    await callback.message.answer_document(document=('templates.json', file), caption="📁 Ваши шаблоны")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "import_templates")
async def import_templates_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📤 Отправьте JSON-файл с шаблонами (экспортированный ранее):", reply_markup=None)
    await state.set_state(BotStates.waiting_import)

@dp.message(StateFilter(BotStates.waiting_import), F.document)
async def process_import_templates(message: Message, state: FSMContext):
    if not message.document:
        await message.answer("❌ Пожалуйста, отправьте файл JSON.")
        return
    file = await bot.get_file(message.document.file_id)
    file_bytes = await bot.download_file(file.file_path)
    try:
        data = json.loads(file_bytes.read().decode('utf-8'))
        if not isinstance(data, list):
            raise ValueError
        import_templates_json(message.from_user.id, data)
        await message.answer(f"✅ Импортировано {len(data)} шаблонов.", reply_markup=main_menu(message.from_user.id))
    except:
        await message.answer("❌ Неверный формат файла. Загрузите корректный JSON.")
    await state.clear()

@dp.callback_query(lambda c: c.data == "delete_template")
async def delete_template_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    tpls = get_templates(uid)
    if not tpls:
        await callback.answer("Нет шаблонов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in tpls:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"❌ {t[1]}", callback_data=f"del_tpl_{t[0]}", style="danger")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="my_templates", style="default")])
    await callback.message.edit_text("🗑️ Выберите шаблон для удаления:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("del_tpl_"))
async def confirm_delete_template(callback: CallbackQuery):
    tpl_id = int(callback.data.split("_")[2])
    delete_template(tpl_id, callback.from_user.id)
    await callback.answer("Шаблон удалён", show_alert=True)
    await templates_menu(callback)

# ---- Подписка ----
@dp.callback_query(lambda c: c.data == "buy_sub")
async def buy_sub_menu(callback: CallbackQuery):
    await callback.answer()
    if not CRYPTOBOT_TOKEN:
        await callback.message.edit_text("⚠️ CryptoBot недоступен", reply_markup=back_button())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📆 1 день - 2$", callback_data="sub_1_2", icon_custom_emoji_id="5195058841988914267", style="primary")],
        [InlineKeyboardButton(text="📆 7 дней - 5$", callback_data="sub_7_5", style="primary")],
        [InlineKeyboardButton(text="📆 30 дней - 15$", callback_data="sub_30_15", style="primary")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")]
    ])
    await callback.message.edit_text("💰 Выберите срок подписки:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("sub_"))
async def process_sub(callback: CallbackQuery):
    _, days_str, price_str = callback.data.split("_")
    days = int(days_str)
    price = float(price_str)
    pay_url = await create_crypto_invoice(callback.from_user.id, days, price)
    if not pay_url:
        await callback.answer("Ошибка", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить", url=pay_url, style="success")],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="check_payment", style="primary")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="buy_sub", style="default")]
    ])
    await callback.message.edit_text(f"💳 Счёт на {days} дней, {price}$\nПосле оплаты нажмите «Проверить оплату»", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "check_payment")
async def check_pay(callback: CallbackQuery):
    paid = await check_payment(callback.from_user.id)
    if paid:
        await callback.answer("✅ Подписка активирована!", show_alert=True)
        await callback.message.edit_text("✅ Подписка активирована!", reply_markup=back_button())
    else:
        await callback.answer("⏳ Оплата не найдена", show_alert=True)

# ---- РАССЫЛКА ТОЛЬКО ДРУЗЬЯМ (с ротацией аккаунтов) ----
@dp.callback_query(lambda c: c.data == "start_mailing")
async def start_mailing(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await has_subscription(callback.from_user.id):
        await callback.message.edit_text("❌ Нет подписки", reply_markup=back_button())
        return
    tokens = get_all_tokens(callback.from_user.id)
    if not tokens:
        await callback.message.edit_text("❌ Нет добавленных аккаунтов. Добавьте токен или войдите по номеру.", reply_markup=back_button())
        return
    await state.update_data(tokens=tokens)
    await callback.message.edit_text("✏️ Введите текст сообщения (можно HTML):", reply_markup=None)
    await state.set_state(BotStates.waiting_newsletter_text)

@dp.message(BotStates.waiting_newsletter_text)
async def get_newsletter_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("⏱ Введите задержку в секундах (например, 2):", reply_markup=None)
    await state.set_state(BotStates.waiting_delay)

@dp.message(BotStates.waiting_delay)
async def get_newsletter_delay(message: Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите число > 0")
        return
    data = await state.get_data()
    text = data.get('text')
    tokens = data.get('tokens')
    await state.clear()
    # Определяем активный токен для получения списка друзей (используем первый активный)
    active_token = None
    for t in tokens:
        if t[3] == 1:
            active_token = t
            break
    if not active_token:
        active_token = tokens[0]
    token_id, token, token_name = active_token
    # Проверяем валидность токена
    valid, fn, ln, phone, vid = validate_vk_token(token)
    if not valid:
        await message.answer(f"❌ Аккаунт {token_name} невалиден: {fn}\nПожалуйста, добавьте новый или активируйте другой.", reply_markup=main_menu(message.from_user.id))
        return
    # Загружаем друзей
    try:
        friends = get_friends(token)
    except Exception as e:
        await message.answer(f"❌ Ошибка загрузки друзей: {e}", reply_markup=main_menu(message.from_user.id))
        return
    if not friends:
        await message.answer("⚠️ Нет друзей, которым можно отправить сообщение.", reply_markup=main_menu(message.from_user.id))
        return
    total = len(friends)
    sent = 0
    errors = 0
    progress_msg = await message.answer(f"🚀 Начинаю рассылку {total} друзьям с задержкой {delay} сек...")
    token_list = tokens  # (id, token, name, is_active)
    token_idx = 0
    for idx, friend in enumerate(friends, 1):
        tid, ttoken, tname, _ = token_list[token_idx % len(token_list)]
        success, err = send_vk_message(ttoken, friend['peer_id'], text)
        if success:
            sent += 1
            update_token_stats(tid, sent=1)
        else:
            errors += 1
            update_token_stats(tid, errors=1)
        if idx % 5 == 0 or idx == total:
            await progress_msg.edit_text(f"📊 Прогресс: {idx}/{total} | ✅{sent} ❌{errors}")
        token_idx += 1
        await asyncio.sleep(delay)
    await message.answer(
        f"<tg-emoji emoji-id='5206401524200145033'></tg-emoji> <b>Рассылка друзьям завершена!</b>\n"
        f"📝 Отправлено: {sent}/{total}\n"
        f"   ┣ ✅ Успешно: {sent}\n"
        f"   ┗ ❌ Ошибки: {errors}\n"
        f"⏲️ Время: {delay*total:.1f} сек.",
        parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await progress_msg.delete()

# ---- Профиль ----
@dp.callback_query(lambda c: c.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    uid = callback.from_user.id
    sub = get_user_subscription(uid)
    sub_text = sub.strftime('%d.%m.%Y %H:%M') if sub else "Нет"
    if uid in ADMIN_IDS:
        sub_text = "🔹 Вечная"
    text = (f"<tg-emoji emoji-id='5275979556308674886'></tg-emoji> <b>Ваш профиль</b>\n\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"📛 Имя: {callback.from_user.first_name}\n"
            f"⏳ Подписка до: {sub_text}")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())

# ---- Админ-панель ----
@dp.callback_query(lambda c: c.data == "admin_panel" and c.from_user.id in ADMIN_IDS)
async def admin_panel(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton(text="📨 Рассылка пользователям", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="admin_give_sub", style="success")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="danger")]
    ])
    await callback.message.edit_text("👑 Админ-панель", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "admin_stats" and c.from_user.id in ADMIN_IDS)
async def admin_stats(callback: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    users = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM vk_tokens')
    tokens = c.fetchone()[0]
    conn.close()
    await callback.message.edit_text(f"📊 Статистика бота\n👥 Пользователей: {users}\n🔑 Токенов: {tokens}", reply_markup=back_button("admin_panel"))

@dp.callback_query(lambda c: c.data == "admin_broadcast" and c.from_user.id in ADMIN_IDS)
async def admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✏️ Введите текст для рассылки всем пользователям:", reply_markup=None)
    await state.set_state(BotStates.admin_broadcast)

@dp.message(BotStates.admin_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    text = message.text
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT telegram_id FROM users')
    users = [row[0] for row in c.fetchall()]
    conn.close()
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 Анонс от администратора\n\n{text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Отправлено {sent} пользователям", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_give_sub" and c.from_user.id in ADMIN_IDS)
async def admin_give_sub_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔢 Введите Telegram ID пользователя:", reply_markup=None)
    await state.set_state(BotStates.admin_user_id)

@dp.message(BotStates.admin_user_id)
async def admin_get_user_id(message: Message, state: FSMContext):
    try:
        target = int(message.text.strip())
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    await state.update_data(target=target)
    await message.answer("📆 Введите количество дней:", reply_markup=None)
    await state.set_state(BotStates.admin_days)

@dp.message(BotStates.admin_days)
async def admin_give_days(message: Message, state: FSMContext):
    try:
        days = float(message.text.replace(",", "."))
        if days <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    target = data.get('target')
    set_subscription(target, days)
    await message.answer(f"✅ Пользователю {target} выдана подписка на {days} дн.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ---- Возврат в главное меню ----
@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu(callback.from_user.id))

# ========================== ЗАПУСК ==========================
async def on_startup():
    init_db()
    logger.info("✅ Бот запущен")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())