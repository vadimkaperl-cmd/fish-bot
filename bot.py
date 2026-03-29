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


def write_product_headers(sh):
    """
    Записывает формулы =Отчет!A1, =Отчет!A2... в строку 1 листа Прием
    начиная с колонки E (5) до BB (54) — 50 позиций.
    Запускается один раз при старте бота.
    После этого заголовки обновляются автоматически при изменении Отчет!A.
    """
    ws = sh.worksheet("Прием")
    formulas = [[f"=Отчет!A{i}" for i in range(1, 51)]]
    col_start = rowcol_to_a1(1, 5)   # E1
    col_end   = rowcol_to_a1(1, 54)  # BB1
    ws.update(f"{col_start}:{col_end}", formulas, value_input_option="USER_ENTERED")
    log.info("Формулы заголовков записаны в E1:BB1")


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

Товары этого сбора (используй ТОЛЬКО эти названия в ответе):
{products_list}

Таблица распознавания — как клиенты пишут и что это означает:
- "форель", "форелька", "форель св", "форель с/м", "форель шт" → ФОРЕЛЬ ШТ
- "форель х/к", "форель хк", "форель холодного копчения" → ФОРЕЛЬ х/к ШТ
- "тунец", "тунец шт", "тунец св", "тунец свежий" → ТУНЕЦ ШТ
- "тунец ж/б", "тунец банка", "тунец консерва" → ТУНЕЦ ж/б ШТ
- "чавыча", "чавычa" → ЧАВЫЧА ШТ
- "омуль" → ОМУЛЬ ШТ
- "хек", "филе хека", "хек филе" → ФИЛЕ ХЕКА УП
- "сельдь", "селёдка", "сельдь кг" → СЕЛЬДЬ КГ
- "минтай", "филе минт", "минтай филе", "филе минтая" → ФИЛЕ МИНТ ШТ
- "икра горб", "икра горбуши", "горбуша икра", "икра гор" → ИКРА ГОРБ ШТ
- "икра кеты", "икра кет", "кета икра" → ИКРА КЕТЫ ШТ
- "икра нерки", "нерка икра", "икра нер" → ИКРА НЕРКИ ШТ
- "90+", "90 плюс", "набор 90" → 90+ КГ
- "70+", "70 плюс", "набор 70" → 70+ УП
- "ванамей", "креветка", "креветки ванамей" → ВАНАМЕЙ УП
- "чука", "салат чука", "чука салат" → ЧУКА УП
- "нори", "листы нори", "нори уп" → НОРИ УП
- "корюшка", "корюшка вял", "вяленая корюшка" → КОРЮШКА вял КГ
- "ассорти", "ассорти с/с", "ассорти соленое" → АССОРТИ с/с ШТ
- "ассорти минт", "ассорти минтай", "ассорти ж/б" → АССОРТИ МИНТ ж/б ШТ
- "орех кедр", "кедровый орех", "орех", "кедр" → ОРЕХ КЕДР 0.5 ШТ
- "печень трески", "печень трес", "печень" → ПЕЧЕНЬ ТРЕС ж/б ШТ
- "шпроты", "шпрот" → ШПРОТЫ ж/б ШТ
- "мидии", "мидии синие" → МИДИИ СИНИЕ УП
- "греб", "гребешок", "гребешки" → ГРЕБ п/с КГ
- "пельмени", "пельм" → ПЕЛЬМЕНИ ОЛ КГ
- "котлеты", "котлет мясо", "котлеты мясо" → КОТЛЕТ МЯСО УП
- "коллаген" → КОЛЛАГЕН ШТ
- "омега 160", "омега160" → ОМЕГА 160 ШТ
- "омега 42", "омега42" → ОМЕГА 42 ШТ
- "омега 250", "омега250" → ОМЕГА 250 ШТ
- "варенье кедр", "кедровое варенье", "кед ор в кед сир" → КЕД ОР В КЕД СИР ШТ
- "брусника", "брусника в сиропе", "брусника сироп" → БРУСНИКА В СОС СИР ШТ
- "маринад таежный", "маринад таеж", "маринад" → МАРИНАД ТАЕЖ ШТ
- "янтарная роса", "янтарная" → ЯНТАРНАЯ РОСА УП

Сообщение (имя отправителя в мессенджере: {sender_name}):
---
{message_text}
---

Правила:
1. Если это НЕ заказ (вопрос, приветствие, обсуждение, благодарность) — верни {{"is_order": false}}
2. Имя: извлеки из сообщения. Если не указано — используй {sender_name}
3. Телефон: любые 4+ цифры подряд — это телефон
4. Город: извлеки если есть, иначе пустая строка
5. Товары:
   - Используй таблицу распознавания выше
   - Название в ответе должно точно совпадать с названием из списка товаров
   - Если не можешь сопоставить — игнорируй
   - Количество: если не указано — считай 1

Ответь ТОЛЬКО валидным JSON без markdown и пояснений:
{{
  "is_order": true,
  "name": "Алина",
  "phone": "+79001234567",
  "city": "Павлово",
  "products": {{
    "ФОРЕЛЬ ШТ": 2,
    "ИКРА ГОРБ  ШТ": 1
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
        write_product_headers(sh)
    except Exception as e:
        log.error(f"Ошибка записи заголовков: {e}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
