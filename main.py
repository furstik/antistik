import os
import re
import uuid
import sqlite3
import asyncio
import time
import yadisk
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputSticker
from aiogram.filters import Command
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
YADISK_TOKEN = os.getenv("YADISK_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Инициализация Яндекс Диска ---
# Используем асинхронный клиент
y = yadisk.AsyncClient(token=YADISK_TOKEN)
BACKUP_DIR = "/bot_backups"
BACKUP_PATH = f"{BACKUP_DIR}/stickers.db"

db_changes_count = 0  # Счетчик изменений БД

# Блокировка для предотвращения одновременного копирования одного и того же пака
processing_packs = set()
# Хранение ID последних отправленных ссылок: {(chat_id, original_pack_name): message_id}
last_bot_messages = {}
user_copy_timestamps = {}


# --- Функции работы с Яндекс Диском ---
async def download_db_from_yadisk():
    """Скачивает резервную копию при старте бота."""
    try:
        if await y.check_token():
            if not await y.exists(BACKUP_DIR):
                await y.mkdir(BACKUP_DIR)
            
            if await y.exists(BACKUP_PATH):
                await y.download(BACKUP_PATH, "stickers.db")
                print("✅ [Yandex Disk] Актуальная БД 'stickers.db' успешно загружена с облака.")
            else:
                print("⚠️ [Yandex Disk] Резервная копия не найдена. Будет создана новая локальная БД.")
        else:
            print("❌ [Yandex Disk] Неверный токен Яндекс Диска!")
    except Exception as e:
        print(f"❌ [Yandex Disk] Ошибка при стартовой загрузке БД: {e}")

async def upload_db_to_yadisk():
    """Загружает локальную базу на Яндекс Диск (перезаписывает)."""
    try:
        if not await y.exists(BACKUP_DIR):
            await y.mkdir(BACKUP_DIR)
        await y.upload("stickers.db", BACKUP_PATH, overwrite=True)
        return True
    except Exception as e:
        print(f"❌ [Yandex Disk] Ошибка загрузки бэкапа: {e}")
        return False

async def check_and_backup():
    """Счетчик изменений. Делает бэкап каждые 5 изменений."""
    global db_changes_count
    db_changes_count += 1
    if db_changes_count >= 5:
        success = await upload_db_to_yadisk()
        if success:
            print("✅ [Yandex Disk] Автоматический бэкап (5 изменений) выполнен успешно.")
            db_changes_count = 0


# --- База данных SQLite ---
def init_db():
    conn = sqlite3.connect("stickers.db")
    cursor = conn.cursor()
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
    conn = sqlite3.connect("stickers.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM packs WHERE target_name = ?", (pack_name,))
    res = cursor.fetchone()
    conn.close()
    return bool(res)

def get_pack_mapping(original_name: str):
    conn = sqlite3.connect("stickers.db")
    cursor = conn.cursor()
    cursor.execute("SELECT target_name, is_manual FROM packs WHERE original_name = ?", (original_name,))
    res = cursor.fetchone()
    conn.close()
    return res

def delete_pack_mapping(original_name: str):
    conn = sqlite3.connect("stickers.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM packs WHERE original_name = ?", (original_name,))
    conn.commit()
    conn.close()

def save_pack_mapping(original_name: str, target_name: str, is_manual: int = 0):
    conn = sqlite3.connect("stickers.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO packs (original_name, target_name, is_manual) VALUES (?, ?, ?)",
        (original_name, target_name, is_manual)
    )
    conn.commit()
    conn.close()


# --- Машина состояний для Админки ---
class AdminFSM(StatesGroup):
    waiting_for_second_pack = State() 
    waiting_for_clean_pack = State()  
    waiting_for_auto_link = State()   


# --- Вспомогательные функции ---
def extract_pack_name(text: str) -> str:
    if not text: return None
    match = re.search(r'addstickers/(.+)', text)
    return match.group(1) if match else text.strip()

async def fast_copy_pack(original_name: str) -> str:
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
            await bot.add_sticker_to_set(user_id=TARGET_USER_ID, name=new_name, sticker=stk)
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await bot.add_sticker_to_set(user_id=TARGET_USER_ID, name=new_name, sticker=stk)

    return new_name


# --- Ручной Бэкап (Команда /clone) ---
@dp.message(Command("clone"), F.chat.id == ADMIN_ID)
async def cmd_clone_backup(message: Message):
    global db_changes_count
    msg = await message.answer("🔄 Выполняю ручное резервное копирование БД на Яндекс Диск...")
    
    success = await upload_db_to_yadisk()
    if success:
        db_changes_count = 0  # Сбрасываем счетчик, так как бэкап свежий
        await msg.edit_text("✅ Резервная копия `stickers.db` успешно сохранена на Яндекс Диске!", parse_mode="Markdown")
    else:
        await msg.edit_text("❌ Ошибка при сохранении резервной копии. Проверьте логи сервера.")


@dp.message(F.chat.id.in_(MONITOR_CHATS) & F.sticker)
async def process_chat_sticker(message: Message):
    pack_name = message.sticker.set_name
    if not pack_name: return

    if is_target_pack(pack_name): return

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

    if pack_name in processing_packs:
        try: await message.delete()
        except Exception: pass
        return

    original_set = await bot.get_sticker_set(pack_name)
    if any(word in original_set.title.lower() for word in IGNORE_WORDS):
        return

    current_time = time.time()
    if user_id in user_copy_timestamps:
        user_copy_timestamps[user_id] = [t for t in user_copy_timestamps[user_id] if current_time - t < 180]
    else:
        user_copy_timestamps[user_id] = []

    if len(user_copy_timestamps[user_id]) >= 2:
        try: await message.delete()
        except Exception: pass
        return 

    user_copy_timestamps[user_id].append(current_time)

    try: await message.delete()
    except Exception: pass

    processing_packs.add(pack_name)

    try:
        new_pack_name = await fast_copy_pack(pack_name)
        
        save_pack_mapping(pack_name, new_pack_name, is_manual=0)
        await check_and_backup() # Проверка на авто-бэкап после изменения БД

        link = f"https://t.me/addstickers/{new_pack_name}"
        await send_and_cleanup_link(
            f"✅ Стикерпак очищен от рекламы и доступен по ссылке:\n👉 {link}"
        )
    except Exception as e:
        print(f"Ошибка при копировании пака {pack_name}: {e}")
    finally:
        processing_packs.discard(pack_name)


@dp.message((F.chat.id == ADMIN_ID) & (F.sticker | F.text))
async def admin_pm_handler(message: Message, state: FSMContext):
    if message.text and message.text.startswith('/'):
        # Игнорируем команду /clone, чтобы она не сбрасывала состояния
        if message.text == '/clone': return 
        
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

    if current_state == AdminFSM.waiting_for_auto_link:
        orig_pack = data.get('pending_orig')
        if not orig_pack:
            await state.update_data(pending_orig=target_pack)
            return
        else:
            save_pack_mapping(orig_pack, target_pack, is_manual=1)
            await check_and_backup() # Проверка на авто-бэкап после изменения БД
            await state.update_data(pending_orig=None) 

            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{orig_pack}")
            ]])

            text = f"<a href='https://t.me/addstickers/{orig_pack}'>{orig_pack}</a> заменен на <a href='https://t.me/addstickers/{target_pack}'>{target_pack}</a>"
            return await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

    elif current_state == AdminFSM.waiting_for_second_pack:
        orig_pack = data['orig_pack']
        save_pack_mapping(orig_pack, target_pack, is_manual=1)
        await check_and_backup() # Проверка на авто-бэкап после изменения БД
        await state.clear()
        return await message.answer(f"🔗 Связь успешно создана!\nОригинал: {orig_pack}\nЗамена: {target_pack}")

    elif current_state == AdminFSM.waiting_for_clean_pack:
        orig_pack = data['orig_pack']
        save_pack_mapping(orig_pack, target_pack, is_manual=0)
        await check_and_backup() # Проверка на авто-бэкап после изменения БД
        await state.clear()
        return await message.answer(f"♻️ Чистая связь успешно создана!\nОригинал: {orig_pack}\nЗамена: {target_pack}")

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
        await check_and_backup() # Проверка на авто-бэкап после изменения БД
        
        await callback.message.edit_text(f"✅ Успешно скопировано!\nНовая ссылка: https://t.me/addstickers/{new_pack}")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка копирования: {e}")

