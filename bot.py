"""
STK Bot v4.0 — упрощённая облачная версия.

Изменения от v3.2:
- Notion полностью удалён, всё хранится только в SQLite + Telegram
- Все time-based уведомления (11/15/19/21/23/00) удалены
- Парсер времени и напоминания удалены
- STK без разделения urgent/important — один STK
- Дефолт классификации = STK задача (без префикса)
- Один утренний пуш в 10:00 по Алматы:
    карточка дня (pinned) → погода → задачи

Сохранено: карточка дня с 7 чекбоксами, голосовые, /today, /progress,
/tasks, /weather, streak'и, ?AI-вопрос.
"""
import os
import re
import io
import asyncio
import sqlite3
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, time as dtime, date as ddate
from zoneinfo import ZoneInfo

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
ALMATY_TZ = ZoneInfo("Asia/Almaty")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
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
            notion_id TEXT,
            created_at INTEGER NOT NULL,
            done_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'business',
            text TEXT NOT NULL,
            notion_id TEXT,
            done INTEGER DEFAULT 0,
            done_at INTEGER,
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
    # Безопасные миграции (если БД от v3.x)
    for col_def in ("done INTEGER DEFAULT 0", "done_at INTEGER"):
        try:
            c.execute(f"ALTER TABLE ideas ADD COLUMN {col_def}")
        except Exception:
            pass
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


# ─────────── Markdown escape ───────────
def md_escape(text: str) -> str:
    if not text:
        return ""
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


# ─────────── Anthropic API (только для ?вопрос) ───────────
def get_anthropic_client():
    if not ANTHROPIC_KEY:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_KEY)
    except Exception as e:
        log.error("anthropic init: %s", e)
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


# ─────────── Voice transcription (Whisper) ───────────
async def transcribe_voice(audio_bytes: bytes) -> str | None:
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


# ─────────── Классификация ───────────
# Префиксы — для НЕ-STK случаев. Без префикса → STK задача.
CLOQ_PREFIX = re.compile(r"^(cloq|клок)[:\s]+(.+)", re.IGNORECASE)
MARKETING_PREFIX = re.compile(r"^(маркетинг|реклама|креатив)[:\s]+(.+)", re.IGNORECASE)
PERSONAL_PREFIX = re.compile(r"^(личное|личн)[:\s]+(.+)", re.IGNORECASE)
IDEA_PREFIX = re.compile(r"^идея[:\s]+(.+)", re.IGNORECASE)
TASK_PREFIX = re.compile(
    r"^(задача|таск|todo|сделать|важно|срочно)[:\s]+(.+)", re.IGNORECASE
)

GYM_WORDS = ["тренировк", "спорт", "зал ", "зала", "залу", "gym", "жаттығу", "качалк"]


def classify(text: str) -> dict:
    t = text.strip()
    low = t.lower()

    # 1. «Сходил в зал ✅» → отметка в карточке дня
    if any(w in low for w in GYM_WORDS) and (
        "✅" in t or "сходил" in low or "была" in low or "был" in low
    ):
        return {"type": "gym_done"}

    # 2. CLOQ
    m = CLOQ_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "cloq", "text": m.group(2).strip()}

    # 3. Маркетинг → идея
    m = MARKETING_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "marketing", "text": m.group(2).strip()}

    # 4. Личное → идея
    m = PERSONAL_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "personal", "text": m.group(2).strip()}

    # 5. Идея бизнес
    m = IDEA_PREFIX.match(t)
    if m:
        return {"type": "idea", "category": "business", "text": m.group(1).strip()}

    # 6. Любые «задача:/важно:/срочно:/таск:» → STK задача (без приоритетов)
    m = TASK_PREFIX.match(t)
    if m:
        return {"type": "task", "project": "stk", "text": m.group(2).strip()}

    # 7. Команды
    cmd_map = {
        ("задачи", "tasks", "статус", "план", "сегодня", "карточка"): "cmd_today",
        ("прогресс", "progress", "стрик"): "cmd_progress",
        ("погода", "weather"): "cmd_weather",
    }
    for keys, ct in cmd_map.items():
        if low in keys:
            return {"type": ct}

    # 8. AI-вопрос
    if low.startswith(("?", "вопрос:")) and ANTHROPIC_KEY:
        return {
            "type": "ask",
            "text": re.sub(r"^[?]\s*|вопрос:\s*", "", t, flags=re.IGNORECASE),
        }

    # 9. Дефолт — STK задача
    return {"type": "task", "project": "stk", "text": t}


# ─────────── Бизнес-логика ───────────
def add_task(project, text):
    c = db()
    cur = c.execute(
        "INSERT INTO tasks(project,priority,text,created_at) VALUES(?,?,?,?)",
        (project, "normal", text, int(datetime.now().timestamp())),
    )
    c.commit()
    tid = cur.lastrowid
    c.close()
    return tid


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


