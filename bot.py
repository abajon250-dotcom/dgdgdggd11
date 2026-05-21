import os
import re
import json
import io
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
from aiogram import F
from vk_api import VkApi
from vk_api.exceptions import ApiError

from database import (
    init_db, add_user, get_user_subscription, set_subscription,
    add_vk_token, update_token_stats, get_all_tokens, set_active_token,
    delete_token, get_active_token, get_all_user_stats,
    save_template, get_templates, delete_template, export_templates_json, import_templates_json,
    create_invoice, get_pending_invoice, mark_invoice_paid,
    get_all_users, get_bot_stats, save_mailing_stats, get_user_mailing_stats
)

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

# ========================== VK HELPERS ==========================
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
            return False, f"Ошибка VK {e.code}", None, None, None
    except Exception as e:
        return False, str(e), None, None, None

def get_friends(token):
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
            dialogs.append({'peer_id': peer['id'], 'name': f"User {peer['id']}"})
        return dialogs
    except ApiError as e:
        if e.code == 14:
            solve_captcha(e.captcha_img)
            raise Exception("Капча решена, повторите запрос")
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
    params = {"amount": amount, "asset": "USDT", "description": f"Subscription {days}d {amount} USDT", "user_id": user_id}
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        resp = requests.post(CRYPTOBOT_API_URL + "createInvoice", json=params, headers=headers)
        data = resp.json()
        if data.get("ok"):
            invoice_id = str(data["result"]["invoice_id"])
            await create_invoice(invoice_id, user_id, days)
            return data["result"]["pay_url"]
    except:
        return None

async def check_payment(user_id):
    inv = await get_pending_invoice(user_id)
    if not inv:
        return False
    invoice_id, days = inv
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        resp = requests.post(CRYPTOBOT_API_URL + "getInvoices", json={"invoice_ids": invoice_id}, headers=headers)
        data = resp.json()
        if data.get("ok") and data["result"]["items"] and data["result"]["items"][0]["status"] == "paid":
            await mark_invoice_paid(invoice_id)
            await set_subscription(user_id, days)
            return True
    except:
        pass
    return False

# ========================== ПОДПИСКА ==========================
async def has_subscription(user_id):
    if user_id in ADMIN_IDS:
        return True
    sub = await get_user_subscription(user_id)
    return sub and sub > datetime.now()

# ========================== КЛАВИАТУРЫ (КРАСИВЫЕ) ==========================
def main_menu(uid):
    buttons = [
        [InlineKeyboardButton(text="📨 РАССЫЛКА ДРУЗЬЯМ", callback_data="start_mailing", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="🔑 ДОБАВИТЬ ТОКЕН", callback_data="add_token", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="➕ МАССОВОЕ ДОБАВЛЕНИЕ", callback_data="mass_add_tokens", icon_custom_emoji_id="5472096095280572227", style="default")],
        [InlineKeyboardButton(text="📱 ВОЙТИ ПО НОМЕРУ", callback_data="phone_login", icon_custom_emoji_id="5472096095280572227", style="primary")],
        [InlineKeyboardButton(text="📝 МОИ ШАБЛОНЫ", callback_data="my_templates", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="account_stats", icon_custom_emoji_id="5278753302023004775", style="primary")],
        [InlineKeyboardButton(text="👤 МОЙ ПРОФИЛЬ", callback_data="my_profile", icon_custom_emoji_id="5275979556308674886", style="primary")],
        [InlineKeyboardButton(text="💰 КУПИТЬ ПОДПИСКУ", callback_data="buy_sub", icon_custom_emoji_id="5195058841988914267", style="success")],
    ]
    if uid in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="👑 АДМИН", callback_data="admin_panel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_button(callback_data="back_to_main"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ НАЗАД", callback_data=callback_data, style="default")]])

# ========================== FSM ==========================
class BotStates(StatesGroup):
    waiting_token = State()
    waiting_mass_tokens = State()
    waiting_phone = State()
    waiting_password = State()
    waiting_2fa = State()
    waiting_newsletter_text = State()
    waiting_delay = State()
    waiting_template_name = State()
    waiting_template_content = State()
    waiting_template_delay = State()
    waiting_import = State()
    admin_broadcast = State()
    admin_user_id = State()
    admin_days = State()

