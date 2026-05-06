"""
STK Telegram Bot — упрощённая облачная версия.

Всё хранится только в Telegram + SQLite.
Никаких напоминаний, Google Calendar, Notion и прочих внешних переносов.
Один утренний дайджест в 10:00 по Алматы:
  карточка дня → задачи STK → задачи CLOQ → привычки → погода.
"""
import os
import re
import asyncio
import sqlite3
import logging
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

load_dotenv()

# ─────────── ENV ───────────
TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
OWNER_ID = int(os.environ.get("OWNER_CHAT_ID", "0").strip()) or None
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Almaty").strip()
DB_PATH = os.environ.get("DB_PATH", "data.db").strip()
ALMATY_TZ = ZoneInfo("Asia/Almaty")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stk-bot")

# ─────────── DB ───────────
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode = WAL")
    return c

def init_db():
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL DEFAULT 'stk',
            priority TEXT NOT NULL DEFAULT 'normal',
            text TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            done_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'business',
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            UNIQUE(name, date)
        );

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    c.commit()
    c.close()

def get_state(key, default=None):
    c = db(); r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone(); c.close()
    return r["value"] if r else default

def set_state(key, value):
    c = db()
    c.execute(
        "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    c.commit(); c.close()

# ─────────── Карточка дня (бизнес-английский) ───────────
BUSINESS_TERMS = [
    {"term": "Cash Flow", "tr": "/kæʃ floʊ/", "ru": "Денежный поток",
     "ex": "Positive cash flow = больше пришло чем потратили.",
     "tip": "Cash flow ≠ profit! Можно быть прибыльным, но без денег."},
    {"term": "Unit Economics", "tr": "/ˈjuːnɪt ˌekəˈnɒmɪks/", "ru": "Юнит-экономика",
     "ex": "Цена − все расходы = прибыль с одной продажи.",
     "tip": "Если отрицательная — каждая продажа делает беднее."},
    {"term": "CAC", "tr": "/siː eɪ siː/", "ru": "Стоимость привлечения клиента",
     "ex": "$1000 на FB → 10 клиентов = CAC $100.",
     "tip": "Здоровый CAC < 1/3 от LTV."},
    {"term": "LTV", "tr": "/el tiː viː/", "ru": "Пожизненная ценность клиента",
     "ex": "Клиент покупает 3 раза по 42 580 = LTV 127 740 тг.",
     "tip": "LTV / CAC = 3+ — здоровый бизнес."},
    {"term": "Burn Rate", "tr": "/bɜːrn reɪt/", "ru": "Скорость сжигания денег",
     "ex": "Burn rate $50K/месяц = тратите 50 тысяч каждый месяц.",
     "tip": "Burn rate × 12 = сколько нужно на год."},
    {"term": "Runway", "tr": "/ˈrʌnweɪ/", "ru": "Запас денег",
     "ex": "$300K / $50K burn = 6 месяцев runway.",
     "tip": "Меньше 6 месяцев — пора искать инвестиции или резать расходы."},
    {"term": "Gross Margin", "tr": "/ɡroʊs ˈmɑːrdʒɪn/", "ru": "Валовая маржа",
     "ex": "Цена − себестоимость = 88% gross margin.",
     "tip": "> 70% — отличный товарный бизнес."},
    {"term": "ROI", "tr": "/ɑːr oʊ aɪ/", "ru": "Возврат инвестиций",
     "ex": "Вложил $1, получил $3 → ROI 200%.",
     "tip": "ROI < 100% — инвестиция не окупается."},
    {"term": "Pipeline", "tr": "/ˈpaɪplaɪn/", "ru": "Воронка сделок",
     "ex": "В пайплайне 50 лидов на разных стадиях.",
     "tip": "Healthy pipeline = 3-4× от месячной цели."},
    {"term": "Conversion Rate", "tr": "/kənˈvɜːrʃən reɪt/", "ru": "Конверсия",
     "ex": "1000 посетителей → 30 заявок = 3% CR.",
     "tip": "Хорошая CR для лендинга = 2-5%."},
    {"term": "Churn Rate", "tr": "/tʃɜːrn reɪt/", "ru": "Отток клиентов",
     "ex": "Из 100 клиентов 5 ушли = 5% monthly churn.",
     "tip": "Снижай churn в первую очередь."},
    {"term": "ARPU", "tr": "/ˈɑːrpuː/", "ru": "Средний доход на клиента",
     "ex": "17.5М / 412 клиентов = ARPU 42 500 тг.",
     "tip": "Растёт ARPU — растёт бизнес без новых клиентов."},
    {"term": "MRR / ARR", "tr": "/em ɑːr ɑːr/", "ru": "Месячная/годовая регулярная выручка",
     "ex": "100 клиентов × 5000/мес = MRR 500K.",
     "tip": "Инвесторы оценивают компании по ARR."},
    {"term": "Pivot", "tr": "/ˈpɪvət/", "ru": "Резкая смена стратегии",
     "ex": "Slack начинался как игра → пивот в мессенджер.",
     "tip": "Pivot — не провал. Failure to pivot — провал."},
    {"term": "Bootstrap", "tr": "/ˈbuːtstræp/", "ru": "Развиваться без внешних денег",
     "ex": "Mailchimp bootstrap до $700M.",
     "tip": "Подходит когда unit economics уже работает."},
    {"term": "Scale", "tr": "/skeɪl/", "ru": "Масштабирование",
     "ex": "Программу пишут 1 раз, продают 1000 раз.",
     "tip": "Сначала PMF, потом scale."},
    {"term": "PMF (Product-Market Fit)", "tr": "/piː em ef/", "ru": "Соответствие продукт-рынок",
     "ex": "40%+ клиентов скажут «расстроюсь без вас» — у тебя PMF.",
     "tip": "До PMF — итерируй. После — масштабируй."},
    {"term": "Funnel", "tr": "/ˈfʌnəl/", "ru": "Воронка",
     "ex": "1000 видели → 100 кликнули → 30 заявок → 10 оплат.",
     "tip": "Найди узкое место и расшивай его."},
    {"term": "B2B / B2C / D2C", "tr": "/biː tuː biː/", "ru": "Бизнес-модели",
     "ex": "STK = D2C — продаёте напрямую через рекламу.",
     "tip": "D2C = выше маржа, нужны свои каналы."},
    {"term": "Cohort Analysis", "tr": "/ˈkoʊhɔːrt/", "ru": "Анализ когорт",
     "ex": "Январь — 30% повторных. Февраль — 40%. Тренд растёт.",
     "tip": "Без когорт — ты гадаешь."},
    {"term": "NPS", "tr": "/en piː es/", "ru": "Индекс лояльности",
     "ex": "70% промоутеров − 10% детракторов = NPS 60.",
     "tip": "NPS > 50 — отлично. < 0 — проблема."},
    {"term": "Stakeholder", "tr": "/ˈsteɪkhoʊldər/", "ru": "Заинтересованная сторона",
     "ex": "Перед изменениями — обсуди со stakeholders.",
     "tip": "Управляй ими как картой."},
    {"term": "Bandwidth", "tr": "/ˈbændwɪdθ/", "ru": "Свободное время / ресурсы",
     "ex": "I don't have bandwidth = у меня нет времени.",
     "tip": "Делегируй пока не достиг bandwidth."},
    {"term": "Headcount", "tr": "/ˈhedkaʊnt/", "ru": "Количество сотрудников",
     "ex": "Need to grow headcount by 20% next quarter.",
     "tip": "Headcount × ЗП = большая часть burn rate."},
    {"term": "AOV (Average Order Value)", "tr": "/eɪ oʊ viː/", "ru": "Средний чек",
     "ex": "Выручка / число заказов = AOV.",
     "tip": "Подними AOV upsell'ом — это легче чем привлечь нового."},
]

# ─────────── Погода ───────────
def get_weather():
    try:
        url = f"https://wttr.in/{urllib.parse.quote(WEATHER_CITY)}?format=%t|%C|%w&lang=ru"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            parts = r.read().decode().strip().split("|")
        temp, cond, wind = parts[0].strip(), parts[1].strip(), parts[2].strip()
        m = re.search(r"[+-]?\d+", temp); t = int(m.group()) if m else 15
        clothes = (
            "🧥 Куртка, шапка, перчатки" if t <= 0 else
            "🧤 Куртка, шарф" if t <= 10 else
            "🧥 Лёгкая куртка" if t <= 18 else
            "👕 Футболка" if t <= 25 else "🩳 Шорты"
        )
        return f"🌡 {temp} · {cond} · 💨 {wind}\n{clothes}"
    except Exception as e:
        log.warning("weather error: %s", e)
        return "🌡 Погода недоступна"

# ─────────── Классификация ───────────
# Префиксы — для редких случаев когда нужно НЕ-STK.
# Без префикса → дефолт STK задача.
CLOQ_PREFIX = re.compile(r"^(cloq|клок)[:\s]+(.+)", re.IGNORECASE)
MARKETING_PREFIX = re.compile(r"^(маркетинг|реклама|креатив)[:\s]+(.+)", re.IGNORECASE)
PERSONAL_PREFIX = re.compile(r"^(личное|личн)[:\s]+(.+)", re.IGNORECASE)
IDEA_PREFIX = re.compile(r"^идея[:\s]+(.+)", re.IGNORECASE)
TASK_PREFIX = re.compile(r"^(задача|таск|todo|сделать|важно|срочно)[:\s]+(.+)", re.IGNORECASE)
HABIT_WORDS = {
    "тренировка": ["тренировк", "зал ", "спорт", "gym", "жаттығу"],
    "чтение": ["чтени", "книг", "read", "оқу"],
    "вода": ["вода 2", "вода ✅", "water", "су 2"],
    "подъём": ["подъём", "ранний подъ", "ерте тұрдым"],
}

def classify(text: str):
    """Возвращает dict с типом и данными."""
    t = text.strip()
    low = t.lower()

    # Привычки (✅)
    if "✅" in t or low.endswith(" ок") or low.endswith(" done"):
        clean = t.replace("✅", "").strip().rstrip(" ок").rstrip(" done")
        for habit, kws in HABIT_WORDS.items():
            if any(w in clean.lower() for w in kws):
                return {"type": "habit", "name": habit}

    # CLOQ
    m = CLOQ_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "cloq", "text": m.group(2).strip()}

    # Маркетинг → идея
    m = MARKETING_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "marketing", "text": m.group(2).strip()}

    # Личное → идея
    m = PERSONAL_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "personal", "text": m.group(2).strip()}

    # Идея бизнес
    m = IDEA_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "business", "text": m.group(1).strip()}

    # Любые «задача:/важно:/срочно:/таск:» → STK задача
    m = TASK_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "text": m.group(2).strip()}

    # Команды
    if low in ("задачи", "tasks", "статус", "план", "дайджест"):
        return {"type": "cmd_digest"}
    if low in ("привычки", "habits", "трекер"):
        return {"type": "cmd_habits"}
    if low in ("погода", "weather"):
        return {"type": "cmd_weather"}
    if low.startswith(("?", "вопрос:")) and ANTHROPIC_KEY:
        return {"type": "ask", "text": re.sub(r"^[?]\s*|вопрос:\s*", "", t, flags=re.IGNORECASE)}

    # ✅ Дефолт — STK задача
    return {"type": "task", "project": "stk", "text": t}