def complete_task(tid):
    c = db()
    row = c.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()
    if not row:
        c.close()
        return False
    if not row["done"]:
        c.execute(
            "UPDATE tasks SET done=1, done_at=? WHERE id=?",
            (int(datetime.now().timestamp()), tid),
        )
        c.commit()
    c.close()
    return True


def complete_idea(iid):
    c = db()
    row = c.execute("SELECT done FROM ideas WHERE id=?", (iid,)).fetchone()
    if not row:
        c.close()
        return False
    if not row["done"]:
        c.execute(
            "UPDATE ideas SET done=1, done_at=? WHERE id=?",
            (int(datetime.now().timestamp()), iid),
        )
        c.commit()
    c.close()
    return True


def get_open_tasks(project=None, limit=20):
    c = db()
    q = "SELECT * FROM tasks WHERE done=0"
    args = []
    if project:
        q += " AND project=?"
        args.append(project)
    q += " ORDER BY id ASC LIMIT ?"
    args.append(limit)
    rows = c.execute(q, args).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_personal_ideas(limit=15):
    c = db()
    rows = c.execute(
        "SELECT id, text FROM ideas WHERE category='personal' AND done=0 "
        "ORDER BY id DESC LIMIT ?",
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


def today_almaty() -> ddate:
    return datetime.now(ALMATY_TZ).date()


def get_daily_row(d: ddate = None) -> dict:
    if d is None:
        d = today_almaty()
    c = db()
    r = c.execute("SELECT * FROM daily WHERE date=?", (d.isoformat(),)).fetchone()
    if not r:
        c.execute(
            "INSERT INTO daily(date, updated_at) VALUES(?,?)",
            (d.isoformat(), int(datetime.now().timestamp())),
        )
        c.commit()
        r = c.execute("SELECT * FROM daily WHERE date=?", (d.isoformat(),)).fetchone()
    c.close()
    return dict(r)


def toggle_daily_field(field: str, d: ddate = None) -> dict:
    if d is None:
        d = today_almaty()
    get_daily_row(d)
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
        d = today_almaty()
    row = get_daily_row(d)
    days = ["Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье"]
    score = daily_score(row)

    filled = "█" * score
    empty = "░" * (len(DAILY_FIELDS) - score)
    bar = filled + empty

    msg = f"📅 *{d.strftime('%d.%m')}* · {days[d.weekday()]}\n"
    msg += f"`{bar}` *{score}/{len(DAILY_FIELDS)}*\n\n"

    for key, ico, name in DAILY_FIELDS:
        check = "✅" if row.get(key) else "☐"
        msg += f"{check} {ico} {name}\n"

    kb = []
    for i in range(0, len(DAILY_FIELDS), 2):
        row_btns = []
        for j in range(2):
            if i + j >= len(DAILY_FIELDS):
                break
            key, ico, name = DAILY_FIELDS[i + j]
            check = "✅" if row.get(key) else "☐"
            row_btns.append(
                InlineKeyboardButton(
                    f"{check} {ico} {name[:14]}",
                    callback_data=f"daily:{key}",
                )
            )
        kb.append(row_btns)

    return msg, InlineKeyboardMarkup(kb)


# ─────────── Streak'и ───────────
def calc_streak(field: str) -> int:
    c = db()
    rows = c.execute(
        f"SELECT date, {field} FROM daily ORDER BY date DESC LIMIT 365"
    ).fetchall()
    c.close()
    streak = 0
    expected = today_almaty()
    for r in rows:
        rd = ddate.fromisoformat(r["date"])
        if rd > expected:
            continue
        if rd != expected:
            break
        if not r[field]:
            break
        streak += 1
        expected = expected - timedelta(days=1)
    return streak


def calc_gym_week_count() -> int:
    today = today_almaty()
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
        "👋 *STK Bot v4.0*\n\n"
        "Просто пиши задачи — *по умолчанию это STK*.\n"
        "Никаких напоминаний, всё в Telegram.\n\n"
        "*Префиксы (опционально):*\n"
        "• `cloq: купить часы` → ⌚ CLOQ\n"
        "• `маркетинг: новый креатив` → 🎯 идея\n"
        "• `личное: записаться к врачу` → 🏃 идея\n"
        "• `идея: запустить b2b` → 💡 идея\n\n"
        "*Команды:*\n"
        "📅 `/today` — карточка дня\n"
        "📊 `/progress` — стрики\n"
        "📋 `/tasks` — все задачи\n"
        "🌡 `/weather` — погода\n"
        "💬 `?вопрос` → AI-ответ\n\n"
        "🎤 Голосовые тоже работают.\n\n"
        "⏰ _Каждый день в 10:00 по Алматы — карточка дня + погода + задачи._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, kb = build_daily_card()
    sent = await update.effective_chat.send_message(
        msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        log.warning("pin failed: %s", e)
    today = today_almaty().isoformat()
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

    await update.message.reply_text(
        f"🎤 _распознал:_\n{text}", parse_mode=ParseMode.MARKDOWN
    )
    await process_text(update, context, text)


async def refresh_pinned_daily(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    today = today_almaty().isoformat()
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
        row = toggle_daily_field("gym")
        gym_week = calc_gym_week_count()
        kept = "✅" if row["gym"] else "☐"
        msg = (
            f"{kept} 🏋 Тренажёрка отмечена сегодня\n"
            f"📊 Эта неделя: *{gym_week}*/3+ раз"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        await refresh_pinned_daily(context, chat_id)

    elif t == "task":
        tid = add_task(res["project"], res["text"])
        proj_label = "🌹 *STK*" if res["project"] == "stk" else "⌚ *CLOQ*"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готово", callback_data=f"task_done:{tid}"),
             InlineKeyboardButton("🔄 → CLOQ" if res["project"] == "stk" else "🔄 → STK",
                                  callback_data=f"task_move:{tid}")],
        ])
        await update.message.reply_text(
            f"{proj_label}\n\n☐ {md_escape(res['text'])}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    elif t == "idea":
        iid = add_idea(res["category"], res["text"])
        labels = {
            "business": "💡 Бизнес-идея",
            "marketing": "🎯 Маркетинг",
            "personal": "🏃 Личное",
        }
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готово", callback_data=f"idea_done:{iid}"),
             InlineKeyboardButton("🔄 Переместить", callback_data=f"idea_move:{iid}")],
            [InlineKeyboardButton("❌ Удалить", callback_data=f"idea_del:{iid}")],
        ])
        await update.message.reply_text(
            f"{labels.get(res['category'], '💡')}\n\n☐ {md_escape(res['text'])}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    elif t == "ask":
        await update.message.chat.send_action(ChatAction.TYPING)
        answer = ask_claude(res["text"])
        await update.message.reply_text(f"💬 {answer}")


async def _remove_button_or_finish(q, callback_data: str, mark: str = "✅"):
    try:
        old = q.message.reply_markup
        if not old or not old.inline_keyboard:
            await q.answer(f"{mark} Готово")
            return

        new_rows = []
        for row in old.inline_keyboard:
            new_row = [b for b in row if b.callback_data != callback_data]
            if new_row:
                new_rows.append(new_row)

        if new_rows:
            await q.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
            await q.answer(f"{mark} выполнено")
        else:
            try:
                await q.edit_message_text(
                    text=(q.message.text or "") + f"\n\n{mark} Выполнено!",
                )
            except Exception:
                await q.answer(f"{mark} выполнено")
    except Exception as e:
        log.warning("_remove_button_or_finish: %s", e)
        await q.answer(f"{mark} выполнено")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    # Карточка дня — переключение чекбоксов
    if data.startswith("daily:"):
        field = data.split(":")[1]
        if field not in [f[0] for f in DAILY_FIELDS]:
            return
        toggle_daily_field(field)
        msg, kb = build_daily_card()
        try:
            await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            pass
        return

    # Задача — выполнено
    if data.startswith("task_done:"):
        tid = int(data.split(":")[1])
        complete_task(tid)
        await _remove_button_or_finish(q, data, "✅")
        return

    # Задача — удалить
    if data.startswith("task_del:"):
        tid = int(data.split(":")[1])
        c = db()
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        c.commit()
        c.close()
        try:
            await q.edit_message_text(q.message.text + "\n\n❌ Удалено")
        except Exception:
            pass
        return

    # Задача — переключить STK ↔ CLOQ
    if data.startswith("task_move:"):
        tid = int(data.split(":")[1])
        c = db()
        r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not r:
            c.close()
            await q.answer("⚠️ Не найдено")
            return
        nxt = "cloq" if r["project"] == "stk" else "stk"
        c.execute("UPDATE tasks SET project=? WHERE id=?", (nxt, tid))
        c.commit()
        c.close()
        proj_label = "🌹 *STK*" if nxt == "stk" else "⌚ *CLOQ*"
        new_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готово", callback_data=f"task_done:{tid}"),
             InlineKeyboardButton("🔄 → CLOQ" if nxt == "stk" else "🔄 → STK",
                                  callback_data=f"task_move:{tid}")],
        ])
        try:
            new_text = re.sub(
                r"^.*\n\n", f"{proj_label}\n\n", q.message.text, count=1
            )
            await q.edit_message_text(
                new_text, parse_mode=ParseMode.MARKDOWN, reply_markup=new_kb,
            )
        except Exception:
            pass
        await q.answer(f"→ {nxt.upper()}")
        return

    # Идея — выполнено
    if data.startswith("idea_done:"):
        iid = int(data.split(":")[1])
        complete_idea(iid)
        await _remove_button_or_finish(q, data, "✅")
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

    # Идея — переместить категорию
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
        labels = {
            "business": "💡 Бизнес-идея",
            "marketing": "🎯 Маркетинг",
            "personal": "🏃 Личное",
        }
        try:
            new_text = re.sub(
                r"^.*\n\n", f"{labels[nxt]}\n\n", q.message.text, count=1
            )
            await q.edit_message_text(
                new_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=q.message.reply_markup,
            )
        except Exception:
            pass
        return


