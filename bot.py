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
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from vk_api import VkApi
from vk_api.exceptions import ApiError
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# Импорт асинхронных функций БД
from database import (
    init_db, add_user, get_user, get_user_subscription, set_subscription,
    revoke_subscription, save_vk_token, get_vk_token, get_all_users,
    get_bot_stats, save_mailing_stats, get_mailing_stats, get_user_mailing_stats,
    save_template, get_templates, delete_template, get_template_by_id,
    create_invoice_db, get_pending_invoice, mark_invoice_paid
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = []
for x in os.getenv("ADMIN_IDS", "").split(","):
    x = x.strip().lstrip('@')
    if x.isdigit():
        ADMIN_IDS.append(int(x))
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

# Отключаем проверку SSL для Windows
class SSLDisabledHTTPAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = ssl._create_unverified_context()
        return super().init_poolmanager(*args, **kwargs)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# ===================== ПЛАНЫ ПОДПИСКИ =====================
SUBSCRIPTION_PLANS = {3: 1.0, 7: 2.0, 30: 5.0, 90: 12.0}

# ===================== ФУНКЦИИ VK =====================
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

def get_vk_user_info(vk_token: str) -> Dict[str, Any]:
    vk_session = create_vk_session(vk_token)
    vk = vk_session.get_api()
    try:
        info = vk.account.getProfileInfo()
        try:
            phone = vk.account.getPhone().get('phone', 'не указан')
        except:
            phone = 'нет доступа'
        return {
            'first_name': info.get('first_name', ''),
            'last_name': info.get('last_name', ''),
            'phone': phone,
            'id': info.get('id', 0)
        }
    except:
        return {'first_name': 'Неизвестно', 'last_name': '', 'phone': 'ошибка', 'id': 0}

def get_recipients(vk_token: str, group_filter: str = None) -> Tuple[List[Dict], Dict]:
    vk_session = create_vk_session(vk_token)
    vk = vk_session.get_api()
    recipients = []
    dialogues_count = 0
    contacts_count = 0
    offset = 0
    count = 200
    while True:
        try:
            convs = vk.messages.getConversations(offset=offset, count=count, filter="all")
        except ApiError as e:
            if e.code == 14:
                logger.warning("Капча при загрузке диалогов, решаем...")
                captcha_key = solve_captcha(e.captcha_img)
                convs = vk.messages.getConversations(offset=offset, count=count, filter="all",
                                                     captcha_sid=e.captcha_sid, captcha_key=captcha_key)
            else:
                logger.error(f"Ошибка VK: {e}")
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
            title = None
            if peer_type != "user":
                title = conversation.get("chat_settings", {}).get("title", "")
                if group_filter and group_filter.lower() not in title.lower():
                    continue
            if peer_type == "user":
                contacts_count += 1
            else:
                dialogues_count += 1
            if peer_type == "user":
                try:
                    user_info = vk.users.get(user_ids=peer_id, fields="first_name,last_name")
                    name = f"{user_info[0]['first_name']} {user_info[0]['last_name']}" if user_info else str(peer_id)
                except:
                    name = f"User {peer_id}"
            else:
                name = title if title else f"Беседа {peer_id}"
            recipients.append({"peer_id": peer_id, "name": name, "type": peer_type})
        offset += count
        if len(items) < count:
            break
    stats = {'dialogues': dialogues_count, 'contacts': contacts_count, 'total': len(recipients)}
    return recipients, stats

def send_vk_message(vk, peer_id: int, text: str) -> bool:
    text = obfuscate_links(text)
    random_id = os.urandom(8).hex()
    while True:
        try:
            vk.messages.send(peer_id=peer_id, message=text, random_id=random_id)
            return True
        except ApiError as e:
            if e.code == 14:
                logger.warning(f"Капча при отправке, решаем...")
                captcha_key = solve_captcha(e.captcha_img)
                try:
                    vk.messages.send(peer_id=peer_id, message=text, random_id=random_id,
                                     captcha_sid=e.captcha_sid, captcha_key=captcha_key)
                    return True
                except ApiError as e2:
                    logger.error(f"Не удалось отправить после капчи: {e2}")
                    return False
            elif e.code in (901, 913, 917):
                logger.warning(f"Ошибка {e.code}, пропускаем")
                return False
            else:
                logger.error(f"Ошибка {e.code}: {e}")
                return False
        except Exception as e:
            logger.exception(f"Критическая ошибка: {e}")
            return False

def make_progress_bar(percent: float, length: int = 10) -> str:
    filled = int(length * percent / 100)
    return "🤖" * filled + "⚙️" * (length - filled)

# ===================== CRYPTOBOT INVOICE =====================
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
        else:
            logger.error(f"CryptoBot error: {data}")
            return None
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
        if data.get("ok") and data["result"]["items"]:
            status = data["result"]["items"][0]["status"]
            if status == "paid":
                await mark_invoice_paid(invoice_id, days)
                current_sub = await get_user_subscription(user_id)
                if current_sub and current_sub > datetime.now():
                    new_until = current_sub + timedelta(days=days)
                else:
                    new_until = datetime.now() + timedelta(days=days)
                await set_subscription(user_id, (new_until - datetime.now()).days)
                return True
        return False
    except:
        return False

# ===================== ЗАДАЧА РАССЫЛКИ =====================
async def mailing_task(vk_token: str, recipients: List[Dict], text: str, delay: float,
                       chat_id: int, user_info: Dict, stats: Dict, user_telegram_id: int):
    vk_session = create_vk_session(vk_token)
    vk = vk_session.get_api()
    total = len(recipients)
    sent_ok = 0
    sent_err = 0
    start_time = time.time()
    last_update = start_time
    progress_msg = await bot.send_message(chat_id, "⏳ Запуск рассылки...")
    for idx, rec in enumerate(recipients, 1):
        success = await asyncio.to_thread(send_vk_message, vk, rec["peer_id"], text)
        if success:
            sent_ok += 1
        else:
            sent_err += 1
        now = time.time()
        if now - last_update >= UPDATE_INTERVAL or idx == total:
            percent = (idx / total) * 100
            elapsed = now - start_time
            remaining = ((total - idx) / (idx / elapsed)) if idx > 0 else 0
            bar = make_progress_bar(percent)
            status = (
                f"📲 Аккаунт VK загружен!\n"
                f"👤 {user_info.get('first_name')} {user_info.get('last_name')}\n"
                f"🤙 Телефон: {user_info.get('phone')}\n"
                f"📂 Всего чатов: {stats['total']} (диалогов: {stats['dialogues']}, личных: {stats['contacts']})\n"
                f"🔄 Прогресс — {percent:.1f}%\n"
                f"{bar}\n"
                f"⏲️ Осталось {remaining:.1f} с\n"
                f"📊 {idx}/{total} | ✅{sent_ok} ❌{sent_err}"
            )
            try:
                await bot.edit_message_text(status, chat_id, progress_msg.message_id)
            except:
                pass
            last_update = now
        await asyncio.sleep(max(delay, RATE_LIMIT_SECONDS))
    total_time = time.time() - start_time
    success_rate = (sent_ok / total * 100) if total > 0 else 0
    final_text = (
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📝 Отправлено: {sent_ok}/{total}\n"
        f"   ┣ ✅ Успешно: {sent_ok}\n"
        f"   ┗ ❌ Ошибки: {sent_err}\n"
        f"{'✅' if success_rate >= 70 else '⚠️'} Успешность: {success_rate:.1f}%\n"
        f"⏲️ Время: {total_time:.1f} сек.\n\n"
        f"👤 Аккаунт: {user_info.get('first_name')} {user_info.get('last_name')}\n"
        f"📂 Всего чатов: {stats['total']} (бесед: {stats['dialogues']}, личных: {stats['contacts']})"
    )
    await bot.send_message(chat_id, final_text, parse_mode="HTML")
    await bot.delete_message(chat_id, progress_msg.message_id)
    vk_name = f"{user_info.get('first_name')} {user_info.get('last_name')}".strip()
    await save_mailing_stats(user_telegram_id, total, sent_ok, sent_err, total_time, text[:200], vk_name)

# ===================== СОСТОЯНИЯ FSM =====================
class BotStates(StatesGroup):
    waiting_vk_token = State()
    waiting_group_name = State()
    waiting_newsletter_text = State()
    waiting_delay = State()
    waiting_template_name = State()
    waiting_template_content = State()
    waiting_template_delay = State()
    admin_waiting_broadcast = State()
    admin_waiting_user_id = State()
    admin_waiting_days = State()

# ===================== ПРОВЕРКА ПОДПИСКИ =====================
async def check_subscription(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    sub = await get_user_subscription(user_id)
    if not sub or sub <= datetime.now():
        return False
    return True

async def check_channel(user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ('member', 'administrator', 'creator')
    except:
        return False

def main_keyboard(uid):
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📨 Начать рассылку"))
    kb.add(KeyboardButton("📝 Мои шаблоны"))
    kb.add(KeyboardButton("👤 Мой профиль"))
    kb.add(KeyboardButton("📊 Моя статистика"))
    kb.add(KeyboardButton("💰 Купить подписку"))
    kb.add(KeyboardButton("🔑 Ввести токен VK"))
    if uid in ADMIN_IDS:
        kb.add(KeyboardButton("👑 Админ-панель"))
    return kb

# ===================== ОБРАБОТЧИКИ =====================
@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: types.Message, state: FSMContext):
    await state.finish()
    uid = message.from_user.id
    await add_user(uid, message.from_user.username, message.from_user.first_name)
    if not await check_channel(uid):
        await message.answer(f"❌ Подпишитесь на канал {REQUIRED_CHANNEL} и нажмите /start снова.")
        return
    welcome_text = (
        "🌟 <b>Добро пожаловать в VK Рассыльщик!</b> 🌟\n\n"
        "🤖 Я помогу вам отправлять сообщения в личные диалоги и беседы ВКонтакте.\n\n"
        "📌 <b>Что нужно сделать:</b>\n"
        "1️⃣ Пополнить баланс в USDT через кнопку <b>💰 Купить подписку</b> (или обратиться к администратору)\n"
        "2️⃣ Ввести токен ВК через кнопку <b>🔑 Ввести токен VK</b>\n"
        "3️⃣ Нажать <b>📨 Начать рассылку</b>\n\n"
        "💡 Также доступны шаблоны сообщений, статистика и защита от спама.\n"
        "⚡️ Для получения токена VK используйте: https://vkhost.github.io\n\n"
        "🔹 <b>Админ:</b> @admin_username"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_keyboard(uid))
    token = await get_vk_token(uid)
    if token:
        await message.answer("✅ Ваш токен VK уже сохранён. Можете сразу начинать рассылку.", reply_markup=main_keyboard(uid))

@dp.message_handler(lambda msg: msg.text == "🔑 Ввести токен VK", state='*')
async def enter_vk_token(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("🔑 Отправьте <b>токен доступа VK</b> (сообщества или пользователя).\n\n"
                         "Токен должен иметь право на отправку сообщений (scope: messages).\n"
                         "Получить токен можно здесь: https://vkhost.github.io", parse_mode="HTML")
    await BotStates.waiting_vk_token.set()

@dp.message_handler(state=BotStates.waiting_vk_token)
async def process_vk_token(message: types.Message, state: FSMContext):
    token = message.text.strip()
    if not token:
        await message.answer("❌ Токен не может быть пустым.")
        return
    msg = await message.answer("🔄 Проверяю токен и загружаю диалоги...")
    try:
        user_info = await asyncio.to_thread(get_vk_user_info, token)
        recipients, stats = await asyncio.to_thread(get_recipients, token)
    except Exception as e:
        await msg.delete()
        await message.answer(f"❌ Ошибка: {e}\nПроверьте токен или права доступа.")
        return
    if not recipients:
        await msg.delete()
        await message.answer("⚠️ Нет доступных диалогов (нет прав или список пуст).")
        return
    await save_vk_token(message.from_user.id, token)
    async with state.proxy() as data:
        data["vk_token"] = token
        data["recipients"] = recipients
        data["user_info"] = user_info
        data["stats"] = stats
    await msg.delete()
    await message.answer(
        f"✅ <b>Аккаунт VK:</b> {user_info.get('first_name')} {user_info.get('last_name')}\n"
        f"📊 <b>Найдено получателей:</b> {len(recipients)} (бесед: {stats['dialogues']}, личных: {stats['contacts']})\n\n"
        f"Теперь можно начинать рассылку.",
        parse_mode="HTML"
    )
    await state.finish()

@dp.message_handler(lambda msg: msg.text == "💰 Купить подписку", state='*')
async def buy_subscription_menu(message: types.Message, state: FSMContext):
    await state.finish()
    if not CRYPTOBOT_TOKEN:
        await message.answer("⚠️ Оплата через CryptoBot недоступна. Обратитесь к администратору.")
        return
    kb = InlineKeyboardMarkup(row_width=2)
    for days, price in SUBSCRIPTION_PLANS.items():
        kb.add(InlineKeyboardButton(f"{days} дней - {price} USDT", callback_data=f"sub_{days}_{price}"))
    await message.answer("💰 <b>Выберите срок подписки:</b>\n\nПосле оплаты подписка активируется автоматически.", parse_mode="HTML", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("sub_"))
async def process_subscription_choice(callback: types.CallbackQuery):
    _, days_str, price_str = callback.data.split("_")
    days = int(days_str)
    price = float(price_str)
    pay_url = await create_crypto_invoice(callback.from_user.id, days, price)
    if not pay_url:
        await callback.answer("❌ Ошибка создания счёта. Попробуйте позже.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💸 Оплатить", url=pay_url))
    kb.add(InlineKeyboardButton("🔄 Проверить оплату", callback_data="check_payment"))
    await callback.message.edit_text(
        f"💰 <b>Оплата подписки на {days} дней</b>\n\n"
        f"Сумма: <b>{price} USDT</b>\n"
        f"После оплаты нажмите «Проверить оплату».\n"
        f"Счёт действителен 1 час.",
        parse_mode="HTML", reply_markup=kb
    )
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "check_payment")
async def check_payment_callback(callback: types.CallbackQuery):
    paid = await check_payment(callback.from_user.id)
    if paid:
        await callback.answer("✅ Оплата получена! Подписка активирована.", show_alert=True)
        await callback.message.edit_text("✅ Подписка успешно активирована!")
    else:
        await callback.answer("⏳ Оплата ещё не получена. Попробуйте позже.", show_alert=True)

@dp.message_handler(lambda msg: msg.text == "📨 Начать рассылку", state='*')
async def start_newsletter_button(message: types.Message, state: FSMContext):
    await state.finish()
    if not await check_subscription(message.from_user.id):
        await message.answer("❌ У вас нет активной подписки. Купите её через кнопку <b>💰 Купить подписку</b>.", parse_mode="HTML")
        return
    token = await get_vk_token(message.from_user.id)
    if not token:
        await message.answer("❌ Сначала введите токен VK через кнопку <b>🔑 Ввести токен VK</b>.", parse_mode="HTML")
        return
    await message.answer("📌 Если хотите отправить только в беседы с определённым названием, введите его.\n"
                         "Если нужно отправить во все диалоги, просто напишите <b>пропустить</b> или нажмите /skip.",
                         parse_mode="HTML")
    await BotStates.waiting_group_name.set()
    async with state.proxy() as data:
        data["vk_token"] = token

@dp.message_handler(state=BotStates.waiting_group_name, commands=['skip'])
async def skip_group_filter(message: types.Message, state: FSMContext):
    await state.update_data(group_filter=None)
    await proceed_to_load_recipients(message, state)

@dp.message_handler(state=BotStates.waiting_group_name)
async def process_group_filter(message: types.Message, state: FSMContext):
    group_name = message.text.strip()
    if group_name.lower() == "пропустить":
        group_name = None
    await state.update_data(group_filter=group_name)
    await proceed_to_load_recipients(message, state)

async def proceed_to_load_recipients(message: types.Message, state: FSMContext):
    data = await state.get_data()
    token = data.get("vk_token")
    group_filter = data.get("group_filter")
    await message.answer("🔄 Загружаю диалоги...")
    try:
        user_info = await asyncio.to_thread(get_vk_user_info, token)
        recipients, stats = await asyncio.to_thread(get_recipients, token, group_filter)
    except Exception as e:
        await message.answer(f"❌ Ошибка загрузки диалогов: {e}")
        return
    if not recipients:
        await message.answer("⚠️ Нет диалогов, соответствующих критериям. Попробуйте изменить фильтр.")
        return
    async with state.proxy() as sd:
        sd["recipients"] = recipients
        sd["user_info"] = user_info
        sd["stats"] = stats
    templates = await get_templates(message.from_user.id)
    if templates:
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("📝 Выбрать шаблон", callback_data="use_template"))
        kb.add(InlineKeyboardButton("✏️ Ввести текст вручную", callback_data="manual_text"))
        await message.answer("Выберите способ ввода текста:", reply_markup=kb)
    else:
        await message.answer("✏️ Введите текст сообщения для рассылки (можно использовать HTML):")
        await BotStates.waiting_newsletter_text.set()

