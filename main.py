import os
import re
import uuid
import sqlite3
import asyncio
import time
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputSticker
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramRetryAfter

# --- Загрузка конфигурации ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID"))
MONITOR_CHATS = [int(x.strip()) for x in os.getenv("MONITOR_CHATS", "").split(",") if x.strip()]
IGNORE_WORDS = [x.strip().lower() for x in os.getenv("IGNORE_WORDS", "").split(",") if x.strip()]

DATA_DIR = os.getenv('DATA_DIR', '/app/data') 
os.makedirs(DATA_DIR, exist_ok=True)  # Гарантируем, что папка существует
DB_PATH = os.path.join(DATA_DIR, "stickers.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Блокировка для предотвращения одновременного копирования одного и того же пака
processing_packs = set()
# Хранение ID последних отправленных ссылок: {(chat_id, original_pack_name): message_id}
last_bot_messages = {}
# Формат: {user_id: [timestamp1, timestamp2, ...]}
user_copy_timestamps = {}


# --- База данных SQLite ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # original_name: Имя оригинального пака
    # target_name: Имя скопированного пака или привязанного вручную
    # is_manual: 0 - авто-копия/чистая замена, 1 - привязан вручную админом с пометкой
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS packs (
            original_name TEXT PRIMARY KEY,
            target_name TEXT,
            is_manual INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def is_target_pack(pack_name: str) -> bool:
    """Проверяет, является ли пак уже очищенным клоном или ручной заменой"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM packs WHERE target_name = ?", (pack_name,))
    res = cursor.fetchone()
    conn.close()
    return bool(res)


def get_pack_mapping(original_name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT target_name, is_manual FROM packs WHERE original_name = ?", (original_name,))
    res = cursor.fetchone()
    conn.close()
    return res


def save_pack_mapping(original_name: str, target_name: str, is_manual: int = 0):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO packs (original_name, target_name, is_manual) VALUES (?, ?, ?)",
        (original_name, target_name, is_manual)
    )
    conn.commit()
    conn.close()


def delete_pack_mapping(original_name: str):
    """Удаляет связь из базы (для кнопки Отмены)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM packs WHERE original_name = ?", (original_name,))
    conn.commit()
    conn.close()


# --- Машина состояний для Админки ---
class AdminFSM(StatesGroup):
    waiting_for_second_pack = State()  # Замена с пометкой "заменено админом"
    waiting_for_clean_pack = State()  # Чистая замена (is_manual=0)
    waiting_for_auto_link = State()  # Поточная авто-связь парами


# --- Вспомогательные функции ---
def extract_pack_name(text: str) -> str:
    """Извлекает short_name пака из ссылки. Улучшенная регулярка отсекает мусор."""
    if not text: return None
    match = re.search(r'addstickers/([a-zA-Z0-9_]+)', text)
    return match.group(1) if match else text.strip()


async def fast_copy_pack(original_name: str) -> str:
    """Логика быстрого копирования пака через file_id"""
    original_set = await bot.get_sticker_set(original_name)
    bot_info = await bot.get_me()

    clean_title = original_set.title.replace("@", "").replace("t.me", "")
    new_name = f"cp_{uuid.uuid4().hex[:8]}_by_{bot_info.username}"

    if original_set.is_animated:
        st_format = "animated"
    elif original_set.is_video:
        st_format = "video"
    else:
        st_format = "static"

    input_stickers = [
        InputSticker(
            sticker=stk.file_id,
            format=st_format,
            emoji_list=[stk.emoji] if stk.emoji else ["📌"]
        )
        for stk in original_set.stickers
    ]

    initial_batch = input_stickers[:50]
    remaining_batch = input_stickers[50:]

    await bot.create_new_sticker_set(
        user_id=TARGET_USER_ID,
        name=new_name,
        title=clean_title,
        stickers=initial_batch
    )

    for stk in remaining_batch:
        try:
            await bot.add_sticker_to_set(
                user_id=TARGET_USER_ID,
                name=new_name,
                sticker=stk
            )
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await bot.add_sticker_to_set(user_id=TARGET_USER_ID, name=new_name, sticker=stk)

    return new_name


# --- Основной обработчик в чатах ---
@dp.message(F.chat.id.in_(MONITOR_CHATS) & F.sticker)
async def process_chat_sticker(message: Message):
    pack_name = message.sticker.set_name
    if not pack_name:
        return  # Это одиночный стикер-картинка, игнорируем

    if is_target_pack(pack_name):
        return  # Это уже наш клон или ручная замена

    chat_id = message.chat.id
    user_id = message.from_user.id

    async def send_and_cleanup_link(text: str):
        if (chat_id, pack_name) in last_bot_messages:
            try:
                await bot.delete_message(chat_id, last_bot_messages[(chat_id, pack_name)])
            except Exception:
                pass
        sent_msg = await message.answer(text, disable_web_page_preview=True)
        last_bot_messages[(chat_id, pack_name)] = sent_msg.message_id

    # 1. Проверяем в БД (если пак уже скопирован ранее)
    mapping = get_pack_mapping(pack_name)
    if mapping:
        target_name, is_manual = mapping
        try:
            await message.delete()
        except Exception:
            pass

        link = f"https://t.me/addstickers/{target_name}"
        if is_manual:
            text = f"🛡 Этот стикерпак имеется в канале @FurriStik:\n👉 {link}"
        else:
            text = f"✅ Стикерпак очищен от рекламы и доступен по ссылке:\n👉 {link}"

        await send_and_cleanup_link(text)
        return

    # 2. ЗАЩИТА ОТ ГОНКИ
    if pack_name in processing_packs:
        try:
            await message.delete()
        except Exception:
            pass
        return

    # 3. Проверка на игнор-слова
    original_set = await bot.get_sticker_set(pack_name)
    if any(word in original_set.title.lower() for word in IGNORE_WORDS):
        return

    # 4. ПРОВЕРКА ЛИМИТА: НЕ БОЛЕЕ 2 ПАКОВ ЗА 3 МИНУТЫ НА ПОЛЬЗОВАТЕЛЯ
    current_time = time.time()

    if user_id in user_copy_timestamps:
        user_copy_timestamps[user_id] = [
            t for t in user_copy_timestamps[user_id]
            if current_time - t < 180
        ]
    else:
        user_copy_timestamps[user_id] = []

    if len(user_copy_timestamps[user_id]) >= 2:
        try:
            await message.delete()
        except Exception:
            pass
        return

    user_copy_timestamps[user_id].append(current_time)

    # 5. Пройдены все проверки: начинаем процесс копирования
    try:
        await message.delete()
    except Exception:
        pass

    processing_packs.add(pack_name)

    try:
        new_pack_name = await fast_copy_pack(pack_name)
        save_pack_mapping(pack_name, new_pack_name, is_manual=0)

        link = f"https://t.me/addstickers/{new_pack_name}"
        await send_and_cleanup_link(
            f"✅ Стикерпак очищен от рекламы и доступен по ссылке:\n👉 {link}"
        )
    except Exception as e:
        print(f"Ошибка при копировании пака {pack_name}: {e}")
    finally:
        processing_packs.discard(pack_name)


# --- Логика управления в ЛС Администратора ---
@dp.message((F.chat.id == ADMIN_ID) & (F.sticker | F.text))
async def admin_pm_handler(message: Message, state: FSMContext):
    # ЗАЩИТА ОТ КОМАНД
    if message.text and message.text.startswith('/'):
        current_state = await state.get_state()
        if current_state is not None:
            await state.clear()
            return await message.answer("🔄 Действие отменено, состояние сброшено.")
        if message.text == '/start':
            return await message.answer("👋 Привет, админ! Отправь мне стикер или ссылку на пак для настройки.")
        return

    target_pack = None
    if message.sticker and message.sticker.set_name:
        target_pack = message.sticker.set_name
    elif message.text:
        target_pack = extract_pack_name(message.text)

    if not target_pack:
        if message.chat.type == "private":
            await message.answer("❌ Это не похоже на пак. Отправь стикер из набора или ссылку на него.")
        return

    current_state = await state.get_state()
    data = await state.get_data()

    # РЕЖИМ 1: Авто-связь (Пакетная привязка пар)
    if current_state == AdminFSM.waiting_for_auto_link:
        orig_pack = data.get('pending_orig')

        if not orig_pack:
            await state.update_data(pending_orig=target_pack)
            return
        else:
            save_pack_mapping(orig_pack, target_pack, is_manual=1)
            await state.update_data(pending_orig=None)

            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{orig_pack}")
            ]])

            text = f"<a href='https://t.me/addstickers/{orig_pack}'>{orig_pack}</a> заменен на <a href='https://t.me/addstickers/{target_pack}'>{target_pack}</a>"
            return await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

    # РЕЖИМ 2: Ожидание второго пака (Ручная замена с пометкой)
    elif current_state == AdminFSM.waiting_for_second_pack:
        orig_pack = data['orig_pack']
        save_pack_mapping(orig_pack, target_pack, is_manual=1)
        await state.clear()
        return await message.answer(f"🔗 Связь успешно создана!\nОригинал: {orig_pack}\nЗамена: {target_pack}")

    # РЕЖИМ 3: Ожидание второго пака (Чистая замена)
    elif current_state == AdminFSM.waiting_for_clean_pack:
        orig_pack = data['orig_pack']
        save_pack_mapping(orig_pack, target_pack, is_manual=0)
        await state.clear()
        return await message.answer(f"♻️ Чистая связь успешно создана!\nОригинал: {orig_pack}\nЗамена: {target_pack}")

    # ОБЫЧНЫЙ РЕЖИМ (Главное меню)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скопировать (Авто)", callback_data=f"copy_{target_pack}")],
        [InlineKeyboardButton(text="🔗 Заменить на другой", callback_data=f"add_{target_pack}")],
        [InlineKeyboardButton(text="♻️ Чистая замена", callback_data=f"clean_{target_pack}")],
        [InlineKeyboardButton(text="🔄 Включить Авто-связь (Парами)", callback_data="start_auto_link")]
    ])

    await message.answer(f"Управление стикерпаком: <code>{target_pack}</code>", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("copy_"))
