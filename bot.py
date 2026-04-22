"""
STK Telegram Bot — облачная версия
Работает на Render Free Background Worker.
Без зависимости от Claude CLI и Obsidian.
"""
import os
import re
import json
import asyncio
import sqlite3
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

load_dotenv()

# ─────────── ENV ───────────
TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_CHAT_ID", "0")) or None
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Almaty")
DB_PATH = os.environ.get("DB_PATH", "data.db")

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
            priority TEXT NOT NULL DEFAULT 'urgent',
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

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            remind_at INTEGER NOT NULL,
            kind TEXT DEFAULT 'general',
            sent INTEGER DEFAULT 0,
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

# ─────────── Бизнес-английский ───────────
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
TASK_PREFIX = re.compile(r"^(задача|таск|todo|сделать)[:\s]+(.+)", re.IGNORECASE)
IMPORTANT_PREFIX = re.compile(r"^важно[:\s]+(.+)", re.IGNORECASE)
IDEA_PREFIX = re.compile(r"^идея[:\s]+(.+)", re.IGNORECASE)
MARKETING_PREFIX = re.compile(r"^(маркетинг|реклама|креатив)[:\s]+(.+)", re.IGNORECASE)
CLOQ_PREFIX = re.compile(r"^(cloq|клок)[:\s]+(.+)", re.IGNORECASE)
PERSONAL_WORDS = ["купить", "купи ", "тренировк", "встреча", "посылк", "ремонт",
                  "врач", "стрижк", "спортзал", "магазин"]
HABIT_WORDS = {
    "тренировка": ["тренировк", "зал ", "спорт", "gym", "жаттығу"],
    "чтение": ["чтени", "книг", "read", "оқу"],
    "вода": ["вода 2", "вода ✅", "water", "су 2"],
    "подъём": ["подъём", "ранний подъ", "ерте тұрдым"],
}

def parse_time(text: str):
    """Возвращает (datetime, чистый_текст, early_flag) или None"""
    now = datetime.now()
    tomorrow = now + timedelta(days=1)
    early = any(w in text.lower() for w in ["тренировк", "спорт", "зал ", "gym"])

    # завтра в N(:MM)
    m = re.search(r"завтра\s+в\s+(\d{1,2})(?::(\d{2}))?", text, re.IGNORECASE)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        dt = tomorrow.replace(hour=h, minute=mi, second=0, microsecond=0)
        clean = re.sub(r"завтра\s+в\s+\d{1,2}(?::\d{2})?\s*", "", text, flags=re.IGNORECASE).strip()
        return dt, clean or text, early

    # сегодня в N
    m = re.search(r"^в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)", text, re.IGNORECASE)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dt <= now: dt += timedelta(days=1)
        return dt, m.group(3).strip(), early

    # через N мин/час
    m = re.search(r"через\s+(\d+)\s*(мин|час)", text, re.IGNORECASE)
    if m:
        amount = int(m.group(1))
        dt = now + (timedelta(hours=amount) if "час" in m.group(2).lower() else timedelta(minutes=amount))
        clean = re.sub(r"через\s+\d+\s*(мин\w*|час\w*)\s*", "", text, flags=re.IGNORECASE).strip()
        return dt, clean or text, early

    # напомни ...
    if text.lower().startswith("напомни"):
        rest = re.sub(r"^напомни\w*[:\s]*", "", text, flags=re.IGNORECASE).strip()
        sub = parse_time(rest)
        if sub: return sub
        # если без времени — через час по умолчанию
        return now + timedelta(hours=1), rest or text, False

    return None

