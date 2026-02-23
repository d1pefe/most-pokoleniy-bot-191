import asyncio
import logging
import sqlite3
import json
import os
from datetime import datetime
import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    Message, CallbackQuery, FSInputFile
)
from openai import AsyncOpenAI

# --- НАСТРОЙКИ ---
BOT_TOKEN = "ВАШ_ТОКЕН"
OPENAI_API_KEY = "ВАШ_КЛЮЧ"
MAIN_ADMIN_ID = 123456789  # ВАШ ID

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
DB_NAME = "bridge_bot.db"

# --- МАШИНА СОСТОЯНИЙ ---
class RegStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()

class UserStates(StatesGroup):
    writing_case = State()
    taking_survey = State()

class AdminSurveyStates(StatesGroup):
    new_title = State()
    new_q_text = State()
    new_q_options = State()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Профили пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        role_type TEXT, -- Родитель, Ученик, Педагог, Гость
        phone TEXT,
        is_admin INTEGER DEFAULT 0,
        join_date TEXT
    )''')
    
    # Обратная связь (Анонимный ящик)
    c.execute('''CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        text TEXT,
        created_at TEXT
    )''')

    # Анкеты (Опросы)
    c.execute('''CREATE TABLE IF NOT EXISTS surveys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        status TEXT DEFAULT 'draft', -- draft, active, closed
        created_at TEXT
    )''')

    # Вопросы к анкетам
    c.execute('''CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        survey_id INTEGER,
        text TEXT,
        q_type TEXT, -- single, text
        options TEXT -- JSON массив для single
    )''')

    # Ответы пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        survey_id INTEGER,
        question_id INTEGER,
        user_id INTEGER,
        answer_text TEXT
    )''')

    c.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", (MAIN_ADMIN_ID,))
    c.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (MAIN_ADMIN_ID,))
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_NAME)

# --- КЛАВИАТУРЫ ---
def kb_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📬 Анонимный ящик")],
        [KeyboardButton(text="📋 Актуальные анкеты")],
        [KeyboardButton(text="ℹ️ О клубе")]
    ], resize_keyboard=True)

def kb_roles():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍👩‍👧 Родитель", callback_data="role_parent"),
         InlineKeyboardButton(text="🎓 Ученик", callback_data="role_student")],
        [InlineKeyboardButton(text="👩‍🏫 Педагог", callback_data="role_teacher"),
         InlineKeyboardButton(text="👤 Гость", callback_data="role_guest")]
    ])

def kb_skip_phone():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏭ Пропустить")]], resize_keyboard=True)

def kb_admin_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Управление анкетами", callback_data="adm_surveys")],
        [InlineKeyboardButton(text="📥 Читать анонимный ящик", callback_data="adm_feedback")],
        [InlineKeyboardButton(text="👥 Выгрузить пользователей", callback_data="adm_users")]
    ])

