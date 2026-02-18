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
# ⚠️ ВСТАВЬТЕ СЮДА ВАШИ КЛЮЧИ
BOT_TOKEN = "7802243169:AAHmow-BnBE9T5PK5FxrbyQnf4caklqmB9c"
OPENAI_API_KEY = "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"
MAIN_ADMIN_ID = 7233585816 

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
        event_id INTEGER, -- NULL если это общая тема из главного меню
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

    # Гарантируем, что главный админ есть в базе
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
    broadcast_schedule = State()
    add_admin = State()

class UserStates(StatesGroup):
    writing_question = State()
    writing_case = State()
    writing_topic = State() # Общее предложение темы

# --- КЛАВИАТУРЫ ---
def kb_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Афиша встреч")],
        [KeyboardButton(text="💡 Предложить тему"), KeyboardButton(text="📬 Анонимный ящик")],
        [KeyboardButton(text="ℹ️ О клубе")]
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
        [InlineKeyboardButton(text="✅ Иду / Занять место", callback_data=f"reg_{event_id}")],
        [InlineKeyboardButton(text="❓ Задать вопрос спикеру", callback_data=f"ask_{event_id}")],
        [InlineKeyboardButton(text="🔥 Разобрать мой кейс", callback_data=f"case_{event_id}")]
    ])

# --- ЛОГИКА ИИ ---
async def ai_analyze(text_data, event_title="Общее"):
    if not ai_client: return "⚠️ ИИ не подключен (нет ключа)."
    
    prompt = (
        "Ты — профессиональный модератор педагогических дискуссий и психолог. "
        f"Твоя задача — проанализировать массив сообщений от участников перед мероприятием на тему: '{event_title}'.\n\n"
        f"Вот список вопросов и кейсов:\n{text_data[:3500]}\n\n"
        "Сформируй отчет для ведущего в формате HTML (без markdown разметки ```):\n"
        "<b>1. Эмоциональный градус:</b> (Опиши одним предложением: напряженный, заинтересованный, агрессивный и т.д.)\n"
        "<b>2. ТОП-3 боли аудитории:</b> (Сгруппируй похожие вопросы и выдели 3 главные проблемы)\n"
        "<b>3. Провокационный вопрос:</b> (Сформулируй 1 острый вопрос для начала дискуссии, который зацепит всех)\n"
        "<b>4. Рекомендация ведущему:</b> (На что сделать упор, чего избегать)"
    )
    
    try:
        resp = await ai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"Ошибка ИИ: {e}"

