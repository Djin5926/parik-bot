import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import telebot
from telebot import types
from telebot.types import BotCommand, BotCommandScopeChat
from flask import Flask, request

# ==========================================================
# ===================== НАСТРОЙКИ ==========================
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
DB_PATH = os.getenv("DB_PATH", "barber.db")
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")

    def _now_local():
        return datetime.now(MSK)
except Exception:
    MSK = None

    def _now_local():
        return datetime.utcnow() + timedelta(hours=3)


def now_msk():
    return _now_local().replace(tzinfo=None)


ADMIN_IDS = [201946011, 515192929]
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0

if not BOT_TOKEN:
    raise ValueError("Укажите BOT_TOKEN в переменных окружения")


# ==========================================================
# ==================== КОНФИГ ==============================
# ==========================================================

DEFAULT_CONFIG = {
    "salon": {
        "name": "Парикмахерская «У Катерины»",
        "address": "ул. Чехова, д.79 корпус 1",
        "phone": "+7 985 214-57-94",
    },
    "schedule": {
        "work_start_hour": 10,
        "work_end_hour": 20,
        "slot_step_minutes": 30,
        "days_ahead": 14,
        "min_lead_minutes": 30,
        "days_off": [6],
    },
    "services": {
        "М · Стрижка мужская":            {"price": "1400 ₽",        "duration": 30},
        "М · Стрижка 1 насадкой":         {"price": "800 ₽",         "duration": 30},
        "М · Стрижка 2 насадками":        {"price": "1000 ₽",        "duration": 30},
        "М · Стрижка ножницами":          {"price": "1500 ₽",        "duration": 60},
        "М · Камуфляж седины":            {"price": "1500–2500 ₽",   "duration": 120},
        "М · Рисунки на голове":          {"price": "100–300 ₽",     "duration": 30},
        "Ж · Стрижка женская":            {"price": "2000 ₽",        "duration": 60},
        "Ж · Уход за волосами":           {"price": "2000–2500 ₽",   "duration": 60},
        "Ж · Укладка":                    {"price": "800–1500 ₽",    "duration": 90},
        "Ж · Локоны":                     {"price": "1800–2000 ₽",   "duration": 90},
        "Ж · Мытьё головы + укладка":     {"price": "1200–2000 ₽",   "duration": 60},
        "Ж · Окрашивание в 1 тон":        {"price": "5000 ₽",        "duration": 120},
        "Ж · Тонирование":                {"price": "2500–3500 ₽",   "duration": 90},
        "Ж · Мелирование + тонирование":  {"price": "10000–13000 ₽", "duration": 270},
        "Ж · Контуринг":                  {"price": "2500–5000 ₽",   "duration": 210},
        "Ж · Airtouch":                   {"price": "15000–18000 ₽", "duration": 480},
        "Ж · Стрижка чёлки":              {"price": "250–350 ₽",     "duration": 15},
    },
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, val in DEFAULT_CONFIG.items():
            if key not in data:
                data[key] = val
        return data
    except Exception as e:
        print(f"[CONFIG] {e}", flush=True)
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[CONFIG] {e}", flush=True); return False


CONFIG = load_config()
WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ==========================================================
# ====================== БОТ И БД ==========================
# ==========================================================

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

STATUS_LABELS = {"new": "🆕", "confirmed": "✅", "done": "✔️", "cancelled": "❌"}
STATUS_LABELS_LONG = {"new": "🆕 Новая", "confirmed": "✅ Подтверждена",
                      "done": "✔️ Выполнена", "cancelled": "❌ Отменена"}

user_state = {}
admin_state = {}
ab_state = {}


def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)


def init_db():
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, name TEXT, phone TEXT,
            service TEXT, price TEXT, date_time TEXT,
            status TEXT DEFAULT 'new',
            reminded_day INTEGER DEFAULT 0, reminded_hour INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_unique "
                    "ON appointments(date_time) WHERE status != 'cancelled'")
    except sqlite3.OperationalError:
        pass
    conn.commit(); conn.close()


init_db()


# ---------- Хелперы ----------

def get_services_list():
    return list(CONFIG["services"].items())


def _service_duration(name):
    return CONFIG["services"].get(name, {}).get("duration", 60)


def is_day_off(date_obj):
    return date_obj.weekday() in CONFIG["schedule"]["days_off"]


def salon_hours_text():
    s = CONFIG["schedule"]; offs = s["days_off"]
    if not offs:
        return f"{s['work_start_hour']:02d}:00–{s['work_end_hour']:02d}:00, ежедневно"
    off_names = ", ".join(WEEKDAY_NAMES[d] for d in sorted(offs))
    return (f"{s['work_start_hour']:02d}:00–{s['work_end_hour']:02d}:00, "
            f"кроме {off_names}")


def is_slot_free(date_time_str, duration_min=60, ignore_id=None):
    try:
        start = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    if is_day_off(start.date()):
        return False
    end = start + timedelta(minutes=duration_min)
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id, service, date_time FROM appointments WHERE status != 'cancelled'")
    rows = cur.fetchall(); conn.close()
    for aid, service, dt_str in rows:
        if ignore_id is not None and aid == ignore_id:
            continue
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        dt_end = dt + timedelta(minutes=_service_duration(service))
        if start < dt_end and dt < end:
            return False
    return True


