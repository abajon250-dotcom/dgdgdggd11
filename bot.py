import os
import re
import time
import random
import asyncio
import logging
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
from vk_api import VkApi
from vk_api.exceptions import ApiError
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

from database import (
    init_db, add_user, get_user, get_user_subscription, set_subscription,
    revoke_subscription, get_all_users,
    get_bot_stats, save_mailing_stats, get_mailing_stats, get_user_mailing_stats,
    save_template, get_templates, delete_template, get_template_by_id,
    create_invoice_db, get_pending_invoice, mark_invoice_paid,
    add_vk_token, add_multiple_vk_tokens, get_user_tokens, set_active_token, delete_token, get_active_token, update_token_stats, validate_and_clean_tokens
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip().lstrip('@')) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip('@').isdigit()]
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")
ANTICAPTCHA_KEY = os.getenv("ANTICAPTCHA_KEY", "")
VK_API_VERSION = "5.131"
RATE_LIMIT_SECONDS = 2
UPDATE_INTERVAL = 2.0

CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api/"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SSLDisabledHTTPAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = ssl._create_unverified_context()
        return super().init_poolmanager(*args, **kwargs)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

SUBSCRIPTION_PLANS = {1: 2.0, 7: 5.0, 30: 15.0}

# ---------------------- VK helpers ----------------------
def create_vk_session(token: str):
    session = requests.Session()
    session.mount('https://', SSLDisabledHTTPAdapter())
    return VkApi(token=token, api_version=VK_API_VERSION, session=session)

INVISIBLE_CHAR = '\u200B'
def obfuscate_links(text: str) -> str:
    text = re.sub(r'https://', 'https: //', text, flags=re.IGNORECASE)
    text = re.sub(r'http://', 'http: //', text, flags=re.IGNORECASE)
    def insert_invisible(match):
        domain = match.group(0)
        if len(domain) > 2:
            pos = random.randint(1, len(domain)-1)
            return domain[:pos] + INVISIBLE_CHAR + domain[pos:]
        return domain
    text = re.sub(r'\S+\.\S+', insert_invisible, text)
    return text

def solve_captcha(captcha_url: str) -> str:
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

def validate_vk_token_strong(token: str) -> Tuple[bool, str, Optional[Dict], Optional[Dict]]:
    vk_session = create_vk_session(token)
    vk = vk_session.get_api()
    try:
        info = vk.account.getProfileInfo()
        try:
            phone = vk.account.getPhone().get('phone', 'не указан')
        except:
            phone = 'нет доступа'
        user_info = {
            'first_name': info.get('first_name', ''),
            'last_name': info.get('last_name', ''),
            'phone': phone,
            'id': info.get('id', 0)
        }
        convs = vk.messages.getConversations(count=1)
        if 'items' not in convs:
            return False, "Токен не имеет прав на чтение диалогов (scope: messages).", None, None
        recipients, stats = get_recipients_from_token(vk, None, only_friends=False)
        return True, "OK", user_info, stats
    except ApiError as e:
        if e.code == 5:
            return False, "Токен невалиден (ошибка авторизации).", None, None
        elif e.code == 30:
            return False, "Профиль пользователя удалён или заблокирован.", None, None
        elif e.code == 200:
            return False, "Нет доступа. Убедитесь, что токен имеет права на отправку сообщений.", None, None
        else:
            return False, f"Ошибка VK {e.code}: {e.error.get('error_msg', 'Неизвестная ошибка')}", None, None
    except Exception as e:
        return False, f"Неизвестная ошибка: {str(e)}", None, None

def get_recipients_from_token(vk, token: str = None, only_friends: bool = False) -> Tuple[List[Dict], Dict]:
    if vk is None:
        vk_session = create_vk_session(token)
        vk = vk_session.get_api()
    recipients = []
    dialogues_count = 0
    contacts_count = 0
    groups_count = 0
    offset = 0
    count = 200
    while True:
        try:
            convs = vk.messages.getConversations(offset=offset, count=count, filter="all")
        except ApiError as e:
            if e.code == 14 and ANTICAPTCHA_KEY:
                logger.warning("🛡️ Капча при загрузке диалогов, решаем...")
                captcha_key = solve_captcha(e.captcha_img)
                convs = vk.messages.getConversations(offset=offset, count=count, filter="all",
                                                     captcha_sid=e.captcha_sid, captcha_key=captcha_key)
            else:
                logger.error(f"❌ Ошибка VK: {e}")
                break
        items = convs.get("items", [])
        if not items:
            break
        for item in items:
            conversation = item.get("conversation", {})
            peer = conversation.get("peer", {})
            peer_id = peer.get("id")
            can_write = conversation.get("can_write", {}).get("allowed", False)
            if not can_write:
                continue
            peer_type = peer.get("type")
            if only_friends and peer_type != "user":
                continue
            title = None
            if peer_type != "user":
                title = conversation.get("chat_settings", {}).get("title", "")
            if peer_type == "user":
                contacts_count += 1
            elif peer_type == "chat":
                dialogues_count += 1
            elif peer_type == "group":
                groups_count += 1

            if peer_type == "user":
                try:
                    user_info = vk.users.get(user_ids=peer_id, fields="first_name,last_name")
                    name = f"{user_info[0]['first_name']} {user_info[0]['last_name']}" if user_info else str(peer_id)
                except:
                    name = f"User {peer_id}"
            elif peer_type == "chat":
                name = title if title else f"Беседа {peer_id}"
            else:
                name = title if title else f"Группа {peer_id}"
            recipients.append({"peer_id": peer_id, "name": name, "type": peer_type})
        offset += count
        if len(items) < count:
            break
    stats = {
        'dialogues': dialogues_count,
        'contacts': contacts_count,
        'groups': groups_count,
        'total': len(recipients)
    }
    return recipients, stats

