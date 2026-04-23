"""
STK Bot diagnostic script v1
=============================
Проверяет все точки отказа по очереди и печатает понятный отчёт.

ЗАПУСК:
  Локально:        python diagnose.py
  На Render Shell: cd /opt/render/project/src && python diagnose.py

Требует только httpx (уже установлен в окружении бота).
Не правит никакие данные — только читает + делает ОДИН тестовый write-and-delete в Notion.
"""
import os
import sys
import asyncio
import sqlite3
from datetime import datetime

try:
    import httpx
except ImportError:
    print("❌ httpx не установлен. pip install httpx")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # на Render переменные и так в env

# ─── Цвета для терминала ───
OK = "\033[92m✅\033[0m"
FAIL = "\033[91m❌\033[0m"
WARN = "\033[93m⚠️ \033[0m"
INFO = "\033[96mℹ️ \033[0m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(title: str):
    print(f"\n{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD} {title}{RESET}")
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")


# ─── Переменные ───
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

NOTION_DBS = {
    "STK Tasks":   os.environ.get("NOTION_DB_STK", "").strip(),
    "CLOQ Tasks":  os.environ.get("NOTION_DB_CLOQ", "").strip(),
    "Личное":      os.environ.get("NOTION_DB_PERSONAL", "").strip(),
    "Идеи":        os.environ.get("NOTION_DB_IDEAS", "").strip(),
    "Привычки":    os.environ.get("NOTION_DB_HABITS", "").strip(),
}

DB_PATH = os.environ.get("DB_PATH", "data.db").strip()

problems = []


def mask(s: str, keep: int = 6) -> str:
    if not s:
        return "(пусто)"
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-4:]}"


# ═══════════════════════ 1. ENV ═══════════════════════
def check_env():
    header("1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ")
    print(f"  TELEGRAM_TOKEN      : {mask(TELEGRAM_TOKEN)}")
    print(f"  OWNER_CHAT_ID       : {OWNER_CHAT_ID or '(пусто)'}")
    print(f"  ANTHROPIC_API_KEY   : {mask(ANTHROPIC_KEY)}")
    print(f"  NOTION_TOKEN        : {mask(NOTION_TOKEN)}")
    print(f"  GROQ_API_KEY        : {mask(GROQ_API_KEY)}")
    print(f"  OPENAI_API_KEY      : {mask(OPENAI_API_KEY)}")
    print()
    for name, db_id in NOTION_DBS.items():
        status = OK if db_id else FAIL
        print(f"  NOTION_DB {name:12}: {status} {mask(db_id, 8)}")

    if not TELEGRAM_TOKEN:
        problems.append("TELEGRAM_TOKEN не задан")
    if not NOTION_TOKEN:
        problems.append("NOTION_TOKEN не задан — Notion-синк выключен")
    if not NOTION_DBS["STK Tasks"]:
        problems.append("NOTION_DB_STK не задан — Notion-синк выключен")


# ═══════════════════════ 2. Telegram ═══════════════════════
async def check_telegram():
    header("2. TELEGRAM API")
    if not TELEGRAM_TOKEN:
        print(f"  {FAIL} Пропускаю (токен пустой)")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
        if r.status_code == 200 and r.json().get("ok"):
            data = r.json()["result"]
            print(f"  {OK} getMe: @{data.get('username')} (id={data.get('id')})")
        else:
            print(f"  {FAIL} getMe → {r.status_code}")
            print(f"     ответ: {r.text[:300]}")
            problems.append(f"Telegram токен невалидный (HTTP {r.status_code})")
            return

        r = await client.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo")
        if r.status_code == 200:
            wh = r.json()["result"]
            url = wh.get("url", "")
            pending = wh.get("pending_update_count", 0)
            if url:
                print(f"  {WARN} Webhook установлен на: {url}")
                problems.append("Установлен webhook — может конфликтовать с polling")
            else:
                print(f"  {OK} Webhook пуст (чистый polling)")
            if pending > 0:
                print(f"  {INFO} В очереди висит {pending} необработанных апдейтов")

        print(f"  {INFO} Пробую getUpdates (если кто-то ещё слушает — увижу Conflict)...")
        r = await client.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": -1, "timeout": 0, "limit": 1},
        )
        if r.status_code == 409:
            print(f"  {FAIL} HTTP 409 Conflict — ДРУГОЙ КЛИЕНТ АКТИВЕН!")
            print(f"     Это значит другой процесс/сервер прямо сейчас слушает твоего бота.")
            print(f"     Проверь Render (возможно два контейнера), локальный запуск, другие хостинги.")
            problems.append("Telegram Conflict: второй клиент уже слушает updates")
        elif r.status_code == 200:
            print(f"  {OK} getUpdates прошёл без конфликта — единственный активный клиент")
        else:
            print(f"  {WARN} getUpdates → {r.status_code}: {r.text[:200]}")