# ========================== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await add_user(uid, message.from_user.username, message.from_user.first_name)
    welcome = (
        f"<tg-emoji emoji-id='5278611606756942667'></tg-emoji> <b>✨ VK РАССЫЛЬЩИК ✨</b>\n\n"
        f"🚀 Профессиональный инструмент для рассылки друзьям\n\n"
        f"📌 <b>Возможности:</b>\n"
        f"├ 🎯 Рассылка только друзьям (без риска бана)\n"
        f"├ 🔄 Ротация нескольких аккаунтов\n"
        f"├ 📊 Детальная статистика по каждому токену\n"
        f"├ 📝 Шаблоны сообщений с экспортом/импортом\n"
        f"└ ⚡ Авторешение капчи через 2captcha\n\n"
        f"🔐 <b>Доступ по подписке</b>\n"
        f"💰 Купить: кнопка ниже\n\n"
        f"⬇️ <b>Выбери действие:</b>"
    )
    await message.answer(welcome, parse_mode="HTML", reply_markup=main_menu(uid))

# ----- Добавление токена -----
@dp.callback_query(lambda c: c.data == "add_token")
async def add_token_prompt(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("❌ Нет подписки!", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"<tg-emoji emoji-id='5472096095280572227'></tg-emoji> <b>ВВЕДИТЕ ТОКЕН VK</b>\n\n"
        f"📌 <b>Инструкция:</b>\n"
        f"1️⃣ Перейдите на https://vkhost.github.io\n"
        f"2️⃣ Выберите права: Сообщения, Доступ к друзьям\n"
        f"3️⃣ Скопируйте токен (начинается с vk1.)\n"
        f"4️⃣ Отправьте его сюда\n\n"
        f"⚠️ Токен очень длинный, копируйте полностью!",
        parse_mode="HTML", reply_markup=back_button())
    await state.set_state(BotStates.waiting_token)

@dp.message(BotStates.waiting_token)
async def process_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if not token:
        await message.answer("❌ Токен не может быть пустым.")
        return
    msg = await message.answer("🔄 Проверка токена...")
    valid, fn, ln, phone, vid = validate_vk_token(token)
    if not valid:
        await msg.delete()
        await message.answer(
            f"❌ <b>ОШИБКА ПРОВЕРКИ</b>\n\n"
            f"<b>Детали:</b>\n{fn}\n\n"
            f"💡 <b>Что делать:</b>\n"
            f"├ Попробуй еще раз\n"
            f"├ Получи новый токен\n"
            f"└ Обратись в поддержку @bloodworn",
            parse_mode="HTML")
        return
    name = f"{fn} {ln}"
    await add_vk_token(message.from_user.id, token, name)
    await msg.delete()
    info_text = (
        f"✅ <b>ТОКЕН УСПЕШНО СОХРАНЕН!</b>\n\n"
        f"📷 <b>ВКонтакте подключен</b>\n"
        f"Токен зашифрован и надежно сохранен\n\n"
        f"👤 <b>Данные аккаунта:</b>\n"
        f"├ Имя: {name}\n"
        f"├ ID: {vid}\n"
        f"└ Телефон: {phone}\n\n"
        f"🎉 <b>Что дальше?</b>\n"
        f"Теперь вы можете создавать рассылки!\n\n"
        f"💡 <b>Попробуйте:</b>\n"
        f"├ ✏️ Создать рассылку\n"
        f"└ 📤 Отправить всем друзьям\n\n"
        f"🚀 <b>Готовы начать!</b>"
    )
    await message.answer(info_text, parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ----- Массовое добавление -----
@dp.callback_query(lambda c: c.data == "mass_add_tokens")
async def mass_add_prompt(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("❌ Нет подписки!", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"📦 <b>МАССОВОЕ ДОБАВЛЕНИЕ</b>\n\n"
        f"Отправьте список токенов в формате:\n\n"
        f"<code>токен1 | Название1</code>\n"
        f"<code>токен2 | Название2</code>\n\n"
        f"Каждый токен с новой строки.\n"
        f"Название можно не указывать.\n\n"
        f"<b>Пример:</b>\n"
        f"<code>vk1.a... | Мой основной</code>\n"
        f"<code>vk1.b... </code>",
        parse_mode="HTML", reply_markup=back_button())
    await state.set_state(BotStates.waiting_mass_tokens)

@dp.message(BotStates.waiting_mass_tokens)
async def process_mass_tokens(message: Message, state: FSMContext):
    lines = message.text.strip().split('\n')
    added = 0
    errors_list = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split('|')
        token = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else None
        valid, fn, ln, phone, vid = validate_vk_token(token)
        if not valid:
            errors_list.append(f"❌ {token[:20]}... — {fn}")
            continue
        if not name:
            name = f"{fn} {ln}"
        await add_vk_token(message.from_user.id, token, name)
        added += 1
    result_text = f"✅ <b>Добавлено аккаунтов:</b> {added}\n"
    if errors_list:
        result_text += f"⚠️ <b>Ошибки:</b>\n" + "\n".join(errors_list[:5])
        if len(errors_list) > 5:
            result_text += f"\n... и ещё {len(errors_list)-5}"
    await message.answer(result_text, parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ----- Вход по номеру телефона -----
@dp.callback_query(lambda c: c.data == "phone_login")
async def phone_login_start(callback: CallbackQuery, state: FSMContext):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("❌ Нет подписки!", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"📱 <b>ВХОД ПО НОМЕРУ ТЕЛЕФОНА</b>\n\n"
        f"Введите номер телефона в формате <b>+71234567890</b>:",
        parse_mode="HTML", reply_markup=back_button())
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
    vk_session = VkApi(login=login, password=password, api_version='5.131')
    try:
        vk_session.auth(token_only=True)
        token = vk_session.token['access_token']
    except ApiError as e:
        if e.code == 5:
            await message.answer("❌ Неверный логин или пароль")
            await state.clear()
            return
        elif e.code == 17:
            await state.update_data(vk_session=vk_session)
            await message.answer("📱 Введите код двухфакторной аутентификации:")
            await state.set_state(BotStates.waiting_2fa)
            return
        elif e.code == 14:
            await message.answer("❌ Требуется капча. Используйте вход по токену.")
            await state.clear()
            return
        else:
            await message.answer(f"❌ Ошибка VK: {e}")
            await state.clear()
            return
    valid, fn, ln, phone, vid = validate_vk_token(token)
    if not valid:
        await message.answer(f"❌ Ошибка проверки токена: {fn}")
        await state.clear()
        return
    name = f"{fn} {ln}"
    await add_vk_token(message.from_user.id, token, name)
    info_text = (
        f"✅ <b>АККАУНТ ДОБАВЛЕН ЧЕРЕЗ НОМЕР ТЕЛЕФОНА</b>\n\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"🆔 ID: {vid}"
    )
    await message.answer(info_text, parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.message(BotStates.waiting_2fa)
async def phone_login_2fa(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    vk_session = data.get('vk_session')
    try:
        vk_session.auth(token_only=True, auth_handler=lambda: code)
        token = vk_session.token['access_token']
    except ApiError as e:
        await message.answer(f"❌ Неверный код: {e}")
        await state.clear()
        return
    valid, fn, ln, phone, vid = validate_vk_token(token)
    if not valid:
        await message.answer(f"❌ Ошибка проверки токена: {fn}")
        await state.clear()
        return
    name = f"{fn} {ln}"
    await add_vk_token(message.from_user.id, token, name)
    info_text = (
        f"✅ <b>АККАУНТ ДОБАВЛЕН ЧЕРЕЗ НОМЕР ТЕЛЕФОНА (С 2FA)</b>\n\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: {phone}\n"
        f"🆔 ID: {vid}"
    )
    await message.answer(info_text, parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

# ----- Статистика аккаунтов (красивая, с деревом) -----
@dp.callback_query(lambda c: c.data == "account_stats")
async def show_account_stats(callback: CallbackQuery):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("❌ Нет подписки!", show_alert=True)
        return
    stats = await get_all_user_stats(callback.from_user.id)
    mailing_stats = await get_user_mailing_stats(callback.from_user.id)
    if not stats:
        await callback.message.edit_text("📭 Нет добавленных аккаунтов.", reply_markup=back_button())
        return
    
    # Общая статистика
    total_sent = sum(s['sent'] for s in stats)
    total_errors = sum(s['errors'] for s in stats)
    success_rate = (total_sent / (total_sent + total_errors) * 100) if (total_sent + total_errors) > 0 else 0
    trend = "📈 Рост" if mailing_stats['week_mailings'] > 0 else "➖ Стабильно"
    
    text = (
        f"📊 <b>СТАТИСТИКА АККАУНТА</b>\n\n"
        f"📈 <b>Тренд:</b> {trend}\n"
        f"🎯 <b>Средняя успешность:</b> {success_rate:.1f}%\n\n"
        f"📅 <b>По периодам:</b>\n"
        f"├ За неделю: {mailing_stats['week_mailings']} рассылок\n"
        f"├ За месяц: {mailing_stats['month_mailings']} рассылок\n"
        f"└ Всего: {mailing_stats['total_mailings']} рассылок\n\n"
        f"✉️ <b>Отправлено сообщений:</b>\n"
        f"├ За неделю: {mailing_stats['week_sent']}\n"
        f"├ Всего: {mailing_stats['total_sent']}\n"
        f"└ Успешно: {total_sent}\n\n"
    )
    if mailing_stats['best']:
        best = mailing_stats['best']
        text += f"🏆 <b>Лучший результат:</b>\n├ {best['date'].strftime('%d.%m.%Y')}: {best['success']} сообщений\n\n"
    text += (
        f"💡 <b>Аналитика:</b>\n"
        f"├ 🔷 ВКонтакте: <b>Активна</b>\n"
        f"└ 💰 Потрачено: $0.0\n\n"
        f"📈 <b>Эффективность:</b>\n"
        f"Отличная работа! Продолжай в том же духе! 🎉"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())

# ----- Шаблоны -----
@dp.callback_query(lambda c: c.data == "my_templates")
async def templates_menu(callback: CallbackQuery):
    if not await has_subscription(callback.from_user.id):
        await callback.answer("❌ Нет подписки!", show_alert=True)
        return
    uid = callback.from_user.id
    tpls = await get_templates(uid)
    if not tpls:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ СОЗДАТЬ ШАБЛОН", callback_data="create_template", style="primary")],
            [InlineKeyboardButton(text="📥 ИМПОРТ", callback_data="import_templates", style="default")],
            [InlineKeyboardButton(text="◀️ НАЗАД", callback_data="back_to_main", style="default")]
        ])
        await callback.message.edit_text("📭 У вас пока нет шаблонов.", reply_markup=kb)
        return
    text = "📋 <b>ВАШИ ШАБЛОНЫ</b>\n\n"
    for t in tpls:
        t_id, t_name, t_content, t_delay = t
        short_content = (t_content[:40] + '...') if len(t_content) > 40 else t_content
        text += f"🔹 <b>{t_name}</b>\n   📝 <code>{short_content}</code>\n   ⏱ Задержка: {t_delay} сек.\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ СОЗДАТЬ", callback_data="create_template", style="success")],
        [InlineKeyboardButton(text="📤 ЭКСПОРТ", callback_data="export_templates", style="default")],
        [InlineKeyboardButton(text="📥 ИМПОРТ", callback_data="import_templates", style="default")],
        [InlineKeyboardButton(text="❌ УДАЛИТЬ", callback_data="delete_template", style="danger")],
        [InlineKeyboardButton(text="◀️ НАЗАД", callback_data="back_to_main", style="default")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "create_template")
async def create_template(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📝 Введите <b>название</b> шаблона:", parse_mode="HTML", reply_markup=back_button())
    await state.set_state(BotStates.waiting_template_name)

@dp.message(BotStates.waiting_template_name)
async def get_tpl_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("❌ Название не может быть пустым.")
        return
    await state.update_data(tpl_name=name)
    await message.answer("✏️ Введите <b>текст</b> шаблона (можно HTML):", parse_mode="HTML", reply_markup=None)
    await state.set_state(BotStates.waiting_template_content)

@dp.message(BotStates.waiting_template_content)
async def get_tpl_content(message: Message, state: FSMContext):
    content = message.text
    await state.update_data(tpl_content=content)
    await message.answer("⏱️ Введите <b>задержку</b> (сек):", parse_mode="HTML", reply_markup=None)
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
    await save_template(message.from_user.id, name, content, delay)
    await message.answer(f"✅ Шаблон <b>{name}</b> сохранён!", parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(lambda c: c.data == "export_templates")
async def export_templates_handler(callback: CallbackQuery):
    uid = callback.from_user.id
    data = await export_templates_json(uid)
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
    await callback.message.edit_text("📤 Отправьте JSON-файл с шаблонами (экспортированный ранее):", reply_markup=back_button())
    await state.set_state(BotStates.waiting_import)

@dp.message(StateFilter(BotStates.waiting_import), F.document)
async def process_import_templates(message: Message, state: FSMContext):
    if not message.document:
        await message.answer("❌ Отправьте файл JSON.")
        return
    file = await bot.get_file(message.document.file_id)
    file_bytes = await bot.download_file(file.file_path)
    try:
        data = json.loads(file_bytes.read().decode('utf-8'))
        if not isinstance(data, list):
            raise ValueError
        await import_templates_json(message.from_user.id, data)
        await message.answer(f"✅ Импортировано {len(data)} шаблонов.", reply_markup=main_menu(message.from_user.id))
    except:
        await message.answer("❌ Неверный формат файла. Загрузите корректный JSON.")
    await state.clear()

@dp.callback_query(lambda c: c.data == "delete_template")
async def delete_template_menu(callback: CallbackQuery):
    uid = callback.from_user.id
    tpls = await get_templates(uid)
    if not tpls:
        await callback.answer("Нет шаблонов", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in tpls:
        t_id, t_name, _, _ = t
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"❌ {t_name}", callback_data=f"del_tpl_{t_id}", style="danger")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ НАЗАД", callback_data="my_templates", style="default")])
    await callback.message.edit_text("🗑️ Выберите шаблон для удаления:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("del_tpl_"))
async def confirm_delete_template(callback: CallbackQuery):
    tpl_id = int(callback.data.split("_")[2])
    await delete_template(tpl_id, callback.from_user.id)
    await callback.answer("✅ Шаблон удалён", show_alert=True)
    await templates_menu(callback)

# ----- Подписка -----
@dp.callback_query(lambda c: c.data == "buy_sub")
async def buy_sub_menu(callback: CallbackQuery):
    await callback.answer()
    if not CRYPTOBOT_TOKEN:
        await callback.message.edit_text("⚠️ CryptoBot недоступен", reply_markup=back_button())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📆 1 ДЕНЬ - 2$", callback_data="sub_1_2", icon_custom_emoji_id="5195058841988914267", style="primary")],
        [InlineKeyboardButton(text="📆 7 ДНЕЙ - 5$", callback_data="sub_7_5", style="primary")],
        [InlineKeyboardButton(text="📆 30 ДНЕЙ - 15$", callback_data="sub_30_15", style="primary")],
        [InlineKeyboardButton(text="◀️ НАЗАД", callback_data="back_to_main", style="default")]
    ])
    await callback.message.edit_text("💰 <b>ВЫБЕРИТЕ СРОК ПОДПИСКИ</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("sub_"))
async def process_sub(callback: CallbackQuery):
    _, days_str, price_str = callback.data.split("_")
    days = int(days_str)
    price = float(price_str)
    pay_url = await create_crypto_invoice(callback.from_user.id, days, price)
    if not pay_url:
        await callback.answer("❌ Ошибка создания счёта", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 ОПЛАТИТЬ", url=pay_url, style="success")],
        [InlineKeyboardButton(text="🔄 ПРОВЕРИТЬ ОПЛАТУ", callback_data="check_payment", style="primary")],
        [InlineKeyboardButton(text="◀️ НАЗАД", callback_data="buy_sub", style="default")]
    ])
    await callback.message.edit_text(f"💳 Счёт на {days} дней, {price}$\nПосле оплаты нажмите «ПРОВЕРИТЬ ОПЛАТУ»", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "check_payment")
async def check_pay(callback: CallbackQuery):
    paid = await check_payment(callback.from_user.id)
    if paid:
        await callback.answer("✅ Подписка активирована!", show_alert=True)
        await callback.message.edit_text("✅ Подписка активирована!", reply_markup=back_button())
    else:
        await callback.answer("⏳ Оплата не найдена", show_alert=True)

# ----- РАССЫЛКА ДРУЗЬЯМ -----
@dp.callback_query(lambda c: c.data == "start_mailing")
async def start_mailing(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await has_subscription(callback.from_user.id):
        await callback.message.edit_text("❌ Нет подписки", reply_markup=back_button())
        return
    tokens = await get_all_tokens(callback.from_user.id)
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
    
    # Получаем активный токен (или первый)
    active_tuple = await get_active_token(message.from_user.id)
    if active_tuple:
        token_id, token, token_name = active_tuple
    elif tokens:
        token_id, token, token_name, _ = tokens[0]
    else:
        await message.answer("❌ Нет доступных аккаунтов", reply_markup=main_menu(message.from_user.id))
        return
    
    await message.answer("🔄 Проверяю токен...")
    valid, fn, ln, phone, vid = validate_vk_token(token)
    if not valid:
        await message.answer(f"❌ Аккаунт {token_name} невалиден: {fn}\nДобавьте новый или активируйте другой.", reply_markup=main_menu(message.from_user.id))
        return
    
    await message.answer("👥 Загружаю список друзей...")
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
    
    token_list = tokens
    token_idx = 0
    for idx, friend in enumerate(friends, 1):
        tid, ttoken, tname, _ = token_list[token_idx % len(token_list)]
        success, err = send_vk_message(ttoken, friend['peer_id'], text)
        if success:
            sent += 1
            await update_token_stats(tid, sent=1)
        else:
            errors += 1
            await update_token_stats(tid, errors=1)
        if idx % 5 == 0 or idx == total:
            await progress_msg.edit_text(f"📊 Прогресс: {idx}/{total} | ✅{sent} ❌{errors}")
        token_idx += 1
        await asyncio.sleep(delay)
    
    # Сохраняем статистику
    await save_mailing_stats(message.from_user.id, token_id, total, sent, errors)
    
    await message.answer(
        f"✅ <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\n"
        f"📝 Отправлено: {sent}/{total}\n"
        f"   ├ ✅ Успешно: {sent}\n"
        f"   └ ❌ Ошибки: {errors}\n\n"
        f"📊 Успешность: {sent/total*100:.1f}%\n"
        f"⏲️ Время: {delay*total:.1f} сек.\n\n"
        f"👤 Аккаунт: {token_name}\n"
        f"📂 Всего друзей: {total}",
        parse_mode="HTML", reply_markup=main_menu(message.from_user.id))
    await progress_msg.delete()

# ----- Профиль -----
@dp.callback_query(lambda c: c.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    uid = callback.from_user.id
    sub = await get_user_subscription(uid)
    sub_text = sub.strftime('%d.%m.%Y %H:%M') if sub else "Нет"
    if uid in ADMIN_IDS:
        sub_text = "🔹 Вечная"
    text = (
        f"<tg-emoji emoji-id='5275979556308674886'></tg-emoji> <b>ВАШ ПРОФИЛЬ</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 Имя: {callback.from_user.first_name}\n"
        f"⏳ Подписка до: {sub_text}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button())

# ----- Админ-панель -----
@dp.callback_query(lambda c: c.data == "admin_panel" and c.from_user.id in ADMIN_IDS)
async def admin_panel(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 СТАТИСТИКА БОТА", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton(text="📨 РАССЫЛКА ПОЛЬЗОВАТЕЛЯМ", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton(text="🎁 ВЫДАТЬ ПОДПИСКУ", callback_data="admin_give_sub", style="success")],
        [InlineKeyboardButton(text="◀️ НАЗАД", callback_data="back_to_main", style="danger")]
    ])
    await callback.message.edit_text("👑 <b>АДМИН-ПАНЕЛЬ</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "admin_stats" and c.from_user.id in ADMIN_IDS)
async def admin_stats(callback: CallbackQuery):
    stats = await get_bot_stats()
    await callback.message.edit_text(
        f"📊 <b>СТАТИСТИКА БОТА</b>\n\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"🔑 Токенов: {stats['tokens']}\n"
        f"🚀 Рассылок: {stats['mailings']}\n"
        f"✉️ Отправлено: {stats['sent']}",
        parse_mode="HTML", reply_markup=back_button("admin_panel"))

@dp.callback_query(lambda c: c.data == "admin_broadcast" and c.from_user.id in ADMIN_IDS)
async def admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✏️ Введите текст для рассылки ВСЕМ пользователям:", reply_markup=None)
    await state.set_state(BotStates.admin_broadcast)

@dp.message(BotStates.admin_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
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
    await set_subscription(target, days)
    await message.answer(f"✅ Пользователю {target} выдана подписка на {days} дн.", reply_markup=main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu(callback.from_user.id))

# ========================== ЗАПУСК ==========================
async def on_startup():
    await init_db()
    logger.info("✅ Бот запущен")

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