def send_vk_message(vk, peer_id: int, text: str) -> bool:
    text = obfuscate_links(text)
    random_id = random.randint(1, 2**31 - 1)
    while True:
        try:
            vk.messages.send(peer_id=peer_id, message=text, random_id=random_id)
            return True
        except ApiError as e:
            if e.code == 14 and ANTICAPTCHA_KEY:
                logger.warning(f"🛡️ Капча при отправке, решаем...")
                captcha_key = solve_captcha(e.captcha_img)
                try:
                    vk.messages.send(peer_id=peer_id, message=text, random_id=random_id,
                                     captcha_sid=e.captcha_sid, captcha_key=captcha_key)
                    return True
                except ApiError as e2:
                    logger.error(f"❌ Не удалось отправить после капчи: {e2}")
                    return False
            elif e.code in (901, 913, 917, 902):
                logger.warning(f"⚠️ Ошибка {e.code}, пропускаем получателя {peer_id}")
                return False
            else:
                logger.error(f"❌ Ошибка {e.code}: {e}")
                return False
        except Exception as e:
            logger.exception(f"🔥 Критическая ошибка: {e}")
            return False

def make_progress_bar_text(percent: float, length: int = 20) -> str:
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)

# ---------------------- CryptoBot ----------------------
async def create_crypto_invoice(user_id: int, days: int, amount: float) -> Optional[str]:
    if not CRYPTOBOT_TOKEN:
        return None
    description = f"Subscription {days}d {amount} USDT"
    params = {"amount": amount, "asset": "USDT", "description": description, "user_id": user_id}
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        resp = requests.post(CRYPTOBOT_API_URL + "createInvoice", json=params, headers=headers)
        data = resp.json()
        if data.get("ok"):
            invoice = data["result"]
            invoice_id = str(invoice["invoice_id"])
            await create_invoice_db(invoice_id, user_id, amount, "USDT", days)
            return invoice["pay_url"]
    except Exception as e:
        logger.exception(f"CryptoBot error: {e}")
    return None

async def check_payment(user_id: int) -> bool:
    inv = await get_pending_invoice(user_id)
    if not inv:
        return False
    invoice_id = inv['invoice_id']
    days = inv['days']
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        resp = requests.post(CRYPTOBOT_API_URL + "getInvoices", json={"invoice_ids": invoice_id}, headers=headers)
        data = resp.json()
        if data.get("ok") and data["result"]["items"] and data["result"]["items"][0]["status"] == "paid":
            await mark_invoice_paid(invoice_id, days)
            current_sub = await get_user_subscription(user_id)
            if current_sub and current_sub > datetime.now():
                new_until = current_sub + timedelta(days=days)
            else:
                new_until = datetime.now() + timedelta(days=days)
            await set_subscription(user_id, (new_until - datetime.now()).days)
            return True
    except:
        pass
    return False