@dp.callback_query_handler(lambda c: c.data == "use_template")
async def use_template_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    templates = await get_templates(uid)
    if not templates:
        await callback.message.answer("У вас нет шаблонов. Сначала создайте их через раздел «Мои шаблоны».")
        await BotStates.waiting_newsletter_text.set()
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for tpl in templates:
        kb.add(InlineKeyboardButton(f"{tpl['name']} (задержка {tpl['delay']} с)", callback_data=f"tpl_{tpl['id']}"))
    await callback.message.answer("Выберите шаблон:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("tpl_"))
async def apply_template_callback(callback: types.CallbackQuery, state: FSMContext):
    tpl_id = int(callback.data.split("_")[1])
    uid = callback.from_user.id
    tpl = await get_template_by_id(tpl_id, uid)
    if not tpl:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    await callback.answer()
    async with state.proxy() as data:
        data["newsletter_text"] = tpl["content"]
        data["delay"] = tpl["delay"]
    await start_mailing_with_data(callback.message, state)

@dp.callback_query_handler(lambda c: c.data == "manual_text")
async def manual_text_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("✏️ Введите текст сообщения для рассылки (можно использовать HTML):")
    await BotStates.waiting_newsletter_text.set()

@dp.message_handler(state=BotStates.waiting_newsletter_text)
async def process_newsletter_text(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data["newsletter_text"] = message.text
    if "delay" in await state.get_data():
        await start_mailing_with_data(message, state)
    else:
        await message.answer("⏱ Введите задержку в секундах (например, 3):")
        await BotStates.waiting_delay.set()

@dp.message_handler(state=BotStates.waiting_delay)
async def process_delay(message: types.Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число секунд.")
        return
    async with state.proxy() as data:
        data["delay"] = delay
    await start_mailing_with_data(message, state)

async def start_mailing_with_data(message: types.Message, state: FSMContext):
    data = await state.get_data()
    token = data.get("vk_token")
    recipients = data.get("recipients")
    text = data.get("newsletter_text")
    delay = data.get("delay")
    user_info = data.get("user_info")
    stats = data.get("stats")
    if not all([token, recipients, text, user_info, stats]):
        await message.answer("❌ Ошибка данных. Начните заново через кнопки меню.")
        await state.finish()
        return
    asyncio.create_task(mailing_task(token, recipients, text, delay, message.chat.id, user_info, stats, message.from_user.id))
    await message.answer("🚀 Рассылка запущена! Прогресс будет здесь.")
    await state.finish()

@dp.message_handler(lambda msg: msg.text == "📝 Мои шаблоны", state='*')
async def templates_menu(message: types.Message, state: FSMContext):
    await state.finish()
    uid = message.from_user.id
    templates = await get_templates(uid)
    if not templates:
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("➕ Создать шаблон", callback_data="create_template"))
        await message.answer("📭 У вас пока нет шаблонов. Создайте первый!", reply_markup=kb)
        return
    text = "📋 <b>Ваши шаблоны:</b>\n\n"
    for t in templates:
        text += f"🔹 <b>{t['name']}</b> - <code>{t['content'][:50]}...</code> (задержка: {t['delay']} с)\n"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("➕ Создать", callback_data="create_template"))
    kb.add(InlineKeyboardButton("❌ Удалить", callback_data="delete_template"))
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "create_template")
async def create_template_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("Введите <b>название</b> шаблона:", parse_mode="HTML")
    await BotStates.waiting_template_name.set()