# --- ФУНКЦИЯ РАССЫЛКИ ---
async def send_broadcast_task(event_id):
    conn = get_db()
    event = conn.execute("SELECT title, description, event_date, photo_id FROM events WHERE id=?", (event_id,)).fetchone()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    if not event: return

    # Улучшенный текст рассылки
    text = (
        f"🔔 <b>АНОНС: {event[0]}</b>\n\n"
        f"Тема, о которой часто молчат, но которая касается каждого.\n\n"
        f"👇 <b>О чем будем говорить:</b>\n{event[1]}\n\n"
        f"🗓 <b>Когда:</b> {event[2]}\n"
        f"📍 <b>Где:</b> Актовый зал / Онлайн\n\n"
        "Ваш голос важен! Чтобы встреча прошла продуктивно, мы собираем вопросы заранее."
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
            await asyncio.sleep(0.05) 
        except Exception as e:
            logger.error(f"Не удалось отправить юзеру {user[0]}: {e}")

    try:
        await bot.send_message(MAIN_ADMIN_ID, f"✅ Авто-рассылка завершена! Отправлено: {count}")
    except:
        pass


# --- ХЕНДЛЕРЫ: СТАРТ И МЕНЮ ---
@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    conn = get_db()
    role = 2 if user_id == MAIN_ADMIN_ID else 0
    
    conn.execute("""
        INSERT INTO users (user_id, username, full_name, role, join_date) 
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username
    """, (user_id, message.from_user.username, message.from_user.full_name, role, datetime.now().isoformat()))
    
    if user_id == MAIN_ADMIN_ID:
        conn.execute("UPDATE users SET role=2 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    
    welcome_text = (
        f"Здравствуйте, {message.from_user.full_name}! 👋\n\n"
        "Я — цифровой помощник клуба <b>«Мост поколений»</b>. "
        "Здесь мы строим диалог между родителями, учителями и учениками.\n\n"
        "Через меня вы можете регистрироваться на встречи, задавать острые вопросы и делиться ситуациями (даже анонимно)."
    )
    await message.answer(welcome_text, reply_markup=kb_main_menu())

# --- ХЕНДЛЕРЫ: ПОЛЬЗОВАТЕЛЬСКИЕ ---
@dp.message(F.text == "ℹ️ О клубе")
async def about(message: Message):
    await message.answer("Мы создаем диалог между поколениями. Здесь можно обсудить проблемы школы, семьи и общения в безопасной обстановке.")

@dp.message(F.text == "💡 Предложить тему")
async def suggest_topic(message: Message, state: FSMContext):
    # Очищаем event_id, так как это общее предложение
    await state.update_data(event_id=None)
    await message.answer("О чем нам стоит поговорить в следующий раз? Мы ищем темы, которые реально волнуют школу.")
    await state.set_state(UserStates.writing_topic)

@dp.message(F.text == "📬 Анонимный ящик") # Бывшее "Моя ситуация"
async def my_case_general(message: Message, state: FSMContext):
    # Очищаем event_id
    await state.update_data(event_id=None)
    await message.answer(
        "Здесь вы можете выговориться. Напишите, что происходит.\n"
        "Мы читаем все сообщения, но <b>никогда не раскрываем авторов</b>."
    )
    await state.set_state(UserStates.writing_case)

@dp.message(F.text == "📅 Афиша встреч")
async def nearest_event(message: Message):
    conn = get_db()
    event = conn.execute("SELECT id, title, description, event_date, photo_id FROM events WHERE is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    
    if not event:
        await message.answer("Пока нет анонсов будущих встреч. Загляните позже!")
        return
        
    text = (
        f"🗓 <b>{event[1]}</b>\n"
        f"🕒 {event[3]}\n\n"
        f"{event[2]}\n\n"
        "👇 <b>Что будем делать:</b>"
    )
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
    event_id = data.get('event_id') # Будет None для общих вопросов, и ID для конкретных
    
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
    
    if submission_type == 'question':
        ans = "<b>Принято!</b> Ваш вопрос передан модератору. Спасибо за смелость."
    elif submission_type == 'case':
        ans = "<b>История сохранена.</b> Мы убрали ваше имя, чтобы сохранить анонимность."
    else:
        ans = "Спасибо! Ваше предложение записано."

    await message.answer(ans, reply_markup=kb_main_menu())
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
        await cb.answer("✅ Вы в списке! Напомним за день до встречи.", show_alert=True)
    except sqlite3.IntegrityError:
        await cb.answer("Вы уже записаны на это событие.", show_alert=True)
    conn.close()

@dp.callback_query(F.data.startswith("ask_"))
async def cb_ask(cb: CallbackQuery, state: FSMContext):
    eid = cb.data.split("_")[1]
    await state.update_data(event_id=eid)
    await cb.message.answer("Напишите ваш вопрос спикерам этого мероприятия в одном сообщении:")
    await state.set_state(UserStates.writing_question)
    await cb.answer()

@dp.callback_query(F.data.startswith("case_"))
async def cb_case(cb: CallbackQuery, state: FSMContext):
    eid = cb.data.split("_")[1]
    await state.update_data(event_id=eid)
    await cb.message.answer(
        "Опишите конфликтную или сложную ситуацию по теме встречи.\n"
        "<i>Например: 'Ученик отказывается сдавать телефон...'</i>"
    )
    await state.set_state(UserStates.writing_case)
    await cb.answer()


# --- ХЕНДЛЕРЫ: АДМИНКА ---
@dp.message(Command("admin"))
async def admin_start(message: Message):
    conn = get_db()
    user = conn.execute("SELECT role FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if message.from_user.id == MAIN_ADMIN_ID or (user and user[0] > 0):
        await message.answer("Панель управления:", reply_markup=kb_admin_main())
    else:
        await message.answer("⛔️ Нет прав доступа.")

# 1. Создание (Без изменений, только тексты чуть мягче)
@dp.callback_query(F.data == "adm_create")
async def adm_create(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите название мероприятия:")
    await state.set_state(AdminStates.new_event_title)

@dp.message(AdminStates.new_event_title)
async def adm_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    await m.answer("Введите описание (тезисы, о чем встреча):")
    await state.set_state(AdminStates.new_event_desc)

@dp.message(AdminStates.new_event_desc)
async def adm_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.text)
    await m.answer("Дата и время (текстом, напр. '25 Мая 18:00'):")
    await state.set_state(AdminStates.new_event_date)

@dp.message(AdminStates.new_event_date)
async def adm_date(m: Message, state: FSMContext):
    await state.update_data(date=m.text)
    await m.answer("Пришлите афишу/картинку (или напишите 'нет'):")
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
        "Введите дату рассылки (серверное время):\nFormat: <b>YYYY-MM-DD HH:MM</b>"
    )
    await state.set_state(AdminStates.broadcast_schedule)

@dp.message(AdminStates.broadcast_schedule)
async def adm_set_schedule(m: Message, state: FSMContext):
    try:
        run_date = datetime.strptime(m.text, "%Y-%m-%d %H:%M")
        data = await state.get_data()
        event_id = data['event_id']
        scheduler.add_job(send_broadcast_task, 'date', run_date=run_date, args=[event_id])
        await m.answer(f"✅ Рассылка уйдет {run_date}!", reply_markup=kb_admin_main())
        await state.clear()
    except ValueError:
        await m.answer("❌ Формат: YYYY-MM-DD HH:MM")

# 3. Просмотр ответов + ИИ
@dp.callback_query(F.data == "adm_view_answers")
async def adm_view(cb: CallbackQuery):
    conn = get_db()
    events = conn.execute("SELECT id, title FROM events ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    
    # Кнопки событий + Общий ящик
    btns = [[InlineKeyboardButton(text=f"📂 {e[1]}", callback_data=f"data_{e[0]}")] for e in events]
    btns.insert(0, [InlineKeyboardButton(text="📥 Общий ящик (Вне тем)", callback_data="data_general")])
    
    await cb.message.answer("Выберите источник данных:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("data_"))
async def adm_show_data(cb: CallbackQuery):
    param = cb.data.split("_")[1] # 'general' или ID события
    conn = get_db()
    
    if param == 'general':
        # Берем данные где event_id IS NULL
        event_title = "Общий ящик"
        questions = conn.execute("SELECT text FROM submissions WHERE event_id IS NULL AND type='question'").fetchall()
        cases = conn.execute("SELECT text FROM submissions WHERE event_id IS NULL AND type='case'").fetchall()
        topics = conn.execute("SELECT text FROM submissions WHERE event_id IS NULL AND type='topic'").fetchall()
        reg_count = 0
        extra_text = f"Предложения тем: {len(topics)}\n"
        full_text = "Темы:\n" + "\n".join([t[0] for t in topics]) + "\n\n"
    else:
        # Берем данные по ID
        eid = int(param)
        event = conn.execute("SELECT title FROM events WHERE id=?", (eid,)).fetchone()
        event_title = event[0] if event else "Событие удалено"
        
        reg_count = conn.execute("SELECT count(*) FROM registrations WHERE event_id=?", (eid,)).fetchone()[0]
        questions = conn.execute("SELECT text FROM submissions WHERE event_id=? AND type='question'", (eid,)).fetchall()
        cases = conn.execute("SELECT text FROM submissions WHERE event_id=? AND type='case'", (eid,)).fetchall()
        extra_text = ""
        full_text = ""

    conn.close()
    
    text_report = (
        f"📊 <b>{event_title}</b>\n"
        f"Регистраций: {reg_count}\n"
        f"Вопросов: {len(questions)}\n"
        f"Кейсов: {len(cases)}\n"
        f"{extra_text}\n"
    )
    
    # Собираем полный текст для отчета или ИИ
    full_text += "Вопросы:\n" + "\n".join([q[0] for q in questions]) + "\n\nКейсы:\n" + "\n".join([c[0] for c in cases])
    
    # Клавиатура ИИ
    btn_ai = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧠 Нейро-анализ", callback_data=f"ai_{param}")]])
    
    if len(full_text) > 30: # Если есть хоть какой-то текст
        await cb.message.answer(text_report, reply_markup=btn_ai)
        if len(full_text) < 4000:
            await cb.message.answer(f"📜 <b>Содержимое:</b>\n{full_text}")
        else:
            await cb.message.answer("Данных слишком много, показаны первые 4000 символов.")
            await cb.message.answer(full_text[:4000])
    else:
        await cb.message.answer(text_report + "📭 Данных пока нет.")

@dp.callback_query(F.data.startswith("ai_"))
async def adm_run_ai(cb: CallbackQuery):
    param = cb.data.split("_")[1]
    await cb.message.answer("⏳ Анализирую... Это может занять секунд 10.")
    
    conn = get_db()
    if param == 'general':
        data = conn.execute("SELECT text FROM submissions WHERE event_id IS NULL").fetchall()
        title = "Общий фидбек и темы"
    else:
        data = conn.execute("SELECT text FROM submissions WHERE event_id=?", (param,)).fetchall()
        title_row = conn.execute("SELECT title FROM events WHERE id=?", (param,)).fetchone()
        title = title_row[0] if title_row else "Событие"
    conn.close()
    
    full_text = "\n".join([d[0] for d in data])
    
    if not full_text:
        await cb.message.answer("Нечего анализировать.")
        return

    res = await ai_analyze(full_text, title)
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
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
