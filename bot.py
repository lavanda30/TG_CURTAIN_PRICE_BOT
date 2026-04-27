"""
bot.py — Price Bot (пробний доступ 2 дні, без оплати)

Змінні середовища (Railway):
  BOT_TOKEN    — токен бота
  ADMIN_ID     — telegram_id адміна
  DATABASE_URL — PostgreSQL URL
"""
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from db import (
    init_db, upsert_user, get_user, start_trial,
    get_active_subscription,
    get_all_brands, get_prices_for_brands,
    get_all_users_with_subs,
    save_purchase_request,
    mark_purchase_requested,
    get_last_brands,
    TRIAL_DAYS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))
PAGE_SIZE = 8          # позицій товару на сторінці при перегляді бренду
BRAND_PAGE_SIZE = 20   # брендів на сторінці вибору (2 стовпчики × 10)

# Бренди-виключення (не показувати)
EXCLUDED_BRANDS = {"NO NAME", "NoName", "прайс 01.10.2025"}

# Набір іконок для брендів (по індексу, циклічно)
BRAND_ICONS = [
    "🌸", "🌺", "🌼", "🌻", "🌹", "🌷", "💐", "🍀",
    "🎀", "✨", "💎", "🎨", "🎭", "🦋", "🌿", "🍃",
    "🔷", "🔶", "🟣", "🟤", "⭐", "🌟", "💫", "🎯",
]

# Стан вибору брендів в пам'яті
_pending: dict = {}
# Стан purchase flow: {user_id: {"step": ..., "brands": [...]}}
_purchase: dict = {}


# ══════════════════════════════════════════
#  Допоміжні функції
# ══════════════════════════════════════════
UAH_RATE = 45

def brand_icon(brand: str, all_brands: list) -> str:
    try:
        idx = all_brands.index(brand)
    except ValueError:
        idx = 0
    return BRAND_ICONS[idx % len(BRAND_ICONS)]


def fmt_price(row: dict) -> str:
    currency = str(row.get("currency") or "USD").strip().upper()
    retail   = row.get("price_retail")
    price    = row.get("price")
    main     = retail if retail is not None else price
    if main is None:
        return "—"
    try:
        f = float(main)
    except (TypeError, ValueError):
        return str(main)
    if currency in ("USD", "У.Е.", "U.E.", "$"):
        uah = round(f * UAH_RATE * 2)
        val = int(f) if f == int(f) else f
        return f"*{val}$* · ~{uah}грн"
    return f"*{f:.2f}* грн"


def get_tag(row: dict) -> str:
    in_stock = str(row.get("in_stock") or "").upper()
    if "OUT OF STOCK" in in_stock or "ЗНЯТО" in in_stock:
        return "⛔"
    if "SALE" in in_stock or "РОЗПРОДАЖ" in in_stock:
        return "🔴"
    if "ORDER" in in_stock:
        return "📦"
    return ""


# ══════════════════════════════════════════
#  Клавіатура вибору брендів (2 стовпчики)
# ══════════════════════════════════════════
def kb_brand_select(selected: set, all_brands: list, page: int = 0) -> InlineKeyboardMarkup:
    start = page * BRAND_PAGE_SIZE
    end   = min(start + BRAND_PAGE_SIZE, len(all_brands))
    chunk = all_brands[start:end]
    rows  = []

    # Два стовпчики, іконка перед чекбоксом — назва бренду завжди починається з однакової позиції
    for i in range(0, len(chunk), 2):
        row = []
        for brand in chunk[i:i+2]:
            mark = "✅" if brand in selected else "⬜"
            icon = brand_icon(brand, all_brands)
            row.append(InlineKeyboardButton(
                f"{icon} {mark} {brand}",
                callback_data=f"tgl:{brand}:{page}"
            ))
        rows.append(row)

    # Навігація + Підтвердити в одному рядку
    n = len(selected)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"bpg:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"✔️ Підтвердити ({n})", callback_data="confirm_brands"))
    if end < len(all_brands):
        nav_row.append(InlineKeyboardButton("Далі ▶️", callback_data=f"bpg:{page+1}"))
    rows.append(nav_row)
    rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="cancel_purchase")])
    return InlineKeyboardMarkup(rows)