def save_appointment(user_id, username, name, phone, service, price, date_time):
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO appointments
            (user_id, username, name, phone, service, price, date_time, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """, (user_id, username, name, phone, service, price, date_time, now_msk().isoformat()))
        conn.commit(); ok = True
    except sqlite3.IntegrityError:
        ok = False
    finally:
        conn.close()
    return ok


def notify_admins(text, skip_id=None):
    for admin in ADMIN_IDS:
        if admin <= 0 or admin == skip_id:
            continue
        try:
            bot.send_message(admin, text)
        except Exception as e:
            print(f"[NOTIFY] {admin}: {e}", flush=True)


# ==========================================================
# ====================== КЛАВИАТУРЫ ========================
# ==========================================================

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Услуги", "Записаться")
    kb.add("Мои записи", "Контакты")
    return kb


def services_inline(prefix="svc", cancel_cb="cancel_flow"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (name, info) in enumerate(get_services_list()):
        kb.add(types.InlineKeyboardButton(
            text=f"{name} — {info['price']} (~{info['duration']} мин)",
            callback_data=f"{prefix}:{i}",
        ))
    kb.add(types.InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb))
    return kb


def calendar_inline(prefix="date", back_cb="back_to_services", cancel_cb="cancel_flow"):
    kb = types.InlineKeyboardMarkup(row_width=3)
    today = now_msk().date()
    buttons = []
    for i in range(CONFIG["schedule"]["days_ahead"]):
        d = today + timedelta(days=i)
        if is_day_off(d):
            buttons.append(types.InlineKeyboardButton(
                text=f"🚫 {d.strftime('%d.%m')}", callback_data="day_off"))
        else:
            buttons.append(types.InlineKeyboardButton(
                text=d.strftime("%d.%m"), callback_data=f"{prefix}:{d.isoformat()}"))
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    kb.add(types.InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb))
    return kb


def time_slots_inline(date_str, duration_min=60, prefix="time",
                       back_cb="back_to_calendar", cancel_cb="cancel_flow"):
    kb = types.InlineKeyboardMarkup(row_width=4)
    s = CONFIG["schedule"]
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        date_obj = None
    if date_obj and is_day_off(date_obj):
        kb.add(types.InlineKeyboardButton(text="Выходной", callback_data="busy"))
        kb.add(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
        return kb

    day_start = datetime.strptime(f"{date_str} {s['work_start_hour']:02d}:00", "%Y-%m-%d %H:%M")
    day_end = datetime.strptime(f"{date_str} {s['work_end_hour']:02d}:00", "%Y-%m-%d %H:%M")
    earliest = now_msk() + timedelta(minutes=s["min_lead_minutes"])

    t = day_start; buttons = []
    while t < day_end:
        slot_human = t.strftime("%Y-%m-%d %H:%M")
        slot_cb = t.strftime("%Y-%m-%d|%H:%M")
        label = t.strftime("%H:%M")
        if t + timedelta(minutes=duration_min) > day_end:
            break
        if t < earliest:
            buttons.append(types.InlineKeyboardButton(text=f"·{label}", callback_data="busy"))
        elif is_slot_free(slot_human, duration_min):
            buttons.append(types.InlineKeyboardButton(text=label, callback_data=f"{prefix}:{slot_cb}"))
        else:
            buttons.append(types.InlineKeyboardButton(text=f"❌{label}", callback_data="busy"))
        t += timedelta(minutes=s["slot_step_minutes"])

    if not buttons:
        kb.add(types.InlineKeyboardButton(text="Нет доступного времени", callback_data="busy"))
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    kb.add(types.InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb))
    return kb


# ==========================================================
# ================== БАЗОВЫЕ ОБРАБОТЧИКИ ===================
# ==========================================================

@bot.message_handler(commands=["start"])
def start(message):
    parts = message.text.split(maxsplit=1)
    payload = parts[1].strip().lower() if len(parts) > 1 else ""
    print(f"[HANDLER] /start '{payload}' от {message.from_user.id}", flush=True)
    user_state.pop(message.from_user.id, None)
    admin_state.pop(message.from_user.id, None)
    ab_state.pop(message.from_user.id, None)

    if payload in ("book", "zapis"): book_start(message); return
    if payload in ("my", "appointments"): my_appointments(message); return
    if payload in ("help", "contacts"): contacts(message); return

    bot.send_message(
        message.chat.id,
        f"Здравствуйте! Это бот {CONFIG['salon']['name']}.\n"
        f"Я помогу вам записаться. Выберите действие:",
        reply_markup=main_menu(),
    )


@bot.message_handler(commands=["help"])
def help_cmd(message): contacts(message)


@bot.message_handler(commands=["book"])
def book_cmd(message): book_start(message)


@bot.message_handler(commands=["my"])
def my_cmd(message): my_appointments(message)


@bot.message_handler(func=lambda m: m.text == "Услуги")
def show_services(message):
    services = CONFIG["services"]
    male = [f"• {n.replace('М · ', '')} — {i['price']} (~{i['duration']} мин)"
            for n, i in services.items() if n.startswith("М ·")]
    female = [f"• {n.replace('Ж · ', '')} — {i['price']} (~{i['duration']} мин)"
              for n, i in services.items() if n.startswith("Ж ·")]
    text = ("✂️ <b>Мужской зал</b>\n" + "\n".join(male) + "\n\n"
            "💇‍♀️ <b>Женский зал</b>\n" + "\n".join(female) + "\n\n"
            f"🕒 Работаем: {salon_hours_text()}")
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(func=lambda m: m.text == "Контакты")
def contacts(message):
    s = CONFIG["salon"]
    bot.send_message(message.chat.id,
                     f"📍 {s['address']}\n📞 {s['phone']}\n🕒 {salon_hours_text()}")


# ==========================================================
# ====================== ЗАПИСЬ КЛИЕНТА ====================
# ==========================================================

@bot.message_handler(func=lambda m: m.text == "Записаться")
def book_start(message):
    user_state[message.from_user.id] = {}
    bot.send_message(message.chat.id, "Выберите услугу:", reply_markup=services_inline())


@bot.callback_query_handler(func=lambda c: c.data.startswith("svc:"))
def cb_service(call):
    idx = int(call.data.split(":", 1)[1])
    services_list = get_services_list()
    if idx >= len(services_list):
        bot.answer_callback_query(call.id, "Услуга недоступна"); return
    name, info = services_list[idx]
    state = user_state.setdefault(call.from_user.id, {})
    state["service"] = name; state["price"] = info["price"]
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"Услуга: {name} ({info['price']}, ~{info['duration']} мин)\n\nВыберите дату:",
        chat_id=call.message.chat.id, message_id=call.message.message_id,
        reply_markup=calendar_inline())


@bot.callback_query_handler(func=lambda c: c.data == "back_to_services")
def cb_back_to_services(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("Выберите услугу:", chat_id=call.message.chat.id,
                          message_id=call.message.message_id, reply_markup=services_inline())


@bot.callback_query_handler(func=lambda c: c.data == "day_off")
def cb_day_off(call):
    bot.answer_callback_query(call.id, "Это выходной. Выберите другой день.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("date:"))
def cb_date(call):
    date_str = call.data.split(":", 1)[1]
    state = user_state.setdefault(call.from_user.id, {})
    if "service" not in state:
        bot.answer_callback_query(call.id, "Сначала выберите услугу"); return
    duration = _service_duration(state["service"])
    state["date"] = date_str
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"Услуга: {state['service']}\nДата: {date_str}\n\nВыберите время:",
        chat_id=call.message.chat.id, message_id=call.message.message_id,
        reply_markup=time_slots_inline(date_str, duration))


@bot.callback_query_handler(func=lambda c: c.data == "back_to_calendar")
def cb_back(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("Выберите дату:", chat_id=call.message.chat.id,
                          message_id=call.message.message_id, reply_markup=calendar_inline())


@bot.callback_query_handler(func=lambda c: c.data == "busy")
def cb_busy(call):
    bot.answer_callback_query(call.id, "Это время недоступно")


@bot.callback_query_handler(func=lambda c: c.data == "cancel_flow")
def cb_cancel_flow(call):
    user_state.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "Отменено")
    bot.send_message(call.message.chat.id, "Запись отменена.", reply_markup=main_menu())


@bot.callback_query_handler(func=lambda c: c.data.startswith("time:"))
def cb_time(call):
    slot = call.data.split(":", 1)[1].replace("|", " ")
    state = user_state.setdefault(call.from_user.id, {})
    if "service" not in state:
        bot.answer_callback_query(call.id, "Сначала выберите услугу"); return
    duration = _service_duration(state["service"])
    if not is_slot_free(slot, duration):
        bot.answer_callback_query(call.id, "Это время уже занято"); return
    state["datetime"] = slot
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"Услуга: {state['service']}\nВремя: {slot}\n\nВведите ваше имя:",
        chat_id=call.message.chat.id, message_id=call.message.message_id)
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, step_name)


def step_name(message):
    name = message.text.strip()
    if len(name) < 2:
        msg = bot.send_message(message.chat.id, "Имя слишком короткое, введите ещё раз:")
        bot.register_next_step_handler(msg, step_name); return
    user_state.setdefault(message.from_user.id, {})["name"] = name
    msg = bot.send_message(message.chat.id, "Введите номер телефона для связи:")
    bot.register_next_step_handler(msg, step_phone)


def step_phone(message):
    phone = message.text.strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        msg = bot.send_message(message.chat.id, "Похоже, номер некорректный. Введите ещё раз:")
        bot.register_next_step_handler(msg, step_phone); return
    state = user_state.get(message.from_user.id, {})
    if not all(k in state for k in ("service", "price", "datetime", "name")):
        bot.send_message(message.chat.id, "Что-то пошло не так. Начните заново.",
                         reply_markup=main_menu())
        user_state.pop(message.from_user.id, None); return
    duration = _service_duration(state["service"])
    if not is_slot_free(state["datetime"], duration):
        bot.send_message(message.chat.id, "Увы, это время только что заняли.",
                         reply_markup=main_menu())
        user_state.pop(message.from_user.id, None); return
    ok = save_appointment(
        user_id=message.from_user.id, username=message.from_user.username or "",
        name=state["name"], phone=phone, service=state["service"],
        price=state["price"], date_time=state["datetime"])
    if not ok:
        bot.send_message(message.chat.id, "Увы, это время только что заняли.",
                         reply_markup=main_menu())
        user_state.pop(message.from_user.id, None); return
    bot.send_message(
        message.chat.id,
        f"✅ Вы записаны!\n\nУслуга: {state['service']}\n"
        f"Стоимость: {state['price']}\nДата и время: {state['datetime']}\n\n"
        f"Мы свяжемся с вами для подтверждения.\nАдрес: {CONFIG['salon']['address']}",
        reply_markup=main_menu())
    notify_admins(
        f"🆕 Новая запись!\n\nУслуга: {state['service']} ({state['price']})\n"
        f"Имя: {state['name']}\nТелефон: {phone}\nДата и время: {state['datetime']}\n"
        f"Клиент: @{message.from_user.username or '—'} (ID: {message.from_user.id})")
    user_state.pop(message.from_user.id, None)


# ==========================================================
# ================== МОИ ЗАПИСИ ============================
# ==========================================================

@bot.message_handler(func=lambda m: m.text == "Мои записи")
def my_appointments(message):
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT id, service, date_time, status, price FROM appointments
                   WHERE user_id = ? AND status != 'cancelled'
                   ORDER BY date_time ASC""", (message.from_user.id,))
    rows = cur.fetchall(); conn.close()
    if not rows:
        bot.send_message(message.chat.id, "У вас пока нет активных записей."); return
    for appt_id, service, dt, status, price in rows:
        text = (f"Услуга: {service} ({price})\nДата: {dt}\n"
                f"Статус: {STATUS_LABELS_LONG.get(status, status)}")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Отменить запись", callback_data=f"cancel:{appt_id}"))
        bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel_appointment(call):
    appt_id = int(call.data.split(":", 1)[1])
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT user_id, service, date_time, status FROM appointments WHERE id = ?", (appt_id,))
    row = cur.fetchone()
    if not row or row[0] != call.from_user.id:
        conn.close(); bot.answer_callback_query(call.id, "Запись не найдена"); return
    if row[3] == "cancelled":
        conn.close(); bot.answer_callback_query(call.id, "Уже отменена"); return
    cur.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))
    conn.commit(); conn.close()
    bot.answer_callback_query(call.id, "Отменено")
    bot.edit_message_text(f"❌ Запись отменена: {row[1]} на {row[2]}",
                          chat_id=call.message.chat.id, message_id=call.message.message_id)
    notify_admins(f"❌ Клиент отменил запись #{appt_id}: {row[1]} на {row[2]}")


