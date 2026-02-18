import asyncio
import logging
import sqlite3
import os
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, 
    Message, CallbackQuery, FSInputFile
)
from openai import AsyncOpenAI

# --- НАСТРОЙКИ ---
BOT_TOKEN = "7802243169:AAHmow-BnBE9T5PK5FxrbyQnf4caklqmB9c"
OPENAI_API_KEY = "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"
MAIN_ADMIN_ID = 7199344406 

# Инициализация
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler() # Планировщик задач
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА ДАННЫХ ---
DB_NAME = "school_bot_final.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Пользователи
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        role INTEGER DEFAULT 0, -- 0=User, 1=Admin, 2=SuperAdmin
        join_date TEXT
    )''')
    
    # Мероприятия
    cursor.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        description TEXT,
        event_date TEXT,
        photo_id TEXT,
        is_active BOOLEAN DEFAULT 1
    )''')
    
    # Ответы (Вопросы/Кейсы/Темы)
    cursor.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        event_id INTEGER, -- NULL если это общая тема
        type TEXT, -- question, case, topic
        text TEXT,
        created_at TEXT
    )''')

    # Регистрации
    cursor.execute('''CREATE TABLE IF NOT EXISTS registrations (
        user_id INTEGER,
        event_id INTEGER,
        registered_at TEXT,
        PRIMARY KEY (user_id, event_id)
    )''')

    # Гарантируем, что главный админ есть в базе с ролью 2
    cursor.execute("INSERT OR IGNORE INTO users (user_id, role) VALUES (?, 2)", (MAIN_ADMIN_ID,))
    cursor.execute("UPDATE users SET role=2 WHERE user_id=?", (MAIN_ADMIN_ID,))
    
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_NAME)

# --- FSM (Машина состояний) ---
class AdminStates(StatesGroup):
    new_event_title = State()
    new_event_desc = State()
    new_event_date = State()
    new_event_photo = State()
    broadcast_schedule = State() # Ввод времени для рассылки
    add_admin = State()

class UserStates(StatesGroup):
    writing_question = State()
    writing_case = State()
    writing_topic = State()

# --- КЛАВИАТУРЫ ---
def kb_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Ближайшее мероприятие")],
        [KeyboardButton(text="💡 Предложить тему"), KeyboardButton(text="📜 Моя ситуация")],
        [KeyboardButton(text="📚 О проекте")]
    ], resize_keyboard=True)

def kb_admin_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать мероприятие", callback_data="adm_create")],
        [InlineKeyboardButton(text="📢 Запланировать рассылку", callback_data="adm_broadcast_menu")],
        [InlineKeyboardButton(text="📂 Смотреть ответы", callback_data="adm_view_answers")],
        [InlineKeyboardButton(text="👮 Добавить админа", callback_data="adm_add_admin")]
    ])

def kb_event_actions(event_id):
    """Кнопки под постом о мероприятии"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зарегистрироваться", callback_data=f"reg_{event_id}")],
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data=f"ask_{event_id}")],
        [InlineKeyboardButton(text="📝 Рассказать кейс", callback_data=f"case_{event_id}")]
    ])

# --- ЛОГИКА ИИ ---
async def ai_analyze(text_data):
    if not ai_client: return "⚠️ ИИ не подключен (нет ключа)."
    prompt = (
        "Ты помощник организатора дискуссионного клуба. "
        "Проанализируй эти сообщения пользователей. "
        "1. Выдели 3 главные боли/проблемы. "
        "2. Оцени эмоциональный фон. "
        "3. Предложи 1 провокационный вопрос для начала дискуссии.\n\n"
        f"Данные:\n{text_data[:3000]}" # Обрезаем, чтобы не превысить лимиты
    )
    try:
        resp = await ai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"Ошибка ИИ: {e}"

# --- ФУНКЦИЯ РАССЫЛКИ (Вызывается планировщиком) ---
async def send_broadcast_task(event_id):
    conn = get_db()
    # Получаем данные события
    event = conn.execute("SELECT title, description, event_date, photo_id FROM events WHERE id=?", (event_id,)).fetchone()
    # Получаем всех пользователей
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    if not event: return

    text = (
        f"🔔 <b>Напоминание о встрече!</b>\n\n"
        f"Тема: <b>{event[0]}</b>\n"
        f"Когда: {event[2]}\n\n"
        f"{event[1]}\n\n"
        "👇 Выберите действие ниже:"
    )
    kb = kb_event_actions(event_id)
    
    count = 0
    logger.info(f"Начинаю авто-рассылку для события {event_id}")
    
    for user in users:
        try:
            if event[3]:
                await bot.send_photo(user[0], event[3], caption=text, reply_markup=kb)
            else:
                await bot.send_message(user[0], text, reply_markup=kb)
            count += 1
            await asyncio.sleep(0.05) # Анти-спам задержка
        except Exception as e:
            logger.error(f"Не удалось отправить юзеру {user[0]}: {e}")

    # Уведомляем админа об успехе
    try:
        await bot.send_message(MAIN_ADMIN_ID, f"✅ Авто-рассылка завершена! Отправлено: {count}")
    except:
        pass


# --- ХЕНДЛЕРЫ: СТАРТ И МЕНЮ ---
@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    conn = get_db()
    
    # Проверка: если это ГЛАВНЫЙ АДМИН, сразу даем роль 2
    role = 2 if user_id == MAIN_ADMIN_ID else 0
    
    # Обновляем или вставляем
    conn.execute("""
        INSERT INTO users (user_id, username, full_name, role, join_date) 
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username
    """, (user_id, message.from_user.username, message.from_user.full_name, role, datetime.now().isoformat()))
    
    # Если это админ, форсируем обновление роли (на случай старой базы)
    if user_id == MAIN_ADMIN_ID:
        conn.execute("UPDATE users SET role=2 WHERE user_id=?", (user_id,))

    conn.commit()
    conn.close()
    
    await message.answer(f"Привет, {message.from_user.full_name}! Это бот 'Мост поколений'.", reply_markup=kb_main_menu())

# --- ХЕНДЛЕРЫ: ПОЛЬЗОВАТЕЛЬСКИЕ ---
@dp.message(F.text == "📚 О проекте")
async def about(message: Message):
    await message.answer("Мы создаем диалог между поколениями. Здесь можно обсудить проблемы школы, семьи и общения.")

@dp.message(F.text == "💡 Предложить тему")
async def suggest_topic(message: Message, state: FSMContext):
    await message.answer("Какую тему вы хотите обсудить в будущем?")
    await state.set_state(UserStates.writing_topic)

@dp.message(F.text == "📜 Моя ситуация")
async def my_case(message: Message, state: FSMContext):
    await message.answer("Опишите вашу ситуацию анонимно. Мы сохраним её для анализа.")
    await state.set_state(UserStates.writing_case)

@dp.message(F.text == "📅 Ближайшее мероприятие")
async def nearest_event(message: Message):
    conn = get_db()
    event = conn.execute("SELECT id, title, description, event_date, photo_id FROM events WHERE is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    
    if not event:
        await message.answer("Пока нет анонсов.")
        return
        
    text = f"🗓 <b>{event[1]}</b>\n🕒 {event[3]}\n\n{event[2]}"
    kb = kb_event_actions(event[0])
    
    if event[4]:
        await message.answer_photo(event[4], caption=text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)

# Обработка ввода текста от юзеров
@dp.message(UserStates.writing_topic)
@dp.message(UserStates.writing_case)
@dp.message(UserStates.writing_question)
async def save_user_input(message: Message, state: FSMContext):
    st = await state.get_state()
    data = await state.get_data()
    event_id = data.get('event_id') # Может быть None
    
    type_map = {
        UserStates.writing_topic: 'topic',
        UserStates.writing_case: 'case',
        UserStates.writing_question: 'question'
    }
    submission_type = type_map.get(st, 'unknown')
    
    conn = get_db()
    conn.execute("INSERT INTO submissions (user_id, event_id, type, text, created_at) VALUES (?, ?, ?, ?, ?)",
                 (message.from_user.id, event_id, submission_type, message.text, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    await message.answer("Спасибо! Ваше сообщение принято.", reply_markup=kb_main_menu())
    await state.clear()

# Кнопки под ивентом (регистрация, вопросы)
@dp.callback_query(F.data.startswith("reg_"))
async def cb_reg(cb: CallbackQuery):
    eid = cb.data.split("_")[1]
    conn = get_db()
    try:
        conn.execute("INSERT INTO registrations (user_id, event_id, registered_at) VALUES (?, ?, ?)",
                     (cb.from_user.id, eid, datetime.now().isoformat()))
        conn.commit()
        await cb.answer("✅ Вы записаны!", show_alert=True)
    except sqlite3.IntegrityError:
        await cb.answer("Вы уже записаны.", show_alert=True)
    conn.close()

@dp.callback_query(F.data.startswith("ask_"))
async def cb_ask(cb: CallbackQuery, state: FSMContext):
    eid = cb.data.split("_")[1]
    await state.update_data(event_id=eid)
    # ИСПРАВЛЕНИЕ: используем cb.message вместо message
    await cb.message.answer("Напишите ваш вопрос спикерам этого мероприятия:")
    await state.set_state(UserStates.writing_question)
    await cb.answer()

@dp.callback_query(F.data.startswith("case_"))
async def cb_case(cb: CallbackQuery, state: FSMContext):
    eid = cb.data.split("_")[1]
    await state.update_data(event_id=eid)
    await cb.message.answer("Опишите ситуацию для разбора на этом мероприятии:")
    await state.set_state(UserStates.writing_case)
    await cb.answer()


# --- ХЕНДЛЕРЫ: АДМИНКА ---
@dp.message(Command("admin"))
async def admin_start(message: Message):
    conn = get_db()
    # Жесткая проверка: либо ID совпадает с MAIN, либо роль в базе > 0
    user = conn.execute("SELECT role FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if message.from_user.id == MAIN_ADMIN_ID or (user and user[0] > 0):
        await message.answer("Добро пожаловать в админку!", reply_markup=kb_admin_main())
    else:
        await message.answer(f"⛔️ Отказано в доступе. Ваш ID: {message.from_user.id}")

# 1. Создание
@dp.callback_query(F.data == "adm_create")
async def adm_create(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Название мероприятия?")
    await state.set_state(AdminStates.new_event_title)

@dp.message(AdminStates.new_event_title)
async def adm_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    await m.answer("Описание?")
    await state.set_state(AdminStates.new_event_desc)

@dp.message(AdminStates.new_event_desc)
async def adm_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.text)
    await m.answer("Дата проведения (текстом, напр. '25 Мая 18:00'):")
    await state.set_state(AdminStates.new_event_date)

@dp.message(AdminStates.new_event_date)
async def adm_date(m: Message, state: FSMContext):
    await state.update_data(date=m.text)
    await m.answer("Пришлите картинку (или напишите 'нет'):")
    await state.set_state(AdminStates.new_event_photo)

@dp.message(AdminStates.new_event_photo)
async def adm_finish(m: Message, state: FSMContext):
    photo = m.photo[-1].file_id if m.photo else None
    d = await state.get_data()
    conn = get_db()
    conn.execute("INSERT INTO events (title, description, event_date, photo_id) VALUES (?, ?, ?, ?)",
                 (d['title'], d['desc'], d['date'], photo))
    conn.commit()
    conn.close()
    await m.answer("✅ Мероприятие создано!", reply_markup=kb_admin_main())
    await state.clear()

# 2. Планирование рассылки
@dp.callback_query(F.data == "adm_broadcast_menu")
async def adm_cast_menu(cb: CallbackQuery):
    conn = get_db()
    events = conn.execute("SELECT id, title FROM events WHERE is_active=1 ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    btns = [[InlineKeyboardButton(text=f"⏰ {e[1]}", callback_data=f"sched_{e[0]}")] for e in events]
    await cb.message.answer("Выберите мероприятие для АВТО-рассылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("sched_"))
async def adm_ask_time(cb: CallbackQuery, state: FSMContext):
    eid = cb.data.split("_")[1]
    await state.update_data(event_id=eid)
    await cb.message.answer(
        "Введите дату и время отправки рассылки в формате:\n"
        "<b>YYYY-MM-DD HH:MM</b>\n"
        "Пример: 2024-05-20 14:30\n"
        "(Часовой пояс сервера!)"
    )
    await state.set_state(AdminStates.broadcast_schedule)

@dp.message(AdminStates.broadcast_schedule)
async def adm_set_schedule(m: Message, state: FSMContext):
    try:
        run_date = datetime.strptime(m.text, "%Y-%m-%d %H:%M")
        data = await state.get_data()
        event_id = data['event_id']
        
        # Добавляем задачу в планировщик
        scheduler.add_job(send_broadcast_task, 'date', run_date=run_date, args=[event_id])
        
        await m.answer(f"✅ Рассылка запланирована на {run_date}!", reply_markup=kb_admin_main())
        await state.clear()
    except ValueError:
        await m.answer("❌ Ошибка формата. Попробуйте еще раз: YYYY-MM-DD HH:MM")

# 3. Просмотр ответов + ИИ
@dp.callback_query(F.data == "adm_view_answers")
async def adm_view(cb: CallbackQuery):
    conn = get_db()
    events = conn.execute("SELECT id, title FROM events ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    btns = [[InlineKeyboardButton(text=f"📂 {e[1]}", callback_data=f"data_{e[0]}")] for e in events]
    await cb.message.answer("По какому событию показать данные?", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("data_"))
async def adm_show_data(cb: CallbackQuery):
    eid = cb.data.split("_")[1]
    conn = get_db()
    
    # Считаем регистрации
    reg_count = conn.execute("SELECT count(*) FROM registrations WHERE event_id=?", (eid,)).fetchone()[0]
    
    # Берем вопросы
    questions = conn.execute("SELECT text FROM submissions WHERE event_id=? AND type='question'", (eid,)).fetchall()
    cases = conn.execute("SELECT text FROM submissions WHERE event_id=? AND type='case'", (eid,)).fetchall()
    conn.close()
    
    text_report = f"📊 <b>Отчет по мероприятию</b>\nЗаписей: {reg_count}\nВопросов: {len(questions)}\nКейсов: {len(cases)}\n\n"
    
    # Собираем текст для ИИ
    full_text = "Вопросы:\n" + "\n".join([q[0] for q in questions]) + "\n\nКейсы:\n" + "\n".join([c[0] for c in cases])
    
    if len(full_text) > 20:
        btn_ai = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧠 Нейро-анализ", callback_data=f"ai_{eid}")]])
        await cb.message.answer(text_report + "Нажмите кнопку для анализа содержимого.", reply_markup=btn_ai)
        # Отправляем файл или длинный текст (здесь просто текст для примера)
        if len(full_text) < 4000:
            await cb.message.answer(f"📜 <b>Данные:</b>\n{full_text}")
        else:
            await cb.message.answer("Данных слишком много, показаны первые 4000 символов.")
            await cb.message.answer(full_text[:4000])
    else:
        await cb.message.answer(text_report + "Данных пока нет.")

@dp.callback_query(F.data.startswith("ai_"))
async def adm_run_ai(cb: CallbackQuery):
    eid = cb.data.split("_")[1]
    await cb.message.answer("⏳ Думаю...")
    conn = get_db()
    questions = conn.execute("SELECT text FROM submissions WHERE event_id=?", (eid,)).fetchall()
    conn.close()
    full_text = "\n".join([q[0] for q in questions])
    
    res = await ai_analyze(full_text)
    await cb.message.answer(res)

# 4. Добавить админа
@dp.callback_query(F.data == "adm_add_admin")
async def adm_add(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите числовой ID пользователя:")
    await state.set_state(AdminStates.add_admin)

@dp.message(AdminStates.add_admin)
async def adm_save_admin(m: Message, state: FSMContext):
    try:
        new_id = int(m.text)
        conn = get_db()
        conn.execute("UPDATE users SET role=1 WHERE user_id=?", (new_id,))
        conn.commit()
        conn.close()
        await m.answer("✅ Админ добавлен.")
    except:
        await m.answer("Ошибка ID.")
    await state.clear()

async def main():
    init_db()
    scheduler.start() # Запускаем планировщик
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