# ─────────── Anthropic API (опционально, для вопросов) ───────────
def ask_claude(question: str) -> str:
    if not ANTHROPIC_KEY:
        return "AI выключен (ANTHROPIC_API_KEY не задан)"
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            system=("Ты помощник Абылая (STK парфюмерия, Алматы). "
                    "Цены: 30мл=42 580 тг, 50мл=63 700 тг, Kaspi=3 548 тг/мес. "
                    "137 менеджеров, 8 отделов. Розыгрыш Changan X5 15 мая. "
                    "Отвечай кратко (2-4 предложения), по делу."),
            messages=[{"role": "user", "content": question}],
        )
        return msg.content[0].text
    except Exception as e:
        log.error("claude error: %s", e)
        return f"⚠️ Ошибка AI: {e}"

# ─────────── Бизнес-логика ───────────
def add_task(project, text):
    c = db()
    cur = c.execute(
        "INSERT INTO tasks(project,priority,text,created_at) VALUES(?,?,?,?)",
        (project, "normal", text, int(datetime.now().timestamp())),
    )
    c.commit(); tid = cur.lastrowid; c.close()
    return tid

def add_idea(category, text):
    c = db()
    cur = c.execute(
        "INSERT INTO ideas(category,text,created_at) VALUES(?,?,?)",
        (category, text, int(datetime.now().timestamp())),
    )
    c.commit(); iid = cur.lastrowid; c.close()
    return iid

