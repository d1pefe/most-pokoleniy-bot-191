import asyncio
import logging
import sqlite3
import os
from datetime import datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile,
    Message, CallbackQuery, InputMediaPhoto
)
from dotenv import load_dotenv
from openai import AsyncOpenAI  # Библиотека для ИИ

# Загружаем переменные из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MAIN_ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Инициализация ИИ клиента (если ключа нет, клиент не создастся)
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# --- БАЗА ДАННЫХ ---
DB_NAME = "school_bot_v2.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Пользователи + Роли (0=user, 1=admin, 2=super_admin)
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        role INTEGER DEFAULT 0, 
        join_date TEXT
    )''')
    
    # Мероприятия (+ фото)
    cursor.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        event_date TEXT,
        description TEXT,
        photo_id TEXT, 
        is_active BOOLEAN DEFAULT 1
    )''')
    
    # Настройки (текст приветствия)
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    # Вопросы и кейсы
    cursor.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        event_id INTEGER,
        type TEXT, 
        text TEXT,
        created_at TEXT
    )''')

    # Устанавливаем дефолтное приветствие, если его нет
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", 
                   ("welcome_text", "Добро пожаловать в 'Мост поколений'!"))
    
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_NAME)

# --- FSM (СОСТОЯНИЯ) ---
class AdminStates(StatesGroup):
    create_event_title = State()
    create_event_desc = State()
    create_event_date = State()
    create_event_photo = State()
    
    set_welcome_text = State()
    add_new_admin = State()
    broadcast_text = State()
    broadcast_photo = State()

class UserStates(StatesGroup):
    writing_question = State()

# --- КЛАВИАТУРЫ ---
def get_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Ближайшее мероприятие")],
        [KeyboardButton(text="💡 Предложить тему"), KeyboardButton(text="📚 О проекте")]
    ], resize_keyboard=True)

def get_admin_kb(user_id):
    buttons = [
        [InlineKeyboardButton(text="➕ Создать мероприятие", callback_data="admin_new_event")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="✍️ Изменить приветствие", callback_data="admin_set_welcome")],
        [InlineKeyboardButton(text="📊 Статистика и ИИ", callback_data="admin_stats")]
    ]
    if user_id == MAIN_ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="👮 Добавить админа", callback_data="admin_add_admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ЛОГИКА ИИ (НЕЙРООБРАБОТКА) ---
async def analyze_questions_with_ai(questions_list):
    if not ai_client:
        return "⚠️ Ошибка: API ключ OpenAI не настроен."
    
    if not questions_list:
        return "Нет вопросов для анализа."

    prompt = (
        f"Проанализируй эти {len(questions_list)} вопросов от участников школьной дискуссии.\n"
        "1. Выдели 3 основные темы, которые волнуют людей.\n"
        "2. Определи общее эмоциональное настроение (тревога, интерес, агрессия).\n"
        "3. Дай краткий совет модератору дискуссии.\n\n"
        "Список вопросов:\n" + "\n".join(f"- {q}" for q in questions_list[:50]) # Берем первые 50 чтобы не перегрузить
    )

    try:
        response = await ai_client.chat.completions.create(
            model="gpt-3.5-turbo", # Или gpt-4o, если бюджет позволяет
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Ошибка ИИ: {e}"

# --- БОТ И ХЕНДЛЕРЫ ---
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# 1. ПРИВЕТСТВИЕ И БЛИЖАЙШЕЕ СОБЫТИЕ
@dp.message(CommandStart())
async def start_cmd(message: Message):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Регистрируем/обновляем юзера
    role = 2 if message.from_user.id == MAIN_ADMIN_ID else 0
    cursor.execute("INSERT OR IGNORE INTO users (user_id, full_name, role, join_date) VALUES (?, ?, ?, ?)",
                   (message.from_user.id, message.from_user.full_name, role, datetime.now().isoformat()))
    
    # Берем приветствие
    cursor.execute("SELECT value FROM settings WHERE key='welcome_text'")
    welcome_text = cursor.fetchone()[0]
    
    # Ищем ближайшее мероприятие (дата >= сегодня)
    cursor.execute("SELECT title, event_date FROM events WHERE is_active=1 ORDER BY id DESC LIMIT 1")
    event = cursor.fetchone()
    
    conn.close()

    response = f"{welcome_text}\n\n"
    if event:
        response += f"🗓 <b>Ближайшая встреча:</b> {event[0]} ({event[1]})"
    else:
        response += "Пока нет запланированных встреч."

    await message.answer(response, reply_markup=get_main_menu())

# 2. АДМИНКА
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE user_id=?", (message.from_user.id,))
    res = cursor.fetchone()
    conn.close()

    if res and res[0] > 0: # Если админ (1) или супер-админ (2)
        await message.answer("🛠 Панель управления:", reply_markup=get_admin_kb(message.from_user.id))
    else:
        await message.answer("У вас нет прав администратора.")

# Настройка приветствия
@dp.callback_query(F.data == "admin_set_welcome")
async def ask_welcome_text(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите новый текст приветствия для пользователей:")
    await state.set_state(AdminStates.set_welcome_text)

@dp.message(AdminStates.set_welcome_text)
async def save_welcome_text(message: Message, state: FSMContext):
    conn = get_db_connection()
    conn.execute("UPDATE settings SET value=? WHERE key='welcome_text'", (message.text,))
    conn.commit()
    conn.close()
    await message.answer("✅ Приветствие обновлено!")
    await state.clear()

# Добавление админа
@dp.callback_query(F.data == "admin_add_admin")
async def ask_new_admin_id(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите ID пользователя (число) или перешлите сообщение от него, чтобы сделать его админом:")
    await state.set_state(AdminStates.add_new_admin)

@dp.message(AdminStates.add_new_admin)
async def save_new_admin(message: Message, state: FSMContext):
    try:
        if message.forward_from:
            new_admin_id = message.forward_from.id
        else:
            new_admin_id = int(message.text)
            
        conn = get_db_connection()
        conn.execute("UPDATE users SET role=1 WHERE user_id=?", (new_admin_id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Пользователь {new_admin_id} теперь администратор.")
    except Exception:
        await message.answer("❌ Ошибка. Введите корректный ID.")
    await state.clear()

# Создание события с ФОТО
@dp.callback_query(F.data == "admin_new_event")
async def start_event_creation(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("1. Введите название мероприятия:")
    await state.set_state(AdminStates.create_event_title)

@dp.message(AdminStates.create_event_title)
async def event_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("2. Введите описание (можно с эмодзи):")
    await state.set_state(AdminStates.create_event_desc)

@dp.message(AdminStates.create_event_desc)
async def event_desc(message: Message, state: FSMContext):
    await state.update_data(desc=message.text)
    await message.answer("3. Введите дату (например, '20 Февраля, 18:00'):")
    await state.set_state(AdminStates.create_event_date)

@dp.message(AdminStates.create_event_date)
async def event_date(message: Message, state: FSMContext):
    await state.update_data(date=message.text)
    await message.answer("4. Пришлите фото для афиши (или напишите 'нет', если без фото):")
    await state.set_state(AdminStates.create_event_photo)

@dp.message(AdminStates.create_event_photo)
async def event_finish(message: Message, state: FSMContext):
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id # Берем самое лучшее качество
    
    data = await state.get_data()
    
    conn = get_db_connection()
    conn.execute("INSERT INTO events (title, description, event_date, photo_id) VALUES (?, ?, ?, ?)",
                 (data['title'], data['desc'], data['date'], photo_id))
    conn.commit()
    conn.close()
    
    await message.answer("✅ Мероприятие создано!")
    await state.clear()

# Статистика и ИИ
@dp.callback_query(F.data == "admin_stats")
async def show_stats_menu(cb: CallbackQuery):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title FROM events ORDER BY id DESC LIMIT 5")
    events = cursor.fetchall()
    conn.close()
    
    buttons = []
    for evt in events:
        buttons.append([InlineKeyboardButton(text=f"🧠 Анализ ИИ: {evt[1]}", callback_data=f"ai_analyze_{evt[0]}")])
    
    await cb.message.answer("Выберите мероприятие для нейро-анализа:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("ai_analyze_"))
async def run_ai_analysis(cb: CallbackQuery):
    event_id = cb.data.split("_")[2]
    await cb.message.answer("⏳ Собираю вопросы и отправляю нейросети... Подождите 10-20 секунд.")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    # Берем вопросы
    cursor.execute("SELECT text FROM submissions WHERE event_id=? AND type='question'", (event_id,))
    questions = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    # Запускаем ИИ
    ai_result = await analyze_questions_with_ai(questions)
    
    await cb.message.answer(f"🤖 **Результат анализа:**\n\n{ai_result}", parse_mode="Markdown")

# Запуск
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped")
