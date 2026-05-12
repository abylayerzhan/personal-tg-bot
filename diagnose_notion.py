"""
diagnose_notion.py — найди почему бот не пишет в Notion.

ЗАПУСК (в Antigravity или локально):
    1. pip install httpx python-dotenv
    2. Создай рядом файл .env с теми же переменными что на Render:
        NOTION_TOKEN=ntn_...
        NOTION_DB_STK=b369ba922f60465c8d3c4c632907c902
        NOTION_DB_CLOQ=fed77cce9520438392e034d75f645d65
        NOTION_DB_PERSONAL=4be9ef4f7cad42f6a5846191d090d059
        NOTION_DB_IDEAS=e9a5d64e6ccb4ec28f6b954ac90c7208
    3. python diagnose_notion.py

Что делает:
1. Проверяет валидность NOTION_TOKEN
2. К каждой из 4 баз — пытается получить схему. Если 404 → Integration не подключена.
3. К каждой базе — пытается записать тестовую страницу. Если 400 → схема не совпадает.
4. Если запись прошла — сразу архивирует тестовую страницу.
5. Печатает понятный отчёт что чинить.
"""
import os
import sys
import asyncio
from datetime import datetime

try:
    import httpx
except ImportError:
    print("❌ pip install httpx")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv не установлен (pip install python-dotenv) — читаю переменные из окружения")

OK   = "\033[92m✅\033[0m"
FAIL = "\033[91m❌\033[0m"
WARN = "\033[93m⚠️ \033[0m"
INFO = "\033[96mℹ️ \033[0m"
BOLD = "\033[1m"
RST  = "\033[0m"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
DBS = {
    "🌹 STK Tasks":  os.environ.get("NOTION_DB_STK", "").strip(),
    "⌚ CLOQ Tasks": os.environ.get("NOTION_DB_CLOQ", "").strip(),
    "🏃 Личное":     os.environ.get("NOTION_DB_PERSONAL", "").strip(),
    "💡 Идеи":       os.environ.get("NOTION_DB_IDEAS", "").strip(),
}

if not NOTION_TOKEN:
    print(f"{FAIL} NOTION_TOKEN не задан. Положи его в .env или экспортируй в окружение.")
    sys.exit(1)

problems = []
hints = []

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def section(title):
    print(f"\n{BOLD}━━━ {title} ━━━{RST}")


async def check_token(client):
    section("1. NOTION TOKEN")
    r = await client.get("https://api.notion.com/v1/users/me", headers=HEADERS)
    if r.status_code == 200:
        data = r.json()
        bot_name = data.get("name") or data.get("bot", {}).get("owner", {}).get("user", {}).get("name", "?")
        ws = data.get("bot", {}).get("workspace_name")
        print(f"  {OK} Токен валиден. Бот: '{bot_name}'")
        if ws:
            print(f"     Workspace: {ws}")
        return True
    elif r.status_code == 401:
        print(f"  {FAIL} 401 — токен невалидный или отозван")
        problems.append("NOTION_TOKEN невалидный (401)")
        hints.append("Зайди на https://www.notion.so/my-integrations → создай новый токен → положи в .env и в Render Environment")
        return False
    else:
        print(f"  {FAIL} HTTP {r.status_code}: {r.text[:200]}")
        problems.append(f"Notion API недоступен (HTTP {r.status_code})")
        return False


async def check_database(client, name, db_id):
    print(f"\n  Проверяю {name} (id: {db_id[:8]}...{db_id[-4:]})")
    if not db_id:
        print(f"    {FAIL} ID пустой")
        problems.append(f"{name}: ID не задан в .env")
        return None

    # 1. Доступ к БД (читаем схему)
    r = await client.get(f"https://api.notion.com/v1/databases/{db_id}", headers=HEADERS)
    if r.status_code == 200:
        schema = r.json().get("properties", {})
        props = list(schema.keys())
        print(f"    {OK} Доступ есть. Свойства: {', '.join(props)}")
        return schema
    elif r.status_code == 404:
        print(f"    {FAIL} 404 — Integration НЕ ПОДКЛЮЧЕНА к этой базе")
        problems.append(f"{name}: Integration не подключена")
        hints.append(f"Открой базу '{name}' в Notion → ··· справа сверху → Connections → Connect to → выбери STK Bot integration")
        return None
    elif r.status_code == 401:
        print(f"    {FAIL} 401 — Integration отозвана или токен мёртвый")
        problems.append(f"{name}: 401")
        return None
    else:
        print(f"    {FAIL} HTTP {r.status_code}: {r.text[:300]}")
        problems.append(f"{name}: HTTP {r.status_code}")
        return None