@dp.callback_query(F.data.startswith("add_"))
async def admin_action_add(callback: CallbackQuery, state: FSMContext):
    pack_name = callback.data.split("add_")[1]
    await state.set_state(AdminFSM.waiting_for_second_pack)
    await state.update_data(orig_pack=pack_name)
    await callback.message.edit_text(f"Оригинал: <code>{pack_name}</code>\n\nОтправь стикер для ЗАМЕНЫ (с пометкой админа).", parse_mode="HTML")

@dp.callback_query(F.data.startswith("clean_"))
async def admin_action_clean(callback: CallbackQuery, state: FSMContext):
    pack_name = callback.data.split("clean_")[1]
    await state.set_state(AdminFSM.waiting_for_clean_pack)
    await state.update_data(orig_pack=pack_name)
    await callback.message.edit_text(f"Оригинал: <code>{pack_name}</code>\n\nОтправь стикер для ЧИСТОЙ ЗАМЕНЫ.", parse_mode="HTML")

@dp.callback_query(F.data == "start_auto_link")
async def admin_start_auto_link(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.waiting_for_auto_link)
    await state.update_data(pending_orig=None)
    await callback.message.edit_text(
        "🔄 <b>Режим поточной Авто-связи Включен!</b>\n\n"
        "Теперь просто отправляйте мне стикеры парами...", parse_mode="HTML")

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_auto_link(callback: CallbackQuery):
    orig = callback.data.split("cancel_")[1]
    delete_pack_mapping(orig) 
    await check_and_backup() # Проверка на авто-бэкап после изменения (удаления)
    await callback.message.edit_text(f"❌ Связь отменена для пака: <code>{orig}</code>", parse_mode="HTML")

# --- Запуск ---
async def main():
    # 1. Скачиваем актуальную БД с облака ПЕРЕД инициализацией БД и запуском бота
    await download_db_from_yadisk()
    
    # 2. Инициализируем локальную (возможно, только что скачанную) БД
    init_db()
    
    print("Бот успешно запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot)
    finally:
        # Важно закрывать клиентское подключение при выключении бота
        await y.close()

if __name__ == "__main__":
    asyncio.run(main())