# --- РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ ---
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    conn = get_db()
    user = conn.execute("SELECT full_name, role_type FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    conn.close()

    if user and user[0] and user[1]:
        await message.answer(f"С возвращением, {user[0]}! 👋", reply_markup=kb_main_menu())
    else:
        # Новый пользователь
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO users (user_id, username, join_date) VALUES (?, ?, ?)", 
                     (message.from_user.id, message.from_user.username, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        await message.answer(
            "Добро пожаловать в цифровое пространство «Мост поколений»! 👋\n\n"
            "Давайте познакомимся. Напишите ваше <b>Имя и Фамилию</b>:",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(RegStates.waiting_for_name)

@dp.message(RegStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await message.answer("Отлично! А какую группу вы представляете?", reply_markup=kb_roles())

@dp.callback_query(F.data.startswith("role_"))
async def process_role(cb: CallbackQuery, state: FSMContext):
    role_map = {
        "role_parent": "Родитель", "role_student": "Ученик",
        "role_teacher": "Педагог", "role_guest": "Гость"
    }
    role = role_map.get(cb.data, "Гость")
    await state.update_data(role_type=role)
    
    await cb.message.delete()
    await cb.message.answer(
        "Последний шаг: укажите ваш номер телефона для связи организаторов.\n"
        "<i>Это необязательно, можете нажать «Пропустить».</i>",
        reply_markup=kb_skip_phone()
    )
    await state.set_state(RegStates.waiting_for_phone)

@dp.message(RegStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = message.text if message.text != "⏭ Пропустить" else "Не указан"
    
    conn = get_db()
    conn.execute(
        "UPDATE users SET full_name=?, role_type=?, phone=? WHERE user_id=?", 
        (data['full_name'], data['role_type'], phone, message.from_user.id)
    )
    conn.commit()
    conn.close()
    
    await message.answer("Регистрация завершена! Спасибо.", reply_markup=kb_main_menu())
    await state.clear()

# --- ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ ---
@dp.message(F.text == "ℹ️ О клубе")
async def about(message: Message):
    await message.answer("«Мост поколений» — безопасное пространство для открытого диалога.")

@dp.message(F.text == "📬 Анонимный ящик")
async def feedback_start(message: Message, state: FSMContext):
    await message.answer("Напишите вашу ситуацию или мысль. Это полностью анонимно.")
    await state.set_state(UserStates.writing_case)

@dp.message(UserStates.writing_case)
async def feedback_save(message: Message, state: FSMContext):
    conn = get_db()
    conn.execute("INSERT INTO feedback (user_id, text, created_at) VALUES (?, ?, ?)",
                 (message.from_user.id, message.text, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    await message.answer("Ваше сообщение сохранено. Спасибо за честность!", reply_markup=kb_main_menu())
    await state.clear()

# --- ПРОХОЖДЕНИЕ АНКЕТЫ ПОЛЬЗОВАТЕЛЕМ ---
@dp.message(F.text == "📋 Актуальные анкеты")
async def list_active_surveys(message: Message):
    conn = get_db()
    surveys = conn.execute("SELECT id, title FROM surveys WHERE status='active'").fetchall()
    
    # Проверяем, какие уже пройдены
    available = []
    for s in surveys:
        answered = conn.execute("SELECT id FROM answers WHERE survey_id=? AND user_id=? LIMIT 1", (s[0], message.from_user.id)).fetchone()
        if not answered:
            available.append(s)
    conn.close()

    if not available:
        await message.answer("Сейчас нет новых анкет для вас.")
        return

    btns = [[InlineKeyboardButton(text=f"📝 {s[1]}", callback_data=f"take_{s[0]}")] for s in available]
    await message.answer("Выберите анкету для прохождения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("take_"))
async def start_survey(cb: CallbackQuery, state: FSMContext):
    survey_id = int(cb.data.split("_")[1])
    conn = get_db()
    questions = conn.execute("SELECT id, text, q_type, options FROM questions WHERE survey_id=? ORDER BY id", (survey_id,)).fetchall()
    conn.close()

    if not questions:
        await cb.answer("В анкете пока нет вопросов.", show_alert=True)
        return

    # Сохраняем вопросы в FSM
    await state.update_data(survey_id=survey_id, questions=questions, current_idx=0, user_answers={})
    await ask_next_question(cb.message, state)
    await cb.answer()

async def ask_next_question(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data['current_idx']
    questions = data['questions']

    if idx >= len(questions):
        # Анкета завершена, сохраняем в БД
        conn = get_db()
        for q_id, ans_text in data['user_answers'].items():
            conn.execute("INSERT INTO answers (survey_id, question_id, user_id, answer_text) VALUES (?, ?, ?, ?)",
                         (data['survey_id'], q_id, message.chat.id, ans_text))
        conn.commit()
        conn.close()
        await message.answer("✅ Спасибо! Ваши ответы сохранены.", reply_markup=kb_main_menu())
        await state.clear()
        return

    q = questions[idx]
    q_id, q_text, q_type, q_opts = q

    if q_type == 'single':
        opts = json.loads(q_opts)
        btns = [[InlineKeyboardButton(text=opt, callback_data=f"ans_{idx}_{i}")] for i, opt in enumerate(opts)]
        await message.answer(f"Вопрос {idx+1}:\n<b>{q_text}</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    else: # text
        await message.answer(f"Вопрос {idx+1}:\n<b>{q_text}</b>\n\n<i>Напишите ответ текстом:</i>")
    
    await state.set_state(UserStates.taking_survey)

@dp.callback_query(UserStates.taking_survey, F.data.startswith("ans_"))
async def save_inline_answer(cb: CallbackQuery, state: FSMContext):
    _, idx, opt_idx = cb.data.split("_")
    idx, opt_idx = int(idx), int(opt_idx)
    
    data = await state.get_data()
    q = data['questions'][idx]
    opt_text = json.loads(q[3])[opt_idx]

    answers = data.get('user_answers', {})
    answers[q[0]] = opt_text
    
    await state.update_data(user_answers=answers, current_idx=idx + 1)
    await cb.message.delete()
    await ask_next_question(cb.message, state)

@dp.message(UserStates.taking_survey)
async def save_text_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data['current_idx']
    q = data['questions'][idx]

    answers = data.get('user_answers', {})
    answers[q[0]] = message.text
    
    await state.update_data(user_answers=answers, current_idx=idx + 1)
    await ask_next_question(message, state)


# --- АДМИНКА: ГЛАВНОЕ ---
@dp.message(Command("admin"))
async def admin_start(message: Message):
    conn = get_db()
    is_adm = conn.execute("SELECT is_admin FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    conn.close()
    
    if is_adm and is_adm[0] == 1:
        await message.answer("Панель администратора:", reply_markup=kb_admin_main())
    else:
        await message.answer("⛔️ Нет доступа.")

# --- АДМИНКА: ВЫГРУЗКА ---
@dp.callback_query(F.data == "adm_users")
async def export_users(cb: CallbackQuery):
    await cb.message.answer("⏳ Формирую Excel...")
    conn = get_db()
    df = pd.read_sql_query("SELECT user_id, full_name, role_type, phone, join_date FROM users", conn)
    conn.close()
    
    filename = "users_export.xlsx"
    df.to_excel(filename, index=False)
    await cb.message.answer_document(FSInputFile(filename), caption="Выгрузка пользователей")
    os.remove(filename)
    await cb.answer()

# --- АДМИНКА: УПРАВЛЕНИЕ АНКЕТАМИ ---
@dp.callback_query(F.data == "adm_surveys")
async def adm_surveys_list(cb: CallbackQuery):
    conn = get_db()
    surveys = conn.execute("SELECT id, title, status FROM surveys ORDER BY id DESC").fetchall()
    conn.close()

    btns = [[InlineKeyboardButton(text="➕ Создать новую анкету", callback_data="surv_create")]]
    for s in surveys:
        status_emoji = "🟢" if s[2] == 'active' else "🔴" if s[2] == 'closed' else "📝"
        btns.append([InlineKeyboardButton(text=f"{status_emoji} {s[1]}", callback_data=f"sm_{s[0]}")])

    await cb.message.answer("Управление анкетами:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

# Создание анкеты
@dp.callback_query(F.data == "surv_create")
async def surv_create(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите название новой анкеты:")
    await state.set_state(AdminSurveyStates.new_title)
    await cb.answer()

@dp.message(AdminSurveyStates.new_title)
async def surv_save_title(message: Message, state: FSMContext):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO surveys (title, created_at) VALUES (?, ?)", (message.text, datetime.now().isoformat()))
    sid = c.lastrowid
    conn.commit()
    conn.close()
    
    await message.answer(f"Анкета создана! Выберите ее в меню для настройки.", reply_markup=kb_admin_main())
    await state.clear()

# Меню конкретной анкеты
@dp.callback_query(F.data.startswith("sm_"))
async def surv_menu(cb: CallbackQuery):
    sid = cb.data.split("_")[1]
    conn = get_db()
    surv = conn.execute("SELECT title, status FROM surveys WHERE id=?", (sid,)).fetchone()
    q_count = conn.execute("SELECT count(*) FROM questions WHERE survey_id=?", (sid,)).fetchone()[0]
    conn.close()

    text = f"📋 <b>{surv[0]}</b>\nСтатус: {surv[1]}\nВопросов: {q_count}"
    
    btns = []
    if surv[1] == 'draft':
        btns.append([InlineKeyboardButton(text="➕ Добавить вопрос", callback_data=f"sqadd_{sid}")])
        btns.append([InlineKeyboardButton(text="▶️ Запустить", callback_data=f"sstat_{sid}_active")])
    elif surv[1] == 'active':
        btns.append([InlineKeyboardButton(text="🔴 Закрыть сбор", callback_data=f"sstat_{sid}_closed")])
    elif surv[1] == 'closed':
        btns.append([InlineKeyboardButton(text="📊 Выгрузить Excel", callback_data=f"sexp_{sid}")])
        btns.append([InlineKeyboardButton(text="🗑 Удалить анкету", callback_data=f"sdel_{sid}")])

    await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

# Изменение статуса анкеты
@dp.callback_query(F.data.startswith("sstat_"))
async def surv_change_status(cb: CallbackQuery):
    _, sid, new_status = cb.data.split("_")
    conn = get_db()
    conn.execute("UPDATE surveys SET status=? WHERE id=?", (new_status, sid))
    conn.commit()
    conn.close()
    await cb.message.edit_text(f"Статус изменен на {new_status}.")
    await cb.answer()

# Добавление вопроса
@dp.callback_query(F.data.startswith("sqadd_"))
async def surv_add_q(cb: CallbackQuery, state: FSMContext):
    sid = cb.data.split("_")[1]
    await state.update_data(survey_id=sid)
    
    btns = [
        [InlineKeyboardButton(text="Один выбор (Кнопки)", callback_data="qtype_single")],
        [InlineKeyboardButton(text="Развернутый ответ (Текст)", callback_data="qtype_text")]
    ]
    await cb.message.answer("Выберите тип вопроса:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@dp.callback_query(F.data.startswith("qtype_"))
async def surv_q_type(cb: CallbackQuery, state: FSMContext):
    qtype = cb.data.split("_")[1]
    await state.update_data(q_type=qtype)
    await cb.message.answer("Напишите текст вопроса (например: 'Что вас больше всего пугает?'):")
    await state.set_state(AdminSurveyStates.new_q_text)
    await cb.answer()

@dp.message(AdminSurveyStates.new_q_text)
async def surv_q_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if data['q_type'] == 'text':
        # Сохраняем сразу
        conn = get_db()
        conn.execute("INSERT INTO questions (survey_id, text, q_type) VALUES (?, ?, ?)",
                     (data['survey_id'], message.text, 'text'))
        conn.commit()
        conn.close()
        await message.answer("Вопрос добавлен!", reply_markup=kb_admin_main())
        await state.clear()
    else:
        await state.update_data(q_text=message.text)
        await message.answer("Напишите варианты ответов через символ `|`.\nНапример: <i>Тревогу | Злость | Радость</i>")
        await state.set_state(AdminSurveyStates.new_q_options)

@dp.message(AdminSurveyStates.new_q_options)
async def surv_q_opts(message: Message, state: FSMContext):
    data = await state.get_data()
    options = [opt.strip() for opt in message.text.split('|')]
    
    conn = get_db()
    conn.execute("INSERT INTO questions (survey_id, text, q_type, options) VALUES (?, ?, ?, ?)",
                 (data['survey_id'], data['q_text'], 'single', json.dumps(options, ensure_ascii=False)))
    conn.commit()
    conn.close()
    
    await message.answer("Вопрос с вариантами добавлен!", reply_markup=kb_admin_main())
    await state.clear()

# Выгрузка результатов анкеты
@dp.callback_query(F.data.startswith("sexp_"))
async def surv_export(cb: CallbackQuery):
    sid = cb.data.split("_")[1]
    await cb.message.answer("⏳ Формирую отчет...")
    
    conn = get_db()
    query = """
    SELECT u.full_name, u.role_type, q.text as question, a.answer_text 
    FROM answers a
    JOIN users u ON a.user_id = u.user_id
    JOIN questions q ON a.question_id = q.id
    WHERE a.survey_id = ?
    """
    df = pd.read_sql_query(query, conn, params=(sid,))
    conn.close()
    
    if df.empty:
        await cb.message.answer("Ответов пока нет.")
        return

    filename = f"survey_{sid}_results.xlsx"
    df.to_excel(filename, index=False)
    await cb.message.answer_document(FSInputFile(filename), caption="Результаты анкеты")
    os.remove(filename)
    await cb.answer()

# Удаление анкеты
@dp.callback_query(F.data.startswith("sdel_"))
async def surv_delete(cb: CallbackQuery):
    sid = cb.data.split("_")[1]
    conn = get_db()
    conn.execute("DELETE FROM surveys WHERE id=?", (sid,))
    conn.execute("DELETE FROM questions WHERE survey_id=?", (sid,))
    conn.execute("DELETE FROM answers WHERE survey_id=?", (sid,))
    conn.commit()
    conn.close()
    await cb.message.edit_text("🗑 Анкета и все ответы удалены.")
    await cb.answer()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
