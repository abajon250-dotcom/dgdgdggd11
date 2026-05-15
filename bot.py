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
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery
)
from vk_api import VkApi
from vk_api.exceptions import ApiError
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

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

class SSLDisabledHTTPAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = ssl._create_unverified_context()
        return super().init_poolmanager(*args, **kwargs)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

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
                logger.warning(f"🛡️ Капча при отправке, решаем...")
                captcha_key = solve_captcha(e.captcha_img)
                try:
                    vk.messages.send(peer_id=peer_id, message=text, random_id=random_id,
                                     captcha_sid=e.captcha_sid, captcha_key=captcha_key)
                    return True
                except ApiError as e2:
                    logger.error(f"❌ Не удалось отправить после капчи: {e2}")
                    return False
            elif e.code in (901, 913, 917):
                logger.warning(f"⚠️ Ошибка {e.code}, пропускаем получателя {peer_id}")
                return False
            else:
                logger.error(f"❌ Ошибка {e.code}: {e}")
                return False
        except Exception as e:
            logger.exception(f"🔥 Критическая ошибка: {e}")
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
                f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Аккаунт VK загружен!\n"
                f"👤 {user_info.get('first_name')} {user_info.get('last_name')}\n"
                f"🤙 Телефон: {user_info.get('phone')}\n"
                f"📂 Всего чатов: {stats['total']} (диалогов: {stats['dialogues']}, личных: {stats['contacts']})\n"
                f"<tg-emoji emoji-id='5382194935057372936'></tg-emoji> Прогресс — {percent:.1f}%\n"
                f"{bar}\n"
                f"⏲️ Осталось {remaining:.1f} с\n"
                f"📊 {idx}/{total} | <tg-emoji emoji-id='5206401524200145033'></tg-emoji>{sent_ok} <tg-emoji emoji-id='5206510891247371052'></tg-emoji>{sent_err}"
            )
            try:
                await bot.edit_message_text(status, chat_id, progress_msg.message_id, parse_mode="HTML")
            except:
                pass
            last_update = now
        await asyncio.sleep(max(delay, RATE_LIMIT_SECONDS))
    total_time = time.time() - start_time
    success_rate = (sent_ok / total * 100) if total > 0 else 0
    final_text = (
        f"<tg-emoji emoji-id='5206401524200145033'></tg-emoji> <b>Рассылка завершена!</b>\n\n"
        f"📝 Отправлено: {sent_ok}/{total}\n"
        f"   ┣ <tg-emoji emoji-id='5206401524200145033'></tg-emoji> Успешно: {sent_ok}\n"
        f"   ┗ <tg-emoji emoji-id='5206510891247371052'></tg-emoji> Ошибки: {sent_err}\n"
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

# ---------------------- Reply клавиатура (главное меню) ----------------------
def get_main_keyboard(uid: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📨 Начать рассылку"), KeyboardButton(text="📝 Мои шаблоны")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="📊 Моя статистика")],
        [KeyboardButton(text="💰 Купить подписку"), KeyboardButton(text="🔑 Ввести токен VK")]
    ]
    if uid in ADMIN_IDS:
        buttons.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ===================== ОБРАБОТЧИКИ =====================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await add_user(uid, message.from_user.username, message.from_user.first_name)
    if not await check_channel(uid):
        await message.answer(f"❌ Подпишитесь на канал {REQUIRED_CHANNEL} и нажмите /start снова.")
        return
    welcome_text = (
        "<tg-emoji emoji-id='5278611606756942667'></tg-emoji> <b>Добро пожаловать в VK Рассыльщик!</b>\n\n"
        "🤖 Я помогу вам отправлять сообщения в личные диалоги и беседы ВКонтакте.\n\n"
        "📌 <b>Что нужно сделать:</b>\n"
        "1️⃣ Пополнить баланс через кнопку <b>💰 Купить подписку</b>\n"
        "2️⃣ Ввести токен ВК через кнопку <b>🔑 Ввести токен VK</b>\n"
        "3️⃣ Нажать <b>📨 Начать рассылку</b>\n\n"
        "💡 Также доступны шаблоны сообщений, статистика.\n"
        "⚡️ Для получения токена VK используйте: https://vkhost.github.io\n\n"
        "🔹 <b>Админ:</b> @admin_username"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard(uid))
    token = await get_vk_token(uid)
    if token:
        await message.answer("✅ Ваш токен VK уже сохранён. Можете сразу начинать рассылку.", reply_markup=get_main_keyboard(uid))

@dp.message(lambda msg: msg.text == "🔑 Ввести токен VK")
async def enter_vk_token(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔑 Отправьте <b>токен доступа VK</b> (сообщества или пользователя).\n\n"
                         "Токен должен иметь право на отправку сообщений (scope: messages).\n"
                         "Получить токен можно здесь: https://vkhost.github.io", parse_mode="HTML")
    await state.set_state(BotStates.waiting_vk_token)

@dp.message(BotStates.waiting_vk_token)
async def process_vk_token(message: Message, state: FSMContext):
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
        await message.answer(f"<tg-emoji emoji-id='5276240711795107620'></tg-emoji> Ошибка: {e}\nПроверьте токен или права доступа.", parse_mode="HTML")
        return
    if not recipients:
        await msg.delete()
        await message.answer("⚠️ Нет доступных диалогов (нет прав или список пуст).")
        return
    await save_vk_token(message.from_user.id, token)
    await state.update_data(vk_token=token, recipients=recipients, user_info=user_info, stats=stats)
    await msg.delete()
    info_text = (
        f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> <b>Аккаунт VK подключен</b>\n\n"
        f"👤 Имя: {user_info.get('first_name')} {user_info.get('last_name')}\n"
        f"🤙 Телефон: {user_info.get('phone')}\n"
        f"🆔 ID: {user_info.get('id')}\n\n"
        f"📊 <b>Найдено получателей:</b> {len(recipients)}\n"
        f"   ┣ Беседы: {stats['dialogues']}\n"
        f"   ┗ Личные: {stats['contacts']}\n\n"
        f"Теперь можно начинать рассылку через кнопку <b>📨 Начать рассылку</b>"
    )
    await message.answer(info_text, parse_mode="HTML", reply_markup=get_main_keyboard(message.from_user.id))
    await state.clear()

@dp.message(lambda msg: msg.text == "💰 Купить подписку")
async def buy_subscription_menu(message: Message, state: FSMContext):
    await state.clear()
    if not CRYPTOBOT_TOKEN:
        await message.answer("⚠️ Оплата через CryptoBot недоступна. Обратитесь к администратору.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for days, price in SUBSCRIPTION_PLANS.items():
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"📆 {days} дней - {price} USDT", callback_data=f"sub_{days}_{price}")])
    await message.answer("<tg-emoji emoji-id='5195058841988914267'></tg-emoji> <b>Выберите срок подписки</b>\n\nПосле оплаты подписка активируется автоматически.", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("sub_"))