# ─────────── Списки задач ───────────
async def send_open_tasks(send_fn):
    """Отправляет список открытых задач/идей с кнопками."""
    stk = get_open_tasks(project="stk", limit=20)
    cloq = get_open_tasks(project="cloq", limit=15)
    pers = get_personal_ideas(15)

    sections = [
        ("🌹 STK", stk, "task"),
        ("⌚ CLOQ", cloq, "task"),
        ("🏃 Личное", pers, "idea"),
    ]

    if not any(items for _, items, _ in sections):
        await send_fn("📋 *Открытые задачи*\n\n_всё чисто 🎉_", None)
        return

    await send_fn(
        "📋 *Открытые задачи на сегодня*\n_нажми на задачу — она выполнится_",
        None,
    )

    for label, items, kind in sections:
        if not items:
            continue
        header = f"━━ *{label} ({len(items)})* ━━"
        rows = []
        for it in items:
            text = it["text"]
            iid = it["id"]
            cb = f"task_done:{iid}" if kind == "task" else f"idea_done:{iid}"
            label_btn = text if len(text) <= 50 else text[:47] + "…"
            rows.append([InlineKeyboardButton(f"☐ {label_btn}", callback_data=cb)])
        kb = InlineKeyboardMarkup(rows)
        await send_fn(header, kb)


