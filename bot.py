"""
Бот для сбора заказов из Telegram → Google Sheets
Таблица: "Отличный Улов"

Структура листа Прием:
  A  = имя
  B  = телефон
  C  = город
  D  = ЗАКАЗ (текст, заполняется формулой автоматически)
  Колонки 55–104 (BC–DD) = количество по каждому товару (позиции 1–50)

Товары берутся из листа Отчет, колонка A (строки 1–50).
Вы меняете список в Отчет — бот подхватывает автоматически.
"""

import os
import json
import asyncio
import logging

import anthropic
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────
TG_TOKEN          = os.environ["TG_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SHEET_ID          = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
ALLOWED_CHAT_IDS  = set(
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
)

# Колонка с которой начинаются количества товаров (колонка BC = 55)
PRODUCTS_START_COL = 5

# ─── Google Sheets ────────────────────────────────────────────────────────────
def get_spreadsheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


def get_products(sh) -> list[str]:
    """
    Читает актуальный список товаров из листа Отчет, колонка A (строки 1–50).
    Вы вносите/меняете товары там — бот сразу видит изменения.
    """
    ws = sh.worksheet("Отчет")
    values = ws.col_values(1)[:50]  # колонка A, до 50 товаров
    products = [v.strip() for v in values if v and v.strip()]
    log.info(f"Товаров загружено: {len(products)} → {products}")
    return products


def write_product_headers(sh, products: list[str]):
    """
    Записывает названия товаров в строку 1 листа Прием,
    начиная с колонки E (5). Запускается при старте бота.
    """
    ws = sh.worksheet("Прием")
    if not products:
        return
    col_start = rowcol_to_a1(1, 5)
    col_end   = rowcol_to_a1(1, 5 + len(products) - 1)
    ws.update(f"{col_start}:{col_end}", [products])
    log.info(f"Заголовки товаров записаны в строку 1: {products}")


def find_next_row(ws) -> int:
    """Находит первую пустую строку в листе Прием (начиная со строки 2)."""
    col_a = ws.col_values(1)  # колонка A (имя)
    # Ищем первую пустую после заголовка
    for i, val in enumerate(col_a[1:], start=2):  # строки 2, 3, 4...
        if not val or not val.strip():
            return i
    return len(col_a) + 1


def write_order(sh, order: dict, products: list[str]) -> int:
    """
    Записывает заказ в лист Прием.

    Колонка A = имя
    Колонка B = телефон
    Колонка C = город
    Колонки BC(55)..DD(104) = количество товаров по позициям 1–50
    """
    ws = sh.worksheet("Прием")
    row = find_next_row(ws)

    # Формируем текст заказа для колонки D
    ordered = order.get("products", {})
    order_text = ", ".join(f"{k} {v}шт" for k, v in ordered.items() if v)

    # Основные данные: A, B, C, D
    ws.update(f"A{row}:D{row}", [[
        order.get("name", ""),
        order.get("phone", ""),
        order.get("city", ""),
        order_text,
    ]])

    # Количества товаров: колонки BC..DD (55..104)
    qty_row = []
    for product_name in products:
        qty = ordered.get(product_name, "")
        qty_row.append(qty if qty else "")

    if qty_row:
        col_start = rowcol_to_a1(row, PRODUCTS_START_COL)
        col_end   = rowcol_to_a1(row, PRODUCTS_START_COL + len(qty_row) - 1)
        ws.update(f"{col_start}:{col_end}", [qty_row])

    log.info(f"Записано в строку {row}: {order.get('name')} | {ordered}")
    return row


# ─── Claude AI — парсинг заказа ───────────────────────────────────────────────
def parse_order(message_text: str, sender_name: str, products: list[str]) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    products_list = "\n".join(f"- {p}" for p in products)

    prompt = f"""Ты помощник рыбного магазина. Клиент написал сообщение в группу — извлеки данные заказа.

Товары этого сбора (ТОЛЬКО эти позиции существуют):
{products_list}

Сообщение (имя отправителя в мессенджере: {sender_name}):
---
{message_text}
---

Правила:
1. Если это НЕ заказ (вопрос, приветствие, обсуждение, благодарность) — верни {{"is_order": false}}
2. Имя: извлеки из сообщения. Если не указано — используй {sender_name}
3. Телефон: извлеки если есть. Цифры подряд (4+ цифры) без явного контекста — это телефон
4. Город: извлеки если есть, иначе пустая строка
5. Товары — ВАЖНО:
   - Сопоставляй написанное клиентом с товарами из списка максимально гибко
   - "форель свежая", "форелька", "форель 1 шт", "форель с/м", "форель х/к" — всё это "Форель"
   - "икра гор", "икра горб", "горбуша икра" — это "Икра горбуши"
   - Игнорируй уточняющие слова: свежая, замороженная, с/м, х/к, шт, кг, порция — они не меняют товар
   - Если товар похож на позицию из списка — сопоставляй с ней
   - Если товар совсем не похож ни на что из списка — игнорируй его
   - Количество: если не указано — считай 1
   - "- 1 шт", "-1", "1шт", "1 штука", "одна" — всё это количество 1

Ответь ТОЛЬКО валидным JSON без markdown и пояснений:
{{
  "is_order": true,
  "name": "Алина",
  "phone": "+79001234567",
  "city": "Павлово",
  "products": {{
    "Форель": 2,
    "Икра горбуши": 1
  }}
}}"""

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ─── Telegram Bot ─────────────────────────────────────────────────────────────
bot = Bot(token=TG_TOKEN)
dp  = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🐟 Бот приёма заказов активен!\n\n"
        "Читаю сообщения группы и вношу заказы в таблицу.\n"
        "Список товаров берётся из листа Отчет (колонка A).\n\n"
        "Команды:\n"
        "/товары — показать текущий список товаров"
    )


