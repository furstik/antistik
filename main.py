import os
import re
import uuid
import sqlite3
import asyncio
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

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Блокировка для предотвращения одновременного копирования одного и того же пака
processing_packs = set()
# Хранение ID последних отправленных ссылок: {(chat_id, original_pack_name): message_id}
last_bot_messages = {}


# --- База данных SQLite ---
def init_db():
    conn = sqlite3.connect("stickers.db")
    cursor = conn.cursor()
    # original_name: Имя оригинального пака
    # target_name: Имя скопированного пака или привязанного вручную
    # is_manual: 0 - авто-копия, 1 - привязан вручную админом
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


# --- Вспомогательные функции ---
def extract_pack_name(text: str) -> str:
    """Извлекает short_name пака из ссылки или возвращает текст, если это уже имя"""
    if not text: return None
    match = re.search(r'addstickers/(.+)', text)
    return match.group(1) if match else text.strip()


async def fast_copy_pack(original_name: str) -> str:
    """Логика быстрого копирования пака через file_id"""
    original_set = await bot.get_sticker_set(original_name)
    bot_info = await bot.get_me()

    # Очистка названия от рекламы
    clean_title = original_set.title.replace("@", "").replace("t.me", "")

    # Генерация уникального имени для нового пака
    # Правило Telegram: имя должно оканчиваться на _by_ИмяБота
    new_name = f"cp_{uuid.uuid4().hex[:8]}_by_{bot_info.username}"

    # Определяем формат стикеров
    if original_set.is_animated:
        st_format = "animated"
    elif original_set.is_video:
        st_format = "video"
    else:
        st_format = "static"

    # Подготовка объектов InputSticker (ТЕПЕРЬ ФОРМАТ УКАЗЫВАЕТСЯ ЗДЕСЬ)
    input_stickers = [
        InputSticker(
            sticker=stk.file_id,
            format=st_format,
            emoji_list=[stk.emoji] if stk.emoji else ["📌"]
        )
        for stk in original_set.stickers
    ]

    # API позволяет за раз создать пак максимум с 50 стикерами
    initial_batch = input_stickers[:50]
    remaining_batch = input_stickers[50:]

    # Создаем пак (ТУТ ПАРАМЕТР sticker_format БОЛЬШЕ НЕ НУЖЕН)
    await bot.create_new_sticker_set(
        user_id=TARGET_USER_ID,
        name=new_name,
        title=clean_title,
        stickers=initial_batch
    )

    # Если в паке больше 50 стикеров, докидываем по одному
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

    # ДОБАВЛЕНО: возвращаем имя созданного пака
    return new_name


@dp.message(F.chat.id.in_(MONITOR_CHATS) & F.sticker)
async def process_chat_sticker(message: Message):
    pack_name = message.sticker.set_name
    if not pack_name:
        return  # Это одиночный стикер-картинка, игнорируем

    # --- ДОБАВЛЕНО: Проверка на "чистый" пак ---
    # Если это уже наш клон или ручная замена — просто разрешаем его и ничего не делаем
    if is_target_pack(pack_name):
        return
    # -------------------------------------------

    chat_id = message.chat.id



    # Вспомогательная локальная функция для отправки ссылки с удалением предыдущей
    async def send_and_cleanup_link(text: str):
        # Удаляем предыдущее сообщение бота с этой же ссылкой в этом чате
        if (chat_id, pack_name) in last_bot_messages:
            try:
                await bot.delete_message(chat_id, last_bot_messages[(chat_id, pack_name)])
            except Exception:
                pass # Сообщение могло быть уже удалено пользователем/админом

        # Отправляем новое и сохраняем его ID
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
            text = f"🛡 Этот стикерпак имеется в канале @FurriStik \n👉 {link}"
        else:
            text = f"✅ Стикерпак очищен от рекламы и доступен по ссылке:\n👉 {link}"

        await send_and_cleanup_link(text)
        return

    # 2. ЗАЩИТА ОТ ГОНКИ: Если пак прямо сейчас копируется соседним процессом
    if pack_name in processing_packs:
        try:
            await message.delete() # Просто удаляем стикер
        except Exception:
            pass
        return  # Ничего не делаем, ждем пока первый процесс закончит и выдаст ссылку

    # 3. Проверка на игнор-слова
    original_set = await bot.get_sticker_set(pack_name)
    if any(word in original_set.title.lower() for word in IGNORE_WORDS):
        return  # Игнорируем (не удаляем и не копируем)

    # 4. Пройдены все проверки: начинаем процесс копирования
    try:
        await message.delete()
    except Exception:
        pass

    # Блокируем пак, чтобы другие стикеры из него не запустили копирование
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
        # Обязательно снимаем блокировку, даже если произошла ошибка
        processing_packs.discard(pack_name)

# --- Логика управления в ЛС Администратора ---
@dp.message((F.chat.id == ADMIN_ID) & (F.sticker | F.text))
async def admin_pm_handler(message: Message, state: FSMContext):
    # Если мы ждем второй стикерпак для "добавления"
    current_state = await state.get_state()
    if current_state == AdminFSM.waiting_for_second_pack:
        target_pack = None
        if message.sticker and message.sticker.set_name:
            target_pack = message.sticker.set_name
        elif message.text:
            target_pack = extract_pack_name(message.text)

        if not target_pack:
            return await message.answer("❌ Это не похоже на пак. Отправь стикер из набора или ссылку на него.")

        data = await state.get_data()
        orig_pack = data['orig_pack']

        save_pack_mapping(orig_pack, target_pack, is_manual=1)
        await state.clear()
        return await message.answer(f"🔗 Связь успешно создана!\n\nОригинал: {orig_pack}\nЗамена: {target_pack}")

    # Обычный режим управления
    pack_name = None
    if message.sticker and message.sticker.set_name:
        pack_name = message.sticker.set_name
    elif message.text and "addstickers" in message.text:
        pack_name = extract_pack_name(message.text)

    if not pack_name:
        if message.chat.type == "private":
            await message.answer("Отправь мне стикер из пака или ссылку на него.")
        return

    # Клавиатура действий
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скопировать (Авто)", callback_data=f"copy_{pack_name}")],
        [InlineKeyboardButton(text="🔗 Заменить на другой (Добавить)", callback_data=f"add_{pack_name}")]
    ])

    await message.answer(f"Управление стикерпаком: <code>{pack_name}</code>", reply_markup=kb, parse_mode="HTML")


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
        f"Оригинал выбран: <code>{pack_name}</code>\n\n"
        "Теперь отправь мне стикер ИЛИ ссылку на стикерпак, который нужно выдавать ВМЕСТО этого (замена).",
        parse_mode="HTML"
    )


# --- Запуск ---
async def main():
    init_db()
    print("Бот успешно запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
