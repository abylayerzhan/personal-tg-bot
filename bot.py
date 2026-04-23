"""
STK Telegram Bot v2 — Notion-синхронизированная облачная версия
=================================================================
Новое в v2:
• Двусторонняя синхронизация с Notion (5 баз: STK, CLOQ, Личное, Идеи, Привычки)
• Голосовые сообщения через Whisper (Groq или OpenAI)
• LLM-классификатор (Claude Haiku) как fallback
• Персистентные напоминания (переживают рестарт)
• Еженедельный отчёт по воскресеньям в 21:00
• Команды: /sync, /help + текстовые "синк", "отмена"
• Кнопки "Удалить" и "Отменить" для каждой записи
"""
import os
import re
import json
import asyncio
import sqlite3
import logging
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime
from typing import Optional

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

load_dotenv()

# ═══════════════════════ ENV ═══════════════════════
TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
OWNER_ID = int(os.environ.get("OWNER_CHAT_ID", "0").strip()) or None
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Almaty").strip()
DB_PATH = os.environ.get("DB_PATH", "data.db").strip()

# Notion
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_STK = os.environ.get("NOTION_DB_STK", "").strip()
NOTION_DB_CLOQ = os.environ.get("NOTION_DB_CLOQ", "").strip()
NOTION_DB_PERSONAL = os.environ.get("NOTION_DB_PERSONAL", "").strip()
NOTION_DB_IDEAS = os.environ.get("NOTION_DB_IDEAS", "").strip()
NOTION_DB_HABITS = os.environ.get("NOTION_DB_HABITS", "").strip()

# Voice (хотя бы один)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