def mark_habit(name):
    today = datetime.now(ALMATY_TZ).strftime("%Y-%m-%d")
    c = db()
    try:
        c.execute("INSERT INTO habits(name,date) VALUES(?,?)", (name, today))
        c.commit()
        cur = c.execute("SELECT date FROM habits WHERE name=? ORDER BY date DESC", (name,))
        dates = [r["date"] for r in cur.fetchall()]
        from datetime import timedelta
        streak = 0
        d = datetime.now(ALMATY_TZ).date()
        for ds in dates:
            if datetime.strptime(ds, "%Y-%m-%d").date() == d:
                streak += 1; d -= timedelta(days=1)
            else: break
        c.close()
        return streak
    except sqlite3.IntegrityError:
        c.close()
        return 0

def complete_task(tid):
    c = db()
    c.execute("UPDATE tasks SET done=1, done_at=? WHERE id=? AND done=0",
              (int(datetime.now().timestamp()), tid))
    c.commit(); changed = c.total_changes; c.close()
    return changed > 0

def get_open_tasks(project=None, limit=20):
    c = db()
    q = "SELECT * FROM tasks WHERE done=0"
    args = []
    if project: q += " AND project=?"; args.append(project)
    q += " ORDER BY id ASC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall(); c.close()
    return [dict(r) for r in rows]