@dp.message(Command("товары"))
async def cmd_products(message: Message):
    """Показывает актуальный список товаров из таблицы."""
    try:
        sh = get_spreadsheet()
        products = get_products(sh)
        if products:
            lines = "\n".join(f"{i+1}. {p}" for i, p in enumerate(products))
            await message.answer(f"📋 Товары текущего сбора:\n\n{lines}")
        else:
            await message.answer(
                "⚠️ Список товаров пуст.\n"
                "Внесите наименования в лист Отчет, колонка A."
            )
    except Exception as e:
        log.error(f"Ошибка /товары: {e}", exc_info=True)
        await message.answer(f"Ошибка чтения таблицы: {e}")


@dp.message()
async def handle_message(message: Message):
    # Фильтр по разрешённым чатам
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        return

    text = message.text or message.caption or ""
    if not text or text.startswith("/"):
        return

    sender = message.from_user.full_name if message.from_user else "Неизвестно"

    try:
        sh       = get_spreadsheet()
        products = get_products(sh)

        if not products:
            log.warning("Список товаров пуст — заказ не обрабатывается")
            return

        order = parse_order(text, sender, products)

        if not order.get("is_order"):
            log.info(f"Не заказ от {sender}: {text[:80]}")
            return

        row = write_order(sh, order, products)

        # Подтверждение в группе
        if message.chat.type in ("group", "supergroup"):
            items = order.get("products", {})
            items_text = "\n".join(f"  • {k}: {v}" for k, v in items.items()) or "  —"
            await message.reply(
                f"✅ Заказ принят! (строка {row})\n"
                f"👤 {order.get('name', '—')}\n"
                f"📞 {order.get('phone', '—')}\n"
                f"🏙 {order.get('city', '—')}\n"
                f"📦 Товары:\n{items_text}"
            )

    except json.JSONDecodeError as e:
        log.error(f"Claude вернул невалидный JSON: {e}")
    except Exception as e:
        log.error(f"Ошибка обработки сообщения: {e}", exc_info=True)


async def main():
    log.info("Бот запущен. Таблица: Отличный Улов. Товары → Отчет!A. Прием → E(5)+")
    # При старте записываем заголовки товаров в строку 1 листа Прием
    try:
        sh = get_spreadsheet()
        products = get_products(sh)
        if products:
            write_product_headers(sh, products)
    except Exception as e:
        log.error(f"Ошибка записи заголовков: {e}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