NOTION_ENABLED = bool(NOTION_TOKEN and NOTION_DB_STK)
VOICE_ENABLED = bool(OPENAI_API_KEY or GROQ_API_KEY)
LLM_CLASSIFIER_ENABLED = bool(ANTHROPIC_KEY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stk-bot")

# ═══════════════════════ SQLite ═══════════════════════
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
            done INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            done_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            remind_at INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            kind TEXT DEFAULT 'general',
            early_fired INTEGER DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS notion_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_type TEXT NOT NULL,
            local_id INTEGER NOT NULL,
            notion_page_id TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(local_type, local_id)
        );

        CREATE TABLE IF NOT EXISTS undo_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            local_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
    """)
    # миграция: если старая БД без done в ideas
    try:
        c.execute("SELECT done FROM ideas LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE ideas ADD COLUMN done INTEGER DEFAULT 0")
        c.execute("ALTER TABLE ideas ADD COLUMN done_at INTEGER")
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


def save_notion_map(local_type: str, local_id: int, notion_page_id: str):
    c = db()
    c.execute("""
        INSERT INTO notion_map(local_type, local_id, notion_page_id, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(local_type, local_id) DO UPDATE SET
            notion_page_id=excluded.notion_page_id,
            updated_at=excluded.updated_at
    """, (local_type, local_id, notion_page_id, int(datetime.now().timestamp())))
    c.commit()
    c.close()


def get_notion_page(local_type: str, local_id: int) -> Optional[str]:
    c = db()
    r = c.execute(
        "SELECT notion_page_id FROM notion_map WHERE local_type=? AND local_id=?",
        (local_type, local_id),
    ).fetchone()
    c.close()
    return r["notion_page_id"] if r else None


# ── tasks ──
def add_task(project, priority, text):
    c = db()
    cur = c.execute(
        "INSERT INTO tasks(project,priority,text,created_at) VALUES(?,?,?,?)",
        (project, priority, text, int(datetime.now().timestamp())),
    )
    c.commit()
    tid = cur.lastrowid
    c.close()
    return tid


def complete_task_local(tid) -> bool:
    c = db()
    c.execute(
        "UPDATE tasks SET done=1, done_at=? WHERE id=? AND done=0",
        (int(datetime.now().timestamp()), tid),
    )
    c.commit()
    changed = c.total_changes
    c.close()
    return changed > 0


def delete_task_local(tid):
    c = db()
    c.execute("DELETE FROM tasks WHERE id=?", (tid,))
    c.commit()
    c.close()


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


# ── ideas / personal ──
def add_idea(category, text):
    c = db()
    cur = c.execute(
        "INSERT INTO ideas(category,text,created_at) VALUES(?,?,?)",
        (category, text, int(datetime.now().timestamp())),
    )
    c.commit()
    iid = cur.lastrowid
    c.close()
    return iid


def complete_idea_local(iid) -> bool:
    c = db()
    c.execute(
        "UPDATE ideas SET done=1, done_at=? WHERE id=? AND done=0",
        (int(datetime.now().timestamp()), iid),
    )
    c.commit()
    changed = c.total_changes
    c.close()
    return changed > 0


def delete_idea_local(iid):
    c = db()
    c.execute("DELETE FROM ideas WHERE id=?", (iid,))
    c.commit()
    c.close()


# ── reminders ──
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


def mark_reminder_sent(rid):
    c = db()
    c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
    c.commit()
    c.close()


def mark_reminder_early_fired(rid):
    c = db()
    c.execute("UPDATE reminders SET early_fired=1 WHERE id=?", (rid,))
    c.commit()
    c.close()


def get_pending_reminders():
    c = db()
    rows = c.execute(
        "SELECT * FROM reminders WHERE sent=0 ORDER BY remind_at ASC"
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── habits ──
def mark_habit(name):
    today = datetime.now().strftime("%Y-%m-%d")
    c = db()
    try:
        cur = c.execute("INSERT INTO habits(name,date) VALUES(?,?)", (name, today))
        c.commit()
        hid = cur.lastrowid
        rows = c.execute(
            "SELECT date FROM habits WHERE name=? ORDER BY date DESC", (name,)
        ).fetchall()
        streak = 0
        d = datetime.now().date()
        for r in rows:
            if datetime.strptime(r["date"], "%Y-%m-%d").date() == d:
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        c.close()
        return streak, hid
    except sqlite3.IntegrityError:
        c.close()
        return 0, None


def get_habits_today():
    today = datetime.now().strftime("%Y-%m-%d")
    out = {}
    c = db()
    for habit in ["тренировка", "чтение", "вода", "подъём"]:
        done = c.execute(
            "SELECT 1 FROM habits WHERE name=? AND date=?", (habit, today)
        ).fetchone() is not None
        rows = c.execute(
            "SELECT date FROM habits WHERE name=? ORDER BY date DESC", (habit,)
        ).fetchall()
        streak = 0
        d = datetime.now().date()
        for r in rows:
            if datetime.strptime(r["date"], "%Y-%m-%d").date() == d:
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        out[habit] = (done, streak)
    c.close()
    return out


# ── undo ──
def push_undo(kind, local_id, chat_id):
    c = db()
    c.execute(
        "INSERT INTO undo_log(kind,local_id,chat_id,created_at) VALUES(?,?,?,?)",
        (kind, local_id, chat_id, int(datetime.now().timestamp())),
    )
    c.commit()
    c.close()


def pop_last_undo(chat_id):
    c = db()
    r = c.execute(
        "SELECT * FROM undo_log WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if r:
        c.execute("DELETE FROM undo_log WHERE id=?", (r["id"],))
        c.commit()
    c.close()
    return dict(r) if r else None


# ═══════════════════════ Notion ═══════════════════════
NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def _title(text):
    return {"title": [{"text": {"content": str(text)[:2000]}}]}


def _select(name):
    return {"select": {"name": name}}


def _checkbox(value):
    return {"checkbox": bool(value)}


def _date(dt):
    if isinstance(dt, datetime):
        return {"date": {"start": dt.isoformat()}}
    return {"date": {"start": str(dt)}}


async def _notion_post(path: str, json_body: dict) -> Optional[dict]:
    if not NOTION_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{NOTION_API}{path}", headers=NOTION_HEADERS, json=json_body)
            if r.status_code >= 400:
                log.error("notion POST %s → %s: %s", path, r.status_code, r.text[:300])
                return None
            return r.json()
    except Exception as e:
        log.error("notion POST %s exception: %s", path, e)
        return None


async def _notion_patch(path: str, json_body: dict) -> bool:
    if not NOTION_ENABLED:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.patch(f"{NOTION_API}{path}", headers=NOTION_HEADERS, json=json_body)
            if r.status_code >= 400:
                log.error("notion PATCH %s → %s: %s", path, r.status_code, r.text[:300])
                return False
            return True
    except Exception as e:
        log.error("notion PATCH %s exception: %s", path, e)
        return False


async def notion_create(db_id: str, properties: dict) -> Optional[str]:
    if not db_id:
        return None
    data = await _notion_post("/pages", {
        "parent": {"database_id": db_id},
        "properties": properties,
    })
    return data.get("id") if data else None


async def notion_set_done(page_id: str, done: bool) -> bool:
    return await _notion_patch(f"/pages/{page_id}", {"properties": {"Done": _checkbox(done)}})


async def notion_archive(page_id: str) -> bool:
    return await _notion_patch(f"/pages/{page_id}", {"archived": True})


async def notion_query_done_pages(db_id: str) -> list:
    if not db_id:
        return []
    data = await _notion_post(f"/databases/{db_id}/query", {
        "filter": {"property": "Done", "checkbox": {"equals": True}},
        "page_size": 100,
    })
    return data.get("results", []) if data else []


PRIORITY_LABELS = {
    "urgent": "🔴 Срочно",
    "important": "🟡 Важно",
    "strategic": "🟢 Стратегия",
}
CATEGORY_LABELS = {
    "business": "💡 Business",
    "marketing": "🎯 Marketing",
}


async def sync_task_to_notion(tid: int, project: str, priority: str, text: str):
    db_id = NOTION_DB_STK if project == "stk" else NOTION_DB_CLOQ
    page_id = await notion_create(db_id, {
        "Name": _title(text),
        "Priority": _select(PRIORITY_LABELS.get(priority, "🔴 Срочно")),
        "Done": _checkbox(False),
        "Created": _date(datetime.now()),
    })
    if page_id:
        save_notion_map("task", tid, page_id)
        log.info("notion ✓ task #%s → %s", tid, page_id[:8])


async def sync_idea_to_notion(iid: int, category: str, text: str):
    if category == "personal":
        db_id = NOTION_DB_PERSONAL
        props = {
            "Name": _title(text),
            "Done": _checkbox(False),
            "Created": _date(datetime.now()),
        }
        local_type = "personal"
    else:
        db_id = NOTION_DB_IDEAS
        props = {
            "Name": _title(text),
            "Category": _select(CATEGORY_LABELS.get(category, "💡 Business")),
            "Created": _date(datetime.now()),
        }
        local_type = "idea"
    page_id = await notion_create(db_id, props)
    if page_id:
        save_notion_map(local_type, iid, page_id)
        log.info("notion ✓ %s #%s → %s", local_type, iid, page_id[:8])


async def sync_habit_to_notion(hid: int, name: str):
    if not hid or not NOTION_DB_HABITS:
        return
    page_id = await notion_create(NOTION_DB_HABITS, {
        "Name": _title(name),
        "Date": _date(datetime.now()),
    })
    if page_id:
        save_notion_map("habit", hid, page_id)
        log.info("notion ✓ habit #%s → %s", hid, page_id[:8])


async def sync_done_to_notion(local_type: str, local_id: int):
    page_id = get_notion_page(local_type, local_id)
    if page_id:
        await notion_set_done(page_id, True)


async def sync_archive_to_notion(local_type: str, local_id: int):
    page_id = get_notion_page(local_type, local_id)
    if page_id:
        await notion_archive(page_id)


async def pull_completions_from_notion() -> int:
    """Забирает Done=True из Notion и обновляет локальную БД. Возвращает число изменений."""
    if not NOTION_ENABLED:
        return 0
    count = 0
    # Tasks
    for db_id, _ in [(NOTION_DB_STK, "stk"), (NOTION_DB_CLOQ, "cloq")]:
        if not db_id:
            continue
        pages = await notion_query_done_pages(db_id)
        c = db()
        for p in pages:
            pid = p.get("id")
            r = c.execute(
                "SELECT local_id FROM notion_map WHERE local_type='task' AND notion_page_id=?",
                (pid,),
            ).fetchone()
            if r:
                c.execute(
                    "UPDATE tasks SET done=1, done_at=? WHERE id=? AND done=0",
                    (int(datetime.now().timestamp()), r["local_id"]),
                )
                if c.total_changes > 0:
                    count += c.total_changes
        c.commit()
        c.close()
    # Personal
    if NOTION_DB_PERSONAL:
        pages = await notion_query_done_pages(NOTION_DB_PERSONAL)
        c = db()
        for p in pages:
            pid = p.get("id")
            r = c.execute(
                "SELECT local_id FROM notion_map WHERE local_type='personal' AND notion_page_id=?",
                (pid,),
            ).fetchone()
            if r:
                c.execute(
                    "UPDATE ideas SET done=1, done_at=? WHERE id=? AND done=0",
                    (int(datetime.now().timestamp()), r["local_id"]),
                )
                if c.total_changes > 0:
                    count += c.total_changes
        c.commit()
        c.close()
    log.info("notion pull: %s items marked done", count)
    return count


# ═══════════════════════ Voice (Whisper) ═══════════════════════
async def transcribe_voice(file_bytes: bytes) -> Optional[str]:
    """Groq (whisper-large-v3) → OpenAI (whisper-1) fallback."""
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": ("voice.ogg", file_bytes, "audio/ogg")},
                    data={"model": "whisper-large-v3", "language": "ru"},
                )
                if r.status_code < 400:
                    return (r.json().get("text") or "").strip()
                log.warning("groq whisper %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("groq whisper exception: %s", e)
    if OPENAI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    files={"file": ("voice.ogg", file_bytes, "audio/ogg")},
                    data={"model": "whisper-1", "language": "ru"},
                )
                if r.status_code < 400:
                    return (r.json().get("text") or "").strip()
                log.warning("openai whisper %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("openai whisper exception: %s", e)
    return None


# ═══════════════════════ LLM Classifier ═══════════════════════
LLM_SYSTEM_PROMPT = """Ты — классификатор сообщений для продуктового бота Абылая (владелец STK парфюмерии в Алматы и проекта CLOQ — часы).

Верни ТОЛЬКО валидный JSON (без ```, без комментариев, без объяснений):
{
  "type": "task_stk" | "task_cloq" | "idea_business" | "idea_marketing" | "personal" | "habit" | "question",
  "priority": "urgent" | "important" | "strategic",
  "habit_name": "тренировка" | "чтение" | "вода" | "подъём",
  "text": "очищенный текст"
}

Правила:
- "task_stk" — действие по парфюмерному бизнесу (менеджеры, склад, сайт, Kaspi, реклама STK)
- "task_cloq" — действие по часовому бизнесу
- "idea_business" — мысль/гипотеза для бизнеса (не действие)
- "idea_marketing" — креативная / рекламная идея
- "personal" — личные дела (купить, забрать, к врачу, встреча)
- "habit" — отметка выполненной привычки (явно: "сделал", "прошёл", "выпил", "прочитал")
- "question" — вопрос, требующий ответа

Приоритет для task_*: urgent (горит сегодня-завтра), important (важно на неделе), strategic (долгосрочное).

Примеры:
"закупить флаконы 50мл" → {"type":"task_stk","priority":"urgent","text":"закупить флаконы 50мл"}
"обновить дизайн для CLOQ" → {"type":"task_cloq","priority":"important","text":"обновить дизайн"}
"может запустим подкаст про парфюм" → {"type":"idea_business","text":"запустить подкаст про парфюм"}
"крутая идея — до/после аромат в рекламе" → {"type":"idea_marketing","text":"реклама до/после по аромату"}
"купить молоко по пути" → {"type":"personal","text":"купить молоко"}
"сходил на тренировку" → {"type":"habit","habit_name":"тренировка","text":""}
"какая маржа на 50мл" → {"type":"question","text":"какая маржа на 50мл"}"""


async def llm_classify(text: str) -> Optional[dict]:
    if not LLM_CLASSIFIER_ENABLED:
        return None
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=LLM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        log.info("LLM classify → %s", data.get("type"))
        return data
    except Exception as e:
        log.warning("llm_classify error: %s", e)
        return None


# ═══════════════════════ AI Q&A ═══════════════════════
async def ask_claude(question: str) -> str:
    if not ANTHROPIC_KEY:
        return "AI выключен (ANTHROPIC_API_KEY не задан)"
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=(
                "Ты — персональный AI-ассистент внутри Telegram-бота Абылая. "
                "Абылай — предприниматель из Алматы, владеет STK (парфюмерия) и CLOQ (часы). "
                "Ты помогаешь ему думать и отвечаешь на его личные вопросы — обсудить идею, "
                "обдумать решение, дать совет по бизнесу, маркетингу, менеджменту. "
                "\n\n"
                "ВАЖНО: Сам бот ведёт учёт задач, идей и привычек в отдельной базе (SQLite + Notion). "
                "Ты НЕ имеешь прямого доступа к этой базе. Если Абылай просит показать/отправить/вывести "
                "существующие задачи, список дел, что сделано и т.п. — коротко ответь: "
                "\"Для списка задач напиши боту команду «все» (полный список с чекбоксами) или «задачи» (утренний дайджест).\" "
                "Не придумывай задачи и не пиши про ограничения доступа — это просто команды бота.\n\n"
                "Фоновый контекст (используй ТОЛЬКО если прямо спрашивают про цены STK): "
                "STK 30мл=42 580 тг, 50мл=63 700 тг, Kaspi рассрочка=3 548 тг/мес. "
                "137 менеджеров в 8 отделах. Розыгрыш Changan X5 — 15 мая.\n\n"
                "Отвечай кратко (2-4 предложения), по делу, как умный партнёр. Обращайся на «ты». "
                "Без лишних смайлов и без воды."
            ),
            messages=[{"role": "user", "content": question}],
        )
        return msg.content[0].text
    except Exception as e:
        log.error("claude error: %s", e)
        return f"⚠️ Ошибка AI: {e}"


# ═══════════════════════ Weather ═══════════════════════
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


# ═══════════════════════ Business Terms ═══════════════════════
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
     "tip": "Подними AOV upsell'ом — легче чем привлечь нового."},
]


# ═══════════════════════ Regex classifier ═══════════════════════
TASK_PREFIX = re.compile(r"^(задача|таск|todo|сделать)[:\s]+(.+)", re.IGNORECASE)
IMPORTANT_PREFIX = re.compile(r"^важно[:\s]+(.+)", re.IGNORECASE)
STRATEGIC_PREFIX = re.compile(r"^(стратег\w*|цель)[:\s]+(.+)", re.IGNORECASE)
IDEA_PREFIX = re.compile(r"^идея[:\s]+(.+)", re.IGNORECASE)
MARKETING_PREFIX = re.compile(r"^(маркетинг|реклама|креатив)[:\s]+(.+)", re.IGNORECASE)
CLOQ_PREFIX = re.compile(r"^(cloq|клок)[:\s]+(.+)", re.IGNORECASE)
PERSONAL_PREFIX = re.compile(r"^(личн\w+|personal)[:\s]+(.+)", re.IGNORECASE)
PERSONAL_WORDS = ["купить", "купи ", "встреча", "посылк", "ремонт", "забрать",
                  "врач", "стрижк", "спортзал", "магазин"]
HABIT_WORDS = {
    "тренировка": ["тренировк", "зал ", "спорт", "gym", "жаттығу"],
    "чтение":     ["чтени", "книг", "read", "оқу"],
    "вода":       ["вода 2", "вода ✅", "water", "су 2"],
    "подъём":     ["подъём", "ранний подъ", "ерте тұрдым"],
}


def parse_time(text: str):
    now = datetime.now()
    tomorrow = now + timedelta(days=1)
    early = any(w in text.lower() for w in ["тренировк", "спорт", "зал ", "gym"])

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
        dt = now + (timedelta(hours=amount) if "час" in m.group(2).lower() else timedelta(minutes=amount))
        clean = re.sub(r"через\s+\d+\s*(мин\w*|час\w*)\s*", "", text, flags=re.IGNORECASE).strip()
        return dt, clean or text, early

    if text.lower().startswith("напомни"):
        rest = re.sub(r"^напомни\w*[:\s]*", "", text, flags=re.IGNORECASE).strip()
        sub = parse_time(rest)
        if sub:
            return sub
        return now + timedelta(hours=1), rest or text, False

    return None


def classify_regex(text: str):
    t = text.strip()
    low = t.lower()

    if "✅" in t or low.endswith(" ок") or low.endswith(" done"):
        clean = t.replace("✅", "").strip().rstrip(" ок").rstrip(" done")
        for habit, kws in HABIT_WORDS.items():
            if any(w in clean.lower() for w in kws):
                return {"type": "habit", "name": habit}

    parsed = parse_time(t)
    if parsed:
        dt, clean, early = parsed
        return {"type": "reminder", "text": clean, "dt": dt, "early": early}

    m = CLOQ_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "cloq", "priority": "urgent", "text": m.group(2).strip()}
    m = MARKETING_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "marketing", "text": m.group(2).strip()}
    m = TASK_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "priority": "urgent", "text": m.group(2).strip()}
    m = IMPORTANT_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "priority": "important", "text": m.group(1).strip()}
    m = STRATEGIC_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "priority": "strategic", "text": m.group(2).strip()}
    m = IDEA_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "business", "text": m.group(1).strip()}
    m = PERSONAL_PREFIX.match(t)
    if m:
        return {"type": "personal", "text": m.group(2).strip()}

    if any(w in low for w in PERSONAL_WORDS):
        return {"type": "personal", "text": t}

    # Intent-распознавание команд списка задач — работает и с '?' префиксом
    # Ловим: "отправь задачи", "покажи все задачи", "?покажи задачи", "список дел", "что у меня", ...
    low_stripped = re.sub(r"^[?\s]+", "", low).strip()
    SHOW_VERBS = ("отправь", "покажи", "покаж", "вывед", "выведи", "дай", "скинь", "список", "перечисли")
    TASK_NOUNS = ("задач", "дел", "тудушк", "тасков", "тасок", "списке")
    if any(v in low_stripped for v in SHOW_VERBS) and any(n in low_stripped for n in TASK_NOUNS):
        # "все" в фразе → полный список, иначе утренний дайджест
        if "все" in low_stripped or "всё" in low_stripped or "полн" in low_stripped:
            return {"type": "cmd_all"}
        return {"type": "cmd_digest"}
    # Короткие запросы «что у меня», «что там», «статус» и т.п.
    if low_stripped in ("что у меня", "что там", "как дела по задачам", "что по задачам"):
        return {"type": "cmd_digest"}

    if low in ("задачи", "tasks", "статус", "план"):
        return {"type": "cmd_digest"}
    if low in ("все", "всё", "все задачи", "всё задачи", "all", "полный список"):
        return {"type": "cmd_all"}
    if low in ("привычки", "habits", "трекер"):
        return {"type": "cmd_habits"}
    if low in ("погода", "weather"):
        return {"type": "cmd_weather"}
    if low in ("синк", "sync", "синхронизация"):
        return {"type": "cmd_sync"}
    if low in ("отмена", "undo", "откат"):
        return {"type": "cmd_undo"}
    if low.startswith(("?", "вопрос:")) and ANTHROPIC_KEY:
        return {"type": "ask", "text": re.sub(r"^[?]\s*|вопрос:\s*", "", t, flags=re.IGNORECASE)}

    return None  # неизвестно — упадёт в LLM


async def classify(text: str) -> dict:
    """Regex → LLM fallback → default idea."""
    res = classify_regex(text)
    if res is not None:
        return res

    llm = await llm_classify(text)
    if llm:
        kind = llm.get("type", "idea_business")
        body = (llm.get("text") or text).strip()
        if kind == "task_stk":
            return {"type": "task", "project": "stk", "priority": llm.get("priority", "urgent"), "text": body}
        if kind == "task_cloq":
            return {"type": "task", "project": "cloq", "priority": llm.get("priority", "urgent"), "text": body}
        if kind == "idea_business":
            return {"type": "idea", "category": "business", "text": body}
        if kind == "idea_marketing":
            return {"type": "idea", "category": "marketing", "text": body}
        if kind == "personal":
            return {"type": "personal", "text": body}
        if kind == "habit":
            hname = llm.get("habit_name", "тренировка")
            if hname not in HABIT_WORDS:
                hname = "тренировка"
            return {"type": "habit", "name": hname}
        if kind == "question":
            return {"type": "ask", "text": body}

    return {"type": "idea", "category": "business", "text": text}


# ═══════════════════════ Reminder scheduling ═══════════════════════
async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    rid = data.get("rid")
    early = data.get("early", False)
    try:
        await context.bot.send_message(chat_id=context.job.chat_id, text=data["text"])
    except Exception as e:
        log.error("reminder send error: %s", e)
    if rid:
        if early:
            mark_reminder_early_fired(rid)
        else:
            mark_reminder_sent(rid)


def schedule_reminder_jobs(job_queue, rid: int, text: str, dt: datetime,
                           chat_id: int, early: bool = False,
                           early_fired_already: bool = False):
    delay = (dt - datetime.now()).total_seconds()
    if delay <= 0:
        return
    if early and delay > 3600 and not early_fired_already:
        job_queue.run_once(
            send_reminder_job, delay - 3600,
            data={"rid": rid, "text": f"⏰ Через 1 час: {text}", "early": True},
            chat_id=chat_id,
        )
    job_queue.run_once(
        send_reminder_job, delay,
        data={"rid": rid, "text": f"🔔 {text}", "early": False},
        chat_id=chat_id,
    )


def rehydrate_reminders(job_queue):
    pending = get_pending_reminders()
    now = datetime.now()
    count = 0
    for r in pending:
        dt = datetime.fromtimestamp(r["remind_at"])
        if dt <= now:
            mark_reminder_sent(r["id"])  # просроченные просто закрываем
            continue
        early = (r["kind"] == "early") or ("тренировк" in r["text"].lower())
        schedule_reminder_jobs(
            job_queue, r["id"], r["text"], dt, r["chat_id"],
            early=early, early_fired_already=bool(r["early_fired"]),
        )
        count += 1
    log.info("rehydrated %s reminders", count)


# ═══════════════════════ Digest ═══════════════════════
def _age_badge(created_at_ts: int) -> str:
    """Маркер возраста: '' для свежей, ' ⏭️' для вчера, ' ⏭️Xд' дальше, ' ⏭️Xд ⚠️' для недели+."""
    days_old = int((datetime.now().timestamp() - int(created_at_ts or 0)) / 86400)
    if days_old >= 7:
        return f" ⏭️{days_old}д ⚠️"
    if days_old >= 2:
        return f" ⏭️{days_old}д"
    if days_old >= 1:
        return " ⏭️"
    return ""


def _buttons_grid(items, cols=2, label_len=18):
    """Раскладывает кнопки в сетку по N колонок. items: [(callback_data, label_text), ...]"""
    rows = []
    row = []
    for cb, label in items:
        row.append(InlineKeyboardButton(f"☑️ {label[:label_len]}", callback_data=cb))
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def build_digest():
    now = datetime.now()
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    msg = f"☀️ *Доброе утро, Абылай!*\n"
    msg += f"📅 {now.strftime('%d.%m.%Y')} · {days[now.weekday()]}\n\n"
    msg += get_weather() + "\n"

    tasks_for_buttons = []  # [(callback_data, display_label)]

    stk_urgent = get_open_tasks(project="stk", priority="urgent", limit=10)
    stk_imp = get_open_tasks(project="stk", priority="important", limit=6)
    stk_strat = get_open_tasks(project="stk", priority="strategic", limit=4)
    if stk_urgent or stk_imp or stk_strat:
        msg += "\n━━━━━ 🌹 *STK* ━━━━━\n"
        if stk_urgent:
            msg += f"\n🔴 *Срочно ({len(stk_urgent)}):*\n"
            for t in stk_urgent:
                age = _age_badge(t["created_at"])
                msg += f"   ☐ {t['text'][:45]}{age}\n"
                tasks_for_buttons.append((f"done:task:{t['id']}", t["text"]))
        if stk_imp:
            msg += f"\n🟡 *Важно ({len(stk_imp)}):*\n"
            for t in stk_imp:
                age = _age_badge(t["created_at"])
                msg += f"   ☐ {t['text'][:45]}{age}\n"
                tasks_for_buttons.append((f"done:task:{t['id']}", t["text"]))
        if stk_strat:
            msg += f"\n🟢 *Стратегия ({len(stk_strat)}):*\n"
            for t in stk_strat:
                age = _age_badge(t["created_at"])
                msg += f"   ☐ {t['text'][:45]}{age}\n"
                tasks_for_buttons.append((f"done:task:{t['id']}", t["text"]))

    cloq_urgent = get_open_tasks(project="cloq", priority="urgent", limit=5)
    cloq_imp = get_open_tasks(project="cloq", priority="important", limit=5)
    if cloq_urgent or cloq_imp:
        msg += "\n━━━━━ ⌚ *CLOQ* ━━━━━\n"
        for t in cloq_urgent + cloq_imp:
            mark = "🔴" if t["priority"] == "urgent" else "🟡"
            age = _age_badge(t["created_at"])
            msg += f"   ☐ {mark} {t['text'][:45]}{age}\n"
            tasks_for_buttons.append((f"done:task:{t['id']}", t["text"]))

    c = db()
    personal = c.execute(
        "SELECT id, text, created_at FROM ideas WHERE category='personal' AND done=0 ORDER BY id DESC LIMIT 8"
    ).fetchall()
    c.close()
    if personal:
        msg += "\n━━━━━ 🏃 *ЛИЧНОЕ* ━━━━━\n"
        for r in personal:
            age = _age_badge(r["created_at"])
            msg += f"   ☐ {r['text'][:45]}{age}\n"
            tasks_for_buttons.append((f"done:personal:{r['id']}", r["text"]))

    msg += "\n━━━━━ 🔥 *ПРИВЫЧКИ* ━━━━━\n"
    icons = {"тренировка": "🏋", "чтение": "📖", "вода": "💧", "подъём": "🌅"}
    for name, (done, streak) in get_habits_today().items():
        fire = "🔥" * min(streak, 5) if streak >= 2 else ""
        msg += f"   {icons[name]} {'✅' if done else '☐'} {name} · {streak} дн {fire}\n"

    day_idx = now.timetuple().tm_yday % len(BUSINESS_TERMS)
    term = BUSINESS_TERMS[day_idx]
    msg += "\n━━━━━ 🇬🇧 *БИЗНЕС-ТЕРМИН* ━━━━━\n"
    msg += f"\n*{term['term']}* {term['tr']}\n"
    msg += f"📖 _{term['ru']}_\n\n"
    msg += f"💬 {term['ex']}\n\n"
    msg += f"💡 {term['tip']}"

    # Кнопка на каждой задаче (2 колонки, до 20 штук)
    visible = tasks_for_buttons[:20]
    if len(tasks_for_buttons) > 20:
        msg += f"\n\n_…и ещё {len(tasks_for_buttons) - 20}. Напиши «все» для полного списка._"
    kb_rows = _buttons_grid(visible, cols=2, label_len=18)
    kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None
    return msg, kb


def build_full_open_list():
    """Все открытые задачи + личное, с чекбоксом на каждой. Возвращает [(text, kb), ...]."""
    all_items = []  # [(kind, id, label, text, created_at)]

    for proj, proj_icon in [("stk", "🌹"), ("cloq", "⌚")]:
        for pri, pri_icon in [("urgent", "🔴"), ("important", "🟡"), ("strategic", "🟢")]:
            tasks = get_open_tasks(project=proj, priority=pri, limit=50)
            for t in tasks:
                all_items.append(("task", t["id"], f"{proj_icon}{pri_icon}", t["text"], t["created_at"]))

    c = db()
    personal = c.execute(
        "SELECT id, text, created_at FROM ideas WHERE category='personal' AND done=0 ORDER BY id DESC"
    ).fetchall()
    c.close()
    for r in personal:
        all_items.append(("personal", r["id"], "🏃", r["text"], r["created_at"]))

    if not all_items:
        return [("📋 Открытых задач нет — красавчик!", None)]

    # Разбиваем на чанки по 8 (чтобы и текст и кнопки помещались)
    chunks = []
    chunk_size = 8
    total = len(all_items)
    for i in range(0, total, chunk_size):
        part = all_items[i:i + chunk_size]
        text = f"📋 *Открытые задачи* ({i + 1}-{i + len(part)} из {total}):\n\n"
        buttons_data = []
        for kind, iid, label, tt, ca in part:
            age = _age_badge(ca)
            text += f"☐ {label} {tt[:45]}{age}\n"
            buttons_data.append((f"done:{kind}:{iid}", tt))
        kb = InlineKeyboardMarkup(_buttons_grid(buttons_data, cols=1, label_len=35))
        chunks.append((text, kb))
    return chunks


def build_weekly_report():
    now = datetime.now()
    week_ago = int((now - timedelta(days=7)).timestamp())
    c = db()
    closed_stk = c.execute(
        "SELECT COUNT(*) as n FROM tasks WHERE project='stk' AND done=1 AND done_at>=?", (week_ago,)
    ).fetchone()["n"]
    closed_cloq = c.execute(
        "SELECT COUNT(*) as n FROM tasks WHERE project='cloq' AND done=1 AND done_at>=?", (week_ago,)
    ).fetchone()["n"]
    new_ideas = c.execute(
        "SELECT COUNT(*) as n FROM ideas WHERE category IN ('business','marketing') AND created_at>=?",
        (week_ago,),
    ).fetchone()["n"]
    open_total = c.execute("SELECT COUNT(*) as n FROM tasks WHERE done=0").fetchone()["n"]
    c.close()

    msg = "📊 *Еженедельный отчёт*\n"
    msg += f"_{(now - timedelta(days=7)).strftime('%d.%m')} — {now.strftime('%d.%m')}_\n\n"
    msg += f"🌹 STK закрыто: *{closed_stk}*\n"
    msg += f"⌚ CLOQ закрыто: *{closed_cloq}*\n"
    msg += f"💡 Новых идей: *{new_ideas}*\n"
    msg += f"📋 Открыто всего: *{open_total}*\n\n"
    msg += "🔥 *Привычки (streak):*\n"
    icons = {"тренировка": "🏋", "чтение": "📖", "вода": "💧", "подъём": "🌅"}
    for name, (_, streak) in get_habits_today().items():
        fire = "🔥" * min(streak, 5) if streak >= 2 else ""
        msg += f"   {icons[name]} {name}: *{streak}* дн {fire}\n"
    return msg


# ═══════════════════════ Core processing ═══════════════════════
async def process_text(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state("last_chat_id", update.effective_chat.id)
    log.info("MSG: %s", text[:100])
    res = await classify(text)
    log.info("CLS: %s", res.get("type"))
    t = res["type"]
    chat_id = update.effective_chat.id

    async def reply(*a, **kw):
        return await update.effective_message.reply_text(*a, **kw)

    if t == "cmd_digest":
        msg, kb = build_digest()
        await reply(msg, parse_mode="Markdown", reply_markup=kb)

    elif t == "cmd_all":
        chunks = build_full_open_list()
        for text, kb in chunks:
            await reply(text, parse_mode="Markdown", reply_markup=kb)

    elif t == "cmd_habits":
        msg = "🔥 *Привычки сегодня:*\n\n"
        icons = {"тренировка": "🏋", "чтение": "📖", "вода": "💧", "подъём": "🌅"}
        for name, (done, streak) in get_habits_today().items():
            fire = "🔥" * min(streak, 5) if streak >= 2 else ""
            msg += f"{icons[name]} {'✅' if done else '☐'} {name} · {streak} дн {fire}\n"
        await reply(msg, parse_mode="Markdown")

    elif t == "cmd_weather":
        await reply(get_weather())

    elif t == "cmd_sync":
        if not NOTION_ENABLED:
            await reply("⚠️ Notion не настроен")
            return
        await update.effective_chat.send_action("typing")
        count = await pull_completions_from_notion()
        await reply(f"✅ Из Notion подтянуто: *{count}* выполненных записей", parse_mode="Markdown")

    elif t == "cmd_undo":
        last = pop_last_undo(chat_id)
        if not last:
            await reply("⚠️ Нечего отменять")
            return
        kind, lid = last["kind"], last["local_id"]
        if kind == "task":
            delete_task_local(lid)
            if NOTION_ENABLED:
                asyncio.create_task(sync_archive_to_notion("task", lid))
            await reply(f"↩️ Задача #{lid} отменена")
        else:
            delete_idea_local(lid)
            if NOTION_ENABLED:
                nt = "personal" if kind == "personal" else "idea"
                asyncio.create_task(sync_archive_to_notion(nt, lid))
            await reply(f"↩️ Запись #{lid} отменена")

    elif t == "habit":
        streak, hid = mark_habit(res["name"])
        if streak == 0:
            await reply(f"✅ {res['name']} — уже отмечено сегодня!")
        else:
            if NOTION_ENABLED and NOTION_DB_HABITS:
                asyncio.create_task(sync_habit_to_notion(hid, res["name"]))
            fire = "🔥" * min(streak, 5)
            extra = ""
            if streak >= 7:
                extra = "\n\n🎉 *НЕДЕЛЯ ПОДРЯД!*"
            elif streak >= 3:
                extra = "\n\n💪 Так держать!"
            await reply(
                f"✅ *{res['name']}* отмечено!\n\n📊 Серия: *{streak} дней* {fire}{extra}",
                parse_mode="Markdown",
            )

    elif t == "task":
        tid = add_task(res["project"], res["priority"], res["text"])
        push_undo("task", tid, chat_id)
        if NOTION_ENABLED:
            asyncio.create_task(sync_task_to_notion(tid, res["project"], res["priority"], res["text"]))
        icons = {"urgent": "🔴", "important": "🟡", "strategic": "🟢"}
        proj = "STK" if res["project"] == "stk" else "CLOQ"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Готово", callback_data=f"done:task:{tid}"),
            InlineKeyboardButton("❌ Удалить", callback_data=f"del:task:{tid}"),
        ]])
        await reply(
            f"{icons.get(res['priority'], '🔴')} *{proj} — Задача:*\n\n☐ {res['text']}",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif t == "idea":
        iid = add_idea(res["category"], res["text"])
        push_undo("idea", iid, chat_id)
        if NOTION_ENABLED:
            asyncio.create_task(sync_idea_to_notion(iid, res["category"], res["text"]))
        labels = {"business": "💡 Бизнес-идея", "marketing": "🎯 Маркетинг"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Удалить", callback_data=f"del:idea:{iid}")]])
        await reply(
            f"{labels.get(res['category'], '💡')}:\n\n☐ {res['text']}",
            reply_markup=kb,
        )

    elif t == "personal":
        iid = add_idea("personal", res["text"])
        push_undo("personal", iid, chat_id)
        if NOTION_ENABLED:
            asyncio.create_task(sync_idea_to_notion(iid, "personal", res["text"]))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Готово", callback_data=f"done:personal:{iid}"),
            InlineKeyboardButton("❌ Удалить", callback_data=f"del:personal:{iid}"),
        ]])
        await reply(
            f"🏃 *Личное:*\n\n☐ {res['text']}",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif t == "reminder":
        dt = res["dt"]
        delay = (dt - datetime.now()).total_seconds()
        if delay <= 0:
            await reply("⚠️ Время прошло")
            return
        early = res.get("early", False)
        rid = add_reminder_db(res["text"], dt, chat_id, kind=("early" if early else "general"))
        schedule_reminder_jobs(context.job_queue, rid, res["text"], dt, chat_id, early=early)
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
        if early:
            msg += "\n⏰ + напомню за 1 час"
        await reply(msg, parse_mode="Markdown", reply_markup=kb)

    elif t == "ask":
        await update.effective_chat.send_action("typing")
        answer = await ask_claude(res["text"])
        await reply(f"💬 {answer}")


# ═══════════════════════ Telegram handlers ═══════════════════════
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID and update.effective_user.id != OWNER_ID:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await process_text(text, update, context)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID and update.effective_user.id != OWNER_ID:
        return
    if not VOICE_ENABLED:
        await update.message.reply_text("🎤 Голосовые выключены (нет GROQ_API_KEY или OPENAI_API_KEY)")
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    await update.effective_chat.send_action("typing")
    tmp_path = None
    try:
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as f:
            file_bytes = f.read()
        text = await transcribe_voice(file_bytes)
        if not text:
            await update.message.reply_text("⚠️ Не удалось распознать голосовое")
            return
        await update.message.reply_text(f"🎤 _{text}_", parse_mode="Markdown")
        await process_text(text, update, context)
    except Exception as e:
        log.error("voice handler: %s", e)
        await update.message.reply_text(f"⚠️ Ошибка голосового: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def _edit_done(q):
    old = q.message.reply_markup
    rows = []
    if old:
        for row in old.inline_keyboard:
            new_row = [b for b in row if b.callback_data != q.data]
            if new_row:
                rows.append(new_row)
    try:
        new_text = (q.message.text or "") + "\n\n✅ Выполнено!"
        if rows:
            await q.edit_message_text(text=new_text, reply_markup=InlineKeyboardMarkup(rows))
        else:
            await q.edit_message_text(text=new_text)
    except Exception:
        pass


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if ":" not in (q.data or ""):
        return
    parts = q.data.split(":", 2)
    if len(parts) != 3:
        return
    action, kind, sid = parts
    try:
        lid = int(sid)
    except ValueError:
        return

    if action == "done":
        if kind == "task":
            ok = complete_task_local(lid)
            if ok and NOTION_ENABLED:
                asyncio.create_task(sync_done_to_notion("task", lid))
            if ok:
                await _edit_done(q)
            else:
                await q.answer("⚠️ Уже выполнена или не найдена")
        elif kind == "personal":
            ok = complete_idea_local(lid)
            if ok and NOTION_ENABLED:
                asyncio.create_task(sync_done_to_notion("personal", lid))
            if ok:
                await _edit_done(q)
            else:
                await q.answer("⚠️ Уже выполнено")
    elif action == "del":
        if kind == "task":
            delete_task_local(lid)
            if NOTION_ENABLED:
                asyncio.create_task(sync_archive_to_notion("task", lid))
        else:
            delete_idea_local(lid)
            if NOTION_ENABLED:
                nt = "personal" if kind == "personal" else "idea"
                asyncio.create_task(sync_archive_to_notion(nt, lid))
        try:
            await q.edit_message_text((q.message.text or "") + "\n\n❌ Удалено")
        except Exception:
            pass


# ═══════════════════════ Commands ═══════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *STK Bot v2 — Notion edition*\n\n"
        "Пиши задачи, идеи, напоминания. Голосовые 🎤 тоже работают.\n\n"
        "*Быстрые команды:*\n"
        "• `задачи` — утренний дайджест\n"
        "• `все` — полный список с чекбоксами ☑️\n"
        "• `привычки` · `погода`\n"
        "• `синк` — подтянуть изменения из Notion\n"
        "• `отмена` — откатить последнее добавление\n\n"
        "*Примеры ввода:*\n"
        "• `задача: исправить цену` → 🔴 STK\n"
        "• `важно: добавить товары` → 🟡 STK\n"
        "• `стратегия: выход на Узбекистан` → 🟢 STK\n"
        "• `cloq: новый дизайн` → ⌚ CLOQ\n"
        "• `маркетинг: до/после креатив` → 🎯\n"
        "• `личное: забрать костюм`\n"
        "• `тренировка завтра в 7` → ⏰ + за час\n"
        "• `тренировка ✅` → 🔥 streak\n"
        "• `?какая маржа на 50мл` → 💬 AI",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID and update.effective_user.id != OWNER_ID:
        return
    if not NOTION_ENABLED:
        await update.message.reply_text("⚠️ Notion не настроен")
        return
    await update.effective_chat.send_action("typing")
    count = await pull_completions_from_notion()
    await update.message.reply_text(f"✅ Из Notion подтянуто: *{count}* выполненных записей", parse_mode="Markdown")


# ═══════════════════════ Jobs ═══════════════════════
async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    if NOTION_ENABLED:
        try:
            await pull_completions_from_notion()
        except Exception as e:
            log.error("morning sync error: %s", e)
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


async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    msg = build_weekly_report()
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    log.info("WEEKLY REPORT sent")


async def evening_rollover_job(context: ContextTypes.DEFAULT_TYPE):
    """Вечером (22:00) — напомнить о незакрытых задачах дня + переносах."""
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        return
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    c = db()
    created_today = c.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE done=0 AND created_at>=?",
        (today_start,),
    ).fetchone()["n"]
    old_open = c.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE done=0 AND created_at<?",
        (today_start,),
    ).fetchone()["n"]
    closed_today = c.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE done=1 AND done_at>=?",
        (today_start,),
    ).fetchone()["n"]
    c.close()

    if created_today == 0 and old_open == 0 and closed_today == 0:
        return  # день без активности — не беспокоим

    msg = "🌙 *Вечерний итог*\n\n"
    if closed_today:
        msg += f"✅ Закрыто сегодня: *{closed_today}*\n"
    if created_today:
        msg += f"📝 Открыто из сегодняшних: *{created_today}*\n"
    if old_open:
        msg += f"⏭️ Переносится с прошлых дней: *{old_open}*\n"
    msg += "\n_Утром всё снова будет в дайджесте._\n"
    msg += "Напиши `все` — покажу полный список с чекбоксами."
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        log.info("EVENING ROLLOVER sent (closed=%s new=%s carry=%s)", closed_today, created_today, old_open)
    except Exception as e:
        log.error("evening rollover error: %s", e)


# ═══════════════════════ Main ═══════════════════════
async def run():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_daily(
        morning_job, time=dtime(hour=5, minute=0, second=0), name="morning_digest",
    )
    app.job_queue.run_daily(
        evening_rollover_job, time=dtime(hour=22, minute=0, second=0), name="evening_rollover",
    )
    app.job_queue.run_daily(
        weekly_report_job, time=dtime(hour=21, minute=0, second=0),
        days=(6,), name="weekly_report",
    )

    log.info("🤖 STK Bot v2 starting")
    log.info("   OWNER=%s  NOTION=%s  VOICE=%s  LLM_CLS=%s  CITY=%s",
             OWNER_ID,
             "ON" if NOTION_ENABLED else "OFF",
             "ON" if VOICE_ENABLED else "OFF",
             "ON" if LLM_CLASSIFIER_ENABLED else "OFF",
             WEATHER_CITY)

    await app.initialize()
    await app.start()
    rehydrate_reminders(app.job_queue)
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