def kb_main(brands: list) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(brands), 2):
        row = []
        for b in brands[i:i+2]:
            row.append(InlineKeyboardButton(f"🧵 {b}", callback_data=f"brand:{b}:0"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("📋 Мої бренди",      callback_data="mysub"),
        InlineKeyboardButton("🔄 Змінити бренди", callback_data="change_brands"),
    ])
    rows.append([InlineKeyboardButton("💳 Отримати бота",         callback_data="purchase_start")])
    return InlineKeyboardMarkup(rows)


def kb_brand_nav(supplier: str, items: list, page: int) -> InlineKeyboardMarkup:
    total = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    nav   = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"page:{supplier}:{page-1}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"page:{supplier}:{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🏠 Головна", callback_data="main"),
    ])
    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════
#  /start
# ══════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    db_user = upsert_user(user.id, user.username or "", user.first_name or "")

    # Якщо вже є активна підписка (триал) — одразу головна
    sub = get_active_subscription(user.id)
    if sub:
        await _show_main(update.message, sub)
        return

    # Якщо вже купував (purchase_requested) — бот закритий
    if db_user.get("purchase_requested"):
        await update.message.reply_text(
            "ℹ️ Ваш запит на розробку вже надіслано.\n"
            "Адмін зв'яжеться з вами найближчим часом! 🙏"
        )
        return

    # Якщо пробний закінчився
    if db_user["trial_used"]:
        await update.message.reply_text(
            f"👋 Привіт, {user.first_name}!\n\n"
            "⏰ На жаль, Ваш пробний доступ завершено.\n\n"
            "Натисніть *«Отримати бота»* щоб мати такого бота:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 Отримати бота", callback_data="purchase_start")
            ]])
        )
        return

    # Перший раз — показуємо вибір брендів
    all_brands = get_all_brands()
    _pending[user.id] = {"selected": set(), "mode": "trial"}
    await update.message.reply_text(
        f"👋 Привіт, *{user.first_name}*!\n\n"
        f"🎁 Вам доступний *{TRIAL_DAYS}-денний повний доступ*.\n\n"
        f"Цей бот дозволяє *автоматично і миттєво* шукати необхідні позиції в множині прайс-листів постачальників.\n\n"
        f"Достатньо ввести код товару або його частину — бот покаже постачальника, ціну в доларах і гривнях "
        f"(комерційний курс 45 та подвійна націнка).\n\n"
        f"Оберіть бренди, які Вас цікавлять:",
        parse_mode="Markdown",
        reply_markup=kb_brand_select(set(), all_brands)
    )


async def _show_main(message_or_query, sub):
    brands  = sub["brands"]
    d       = get_prices_for_brands(brands)
    total   = sum(len(v) for v in d.values())
    expires = sub["expires_at"].strftime("%d.%m.%Y")

    text = (
        f"🛍 *Миттєвий Прайс Штори*\n\n"
        f"⏱ Пробний доступ до: *{expires}*\n"
        f"📦 Брендів: *{len(brands)}* · Позицій: *{total}*\n\n"
        "Оберіть бренд або введіть код/ім'я тканини:"
    )
    kb = kb_main(sorted(d.keys()))

    if hasattr(message_or_query, "reply_text"):
        await message_or_query.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# ══════════════════════════════════════════
#  Purchase flow (Отримати функціонал)
# ══════════════════════════════════════════
async def _ask_purchase_q1(query):
    await query.edit_message_text(
        "💳 *Оформлення запиту на бота*\n\n"
        "❓ *Чи є у вас прайс-листи брендів, яких немає у наданому списку?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Так", callback_data="pq1:yes"),
                InlineKeyboardButton("❌ Ні",  callback_data="pq1:no"),
            ],
            [InlineKeyboardButton("↩️ Скасувати", callback_data="cancel_purchase")]
        ])
    )