# ---------------------- Рассылка ----------------------
async def mailing_task(vk_token: str, recipients: List[Dict], text: str, delay: float,
                       chat_id: int, user_info: Dict, stats: Dict, user_telegram_id: int,
                       token_name: str, progress_msg_id: int):
    vk_session = create_vk_session(vk_token)
    vk = vk_session.get_api()
    total = len(recipients)
    sent_ok = 0
    sent_err = 0
    start_time = time.time()
    last_update = start_time

    async def update_progress(current_idx: int):
        nonlocal last_update
        now = time.time()
        if now - last_update < UPDATE_INTERVAL and current_idx < total:
            return
        last_update = now
        percent = (current_idx / total) * 100 if total > 0 else 100
        elapsed = now - start_time
        remaining = ((total - current_idx) / (current_idx / elapsed)) if current_idx > 0 else 0
        bar = make_progress_bar_text(percent)
        status = (
            f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> <b>Рассылка друзьям VK в процессе</b>\n\n"
            f"👤 Аккаунт: {token_name}\n"
            f"👤 Владелец: {user_info.get('first_name')} {user_info.get('last_name')}\n"
            f"🤙 Телефон: {user_info.get('phone')}\n"
            f"📂 Всего друзей: {stats['total']}\n"
            f"📊 Статистика рассылки:\n"
            f"   Всего: {total}\n"
            f"   <tg-emoji emoji-id='5206401524200145033'></tg-emoji> Отправлено: {sent_ok}\n"
            f"   <tg-emoji emoji-id='5206510891247371052'></tg-emoji> Ошибок: {sent_err}\n"
            f"   Прогресс: {percent:.1f}%\n"
            f"   {bar}\n"
            f"<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Осталось ~ {remaining:.0f} с"
        )
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=status, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Ошибка обновления прогресса: {e}")

    for idx, rec in enumerate(recipients, 1):
        success = await asyncio.to_thread(send_vk_message, vk, rec["peer_id"], text)
        if success:
            sent_ok += 1
        else:
            sent_err += 1
        await update_progress(idx)
        await asyncio.sleep(max(delay, RATE_LIMIT_SECONDS))

    total_time = time.time() - start_time
    success_rate = (sent_ok / total * 100) if total > 0 else 0
    final_text = (
        f"<tg-emoji emoji-id='5206401524200145033'></tg-emoji> <b>Рассылка друзьям завершена!</b>\n\n"
        f"📝 Отправлено: {sent_ok}/{total}\n"
        f"   ┣ <tg-emoji emoji-id='5206401524200145033'></tg-emoji> Успешно: {sent_ok}\n"
        f"   ┗ <tg-emoji emoji-id='5206510891247371052'></tg-emoji> Ошибки: {sent_err}\n"
        f"{'✅' if success_rate >= 70 else '⚠️'} Успешность: {success_rate:.1f}%\n"
        f"<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Время: {total_time:.1f} сек."
    )
    await bot.send_message(chat_id, final_text, parse_mode="HTML")
    await bot.delete_message(chat_id, progress_msg_id)
    vk_name = f"{user_info.get('first_name')} {user_info.get('last_name')}".strip()
    await save_mailing_stats(user_telegram_id, total, sent_ok, sent_err, total_time, text[:200], vk_name)

# ---------------------- Функция проверки токена для массовой валидации ----------------------
async def check_token_valid(token: str):
    valid, msg, _, _ = await asyncio.to_thread(validate_vk_token_strong, token)
    if not valid:
        raise Exception(msg)

# ---------------------- FSM ----------------------
class BotStates(StatesGroup):
    waiting_vk_token = State()
    waiting_mass_tokens = State()
    waiting_phone_login = State()
    waiting_phone_password = State()
    waiting_phone_2fa = State()
    waiting_newsletter_text = State()
    waiting_delay = State()
    waiting_template_name = State()
    waiting_template_content = State()
    waiting_template_delay = State()
    admin_waiting_broadcast = State()
    admin_waiting_user_id = State()
    admin_waiting_days = State()

