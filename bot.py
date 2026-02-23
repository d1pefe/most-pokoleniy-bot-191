import asyncio
import logging
import sqlite3
import json
import os
import io
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
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError

# --- НАСТРОЙКИ ---
BOT_TOKEN = "7802243169:AAHmow-BnBE9T5PK5FxrbyQnf4caklqmB9c"
MAIN_ADMIN_ID = 7233585816  # ВАШ ID

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
DB_NAME = "bridge_bot_crm.db"

# --- МАШИНА СОСТОЯНИЙ ---
class RegStates(StatesGroup):
    name = State()
    phone = State()

class UserStates(StatesGroup):
    general_feedback = State()
    event_question = State()
    event_case = State()
    taking_survey = State()

class AdminEventStates(StatesGroup):
    title = State()
    desc = State()
    date = State()
    photo = State()
    broadcast_time = State()
    edit_value = State()

class AdminSurveyStates(StatesGroup):
    title = State()
    target_role = State()
    q_text = State()
    q_options = State()
    import_excel = State()

class AdminGlobalStates(StatesGroup):
    free_broadcast = State()
    add_admin = State()
    del_admin = State()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Пользователи
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, 
        role_type TEXT, phone TEXT, is_admin INTEGER DEFAULT 0, join_date TEXT)''')
    
    # Мероприятия
    c.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, 
        event_date TEXT, photo_id TEXT, is_active INTEGER DEFAULT 1)''')
        
    # Регистрации на мероприятия
    c.execute('''CREATE TABLE IF NOT EXISTS registrations (
        user_id INTEGER, event_id INTEGER, registered_at TEXT, 
        PRIMARY KEY (user_id, event_id))''')

    # Вопросы и кейсы (Анонимный ящик + К мероприятиям)
    c.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, event_id INTEGER, 
        type TEXT, text TEXT, created_at TEXT)''')

    # Анкеты (Опросы)
    c.execute('''CREATE TABLE IF NOT EXISTS surveys (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, target_role TEXT, 
        status TEXT DEFAULT 'draft', created_at TEXT)''')

    # Вопросы к анкетам
    c.execute('''CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, survey_id INTEGER, 
        text TEXT, q_type TEXT, options TEXT)''')

    # Ответы на анкеты
    c.execute('''CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, survey_id INTEGER, 
        question_id INTEGER, user_id INTEGER, answer_text TEXT)''')

    c.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", (MAIN_ADMIN_ID,))
    c.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (MAIN_ADMIN_ID,))
    conn.commit()
    conn.close()

def get_db(): return sqlite3.connect(DB_NAME)

# --- КЛАВИАТУРЫ ---
def kb_main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Афиша встреч")],
        [KeyboardButton(text="📋 Актуальные анкеты")],
        [KeyboardButton(text="📬 Анонимный ящик"), KeyboardButton(text="ℹ️ О клубе")]
    ], resize_keyboard=True)

def kb_roles():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍👩‍👧 Родитель", callback_data="role_Родитель"),
         InlineKeyboardButton(text="🎓 Ученик", callback_data="role_Ученик")],
        [InlineKeyboardButton(text="👩‍🏫 Педагог", callback_data="role_Педагог"),
         InlineKeyboardButton(text="👤 Гость", callback_data="role_Гость")]
    ])

def kb_admin_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Мероприятия", callback_data="adm_events_menu"),
         InlineKeyboardButton(text="📋 Анкеты", callback_data="adm_surveys_menu")],
        [InlineKeyboardButton(text="📢 Свободная рассылка", callback_data="adm_free_broadcast")],
        [InlineKeyboardButton(text="📥 Анонимный ящик (Общий)", callback_data="adm_view_general")],
        [InlineKeyboardButton(text="👥 База (Excel)", callback_data="adm_export_users"),
         InlineKeyboardButton(text="👮 Админы", callback_data="adm_admins_menu")]
    ])

def kb_event_actions(event_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Иду / Занять место", callback_data=f"reg_{event_id}")],
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data=f"ask_{event_id}"),
         InlineKeyboardButton(text="🔥 Мой кейс", callback_data=f"case_{event_id}")]
    ])

# --- РЕГИСТРАЦИЯ ---
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    conn = get_db()
    user = conn.execute("SELECT full_name, role_type FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    if user and user[0] and user[1]:
        await message.answer(f"С возвращением, {user[0]}! 👋", reply_markup=kb_main_menu())
    else:
        conn.execute("INSERT OR IGNORE INTO users (user_id, username, join_date) VALUES (?, ?, ?)", 
                     (message.from_user.id, message.from_user.username, datetime.now().isoformat()))
        conn.commit()
        await message.answer("Добро пожаловать в «Мост поколений»! 👋\nНапишите ваше <b>Имя и Фамилию</b>:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(RegStates.name)
    conn.close()

@dp.message(RegStates.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text)
    await message.answer("Какую группу вы представляете?", reply_markup=kb_roles())

@dp.callback_query(F.data.startswith("role_"))
async def process_role(cb: CallbackQuery, state: FSMContext):
    role = cb.data.split("_")[1]
    await state.update_data(role_type=role)
    await cb.message.delete()
    await cb.message.answer("Номер телефона (необязательно):", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏭ Пропустить")]], resize_keyboard=True))
    await state.set_state(RegStates.phone)

@dp.message(RegStates.phone)
async def process_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = message.text if message.text != "⏭ Пропустить" else "Не указан"
    conn = get_db()
    conn.execute("UPDATE users SET full_name=?, role_type=?, phone=? WHERE user_id=?", 
                 (data['full_name'], data['role_type'], phone, message.from_user.id))
    conn.commit()
    conn.close()
    await message.answer("Регистрация завершена!", reply_markup=kb_main_menu())
    await state.clear()

# --- ПОЛЬЗОВАТЕЛЬ: МЕНЮ И МЕРОПРИЯТИЯ ---
@dp.message(F.text == "ℹ️ О клубе")
async def about(message: Message):
    await message.answer("«Мост поколений» — безопасное пространство для открытого диалога.")

@dp.message(F.text == "📅 Афиша встреч")
async def nearest_event(message: Message):
    conn = get_db()
    event = conn.execute("SELECT id, title, description, event_date, photo_id FROM events WHERE is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not event:
        return await message.answer("Пока нет анонсов будущих встреч.")
    text = f"🗓 <b>{event[1]}</b>\n🕒 {event[3]}\n\n{event[2]}\n\n👇 <b>Выберите действие:</b>"
    kb = kb_event_actions(event[0])
    if event[4]: await message.answer_photo(event[4], caption=text, reply_markup=kb)
    else: await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("reg_"))
async def cb_reg(cb: CallbackQuery):
    eid = cb.data.split("_")[1]
    conn = get_db()
    try:
        conn.execute("INSERT INTO registrations (user_id, event_id, registered_at) VALUES (?, ?, ?)", (cb.from_user.id, eid, datetime.now().isoformat()))
        conn.commit()
        await cb.answer("✅ Вы успешно записаны!", show_alert=True)
    except sqlite3.IntegrityError:
        await cb.answer("Вы уже записаны.", show_alert=True)
    conn.close()

@dp.callback_query(F.data.startswith("ask_"))
async def cb_ask(cb: CallbackQuery, state: FSMContext):
    await state.update_data(event_id=cb.data.split("_")[1])
    await cb.message.answer("Напишите ваш вопрос спикерам (анонимно):")
    await state.set_state(UserStates.event_question)
    await cb.answer()

@dp.callback_query(F.data.startswith("case_"))
async def cb_case(cb: CallbackQuery, state: FSMContext):
    await state.update_data(event_id=cb.data.split("_")[1])
    await cb.message.answer("Опишите вашу ситуацию (кейс) для разбора на встрече:")
    await state.set_state(UserStates.event_case)
    await cb.answer()

@dp.message(F.text == "📬 Анонимный ящик")
async def feedback_start(message: Message, state: FSMContext):
    await message.answer("Напишите вашу проблему или предложение. Это не привязано к дате и полностью анонимно.")
    await state.set_state(UserStates.general_feedback)

@dp.message(UserStates.general_feedback)
@dp.message(UserStates.event_question)
@dp.message(UserStates.event_case)
async def save_submission(message: Message, state: FSMContext):
    st = await state.get_state()
    data = await state.get_data()
    event_id = data.get('event_id') if st in [UserStates.event_question, UserStates.event_case] else None
    s_type = 'general' if st == UserStates.general_feedback else ('question' if st == UserStates.event_question else 'case')
    
    conn = get_db()
    conn.execute("INSERT INTO submissions (user_id, event_id, type, text, created_at) VALUES (?, ?, ?, ?, ?)",
                 (message.from_user.id, event_id, s_type, message.text, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    await message.answer("Принято! Спасибо за честность.", reply_markup=kb_main_menu())
    await state.clear()


# --- ПОЛЬЗОВАТЕЛЬ: АНКЕТЫ ---
@dp.message(F.text == "📋 Актуальные анкеты")
async def list_surveys(message: Message):
    conn = get_db()
    role = conn.execute("SELECT role_type FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()[0]
    surveys = conn.execute("SELECT id, title, target_role FROM surveys WHERE status='active'").fetchall()
    
    available = [s for s in surveys if (s[2] == 'Все' or s[2] == role) and not conn.execute("SELECT id FROM answers WHERE survey_id=? AND user_id=? LIMIT 1", (s[0], message.from_user.id)).fetchone()]
    conn.close()

    if not available: return await message.answer("Сейчас для вас нет новых анкет.")
    btns = [[InlineKeyboardButton(text=f"📝 {s[1]}", callback_data=f"take_{s[0]}")] for s in available]
    await message.answer("Доступные опросы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("take_"))
async def start_survey(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split("_")[1])
    conn = get_db()
    questions = conn.execute("SELECT id, survey_id, text, q_type, options FROM questions WHERE survey_id=? ORDER BY id", (sid,)).fetchall()
    conn.close()
    if not questions: return await cb.answer("Анкета пуста.", show_alert=True)
    
    await state.update_data(survey_id=sid, questions=questions, current_idx=0, user_answers={}, current_multi=[])
    await ask_next_question(cb.message, state)
    await cb.answer()

async def ask_next_question(message, state: FSMContext):
    data = await state.get_data()
    idx = data['current_idx']
    qs = data['questions']

    if idx >= len(qs):
        conn = get_db()
        for q_id, ans_text in data['user_answers'].items():
            conn.execute("INSERT INTO answers (survey_id, question_id, user_id, answer_text) VALUES (?, ?, ?, ?)", 
                         (data['survey_id'], q_id, message.chat.id, ans_text))
        conn.commit()
        conn.close()
        
        target_msg = message.message if isinstance(message, CallbackQuery) else message
        await target_msg.answer("✅ Анкета завершена! Спасибо за ваши ответы.", reply_markup=kb_main_menu())
        return await state.clear()

    q = qs[idx]
    q_id, surv_id, q_text, q_type, q_opts = q
    
    hint = ""
    btns = []
    if q_type in ['single', 'multi']:
        opts = json.loads(q_opts)
        selected = data.get('current_multi', [])
        
        for i, opt in enumerate(opts):
            mark = "✅ " if opt in selected else ""
            btns.append([InlineKeyboardButton(text=f"{mark}{opt}", callback_data=f"ans_{idx}_{i}")])
            
        if q_type == 'multi':
            btns.append([InlineKeyboardButton(text="➡️ Отправить ответы", callback_data=f"ans_done_{idx}")])
            hint = "\n<i>(выберите один или несколько вариантов)</i>"
        else:
            hint = "\n<i>(выберите один вариант)</i>"
            
    text = f"<b>{q_text}</b>{hint}"
    
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    else:
        if q_type == 'text':
            await message.answer(f"<b>{q_text}</b>\n<i>(напишите ответ текстом)</i>")
        else:
            await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await state.set_state(UserStates.taking_survey)

@dp.callback_query(UserStates.taking_survey, F.data.startswith("ans_"))
async def save_inline_ans(cb: CallbackQuery, state: FSMContext):
    cmd = cb.data.split("_")
    data = await state.get_data()
    idx = int(cmd[2]) if cmd[1] == 'done' else int(cmd[1])
    q = data['questions'][idx]
    q_id, surv_id, q_text, q_type, q_opts = q

    if cmd[1] == 'done':
        selected = data.get('current_multi', [])
        if not selected: return await cb.answer("Выберите хотя бы один вариант!", show_alert=True)
        data.setdefault('user_answers', {})[q_id] = ", ".join(selected)
        await state.update_data(user_answers=data['user_answers'], current_idx=idx + 1, current_multi=[])
        await cb.message.delete()
        await ask_next_question(cb.message, state)
        return

    opt_val = json.loads(q_opts)[int(cmd[2])]
    if q_type == 'single':
        data.setdefault('user_answers', {})[q_id] = opt_val
        await state.update_data(user_answers=data['user_answers'], current_idx=idx + 1)
        await cb.message.delete()
        await ask_next_question(cb.message, state)
    elif q_type == 'multi':
        selected = data.get('current_multi', [])
        if opt_val in selected: selected.remove(opt_val)
        else: selected.append(opt_val)
        await state.update_data(current_multi=selected)
        await ask_next_question(cb, state)

@dp.message(UserStates.taking_survey)
async def save_text_ans(message: Message, state: FSMContext):
    data = await state.get_data()
    q = data['questions'][data['current_idx']]
    data.setdefault('user_answers', {})[q[0]] = message.text
    await state.update_data(user_answers=data['user_answers'], current_idx=data['current_idx'] + 1)
    await ask_next_question(message, state)


# --- АДМИНКА: ПРОВЕРКА И ГЛАВНОЕ ---
@dp.message(Command("admin"))
async def admin_start(message: Message):
    conn = get_db()
    is_adm = conn.execute("SELECT is_admin FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    conn.close()
    if is_adm and is_adm[0] == 1: await message.answer("Панель управления:", reply_markup=kb_admin_main())

# --- АДМИНКА: СВОБОДНАЯ РАССЫЛКА И БАЗА ---
@dp.callback_query(F.data == "adm_free_broadcast")
async def free_cast_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Пришлите сообщение для рассылки ВСЕМ пользователям (текст или текст+фото):")
    await state.set_state(AdminGlobalStates.free_broadcast)
    await cb.answer()

@dp.message(AdminGlobalStates.free_broadcast)
async def free_cast_send(message: Message, state: FSMContext):
    conn = get_db()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    count = 0
    await message.answer("⏳ Начинаю рассылку...")
    
    for u in users:
        try:
            await message.copy_to(u[0])
            count += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            logger.warning(f"Лимит TelegramAPI. Ожидание {e.retry_after} секунд.")
            await asyncio.sleep(e.retry_after)
            try:
                await message.copy_to(u[0])
                count += 1
            except Exception:
                pass
        except TelegramAPIError as e:
            logger.warning(f"Не удалось отправить пользователю {u[0]}: {e}")
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при отправке {u[0]}: {e}")
            
    await message.answer(f"✅ Рассылка завершена. Доставлено: {count}")
    await state.clear()

@dp.callback_query(F.data == "adm_export_users")
async def export_users(cb: CallbackQuery):
    await cb.message.answer("⏳ Формирую базу...")
    conn = get_db()
    df = pd.read_sql_query("SELECT user_id, full_name, role_type, phone, join_date FROM users", conn)
    conn.close()
    df.to_excel("users.xlsx", index=False)
    await cb.message.answer_document(FSInputFile("users.xlsx"))
    os.remove("users.xlsx")
    await cb.answer()

@dp.callback_query(F.data == "adm_view_general")
async def view_general_feedback(cb: CallbackQuery):
    conn = get_db()
    feed = conn.execute("SELECT text FROM submissions WHERE type='general' ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    if not feed: return await cb.answer("Ящик пуст.", show_alert=True)
    text = "📬 <b>Последние 20 сообщений:</b>\n\n" + "\n---\n".join([f[0] for f in feed])
    if len(text) > 4000: text = text[:4000] + "..."
    await cb.message.answer(text)
    await cb.answer()

# --- АДМИНКА: УПРАВЛЕНИЕ АДМИНАМИ ---
@dp.callback_query(F.data == "adm_admins_menu")
async def adm_admins_menu(cb: CallbackQuery):
    conn = get_db()
    admins = conn.execute("SELECT user_id, full_name FROM users WHERE is_admin=1").fetchall()
    conn.close()
    
    text = "👮 <b>Список администраторов:</b>\n\n"
    for a in admins:
        text += f"- {a[1]} (ID: <code>{a[0]}</code>)\n"
        
    btns = [
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add_admin")],
        [InlineKeyboardButton(text="➖ Удалить админа", callback_data="adm_del_admin")]
    ]
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data == "adm_add_admin")
async def adm_add_admin(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите <b>ID пользователя</b> (число), которого хотите сделать администратором.\n\n<i>❗️ Пользователь уже должен быть зарегистрирован в боте (нажать /start).</i>")
    await state.set_state(AdminGlobalStates.add_admin)
    await cb.answer()

@dp.message(AdminGlobalStates.add_admin)
async def process_add_admin(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("ID должен состоять только из цифр. Попробуйте еще раз.")
    
    user_id = int(message.text)
    conn = get_db()
    user = conn.execute("SELECT full_name FROM users WHERE user_id=?", (user_id,)).fetchone()
    
    if not user:
        conn.close()
        return await message.answer("❌ Пользователь с таким ID не найден в базе. Убедитесь, что он запустил бота.")
        
    conn.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    
    await message.answer(f"✅ Пользователь <b>{user[0]}</b> назначен администратором!", reply_markup=kb_admin_main())
    await state.clear()

@dp.callback_query(F.data == "adm_del_admin")
async def adm_del_admin(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите <b>ID администратора</b>, которого хотите разжаловать:")
    await state.set_state(AdminGlobalStates.del_admin)
    await cb.answer()

@dp.message(AdminGlobalStates.del_admin)
async def process_del_admin(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("ID должен состоять только из цифр. Попробуйте еще раз.")
    
    user_id = int(message.text)
    if user_id == MAIN_ADMIN_ID:
        return await message.answer("❌ Вы не можете удалить главного администратора!")
        
    conn = get_db()
    conn.execute("UPDATE users SET is_admin=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    
    await message.answer("✅ Администратор успешно разжалован.", reply_markup=kb_admin_main())
    await state.clear()

# --- АДМИНКА: МЕРОПРИЯТИЯ ---
@dp.callback_query(F.data == "adm_events_menu")
async def adm_events_menu(cb: CallbackQuery):
    conn = get_db()
    evs = conn.execute("SELECT id, title FROM events ORDER BY id DESC LIMIT 5").fetchall()
    conn.close()
    btns = [[InlineKeyboardButton(text="➕ Создать мероприятие", callback_data="evt_create")]]
    for e in evs: btns.append([InlineKeyboardButton(text=f"📅 {e[1]}", callback_data=f"evt_menu_{e[0]}")])
    await cb.message.edit_text("Управление мероприятиями:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data == "evt_create")
async def evt_create(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Название:")
    await state.set_state(AdminEventStates.title)

@dp.message(AdminEventStates.title)
async def evt_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    await m.answer("Описание:")
    await state.set_state(AdminEventStates.desc)

@dp.message(AdminEventStates.desc)
async def evt_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.text)
    await m.answer("Дата (напр. 25 Мая 18:00):")
    await state.set_state(AdminEventStates.date)

@dp.message(AdminEventStates.date)
async def evt_date(m: Message, state: FSMContext):
    await state.update_data(date=m.text)
    await m.answer("Пришлите фото (или 'нет'):")
    await state.set_state(AdminEventStates.photo)

@dp.message(AdminEventStates.photo)
async def evt_photo(m: Message, state: FSMContext):
    photo = m.photo[-1].file_id if m.photo else None
    d = await state.get_data()
    conn = get_db()
    conn.execute("INSERT INTO events (title, description, event_date, photo_id) VALUES (?, ?, ?, ?)", (d['title'], d['desc'], d['date'], photo))
    conn.commit()
    conn.close()
    await m.answer("✅ Мероприятие создано!", reply_markup=kb_admin_main())
    await state.clear()

@dp.callback_query(F.data.startswith("evt_menu_"))
async def evt_menu(cb: CallbackQuery):
    eid = cb.data.split("_")[2]
    conn = get_db()
    e = conn.execute("SELECT title FROM events WHERE id=?", (eid,)).fetchone()
    conn.close()
    btns = [
        [InlineKeyboardButton(text="📢 Разослать анонс (АВТО)", callback_data=f"evt_cast_{eid}")],
        [InlineKeyboardButton(text="👥 Excel: Участники", callback_data=f"evt_exp_{eid}"),
         InlineKeyboardButton(text="🤖 Промпт для ИИ", callback_data=f"evt_ai_{eid}")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"evt_edit_{eid}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"evt_del_{eid}")]
    ]
    await cb.message.edit_text(f"🔧 <b>{e[0]}</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

# Авто-рассылка анонса мероприятия
@dp.callback_query(F.data.startswith("evt_cast_"))
async def evt_broadcast(cb: CallbackQuery):
    eid = cb.data.split("_")[2]
    conn = get_db()
    ev = conn.execute("SELECT title, description, event_date, photo_id FROM events WHERE id=?", (eid,)).fetchone()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    
    if not ev:
        return await cb.answer("Мероприятие не найдено!", show_alert=True)
        
    await cb.message.answer("⏳ Начинаю авторассылку анонса всем пользователям...")
    count = 0
    text = f"📢 <b>Анонс мероприятия!</b>\n\n🗓 <b>{ev[0]}</b>\n🕒 {ev[2]}\n\n{ev[1]}\n\n👇 <b>Записывайтесь через меню бота!</b>"
    kb = kb_event_actions(eid)
    
    for u in users:
        try:
            if ev[3]:
                await bot.send_photo(u[0], photo=ev[3], caption=text, reply_markup=kb)
            else:
                await bot.send_message(u[0], text=text, reply_markup=kb)
            count += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            logger.warning(f"Лимит. Ожидание {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after)
            try:
                if ev[3]:
                    await bot.send_photo(u[0], photo=ev[3], caption=text, reply_markup=kb)
                else:
                    await bot.send_message(u[0], text=text, reply_markup=kb)
                count += 1
            except Exception:
                pass
        except TelegramAPIError:
            pass
        except Exception:
            pass
            
    await cb.message.answer(f"✅ Анонс разослан. Доставлено: {count}")
    await cb.answer()

# === ИСПРАВЛЕННЫЙ БЛОК: Удаление мероприятия ===
@dp.callback_query(F.data.startswith("evt_del_"))
async def evt_delete(cb: CallbackQuery):
    try:
        # 1. Жестко конвертируем строку в число для базы данных
        eid = int(cb.data.split("_")[2])
        
        # 2. Используем контекстный менеджер для гарантии commit()
        with get_db() as conn:
            conn.execute("DELETE FROM events WHERE id=?", (eid,))
            conn.execute("DELETE FROM registrations WHERE event_id=?", (eid,))
            conn.execute("DELETE FROM submissions WHERE event_id=?", (eid,))
            conn.commit()
            
        # 3. Обязательно "гасим" индикатор загрузки у кнопки с всплывающим окном
        await cb.answer("✅ Мероприятие успешно удалено!", show_alert=True)
        
        # 4. Обновляем сообщение
        await cb.message.edit_text("✅ Мероприятие и все связанные данные удалены.", reply_markup=kb_admin_main())
    except Exception as e:
        logger.error(f"Ошибка при удалении мероприятия: {e}")
        await cb.answer("Произошла ошибка при удалении.", show_alert=True)

# Промпт ИИ для Мероприятия
@dp.callback_query(F.data.startswith("evt_ai_"))
async def evt_ai_prompt(cb: CallbackQuery):
    eid = cb.data.split("_")[2]
    conn = get_db()
    ev = conn.execute("SELECT title FROM events WHERE id=?", (eid,)).fetchone()[0]
    subs = conn.execute("SELECT text FROM submissions WHERE event_id=?", (eid,)).fetchall()
    conn.close()
    if not subs: return await cb.answer("Нет данных для анализа.", show_alert=True)
    
    data_text = "\n".join([f"- {s[0]}" for s in subs])[:3000]
    prompt = (
        f"<b>Скопируйте текст ниже и отправьте в ChatGPT/Claude:</b>\n\n"
        f"Ты — профессиональный модератор педагогических дискуссий. Проанализируй данные от участников "
        f"к мероприятию «{ev}». Сделай выжимку:\n1. ТОП-3 боли аудитории.\n2. Эмоциональный фон.\n3. Один провокационный вопрос для начала дискуссии.\n\n"
        f"Данные (вопросы и кейсы):\n{data_text}"
    )
    await cb.message.answer(prompt)
    await cb.answer()

# Редактирование Мероприятия
@dp.callback_query(F.data.startswith("evt_edit_"))
async def evt_edit(cb: CallbackQuery, state: FSMContext):
    eid = cb.data.split("_")[2]
    await state.update_data(edit_eid=eid)
    btns = [
        [InlineKeyboardButton(text="Название", callback_data="editevt_title"), InlineKeyboardButton(text="Описание", callback_data="editevt_description")],
        [InlineKeyboardButton(text="Дату", callback_data="editevt_event_date"), InlineKeyboardButton(text="Фото", callback_data="editevt_photo_id")]
    ]
    await cb.message.answer("Что изменить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@dp.callback_query(F.data.startswith("editevt_"))
async def evt_edit_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split("_")[1]
    await state.update_data(edit_field=field)
    await cb.message.answer("Пришлите новое значение:")
    await state.set_state(AdminEventStates.edit_value)
    await cb.answer()

@dp.message(AdminEventStates.edit_value)
async def evt_save_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    val = message.photo[-1].file_id if message.photo else message.text
    conn = get_db()
    conn.execute(f"UPDATE events SET {data['edit_field']}=? WHERE id=?", (val, data['edit_eid']))
    conn.commit()
    conn.close()
    await message.answer("✅ Сохранено!", reply_markup=kb_admin_main())
    await state.clear()

# Экспорт участников Мероприятия
@dp.callback_query(F.data.startswith("evt_exp_"))
async def evt_export(cb: CallbackQuery):
    eid = cb.data.split("_")[2]
    conn = get_db()
    df = pd.read_sql_query("SELECT u.full_name, u.role_type, u.phone FROM registrations r JOIN users u ON r.user_id = u.user_id WHERE r.event_id=?", conn, params=(eid,))
    conn.close()
    df.to_excel(f"event_{eid}.xlsx", index=False)
    await cb.message.answer_document(FSInputFile(f"event_{eid}.xlsx"))
    os.remove(f"event_{eid}.xlsx")
    await cb.answer()

# --- АДМИНКА: АНКЕТЫ ---
@dp.callback_query(F.data == "adm_surveys_menu")
async def adm_surv_menu(cb: CallbackQuery):
    conn = get_db()
    survs = conn.execute("SELECT id, title, status, target_role FROM surveys ORDER BY id DESC").fetchall()
    conn.close()
    btns = [[InlineKeyboardButton(text="➕ Создать анкету", callback_data="surv_create")]]
    for s in survs:
        st_e = "🟢" if s[2]=='active' else "🔴" if s[2]=='closed' else "📝"
        btns.append([InlineKeyboardButton(text=f"{st_e} {s[1]} ({s[3]})", callback_data=f"sm_{s[0]}")])
    await cb.message.edit_text("Управление анкетами:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data == "surv_create")
async def surv_create(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Название анкеты:")
    await state.set_state(AdminSurveyStates.title)

@dp.message(AdminSurveyStates.title)
async def surv_target(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    btns = [[InlineKeyboardButton(text="Все", callback_data="starg_Все")],
            [InlineKeyboardButton(text="Родители", callback_data="starg_Родитель"), InlineKeyboardButton(text="Ученики", callback_data="starg_Ученик")],
            [InlineKeyboardButton(text="Педагоги", callback_data="starg_Педагог")]]
    await m.answer("Для кого эта анкета?", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("starg_"))
async def surv_save(cb: CallbackQuery, state: FSMContext):
    targ = cb.data.split("_")[1]
    data = await state.get_data()
    conn = get_db()
    conn.execute("INSERT INTO surveys (title, target_role, created_at) VALUES (?, ?, ?)", (data['title'], targ, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    await cb.message.answer("Анкета создана! Зайдите в меню для добавления вопросов.", reply_markup=kb_admin_main())
    await state.clear()

@dp.callback_query(F.data.startswith("sm_"))
async def sm_menu(cb: CallbackQuery):
    sid = cb.data.split("_")[1]
    conn = get_db()
    surv = conn.execute("SELECT title, status, target_role FROM surveys WHERE id=?", (sid,)).fetchone()
    # Вытаскиваем добавленные вопросы для предпросмотра
    questions = conn.execute("SELECT text, q_type FROM questions WHERE survey_id=? ORDER BY id", (sid,)).fetchall()
    conn.close()
    
    q_map = {'single': 'Один выбор', 'multi': 'Множественный', 'text': 'Текст'}
    q_text_list = "\n".join([f"{i+1}. {q[0]} <i>({q_map.get(q[1], '')})</i>" for i, q in enumerate(questions)])
    if not q_text_list: q_text_list = "<i>Пока нет добавленных вопросов.</i>"

    text = f"📋 <b>{surv[0]}</b>\nДля: {surv[2]}\nСтатус: {surv[1]}\n\n<b>Список вопросов:</b>\n{q_text_list}"
    
    btns = []
    if surv[1] == 'draft':
        btns.append([InlineKeyboardButton(text="➕ Добавить вопрос (вручную)", callback_data=f"sqadd_{sid}")])
        btns.append([InlineKeyboardButton(text="📥 Импорт из Excel", callback_data=f"sqimp_{sid}")]) # <--- Новая кнопка
        if questions: btns.append([InlineKeyboardButton(text="▶️ Запустить", callback_data=f"sstat_{sid}_active")])
    elif surv[1] == 'active': 
        btns.append([InlineKeyboardButton(text="🔴 Закрыть сбор", callback_data=f"sstat_{sid}_closed")])
    elif surv[1] == 'closed':
        btns.append([InlineKeyboardButton(text="📊 Excel: Результаты", callback_data=f"sexp_{sid}")])
        btns.append([InlineKeyboardButton(text="🤖 Промпт для ИИ", callback_data=f"sai_{sid}")])
        btns.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sdel_{sid}")])
    
    btns.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_surveys_menu")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("sqadd_"))
async def surv_add_q(cb: CallbackQuery, state: FSMContext):
    await state.update_data(sid=cb.data.split("_")[1])
    btns = [
        [InlineKeyboardButton(text="Один выбор", callback_data="sqt_single"), InlineKeyboardButton(text="Множественный", callback_data="sqt_multi")],
        [InlineKeyboardButton(text="Текст (Развернутый)", callback_data="sqt_text")]
    ]
    await cb.message.answer("Тип вопроса:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("sqt_"))
async def surv_q_text(cb: CallbackQuery, state: FSMContext):
    await state.update_data(qtype=cb.data.split("_")[1])
    await cb.message.answer("Напишите сам вопрос:")
    await state.set_state(AdminSurveyStates.q_text)

@dp.message(AdminSurveyStates.q_text)
async def surv_q_opts(m: Message, state: FSMContext):
    d = await state.get_data()
    if d['qtype'] == 'text':
        conn = get_db()
        conn.execute("INSERT INTO questions (survey_id, text, q_type) VALUES (?, ?, ?)", (d['sid'], m.text, 'text'))
        conn.commit()
        await m.answer("Вопрос добавлен!", reply_markup=kb_admin_main())
        await state.clear()
    else:
        await state.update_data(qtext=m.text)
        await m.answer("Напишите варианты ответов через символ `|`\n<i>Пример: Да | Нет | Не знаю</i>")
        await state.set_state(AdminSurveyStates.q_options)

@dp.message(AdminSurveyStates.q_options)
async def surv_save_opts(m: Message, state: FSMContext):
    d = await state.get_data()
    opts = [o.strip() for o in m.text.split('|')]
    conn = get_db()
    conn.execute("INSERT INTO questions (survey_id, text, q_type, options) VALUES (?, ?, ?, ?)", (d['sid'], d['qtext'], d['qtype'], json.dumps(opts, ensure_ascii=False)))
    conn.commit()
    await m.answer("Вопрос с вариантами добавлен!", reply_markup=kb_admin_main())
    await state.clear()

@dp.callback_query(F.data.startswith("sqimp_"))
async def surv_import_start(cb: CallbackQuery, state: FSMContext):
    sid = cb.data.split("_")[1]
    await state.update_data(import_sid=sid)
    
    # 1. Создаем DataFrame с примерами
    df_template = pd.DataFrame({
        "Тип (single / multi / text)": ["single", "multi", "text"],
        "Текст вопроса": ["Ваш стаж работы?", "С какими классами вы работаете?", "Ваши пожелания организаторам?"],
        "Варианты ответа (через |)": ["Менее 1 года | 1-3 года | Более 3 лет", "Начальная школа | Средняя | Старшая", ""]
    })
    
    # 2. Сохраняем во временный Excel-файл
    file_name = f"template_survey_{sid}.xlsx"
    df_template.to_excel(file_name, index=False)
    
    # 3. Текст сообщения
    text = (
        "📥 <b>Импорт вопросов из Excel</b>\n\n"
        "Я подготовил для вас готовый шаблон. Скачайте его, удалите примеры и впишите свои вопросы (не меняя названия и порядок столбцов).\n\n"
        "Как заполните — <b>просто отправьте этот файл мне в ответном сообщении</b>."
    )
    
    # 4. Отправляем документ и удаляем временный файл с диска
    await cb.message.answer_document(FSInputFile(file_name), caption=text)
    os.remove(file_name)
    
    # Переводим бота в режим ожидания файла
    await state.set_state(AdminSurveyStates.import_excel)
    await cb.answer()

@dp.message(AdminSurveyStates.import_excel, F.document)
async def surv_import_process(message: Message, state: FSMContext):
    if not message.document.file_name.endswith('.xlsx'):
        return await message.answer("❌ Пожалуйста, отправьте файл в формате .xlsx")

    msg = await message.answer("⏳ Читаю файл...")
    data = await state.get_data()
    sid = data['import_sid']

    # Скачиваем файл в оперативную память (без сохранения на диск)
    file = await bot.get_file(message.document.file_id)
    downloaded_file = await bot.download_file(file.file_path)
    
    try:
        # Читаем Excel, пропускаем первую строку (заголовки)
        df = pd.read_excel(downloaded_file, skiprows=0) 
        
        conn = get_db()
        count = 0
        
        for index, row in df.iterrows():
            if len(row) < 2: continue # Пропускаем пустые строки
            
            q_type = str(row.iloc[0]).strip().lower()
            q_text = str(row.iloc[1]).strip()
            
            if q_type not in ['single', 'multi', 'text'] or not q_text or q_text == 'nan':
                continue # Пропускаем строки с неверным типом или без текста
                
            opts_json = None
            if q_type in ['single', 'multi']:
                if len(row) > 2 and str(row.iloc[2]) != 'nan':
                    opts = [o.strip() for o in str(row.iloc[2]).split('|') if o.strip()]
                    opts_json = json.dumps(opts, ensure_ascii=False)
                else:
                    continue # Если тип выборный, но вариантов нет — пропускаем
                    
            conn.execute("INSERT INTO questions (survey_id, text, q_type, options) VALUES (?, ?, ?, ?)",
                         (sid, q_text, q_type, opts_json))
            count += 1
            
        conn.commit()
        conn.close()
        
        await msg.edit_text(f"✅ Успешно импортировано {count} вопросов!\nВернитесь в меню анкет, чтобы проверить.", reply_markup=kb_admin_main())
        
    except Exception as e:
        logger.error(f"Ошибка импорта Excel: {e}")
        await msg.edit_text("❌ Произошла ошибка при чтении файла. Убедитесь, что формат столбцов правильный.")
        
    finally:
        await state.clear()

@dp.callback_query(F.data.startswith("sstat_"))
async def surv_status(cb: CallbackQuery):
    _, sid, st = cb.data.split("_")
    conn = get_db()
    conn.execute("UPDATE surveys SET status=? WHERE id=?", (st, sid))
    conn.commit()
    await cb.message.edit_text(f"Статус изменен на {st}.")

@dp.callback_query(F.data.startswith("sexp_"))
async def surv_export(cb: CallbackQuery):
    sid = cb.data.split("_")[1]
    conn = get_db()
    query = """
    SELECT u.user_id, u.full_name, q.text as question, a.answer_text 
    FROM answers a
    JOIN users u ON a.user_id = u.user_id
    JOIN questions q ON a.question_id = q.id
    WHERE a.survey_id = ?
    """
    df = pd.read_sql_query(query, conn, params=(sid,))
    conn.close()
    
    if df.empty: return await cb.answer("Ответов пока нет.", show_alert=True)
    
    # ПИВОТИРОВАНИЕ: Строки - пользователи, Столбцы - вопросы
    pivot_df = df.pivot_table(index=['user_id', 'full_name'], columns='question', values='answer_text', aggfunc='first').reset_index()
    pivot_df.columns.name = None # Убираем техническое имя колонок
    
    # Переименовываем для красоты
    pivot_df = pivot_df.rename(columns={'user_id': 'ИД пользователя', 'full_name': 'Имя пользователя'})
    
    filename = f"survey_{sid}_results.xlsx"
    pivot_df.to_excel(filename, index=False)
    await cb.message.answer_document(FSInputFile(filename))
    os.remove(filename)
    await cb.answer()

@dp.callback_query(F.data.startswith("sai_"))
async def surv_ai_prompt(cb: CallbackQuery):
    sid = cb.data.split("_")[1]
    conn = get_db()
    surv = conn.execute("SELECT title FROM surveys WHERE id=?", (sid,)).fetchone()[0]
    ans = conn.execute("SELECT answer_text FROM answers WHERE survey_id=?", (sid,)).fetchall()
    conn.close()
    if not ans: return await cb.answer("Нет ответов.", show_alert=True)
    
    data_text = "\n".join([f"- {a[0]}" for a in ans])[:3000]
    prompt = f"<b>Скопируйте в ChatGPT/Claude:</b>\n\nПроанализируй ответы на анкету «{surv}». Выдели главные тезисы и боли.\nДанные:\n{data_text}"
    await cb.message.answer(prompt)
    await cb.answer()

# === ИСПРАВЛЕННЫЙ БЛОК: Удаление анкеты ===
@dp.callback_query(F.data.startswith("sdel_"))
async def surv_delete(cb: CallbackQuery):
    try:
        # Конвертация в int и менеджер контекста
        sid = int(cb.data.split("_")[1])
        with get_db() as conn:
            conn.execute("DELETE FROM surveys WHERE id=?", (sid,))
            conn.execute("DELETE FROM questions WHERE survey_id=?", (sid,))
            conn.execute("DELETE FROM answers WHERE survey_id=?", (sid,))
            conn.commit()
            
        await cb.answer("✅ Анкета успешно удалена!", show_alert=True)
        await cb.message.edit_text("✅ Анкета и все ответы удалены.", reply_markup=kb_admin_main())
    except Exception as e:
        logger.error(f"Ошибка при удалении анкеты: {e}")
        await cb.answer("Произошла ошибка при удалении.", show_alert=True)

# Фоновая задача
async def background_reminder_task():
    """Фоновая задача для apscheduler. Пример: ежедневная проверка предстоящих мероприятий."""
    logger.info("Запуск фоновой задачи планировщика...")
    # Здесь можно добавить логику проверки дат в БД и автоматической отправки уведомлений
    pass

async def main():
    init_db()
    
    # Добавляем задачу в планировщик (будет запускаться каждый день в 10:00)
    scheduler.add_job(background_reminder_task, "cron", hour=10, minute=0)
    scheduler.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())