async def _ask_purchase_q2(query):
    await query.edit_message_text(
        "❓ *Чи потрібно буде змінювати повідомлення про виведення ціни?*\n\n"
        "_Наприклад: кастомізація формату, автоматичні підрахунки тощо_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Так", callback_data="pq2:yes"),
                InlineKeyboardButton("❌ Ні",  callback_data="pq2:no"),
            ],
            [InlineKeyboardButton("↩️ Скасувати", callback_data="cancel_purchase")]
        ])
    )


async def _finish_purchase(query, user, ctx):
    state = _purchase.get(user.id, {})
    q1    = state.get("q1", "—")
    q2    = state.get("q2", "—")
    brands = state.get("brands", [])

    brands_text = ", ".join(brands) if brands else "невідомо"
    q1_text = "Так" if q1 == "yes" else "Ні"
    q2_text = "Так" if q2 == "yes" else "Ні"

    # Зберегти запит в БД, але НЕ блокувати доступ якщо триал ще активний
    save_purchase_request(user.id, q1, q2)
    active_sub = get_active_subscription(user.id)
    if not active_sub:
        mark_purchase_requested(user.id)
    _purchase.pop(user.id, None)

    await query.edit_message_text(
        "✅ *Ваш запит надіслано!*\n\n"
        "Адмін зв'яжеться з вами найближчим часом. 🙏\n\n"
        + ("⏳ Ваш пробний доступ скоро закінчиться. Після цього ви отримаєте власного бота!" if active_sub else "Дякуємо за інтерес до нашого сервісу!"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Головна", callback_data="main")]]) if active_sub else None
    )

    # Нотифікація адміну
    if ADMIN_ID:
        if user.username:
            # Зберігаємо підкреслення, щоб @Anna_Love не ставало @AnnaLove
            safe_username = user.username.replace("_", "\_")
            name_line = f"@{safe_username} (`{user.id}`)"
        else:
            # Немає username — даємо ім'я + посилання tg://user?id=...
            first = user.first_name or ""
            last  = (" " + user.last_name) if user.last_name else ""
            name_line = f"[{first}{last}](tg://user?id={user.id}) (`{user.id}`)"
        msg = (
            f"💳 *Новий запит на придбання!*\n\n"
            f"👤 Юзер: {name_line}\n"
            f"🏷 Бренди: {brands_text}\n\n"
            f"❓ Є бренди поза списком: *{q1_text}*\n"
            f"❓ Потрібна кастомізація ціни: *{q2_text}*"
        )
        await ctx.bot.send_message(ADMIN_ID, msg, parse_mode="Markdown")


# ══════════════════════════════════════════
#  Callback handler
# ══════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    user = q.from_user
    cmd  = q.data

    # ── Зміна брендів ──
    if cmd == "change_brands":
        sub = get_active_subscription(user.id)
        if not sub:
            await q.edit_message_text(
                "❌ Активного доступу немає.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💳 Отримати бота", callback_data="purchase_start")
                ]])
            )
            return
        all_brands = get_all_brands()
        current    = set(sub["brands"])
        _pending[user.id] = {"selected": current.copy(), "mode": "change"}
        await q.edit_message_text(
            f"🔄 *Зміна брендів*\n\nПоточних: *{len(current)}*\nОберіть нові або приберіть зайві:",
            parse_mode="Markdown",
            reply_markup=kb_brand_select(current, all_brands)
        )

    # ── Toggle бренду ──
    elif cmd.startswith("tgl:"):
        _, brand, page_str = cmd.split(":", 2)
        page  = int(page_str)
        state = _pending.get(user.id)
        if not state:
            await q.edit_message_text("Сесія закінчилась. Натисніть /start")
            return
        selected = state["selected"]
        if brand in selected:
            selected.discard(brand)
        else:
            selected.add(brand)
        all_brands = get_all_brands()
        n = len(selected)
        await q.edit_message_text(
            f"📦 Оберіть бренди:\nОбрано: *{n}*",
            parse_mode="Markdown",
            reply_markup=kb_brand_select(selected, all_brands, page)
        )

    # ── Пагінація бренд-вибору ──
    elif cmd.startswith("bpg:"):
        page       = int(cmd.split(":")[1])
        state      = _pending.get(user.id, {})
        selected   = state.get("selected", set())
        all_brands = get_all_brands()
        n = len(selected)
        await q.edit_message_text(
            f"📦 Оберіть бренди:\nОбрано: *{n}*",
            parse_mode="Markdown",
            reply_markup=kb_brand_select(selected, all_brands, page)
        )

    # ── Підтвердження вибору брендів ──
    elif cmd == "confirm_brands":
        state = _pending.get(user.id)
        if not state:
            await q.edit_message_text("Сесія закінчилась. Натисніть /start")
            return
        selected = state["selected"]
        mode     = state.get("mode", "trial")
        if not selected:
            await q.answer("Оберіть хоча б 1 бренд!", show_alert=True)
            return

        brands_list = sorted(selected)

        if mode == "trial":
            # Запускаємо пробний доступ
            expires = start_trial(user.id, brands_list)
            _pending.pop(user.id, None)

            # Нотифікація адміну
            if ADMIN_ID:
                db_user = get_user(user.id)
                name = f"@{user.username.replace('_', '\\_')}" if user.username else user.first_name
                if user.username:
                    safe_un = user.username.replace("_", "\_")
                    name = f"@{safe_un}"
                else:
                    first = user.first_name or ""
                    last  = (" " + user.last_name) if user.last_name else ""
                    name = f"[{first}{last}](tg://user?id={user.id})"
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"🆕 *Новий користувач (пробний доступ)*\n\n"
                    f"👤 {name} (`{user.id}`)\n"
                    f"🏷 Брендів: *{len(brands_list)}*\n"
                    f"📋 {', '.join(brands_list)}\n"
                    f"⏱ Доступ до: {expires.strftime('%d.%m.%Y')}",
                    parse_mode="Markdown"
                )

            sub = get_active_subscription(user.id)
            # Показуємо бренди в 2 стовпці
            all_b = get_all_brands()
            lines = []
            for i in range(0, len(brands_list), 2):
                left = f"{brand_icon(brands_list[i], all_b)} {brands_list[i]}"
                if i + 1 < len(brands_list):
                    right = f"{brand_icon(brands_list[i+1], all_b)} {brands_list[i+1]}"
                    lines.append(f"{left:<22} {right}")
                else:
                    lines.append(left)
            brands_text = "\n".join(lines)
            await q.edit_message_text(
                f"✅ *Чудово!* Ваш пробний доступ активовано!\n\n"
                f"Ви обрали *{len(brands_list)} брендів:*\n`{brands_text}`\n\n"
                f"⏱ Доступ діє *{TRIAL_DAYS} дні*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Перейти до прайсів", callback_data="main")
                ]])
            )

        elif mode == "change":
            # Оновлюємо бренди в активній підписці
            from db import update_trial_brands
            update_trial_brands(user.id, brands_list)
            _pending.pop(user.id, None)
            sub = get_active_subscription(user.id)
            all_b = get_all_brands()
            # Два стовпці в тексті
            lines = []
            for i in range(0, len(brands_list), 2):
                left  = f"{brand_icon(brands_list[i], all_b)} {brands_list[i]}"
                if i + 1 < len(brands_list):
                    right = f"{brand_icon(brands_list[i+1], all_b)} {brands_list[i+1]}"
                    lines.append(f"{left:<22} {right}")
                else:
                    lines.append(left)
            brands_text = "\n".join(lines)
            await q.edit_message_text(
                f"✅ Бренди оновлено!\n\n"
                f"Обрані бренди *({len(brands_list)}):*\n`{brands_text}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🛒 Переглянути прайси", callback_data="main"),
                    InlineKeyboardButton("🔄 Змінити ще", callback_data="change_brands")
                ]])
            )

    # ── Головна ──
    elif cmd == "main":
        sub = get_active_subscription(user.id)
        if not sub:
            db_user = get_user(user.id)
            if db_user and db_user.get("purchase_requested"):
                await q.edit_message_text(
                    "ℹ️ Ваш запит на придбання вже надіслано.\nАдмін зв'яжеться з вами! 🙏"
                )
                return
            await q.edit_message_text(
                "❌ Доступ завершено.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💳 Отримати бота", callback_data="purchase_start")
                ]])
            )
            return
        await _show_main(q, sub)

    # ── Перегляд бренду / пагінація ──
    elif cmd.startswith("brand:") or cmd.startswith("page:"):
        sub = get_active_subscription(user.id)
        if not sub:
            await q.edit_message_text("❌ Доступ завершено.")
            return
        parts    = cmd.split(":", 2)
        supplier = parts[1]
        page     = int(parts[2])
        d        = get_prices_for_brands(sub["brands"])
        items    = d.get(supplier)
        if not items:
            await q.edit_message_text("❌ Бренд не знайдено або немає доступу.")
            return
        text = _build_brand_text(supplier, items, page)
        await q.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=kb_brand_nav(supplier, items, page))

    # ── Пошук ──
    elif cmd == "search":
        await q.edit_message_text(
            "🔍 *Введіть назву або артикул:*\n\nНаприклад: `HARMONY`, `блекаут`, `FA1106`",
            parse_mode="Markdown"
        )

    # ── Мої бренди ──
    elif cmd == "mysub":
        sub = get_active_subscription(user.id)
        if not sub:
            await q.edit_message_text("❌ Активного доступу немає.", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 Отримати бота", callback_data="purchase_start")
            ]]))
            return
        expires     = sub["expires_at"].strftime('%d.%m.%Y')
        all_b       = get_all_brands()
        brands_text = "\n".join(f"{brand_icon(b, all_b)} {b}" for b in sorted(sub["brands"]))
        await q.edit_message_text(
            f"📋 *Ваші бренди*\n\n"
            f"⏱ Пробний доступ до: *{expires}*\n"
            f"📦 Брендів: *{len(sub['brands'])}*\n\n"
            f"{brands_text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Змінити бренди", callback_data="change_brands")],
                [InlineKeyboardButton("🏠 Головна",        callback_data="main")],
            ])
        )

    # ── Purchase flow ──
    elif cmd == "cancel_purchase":
        _pending.pop(user.id, None)
        _purchase.pop(user.id, None)
        # ВАЖЛИВО: завжди перевіряємо активну підписку — не блокуємо доступ
        sub = get_active_subscription(user.id)
        if sub:
            await _show_main(q, sub)
        else:
            # Триал не активний і не починався — пропонуємо вибрати бренди
            await q.edit_message_text(
                "↩️ Скасовано.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ Почати знову", callback_data="start_over")
                ]])
            )

    elif cmd == "start_over":
        _pending.pop(user.id, None)
        _purchase.pop(user.id, None)
        all_brands = get_all_brands()
        _pending[user.id] = {"selected": set(), "mode": "trial"}
        await q.edit_message_text(
            "Оберіть бренди, які Вас цікавлять:",
            reply_markup=kb_brand_select(set(), all_brands)
        )

    elif cmd == "purchase_start":
        sub = get_active_subscription(user.id)
        brands = sub["brands"] if sub else get_last_brands(user.id)
        _purchase[user.id] = {"brands": brands}
        await _ask_purchase_q1(q)

    elif cmd.startswith("pq1:"):
        answer = cmd.split(":")[1]
        if user.id not in _purchase:
            _purchase[user.id] = {}
        _purchase[user.id]["q1"] = answer
        await _ask_purchase_q2(q)

    elif cmd.startswith("pq2:"):
        answer = cmd.split(":")[1]
        if user.id not in _purchase:
            _purchase[user.id] = {}
        _purchase[user.id]["q2"] = answer
        await _finish_purchase(q, user, ctx)


