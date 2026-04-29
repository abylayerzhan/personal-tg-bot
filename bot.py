"""
STK Bot v3.2 — облачная версия с:
- Карточкой дня (7 чекбоксов, закреплённая, сбрасывается на 0 в 5:00)
- Точечными напоминаниями (11:00, 15:00, 19:00, 21:00, 23:00, 00:00)
- Гибкой тренажёркой (3+/неделю, без привязки к Пн/Ср/Пт)
- Раздельными streak'ами (тренажёрка, утренний/вечерний уход)
- Двусторонним синком STK / CLOQ / Personal / Ideas / Daily Tracker
- LLM-классификатором (Claude Haiku) + голосовыми (Whisper)
- Bot Menu кнопками (/today /progress /tasks /weather)

Деплой: Render Free Background Worker.
"""
import os
import re
import io
import json
import asyncio
import sqlite3
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime, date as ddate

import httpx
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, MenuButtonCommands,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

load_dotenv()

# ─────────── ENV ───────────
TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
OWNER_ID = int(os.environ.get("OWNER_CHAT_ID", "0").strip()) or None
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Almaty").strip()
DB_PATH = os.environ.get("DB_PATH", "data.db").strip()

# Notion
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
DB_STK = os.environ.get("NOTION_DB_STK", "").strip()
DB_CLOQ = os.environ.get("NOTION_DB_CLOQ", "").strip()
DB_PERSONAL = os.environ.get("NOTION_DB_PERSONAL", "").strip()
DB_IDEAS = os.environ.get("NOTION_DB_IDEAS", "").strip()
DB_DAILY = os.environ.get("NOTION_DB_DAILY", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("stk-bot")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

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
            notion_id TEXT,
            created_at INTEGER NOT NULL,
            done_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'business',
            text TEXT NOT NULL,
            notion_id TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            remind_at INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            kind TEXT DEFAULT 'general',
            sent INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily (
            date TEXT PRIMARY KEY,
            morning_care INTEGER DEFAULT 0,
            breakfast INTEGER DEFAULT 0,
            protein INTEGER DEFAULT 0,
            lunch INTEGER DEFAULT 0,
            gym INTEGER DEFAULT 0,
            dinner INTEGER DEFAULT 0,
            evening_care INTEGER DEFAULT 0,
            card_msg_id INTEGER,
            notion_id TEXT,
            updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    c.commit()
    c.close()


def get_state(key, default=None):
    c = db()
    r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else default


def set_state(key, value):
    c = db()
    c.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    c.commit()
    c.close()


# ─────────── Markdown escape for Telegram ───────────
def md_escape(text: str) -> str:
    """Экранирует только реально проблемные для Markdown символы."""
    if not text:
        return ""
    # для ParseMode.MARKDOWN (legacy) экранируем _ * ` [
    return re.sub(r"([_*`\[])", r"\\\1", text)


# ─────────── Погода ───────────
def get_weather():
    try:
        url = f"https://wttr.in/{urllib.parse.quote(WEATHER_CITY)}?format=%t|%C|%w&lang=ru"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            parts = r.read().decode().strip().split("|")
        temp, cond, wind = parts[0].strip(), parts[1].strip(), parts[2].strip()
        m = re.search(r"[+-]?\d+", temp)
        t = int(m.group()) if m else 15
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


# ─────────── Notion API ───────────
class Notion:
    """Асинхронный клиент Notion."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=30.0, headers=self.headers)

    async def close(self):
        await self.client.aclose()

    async def create_page(self, db_id: str, properties: dict, children: list = None) -> str | None:
        """Создаёт страницу в БД, возвращает её id."""
        if not self.token or not db_id:
            return None
        body = {
            "parent": {"database_id": db_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        try:
            r = await self.client.post(f"{NOTION_API}/pages", json=body)
            if r.status_code != 200:
                log.error("notion create_page %s: %s", r.status_code, r.text[:300])
                return None
            return r.json()["id"]
        except Exception as e:
            log.error("notion create_page error: %s", e)
            return None

    async def update_page(self, page_id: str, properties: dict) -> bool:
        if not self.token or not page_id:
            return False
        try:
            r = await self.client.patch(
                f"{NOTION_API}/pages/{page_id}",
                json={"properties": properties},
            )
            if r.status_code != 200:
                log.error("notion update_page %s: %s", r.status_code, r.text[:300])
                return False
            return True
        except Exception as e:
            log.error("notion update error: %s", e)
            return False

    async def query(self, db_id: str, filter_: dict = None, sorts: list = None,
                    page_size: int = 10) -> list:
        if not self.token or not db_id:
            return []
        body = {"page_size": page_size}
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        try:
            r = await self.client.post(
                f"{NOTION_API}/databases/{db_id}/query",
                json=body,
            )
            if r.status_code != 200:
                log.error("notion query %s: %s", r.status_code, r.text[:300])
                return []
            return r.json().get("results", [])
        except Exception as e:
            log.error("notion query error: %s", e)
            return []

    async def get_page(self, page_id: str) -> dict | None:
        try:
            r = await self.client.get(f"{NOTION_API}/pages/{page_id}")
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            log.error("notion get_page error: %s", e)
            return None

    async def get_blocks(self, page_id: str) -> list:
        """Получает все блоки страницы."""
        try:
            r = await self.client.get(
                f"{NOTION_API}/blocks/{page_id}/children",
                params={"page_size": 100},
            )
            if r.status_code != 200:
                return []
            return r.json().get("results", [])
        except Exception as e:
            log.error("notion get_blocks error: %s", e)
            return []


notion = Notion(NOTION_TOKEN) if NOTION_TOKEN else None


# ─────────── Notion property builders ───────────
def prop_title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def prop_rich(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def prop_select(name: str) -> dict:
    return {"select": {"name": name}}


def prop_multi(names: list) -> dict:
    return {"multi_select": [{"name": n} for n in names]}


def prop_status(name: str) -> dict:
    return {"status": {"name": name}}


def prop_check(value: bool) -> dict:
    return {"checkbox": value}


def prop_date(d: ddate | None) -> dict:
    if not d:
        return {"date": None}
    return {"date": {"start": d.isoformat()}}


def prop_number(n: int | float | None) -> dict:
    if n is None:
        return {"number": None}
    return {"number": n}


# ─────────── Notion sync helpers ───────────
async def sync_task_to_notion(project: str, priority: str, text: str) -> str | None:
    """Создаёт задачу в Notion. Возвращает notion_id."""
    if not notion:
        return None
    db_map = {"stk": DB_STK, "cloq": DB_CLOQ}
    db_id = db_map.get(project)
    if not db_id:
        return None
    pri_map = {"urgent": "🔴 Срочно", "important": "🟡 Важно", "strategic": "🟢 Стратегия"}
    props = {
        "Name": prop_title(text),
        "Priority": prop_select(pri_map.get(priority, "🔴 Срочно")),
        "Status": prop_status("Not started"),
    }
    return await notion.create_page(db_id, props)


async def sync_task_done(notion_id: str) -> bool:
    if not notion or not notion_id:
        return False
    return await notion.update_page(notion_id, {
        "Status": prop_status("Done"),
    })


async def sync_idea_to_notion(category: str, text: str) -> str | None:
    if not notion:
        return None
    db_id = DB_PERSONAL if category == "personal" else DB_IDEAS
    if not db_id:
        return None
    props = {"Name": prop_title(text)}
    if category != "personal":
        cat_map = {"business": "💡 Бизнес", "marketing": "🎯 Маркетинг"}
        props["Category"] = prop_select(cat_map.get(category, "💡 Бизнес"))
    return await notion.create_page(db_id, props)


async def sync_daily_to_notion(d: ddate, fields: dict) -> str | None:
    """Создаёт или обновляет дневную карточку в Notion Daily Tracker."""
    if not notion or not DB_DAILY:
        return None
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    day_name = days[d.weekday()]
    title = f"{d.strftime('%d.%m.%Y')} · {day_name}"

    # Проверка — есть ли уже запись на эту дату
    c = db()
    row = c.execute("SELECT notion_id FROM daily WHERE date=?", (d.isoformat(),)).fetchone()
    c.close()

    # Преобразуем в формат Notion
    notion_props = {
        "Day": prop_title(title),
        "Date": prop_date(d),
    }
    field_to_notion = {
        "morning_care": "🧴 Уход утром",
        "breakfast": "🍳 Завтрак",
        "protein": "💪 Протеин",
        "lunch": "🍽 Обед",
        "gym": "🏋 Тренажёрка",
        "dinner": "🍝 Ужин",
        "evening_care": "🧴 Уход вечером",
    }
    for key, notion_name in field_to_notion.items():
        if key in fields:
            notion_props[notion_name] = prop_check(bool(fields[key]))

    if row and row["notion_id"]:
        await notion.update_page(row["notion_id"], notion_props)
        return row["notion_id"]
    else:
        nid = await notion.create_page(DB_DAILY, notion_props)
        if nid:
            c = db()
            c.execute(
                "INSERT OR REPLACE INTO daily(date, notion_id, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET notion_id=excluded.notion_id",
                (d.isoformat(), nid, int(datetime.now().timestamp())),
            )
            c.commit()
            c.close()
        return nid



# ─────────── Anthropic / Claude API ───────────
def get_anthropic_client():
    if not ANTHROPIC_KEY:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_KEY)
    except Exception as e:
        log.error("anthropic init: %s", e)
        return None


def llm_classify(text: str) -> dict | None:
    """LLM-классификатор: возвращает {type, ...} или None если не уверен."""
    client = get_anthropic_client()
    if not client:
        return None
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                "Ты классифицируешь короткие сообщения Абылая (владелец STK парфюмерия Алматы и CLOQ часы). "
                "Верни ТОЛЬКО JSON без markdown, без объяснений. "
                "Категории: "
                "1) {\"type\":\"task\", \"project\":\"stk\"|\"cloq\", \"priority\":\"urgent\"|\"important\", \"text\":\"...\"} — рабочие задачи "
                "2) {\"type\":\"idea\", \"category\":\"business\"|\"marketing\"|\"personal\", \"text\":\"...\"} — идеи и личные дела "
                "3) {\"type\":\"reminder\", \"text\":\"...\", \"hours_from_now\":N} — напоминания со временем "
                "ВАЖНО: если сомневаешься — выбирай personal. "
                "Бытовые дела (купить, встреча, ремонт, врач, посылка) — всегда personal. "
                "Только явные рабочие заявления (исправить цену, добавить товар, KPI, отчёт) — task."
            ),
            messages=[{"role": "user", "content": text[:500]}],
        )
        raw = msg.content[0].text.strip()
        # Убираем markdown fences если есть
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.warning("llm_classify failed: %s", e)
        return None



def ask_claude(question: str) -> str:
    client = get_anthropic_client()
    if not client:
        return "AI выключен (ANTHROPIC_API_KEY не задан)"
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=(
                "Ты помощник Абылая (STK парфюмерия, Алматы, и CLOQ часы). "
                "Цены STK: 30мл=42 580 тг, 50мл=63 700 тг, Kaspi=3 548 тг/мес. "
                "137 менеджеров, 8 отделов. Отвечай кратко (2-4 предложения), по делу."
            ),
            messages=[{"role": "user", "content": question}],
        )
        return msg.content[0].text
    except Exception as e:
        log.error("claude error: %s", e)
        return f"⚠️ Ошибка AI: {e}"


# ─────────── Voice transcription ───────────
async def transcribe_voice(audio_bytes: bytes) -> str | None:
    """Whisper через Groq → fallback OpenAI."""
    if GROQ_KEY:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                files = {"file": ("voice.ogg", audio_bytes, "audio/ogg")}
                data = {"model": "whisper-large-v3", "language": "ru"}
                r = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    files=files, data=data,
                )
                if r.status_code == 200:
                    return r.json().get("text", "").strip()
                log.warning("groq whisper %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("groq whisper error: %s", e)

    if OPENAI_KEY:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                files = {"file": ("voice.ogg", audio_bytes, "audio/ogg")}
                data = {"model": "whisper-1"}
                r = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                    files=files, data=data,
                )
                if r.status_code == 200:
                    return r.json().get("text", "").strip()
        except Exception as e:
            log.error("openai whisper error: %s", e)
    return None


# ─────────── Классификация (regex + LLM fallback) ───────────
TASK_PREFIX = re.compile(r"^(задача|таск|todo|сделать)[:\s]+(.+)", re.IGNORECASE)
IMPORTANT_PREFIX = re.compile(r"^важно[:\s]+(.+)", re.IGNORECASE)
IDEA_PREFIX = re.compile(r"^идея[:\s]+(.+)", re.IGNORECASE)
MARKETING_PREFIX = re.compile(r"^(маркетинг|реклама|креатив)[:\s]+(.+)", re.IGNORECASE)
CLOQ_PREFIX = re.compile(r"^(cloq|клок)[:\s]+(.+)", re.IGNORECASE)

PERSONAL_WORDS = [
    "купить", "купи ", "тренировк", "встреча", "встретит", "посылк",
    "ремонт", "врач", "стрижк", "спортзал", "магазин", "стоматолог",
    "забрать", "отвезти", "съездить", "позвонить маме", "позвонить папе",
    "родител", "семь", "жен", "ребёнок", "детям", "детск", "школ",
    "одежд", "обув", "продукт", "ужин с", "обед с", "выходной",
    "отпуск", "путешеств", "перелёт", "билет", "виза",
    "паспорт", "права", "документ", "банкомат", "kaspi", "каспи",
    "уборк", "стирк", "химчистк", "массаж", "салон",
    "подарок", "поздравить", "день рожден", "годовщин",
]

GYM_WORDS = ["тренировк", "спорт", "зал ", "зала", "залу", "gym", "жаттығу", "качалк"]


def parse_time(text: str):
    """Возвращает (datetime, чистый_текст, early_flag) или None."""
    now = datetime.now()
    tomorrow = now + timedelta(days=1)
    early = any(w in text.lower() for w in GYM_WORDS)

    m = re.search(r"завтра\s+в\s+(\d{1,2})(?::(\d{2}))?", text, re.IGNORECASE)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        dt = tomorrow.replace(hour=h, minute=mi, second=0, microsecond=0)
        clean = re.sub(r"завтра\s+в\s+\d{1,2}(?::\d{2})?\s*", "", text, flags=re.IGNORECASE).strip()
        return dt, clean or text, early

    m = re.search(r"^в\s+(\d{1,2})(?::(\d{2}))?\s+(.+)", text, re.IGNORECASE)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt, m.group(3).strip(), early

    m = re.search(r"через\s+(\d+)\s*(мин|час)", text, re.IGNORECASE)
    if m:
        amount = int(m.group(1))
        dt = now + (timedelta(hours=amount) if "час" in m.group(2).lower()
                    else timedelta(minutes=amount))
        clean = re.sub(r"через\s+\d+\s*(мин\w*|час\w*)\s*", "", text, flags=re.IGNORECASE).strip()
        return dt, clean or text, early

    if text.lower().startswith("напомни"):
        rest = re.sub(r"^напомни\w*[:\s]*", "", text, flags=re.IGNORECASE).strip()
        sub = parse_time(rest)
        if sub:
            return sub
        return now + timedelta(hours=1), rest or text, False

    return None


def classify(text: str) -> dict:
    t = text.strip()
    low = t.lower()

    # 1. Гимнастика — отдельный класс для трекера
    if any(w in low for w in GYM_WORDS) and ("✅" in t or "сходил" in low or "была" in low or "был" in low):
        return {"type": "gym_done"}

    # 2. Время → напоминание
    parsed = parse_time(t)
    if parsed:
        dt, clean, early = parsed
        return {"type": "reminder", "text": clean, "dt": dt, "early": early}

    # 3. CLOQ
    m = CLOQ_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "cloq", "priority": "urgent", "text": m.group(2).strip()}

    # 4. Маркетинг
    m = MARKETING_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "marketing", "text": m.group(2).strip()}

    # 5. Срочная STK
    m = TASK_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "priority": "urgent", "text": m.group(2).strip()}

    # 6. Важная STK
    m = IMPORTANT_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "priority": "important", "text": m.group(1).strip()}

    # 7. Явная идея
    m = IDEA_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "business", "text": m.group(1).strip()}

    # 8. Команды
    cmd_map = {
        ("задачи", "tasks", "статус", "план", "сегодня", "карточка"): "cmd_today",
        ("прогресс", "progress", "стрик"): "cmd_progress",
        ("погода", "weather"): "cmd_weather",
    }
    for keys, ct in cmd_map.items():
        if low in keys:
            return {"type": ct}

    if low.startswith(("?", "вопрос:")) and ANTHROPIC_KEY:
        return {"type": "ask",
                "text": re.sub(r"^[?]\s*|вопрос:\s*", "", t, flags=re.IGNORECASE)}

    # 9. Personal по словам
    if any(w in low for w in PERSONAL_WORDS):
        return {"type": "idea", "category": "personal", "text": t}

    # 10. LLM fallback
    if ANTHROPIC_KEY and len(t) >= 10:
        llm_res = llm_classify(t)
        if llm_res:
            log.info("LLM classified: %s", llm_res.get("type"))
            if llm_res.get("type") == "reminder" and llm_res.get("hours_from_now"):
                dt = datetime.now() + timedelta(hours=llm_res["hours_from_now"])
                return {"type": "reminder", "text": llm_res.get("text", t), "dt": dt, "early": False}
            return llm_res

    # 11. Default — personal
    return {"type": "idea", "category": "personal", "text": t}


# ─────────── Бизнес-логика ───────────
def add_task(project, priority, text, notion_id=None):
    c = db()
    cur = c.execute(
        "INSERT INTO tasks(project,priority,text,notion_id,created_at) VALUES(?,?,?,?,?)",
        (project, priority, text, notion_id, int(datetime.now().timestamp())),
    )
    c.commit()
    tid = cur.lastrowid
    c.close()
    return tid


def add_idea(category, text, notion_id=None):
    c = db()
    cur = c.execute(
        "INSERT INTO ideas(category,text,notion_id,created_at) VALUES(?,?,?,?)",
        (category, text, notion_id, int(datetime.now().timestamp())),
    )
    c.commit()
    iid = cur.lastrowid
    c.close()
    return iid


def add_reminder_db(text, dt, chat_id, kind="general"):
    c = db()
    cur = c.execute(
        "INSERT INTO reminders(text,remind_at,chat_id,kind,created_at) VALUES(?,?,?,?,?)",
        (text, int(dt.timestamp()), chat_id, kind, int(datetime.now().timestamp())),
    )
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def complete_task(tid):
    c = db()
    row = c.execute("SELECT notion_id FROM tasks WHERE id=? AND done=0", (tid,)).fetchone()
    if not row:
        c.close()
        return None
    c.execute("UPDATE tasks SET done=1, done_at=? WHERE id=?",
              (int(datetime.now().timestamp()), tid))
    c.commit()
    nid = row["notion_id"]
    c.close()
    return nid


def get_open_tasks(project=None, priority=None, limit=10):
    c = db()
    q = "SELECT * FROM tasks WHERE done=0"
    args = []
    if project:
        q += " AND project=?"
        args.append(project)
    if priority:
        q += " AND priority=?"
        args.append(priority)
    q += " ORDER BY id ASC LIMIT ?"
    args.append(limit)
    rows = c.execute(q, args).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_personal_ideas(limit=10):
    c = db()
    rows = c.execute(
        "SELECT id, text FROM ideas WHERE category='personal' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ─────────── Daily card ───────────
DAILY_FIELDS = [
    ("morning_care", "🧴", "Уход утром"),
    ("breakfast", "🍳", "Завтрак"),
    ("protein", "💪", "Протеин"),
    ("lunch", "🍽", "Обед"),
    ("gym", "🏋", "Тренажёрка"),
    ("dinner", "🍝", "Ужин"),
    ("evening_care", "🧴", "Уход вечером"),
]


def get_daily_row(d: ddate = None) -> dict:
    if d is None:
        d = ddate.today()
    c = db()
    r = c.execute("SELECT * FROM daily WHERE date=?", (d.isoformat(),)).fetchone()
    if not r:
        c.execute("INSERT INTO daily(date, updated_at) VALUES(?,?)",
                  (d.isoformat(), int(datetime.now().timestamp())))
        c.commit()
        r = c.execute("SELECT * FROM daily WHERE date=?", (d.isoformat(),)).fetchone()
    c.close()
    return dict(r)


def toggle_daily_field(field: str, d: ddate = None) -> dict:
    """Переключает чекбокс. Возвращает обновлённую строку."""
    if d is None:
        d = ddate.today()
    get_daily_row(d)  # ensure exists
    c = db()
    c.execute(
        f"UPDATE daily SET {field} = 1 - {field}, updated_at=? WHERE date=?",
        (int(datetime.now().timestamp()), d.isoformat()),
    )
    c.commit()
    r = c.execute("SELECT * FROM daily WHERE date=?", (d.isoformat(),)).fetchone()
    c.close()
    return dict(r)


def daily_score(row: dict) -> int:
    return sum(int(row.get(f[0], 0) or 0) for f in DAILY_FIELDS)


def build_daily_card(d: ddate = None) -> tuple[str, InlineKeyboardMarkup]:
    if d is None:
        d = ddate.today()
    row = get_daily_row(d)
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    score = daily_score(row)

    # Прогресс-бар
    filled = "█" * score
    empty = "░" * (8 - score)
    bar = filled + empty

    msg = f"📅 *{d.strftime('%d.%m')}* · {days[d.weekday()]}\n"
    msg += f"`{bar}` *{score}/8*\n\n"

    # Список галочек
    for key, ico, name in DAILY_FIELDS:
        check = "✅" if row.get(key) else "☐"
        msg += f"{check} {ico} {name}\n"

    # Кнопки 2x4
    kb = []
    for i in range(0, len(DAILY_FIELDS), 2):
        row_btns = []
        for j in range(2):
            if i + j >= len(DAILY_FIELDS):
                break
            key, ico, name = DAILY_FIELDS[i + j]
            check = "✅" if row.get(key) else "☐"
            row_btns.append(
                InlineKeyboardButton(f"{check} {ico} {name[:14]}",
                                     callback_data=f"daily:{key}")
            )
        kb.append(row_btns)

    return msg, InlineKeyboardMarkup(kb)


# ─────────── Streak'и ───────────
def calc_streak(field: str) -> int:
    """Считает streak подряд идущих дней с галочкой."""
    c = db()
    rows = c.execute(
        f"SELECT date, {field} FROM daily ORDER BY date DESC LIMIT 365"
    ).fetchall()
    c.close()
    streak = 0
    expected = ddate.today()
    for r in rows:
        rd = ddate.fromisoformat(r["date"])
        if rd > expected:
            continue  # будущие — пропускаем
        if rd != expected:
            # Пропуск дня
            break
        if not r[field]:
            break
        streak += 1
        expected = expected - timedelta(days=1)
    return streak


def calc_gym_week_count() -> int:
    """Сколько раз был в зале на этой неделе (Пн-Вс)."""
    today = ddate.today()
    monday = today - timedelta(days=today.weekday())
    c = db()
    n = c.execute(
        "SELECT COUNT(*) AS n FROM daily WHERE date >= ? AND gym = 1",
        (monday.isoformat(),),
    ).fetchone()["n"]
    c.close()
    return n


# ─────────── Telegram Handlers ───────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *STK Bot v3.2*\n\n"
        "📅 `сегодня` — карточка дня (7 чекбоксов)\n"
        "📊 `прогресс` — стрики и статус\n"
        "📋 `задачи` — открытые задачи\n"
        "🌡 `погода`\n\n"
        "*Просто пиши что хочешь записать* — бот сам разберётся:\n"
        "• `задача: исправить цену` → STK 🔴\n"
        "• `cloq: купить часы` → CLOQ\n"
        "• `маркетинг: новый креатив` → 🎯\n"
        "• `купить молоко` → 🏃 личное\n"
        "• `завтра в 7 тренировка` → ⏰\n"
        "• `?какая маржа на 50мл` → 💬 AI\n"
        "🎤 *голосовые* тоже работают.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать карточку дня."""
    msg, kb = build_daily_card()
    sent = await update.effective_chat.send_message(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )
    # Пытаемся закрепить
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        log.warning("pin failed: %s", e)
    # Сохраняем msg_id
    today = ddate.today().isoformat()
    c = db()
    c.execute("UPDATE daily SET card_msg_id=? WHERE date=?", (sent.message_id, today))
    c.commit()
    c.close()


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    morning_streak = calc_streak("morning_care")
    evening_streak = calc_streak("evening_care")
    gym_week = calc_gym_week_count()

    msg = "📊 *Прогресс*\n\n"
    msg += "*🧴 Привычки*\n"
    msg += f"   🌅 Утренний уход: *{morning_streak}* дн\n"
    msg += f"   🌙 Вечерний уход: *{evening_streak}* дн\n"
    msg += f"   🏋 Тренажёрка эта неделя: *{gym_week}* раз\n"
    if gym_week >= 3:
        msg += "   ✨ _норма выполнена_\n"
    elif gym_week == 2:
        msg += "   💪 _осталось 1 раз_\n"
    else:
        msg += f"   ⚠️ _нужно ещё {3 - gym_week}_\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)



async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_weather())


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка голосовых сообщений."""
    if OWNER_ID and update.effective_user.id != OWNER_ID:
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        audio_bytes = buf.getvalue()
    except Exception as e:
        log.error("download voice: %s", e)
        await update.message.reply_text("⚠️ Не получилось скачать голосовое.")
        return

    text = await transcribe_voice(audio_bytes)
    if not text:
        await update.message.reply_text(
            "⚠️ Транскрипция недоступна (нет GROQ_API_KEY / OPENAI_API_KEY)."
        )
        return

    await update.message.reply_text(f"🎤 _распознал:_\n{text}", parse_mode=ParseMode.MARKDOWN)

    # Иначе обрабатываем как обычное сообщение
    await process_text(update, context, text)


async def refresh_pinned_daily(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Обновляет закреплённую карточку дня."""
    today = ddate.today().isoformat()
    c = db()
    r = c.execute("SELECT card_msg_id FROM daily WHERE date=?", (today,)).fetchone()
    c.close()
    if not r or not r["card_msg_id"]:
        return
    msg, kb = build_daily_card()
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=r["card_msg_id"],
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    except Exception as e:
        log.debug("refresh daily card: %s", e)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений."""
    if OWNER_ID and update.effective_user.id != OWNER_ID:
        return
    text = update.message.text.strip()
    if not text:
        return
    set_state("last_chat_id", update.effective_chat.id)
    await process_text(update, context, text)


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    log.info("MSG: %s", text[:80])
    res = classify(text)
    log.info("CLS: %s", res.get("type"))
    t = res["type"]
    chat_id = update.effective_chat.id

    if t == "cmd_today":
        await cmd_today(update, context)

    elif t == "cmd_progress":
        await cmd_progress(update, context)

    elif t == "cmd_weather":
        await cmd_weather(update, context)

    elif t == "gym_done":
        # Отметить gym в сегодня
        row = toggle_daily_field("gym")
        await sync_daily_to_notion(ddate.today(), {"gym": row["gym"]})
        gym_week = calc_gym_week_count()
        kept = "✅" if row["gym"] else "☐"
        msg = f"{kept} 🏋 Тренажёрка отмечена сегодня\n📊 Эта неделя: *{gym_week}*/3+ раз"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        await refresh_pinned_daily(context, chat_id)

    elif t == "task":
        # Сначала Notion (чтобы получить notion_id)
        nid = await sync_task_to_notion(res["project"], res["priority"], res["text"])
        tid = add_task(res["project"], res["priority"], res["text"], nid)
        icons = {"urgent": "🔴", "important": "🟡", "strategic": "🟢"}
        proj = "STK" if res["project"] == "stk" else "CLOQ"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готово", callback_data=f"task_done:{tid}"),
             InlineKeyboardButton("🔄 Переместить", callback_data=f"task_move:{tid}")],
        ])
        sync_mark = " ☁️" if nid else ""
        await update.message.reply_text(
            f"{icons.get(res['priority'], '🔴')} *{proj}{sync_mark}*\n\n☐ {md_escape(res['text'])}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    elif t == "idea":
        nid = await sync_idea_to_notion(res["category"], res["text"])
        iid = add_idea(res["category"], res["text"], nid)
        labels = {"business": "💡 Бизнес-идея", "marketing": "🎯 Маркетинг", "personal": "🏃 Личное"}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Переместить", callback_data=f"idea_move:{iid}"),
             InlineKeyboardButton("❌ Удалить", callback_data=f"idea_del:{iid}")],
        ])
        sync_mark = " ☁️" if nid else ""
        await update.message.reply_text(
            f"{labels.get(res['category'], '💡')}{sync_mark}\n\n☐ {md_escape(res['text'])}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    elif t == "reminder":
        dt = res["dt"]
        delay = (dt - datetime.now()).total_seconds()
        if delay <= 0:
            await update.message.reply_text("⚠️ Время прошло")
            return
        rid = add_reminder_db(res["text"], dt, chat_id)
        # Доп напоминание за час для тренировок
        if res.get("early") and delay > 3600:
            context.job_queue.run_once(
                send_reminder_job, delay - 3600,
                data={"text": f"⏰ Через 1 час: {res['text']}"},
                chat_id=chat_id,
                name=f"rem_early_{rid}",
            )
        context.job_queue.run_once(
            send_reminder_job, delay,
            data={"text": f"🔔 {res['text']}", "rid": rid},
            chat_id=chat_id,
            name=f"rem_{rid}",
        )
        # Google Calendar
        gcal = (
            "https://calendar.google.com/calendar/render?"
            + urllib.parse.urlencode({
                "action": "TEMPLATE",
                "text": res["text"],
                "dates": f"{dt.strftime('%Y%m%dT%H%M%S')}/"
                         f"{(dt + timedelta(hours=1)).strftime('%Y%m%dT%H%M%S')}",
            })
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 В Google Calendar", url=gcal)],
        ])
        msg = f"⏰ *{md_escape(res['text'])}*\n📅 {dt.strftime('%d.%m в %H:%M')}"
        if res.get("early"):
            msg += "\n⏰ + напомню за 1 час"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif t == "ask":
        await update.message.chat.send_action(ChatAction.TYPING)
        answer = ask_claude(res["text"])
        await update.message.reply_text(f"💬 {answer}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    # Карточка дня — переключение чекбоксов
    if data.startswith("daily:"):
        field = data.split(":")[1]
        if field not in [f[0] for f in DAILY_FIELDS]:
            return
        row = toggle_daily_field(field)
        await sync_daily_to_notion(ddate.today(), {field: row[field]})
        msg, kb = build_daily_card()
        try:
            await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            pass
        return

    # Задача — выполнено
    if data.startswith("task_done:"):
        tid = int(data.split(":")[1])
        nid = complete_task(tid)
        if nid:
            await sync_task_done(nid)
        try:
            await q.edit_message_text(
                text=q.message.text + "\n\n✅ Выполнено!",
            )
        except Exception:
            pass
        return

    # Задача — переместить (циклически)
    if data.startswith("task_move:"):
        tid = int(data.split(":")[1])
        c = db()
        r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not r:
            c.close()
            await q.answer("⚠️ Не найдено")
            return
        # Цикл: stk-urgent → stk-important → cloq-urgent → personal_idea
        cur = (r["project"], r["priority"])
        cycle = [
            ("stk", "urgent"), ("stk", "important"),
            ("cloq", "urgent"), ("cloq", "important"),
        ]
        if cur in cycle:
            idx = cycle.index(cur)
            nxt = cycle[(idx + 1) % len(cycle)]
        else:
            nxt = cycle[0]
        c.execute("UPDATE tasks SET project=?, priority=? WHERE id=?",
                  (nxt[0], nxt[1], tid))
        c.commit()
        c.close()
        icons = {"urgent": "🔴", "important": "🟡"}
        proj = nxt[0].upper()
        try:
            new_text = re.sub(r"^[🔴🟡🟢] \*\w+\*",
                              f"{icons[nxt[1]]} *{proj}*",
                              q.message.text)
            await q.edit_message_text(
                new_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=q.message.reply_markup,
            )
        except Exception:
            pass
        await q.answer(f"→ {proj} {nxt[1]}")
        return

    # Идея — удалить
    if data.startswith("idea_del:"):
        iid = int(data.split(":")[1])
        c = db()
        c.execute("DELETE FROM ideas WHERE id=?", (iid,))
        c.commit()
        c.close()
        try:
            await q.edit_message_text(q.message.text + "\n\n❌ Удалено")
        except Exception:
            pass
        return

    if data.startswith("idea_move:"):
        iid = int(data.split(":")[1])
        c = db()
        r = c.execute("SELECT * FROM ideas WHERE id=?", (iid,)).fetchone()
        if not r:
            c.close()
            await q.answer("⚠️ Не найдено")
            return
        cycle = ["personal", "business", "marketing"]
        cur = r["category"]
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]
        c.execute("UPDATE ideas SET category=? WHERE id=?", (nxt, iid))
        c.commit()
        c.close()
        await q.answer(f"→ {nxt}")
        labels = {"business": "💡 Бизнес-идея", "marketing": "🎯 Маркетинг", "personal": "🏃 Личное"}
        try:
            new_text = re.sub(r"^.*\n\n", f"{labels[nxt]}\n\n", q.message.text, count=1)
            await q.edit_message_text(
                new_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=q.message.reply_markup,
            )
        except Exception:
            pass
        return


def toggle_field_to_one(field: str):
    """Помечает поле как 1 (если ещё не)."""
    today = ddate.today().isoformat()
    c = db()
    c.execute(
        f"INSERT INTO daily(date, {field}, updated_at) VALUES(?, 1, ?) "
        f"ON CONFLICT(date) DO UPDATE SET {field}=1, updated_at=excluded.updated_at",
        (today, int(datetime.now().timestamp())),
    )
    c.commit()
    c.close()


# ─────────── Jobs ───────────
async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=data["text"],
    )
    if "rid" in data:
        c = db()
        c.execute("UPDATE reminders SET sent=1 WHERE id=?", (data["rid"],))
        c.commit()
        c.close()


async def morning_card_job(context: ContextTypes.DEFAULT_TYPE):
    """5:00 — отправить карточку дня и закрепить."""
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return

    # Открепить вчерашнюю
    yesterday = (ddate.today() - timedelta(days=1)).isoformat()
    c = db()
    r = c.execute("SELECT card_msg_id FROM daily WHERE date=?", (yesterday,)).fetchone()
    c.close()
    if r and r["card_msg_id"]:
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=r["card_msg_id"])
        except Exception:
            pass

    msg, kb = build_daily_card()
    full_msg = f"☀️ *Доброе утро, Абылай!*\n\n{get_weather()}\n\n{msg}"
    sent = await context.bot.send_message(
        chat_id=chat_id, text=full_msg,
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id, message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        log.warning("pin morning: %s", e)
    today = ddate.today().isoformat()
    c = db()
    c.execute(
        "INSERT OR REPLACE INTO daily(date, card_msg_id, updated_at) VALUES(?,?,?)",
        (today, sent.message_id, int(datetime.now().timestamp())),
    )
    c.commit()
    c.close()
    log.info("MORNING CARD sent and pinned")


async def reminder_11_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    row = get_daily_row()
    missing = []
    if not row.get("morning_care"):
        missing.append("🧴 уход")
    if not row.get("breakfast"):
        missing.append("🍳 завтрак")
    if not row.get("protein"):
        missing.append("💪 протеин")
    if missing:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏰ *11:00* — пора утром:\n" + "\n".join(f"  • {m}" for m in missing),
            parse_mode=ParseMode.MARKDOWN,
        )


async def reminder_15_job(context: ContextTypes.DEFAULT_TYPE):
    """15:00 — обед."""
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    row = get_daily_row()
    if row.get("lunch"):
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text="⏰ *15:00* — пора 🍽 обед",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reminder_19_job(context: ContextTypes.DEFAULT_TYPE):
    """19:00 — тренажёрка (если меньше 3 раз на неделе)."""
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    gym_week = calc_gym_week_count()
    row = get_daily_row()
    if row.get("gym"):
        return  # уже сходил
    if gym_week >= 3:
        return  # норма выполнена, не дёргаем
    remain = 3 - gym_week
    msg = f"🏋 *19:00 — тренажёрка*\nЭта неделя: {gym_week}/3 · нужно ещё *{remain}*"
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)


async def reminder_21_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    row = get_daily_row()
    if row.get("dinner"):
        return
    await context.bot.send_message(chat_id=chat_id, text="🍝 *21:00* — ужин",
                                   parse_mode=ParseMode.MARKDOWN)


async def reminder_23_job(context: ContextTypes.DEFAULT_TYPE):
    """23:00 — итог дня: что не сделано."""
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    row = get_daily_row()
    score = daily_score(row)
    missed = [name for key, ico, name in DAILY_FIELDS if not row.get(key)]

    msg = f"🌙 *23:00 · Итог дня: {score}/8*\n\n"
    if missed:
        msg += "Не сделано:\n" + "\n".join(f"  ☐ {m}" for m in missed)
    else:
        msg += "🎉 *Все 8 — идеальный день!*"
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)


async def reminder_00_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    row = get_daily_row()
    if row.get("evening_care"):
        return
    await context.bot.send_message(chat_id=chat_id, text="🧴 Уход вечером перед сном")


async def rehydrate_reminders(app: Application):
    """При старте — восстанавливает напоминания из БД."""
    c = db()
    rows = c.execute(
        "SELECT * FROM reminders WHERE sent=0 AND remind_at > ?",
        (int(datetime.now().timestamp()),),
    ).fetchall()
    c.close()
    n = 0
    for r in rows:
        delay = r["remind_at"] - datetime.now().timestamp()
        if delay <= 0:
            continue
        app.job_queue.run_once(
            send_reminder_job, delay,
            data={"text": f"🔔 {r['text']}", "rid": r["id"]},
            chat_id=r["chat_id"],
            name=f"rehyd_{r['id']}",
        )
        n += 1
    log.info("rehydrated %d reminders", n)


# ─────────── Setup commands ───────────
async def setup_bot_menu(app: Application):
    cmds = [
        BotCommand("start", "Помощь"),
        BotCommand("today", "📅 Карточка дня"),
        BotCommand("progress", "📊 Прогресс"),
        BotCommand("tasks", "📋 Задачи"),
        BotCommand("weather", "🌡 Погода"),
    ]
    try:
        await app.bot.set_my_commands(cmds)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        log.warning("setup commands: %s", e)


# ─────────── Main ───────────
async def cmd_tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tasks — все открытые задачи."""
    stk_u = get_open_tasks(project="stk", priority="urgent")
    stk_i = get_open_tasks(project="stk", priority="important")
    cloq = get_open_tasks(project="cloq")
    pers = get_personal_ideas(8)

    msg = "📋 *Открытые задачи*\n\n"
    if stk_u:
        msg += f"🔴 *STK · Срочно ({len(stk_u)}):*\n"
        for t in stk_u:
            msg += f"  ☐ {md_escape(t['text'][:50])}\n"
        msg += "\n"
    if stk_i:
        msg += f"🟡 *STK · Важно ({len(stk_i)}):*\n"
        for t in stk_i:
            msg += f"  ☐ {md_escape(t['text'][:50])}\n"
        msg += "\n"
    if cloq:
        msg += f"⌚ *CLOQ ({len(cloq)}):*\n"
        for t in cloq:
            msg += f"  ☐ {md_escape(t['text'][:50])}\n"
        msg += "\n"
    if pers:
        msg += f"🏃 *Личное ({len(pers)}):*\n"
        for p in pers:
            msg += f"  • {md_escape(p['text'][:50])}\n"

    if not (stk_u or stk_i or cloq or pers):
        msg += "_всё чисто 🎉_"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def run():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("tasks", cmd_tasks_handler))
    app.add_handler(CommandHandler("weather", cmd_weather))

    # Голос + текст
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Cron jobs
    jq = app.job_queue
    jq.run_daily(morning_card_job, time=dtime(5, 0), name="morning_card")
    jq.run_daily(reminder_11_job, time=dtime(11, 0), name="rem_11")
    jq.run_daily(reminder_15_job, time=dtime(15, 0), name="rem_15")
    jq.run_daily(reminder_19_job, time=dtime(19, 0), name="rem_19")
    jq.run_daily(reminder_21_job, time=dtime(21, 0), name="rem_21")
    jq.run_daily(reminder_23_job, time=dtime(23, 0), name="rem_23")
    jq.run_daily(reminder_00_job, time=dtime(0, 0), name="rem_00")

    log.info("🤖 STK Bot v3.2 starting — pinned daily card + flexible gym + streaks (no english)")
    log.info("   OWNER_ID=%s", OWNER_ID)
    log.info("   ANTHROPIC=%s GROQ=%s OPENAI=%s",
             "ON" if ANTHROPIC_KEY else "OFF",
             "ON" if GROQ_KEY else "OFF",
             "ON" if OPENAI_KEY else "OFF")
    log.info("   NOTION=%s DB_DAILY=%s",
             "ON" if NOTION_TOKEN else "OFF",
             "✓" if DB_DAILY else "—")
    log.info("   CITY=%s", WEATHER_CITY)

    await app.initialize()
    await app.start()
    await rehydrate_reminders(app)
    await setup_bot_menu(app)
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        if notion:
            await notion.close()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