@dp.message_handler(state=BotStates.waiting_template_name)
async def process_template_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(template_name=name)
    await message.answer("Теперь введите <b>текст</b> шаблона (можно использовать HTML):", parse_mode="HTML")
    await BotStates.waiting_template_content.set()

@dp.message_handler(state=BotStates.waiting_template_content)
async def process_template_content(message: types.Message, state: FSMContext):
    content = message.text
    await state.update_data(template_content=content)
    await message.answer("Введите <b>задержку</b> для этого шаблона (в секундах, например 3):", parse_mode="HTML")
    await BotStates.waiting_template_delay.set()

@dp.message_handler(state=BotStates.waiting_template_delay)
async def process_template_delay(message: types.Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число секунд.")
        return
    data = await state.get_data()
    name = data.get("template_name")
    content = data.get("template_content")
    if not name or not content:
        await state.finish()
        await message.answer("Ошибка, начните заново.")
        return
    await save_template(message.from_user.id, name, content, delay)
    await message.answer(f"✅ Шаблон <b>{name}</b> сохранён с задержкой {delay} с!", parse_mode="HTML")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "delete_template")
async def delete_template_callback(callback: types.CallbackQuery):
    uid = callback.from_user.id
    templates = await get_templates(uid)
    if not templates:
        await callback.answer("Нет шаблонов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for t in templates:
        kb.add(InlineKeyboardButton(f"❌ {t['name']}", callback_data=f"del_tpl_{t['id']}"))
    await callback.message.answer("Выберите шаблон для удаления:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("del_tpl_"))
async def confirm_delete_template(callback: types.CallbackQuery):
    tpl_id = int(callback.data.split("_")[2])
    await delete_template(tpl_id, callback.from_user.id)
    await callback.answer("Шаблон удалён", show_alert=True)
    await callback.message.delete()

@dp.message_handler(lambda msg: msg.text == "👤 Мой профиль", state='*')
async def show_profile(message: types.Message, state: FSMContext):
    await state.finish()
    uid = message.from_user.id
    user = await get_user(uid)
    if not user:
        await message.answer("Ошибка: не найден в базе.")
        return
    sub = user.get('subscription_until')
    sub_text = sub.strftime('%d.%m.%Y %H:%M') if sub else "Нет"
    if uid in ADMIN_IDS:
        sub_text = "🔹 Вечная (админ)"
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 Имя: {user.get('first_name') or 'Не указано'}\n"
        f"🔖 Username: @{user.get('username') or 'Нет'}\n"
        f"📅 Регистрация: {user.get('joined_at').strftime('%d.%m.%Y %H:%M') if user.get('joined_at') else 'Неизвестно'}\n"
        f"⏳ Подписка до: {sub_text}\n\n"
        f"Сменить токен можно через кнопку «🔑 Ввести токен VK»"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message_handler(lambda msg: msg.text == "📊 Моя статистика", state='*')
async def user_stats(message: types.Message, state: FSMContext):
    await state.finish()
    uid = message.from_user.id
    stats = await get_user_mailing_stats(uid, 10)
    if not stats:
        await message.answer("У вас ещё нет завершённых рассылок.")
        return
    text = "📈 <b>Ваши последние 10 рассылок:</b>\n\n"
    for s in stats:
        text += f"🗓 {s['started_at'].strftime('%Y-%m-%d %H:%M')}\n"
        text += f"👤 VK: {s['vk_account_name']}\n"
        text += f"📊 Всего: {s['total_recipients']} | ✅{s['sent_success']} ❌{s['sent_error']} | ⏱{s['total_time_seconds']:.1f}с\n\n"
    await message.answer(text[:4000], parse_mode="HTML")

@dp.message_handler(lambda msg: msg.text == "👑 Админ-панель" and msg.from_user.id in ADMIN_IDS, state='*')
async def admin_panel(message: types.Message, state: FSMContext):
    await state.finish()
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Статистика бота", callback_data="admin_stats"),
        InlineKeyboardButton("📨 Рассылка пользователям", callback_data="admin_broadcast"),
        InlineKeyboardButton("🎁 Выдать подписку", callback_data="admin_give_sub"),
        InlineKeyboardButton("📈 Статистика проливов", callback_data="admin_mailing_stats"),
        InlineKeyboardButton("🔙 Закрыть", callback_data="admin_close")
    )
    await message.answer("👑 <b>Админ-панель</b>\nВыберите действие:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data.startswith("admin_"))
async def admin_callback(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data
    await callback.answer()
    if data == "admin_stats":
        stats = await get_bot_stats()
        await callback.message.answer(
            f"📊 <b>Статистика бота</b>\n👥 Пользователей: {stats['users']}\n🚀 Проливов: {stats['mailings']}\n✉️ Отправлено: {stats['sent']}\n❌ Ошибок: {stats['errors']}",
            parse_mode="HTML"
        )
    elif data == "admin_broadcast":
        await callback.message.answer("✏️ Введите текст для рассылки ВСЕМ пользователям бота:")
        await BotStates.admin_waiting_broadcast.set()
    elif data == "admin_give_sub":
        await callback.message.answer("Введите Telegram ID пользователя (число):")
        await BotStates.admin_waiting_user_id.set()
    elif data == "admin_mailing_stats":
        mailings = await get_mailing_stats(20)
        if not mailings:
            await callback.message.answer("📭 История проливов пуста.")
            return
        text = "📈 <b>Последние 20 проливов (всех пользователей):</b>\n"
        for m in mailings:
            text += f"ID {m['id']} | {m['started_at'].strftime('%Y-%m-%d %H:%M')} | {m['vk_account_name']} | Всего: {m['total_recipients']} | ✅{m['sent_success']} ❌{m['sent_error']} | ⏱️{m['total_time_seconds']:.1f}с\n"
        await callback.message.answer(text[:4000], parse_mode="HTML")
    elif data == "admin_close":
        await callback.message.delete()

@dp.message_handler(state=BotStates.admin_waiting_broadcast)
async def admin_broadcast_process(message: types.Message, state: FSMContext):
    text = message.text
    users = await get_all_users()
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 <b>Анонс от администратора</b>\n\n{text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Рассылка завершена. Отправлено {sent} из {len(users)} пользователей.")
    await state.finish()

@dp.message_handler(state=BotStates.admin_waiting_user_id)
async def admin_get_user_id(message: types.Message, state: FSMContext):
    try:
        target = int(message.text.strip())
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    await state.update_data(target_id=target)
    await message.answer("Введите количество дней (можно дробное, например 0.5 для 12 часов):")
    await BotStates.admin_waiting_days.set()

@dp.message_handler(state=BotStates.admin_waiting_days)
async def admin_give_days(message: types.Message, state: FSMContext):
    try:
        days = float(message.text.replace(",", "."))
        if days <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число (дни).")
        return
    data = await state.get_data()
    target = data.get("target_id")
    await set_subscription(target, days)
    await message.answer(f"✅ Пользователю {target} выдана подписка на {days} дней.")
    try:
        await bot.send_message(target, f"🎉 Администратор выдал вам подписку на {days} дней! Теперь вы можете использовать бота.")
    except:
        pass
    await state.finish()

@dp.message_handler(commands=['revoke_sub'])
async def cmd_revoke_sub(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /revoke_sub <user_id>")
        return
    try:
        uid = int(parts[1])
    except:
        await message.answer("ID должен быть числом.")
        return
    await revoke_subscription(uid)
    await message.answer(f"Подписка пользователя {uid} отозвана.")
    try:
        await bot.send_message(uid, "⚠️ Ваша подписка была отозвана администратором.")
    except:
        pass

# ===================== ЗАПУСК =====================
async def on_startup():
    await init_db()
    logger.info("База данных инициализирована")
    if not ANTICAPTCHA_KEY:
        logger.warning("⚠️ ANTICAPTCHA_KEY не задан. При капче рассылка остановится!")
    if not CRYPTOBOT_TOKEN:
        logger.warning("⚠️ CRYPTOBOT_TOKEN не задан. Покупка подписки через криптобот недоступна.")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(on_startup())
    from aiogram import executor
    executor.start_polling(dp, skip_updates=True)