# ══════════════════════════════════════════
#  Текстовий пошук
# ══════════════════════════════════════════
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Якщо юзер в стані вибору брендів (pending) — підказка
    if user.id in _pending:
        await update.message.reply_text(
            "⏳ Ви в процесі вибору брендів. Скористайтесь кнопками вище або натисніть /start щоб почати знову."
        )
        return

    sub  = get_active_subscription(user.id)
    if not sub:
        await update.message.reply_text(
            "❌ Доступ завершено.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 Отримати бота", callback_data="purchase_start")
            ]])
        )
        return

    query   = update.message.text.strip()
    q_lower = query.lower()
    d       = get_prices_for_brands(sub["brands"])

    results = []
    seen    = set()
    for supplier, items in d.items():
        for row in items:
            for field in ("sku", "name", "category", "fabric", "collection"):
                val = str(row.get(field) or "")
                if q_lower in val.lower():
                    key = (supplier, str(row.get("sku") or row.get("name") or ""))
                    if key not in seen:
                        seen.add(key)
                        results.append((supplier, row))
                    break

    if not results:
        await update.message.reply_text(
            f"❌ По запиту *{query}* нічого не знайдено",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Головна", callback_data="main")
            ]])
        )
        return

    shown = results[:7]
    msg   = f"🔍 Знайдено *{len(results)}* по «{query}»:\n\n"
    for supplier, row in shown:
        tag   = get_tag(row)
        sku   = str(row.get("sku") or row.get("name") or "?").strip()
        price = fmt_price(row)
        h     = row.get("height_cm")
        h_str = f" · {int(float(h))}см" if h else ""
        msg  += f"{tag}[{supplier}] `{sku}` — {price}{h_str}\n"
    if len(results) > 7:
        msg += f"\n_Знайдено більше 7. Уточніть запит для кращого результату._"

    await update.message.reply_text(
        msg, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Головна",     callback_data="main"),
        ]])
    )