def get_habits_today():
    today = datetime.now(ALMATY_TZ).strftime("%Y-%m-%d")
    out = {}
    c = db()
    from datetime import timedelta
    for habit in ["тренировка", "чтение", "вода", "подъём"]:
        done = c.execute("SELECT 1 FROM habits WHERE name=? AND date=?",
                         (habit, today)).fetchone() is not None
        rows = c.execute("SELECT date FROM habits WHERE name=? ORDER BY date DESC", (habit,)).fetchall()
        streak = 0
        d = datetime.now(ALMATY_TZ).date()
        for r in rows:
            if datetime.strptime(r["date"], "%Y-%m-%d").date() == d:
                streak += 1; d -= timedelta(days=1)
            else: break
        out[habit] = (done, streak)
    c.close()
    return out

# ─────────── Дайджест ───────────
def build_digest():
    """Порядок: карточка дня → задачи STK → задачи CLOQ → привычки → погода."""
    now = datetime.now(ALMATY_TZ)
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    msg = f"☀️ *Доброе утро, Абылай!*\n"
    msg += f"📅 {now.strftime('%d.%m.%Y')} · {days[now.weekday()]}\n"

    tasks_for_buttons = []

    # 1. Карточка дня
    day_idx = now.timetuple().tm_yday % len(BUSINESS_TERMS)
    term = BUSINESS_TERMS[day_idx]
    msg += "\n━━━━━ 🇬🇧 *КАРТОЧКА ДНЯ* ━━━━━\n"
    msg += f"\n*{term['term']}* {term['tr']}\n"
    msg += f"📖 _{term['ru']}_\n\n"
    msg += f"💬 {term['ex']}\n\n"
    msg += f"💡 {term['tip']}\n"

    # 2. Задачи STK
    stk = get_open_tasks(project="stk", limit=15)
    msg += "\n━━━━━ 🌹 *STK* ━━━━━\n"
    if stk:
        for t in stk:
            msg += f"   ☐ {t['text'][:60]}\n"
            tasks_for_buttons.append((t["id"], t["text"]))
    else:
        msg += "   _нет открытых задач_\n"

    # 3. Задачи CLOQ (только если есть)
    cloq = get_open_tasks(project="cloq", limit=10)
    if cloq:
        msg += "\n━━━━━ ⌚ *CLOQ* ━━━━━\n"
        for t in cloq:
            msg += f"   ☐ {t['text'][:60]}\n"
            tasks_for_buttons.append((t["id"], t["text"]))

    # 4. Привычки
    msg += "\n━━━━━ 🔥 *ПРИВЫЧКИ* ━━━━━\n"
    icons = {"тренировка": "🏋", "чтение": "📖", "вода": "💧", "подъём": "🌅"}
    for name, (done, streak) in get_habits_today().items():
        fire = "🔥" * min(streak, 5) if streak >= 2 else ""
        msg += f"   {icons[name]} {'✅' if done else '☐'} {name} · {streak} дн {fire}\n"

    # 5. Погода
    msg += "\n━━━━━ 🌡 *ПОГОДА* ━━━━━\n"
    msg += get_weather()

    # Кнопки выполнения задач
    keyboard = []
    for tid, text in tasks_for_buttons[:10]:
        keyboard.append([InlineKeyboardButton(f"✅ {text[:35]}", callback_data=f"done:{tid}")])
    kb = InlineKeyboardMarkup(keyboard) if keyboard else None
    return msg, kb