# ==========================================================
# ==================== АДМИН-ПАНЕЛЬ ========================
# ==========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_root_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📅 Календарь", callback_data="adm:all"),
        types.InlineKeyboardButton("📋 Управление", callback_data="adm:manage"),
    )
    kb.add(types.InlineKeyboardButton("➕ Записать клиента", callback_data="adm:new_booking"))
    kb.add(
        types.InlineKeyboardButton("💰 Прайс", callback_data="adm:price"),
        types.InlineKeyboardButton("🕒 Расписание", callback_data="adm:schedule"),
    )
    kb.add(types.InlineKeyboardButton("📞 Инфо", callback_data="adm:info"))
    return kb


def admin_root_text():
    return "🛠 <b>Админ-панель</b>\n\nЧто будем делать?"


@bot.message_handler(commands=["admin"])
def admin(message):
    print(f"[HANDLER] /admin от {message.from_user.id}", flush=True)
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "У вас нет доступа."); return
    admin_state.pop(message.from_user.id, None)
    ab_state.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, admin_root_text(),
                     reply_markup=admin_root_kb(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data == "adm:root")
def cb_adm_root(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    admin_state.pop(call.from_user.id, None)
    ab_state.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(admin_root_text(), chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=admin_root_kb(), parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, admin_root_text(),
                         reply_markup=admin_root_kb(), parse_mode="HTML")


# ---------- КАЛЕНДАРЬ ----------

def _count_bookings_on(date_obj):
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM appointments "
                "WHERE status != 'cancelled' AND date_time LIKE ?",
                (f"{date_obj.isoformat()}%",))
    n = cur.fetchone()[0]; conn.close()
    return n


def admin_calendar_kb():
    kb = types.InlineKeyboardMarkup(row_width=3)
    today = now_msk().date()
    buttons = []
    for i in range(CONFIG["schedule"]["days_ahead"]):
        d = today + timedelta(days=i)
        wd = WEEKDAY_NAMES[d.weekday()]
        if is_day_off(d):
            buttons.append(types.InlineKeyboardButton(
                text=f"🚫 {wd} {d.strftime('%d.%m')}", callback_data="day_off"))
        else:
            n = _count_bookings_on(d)
            label = f"{wd} {d.strftime('%d.%m')}"
            if n:
                label += f" · {n}"
            buttons.append(types.InlineKeyboardButton(
                text=label, callback_data=f"adm:cal_day:{d.isoformat()}"))
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="adm:root"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "adm:all")
def cb_adm_all(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    bot.answer_callback_query(call.id)
    text = ("📅 <b>Календарь записей</b>\n\n"
            "Тапните по дню, чтобы увидеть все слоты.\n"
            "Цифра рядом — количество записей.\n🚫 — выходной.")
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=admin_calendar_kb(), parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, text,
                         reply_markup=admin_calendar_kb(), parse_mode="HTML")