# ═══════════════════════ 3. Notion ═══════════════════════
async def check_notion():
    header("3. NOTION API")
    if not NOTION_TOKEN:
        print(f"  {FAIL} Пропускаю (токен пустой)")
        return

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://api.notion.com/v1/users/me", headers=headers)
        if r.status_code == 200:
            data = r.json()
            print(f"  {OK} Token valid: bot='{data.get('name')}' type={data.get('type')}")
            workspace = data.get("bot", {}).get("workspace_name")
            if workspace:
                print(f"     workspace: {workspace}")
        elif r.status_code == 401:
            print(f"  {FAIL} 401 Unauthorized — NOTION_TOKEN невалидный или отозван")
            problems.append("Notion токен невалидный (401)")
            return
        else:
            print(f"  {FAIL} /users/me → {r.status_code}: {r.text[:300]}")
            problems.append(f"Notion API недоступен (HTTP {r.status_code})")
            return

        print(f"\n  Проверка доступа к базам:")
        accessible = []
        for name, db_id in NOTION_DBS.items():
            if not db_id:
                print(f"    {WARN} {name:12}: ID не задан, пропускаю")
                continue
            r = await client.get(
                f"https://api.notion.com/v1/databases/{db_id}",
                headers=headers,
            )
            if r.status_code == 200:
                data = r.json()
                props = list(data.get("properties", {}).keys())
                print(f"    {OK} {name:12}: OK | свойства: {', '.join(props)}")
                accessible.append((name, db_id, props))
            elif r.status_code == 404:
                print(f"    {FAIL} {name:12}: 404 — integration НЕ подключена к этой базе!")
                print(f"         Открой базу → ··· → Connections → добавь интеграцию")
                problems.append(f"Notion база '{name}' недоступна (integration не подключена)")
            elif r.status_code == 401:
                print(f"    {FAIL} {name:12}: 401 — токен не имеет доступа")
                problems.append(f"Notion база '{name}' — 401")
            else:
                print(f"    {FAIL} {name:12}: HTTP {r.status_code}: {r.text[:200]}")
                problems.append(f"Notion база '{name}' — HTTP {r.status_code}")

        if not accessible:
            print(f"\n  {FAIL} Ни одна база не доступна. Integration точно не подключена.")
            return

        stk_id = NOTION_DBS.get("STK Tasks")
        if stk_id and any(n == "STK Tasks" for n, _, _ in accessible):
            print(f"\n  Тест записи в STK Tasks...")
            stk_props = next((p for n, _, p in accessible if n == "STK Tasks"), [])
            has_name = "Name" in stk_props
            has_priority = "Priority" in stk_props
            has_done = "Done" in stk_props
            has_created = "Created" in stk_props
            if not has_name:
                print(f"    {FAIL} В базе нет свойства 'Name' (Title). Схема сломана.")
                print(f"         Есть свойства: {stk_props}")
                problems.append("STK Tasks: нет свойства 'Name'")
                return

            props = {
                "Name": {"title": [{"text": {"content": "🧪 diagnostic test — удалится автоматически"}}]},
            }
            if has_priority:
                props["Priority"] = {"select": {"name": "🔴 Срочно"}}
            if has_done:
                props["Done"] = {"checkbox": False}
            if has_created:
                props["Created"] = {"date": {"start": datetime.now().isoformat()}}

            r = await client.post(
                "https://api.notion.com/v1/pages",
                headers=headers,
                json={"parent": {"database_id": stk_id}, "properties": props},
            )
            if r.status_code == 200:
                page_id = r.json()["id"]
                print(f"    {OK} Создал тестовую страницу: {page_id[:8]}...")
                r2 = await client.patch(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=headers,
                    json={"archived": True},
                )
                if r2.status_code == 200:
                    print(f"    {OK} Архивировал — запись в Notion РАБОТАЕТ")
                else:
                    print(f"    {WARN} Не смог архивировать ({r2.status_code}) — страница осталась, удали руками")
            elif r.status_code == 400:
                print(f"    {FAIL} 400 Bad Request — схема базы не совпадает")
                print(f"         ответ: {r.text[:500]}")
                problems.append("Notion STK Tasks: схема не совпадает с кодом")
            else:
                print(f"    {FAIL} HTTP {r.status_code}: {r.text[:500]}")
                problems.append(f"Notion запись не работает (HTTP {r.status_code})")