def _build_brand_text(supplier: str, items: list, page: int) -> str:
    start = page * PAGE_SIZE
    end   = min(start + PAGE_SIZE, len(items))
    text  = f"🧵 *{supplier}*\nПоказано {start+1}–{end} з {len(items)}\n\n"
    for row in items[start:end]:
        tag   = get_tag(row)
        sku   = str(row.get("sku") or row.get("name") or "?").strip()
        price = fmt_price(row)
        h     = row.get("height_cm")
        h_str = f" · {int(float(h))}см" if h else ""
        color = row.get("color")
        c_str = f" · _{color}_" if color else ""
        text += f"{tag}`{sku}` — {price}{h_str}{c_str}\n"
    return text


# ══════════════════════════════════════════
#  Адмін команди
# ══════════════════════════════════════════
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = get_all_users_with_subs()
    if not users:
        await update.message.reply_text("Користувачів немає")
        return
    msg = f"👥 *Користувачі ({len(users)}):*\n\n"
    for u in users[:20]:
        name = f"@{u['username']}" if u['username'] else u['first_name']
        if u['active']:
            expires = u['expires_at'].strftime('%d.%m.%Y')
            msg += f"✅ {name} · до {expires} · {len(u['brands'] or [])} бр.\n"
        else:
            msg += f"⬜ {name} · без доступу\n"
    if len(users) > 20:
        msg += f"\n_...та ще {len(users)-20}_"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ══════════════════════════════════════════
#  main
# ══════════════════════════════════════════
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN не встановлено")

    init_db()
    logger.info("DB ready. Starting bot...")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def post_init(application):
        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("start", "Запустити бота / Головна"),
        ])

    app.post_init = post_init

    logger.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