def _build_day_view(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return ("Неверная дата", None)
    wd = WEEKDAY_NAMES[d.weekday()]

    if is_day_off(d):
        text = f"📅 <b>{wd}, {d.strftime('%d.%m.%Y')}</b>\n\n🚫 Выходной день."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ К календарю", callback_data="adm:all"))
        return (text, kb)

    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT id, name, service, date_time, status FROM appointments
                   WHERE status != 'cancelled' AND date_time LIKE ?
                   ORDER BY date_time ASC""", (f"{date_str}%",))
    rows = cur.fetchall(); conn.close()

    busy_slots = {}
    for appt_id, name, service, dt_str, status in rows:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        dur = _service_duration(service)
        end = dt + timedelta(minutes=dur)
        t = dt
        while t < end:
            busy_slots[t.strftime("%H:%M")] = (appt_id, name, service, status)
            t += timedelta(minutes=CONFIG["schedule"]["slot_step_minutes"])

    lines = [f"📅 <b>{wd}, {d.strftime('%d.%m.%Y')}</b>", ""]
    if rows:
        lines.append(f"<b>Записей: {len(rows)}</b>")
        for appt_id, name, service, dt_str, status in rows:
            t = dt_str.split(" ")[1]
            st = STATUS_LABELS.get(status, "")
            svc = service.replace('М · ', 'М ').replace('Ж · ', 'Ж ')
            lines.append(f"  <b>{t}</b> · {name} · {svc} · {st}")
    else:
        lines.append("<i>Записей нет — весь день свободен.</i>")
    lines.append("")
    lines.append("<i>🟢 — свободно, 🔴 — занято. Тапните по слоту.</i>")
    text = "\n".join(lines)

    s = CONFIG["schedule"]
    day_start = datetime.strptime(f"{date_str} {s['work_start_hour']:02d}:00", "%Y-%m-%d %H:%M")
    day_end = datetime.strptime(f"{date_str} {s['work_end_hour']:02d}:00", "%Y-%m-%d %H:%M")
    t = day_start; btns = []
    while t < day_end:
        label = t.strftime("%H:%M")
        if label in busy_slots:
            appt_id = busy_slots[label][0]
            btns.append(types.InlineKeyboardButton(
                text=f"🔴 {label}", callback_data=f"adm:slot:{appt_id}"))
        else:
            slot_cb = t.strftime("%Y-%m-%d|%H:%M")
            btns.append(types.InlineKeyboardButton(
                text=f"🟢 {label}", callback_data=f"adm:freeslot:{slot_cb}"))
        t += timedelta(minutes=s["slot_step_minutes"])

    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton(
        "➕ Записать на этот день", callback_data=f"adm:book_on:{date_str}"))
    kb.add(types.InlineKeyboardButton("⬅️ К календарю", callback_data="adm:all"))
    kb.add(types.InlineKeyboardButton("🏠 В админ-панель", callback_data="adm:root"))
    return (text, kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:cal_day:"))
def cb_adm_cal_day(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    date_str = call.data.split(":", 2)[2]
    text, kb = _build_day_view(date_str)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:freeslot:"))
def cb_adm_freeslot(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    slot = call.data.split(":", 2)[2].replace("|", " ")
    bot.answer_callback_query(call.id, f"{slot} — свободно", show_alert=False)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:slot:"))
def cb_adm_slot(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    try:
        appt_id = int(call.data.split(":", 2)[2])
    except ValueError:
        bot.answer_callback_query(call.id, "Ошибка"); return

    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT name, phone, service, price, date_time, status, user_id
                   FROM appointments WHERE id = ?""", (appt_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        bot.answer_callback_query(call.id, "Запись не найдена"); return

    name, phone, service, price, dt, status, client_uid = row
    date_str = dt.split(" ")[0]
    time_str = dt.split(" ")[1]

    text = (f"<b>Запись #{appt_id}</b>\n\n"
            f"👤 {name}\n📞 {phone}\n"
            f"💇 {service}\n💵 {price}\n"
            f"🕒 {date_str} в {time_str}\n"
            f"Статус: {STATUS_LABELS_LONG.get(status, status)}")

    kb = types.InlineKeyboardMarkup(row_width=2)
    if status == "new":
        kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"st:confirmed:{appt_id}"))
    if status in ("new", "confirmed"):
        kb.add(types.InlineKeyboardButton("✔️ Выполнено", callback_data=f"st:done:{appt_id}"))
        kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"st:cancelled:{appt_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ К дню", callback_data=f"adm:cal_day:{date_str}"))
    kb.add(types.InlineKeyboardButton("🏠 В админ-панель", callback_data="adm:root"))

    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


# ---------- Управление записями ----------

@bot.callback_query_handler(func=lambda c: c.data == "adm:manage")
def cb_adm_manage(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    bot.answer_callback_query(call.id)
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT id, name, phone, service, date_time, status FROM appointments
                   WHERE status IN ('new','confirmed') ORDER BY date_time ASC LIMIT 20""")
    rows = cur.fetchall(); conn.close()

    kb_back = types.InlineKeyboardMarkup()
    kb_back.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="adm:root"))

    if not rows:
        bot.edit_message_text("Активных записей нет.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id, reply_markup=kb_back)
        return
    bot.edit_message_text("📋 <b>Управление записями</b>", chat_id=call.message.chat.id,
                          message_id=call.message.message_id, parse_mode="HTML")
    for appt_id, name, phone, service, dt, status in rows:
        text = (f"#{appt_id}\nКлиент: {name}\nТелефон: {phone}\n"
                f"Услуга: {service}\nВремя: {dt}\n"
                f"Статус: {STATUS_LABELS_LONG.get(status, status)}")
        kb = types.InlineKeyboardMarkup(row_width=2)
        if status == "new":
            kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"st:confirmed:{appt_id}"))
        kb.add(types.InlineKeyboardButton("✔️ Выполнено", callback_data=f"st:done:{appt_id}"))
        kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"st:cancelled:{appt_id}"))
        bot.send_message(call.message.chat.id, text, reply_markup=kb)
    bot.send_message(call.message.chat.id, "—", reply_markup=kb_back)


@bot.callback_query_handler(func=lambda c: c.data.startswith("st:"))
def cb_status(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    _, new_status, appt_id_str = call.data.split(":")
    appt_id = int(appt_id_str)
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT user_id, service, date_time FROM appointments WHERE id = ?", (appt_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); bot.answer_callback_query(call.id, "Не найдено"); return
    cur.execute("UPDATE appointments SET status = ? WHERE id = ?", (new_status, appt_id))
    conn.commit(); conn.close()
    bot.answer_callback_query(call.id, f"Статус: {STATUS_LABELS_LONG.get(new_status, new_status)}")
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                      message_id=call.message.message_id, reply_markup=None)
    except Exception:
        pass
    client_id, service, dt = row
    try:
        if new_status == "confirmed":
            bot.send_message(client_id, f"✅ Ваша запись подтверждена: {service} — {dt}")
        elif new_status == "cancelled":
            bot.send_message(client_id, f"❌ К сожалению, запись отменена: {service} — {dt}")
        elif new_status == "done":
            bot.send_message(client_id, "Спасибо за визит! Ждём вас снова ✂️")
    except Exception as e:
        print("notify client:", e, flush=True)


# ---------- ЗАПИСЬ КЛИЕНТА АДМИНОМ ----------
# Порядок: дата → время → услуга → имя → телефон

def _admin_show_date_picker(call, edit=True):
    """Шаг 1: выбор даты."""
    text = ("➕ <b>Новая запись</b>\n\n"
            "Шаг 1/4. Выберите дату:")
    kb = calendar_inline(prefix="ab_date", back_cb="adm:root", cancel_cb="ab_cancel")
    if edit:
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            print(f"[AB] edit date failed: {e}", flush=True)
    bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


def _admin_show_time_picker(call, date_str):
    """Шаг 2: выбор времени."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        wd = WEEKDAY_NAMES[d.weekday()]
    except ValueError:
        wd = ""

    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT name, service, date_time FROM appointments
                   WHERE status != 'cancelled' AND date_time LIKE ?
                   ORDER BY date_time ASC""", (f"{date_str}%",))
    busy_rows = cur.fetchall(); conn.close()

    lines = ["➕ <b>Новая запись</b>", f"📅 {wd} {date_str}", ""]
    if busy_rows:
        lines.append("<b>Уже занято:</b>")
        for bname, bsvc, bdt in busy_rows:
            t = bdt.split(" ")[1]
            svc = bsvc.replace('М · ', 'М ').replace('Ж · ', 'Ж ')
            lines.append(f"  {t} — {bname} ({svc})")
        lines.append("")
    lines.append("Шаг 2/4. Выберите время:")
    text = "\n".join(lines)

    default_dur = CONFIG["schedule"]["slot_step_minutes"]
    kb = time_slots_inline(date_str, default_dur, prefix="ab_time",
                            back_cb="ab_back_to_date", cancel_cb="ab_cancel")
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        print(f"[AB] edit time failed: {e}", flush=True)
        bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


def _admin_show_service_picker(call, time_str):
    """Шаг 3: выбор услуги."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (name, info) in enumerate(get_services_list()):
        kb.add(types.InlineKeyboardButton(
            text=f"{name} — {info['price']} (~{info['duration']} мин)",
            callback_data=f"abs_svc:{i}"))
    kb.add(types.InlineKeyboardButton("⬅️ К выбору времени", callback_data="ab_back_to_time"))
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="ab_cancel"))

    t_label = time_str.split(" ")[-1]
    text = (f"➕ <b>Новая запись</b>\n"
            f"🕒 Время: <b>{t_label}</b>\n\n"
            f"Шаг 3/4. Выберите услугу:")
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        print(f"[AB] edit svc failed: {e}", flush=True)
        bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data == "adm:new_booking")