# ---------------------- Проверка подписки ----------------------
async def check_subscription(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    sub = await get_user_subscription(user_id)
    return sub is not None and sub > datetime.now()

async def check_channel(user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ('member', 'administrator', 'creator')
    except:
        return False

# ---------------------- Клавиатуры (кнопки НЕ трогаем!) ----------------------
def main_menu(uid: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📨 Начать рассылку (только друзьям)", callback_data="start_mailing", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="🔍 Мои аккаунты VK", callback_data="check_vk", icon_custom_emoji_id="5472096095280572227", style="default")],
        [InlineKeyboardButton(text="➕ Добавить токен", callback_data="add_token", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="➕ Массовое добавление", callback_data="mass_add_tokens", icon_custom_emoji_id="5472096095280572227", style="default")],
        [InlineKeyboardButton(text="📱 Войти по номеру", callback_data="phone_login", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="📝 Мои шаблоны", callback_data="my_templates", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats", icon_custom_emoji_id="5278753302023004775", style="primary")],
        [InlineKeyboardButton(text="💰 Купить подписку", callback_data="buy_sub", icon_custom_emoji_id="5195058841988914267", style="success")],
    ]
    if uid in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_button(callback_data: str = "back_to_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data, style="default")]])

# ---------------------- Обработчики ----------------------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await add_user(uid, message.from_user.username, message.from_user.first_name)
    if not await check_channel(uid):
        await message.answer(f"❌ Подпишитесь на канал {REQUIRED_CHANNEL} и нажмите /start снова.")
        return
    welcome = (
        f"<tg-emoji emoji-id='5278611606756942667'></tg-emoji> <b>Добро пожаловать в VK Рассыльщик!</b>\n\n"
        f"🤖 Я помогу вам отправлять сообщения в личные диалоги и беседы ВКонтакте.\n\n"
        f"📌 <b>Что нужно сделать:</b>\n"
        f"1️⃣ Пополнить баланс через кнопку <b>💰 Купить подписку</b>\n"
        f"2️⃣ Ввести токен ВК через кнопку <b><tg-emoji emoji-id='5472096095280572227'></tg-emoji> Ввести токен VK</b>\n"
        f"3️⃣ Нажать <b>📨 Начать рассылку</b>\n\n"
        f"💡 Также доступны шаблоны сообщений, статистика.\n"
        f"⚡️ Для получения токена VK используйте: https://vkhost.github.io"
    )
    await message.answer(welcome, parse_mode="HTML", reply_markup=main_menu(uid))

# ---- Добавление токена ----
@dp.callback_query(lambda c: c.data == "add_token")
async def add_token_prompt(callback: CallbackQuery, state: FSMContext):
    if not await check_subscription(callback.from_user.id):
        await callback.answer("<tg-emoji emoji-id='5278578973595427038'></tg-emoji> Нет активной подписки!", show_alert=True, parse_mode="HTML")
        return
    await callback.answer()
    await callback.message.edit_text(
        "<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Отправьте <b>токен VK</b> (scope: messages).\nПолучить можно: https://vkhost.github.io",
        parse_mode="HTML", reply_markup=None
    )
    await state.set_state(BotStates.waiting_vk_token)

@dp.message(BotStates.waiting_vk_token)
async def process_single_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if not token:
        await message.answer("<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Токен не может быть пустым.", parse_mode="HTML")
        return
    uid = message.from_user.id
    msg = await message.answer("<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Жёсткая проверка токена...", parse_mode="HTML")
    valid, error_msg, user_info, stats = await asyncio.to_thread(validate_vk_token_strong, token)
    if not valid:
        await msg.delete()
        await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> <b>Ошибка:</b> {error_msg}", parse_mode="HTML")
        return
    await add_vk_token(uid, token, name=f"{user_info['first_name']} {user_info['last_name']}")
    await msg.delete()
    text = (
        f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> <b>Аккаунт добавлен</b>\n\n"
        f"👤 {user_info['first_name']} {user_info['last_name']}\n🤙 {user_info['phone']}\n🆔 {user_info['id']}\n\n"
        f"📊 Доступно диалогов: {stats['total']} (бесед: {stats['dialogues']}, личных: {stats['contacts']}, групп: {stats['groups']})"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu(uid))
    await state.clear()

# ---- Массовое добавление ----
@dp.callback_query(lambda c: c.data == "mass_add_tokens")
async def mass_add_prompt(callback: CallbackQuery, state: FSMContext):
    if not await check_subscription(callback.from_user.id):
        await callback.answer("<tg-emoji emoji-id='5278578973595427038'></tg-emoji> Нет активной подписки!", show_alert=True, parse_mode="HTML")
        return
    await callback.answer()
    await callback.message.edit_text(
        "📦 Отправьте список токенов в формате:\n\n`токен1 | Название1`\n`токен2 | Название2`\n\nТокены будут проверены и добавлены.",
        parse_mode="Markdown", reply_markup=None
    )
    await state.set_state(BotStates.waiting_mass_tokens)

@dp.message(BotStates.waiting_mass_tokens)
async def process_mass_tokens(message: Message, state: FSMContext):
    lines = message.text.strip().split('\n')
    tokens_data = []
    uid = message.from_user.id
    await message.answer("<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Проверка...", parse_mode="HTML")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split('|')
        token = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else None
        valid, error_msg, user_info, _ = await asyncio.to_thread(validate_vk_token_strong, token)
        if not valid:
            await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка для {token[:10]}...: {error_msg}", parse_mode="HTML")
            continue
        if not name:
            name = f"{user_info['first_name']} {user_info['last_name']}"
        tokens_data.append((token, name))
    if tokens_data:
        await add_multiple_vk_tokens(uid, tokens_data)
        await message.answer(f"<tg-emoji emoji-id='5206401524200145033'></tg-emoji> Добавлено {len(tokens_data)} аккаунтов.", parse_mode="HTML", reply_markup=main_menu(uid))
    else:
        await message.answer("<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Не добавлено ни одного токена.", parse_mode="HTML", reply_markup=main_menu(uid))
    await state.clear()

# ---- Вход по номеру телефона (исправлен) ----
@dp.callback_query(lambda c: c.data == "phone_login")
async def phone_login_start(callback: CallbackQuery, state: FSMContext):
    if not await check_subscription(callback.from_user.id):
        await callback.answer("<tg-emoji emoji-id='5278578973595427038'></tg-emoji> Нет активной подписки!", show_alert=True, parse_mode="HTML")
        return
    await callback.answer()
    await callback.message.edit_text(
        "📱 Введите номер телефона в формате +71234567890:",
        parse_mode="HTML", reply_markup=None
    )
    await state.set_state(BotStates.waiting_phone_login)

@dp.message(BotStates.waiting_phone_login)
async def phone_login_get_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith('+'):
        await message.answer("❌ Номер должен начинаться с +. Попробуйте ещё раз.", reply_markup=None)
        return
    await state.update_data(login=phone)
    await message.answer("🔑 Введите пароль от аккаунта VK:", reply_markup=None)
    await state.set_state(BotStates.waiting_phone_password)

@dp.message(BotStates.waiting_phone_password)
async def phone_login_get_password(message: Message, state: FSMContext):
    password = message.text
    await state.update_data(password=password)
    # Пытаемся авторизоваться
    data = await state.get_data()
    login = data.get('login')
    await message.answer("<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Попытка авторизации...", parse_mode="HTML")
    # Выполняем авторизацию через vk_api
    try:
        vk_session = VkApi(login=login, password=password, api_version=VK_API_VERSION)
        vk_session.auth(token_only=True)
        token = vk_session.token['access_token']
        # Проверяем полученный токен
        valid, error_msg, user_info, stats = validate_vk_token_strong(token)
        if not valid:
            await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка авторизации: {error_msg}", parse_mode="HTML")
            await state.clear()
            return
        # Сохраняем токен
        await add_vk_token(message.from_user.id, token, name=f"{user_info['first_name']} {user_info['last_name']}")
        text = (
            f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> <b>Аккаунт добавлен через номер телефона</b>\n\n"
            f"👤 {user_info['first_name']} {user_info['last_name']}\n🤙 {user_info['phone']}\n🆔 {user_info['id']}\n\n"
            f"📊 Доступно диалогов: {stats['total']} (бесед: {stats['dialogues']}, личных: {stats['contacts']}, групп: {stats['groups']})"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
        await state.clear()
    except ApiError as e:
        if e.code == 5:
            await message.answer("<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Неверный номер телефона или пароль.", parse_mode="HTML")
        elif e.code == 14:
            await message.answer("<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Требуется капча. Используйте вход по токену.", parse_mode="HTML")
        else:
            await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка VK: {e}", parse_mode="HTML")
        await state.clear()
    except Exception as e:
        await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка: {str(e)}", parse_mode="HTML")
        await state.clear()

# ---- Просмотр аккаунтов, активация, удаление, проверка ----
@dp.callback_query(lambda c: c.data == "check_vk")
async def list_accounts(callback: CallbackQuery):
    if not await check_subscription(callback.from_user.id):
        await callback.answer("<tg-emoji emoji-id='5278578973595427038'></tg-emoji> Нет активной подписки!", show_alert=True, parse_mode="HTML")
        return
    await callback.answer()
    uid = callback.from_user.id
    tokens = await get_user_tokens(uid)
    if not tokens:
        await callback.message.edit_text(
            "❌ Нет аккаунтов. Нажмите «➕ Добавить токен» или «📱 Войти по номеру».",
            reply_markup=back_button()
        )
        return
    text = "<tg-emoji emoji-id='5472096095280572227'></tg-emoji> <b>Ваши аккаунты VK</b>\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in tokens:
        status = "✅" if t['is_active'] else "⚪"
        text += f"{status} <b>{t['name']}</b> (id:{t['id']})\n"
        if not t['is_active']:
            kb.inline_keyboard.append([InlineKeyboardButton(text=f"🔘 Активировать {t['name']}", callback_data=f"activate_token_{t['id']}", style="primary")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔄 Проверить все токены", callback_data="validate_all_tokens", style="success")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🗑️ Удалить аккаунт", callback_data="delete_account_menu", style="danger")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "validate_all_tokens")
async def validate_all_tokens_cmd(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    await callback.message.edit_text("<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Проверка всех токенов...", reply_markup=None, parse_mode="HTML")
    deleted, remaining = await validate_and_clean_tokens(uid, check_token_valid)
    await callback.message.edit_text(
        f"<tg-emoji emoji-id='5206401524200145033'></tg-emoji> Проверка завершена.\nУдалено невалидных: {deleted}\nОсталось активных: {remaining}",
        parse_mode="HTML", reply_markup=back_button("check_vk")
    )

@dp.callback_query(lambda c: c.data.startswith("activate_token_"))
async def activate_token(callback: CallbackQuery):
    token_id = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    await set_active_token(uid, token_id)
    await callback.answer("Аккаунт активирован!", show_alert=True)
    await list_accounts(callback)

@dp.callback_query(lambda c: c.data == "delete_account_menu")
async def delete_account_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    tokens = await get_user_tokens(uid)
    if not tokens:
        await callback.answer("Нет аккаунтов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in tokens:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"❌ {t['name']}", callback_data=f"del_token_{t['id']}", style="danger")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="check_vk", style="default")])
    await callback.message.edit_text("🗑️ Выберите аккаунт для удаления:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("del_token_"))
async def delete_token_cmd(callback: CallbackQuery):
    token_id = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    await delete_token(uid, token_id)
    await callback.answer("Аккаунт удалён", show_alert=True)
    await list_accounts(callback)

# ---- Подписка ----
@dp.callback_query(lambda c: c.data == "buy_sub")
async def buy_subscription_menu(callback: CallbackQuery):
    await callback.answer()
    if not CRYPTOBOT_TOKEN:
        await callback.message.edit_text("⚠️ Оплата через CryptoBot недоступна.", reply_markup=back_button())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for days, price in SUBSCRIPTION_PLANS.items():
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"📆 {days} дн. - {price}$", callback_data=f"sub_{days}_{price}", icon_custom_emoji_id="5195058841988914267", style="primary")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")])
    await callback.message.edit_text(
        f"<tg-emoji emoji-id='5195058841988914267'></tg-emoji> <b>Выберите срок подписки</b>\n\nПосле оплаты подписка активируется автоматически.",
        parse_mode="HTML", reply_markup=kb
    )

@dp.callback_query(lambda c: c.data.startswith("sub_"))
async def process_subscription_choice(callback: CallbackQuery):
    _, days_str, price_str = callback.data.split("_")
    days = int(days_str)
    price = float(price_str)
    pay_url = await create_crypto_invoice(callback.from_user.id, days, price)
    if not pay_url:
        await callback.answer("❌ Ошибка создания счёта", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить", url=pay_url, icon_custom_emoji_id="5195058841988914267", style="success")],
        [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_payment", icon_custom_emoji_id="5195058841988914267", style="primary")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="buy_sub", style="default")]
    ])
    await callback.message.edit_text(
        f"<tg-emoji emoji-id='5195058841988914267'></tg-emoji> <b>Оплата подписки на {days} дней</b>\n\n"
        f"Сумма: <b>{price} USDT</b>\n"
        f"После оплаты нажмите «Проверить».\n"
        f"Счёт действителен 1 час.",
        parse_mode="HTML", reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "check_payment")
async def check_payment_callback(callback: CallbackQuery):
    paid = await check_payment(callback.from_user.id)
    if paid:
        await callback.answer("✅ Оплата получена!", show_alert=True)
        await callback.message.edit_text("✅ Подписка активирована!", reply_markup=back_button())
    else:
        await callback.answer("⏳ Оплата не найдена", show_alert=True)

# ---- РАССЫЛКА ТОЛЬКО ДРУЗЬЯМ ----
@dp.callback_query(lambda c: c.data == "start_mailing")
async def start_newsletter(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await check_subscription(callback.from_user.id):
        await callback.message.edit_text(
            "<tg-emoji emoji-id='5278578973595427038'></tg-emoji> Нет активной подписки.",
            parse_mode="HTML", reply_markup=back_button()
        )
        return
    active = await get_active_token(callback.from_user.id)
    if not active:
        await callback.message.edit_text(
            "❌ Нет активного аккаунта. Добавьте и активируйте через «🔍 Мои аккаунты VK».",
            reply_markup=back_button()
        )
        return
    valid, error_msg, user_info, _ = await asyncio.to_thread(validate_vk_token_strong, active['token'])
    if not valid:
        await callback.message.edit_text(
            f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Активный токен невалиден: {error_msg}\nАктивируйте другой или добавьте новый.",
            parse_mode="HTML", reply_markup=back_button()
        )
        return
    try:
        vk_session = create_vk_session(active['token'])
        vk = vk_session.get_api()
        recipients, stats = await asyncio.to_thread(get_recipients_from_token, vk, None, only_friends=True)
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка загрузки друзей: {e}", reply_markup=back_button())
        return
    if not recipients:
        await callback.message.edit_text(
            "⚠️ У вас нет друзей в списке диалогов (или они запретили сообщения).",
            reply_markup=back_button()
        )
        return
    await state.update_data(vk_token=active['token'], token_name=active['name'], recipients=recipients, user_info=user_info, stats=stats)
    await callback.message.edit_text(
        "✏️ Введите текст рассылки для ваших друзей (можно использовать HTML):",
        parse_mode="HTML", reply_markup=None
    )
    await state.set_state(BotStates.waiting_newsletter_text)

@dp.message(BotStates.waiting_newsletter_text)
async def process_newsletter_text(message: Message, state: FSMContext):
    await state.update_data(newsletter_text=message.text)
    if "delay" in await state.get_data():
        await start_mailing(message, state)
    else:
        await message.answer(
            "<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Введите задержку в секундах (например, 2):",
            parse_mode="HTML", reply_markup=None
        )
        await state.set_state(BotStates.waiting_delay)

@dp.message(BotStates.waiting_delay)
async def process_delay(message: Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    await state.update_data(delay=delay)
    await start_mailing(message, state)

async def start_mailing(message: Message, state: FSMContext, callback: CallbackQuery = None):
    data = await state.get_data()
    token = data.get("vk_token")
    token_name = data.get("token_name")
    recipients = data.get("recipients")
    text = data.get("newsletter_text")
    delay = data.get("delay")
    user_info = data.get("user_info")
    stats = data.get("stats")
    if not all([token, recipients, text, user_info, stats]):
        await message.answer("❌ Ошибка данных.", reply_markup=main_menu(message.from_user.id))
        await state.clear()
        return
    progress_msg = await message.answer("<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Запуск рассылки друзьям...", parse_mode="HTML")
    if callback:
        await callback.message.delete()
    await state.clear()
    asyncio.create_task(mailing_task(token, recipients, text, delay, message.chat.id, user_info, stats, message.from_user.id, token_name, progress_msg.message_id))

# ---- Мои шаблоны ----
@dp.callback_query(lambda c: c.data == "my_templates")
async def templates_menu(callback: CallbackQuery):
    if not await check_subscription(callback.from_user.id):
        await callback.answer("<tg-emoji emoji-id='5278578973595427038'></tg-emoji> Нет активной подписки!", show_alert=True, parse_mode="HTML")
        return
    await callback.answer()
    uid = callback.from_user.id
    templates = await get_templates(uid)
    if not templates:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Создать", callback_data="create_template", style="primary")],[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")]])
        await callback.message.edit_text("📭 Нет шаблонов. Создайте первый!", reply_markup=kb)
        return
    text = "<tg-emoji emoji-id='5275979556308674886'></tg-emoji> <b>Ваши шаблоны</b>\n\n"
    for t in templates:
        text += f"🔹 <b>{t['name']}</b> — <code>{t['content'][:40]}...</code> (⏱️ задержка: {t['delay']} с)\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="create_template", style="success")],
        [InlineKeyboardButton(text="❌ Удалить", callback_data="delete_template", style="danger")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="default")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "create_template")
async def create_template(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📝 Введите <b>название</b> шаблона:", parse_mode="HTML", reply_markup=None)
    await state.set_state(BotStates.waiting_template_name)

@dp.message(BotStates.waiting_template_name)
async def process_template_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Название не может быть пустым.")
        return
    await state.update_data(template_name=name)
    await message.answer("✏️ Введите <b>текст</b> шаблона (HTML):", parse_mode="HTML", reply_markup=None)
    await state.set_state(BotStates.waiting_template_content)

@dp.message(BotStates.waiting_template_content)
async def process_template_content(message: Message, state: FSMContext):
    content = message.text
    await state.update_data(template_content=content)
    await message.answer("⏱️ Введите <b>задержку</b> (сек):", parse_mode="HTML", reply_markup=None)
    await state.set_state(BotStates.waiting_template_delay)

@dp.message(BotStates.waiting_template_delay)
async def process_template_delay(message: Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    name = data.get("template_name")
    content = data.get("template_content")
    if not name or not content:
        await state.clear()
        await message.answer("❌ Ошибка, начните заново.")
        return
    await save_template(message.from_user.id, name, content, delay)
    await message.answer(f"✅ Шаблон <b>{name}</b> сохранён!", parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(lambda c: c.data == "delete_template")
async def delete_template_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    templates = await get_templates(uid)
    if not templates:
        await callback.answer("Нет шаблонов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in templates:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"❌ {t['name']}", callback_data=f"del_tpl_{t['id']}", style="danger")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="my_templates", style="default")])
    await callback.message.edit_text("🗑️ Выберите шаблон для удаления:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("del_tpl_"))
async def confirm_delete_template(callback: CallbackQuery):
    tpl_id = int(callback.data.split("_")[2])
    await delete_template(tpl_id, callback.from_user.id)
    await callback.answer("✅ Шаблон удалён", show_alert=True)
    await callback.message.edit_text("✅ Шаблон удалён.", reply_markup=back_button("my_templates"))

# ---- Профиль и статистика (не требуют подписки) ----
@dp.callback_query(lambda c: c.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    user = await get_user(uid)
    if not user:
        await callback.message.edit_text("❌ Ошибка", reply_markup=back_button())
        return
    sub = user.get('subscription_until')
    sub_text = sub.strftime('%d.%m.%Y %H:%M') if sub else "Нет"
    if uid in ADMIN_IDS:
        sub_text = "🔹 Вечная"
    text = (
        f"<tg-emoji emoji-id='5275979556308674886'></tg-emoji> <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 Имя: {user.get('first_name') or 'Не указано'}\n"
        f"🔖 Username: @{user.get('username') or 'Нет'}\n"
        f"📅 Регистрация: {user.get('joined_at').strftime('%d.%m.%Y') if user.get('joined_at') else 'Неизвестно'}\n"
        f"⏳ Подписка до: {sub_text}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())

@dp.callback_query(lambda c: c.data == "my_stats")
async def user_stats(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    stats = await get_user_mailing_stats(uid, 10)
    if not stats:
        await callback.message.edit_text("📭 Нет завершённых рассылок.", reply_markup=back_button())
        return
    text = "<tg-emoji emoji-id='5278753302023004775'></tg-emoji> <b>Последние рассылки</b>\n\n"
    for s in stats:
        text += f"🗓 {s['started_at'].strftime('%Y-%m-%d %H:%M')}\n👤 VK: {s['vk_account_name']}\n📊 Всего: {s['total_recipients']} | <tg-emoji emoji-id='5206401524200145033'></tg-emoji>{s['sent_success']} <tg-emoji emoji-id='5206510891247371052'></tg-emoji>{s['sent_error']}\n\n"
    await callback.message.edit_text(text[:4000], parse_mode="HTML", reply_markup=back_button())

# ---- Админ-панель ----
@dp.callback_query(lambda c: c.data == "admin_panel" and c.from_user.id in ADMIN_IDS)
async def admin_panel(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton(text="📨 Рассылка пользователям", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="admin_give_sub", style="success")],
        [InlineKeyboardButton(text="📈 Статистика проливов", callback_data="admin_mailing_stats", style="default")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main", style="danger")]
    ])
    await callback.message.edit_text("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("admin_"))
async def admin_callback(callback: CallbackQuery, state: FSMContext):
    data = callback.data
    await callback.answer()
    if data == "admin_stats":
        stats = await get_bot_stats()
        await callback.message.edit_text(
            f"📊 Статистика бота\n👥 Пользователей: {stats['users']} | 🚀 Проливов: {stats['mailings']} | ✉️ Отправлено: {stats['sent']} | ❌ Ошибок: {stats['errors']}",
            reply_markup=back_button("admin_panel")
        )
    elif data == "admin_broadcast":
        await callback.message.edit_text("✏️ Введите текст для рассылки всем:", reply_markup=None)
        await state.set_state(BotStates.admin_waiting_broadcast)
    elif data == "admin_give_sub":
        await callback.message.edit_text("🔢 Введите Telegram ID:", reply_markup=None)
        await state.set_state(BotStates.admin_waiting_user_id)
    elif data == "admin_mailing_stats":
        mailings = await get_mailing_stats(20)
        if not mailings:
            await callback.message.edit_text("📭 Пусто", reply_markup=back_button("admin_panel"))
            return
        text = "📈 Последние 20 проливов:\n"
        for m in mailings:
            text += f"🆔 ID {m['id']} | {m['started_at'].strftime('%Y-%m-%d %H:%M')} | {m['vk_account_name']} | Всего: {m['total_recipients']} | ✅{m['sent_success']} ❌{m['sent_error']}\n"
        await callback.message.edit_text(text[:4000], reply_markup=back_button("admin_panel"))

@dp.message(BotStates.admin_waiting_broadcast)
async def admin_broadcast_process(message: Message, state: FSMContext):
    text = message.text
    users = await get_all_users()
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 <b>Анонс</b>\n\n{text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Отправлено {sent}/{len(users)}", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.message(BotStates.admin_waiting_user_id)
async def admin_get_user_id(message: Message, state: FSMContext):
    try:
        target = int(message.text.strip())
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    await state.update_data(target_id=target)
    await message.answer("📆 Введите количество дней:", reply_markup=None)
    await state.set_state(BotStates.admin_waiting_days)

@dp.message(BotStates.admin_waiting_days)
async def admin_give_days(message: Message, state: FSMContext):
    try:
        days = float(message.text.replace(",", "."))
        if days <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    target = data.get("target_id")
    await set_subscription(target, days)
    await message.answer(f"✅ Пользователю {target} выдана подписка на {days} дн.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu(callback.from_user.id))

# ---------------------- Запуск ----------------------
async def on_startup():
    await init_db()
    logger.info("✅ Бот запущен")
    if not ANTICAPTCHA_KEY:
        logger.warning("⚠️ Нет ключа 2captcha")
    if not CRYPTOBOT_TOKEN:
        logger.warning("⚠️ Нет ключа CryptoBot")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())