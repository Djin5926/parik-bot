import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import telebot
from telebot import types
from flask import Flask, request

# ==========================================================
# ===================== НАСТРОЙКИ ==========================
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")   # https://xxx.up.railway.app/
DB_PATH = os.getenv("DB_PATH", "barber.db")

# Список админов: несколько ID через запятую (без пробелов)
_raw_admins = os.getenv("ADMIN_ID", "0")
ADMIN_IDS = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0

if not BOT_TOKEN:
    raise ValueError("Укажите BOT_TOKEN в переменных окружения")

# --- Информация о салоне ---
SALON_INFO = {
    "name": "Парикмахерская «У Катерины»",
    "address": "ул. Чехова, д.79 корпус 1",
    "phone": "+7 985 214-57-94",
    "hours": "10:00–20:00, ежедневно",
}

# --- Услуги: название -> цена (строка) и длительность (мин) ---
SERVICES = {
    "Стрижка женская":            {"price": "1800–2000 ₽",   "duration": 60},
    "Стрижка мужская":            {"price": "1200–1300 ₽",   "duration": 45},
    "Окрашивание в 1 тон":        {"price": "4800 ₽",        "duration": 180},
    "Сложное окрашивание":        {"price": "8000–10000 ₽",  "duration": 240},
    "Мелирование + Тонирование":  {"price": "12000–15000 ₽", "duration": 240},
    "Уход за волосами":           {"price": "2500 ₽",        "duration": 90},
    "Укладка":                    {"price": "1200–2000 ₽",   "duration": 90},
    "Прически вечерние":          {"price": "2000–2500 ₽",   "duration": 90},
}

# --- Рабочее расписание ---
WORK_START_HOUR = 10
WORK_END_HOUR = 20
SLOT_STEP_MINUTES = 60
DAYS_AHEAD = 14

# ==========================================================
# ====================== БОТ И БД ==========================
# ==========================================================

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

STATUS_LABELS = {
    "new":       "🆕 Новая",
    "confirmed": "✅ Подтверждена",
    "done":      "✔️ Выполнена",
    "cancelled": "❌ Отменена",
}

user_state = {}
SERVICES_LIST = list(SERVICES.items())


def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            service TEXT,
            price TEXT,
            date_time TEXT,
            status TEXT DEFAULT 'new',
            reminded_day INTEGER DEFAULT 0,
            reminded_hour INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    for ddl in [
        "ALTER TABLE appointments ADD COLUMN price TEXT DEFAULT ''",
        "ALTER TABLE appointments ADD COLUMN reminded_day INTEGER DEFAULT 0",
        "ALTER TABLE appointments ADD COLUMN reminded_hour INTEGER DEFAULT 0",
    ]:
        try:
            cur.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


init_db()


def is_slot_free(date_time_str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM appointments "
        "WHERE date_time = ? AND status != 'cancelled'",
        (date_time_str,),
    )
    count = cur.fetchone()[0]
    conn.close()
    return count == 0