async def process_subscription_choice(callback: CallbackQuery):
    _, days_str, price_str = callback.data.split("_")
    days = int(days_str)
    price = float(price_str)
    pay_url = await create_crypto_invoice(callback.from_user.id, days, price)
    if not pay_url:
        await callback.answer("❌ Ошибка создания счёта. Попробуйте позже.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="check_payment")]
    ])
    await callback.message.edit_text(
        f"<tg-emoji emoji-id='5195058841988914267'></tg-emoji> <b>Оплата подписки на {days} дней</b>\n\n"
        f"Сумма: <b>{price} USDT</b>\n"
        f"После оплаты нажмите «Проверить оплату».\n"
        f"Счёт действителен 1 час.",
        parse_mode="HTML", reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "check_payment")
async def check_payment_callback(callback: CallbackQuery):
    paid = await check_payment(callback.from_user.id)
    if paid:
        await callback.answer("✅ Оплата получена! Подписка активирована.", show_alert=True)
        await callback.message.edit_text("✅ Подписка успешно активирована!")
    else:
        await callback.answer("⏳ Оплата ещё не получена. Попробуйте позже.", show_alert=True)

@dp.message(lambda msg: msg.text == "📨 Начать рассылку")
async def start_newsletter_button(message: Message, state: FSMContext):
    await state.clear()
    if not await check_subscription(message.from_user.id):
        await message.answer("<tg-emoji emoji-id='5278578973595427038'></tg-emoji> У вас нет активной подписки. Купите её через кнопку <b>💰 Купить подписку</b>.", parse_mode="HTML")
        return
    token = await get_vk_token(message.from_user.id)
    if not token:
        await message.answer("❌ Сначала введите токен VK через кнопку <b>🔑 Ввести токен VK</b>.", parse_mode="HTML")
        return
    await message.answer("📌 Если хотите отправить только в беседы с определённым названием, введите его.\n"
                         "Если нужно отправить во все диалоги, просто напишите <b>пропустить</b> или нажмите /skip.",
                         parse_mode="HTML")
    await state.set_state(BotStates.waiting_group_name)
    await state.update_data(vk_token=token)