async def test_write(client, name, db_id, schema):
    """Создаёт страницу и сразу архивирует её."""
    if not schema:
        return
    # Найти title-property
    title_prop = next((k for k, v in schema.items() if v.get("type") == "title"), None)
    if not title_prop:
        print(f"    {FAIL} Нет title-свойства в схеме!")
        problems.append(f"{name}: нет title-свойства")
        return

    props = {
        title_prop: {"title": [{"text": {"content": f"🧪 diagnostic test {datetime.now().strftime('%H:%M:%S')}"}}]},
    }
    # Если есть другие нужные свойства — заполняем дефолтами
    if "Priority" in schema and schema["Priority"].get("type") == "select":
        props["Priority"] = {"select": {"name": "🔴 Срочно"}}
    if "Done" in schema and schema["Done"].get("type") == "checkbox":
        props["Done"] = {"checkbox": False}
    if "Created" in schema and schema["Created"].get("type") == "date":
        props["Created"] = {"date": {"start": datetime.now().isoformat()}}
    if "Category" in schema and schema["Category"].get("type") == "select":
        # Только для базы Идеи
        props["Category"] = {"select": {"name": "💡 Business"}}

    r = await client.post(
        "https://api.notion.com/v1/pages",
        headers=HEADERS,
        json={"parent": {"database_id": db_id}, "properties": props},
    )

    if r.status_code == 200:
        page_id = r.json()["id"]
        print(f"    {OK} ЗАПИСЬ РАБОТАЕТ. Создана страница {page_id[:8]}...")
        # Архивируем
        r2 = await client.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS,
            json={"archived": True},
        )
        if r2.status_code == 200:
            print(f"       Тест архивирован — следов не осталось")
        else:
            print(f"    {WARN} Тест не архивировался (HTTP {r2.status_code}). Удали руками.")
    elif r.status_code == 400:
        body = r.text[:500]
        print(f"    {FAIL} 400 Bad Request — схема не совпадает с тем что шлёт код")
        print(f"       Ответ Notion: {body}")
        problems.append(f"{name}: схема не совпадает (400)")
        hints.append(f"Открой '{name}' и проверь что есть свойства: Name (title), и для STK/CLOQ — Priority (select), Done (checkbox), Created (date)")
    elif r.status_code == 403:
        print(f"    {FAIL} 403 Forbidden — у Integration нет прав на запись")
        problems.append(f"{name}: нет прав на запись")
        hints.append(f"В настройках Integration на notion.so/my-integrations → Capabilities → включи Insert content")
    else:
        print(f"    {FAIL} HTTP {r.status_code}: {r.text[:300]}")
        problems.append(f"{name}: запись не работает (HTTP {r.status_code})")


async def main():
    print(f"\n{BOLD}🔍 NOTION DIAGNOSTIC — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RST}")

    async with httpx.AsyncClient(timeout=20) as client:
        ok = await check_token(client)
        if not ok:
            print_summary()
            return

        section("2. ДОСТУП К БАЗАМ")
        schemas = {}
        for name, db_id in DBS.items():
            schemas[name] = await check_database(client, name, db_id)

        section("3. ТЕСТ ЗАПИСИ")
        for name, db_id in DBS.items():
            if schemas[name]:
                print(f"\n  {name}:")
                await test_write(client, name, db_id, schemas[name])

    print_summary()


def print_summary():
    section("ИТОГ")
    if not problems:
        print(f"\n  {OK}{OK}{OK} Notion работает идеально. Если бот всё равно не пишет —")
        print(f"     проблема в коде (старая версия на Render?) или ENV (ключи не подхватились).")
        print(f"\n  Проверь в логах Render первую строку при запуске:")
        print(f"     '🤖 STK Bot v2.X starting'")
        print(f"     'OWNER=...  NOTION=ON'")
        print(f"     Если NOTION=OFF — переменные на Render не сохранились.\n")
        return

    print(f"\n  {FAIL} Найдено проблем: {len(problems)}\n")
    for i, p in enumerate(problems, 1):
        print(f"     {i}. {p}")

    if hints:
        print(f"\n  {INFO} Что делать:\n")
        for i, h in enumerate(hints, 1):
            print(f"     {i}. {h}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