def save_appointment(user_id, username, name, phone, service, price, date_time):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO appointments
        (user_id, username, name, phone, service, price, date_time, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)
    """, (user_id, username, name, phone, service, price, date_time, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def notify_admins(text):
    """Отправить сообщение всем админам."""
    for admin in ADMIN_IDS:
        try:
            bot.send_message(admin, text)
        except Exception as e:
            print(f"[NOTIFY] Не удалось уведомить админа {admin}: {e}", flush=True)


# ==========================================================
# ====================== КЛАВИАТУРЫ ========================
# ==========================================================

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Услуги", "Записаться")
    kb.add("Мои записи", "Контакты")
    return kb


def services_inline():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (name, info) in enumerate(SERVICES_LIST):
        kb.add(types.InlineKeyboardButton(
            text=f"{name} — {info['price']}",
            callback_data=f"svc:{i}",
        ))
    kb.add(types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow"))
    return kb


def calendar_inline():
    kb = types.InlineKeyboardMarkup(row_width=3)
    today = datetime.now().date()
    buttons = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        buttons.append(types.InlineKeyboardButton(
            text=d.strftime("%d.%m"), callback_data=f"date:{d.isoformat()}"
        ))
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow"))
    return kb


def time_slots_inline(date_str):
    kb = types.InlineKeyboardMarkup(row_width=3)
    start = datetime.strptime(f"{date_str} {WORK_START_HOUR:02d}:00", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{date_str} {WORK_END_HOUR:02d}:00", "%Y-%m-%d %H:%M")
    t = start
    buttons = []
    while t < end:
        slot_human = t.strftime("%Y-%m-%d %H:%M")
        slot_cb = t.strftime("%Y-%m-%d|%H:%M")
        label = t.strftime("%H:%M")
        if is_slot_free(slot_human):
            buttons.append(types.InlineKeyboardButton(
                text=label, callback_data=f"time:{slot_cb}"
            ))
        else:
            buttons.append(types.InlineKeyboardButton(
                text=f"❌{label}", callback_data="busy"
            ))
        t += timedelta(minutes=SLOT_STEP_MINUTES)
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_calendar"))
    kb.add(types.InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_flow"))
    return kb


# ==========================================================
# ================== БАЗОВЫЕ ОБРАБОТЧИКИ ===================
# ==========================================================

@bot.message_handler(commands=["start"])
def start(message):
    print(f"[HANDLER] /start от user_id={message.from_user.id}", flush=True)
    user_state.pop(message.from_user.id, None)
    bot.send_message(
        message.chat.id,
        f"Здравствуйте! Это бот {SALON_INFO['name']}.\n"
        f"Я помогу вам записаться. Выберите действие:",
        reply_markup=main_menu(),
    )


@bot.message_handler(func=lambda m: m.text == "Услуги")
def show_services(message):
    lines = ["Наши услуги:\n"]
    for name, info in SERVICES.items():
        lines.append(f"• {name} — {info['price']} (~{info['duration']} мин)")
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(func=lambda m: m.text == "Контакты")
def contacts(message):
    bot.send_message(
        message.chat.id,
        f"📍 {SALON_INFO['address']}\n"
        f"📞 {SALON_INFO['phone']}\n"
        f"🕒 {SALON_INFO['hours']}",
    )


# ==========================================================
# ====================== ЗАПИСЬ ============================
# ==========================================================

@bot.message_handler(func=lambda m: m.text == "Записаться")
def book_start(message):
    user_state[message.from_user.id] = {}
    bot.send_message(message.chat.id, "Выберите услугу:", reply_markup=services_inline())


@bot.callback_query_handler(func=lambda c: c.data.startswith("svc:"))
def cb_service(call):
    idx = int(call.data.split(":", 1)[1])
    name, info = SERVICES_LIST[idx]
    state = user_state.setdefault(call.from_user.id, {})
    state["service"] = name
    state["price"] = info["price"]
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"Услуга: {name} ({info['price']})\n\nВыберите дату:",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=calendar_inline(),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("date:"))
def cb_date(call):
    date_str = call.data.split(":", 1)[1]
    state = user_state.setdefault(call.from_user.id, {})
    if "service" not in state:
        bot.answer_callback_query(call.id, "Сначала выберите услугу")
        return
    state["date"] = date_str
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"Услуга: {state['service']}\nДата: {date_str}\n\nВыберите время:",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=time_slots_inline(date_str),
    )


@bot.callback_query_handler(func=lambda c: c.data == "back_to_calendar")
def cb_back(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "Выберите дату:",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=calendar_inline(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "busy")
def cb_busy(call):
    bot.answer_callback_query(call.id, "Это время уже занято, выберите другое")


@bot.callback_query_handler(func=lambda c: c.data == "cancel_flow")
def cb_cancel_flow(call):
    user_state.pop(call.from_user.id, None)
    bot.answer_callback_query(call.id, "Отменено")
    bot.send_message(call.message.chat.id, "Запись отменена.", reply_markup=main_menu())


@bot.callback_query_handler(func=lambda c: c.data.startswith("time:"))
def cb_time(call):
    slot = call.data.split(":", 1)[1].replace("|", " ")
    if not is_slot_free(slot):
        bot.answer_callback_query(call.id, "Это время уже занято")
        return
    state = user_state.setdefault(call.from_user.id, {})
    if "service" not in state:
        bot.answer_callback_query(call.id, "Сначала выберите услугу")
        return
    state["datetime"] = slot
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"Услуга: {state['service']}\n"
        f"Время: {slot}\n\n"
        f"Введите ваше имя:",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, step_name)


def step_name(message):
    name = message.text.strip()
    if len(name) < 2:
        msg = bot.send_message(message.chat.id, "Имя слишком короткое, введите ещё раз:")
        bot.register_next_step_handler(msg, step_name)
        return
    user_state.setdefault(message.from_user.id, {})["name"] = name
    msg = bot.send_message(message.chat.id, "Введите номер телефона для связи:")
    bot.register_next_step_handler(msg, step_phone)


def step_phone(message):
    phone = message.text.strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        msg = bot.send_message(message.chat.id, "Похоже, номер некорректный. Введите ещё раз:")
        bot.register_next_step_handler(msg, step_phone)
        return

    state = user_state.get(message.from_user.id, {})
    if not all(k in state for k in ("service", "price", "datetime", "name")):
        bot.send_message(
            message.chat.id,
            "Что-то пошло не так. Начните заново.",
            reply_markup=main_menu(),
        )
        user_state.pop(message.from_user.id, None)
        return

    if not is_slot_free(state["datetime"]):
        bot.send_message(
            message.chat.id,
            "Увы, это время только что заняли. Попробуйте выбрать другое.",
            reply_markup=main_menu(),
        )
        user_state.pop(message.from_user.id, None)
        return

    save_appointment(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        name=state["name"],
        phone=phone,
        service=state["service"],
        price=state["price"],
        date_time=state["datetime"],
    )

    bot.send_message(
        message.chat.id,
        f"✅ Вы записаны!\n\n"
        f"Услуга: {state['service']}\n"
        f"Стоимость: {state['price']}\n"
        f"Дата и время: {state['datetime']}\n\n"
        f"Мы свяжемся с вами для подтверждения.\n"
        f"Адрес: {SALON_INFO['address']}",
        reply_markup=main_menu(),
    )

    notify_admins(
        f"🆕 Новая запись!\n\n"
        f"Услуга: {state['service']} ({state['price']})\n"
        f"Имя: {state['name']}\n"
        f"Телефон: {phone}\n"
        f"Дата и время: {state['datetime']}\n"
        f"Клиент: @{message.from_user.username or '—'} (ID: {message.from_user.id})"
    )

    user_state.pop(message.from_user.id, None)


# ==========================================================
# ================== МОИ ЗАПИСИ / ОТМЕНА ===================
# ==========================================================

@bot.message_handler(func=lambda m: m.text == "Мои записи")
def my_appointments(message):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, service, date_time, status, price
        FROM appointments
        WHERE user_id = ? AND status != 'cancelled'
        ORDER BY date_time ASC
    """, (message.from_user.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        bot.send_message(message.chat.id, "У вас пока нет активных записей.")
        return

    for appt_id, service, dt, status, price in rows:
        text = (
            f"Услуга: {service} ({price})\n"
            f"Дата: {dt}\n"
            f"Статус: {STATUS_LABELS.get(status, status)}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Отменить запись", callback_data=f"cancel:{appt_id}"))
        bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel_appointment(call):
    appt_id = int(call.data.split(":", 1)[1])
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, service, date_time, status FROM appointments WHERE id = ?", (appt_id,))
    row = cur.fetchone()
    if not row or row[0] != call.from_user.id:
        conn.close()
        bot.answer_callback_query(call.id, "Запись не найдена")
        return
    if row[3] == "cancelled":
        conn.close()
        bot.answer_callback_query(call.id, "Уже отменена")
        return
    cur.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))
    conn.commit()
    conn.close()
    bot.answer_callback_query(call.id, "Отменено")
    bot.edit_message_text(
        f"❌ Запись отменена: {row[1]} на {row[2]}",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )
    notify_admins(f"❌ Клиент отменил запись #{appt_id}: {row[1]} на {row[2]}")