# ═══════════════════════ 4. Local SQLite ═══════════════════════
def check_sqlite():
    header("4. ЛОКАЛЬНАЯ SQLite")
    if not os.path.exists(DB_PATH):
        print(f"  {WARN} Файл {DB_PATH} не существует (норм для чистого запуска)")
        return
    try:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        print(f"  {OK} Таблицы: {', '.join(tables)}")

        for t in ["tasks", "ideas", "reminders", "habits", "notion_map"]:
            if t in tables:
                try:
                    n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    extra = ""
                    if t == "tasks":
                        open_n = c.execute("SELECT COUNT(*) FROM tasks WHERE done=0").fetchone()[0]
                        extra = f" (открыто: {open_n})"
                    elif t == "reminders":
                        pend = c.execute("SELECT COUNT(*) FROM reminders WHERE sent=0").fetchone()[0]
                        extra = f" (pending: {pend})"
                    print(f"    {INFO} {t:12}: {n} записей{extra}")
                except Exception as e:
                    print(f"    {WARN} {t}: ошибка — {e}")

        if "tasks" in tables:
            rows = c.execute(
                "SELECT id, project, priority, text, done FROM tasks ORDER BY id DESC LIMIT 3"
            ).fetchall()
            if rows:
                print(f"\n  Последние 3 задачи:")
                for r in rows:
                    status = "✅" if r["done"] else "☐"
                    print(f"    #{r['id']} [{r['project']}/{r['priority']}] {status} {r['text'][:50]}")

        if "notion_map" in tables:
            mapped = c.execute("SELECT COUNT(*) FROM notion_map").fetchone()[0]
            print(f"\n  {INFO} В notion_map: {mapped} сопоставлений локальных записей с Notion")
            if mapped == 0:
                print(f"     {WARN} Пусто — значит НИ ОДНА запись не была синхронизирована")
                problems.append("notion_map пуст — sync никогда не срабатывал")

        c.close()
    except Exception as e:
        print(f"  {FAIL} Ошибка чтения БД: {e}")


# ═══════════════════════ 5. Voice APIs ═══════════════════════
async def check_voice():
    header("5. VOICE API (Whisper)")
    if not GROQ_API_KEY and not OPENAI_API_KEY:
        print(f"  {WARN} Ни GROQ_API_KEY, ни OPENAI_API_KEY не заданы — голосовые выключены")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        if GROQ_API_KEY:
            r = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            )
            if r.status_code == 200:
                models = [m.get("id") for m in r.json().get("data", [])]
                has_whisper = any("whisper" in (m or "").lower() for m in models)
                if has_whisper:
                    print(f"  {OK} Groq API: токен валиден, whisper-модель доступна")
                else:
                    print(f"  {WARN} Groq API: токен валиден, но whisper не найден среди {len(models)} моделей")
            else:
                print(f"  {FAIL} Groq API → {r.status_code}: {r.text[:200]}")
                problems.append(f"Groq API key невалиден (HTTP {r.status_code})")

        if OPENAI_API_KEY:
            r = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            )
            if r.status_code == 200:
                print(f"  {OK} OpenAI API: токен валиден")
            else:
                print(f"  {FAIL} OpenAI API → {r.status_code}: {r.text[:200]}")
                problems.append(f"OpenAI API key невалиден (HTTP {r.status_code})")


# ═══════════════════════ 6. Anthropic ═══════════════════════
async def check_anthropic():
    header("6. ANTHROPIC API (LLM classifier + Q&A)")
    if not ANTHROPIC_KEY:
        print(f"  {WARN} ANTHROPIC_API_KEY не задан — LLM-классификатор и Q&A выключены")
        return

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "Скажи слово 'работает' без кавычек."}],
            },
        )
        if r.status_code == 200:
            text = r.json()["content"][0]["text"][:80]
            print(f"  {OK} Haiku отвечает: {text}")
        elif r.status_code == 401:
            print(f"  {FAIL} 401 — ANTHROPIC_API_KEY невалидный")
            problems.append("Anthropic API key невалиден")
        elif r.status_code == 404:
            print(f"  {FAIL} 404 — модель claude-haiku-4-5-20251001 не существует в твоём доступе")
            problems.append("Anthropic: модель Haiku 4.5 недоступна")
        else:
            print(f"  {FAIL} HTTP {r.status_code}: {r.text[:300]}")
            problems.append(f"Anthropic API HTTP {r.status_code}")


# ═══════════════════════ SUMMARY ═══════════════════════
def summary():
    header("ИТОГ")
    if not problems:
        print(f"\n  {OK}{OK}{OK} Все проверки пройдены. Если бот не работает — ищи")
        print(f"     проблему в логике кода, а не в конфигурации.\n")
        return
    print(f"\n  {FAIL} Найдено проблем: {len(problems)}\n")
    for i, p in enumerate(problems, 1):
        print(f"     {i}. {p}")
    print()


async def main():
    print(f"\n{BOLD}🔍 STK Bot Diagnostic — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    check_env()
    await check_telegram()
    await check_notion()
    check_sqlite()
    await check_voice()
    await check_anthropic()
    summary()


if __name__ == "__main__":
    asyncio.run(main())