def classify(text: str):
    """Возвращает dict с типом и данными"""
    t = text.strip()
    low = t.lower()

    # Привычки
    if "✅" in t or low.endswith(" ок") or low.endswith(" done"):
        clean = t.replace("✅", "").strip().rstrip(" ок").rstrip(" done")
        for habit, kws in HABIT_WORDS.items():
            if any(w in clean.lower() for w in kws):
                return {"type": "habit", "name": habit}

    # Время → напоминание
    parsed = parse_time(t)
    if parsed:
        dt, clean, early = parsed
        return {"type": "reminder", "text": clean, "dt": dt, "early": early}

    # CLOQ задачи
    m = CLOQ_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "cloq", "priority": "urgent", "text": m.group(2).strip()}

    # Маркетинг → идея
    m = MARKETING_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "marketing", "text": m.group(2).strip()}

    # Задача — срочная
    m = TASK_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "priority": "urgent", "text": m.group(2).strip()}

    # Задача — важная
    m = IMPORTANT_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "priority": "important", "text": m.group(1).strip()}

    # Идея явная
    m = IDEA_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "business", "text": m.group(1).strip()}

    # Личное (по словам)
    if any(w in low for w in PERSONAL_WORDS):
        return {"type": "idea", "category": "personal", "text": t}

    # Команды
    if low in ("задачи", "tasks", "статус", "план"):
        return {"type": "cmd_digest"}
    if low in ("привычки", "habits", "трекер"):
        return {"type": "cmd_habits"}
    if low in ("погода", "weather"):
        return {"type": "cmd_weather"}
    if low.startswith(("?", "вопрос:")) and ANTHROPIC_KEY:
        return {"type": "ask", "text": re.sub(r"^[?]\s*|вопрос:\s*", "", t, flags=re.IGNORECASE)}

    # По умолчанию — идея
    return {"type": "idea", "category": "business", "text": t}

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
def add_task(project, priority, text):
    c = db()
    cur = c.execute(
        "INSERT INTO tasks(project,priority,text,created_at) VALUES(?,?,?,?)",
        (project, priority, text, int(datetime.now().timestamp())),
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

def add_reminder(text, dt, kind="general"):
    c = db()
    cur = c.execute(
        "INSERT INTO reminders(text,remind_at,kind,created_at) VALUES(?,?,?,?)",
        (text, int(dt.timestamp()), kind, int(datetime.now().timestamp())),
    )
    c.commit(); rid = cur.lastrowid; c.close()
    return rid

def mark_habit(name):
    today = datetime.now().strftime("%Y-%m-%d")
    c = db()
    try:
        c.execute("INSERT INTO habits(name,date) VALUES(?,?)", (name, today))
        c.commit()
        # streak
        cur = c.execute("SELECT date FROM habits WHERE name=? ORDER BY date DESC", (name,))
        dates = [r["date"] for r in cur.fetchall()]
        streak = 0
        d = datetime.now().date()
        for ds in dates:
            if datetime.strptime(ds, "%Y-%m-%d").date() == d:
                streak += 1; d -= timedelta(days=1)
            else: break
        c.close()
        return streak
    except sqlite3.IntegrityError:
        c.close()
        return 0  # уже отмечено

def complete_task(tid):
    c = db()
    c.execute("UPDATE tasks SET done=1, done_at=? WHERE id=? AND done=0",
              (int(datetime.now().timestamp()), tid))
    c.commit(); changed = c.total_changes; c.close()
    return changed > 0

def get_open_tasks(project=None, priority=None, limit=10):
    c = db()
    q = "SELECT * FROM tasks WHERE done=0"
    args = []
    if project: q += " AND project=?"; args.append(project)
    if priority: q += " AND priority=?"; args.append(priority)
    q += " ORDER BY id ASC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall(); c.close()
    return [dict(r) for r in rows]

def get_done_count():
    c = db()
    n = c.execute("SELECT COUNT(*) AS n FROM tasks WHERE done=1").fetchone()["n"]
    c.close()
    return n

def get_habits_today():
    today = datetime.now().strftime("%Y-%m-%d")
    out = {}
    c = db()
    for habit in ["тренировка", "чтение", "вода", "подъём"]:
        done = c.execute("SELECT 1 FROM habits WHERE name=? AND date=?",
                         (habit, today)).fetchone() is not None
        # streak
        rows = c.execute("SELECT date FROM habits WHERE name=? ORDER BY date DESC", (habit,)).fetchall()
        streak = 0
        d = datetime.now().date()
        for r in rows:
            if datetime.strptime(r["date"], "%Y-%m-%d").date() == d:
                streak += 1; d -= timedelta(days=1)
            else: break
        out[habit] = (done, streak)
    c.close()
    return out

# ─────────── Дайджест ───────────
def build_digest():
    now = datetime.now()
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    msg = f"☀️ *Доброе утро, Абылай!*\n"
    msg += f"📅 {now.strftime('%d.%m.%Y')} · {days[now.weekday()]}\n\n"
    msg += get_weather() + "\n"

    tasks_for_buttons = []

    # STK
    stk_urgent = get_open_tasks(project="stk", priority="urgent", limit=8)
    stk_imp = get_open_tasks(project="stk", priority="important", limit=5)
    if stk_urgent or stk_imp:
        msg += "\n━━━━━ 🌹 *STK* ━━━━━\n"
        if stk_urgent:
            msg += f"\n🔴 *Срочно ({len(stk_urgent)}):*\n"
            for t in stk_urgent:
                msg += f"   ☐ {t['text'][:50]}\n"
                tasks_for_buttons.append((t["id"], t["text"]))
        if stk_imp:
            msg += f"\n🟡 *Важно ({len(stk_imp)}):*\n"
            for t in stk_imp:
                msg += f"   ☐ {t['text'][:50]}\n"
                tasks_for_buttons.append((t["id"], t["text"]))

    # CLOQ
    cloq_urgent = get_open_tasks(project="cloq", priority="urgent", limit=5)
    cloq_imp = get_open_tasks(project="cloq", priority="important", limit=5)
    if cloq_urgent or cloq_imp:
        msg += "\n━━━━━ ⌚ *CLOQ* ━━━━━\n"
        for t in cloq_urgent + cloq_imp:
            mark = "🔴" if t["priority"] == "urgent" else "🟡"
            msg += f"   ☐ {mark} {t['text'][:50]}\n"
            tasks_for_buttons.append((t["id"], t["text"]))

    # Личное (идеи personal)
    c = db()
    personal = c.execute(
        "SELECT id, text FROM ideas WHERE category='personal' ORDER BY id DESC LIMIT 8"
    ).fetchall()
    c.close()
    if personal:
        msg += "\n━━━━━ 🏃 *ЛИЧНОЕ* ━━━━━\n"
        for r in personal:
            msg += f"   • {r['text'][:50]}\n"

    # Привычки
    msg += "\n━━━━━ 🔥 *ПРИВЫЧКИ* ━━━━━\n"
    icons = {"тренировка": "🏋", "чтение": "📖", "вода": "💧", "подъём": "🌅"}
    for name, (done, streak) in get_habits_today().items():
        fire = "🔥" * min(streak, 5) if streak >= 2 else ""
        msg += f"   {icons[name]} {'✅' if done else '☐'} {name} · {streak} дн {fire}\n"

    # Английский дня
    day_idx = now.timetuple().tm_yday % len(BUSINESS_TERMS)
    term = BUSINESS_TERMS[day_idx]
    msg += "\n━━━━━ 🇬🇧 *БИЗНЕС-ТЕРМИН* ━━━━━\n"
    msg += f"\n*{term['term']}* {term['tr']}\n"
    msg += f"📖 _{term['ru']}_\n\n"
    msg += f"💬 {term['ex']}\n\n"
    msg += f"💡 {term['tip']}"

    # Кнопки выполнения
    keyboard = []
    for tid, text in tasks_for_buttons[:8]:
        keyboard.append([InlineKeyboardButton(f"✅ {text[:35]}", callback_data=f"done:{tid}")])
    kb = InlineKeyboardMarkup(keyboard) if keyboard else None
    return msg, kb

# ─────────── Handlers ───────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *STK Bot — облачная версия*\n\n"
        "Просто пиши задачи, идеи или напоминания.\n\n"
        "*Примеры:*\n"
        "• `задача: исправить цену` → 🔴 STK\n"
        "• `важно: добавить товары` → 🟡 STK\n"
        "• `cloq: купить часы` → ⌚ CLOQ\n"
        "• `маркетинг: новый креатив` → 🎯 Маркетинг\n"
        "• `тренировка завтра в 7` → ⏰ + за час\n"
        "• `напомни в 14 позвонить` → ⏰\n"
        "• `тренировка ✅` → 🔥 трекер\n"
        "• `задачи` → 📋 дайджест\n"
        "• `?какая маржа на 50мл` → 💬 AI-ответ",
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
        tid = add_task(res["project"], res["priority"], res["text"])
        icons = {"urgent": "🔴", "important": "🟡", "strategic": "🟢"}
        proj = "STK" if res["project"] == "stk" else "CLOQ"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{tid}")]])
        await update.message.reply_text(
            f"{icons.get(res['priority'], '🔴')} *{proj} — Задача:*\n\n☐ {res['text']}",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif t == "idea":
        add_idea(res["category"], res["text"])
        labels = {"business": "💡 Бизнес", "marketing": "🎯 Маркетинг", "personal": "🏃 Личное"}
        await update.message.reply_text(
            f"{labels.get(res['category'], '💡')}:\n\n☐ {res['text']}"
        )

    elif t == "reminder":
        dt = res["dt"]
        delay = (dt - datetime.now()).total_seconds()
        if delay <= 0:
            await update.message.reply_text("⚠️ Время прошло")
            return
        if res.get("early") and delay > 3600:
            context.job_queue.run_once(
                send_reminder, delay - 3600,
                data={"text": f"⏰ Через 1 час: {res['text']}"},
                chat_id=update.effective_chat.id,
            )
        context.job_queue.run_once(
            send_reminder, delay,
            data={"text": f"🔔 {res['text']}"},
            chat_id=update.effective_chat.id,
        )
        # google calendar link
        gcal = (
            "https://calendar.google.com/calendar/render?"
            + urllib.parse.urlencode({
                "action": "TEMPLATE",
                "text": res["text"],
                "dates": f"{dt.strftime('%Y%m%dT%H%M%S')}/{(dt + timedelta(hours=1)).strftime('%Y%m%dT%H%M%S')}",
            })
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📅 В Google Calendar", url=gcal)]])
        msg = f"⏰ *{res['text']}*\n📅 {dt.strftime('%d.%m в %H:%M')}"
        if res.get("early"): msg += "\n⏰ + напомню за 1 час"
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

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
            # убрать кнопку
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
                            text=new_text + f"\n\n✅ Выполнено!",
                            reply_markup=InlineKeyboardMarkup(rows),
                        )
                    else:
                        await q.edit_message_text(text=new_text + "\n\n👏 Все задачи выполнены!")
                except Exception:
                    await q.answer("✅ Выполнено!")
        else:
            await q.answer("⚠️ Уже выполнена или не найдена")

# ─────────── Jobs ───────────
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=context.job.chat_id, text=context.job.data["text"])

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id: return
    # удалить вчерашний дайджест
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

    app.job_queue.run_daily(
        morning_job,
        time=dtime(hour=5, minute=0, second=0),
        name="morning_digest",
    )

    log.info("🤖 STK Bot starting")
    log.info("   OWNER_ID=%s ANTHROPIC=%s CITY=%s",
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