# ─────────── Handlers ───────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *STK Bot — упрощённая версия*\n\n"
        "Просто пиши задачи — *по умолчанию это STK*.\n"
        "Никаких напоминаний, всё в Telegram.\n\n"
        "*Префиксы (опционально):*\n"
        "• `cloq: купить часы` → ⌚ CLOQ\n"
        "• `маркетинг: новый креатив` → 🎯 идея\n"
        "• `личное: записаться к врачу` → 🏃 идея\n"
        "• `идея: запустить b2b` → 💡 идея\n\n"
        "*Привычки:*\n"
        "• `тренировка ✅` → 🔥 трекер серии\n\n"
        "*Команды:*\n"
        "• `задачи` → 📋 показать дайджест\n"
        "• `погода` → 🌡\n"
        "• `?вопрос` → 💬 AI-ответ\n\n"
        "⏰ _Каждый день в 10:00 по Алматы — карточка дня + задачи + погода._",
        parse_mode="Markdown",
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID and update.effective_user.id != OWNER_ID:
        return
    text = update.message.text.strip()
    if not text:
        return
    set_state("last_chat_id", update.effective_chat.id)
    log.info("MSG: %s", text)
    res = classify(text)
    log.info("CLS: %s", res["type"])
    t = res["type"]

    if t == "cmd_digest":
        msg, kb = build_digest()
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

    elif t == "cmd_habits":
        msg = "🔥 *Привычки сегодня:*\n\n"
        icons = {"тренировка": "🏋", "чтение": "📖", "вода": "💧", "подъём": "🌅"}
        for name, (done, streak) in get_habits_today().items():
            fire = "🔥" * min(streak, 5) if streak >= 2 else ""
            msg += f"{icons[name]} {'✅' if done else '☐'} {name} · {streak} дн {fire}\n"
        await update.message.reply_text(msg, parse_mode="Markdown")

    elif t == "cmd_weather":
        await update.message.reply_text(get_weather())

    elif t == "habit":
        streak = mark_habit(res["name"])
        if streak == 0:
            await update.message.reply_text(f"✅ {res['name']} — уже отмечено сегодня!")
        else:
            fire = "🔥" * min(streak, 5)
            extra = ""
            if streak >= 7: extra = "\n\n🎉 *НЕДЕЛЯ ПОДРЯД!*"
            elif streak >= 3: extra = "\n\n💪 Так держать!"
            await update.message.reply_text(
                f"✅ *{res['name']}* отмечено!\n\n📊 Серия: *{streak} дней* {fire}{extra}",
                parse_mode="Markdown",
            )

    elif t == "task":
        tid = add_task(res["project"], res["text"])
        proj_label = "🌹 *STK*" if res["project"] == "stk" else "⌚ *CLOQ*"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{tid}")]])
        await update.message.reply_text(
            f"{proj_label} — задача:\n\n☐ {res['text']}",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif t == "idea":
        add_idea(res["category"], res["text"])
        labels = {"business": "💡 Бизнес-идея", "marketing": "🎯 Маркетинг", "personal": "🏃 Личное"}
        await update.message.reply_text(
            f"{labels.get(res['category'], '💡')}:\n\n☐ {res['text']}"
        )

    elif t == "ask":
        await update.message.chat.send_action("typing")
        answer = ask_claude(res["text"])
        await update.message.reply_text(f"💬 {answer}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("done:"):
        tid = int(q.data.split(":")[1])
        if complete_task(tid):
            old = q.message.reply_markup
            if old:
                rows = []
                for row in old.inline_keyboard:
                    new_row = [b for b in row if b.callback_data != q.data]
                    if new_row: rows.append(new_row)
                try:
                    new_text = q.message.text
                    if rows:
                        await q.edit_message_text(
                            text=new_text + "\n\n✅ Выполнено!",
                            reply_markup=InlineKeyboardMarkup(rows),
                        )
                    else:
                        await q.edit_message_text(text=new_text + "\n\n👏 Все задачи выполнены!")
                except Exception:
                    await q.answer("✅ Выполнено!")
        else:
            await q.answer("⚠️ Уже выполнена или не найдена")

# ─────────── Утренний дайджест ───────────
async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id: return
    # удаляем вчерашний дайджест чтобы чат не засорялся
    last_id = get_state("last_digest_msg_id")
    if last_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=int(last_id))
        except Exception:
            pass
    msg, kb = build_digest()
    sent = await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown", reply_markup=kb)
    set_state("last_digest_msg_id", sent.message_id)
    log.info("MORNING DIGEST sent")

# ─────────── Main ───────────
async def run():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Каждый день в 10:00 по Алматы
    app.job_queue.run_daily(
        morning_job,
        time=dtime(hour=10, minute=0, tzinfo=ALMATY_TZ),
        name="morning_digest",
    )

    log.info("🤖 STK Bot starting (упрощённая версия)")
    log.info("   OWNER_ID=%s ANTHROPIC=%s CITY=%s TZ=Asia/Almaty",
             OWNER_ID, "ON" if ANTHROPIC_KEY else "OFF", WEATHER_CITY)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(run())