def cb_adm_new_booking(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    ab_state[call.from_user.id] = {}
    bot.answer_callback_query(call.id)
    _admin_show_date_picker(call)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:book_on:"))
def cb_adm_book_on(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    print(f"[BOOK_ON] {call.from_user.id} → {call.data}", flush=True)
    try:
        date_str = call.data.split(":", 2)[2]
    except IndexError:
        bot.answer_callback_query(call.id, "Ошибка данных"); return
    ab_state[call.from_user.id] = {"date": date_str}
    bot.answer_callback_query(call.id, "Выбираем время")
    _admin_show_time_picker(call, date_str)


# --- Шаг 1: обработка выбора даты ---

@bot.callback_query_handler(func=lambda c: c.data.startswith("ab_date:"))
def cb_ab_date(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    date_str = call.data.split(":", 1)[1]
    st = ab_state.setdefault(call.from_user.id, {})
    st["date"] = date_str
    bot.answer_callback_query(call.id)
    _admin_show_time_picker(call, date_str)


@bot.callback_query_handler(func=lambda c: c.data == "ab_back_to_date")
def cb_ab_back_to_date(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    bot.answer_callback_query(call.id)
    _admin_show_date_picker(call)


# --- Шаг 2: обработка выбора времени ---

@bot.callback_query_handler(func=lambda c: c.data.startswith("ab_time:"))
def cb_ab_time(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    slot = call.data.split(":", 1)[1].replace("|", " ")
    st = ab_state.setdefault(call.from_user.id, {})
    if "date" not in st:
        bot.answer_callback_query(call.id, "Сначала выберите дату"); return
    st["time"] = slot
    bot.answer_callback_query(call.id, f"Время: {slot.split(' ')[-1]}")
    _admin_show_service_picker(call, slot)


@bot.callback_query_handler(func=lambda c: c.data == "ab_back_to_time")
def cb_ab_back_to_time(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    st = ab_state.get(call.from_user.id) or {}
    if "date" not in st:
        bot.answer_callback_query(call.id, "Дата потерялась")
        _admin_show_date_picker(call); return
    bot.answer_callback_query(call.id)
    _admin_show_time_picker(call, st["date"])


# --- Шаг 3: обработка выбора услуги ---

@bot.callback_query_handler(func=lambda c: c.data.startswith("abs_svc:"))
def cb_abs_svc(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    try:
        idx = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Ошибка"); return
    sl = get_services_list()
    if idx >= len(sl):
        bot.answer_callback_query(call.id, "Услуга недоступна"); return
    name, info = sl[idx]
    st = ab_state.setdefault(call.from_user.id, {})
    st["service"] = name
    st["price"] = info["price"]

    dt = st.get("time")
    dur = info["duration"]
    if dt and not is_slot_free(dt, dur):
        bot.answer_callback_query(call.id, "Услуга не помещается — выберите другое время",
                                   show_alert=True)
        _admin_show_time_picker(call, st["date"])
        return

    bot.answer_callback_query(call.id)
    try:
        d = datetime.strptime(st.get("date", ""), "%Y-%m-%d").date()
        wd = WEEKDAY_NAMES[d.weekday()]
    except (ValueError, TypeError):
        wd = ""

    bot.edit_message_text(
        f"➕ <b>Новая запись</b>\n\n"
        f"📅 {wd} {st.get('date','')}\n"
        f"🕒 {st.get('time','')}\n"
        f"💇 <b>{name}</b> ({info['price']}, ~{info['duration']} мин)\n\n"
        f"Шаг 4/4. Введите имя клиента:",
        chat_id=call.message.chat.id, message_id=call.message.message_id,
        parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, ab_step_name)


@bot.callback_query_handler(func=lambda c: c.data == "ab_cancel")
def cb_ab_cancel(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    ab_state.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "Отменено")
    bot.send_message(call.message.chat.id, "Оформление записи отменено.",
                     reply_markup=admin_root_kb())


def ab_step_name(message):
    if not is_admin(message.from_user.id): return
    name = message.text.strip()
    if len(name) < 2:
        msg = bot.send_message(message.chat.id, "Слишком коротко. Введите ещё раз:")
        bot.register_next_step_handler(msg, ab_step_name); return
    ab_state.setdefault(message.from_user.id, {})["name"] = name
    msg = bot.send_message(message.chat.id, "Введите телефон клиента:")
    bot.register_next_step_handler(msg, ab_step_phone)


def ab_step_phone(message):
    if not is_admin(message.from_user.id): return
    phone = message.text.strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        msg = bot.send_message(message.chat.id, "Похоже, номер некорректный. Введите ещё раз:")
        bot.register_next_step_handler(msg, ab_step_phone); return

    st = ab_state.get(message.from_user.id, {})
    if not all(k in st for k in ("service", "price", "time", "name")):
        bot.send_message(message.chat.id, "Что-то потерялось. Начните заново.",
                         reply_markup=admin_root_kb())
        ab_state.pop(message.from_user.id, None); return

    duration = _service_duration(st["service"])
    if not is_slot_free(st["time"], duration):
        bot.send_message(message.chat.id, "Увы, это время только что заняли.",
                         reply_markup=admin_root_kb())
        ab_state.pop(message.from_user.id, None); return

    ok = save_appointment(
        user_id=0, username="", name=st["name"], phone=phone,
        service=st["service"], price=st["price"], date_time=st["time"])
    if not ok:
        bot.send_message(message.chat.id, "Увы, это время только что заняли.",
                         reply_markup=admin_root_kb())
        ab_state.pop(message.from_user.id, None); return

    bot.send_message(
        message.chat.id,
        f"✅ <b>Запись оформлена</b>\n\n"
        f"👤 {st['name']}\n📞 {phone}\n"
        f"💇 {st['service']} ({st['price']})\n"
        f"🕒 {st['time']}",
        reply_markup=admin_root_kb(), parse_mode="HTML")

    notify_admins(
        f"🆕 Запись (оформил админ)\n\n"
        f"Клиент: {st['name']}\nТелефон: {phone}\n"
        f"Услуга: {st['service']} ({st['price']})\nДата и время: {st['time']}",
        skip_id=message.from_user.id)
    ab_state.pop(message.from_user.id, None)


# ---------- Прайс ----------

def price_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (name, info) in enumerate(get_services_list()):
        kb.add(types.InlineKeyboardButton(
            text=f"{name} — {info['price']} ({info['duration']} мин)",
            callback_data=f"pr:svc:{i}"))
    kb.add(types.InlineKeyboardButton("➕ Добавить услугу", callback_data="pr:add"))
    kb.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="adm:root"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "adm:price")
def cb_adm_price(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    bot.answer_callback_query(call.id)
    bot.edit_message_text("💰 <b>Прайс</b>\n\nВыберите услугу, чтобы изменить:",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          reply_markup=price_kb(), parse_mode="HTML")


def service_card_kb(idx):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✏️ Название", callback_data=f"pr:edit_name:{idx}"),
        types.InlineKeyboardButton("💵 Цена", callback_data=f"pr:edit_price:{idx}"),
    )
    kb.add(
        types.InlineKeyboardButton("⏱ Время", callback_data=f"pr:edit_dur:{idx}"),
        types.InlineKeyboardButton("🗑 Удалить", callback_data=f"pr:del:{idx}"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ К прайсу", callback_data="adm:price"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data.startswith("pr:svc:"))
def cb_pr_svc(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    idx = int(call.data.split(":")[2])
    services_list = get_services_list()
    if idx >= len(services_list):
        bot.answer_callback_query(call.id, "Услуга недоступна"); return
    name, info = services_list[idx]
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"<b>{name}</b>\n\n💵 Цена: {info['price']}\n⏱ Длительность: {info['duration']} мин",
        chat_id=call.message.chat.id, message_id=call.message.message_id,
        reply_markup=service_card_kb(idx), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("pr:edit_name:"))
def cb_pr_edit_name(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    idx = int(call.data.split(":")[2])
    services_list = get_services_list()
    if idx >= len(services_list):
        bot.answer_callback_query(call.id, "Услуга недоступна"); return
    name, _ = services_list[idx]
    admin_state[call.from_user.id] = {"action": "edit_name", "idx": idx, "old": name}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     f"Текущее название: <b>{name}</b>\n\nПришлите новое (или /cancel):",
                     parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, adm_step_value, "name")


@bot.callback_query_handler(func=lambda c: c.data.startswith("pr:edit_price:"))
def cb_pr_edit_price(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    idx = int(call.data.split(":")[2])
    services_list = get_services_list()
    if idx >= len(services_list):
        bot.answer_callback_query(call.id, "Услуга недоступна"); return
    name, info = services_list[idx]
    admin_state[call.from_user.id] = {"action": "edit_price", "idx": idx, "old": name}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     f"Текущая цена: <b>{info['price']}</b>\n\n"
                     f"Пришлите новую (например: 1500 ₽ или 1500–2000 ₽).\nИли /cancel.",
                     parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, adm_step_value, "price")


@bot.callback_query_handler(func=lambda c: c.data.startswith("pr:edit_dur:"))
def cb_pr_edit_dur(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    idx = int(call.data.split(":")[2])
    services_list = get_services_list()
    if idx >= len(services_list):
        bot.answer_callback_query(call.id, "Услуга недоступна"); return
    name, info = services_list[idx]
    admin_state[call.from_user.id] = {"action": "edit_dur", "idx": idx, "old": name}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     f"Текущая длительность: <b>{info['duration']} мин</b>\n\n"
                     f"Пришлите новое значение в минутах. Или /cancel.",
                     parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, adm_step_value, "duration")


@bot.callback_query_handler(func=lambda c: c.data.startswith("pr:del:"))
def cb_pr_del(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    idx = int(call.data.split(":")[2])
    services_list = get_services_list()
    if idx >= len(services_list):
        bot.answer_callback_query(call.id, "Услуга недоступна"); return
    name, _ = services_list[idx]
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"pr:del_yes:{idx}"),
        types.InlineKeyboardButton("❌ Нет", callback_data=f"pr:svc:{idx}"),
    )
    bot.answer_callback_query(call.id)
    bot.edit_message_text(f"Удалить услугу <b>{name}</b>?",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("pr:del_yes:"))
def cb_pr_del_yes(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    idx = int(call.data.split(":")[2])
    services_list = get_services_list()
    if idx >= len(services_list):
        bot.answer_callback_query(call.id, "Не найдена"); return
    name = services_list[idx][0]
    CONFIG["services"].pop(name, None); save_config()
    bot.answer_callback_query(call.id, f"Удалено: {name}")
    bot.edit_message_text("💰 <b>Прайс</b>\n\nУслуга удалена. Выберите другую:",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          reply_markup=price_kb(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data == "pr:add")
def cb_pr_add(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    admin_state[call.from_user.id] = {"action": "add_name"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     "➕ Новая услуга.\n\nПришлите <b>название</b> (можно «М · » или «Ж · »).\n"
                     "Или /cancel.", parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, adm_step_value, "add_name")


# ---------- Расписание ----------

def schedule_kb():
    s = CONFIG["schedule"]
    off_names = ", ".join(WEEKDAY_NAMES[d] for d in sorted(s["days_off"])) or "нет"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(f"🕐 Начало: {s['work_start_hour']:02d}:00",
                                      callback_data="adm:sch_start"))
    kb.add(types.InlineKeyboardButton(f"🕗 Конец: {s['work_end_hour']:02d}:00",
                                      callback_data="adm:sch_end"))
    kb.add(types.InlineKeyboardButton(f"🚫 Выходные: {off_names}",
                                      callback_data="adm:sch_offs"))
    kb.add(types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="adm:root"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "adm:schedule")
def cb_adm_schedule(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    bot.answer_callback_query(call.id)
    bot.edit_message_text("🕒 <b>Расписание</b>\n\nЧто меняем?",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          reply_markup=schedule_kb(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data == "adm:sch_start")
def cb_adm_sch_start(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    admin_state[call.from_user.id] = {"action": "sch_start"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     f"Текущее начало: {CONFIG['schedule']['work_start_hour']:02d}:00\n\n"
                     f"Пришлите новый час (0–23), например <b>9</b>. Или /cancel.",
                     parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, adm_step_value, "sch_start")


@bot.callback_query_handler(func=lambda c: c.data == "adm:sch_end")
def cb_adm_sch_end(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    admin_state[call.from_user.id] = {"action": "sch_end"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     f"Текущий конец: {CONFIG['schedule']['work_end_hour']:02d}:00\n\n"
                     f"Пришлите новый час (1–24), например <b>21</b>. Или /cancel.",
                     parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, adm_step_value, "sch_end")


@bot.callback_query_handler(func=lambda c: c.data == "adm:sch_offs")
def cb_adm_sch_offs(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    bot.answer_callback_query(call.id)
    offs = CONFIG["schedule"]["days_off"]
    kb = types.InlineKeyboardMarkup(row_width=4)
    row = []
    for i, wd in enumerate(WEEKDAY_NAMES):
        mark = "✅" if i in offs else "▫️"
        row.append(types.InlineKeyboardButton(f"{mark} {wd}", callback_data=f"adm:tgl_off:{i}"))
    kb.add(*row)
    kb.add(types.InlineKeyboardButton("⬅️ К расписанию", callback_data="adm:schedule"))
    bot.edit_message_text("🚫 Отметьте выходные (тап переключает):",
                          chat_id=call.message.chat.id, message_id=call.message.message_id,
                          reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:tgl_off:"))
def cb_adm_tgl_off(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    wd = int(call.data.split(":")[2])
    offs = CONFIG["schedule"]["days_off"]
    if wd in offs: offs.remove(wd)
    else: offs.append(wd)
    CONFIG["schedule"]["days_off"] = sorted(set(offs))
    save_config()
    bot.answer_callback_query(call.id, f"{WEEKDAY_NAMES[wd]}: обновлено")
    kb = types.InlineKeyboardMarkup(row_width=4)
    row = []
    for i, wname in enumerate(WEEKDAY_NAMES):
        mark = "✅" if i in CONFIG["schedule"]["days_off"] else "▫️"
        row.append(types.InlineKeyboardButton(f"{mark} {wname}", callback_data=f"adm:tgl_off:{i}"))
    kb.add(*row)
    kb.add(types.InlineKeyboardButton("⬅️ К расписанию", callback_data="adm:schedule"))
    try:
        bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                      message_id=call.message.message_id, reply_markup=kb)
    except Exception:
        pass


# ---------- Инфо ----------

def info_kb():
    return types.InlineKeyboardMarkup(row_width=1).add(
        types.InlineKeyboardButton("✏️ Название", callback_data="adm:info_name"),
        types.InlineKeyboardButton("✏️ Адрес", callback_data="adm:info_addr"),
        types.InlineKeyboardButton("✏️ Телефон", callback_data="adm:info_phone"),
        types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="adm:root"),
    )


@bot.callback_query_handler(func=lambda c: c.data == "adm:info")
def cb_adm_info(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    s = CONFIG["salon"]
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"📞 <b>Информация о салоне</b>\n\n"
        f"Название: {s['name']}\nАдрес: {s['address']}\nТелефон: {s['phone']}",
        chat_id=call.message.chat.id, message_id=call.message.message_id,
        reply_markup=info_kb(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data in ("adm:info_name", "adm:info_addr", "adm:info_phone"))
def cb_adm_info_edit(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа"); return
    field = call.data.split("_")[1]
    key = {"name": "name", "addr": "address", "phone": "phone"}[field]
    admin_state[call.from_user.id] = {"action": f"info_{key}"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     f"Текущее значение: <b>{CONFIG['salon'][key]}</b>\n\n"
                     f"Пришлите новое (или /cancel).", parse_mode="HTML")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, adm_step_value, f"info_{key}")


# ---------- /cancel ----------

@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    admin_state.pop(message.from_user.id, None)
    user_state.pop(message.from_user.id, None)
    ab_state.pop(message.from_user.id, None)
    if is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Отменено.", reply_markup=admin_root_kb())
    else:
        bot.send_message(message.chat.id, "Отменено.", reply_markup=main_menu())


# ---------- Шаги ввода для админа ----------

def adm_step_value(message, expected):
    st = admin_state.get(message.from_user.id) or {}
    action = st.get("action")
    text = (message.text or "").strip()
    if action != expected:
        admin_state.pop(message.from_user.id, None); return

    if action == "name":
        idx = st["idx"]; sl = get_services_list()
        if idx >= len(sl): admin_state.pop(message.from_user.id, None); return
        old = sl[idx][0]; info = CONFIG["services"].pop(old)
        CONFIG["services"][text] = info; save_config()
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"✅ Название обновлено: <b>{text}</b>",
                         reply_markup=admin_root_kb(), parse_mode="HTML"); return

    if action == "price":
        idx = st["idx"]; sl = get_services_list()
        if idx >= len(sl): admin_state.pop(message.from_user.id, None); return
        CONFIG["services"][sl[idx][0]]["price"] = text; save_config()
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"✅ Цена обновлена: <b>{text}</b>",
                         reply_markup=admin_root_kb(), parse_mode="HTML"); return

    if action == "duration":
        if not text.isdigit() or int(text) <= 0 or int(text) > 1440:
            msg = bot.send_message(message.chat.id, "Введите число минут (1–1440):")
            bot.register_next_step_handler(msg, adm_step_value, "duration"); return
        idx = st["idx"]; sl = get_services_list()
        if idx >= len(sl): admin_state.pop(message.from_user.id, None); return
        CONFIG["services"][sl[idx][0]]["duration"] = int(text); save_config()
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"✅ Длительность: <b>{text} мин</b>",
                         reply_markup=admin_root_kb(), parse_mode="HTML"); return

    if action == "add_name":
        if len(text) < 2:
            msg = bot.send_message(message.chat.id, "Слишком коротко. Ещё раз:")
            bot.register_next_step_handler(msg, adm_step_value, "add_name"); return
        if text in CONFIG["services"]:
            msg = bot.send_message(message.chat.id, "Уже есть. Введите другое:")
            bot.register_next_step_handler(msg, adm_step_value, "add_name"); return
        admin_state[message.from_user.id] = {"action": "add_price", "name": text}
        msg = bot.send_message(message.chat.id, "Теперь цену (например: <b>1500 ₽</b>):",
                               parse_mode="HTML")
        bot.register_next_step_handler(msg, adm_step_value, "add_price"); return

    if action == "add_price":
        name = st["name"]
        admin_state[message.from_user.id] = {"action": "add_dur", "name": name, "price": text}
        msg = bot.send_message(message.chat.id, "И длительность в минутах (числом):")
        bot.register_next_step_handler(msg, adm_step_value, "add_dur"); return

    if action == "add_dur":
        if not text.isdigit() or int(text) <= 0 or int(text) > 1440:
            msg = bot.send_message(message.chat.id, "Введите число минут (1–1440):")
            bot.register_next_step_handler(msg, adm_step_value, "add_dur"); return
        name = st["name"]; price = st["price"]
        CONFIG["services"][name] = {"price": price, "duration": int(text)}; save_config()
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id,
                         f"✅ Добавлена услуга:\n<b>{name}</b> — {price} ({text} мин)",
                         reply_markup=admin_root_kb(), parse_mode="HTML"); return

    if action == "sch_start":
        if not text.isdigit() or not (0 <= int(text) <= 23):
            msg = bot.send_message(message.chat.id, "Введите час 0–23:")
            bot.register_next_step_handler(msg, adm_step_value, "sch_start"); return
        if int(text) >= CONFIG["schedule"]["work_end_hour"]:
            msg = bot.send_message(message.chat.id, "Должно быть раньше конца. Ещё раз:")
            bot.register_next_step_handler(msg, adm_step_value, "sch_start"); return
        CONFIG["schedule"]["work_start_hour"] = int(text); save_config()
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"✅ Начало: <b>{int(text):02d}:00</b>",
                         reply_markup=admin_root_kb(), parse_mode="HTML"); return

    if action == "sch_end":
        if not text.isdigit() or not (1 <= int(text) <= 24):
            msg = bot.send_message(message.chat.id, "Введите час 1–24:")
            bot.register_next_step_handler(msg, adm_step_value, "sch_end"); return
        if int(text) <= CONFIG["schedule"]["work_start_hour"]:
            msg = bot.send_message(message.chat.id, "Должно быть позже начала. Ещё раз:")
            bot.register_next_step_handler(msg, adm_step_value, "sch_end"); return
        CONFIG["schedule"]["work_end_hour"] = int(text); save_config()
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"✅ Конец: <b>{int(text):02d}:00</b>",
                         reply_markup=admin_root_kb(), parse_mode="HTML"); return

    if action in ("info_name", "info_address", "info_phone"):
        key = action.split("_", 1)[1]
        if len(text) < 2:
            msg = bot.send_message(message.chat.id, "Слишком коротко. Ещё раз:")
            bot.register_next_step_handler(msg, adm_step_value, action); return
        CONFIG["salon"][key] = text; save_config()
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"✅ Обновлено: <b>{text}</b>",
                         reply_markup=admin_root_kb(), parse_mode="HTML"); return


# ==========================================================
# ==================== НАПОМИНАНИЯ =========================
# ==========================================================

def check_reminders():
    now = now_msk()
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT id, user_id, service, date_time, reminded_day, reminded_hour
                   FROM appointments WHERE status IN ('new','confirmed') AND user_id != 0""")
    rows = cur.fetchall()
    for appt_id, user_id, service, dt_str, rd, rh in rows:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        delta = dt - now
        if delta <= timedelta(0): continue
        if not rh and delta <= timedelta(hours=1, minutes=1):
            try:
                bot.send_message(user_id,
                    f"⏰ Напоминание: через час — {service} в {dt.strftime('%H:%M')}.\n"
                    f"📍 {CONFIG['salon']['address']}")
                cur.execute("UPDATE appointments SET reminded_hour = 1 WHERE id = ?", (appt_id,))
                conn.commit()
            except Exception as e:
                print("rem hour:", e, flush=True)
        if not rd and delta <= timedelta(days=1, minutes=1):
            try:
                bot.send_message(user_id,
                    f"📅 Напоминание: завтра — {service} в {dt.strftime('%H:%M')}.\n"
                    f"📍 {CONFIG['salon']['address']}")
                cur.execute("UPDATE appointments SET reminded_day = 1 WHERE id = ?", (appt_id,))
                conn.commit()
            except Exception as e:
                print("rem day:", e, flush=True)
    conn.close()


def reminder_loop():
    while True:
        try:
            check_reminders()
        except Exception as e:
            print("Reminder loop error:", e, flush=True)
        time.sleep(60)


# ==========================================================
# ============== WEBHOOK-РЕЖИМ =============================
# ==========================================================

if WEBHOOK_URL:
    @app.route('/', methods=['GET'])
    def index(): return "Bot is running", 200

    @app.route('/' + BOT_TOKEN, methods=['POST'])
    def webhook():
        try:
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return '', 200
        except Exception as e:
            print("webhook error:", repr(e), flush=True); return '', 500

    def set_webhook():
        url = WEBHOOK_URL.rstrip('/') + '/' + BOT_TOKEN
        try:
            bot.remove_webhook(); time.sleep(1)
            result = bot.set_webhook(url=url)
            print(f"Вебхук: {url} -> {result}", flush=True)
        except Exception as e:
            print(f"Ошибка вебхука: {e}", flush=True)


# ==========================================================
# ================= КОМАНДЫ ================================
# ==========================================================

def set_bot_commands():
    common = [
        BotCommand("start", "🏠 Главное меню"),
        BotCommand("book",  "📝 Записаться"),
        BotCommand("my",    "📋 Мои записи"),
        BotCommand("help",  "📞 Контакты и адрес"),
    ]
    try:
        bot.set_my_commands(common)
        print("Общие команды установлены.", flush=True)
    except Exception as e:
        print(f"Общие команды: {e}", flush=True)
    admin_cmds = common + [BotCommand("admin", "🛠 Админ-панель")]
    for aid in ADMIN_IDS:
        if aid <= 0: continue
        try:
            bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
            print(f"Админ-команды для {aid}.", flush=True)
        except Exception as e:
            print(f"Админ-команды для {aid}: {e}", flush=True)


# ==========================================================
# ============== СТАРТ =====================================
# ==========================================================

if WEBHOOK_URL:
    print("Режим: WEBHOOK", flush=True)
    print(f"ADMIN_IDS: {ADMIN_IDS}", flush=True)
    set_webhook(); set_bot_commands()
    threading.Thread(target=reminder_loop, daemon=True).start()
else:
    print("Режим: POLLING", flush=True)
    print(f"ADMIN_IDS: {ADMIN_IDS}", flush=True)
    print(f"CONFIG_PATH: {CONFIG_PATH}", flush=True)
    try:
        bot.remove_webhook(); time.sleep(1)
        print("Вебхук удалён.", flush=True)
    except Exception as e:
        print(f"remove_webhook: {e}", flush=True)
    set_bot_commands()
    threading.Thread(target=reminder_loop, daemon=True).start()


if __name__ == "__main__":
    if WEBHOOK_URL:
        app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)), threaded=True)
    else:
        print("=" * 50, flush=True)
        print("  БОТ ЗАПУЩЕН", flush=True)
        print("=" * 50, flush=True)
        bot.infinity_polling(timeout=30, long_polling_timeout=25)
        