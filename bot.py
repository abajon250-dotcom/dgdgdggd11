import os
import re
import json
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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery, FSInputFile
from vk_api import VkApi
from vk_api.exceptions import ApiError

# ==================== КОНФИГ ====================
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

# ==================== БАЗА ДАННЫХ ====================
DB_PATH = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Пользователи
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        subscription_until TIMESTAMP,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Аккаунты VK (несколько на одного юзера)
    c.execute('''CREATE TABLE IF NOT EXISTS vk_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        token TEXT NOT NULL,
        name TEXT,
        is_active INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_used TIMESTAMP,
        total_sent INTEGER DEFAULT 0,
        total_errors INTEGER DEFAULT 0,
        total_mailings INTEGER DEFAULT 0
    )''')
    # Шаблоны
    c.execute('''CREATE TABLE IF NOT EXISTS templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        content TEXT,
        delay REAL DEFAULT 3.0
    )''')
    # Счета CryptoBot
    c.execute('''CREATE TABLE IF NOT EXISTS invoices (
        invoice_id TEXT PRIMARY KEY,
        user_id INTEGER,
        days INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Статистика по каждой рассылке (лог)
    c.execute('''CREATE TABLE IF NOT EXISTS mailing_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        token_id INTEGER,
        mode TEXT,
        total INTEGER,
        sent INTEGER,
        errors INTEGER,
        start_time TIMESTAMP,
        end_time TIMESTAMP,
        message TEXT,
        recipients_sample TEXT
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

# ----- VK токены (несколько) -----
def add_vk_token(user_id, token, name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO vk_tokens (user_id, token, name, is_active) VALUES (?, ?, ?, 0)', (user_id, token, name))
    conn.commit()
    conn.close()

def get_all_tokens(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, is_active, total_sent, total_errors, total_mailings FROM vk_tokens WHERE user_id=? ORDER BY created_at', (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_token_by_id(token_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, token, name, is_active FROM vk_tokens WHERE id=? AND user_id=?', (token_id, user_id))
    row = c.fetchone()
    conn.close()
    return row

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

def update_token_stats(token_id, sent, errors, mailings_count=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE vk_tokens SET total_sent = total_sent + ?, total_errors = total_errors + ?, total_mailings = total_mailings + ?, last_used = ? WHERE id=?',
              (sent, errors, mailings_count, datetime.now().isoformat(), token_id))
    conn.commit()
    conn.close()

def get_active_token(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, token, name FROM vk_tokens WHERE user_id=? AND is_active=1', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_token_stats(token_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT total_sent, total_errors, total_mailings FROM vk_tokens WHERE id=?', (token_id,))
    row = c.fetchone()
    conn.close()
    return row if row else (0,0,0)

# ----- Шаблоны -----
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

def get_template_by_id(template_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, content, delay FROM templates WHERE id=? AND user_id=?', (template_id, user_id))
    row = c.fetchone()
    conn.close()
    return row

# ----- Экспорт/импорт шаблонов -----
def export_templates(user_id):
    templates = get_templates(user_id)
    data = []
    for t in templates:
        data.append({'name': t[1], 'content': t[2], 'delay': t[3]})
    return json.dumps(data, ensure_ascii=False, indent=2)

def import_templates(user_id, json_str):
    try:
        data = json.loads(json_str)
        for item in data:
            save_template(user_id, item['name'], item['content'], item['delay'])
        return True, len(data)
    except Exception as e:
        return False, str(e)

# ----- Логи рассылок -----
def add_mailing_log(user_id, token_id, mode, total, sent, errors, start_time, end_time, message, recipients_sample):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO mailing_logs 
        (user_id, token_id, mode, total, sent, errors, start_time, end_time, message, recipients_sample)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, token_id, mode, total, sent, errors, start_time.isoformat(), end_time.isoformat(), message[:200], recipients_sample))
    conn.commit()
    conn.close()

def get_user_mailing_stats(user_id, limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT ml.id, vt.name, ml.mode, ml.total, ml.sent, ml.errors, ml.start_time, ml.end_time
                FROM mailing_logs ml
                JOIN vk_tokens vt ON ml.token_id = vt.id
                WHERE ml.user_id=? ORDER BY ml.start_time DESC LIMIT ?''', (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

# ----- Подписка -----
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

# ==================== VK ФУНКЦИИ ====================
def disable_ssl():
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
disable_ssl()

def create_vk_session(token):
    # Для Windows – отключаем проверку SSL
    session = requests.Session()
    session.verify = False
    requests.packages.urllib3.disable_warnings()
    return VkApi(token=token, api_version='5.131', session=session)

def validate_vk_token(token):
    try:
        vk_session = create_vk_session(token)
        vk = vk_session.get_api()
        info = vk.account.getProfileInfo()
        try:
            phone = vk.account.getPhone().get('phone', 'не указан')
        except:
            phone = 'нет доступа'
        return True, info['first_name'], info['last_name'], phone, info['id']
    except Exception as e:
        return False, str(e), None, None, None

def get_recipients(token, mode='friends'):
    """
    mode: friends - только личные диалоги (друзья)
          chats - только беседы (групповые чаты)
          all - и то и другое
    """
    vk_session = create_vk_session(token)
    vk = vk_session.get_api()
    recipients = []
    offset = 0
    count = 200
    while True:
        try:
            convs = vk.messages.getConversations(offset=offset, count=count, filter="all")
        except Exception as e:
            logger.error(f"Ошибка загрузки диалогов: {e}")
            break
        items = convs.get('items', [])
        if not items:
            break
        for item in items:
            conv = item['conversation']
            peer = conv['peer']
            peer_type = peer['type']
            can_write = conv.get('can_write', {}).get('allowed', False)
            if not can_write:
                continue
            if mode == 'friends' and peer_type != 'user':
                continue
            if mode == 'chats' and peer_type != 'chat':
                continue
            title = conv.get('chat_settings', {}).get('title', f"{peer_type}_{peer['id']}")
            recipients.append({
                'peer_id': peer['id'],
                'title': title,
                'type': peer_type
            })
        offset += count
        if len(items) < count:
            break
    return recipients

def send_vk_message(token, peer_id, text):
    vk_session = create_vk_session(token)
    vk = vk_session.get_api()
    # Обычные эмодзи и гифки не требуют специальной обработки, просто текст
    random_id = random.randint(1, 2**31-1)
    try:
        vk.messages.send(peer_id=peer_id, message=text, random_id=random_id)
        return True, None
    except ApiError as e:
        if e.code in (901, 913, 917, 902):
            return False, f"Ошибка {e.code} (приватность)"
        else:
            return False, str(e)
    except Exception as e:
        return False, str(e)

# ==================== CRYPTOBOT ====================
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

# ==================== FSM состояния ====================
class BotStates(StatesGroup):
    waiting_token = State()
    waiting_phone = State()
    waiting_password = State()
    waiting_newsletter_text = State()
    waiting_delay = State()
    waiting_mode = State()            # выбор режима рассылки (friends/chats/all)
    waiting_template_name = State()
    waiting_template_content = State()
    waiting_template_delay = State()
    waiting_import_json = State()      # импорт шаблонов
    admin_broadcast = State()
    admin_user_id = State()
    admin_days = State()

# ==================== Клавиатуры ====================
def main_menu(uid):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="start_mailing", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="🔑 Мои аккаунты VK", callback_data="my_accounts", icon_custom_emoji_id="5472096095280572227", style="default")],
        [InlineKeyboardButton(text="➕ Добавить токен", callback_data="add_token", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="📱 Войти по номеру", callback_data="phone_login", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="📝 Шаблоны", callback_data="my_templates", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text="📊 Статистика аккаунтов", callback_data="accounts_stats", icon_custom_emoji_id="5278753302023004775", style="default")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="my_profile", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text="💰 Подписка", callback_data="buy_sub", icon_custom_emoji_id="5195058841988914267", style="success")],
    ])
    if uid in ADMIN_IDS:
        kb.inline_keyboard.append([InlineKeyboardButton(text="👑 Админ", callback_data="admin_panel", style="danger")])
    return kb

def back_button(callback_data="back_to_main"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data, style="default")]])

# ==================== Проверка подписки ====================
async def has_subscription(user_id):
    if user_id in ADMIN_IDS:
        return True
    sub = get_user_subscription(user_id)
    return sub and sub > datetime.now()

# ==================== Обработчики ====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    add_user(uid, message.from_user.username, message.from_user.first_name)
    welcome = ("<tg-emoji emoji-id='5278611606756942667'></tg-emoji> <b>Quasar VK Рассыльщик</b>\n\n"
               "Отправляй сообщения друзьям и беседам.\n"
               "Купи подписку для доступа.\n\n"
               "Используй кнопки меню.")
    await message.answer(welcome, parse_mode="HTML", reply_markup=main_menu(uid))

# ----- Добавление токена -----
@dp.callback_query(lambda c: c.data == "add_token")
async def add_token_prompt(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("🔑 Отправьте <b>токен VK</b> (scope: messages).\nПолучить: https://vkhost.github.io", parse_mode="HTML", reply_markup=None)
    await state.set_state(BotStates.waiting_token)

@dp.message(BotStates.waiting_token)
async def process_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if not token:
        await message.answer("❌ Токен не может быть пустым.")
        return
    msg = await message.answer("🔄 Проверка...")
    valid, first_name, last_name, phone, vk_id = validate_vk_token(token)
    if not valid:
        await msg.delete()
        await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка: {first_name}", parse_mode="HTML")
        return
    name = f"{first_name} {last_name}"
    add_vk_token(message.from_user.id, token, name)
    await msg.delete()
    await message.answer(
        f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Аккаунт добавлен\n👤 {name}\n🤙 {phone}\n🆔 {vk_id}\n\n✅ Токен валиден",
        parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ----- Вход по номеру -----
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
        valid, first_name, last_name, phone, vk_id = validate_vk_token(token)
        if not valid:
            await message.answer(f"❌ Ошибка: {first_name}")
            await state.clear()
            return
        name = f"{first_name} {last_name}"
        add_vk_token(message.from_user.id, token, name)
        await message.answer(
            f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Аккаунт добавлен через номер\n👤 {name}\n🤙 {phone}\n🆔 {vk_id}",
            parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()

# ----- Мои аккаунты (управление) -----
@dp.callback_query(lambda c: c.data == "my_accounts")
async def list_accounts(callback: CallbackQuery):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    await callback.answer()
    uid = callback.from_user.id
    tokens = get_all_tokens(uid)
    if not tokens:
        await callback.message.edit_text("❌ Нет аккаунтов. Добавьте через кнопки.", reply_markup=back_button())
        return
    text = "<tg-emoji emoji-id='5472096095280572227'></tg-emoji> <b>Ваши аккаунты VK</b>\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in tokens:
        token_id, name, is_active, sent, errors, mailings = t
        status = "✅" if is_active else "⚪"
        text += f"{status} <b>{name}</b> (id:{token_id})\n   📬 {sent} отправлено, ❌ {errors} ошибок, 🚀 {mailings} рассылок\n"
        if not is_active:
            kb.inline_keyboard.append([InlineKeyboardButton(text=f"🔘 Активировать {name}", callback_data=f"activate_{token_id}", style="primary")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🗑️ Удалить аккаунт", callback_data="delete_acc_menu", style="danger")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("activate_"))
async def activate_token(callback: CallbackQuery):
    token_id = int(callback.data.split("_")[1])
    uid = callback.from_user.id
    set_active_token(uid, token_id)
    await callback.answer("Аккаунт активирован!", show_alert=True)
    await list_accounts(callback)

@dp.callback_query(lambda c: c.data == "delete_acc_menu")
async def delete_account_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    tokens = get_all_tokens(uid)
    if not tokens:
        await callback.answer("Нет аккаунтов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in tokens:
        token_id, name, _, _, _, _ = t
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"❌ {name}", callback_data=f"del_acc_{token_id}", style="danger")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="my_accounts", style="default")])
    await callback.message.edit_text("🗑️ Выберите аккаунт для удаления:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("del_acc_"))
async def delete_account(callback: CallbackQuery):
    token_id = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    delete_token(uid, token_id)
    await callback.answer("Аккаунт удалён", show_alert=True)
    await list_accounts(callback)

# ----- Статистика по аккаунтам (детальная) -----
@dp.callback_query(lambda c: c.data == "accounts_stats")
async def show_accounts_stats(callback: CallbackQuery):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    await callback.answer()
    uid = callback.from_user.id
    tokens = get_all_tokens(uid)
    if not tokens:
        await callback.message.edit_text("Нет аккаунтов.", reply_markup=back_button())
        return
    text = "<tg-emoji emoji-id='5278753302023004775'></tg-emoji> <b>Статистика по аккаунтам</b>\n\n"
    for t in tokens:
        token_id, name, is_active, sent, errors, mailings = t
        status = "Активен ✅" if is_active else "Не активен"
        text += f"🔹 {name} ({status})\n   📬 Отправлено: {sent}\n   ❌ Ошибок: {errors}\n   🚀 Рассылок: {mailings}\n\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())

# ----- Рассылка (с выбором режима: друзья / беседы / всё) -----
@dp.callback_query(lambda c: c.data == "start_mailing")
async def start_mailing(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    active = get_active_token(callback.from_user.id)
    if not active:
        await callback.message.edit_text("❌ Нет активного аккаунта. Добавьте и активируйте в разделе «Мои аккаунты».", reply_markup=back_button())
        return
    token_id, token, name = active
    # Проверяем валидность токена
    valid, fn, ln, phone, vid = validate_vk_token(token)
    if not valid:
        await callback.message.edit_text(f"❌ Активный токен невалиден: {fn}\nУдалите или активируйте другой.", reply_markup=back_button())
        return
    await state.update_data(token_id=token_id, token=token, token_name=name)
    # Предлагаем выбрать режим
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Только друзья", callback_data="mode_friends", style="primary")],
        [InlineKeyboardButton(text="💬 Только беседы", callback_data="mode_chats", style="primary")],
        [InlineKeyboardButton(text="🌐 Всё (друзья+беседы)", callback_data="mode_all", style="primary")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")]
    ])
    await callback.message.edit_text("📌 Выберите тип получателей:", reply_markup=kb)
    await state.set_state(BotStates.waiting_mode)

@dp.callback_query(lambda c: c.data.startswith("mode_"))
async def select_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split("_")[1]  # friends / chats / all
    await state.update_data(mode=mode)
    await callback.answer()
    await callback.message.edit_text("✏️ Введите текст сообщения (можно HTML, эмодзи, GIF):", reply_markup=None)
    await state.set_state(BotStates.waiting_newsletter_text)

@dp.message(BotStates.waiting_newsletter_text)
async def get_text(message: Message, state: FSMContext):
    text = message.text
    if not text:
        await message.answer("❌ Текст не может быть пустым.")
        return
    await state.update_data(text=text)
    await message.answer("⏱ Введите задержку в секундах (например, 2):", reply_markup=None)
    await state.set_state(BotStates.waiting_delay)

@dp.message(BotStates.waiting_delay)
async def get_delay(message: Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите число > 0")
        return
    data = await state.get_data()
    token = data.get('token')
    token_id = data.get('token_id')
    token_name = data.get('token_name')
    mode = data.get('mode')
    text = data.get('text')
    await state.clear()
    # Загружаем получателей в соответствии с режимом
    progress_msg = await message.answer("🔄 Загружаю получателей...")
    try:
        recipients = get_recipients(token, mode=mode)
    except Exception as e:
        await progress_msg.delete()
        await message.answer(f"❌ Ошибка загрузки: {e}", reply_markup=main_menu(message.from_user.id))
        return
    if not recipients:
        await progress_msg.delete()
        await message.answer("⚠️ Нет получателей для выбранного режима.", reply_markup=main_menu(message.from_user.id))
        return
    total = len(recipients)
    sent = 0
    errors = 0
    start_time = datetime.now()
    await progress_msg.edit_text(f"🚀 Начинаю рассылку {total} получателям (режим: {mode}) с задержкой {delay} сек...")
    # Отправка
    for idx, rec in enumerate(recipients, 1):
        success, err = send_vk_message(token, rec['peer_id'], text)
        if success:
            sent += 1
        else:
            errors += 1
        if idx % 10 == 0 or idx == total:
            await progress_msg.edit_text(f"📊 Прогресс: {idx}/{total} | ✅{sent} ❌{errors}")
        await asyncio.sleep(delay)
    end_time = datetime.now()
    # Сохраняем статистику
    update_token_stats(token_id, sent, errors, 1)
    sample = json.dumps([r['peer_id'] for r in recipients[:5]], ensure_ascii=False)
    add_mailing_log(message.from_user.id, token_id, mode, total, sent, errors, start_time, end_time, text, sample)
    await message.answer(
        f"<tg-emoji emoji-id='5206401524200145033'></tg-emoji> <b>Рассылка завершена!</b>\n"
        f"📝 Отправлено: {sent}/{total}\n"
        f"   ┣ ✅ Успешно: {sent}\n"
        f"   ┗ ❌ Ошибки: {errors}\n"
        f"⏲️ Время: {delay*total:.1f} сек.\n"
        f"👤 Аккаунт: {token_name}",
        parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await progress_msg.delete()

# ----- Шаблоны (экспорт/импорт) -----
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
        await callback.message.edit_text("📭 Нет шаблонов. Создайте или импортируйте.", reply_markup=kb)
        return
    text = "📋 <b>Ваши шаблоны</b>\n\n"
    for t in tpls:
        text += f"🔹 <b>{t[1]}</b> — <code>{t[2][:40]}...</code> (⏱{t[3]}с)\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="create_template", style="success")],
        [InlineKeyboardButton(text="❌ Удалить", callback_data="delete_template", style="danger")],
        [InlineKeyboardButton(text="📤 Экспорт", callback_data="export_templates", style="default")],
        [InlineKeyboardButton(text="📥 Импорт", callback_data="import_templates", style="default")],
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
    uid = callback.from_user.id
    delete_template(tpl_id, uid)
    await callback.answer("Шаблон удалён", show_alert=True)
    await templates_menu(callback)

@dp.callback_query(lambda c: c.data == "export_templates")
async def export_templates_handler(callback: CallbackQuery):
    uid = callback.from_user.id
    data = export_templates(uid)
    if data == "[]":
        await callback.answer("Нет шаблонов для экспорта", show_alert=True)
        return
    # Отправляем JSON файлом
    import io
    file = io.BytesIO(data.encode('utf-8'))
    await callback.message.answer_document(FSInputFile(file, filename="templates.json"), caption="📁 Ваши шаблоны")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "import_templates")
async def import_templates_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📂 Отправьте JSON-файл с шаблонами (экспортированный ранее).\nФормат: список объектов с полями name, content, delay.", reply_markup=None)
    await state.set_state(BotStates.waiting_import_json)

@dp.message(BotStates.waiting_import_json)
async def process_import_json(message: Message, state: FSMContext):
    if not message.document:
        await message.answer("❌ Отправьте файл в формате JSON.")
        return
    file = await bot.get_file(message.document.file_id)
    file_bytes = await bot.download_file(file.file_path)
    content = file_bytes.read().decode('utf-8')
    success, result = import_templates(message.from_user.id, content)
    if success:
        await message.answer(f"✅ Импортировано {result} шаблонов.", reply_markup=main_menu(message.from_user.id))
    else:
        await message.answer(f"❌ Ошибка импорта: {result}", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ----- Профиль и статистика рассылок -----
@dp.callback_query(lambda c: c.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    uid = callback.from_user.id
    user = None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT username, first_name, subscription_until, joined_at FROM users WHERE telegram_id=?', (uid,))
    row = c.fetchone()
    conn.close()
    if row:
        username, first_name, sub_until, joined = row
        sub_text = datetime.fromisoformat(sub_until).strftime('%d.%m.%Y %H:%M') if sub_until else "Нет"
        if uid in ADMIN_IDS:
            sub_text = "Вечная"
        text = (f"<tg-emoji emoji-id='5275979556308674886'></tg-emoji> <b>Профиль</b>\n\n"
                f"🆔 ID: <code>{uid}</code>\n"
                f"📛 Имя: {first_name or 'Не указано'}\n"
                f"🔖 Username: @{username or 'Нет'}\n"
                f"📅 Регистрация: {datetime.fromisoformat(joined).strftime('%d.%m.%Y') if joined else 'Неизвестно'}\n"
                f"⏳ Подписка до: {sub_text}")
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())
    else:
        await callback.message.edit_text("Ошибка", reply_markup=back_button())

@dp.callback_query(lambda c: c.data == "my_stats")
async def show_mailing_stats(callback: CallbackQuery):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("Нет подписки!", show_alert=True)
        return
    uid = callback.from_user.id
    logs = get_user_mailing_stats(uid, 10)
    if not logs:
        await callback.message.edit_text("📭 Нет завершённых рассылок.", reply_markup=back_button())
        return
    text = "<tg-emoji emoji-id='5278753302023004775'></tg-emoji> <b>Последние рассылки</b>\n\n"
    for log in logs:
        log_id, token_name, mode, total, sent, errors, start_time, end_time = log
        dt = datetime.fromisoformat(start_time).strftime('%Y-%m-%d %H:%M')
        text += f"🗓 {dt} | {token_name} | {mode}\n   📬 {total} | ✅{sent} ❌{errors}\n\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())

# ----- Подписка через CryptoBot -----
@dp.callback_query(lambda c: c.data == "buy_sub")
async def buy_sub_menu(callback: CallbackQuery):
    if not CRYPTOBOT_TOKEN:
        await callback.message.edit_text("⚠️ CryptoBot недоступен", reply_markup=back_button())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📆 1 день - 2$", callback_data="sub_1_2", style="primary")],
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
        await callback.answer("Ошибка создания счёта", show_alert=True)
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

# ----- Админ-панель (упрощённая) -----
@dp.callback_query(lambda c: c.data == "admin_panel" and c.from_user.id in ADMIN_IDS)
async def admin_panel(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton(text="📨 Рассылка пользователям", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="admin_give_sub", style="success")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="danger")]
    ])
    await callback.message.edit_text("👑 Админ-панель", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users')
    users_count = c.fetchone()[0]
    c.execute('SELECT SUM(total_sent) FROM vk_tokens')
    total_sent = c.fetchone()[0] or 0
    c.execute('SELECT SUM(total_errors) FROM vk_tokens')
    total_errors = c.fetchone()[0] or 0
    c.execute('SELECT COUNT(*) FROM mailing_logs')
    total_mailings = c.fetchone()[0] or 0
    conn.close()
    text = f"📊 Статистика бота\n👥 Пользователей: {users_count}\n🚀 Рассылок: {total_mailings}\n✉️ Отправлено: {total_sent}\n❌ Ошибок: {total_errors}"
    await callback.message.edit_text(text, reply_markup=back_button("admin_panel"))

@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✏️ Введите текст для рассылки всем пользователям:", reply_markup=None)
    await state.set_state(BotStates.admin_broadcast)

@dp.message(BotStates.admin_broadcast)
async def admin_broadcast_process(message: Message, state: FSMContext):
    text = message.text
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT telegram_id FROM users')
    users = c.fetchall()
    conn.close()
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid[0], f"📢 <b>Анонс</b>\n\n{text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Отправлено {sent} из {len(users)} пользователей.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_give_sub")
async def admin_give_sub(callback: CallbackQuery, state: FSMContext):
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
    await message.answer(f"✅ Пользователю {target} выдана подписка на {days} дней.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ----- Возврат в главное меню -----
@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu(callback.from_user.id))

# ==================== ЗАПУСК ====================
async def on_startup():
    init_db()
    logger.info("Бот запущен")
    if not ANTICAPTCHA_KEY:
        logger.warning("ANTICAPTCHA_KEY не задан (капча не будет решаться)")
    if not CRYPTOBOT_TOKEN:
        logger.warning("CRYPTOBOT_TOKEN не задан (подписка через CryptoBot недоступна)")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())