async def admin_action_copy(callback: CallbackQuery):
    pack_name = callback.data.split("copy_")[1]
    await callback.message.edit_text("⏳ Копирую стикерпак, подождите...")
    try:
        new_pack = await fast_copy_pack(pack_name)
        save_pack_mapping(pack_name, new_pack, is_manual=0)
        await callback.message.edit_text(f"✅ Успешно скопировано!\nНовая ссылка: https://t.me/addstickers/{new_pack}")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка копирования: {e}")


@dp.callback_query(F.data.startswith("add_"))
async def admin_action_add(callback: CallbackQuery, state: FSMContext):
    pack_name = callback.data.split("add_")[1]
    await state.set_state(AdminFSM.waiting_for_second_pack)
    await state.update_data(orig_pack=pack_name)
    await callback.message.edit_text(
        f"Оригинал: <code>{pack_name}</code>\n\nОтправь стикер для ЗАМЕНЫ (с пометкой админа).", parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("clean_"))
async def admin_action_clean(callback: CallbackQuery, state: FSMContext):
    pack_name = callback.data.split("clean_")[1]
    await state.set_state(AdminFSM.waiting_for_clean_pack)
    await state.update_data(orig_pack=pack_name)
    await callback.message.edit_text(
        f"Оригинал: <code>{pack_name}</code>\n\nОтправь стикер для ЧИСТОЙ ЗАМЕНЫ (в чате бот напишет, что пак очищен).",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "start_auto_link")
async def admin_start_auto_link(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.waiting_for_auto_link)
    await state.update_data(pending_orig=None)
    await callback.message.edit_text(
        "🔄 <b>Режим поточной Авто-связи Включен!</b>\n\n"
        "Теперь просто отправляйте мне стикеры парами:\n"
        "1️⃣ Стикер из <i>оригинала</i>\n"
        "2️⃣ Стикер из <i>вашего клона</i>\n\n"
        "Бот будет автоматически их связывать. Чтобы выйти, пропишите любую команду (например, /start).",
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_auto_link(callback: CallbackQuery):
    orig = callback.data.split("cancel_")[1]
    delete_pack_mapping(orig)
    await callback.message.edit_text(f"❌ Связь отменена для пака: <code>{orig}</code>", parse_mode="HTML")


# --- Запуск ---
async def main():
    init_db()
    print("Бот успешно запущен и готов к работе!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