@dp.message(Command("skip"), StateFilter(BotStates.waiting_group_name))
async def skip_group_filter(message: Message, state: FSMContext):
    await state.update_data(group_filter=None)
    await proceed_to_load_recipients(message, state)

@dp.message(BotStates.waiting_group_name)
async def process_group_filter(message: Message, state: FSMContext):
    group_name = message.text.strip()
    if group_name.lower() == "пропустить":
        group_name = None
    await state.update_data(group_filter=group_name)
    await proceed_to_load_recipients(message, state)

async def proceed_to_load_recipients(message: Message, state: FSMContext):
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
    await state.update_data(recipients=recipients, user_info=user_info, stats=stats)
    templates = await get_templates(message.from_user.id)
    if templates:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Выбрать шаблон", callback_data="use_template")],
            [InlineKeyboardButton(text="✏️ Ввести текст вручную", callback_data="manual_text")]
        ])
        await message.answer("Выберите способ ввода текста:", reply_markup=kb)
    else:
        await message.answer("✏️ Введите текст сообщения для рассылки (можно использовать HTML):")
        await state.set_state(BotStates.waiting_newsletter_text)

@dp.callback_query(lambda c: c.data == "use_template")
async def use_template_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    uid = callback.from_user.id
    templates = await get_templates(uid)
    if not templates:
        await callback.message.answer("📭 У вас нет шаблонов. Сначала создайте их через раздел «Мои шаблоны».")
        await state.set_state(BotStates.waiting_newsletter_text)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for tpl in templates:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"📄 {tpl['name']} (задержка {tpl['delay']} с)", callback_data=f"tpl_{tpl['id']}")])
    await callback.message.answer("📋 <b>Выберите шаблон</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("tpl_"))
async def apply_template_callback(callback: CallbackQuery, state: FSMContext):
    tpl_id = int(callback.data.split("_")[1])
    uid = callback.from_user.id
    tpl = await get_template_by_id(tpl_id, uid)
    if not tpl:
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return
    await callback.answer()
    await state.update_data(newsletter_text=tpl["content"], delay=tpl["delay"])
    await start_mailing_with_data(callback.message, state)

@dp.callback_query(lambda c: c.data == "manual_text")
async def manual_text_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("✏️ Введите текст сообщения для рассылки (можно использовать HTML):")
    await state.set_state(BotStates.waiting_newsletter_text)

@dp.message(BotStates.waiting_newsletter_text)
async def process_newsletter_text(message: Message, state: FSMContext):
    await state.update_data(newsletter_text=message.text)
    data = await state.get_data()
    if "delay" in data:
        await start_mailing_with_data(message, state)
    else:
        await message.answer("⏱ Введите задержку в секундах (например, 3):")
        await state.set_state(BotStates.waiting_delay)

@dp.message(BotStates.waiting_delay)
async def process_delay(message: Message, state: FSMContext):
    try:
        delay = float(message.text.replace(",", "."))
        if delay < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число секунд.")
        return
    await state.update_data(delay=delay)
    await start_mailing_with_data(message, state)

async def start_mailing_with_data(message: Message, state: FSMContext):
    data = await state.get_data()
    token = data.get("vk_token")
    recipients = data.get("recipients")
    text = data.get("newsletter_text")
    delay = data.get("delay")
    user_info = data.get("user_info")
    stats = data.get("stats")
    if not all([token, recipients, text, user_info, stats]):
        await message.answer("❌ Ошибка данных. Начните заново через кнопки меню.")
        await state.clear()
        return
    asyncio.create_task(mailing_task(token, recipients, text, delay, message.chat.id, user_info, stats, message.from_user.id))
    await message.answer("🚀 Рассылка запущена! Прогресс будет здесь.")
    await state.clear()

@dp.message(lambda msg: msg.text == "📝 Мои шаблоны")
async def templates_menu(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    templates = await get_templates(uid)
    if not templates:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Создать шаблон", callback_data="create_template")]])
        await message.answer("📭 У вас пока нет шаблонов. Создайте первый!", reply_markup=kb)
        return
    text = "📋 <b>Ваши шаблоны:</b>\n\n"
    for t in templates:
        text += f"🔹 <b>{t['name']}</b> - <code>{t['content'][:50]}...</code> (⏱️ задержка: {t['delay']} с)\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="create_template")],
        [InlineKeyboardButton(text="❌ Удалить", callback_data="delete_template")]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "create_template")
async def create_template_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("📝 Введите <b>название</b> шаблона:", parse_mode="HTML")
    await state.set_state(BotStates.waiting_template_name)

@dp.message(BotStates.waiting_template_name)
async def process_template_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Название не может быть пустым.")
        return
    await state.update_data(template_name=name)
    await message.answer("✏️ Теперь введите <b>текст</b> шаблона (можно использовать HTML):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_template_content)

@dp.message(BotStates.waiting_template_content)
async def process_template_content(message: Message, state: FSMContext):
    content = message.text
    await state.update_data(template_content=content)
    await message.answer("⏱️ Введите <b>задержку</b> для этого шаблона (в секундах, например 3):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_template_delay)

@dp.message(BotStates.waiting_template_delay)
async def process_template_delay(message: Message, state: FSMContext):
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
        await state.clear()
        await message.answer("❌ Ошибка, начните заново.")
        return
    await save_template(message.from_user.id, name, content, delay)
    await message.answer(f"✅ Шаблон <b>{name}</b> сохранён с задержкой {delay} с!", parse_mode="HTML")
    await state.clear()

@dp.callback_query(lambda c: c.data == "delete_template")
async def delete_template_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    templates = await get_templates(uid)
    if not templates:
        await callback.answer("📭 Нет шаблонов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in templates:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"❌ {t['name']}", callback_data=f"del_tpl_{t['id']}")])
    await callback.message.answer("🗑️ Выберите шаблон для удаления:", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("del_tpl_"))
async def confirm_delete_template(callback: CallbackQuery):
    tpl_id = int(callback.data.split("_")[2])
    await delete_template(tpl_id, callback.from_user.id)
    await callback.answer("✅ Шаблон удалён", show_alert=True)
    await callback.message.delete()

@dp.message(lambda msg: msg.text == "👤 Мой профиль")
async def show_profile(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    user = await get_user(uid)
    if not user:
        await message.answer("❌ Ошибка: не найден в базе.")
        return
    sub = user.get('subscription_until')
    sub_text = sub.strftime('%d.%m.%Y %H:%M') if sub else "Нет"
    if uid in ADMIN_IDS:
        sub_text = "🔹 Вечная (админ)"
    text = (
        f"<tg-emoji emoji-id='5275979556308674886'></tg-emoji> <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 Имя: {user.get('first_name') or 'Не указано'}\n"
        f"🔖 Username: @{user.get('username') or 'Нет'}\n"
        f"📅 Регистрация: {user.get('joined_at').strftime('%d.%m.%Y %H:%M') if user.get('joined_at') else 'Неизвестно'}\n"
        f"⏳ Подписка до: {sub_text}\n\n"
        f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> Сменить токен можно через кнопку «Ввести токен VK»"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(lambda msg: msg.text == "📊 Моя статистика")
async def user_stats(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    stats = await get_user_mailing_stats(uid, 10)
    if not stats:
        await message.answer("📭 У вас ещё нет завершённых рассылок.")
        return
    text = "<tg-emoji emoji-id='5278753302023004775'></tg-emoji> <b>Ваши последние 10 рассылок</b>\n\n"
    for s in stats:
        text += f"🗓 {s['started_at'].strftime('%Y-%m-%d %H:%M')}\n"
        text += f"👤 VK: {s['vk_account_name']}\n"
        text += f"📊 Всего: {s['total_recipients']} | <tg-emoji emoji-id='5206401524200145033'></tg-emoji>{s['sent_success']} <tg-emoji emoji-id='5206510891247371052'></tg-emoji>{s['sent_error']} | ⏱{s['total_time_seconds']:.1f}с\n\n"
    await message.answer(text[:4000], parse_mode="HTML")

@dp.message(lambda msg: msg.text == "👑 Админ-панель" and msg.from_user.id in ADMIN_IDS)
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📨 Рассылка пользователям", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="admin_give_sub")],
        [InlineKeyboardButton(text="📈 Статистика проливов", callback_data="admin_mailing_stats")],
        [InlineKeyboardButton(text="🔙 Закрыть", callback_data="admin_close")]
    ])
    await message.answer("👑 <b>Админ-панель</b>\nВыберите действие:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(lambda c: c.data.startswith("admin_"))
async def admin_callback(callback: CallbackQuery, state: FSMContext):
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
        await state.set_state(BotStates.admin_waiting_broadcast)
    elif data == "admin_give_sub":
        await callback.message.answer("🔢 Введите Telegram ID пользователя (число):")
        await state.set_state(BotStates.admin_waiting_user_id)
    elif data == "admin_mailing_stats":
        mailings = await get_mailing_stats(20)
        if not mailings:
            await callback.message.answer("📭 История проливов пуста.")
            return
        text = "📈 <b>Последние 20 проливов (всех пользователей):</b>\n"
        for m in mailings:
            text += f"🆔 ID {m['id']} | {m['started_at'].strftime('%Y-%m-%d %H:%M')} | {m['vk_account_name']} | Всего: {m['total_recipients']} | <tg-emoji emoji-id='5206401524200145033'></tg-emoji>{m['sent_success']} <tg-emoji emoji-id='5206510891247371052'></tg-emoji>{m['sent_error']} | ⏱️{m['total_time_seconds']:.1f}с\n"
        await callback.message.answer(text[:4000], parse_mode="HTML")
    elif data == "admin_close":
        await callback.message.delete()

@dp.message(BotStates.admin_waiting_broadcast)
async def admin_broadcast_process(message: Message, state: FSMContext):
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
    await state.clear()

@dp.message(BotStates.admin_waiting_user_id)
async def admin_get_user_id(message: Message, state: FSMContext):
    try:
        target = int(message.text.strip())
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    await state.update_data(target_id=target)
    await message.answer("📆 Введите количество дней (можно дробное, например 0.5 для 12 часов):")
    await state.set_state(BotStates.admin_waiting_days)

@dp.message(BotStates.admin_waiting_days)
async def admin_give_days(message: Message, state: FSMContext):
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
    await state.clear()

@dp.message(Command("revoke_sub"))
async def cmd_revoke_sub(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /revoke_sub <user_id>")
        return
    try:
        uid = int(parts[1])
    except:
        await message.answer("❌ ID должен быть числом.")
        return
    await revoke_subscription(uid)
    await message.answer(f"✅ Подписка пользователя {uid} отозвана.")
    try:
        await bot.send_message(uid, "⚠️ Ваша подписка была отозвана администратором.")
    except:
        pass

# ===================== ЗАПУСК =====================
async def on_startup():
    await init_db()
    logger.info("✅ База данных инициализирована")
    if not ANTICAPTCHA_KEY:
        logger.warning("⚠️ ANTICAPTCHA_KEY не задан. При капче рассылка остановится!")
    if not CRYPTOBOT_TOKEN:
        logger.warning("⚠️ CRYPTOBOT_TOKEN не задан. Покупка подписки через криптобот недоступна.")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())