async def cmd_tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def send(text, kb):
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
    await send_open_tasks(send)


# ─────────── Утренний пуш в 10:00 Алматы ───────────
async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    """Один пуш в 10:00 по Алматы: карточка дня (pinned) → погода → задачи."""
    chat_id = OWNER_ID or int(get_state("last_chat_id", 0) or 0)
    if not chat_id:
        log.warning("morning_job: chat_id unknown — skipping")
        return

    bot = context.bot

    # 1. Открепить вчерашнюю карточку
    yesterday = (today_almaty() - timedelta(days=1)).isoformat()
    c = db()
    r = c.execute(
        "SELECT card_msg_id FROM daily WHERE date=?", (yesterday,)
    ).fetchone()
    c.close()
    if r and r["card_msg_id"]:
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=r["card_msg_id"])
        except Exception:
            pass

    # 2. Карточка дня + приветствие + погода (одно сообщение, оно же закрепляется)
    daily_msg, kb = build_daily_card()
    full_msg = (
        f"☀️ *Доброе утро, Абылай!*\n\n"
        f"{get_weather()}\n\n"
        f"{daily_msg}"
    )
    sent = await bot.send_message(
        chat_id=chat_id,
        text=full_msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        log.warning("pin morning: %s", e)

    today = today_almaty().isoformat()
    c = db()
    c.execute(
        "INSERT INTO daily(date, card_msg_id, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET card_msg_id=excluded.card_msg_id, "
        "updated_at=excluded.updated_at",
        (today, sent.message_id, int(datetime.now().timestamp())),
    )
    c.commit()
    c.close()

    # 3. Задачи — отдельными сообщениями (чтобы кнопки уместились)
    async def send(text, kb_):
        await bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_,
        )
    await send_open_tasks(send)

    log.info("MORNING JOB sent (card + weather + tasks)")


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
async def run():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("tasks", cmd_tasks_handler))
    app.add_handler(CommandHandler("weather", cmd_weather))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Единственное расписание: 10:00 по Алматы
    app.job_queue.run_daily(
        morning_job,
        time=dtime(hour=10, minute=0, tzinfo=ALMATY_TZ),
        name="morning_10",
    )

    log.info("🤖 STK Bot v4.0 starting — clean, no Notion, no time reminders")
    log.info("   OWNER_ID=%s", OWNER_ID)
    log.info(
        "   ANTHROPIC=%s GROQ=%s OPENAI=%s",
        "ON" if ANTHROPIC_KEY else "OFF",
        "ON" if GROQ_KEY else "OFF",
        "ON" if OPENAI_KEY else "OFF",
    )
    log.info("   CITY=%s TZ=Asia/Almaty", WEATHER_CITY)

    await app.initialize()
    await app.start()
    await setup_bot_menu(app)
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