# ==========================================================
# ======================= АДМИН ============================
# ==========================================================

@bot.message_handler(commands=["admin"])
def admin(message):
    print(f"[HANDLER] /admin от user_id={message.from_user.id}, ADMIN_IDS={ADMIN_IDS}", flush=True)
    if message.from_user.id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "У вас нет доступа.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, phone, service, date_time, status
        FROM appointments
        WHERE status IN ('new', 'confirmed')
        ORDER BY date_time ASC
        LIMIT 20
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        bot.send_message(message.chat.id, "Активных записей нет.")
        return

    for appt_id, name, phone, service, dt, status in rows:
        text = (
            f"#{appt_id}\n"
            f"Клиент: {name}\n"
            f"Телефон: {phone}\n"
            f"Услуга: {service}\n"
            f"Время: {dt}\n"
            f"Статус: {STATUS_LABELS.get(status, status)}"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        if status == "new":
            kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"st:confirmed:{appt_id}"))
        kb.add(types.InlineKeyboardButton("✔️ Выполнено", callback_data=f"st:done:{appt_id}"))
        kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"st:cancelled:{appt_id}"))
        bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("st:"))
def cb_status(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Нет доступа")
        return
    _, new_status, appt_id_str = call.data.split(":")
    appt_id = int(appt_id_str)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, service, date_time FROM appointments WHERE id = ?", (appt_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        bot.answer_callback_query(call.id, "Не найдено")
        return
    cur.execute("UPDATE appointments SET status = ? WHERE id = ?", (new_status, appt_id))
    conn.commit()
    conn.close()

    bot.answer_callback_query(call.id, f"Статус: {STATUS_LABELS.get(new_status, new_status)}")
    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
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
        print("Не удалось уведомить клиента:", e, flush=True)


# ==========================================================
# ==================== НАПОМИНАНИЯ =========================
# ==========================================================

def check_reminders():
    now = datetime.now()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, user_id, service, date_time, reminded_day, reminded_hour
        FROM appointments
        WHERE status IN ('new', 'confirmed')
    """)
    rows = cur.fetchall()
    for appt_id, user_id, service, dt_str, rd, rh in rows:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        delta = dt - now
        if delta <= timedelta(0):
            continue
        if not rh and delta <= timedelta(hours=1, minutes=1):
            try:
                bot.send_message(
                    user_id,
                    f"⏰ Напоминание: через час — {service} в {dt.strftime('%H:%M')}.",
                )
                cur.execute("UPDATE appointments SET reminded_hour = 1 WHERE id = ?", (appt_id,))
                conn.commit()
            except Exception as e:
                print("reminder hour:", e, flush=True)
        if not rd and delta <= timedelta(days=1, minutes=1):
            try:
                bot.send_message(
                    user_id,
                    f"📅 Напоминание: завтра — {service} в {dt.strftime('%H:%M')}.",
                )
                cur.execute("UPDATE appointments SET reminded_day = 1 WHERE id = ?", (appt_id,))
                conn.commit()
            except Exception as e:
                print("reminder day:", e, flush=True)
    conn.close()


def reminder_loop():
    while True:
        try:
            check_reminders()
        except Exception as e:
            print("Reminder loop error:", e, flush=True)
        time.sleep(60)


# ==========================================================
# ======================== WEBHOOK =========================
# ==========================================================

@app.route('/', methods=['GET'])
def index():
    return "Bot is running", 200


@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    print(">>> ПОЛУЧЕН POST ОТ TELEGRAM <<<", flush=True)
    try:
        json_string = request.get_data().decode('utf-8')
        print(">>> BODY:", json_string[:500], flush=True)
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        print(">>> ОБРАБОТАНО OK", flush=True)
        return '', 200
    except Exception as e:
        print(">>> ОШИБКА в webhook:", repr(e), flush=True)
        return '', 500


def set_webhook():
    if not WEBHOOK_URL:
        print("WEBHOOK_URL не задан — вебхук не установлен!", flush=True)
        return
    url = WEBHOOK_URL.rstrip('/') + '/' + BOT_TOKEN
    try:
        bot.remove_webhook()
        time.sleep(1)
        result = bot.set_webhook(url=url)
        print(f"Вебхук установлен: {url} -> {result}", flush=True)
    except Exception as e:
        print(f"Ошибка установки вебхука: {e}", flush=True)


# ==========================================================
# ============== ИНИЦИАЛИЗАЦИЯ ПРИ СТАРТЕ ==================
# ==========================================================
# Важно: этот код на уровне модуля, чтобы выполнялся и под gunicorn,
# а не только при запуске `python bot.py`.

print("Бот запущен в режиме webhook...", flush=True)
print(f"ADMIN_IDS: {ADMIN_IDS}", flush=True)
print(f"WEBHOOK_URL: {WEBHOOK_URL}", flush=True)
print(f"DB_PATH: {DB_PATH}", flush=True)
set_webhook()
threading.Thread(target=reminder_loop, daemon=True).start()


# ==========================================================
# ============== ЗАПУСК ДЛЯ ЛОКАЛЬНОЙ ОТЛАДКИ ==============
# ==========================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
