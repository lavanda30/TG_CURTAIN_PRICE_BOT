"""
bot.py — Price Bot з пробним періодом і підпискою

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
    get_active_subscription, create_payment, activate_subscription,
    get_all_brands, get_prices_for_brands,
    get_pending_payments, get_all_users_with_subs,
    calc_amount, CARD_NUMBER, TRIAL_DAYS, BRAND_LIMIT, PRICE_SMALL, PRICE_BIG,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
PAGE_SIZE  = 8

# Стан вибору брендів в пам'яті: {telegram_id: {"selected": set(), "mode": "trial"|"pay"|"change"}}
_pending: dict = {}


# ══════════════════════════════════════════
#  Допоміжні функції форматування
# ══════════════════════════════════════════
UAH_RATE = 45

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
#  Клавіатури
# ══════════════════════════════════════════
def kb_brand_select(selected: set, all_brands: list, page: int = 0) -> InlineKeyboardMarkup:
    start  = page * 8
    end    = min(start + 8, len(all_brands))
    rows   = []

    for brand in all_brands[start:end]:
        mark = "✅" if brand in selected else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {brand}", callback_data=f"tgl:{brand}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"bpg:{page-1}"))
    if end < len(all_brands):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"bpg:{page+1}"))
    if nav:
        rows.append(nav)

    n   = len(selected)
    amt = calc_amount(n)
    rows.append([InlineKeyboardButton(
        f"✔️ Підтвердити ({n} бр. · {amt} грн/міс)",
        callback_data="confirm_brands"
    )])
    return InlineKeyboardMarkup(rows)


def kb_main(brands: list) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(brands), 2):
        row = []
        for b in brands[i:i+2]:
            row.append(InlineKeyboardButton(f"🧵 {b}", callback_data=f"brand:{b}:0"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🔍 Пошук",        callback_data="search"),
        InlineKeyboardButton("📋 Підписка",      callback_data="mysub"),
    ])
    rows.append([InlineKeyboardButton("🔄 Вибрати бренди", callback_data="change_brands")])
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
        InlineKeyboardButton("🔍 Пошук",  callback_data="search"),
        InlineKeyboardButton("🏠 Головна", callback_data="main"),
    ])
    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════
#  /start
# ══════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    db_user = upsert_user(user.id, user.username or "", user.first_name or "")

    sub = get_active_subscription(user.id)
    if sub:
        await _show_main(update.message, sub)
        return

    # Пробний період ще не використовувався
    if not db_user["trial_used"]:
        all_brands = get_all_brands()
        _pending[user.id] = {"selected": set(), "mode": "trial"}
        await update.message.reply_text(
            f"👋 Привіт, {user.first_name}!\n\n"
            f"🎁 У вас є *{TRIAL_DAYS} дні безкоштовного доступу*.\n\n"
            f"Оберіть бренди які вас цікавлять:\n"
            f"_(до {BRAND_LIMIT} брендів — {PRICE_SMALL} грн/міс, більше — {PRICE_BIG} грн/міс)_",
            parse_mode="Markdown",
            reply_markup=kb_brand_select(set(), all_brands)
        )
    else:
        # Підписка закінчилась
        await update.message.reply_text(
            f"👋 Привіт, {user.first_name}!\n\n"
            "❌ Ваш пробний період або підписка закінчились.\n\n"
            f"💳 Вартість підписки:\n"
            f"• до {BRAND_LIMIT} брендів — *{PRICE_SMALL} грн/міс*\n"
            f"• більше {BRAND_LIMIT} брендів — *{PRICE_BIG} грн/міс*\n\n"
            "Оберіть бренди для оформлення підписки:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📦 Вибрати бренди", callback_data="choose_brands_pay")
            ]])
        )


async def _show_main(message_or_query, sub):
    brands = sub["brands"]
    d      = get_prices_for_brands(brands)
    total  = sum(len(v) for v in d.values())
    expires = sub["expires_at"].strftime("%d.%m.%Y")
    is_trial = sub["amount"] == 0

    trial_note = " _(пробний)_" if is_trial else ""
    text = (
        f"🛍 *Прайс — Штори та Тюль*\n\n"
        f"✅ Доступ до: *{expires}*{trial_note}\n"
        f"📦 Брендів: *{len(brands)}* · Позицій: *{total}*\n\n"
        "Оберіть бренд або скористайтесь пошуком:"
    )
    kb = kb_main(sorted(d.keys()))

    if hasattr(message_or_query, "reply_text"):
        await message_or_query.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# ══════════════════════════════════════════
#  Callback handler
# ══════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    user = q.from_user
    cmd  = q.data

    # ── Вибір брендів для оплати ──
    if cmd == "choose_brands_pay":
        all_brands = get_all_brands()
        _pending[user.id] = {"selected": set(), "mode": "pay"}
        await q.edit_message_text(
            f"📦 Оберіть бренди для підписки:\n"
            f"_(до {BRAND_LIMIT} брендів — {PRICE_SMALL} грн/міс, більше — {PRICE_BIG} грн/міс)_",
            parse_mode="Markdown",
            reply_markup=kb_brand_select(set(), all_brands)
        )

    # ── Зміна брендів (для діючих підписників) ──
    elif cmd == "change_brands":
        sub = get_active_subscription(user.id)
        if not sub:
            await q.edit_message_text("❌ Підписка не активна.", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📦 Оформити підписку", callback_data="choose_brands_pay")
            ]]))
            return
        all_brands = get_all_brands()
        current = set(sub["brands"])
        _pending[user.id] = {"selected": current.copy(), "mode": "change"}
        await q.edit_message_text(
            f"🔄 *Зміна брендів*\n\n"
            f"Поточна підписка: *{len(current)} брендів*\n"
            f"Додайте або приберіть бренди:\n"
            f"_(до {BRAND_LIMIT} брендів — {PRICE_SMALL} грн/міс, більше — {PRICE_BIG} грн/міс)_",
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
        n   = len(selected)
        amt = calc_amount(n)
        await q.edit_message_text(
            f"📦 Оберіть бренди:\n"
            f"Обрано: *{n}* · Сума: *{amt} грн/міс*",
            parse_mode="Markdown",
            reply_markup=kb_brand_select(selected, all_brands, page)
        )

    # ── Пагінація бренд-вибору ──
    elif cmd.startswith("bpg:"):
        page  = int(cmd.split(":")[1])
        state = _pending.get(user.id, {})
        selected  = state.get("selected", set())
        all_brands = get_all_brands()
        n   = len(selected)
        amt = calc_amount(n)
        await q.edit_message_text(
            f"📦 Оберіть бренди:\nОбрано: *{n}* · Сума: *{amt} грн/міс*",
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
        mode     = state.get("mode", "pay")

        if not selected:
            await q.answer("Оберіть хоча б 1 бренд!", show_alert=True)
            return

        brands_list = sorted(selected)
        amount      = calc_amount(len(brands_list))

        # Пробний період
        if mode == "trial":
            expires = start_trial(user.id, brands_list)
            _pending.pop(user.id, None)
            sub = get_active_subscription(user.id)
            await _show_main(q, sub)
            return

        # Оплата (новий або зміна)
        payment_id = create_payment(user.id, brands_list, amount)
        _pending.pop(user.id, None)

        brands_text = "\n".join(f"• {b}" for b in brands_list)
        action_note = "Зміна брендів" if mode == "change" else "Нова підписка"
        text = (
            f"✅ *Замовлення #{payment_id}* — {action_note}\n\n"
            f"Бренди ({len(brands_list)}):\n{brands_text}\n\n"
            f"💳 До сплати: *{amount} грн/міс*\n\n"
            f"Реквізити:\n`{CARD_NUMBER}`\n\n"
            "Після оплати натисніть кнопку — адмін активує протягом кількох годин."
        )
        await q.edit_message_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Я оплатив", callback_data=f"paid:{payment_id}")],
                [InlineKeyboardButton("🏠 Головна",   callback_data="main")],
            ])
        )

        if ADMIN_ID:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🆕 *Оплата #{payment_id}* ({action_note})\n"
                f"Юзер: @{user.username or user.first_name} (`{user.id}`)\n"
                f"Бренди: {', '.join(brands_list)}\n"
                f"Сума: {amount} грн\n\n"
                f"Активувати: `/activate {user.id}`",
                parse_mode="Markdown"
            )

    # ── Підтвердження оплати юзером ──
    elif cmd.startswith("paid:"):
        payment_id = cmd.split(":")[1]
        await q.edit_message_text(
            f"⏳ Оплату #{payment_id} отримано.\n\n"
            "Адмін перевірить і активує протягом кількох годин.\n"
            "Ви отримаєте повідомлення як тільки доступ відкриється.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Головна", callback_data="main")
            ]])
        )

    # ── Головна ──
    elif cmd == "main":
        sub = get_active_subscription(user.id)
        if not sub:
            await q.edit_message_text(
                "❌ Підписка не активна.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📦 Оформити підписку", callback_data="choose_brands_pay")
                ]])
            )
            return
        await _show_main(q, sub)

    # ── Перегляд бренду / пагінація ──
    elif cmd.startswith("brand:") or cmd.startswith("page:"):
        sub = get_active_subscription(user.id)
        if not sub:
            await q.edit_message_text("❌ Підписка не активна.")
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

    # ── Моя підписка ──
    elif cmd == "mysub":
        sub = get_active_subscription(user.id)
        if not sub:
            await q.edit_message_text("❌ Активної підписки немає.", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📦 Оформити підписку", callback_data="choose_brands_pay")
            ]]))
            return
        is_trial    = sub["amount"] == 0
        brands_text = "\n".join(f"• {b}" for b in sorted(sub["brands"]))
        trial_note  = "\n🎁 _Пробний період_" if is_trial else f"\n💳 Сума: *{sub['amount']} грн/міс*"
        await q.edit_message_text(
            f"📋 *Ваша підписка*\n\n"
            f"Діє до: *{sub['expires_at'].strftime('%d.%m.%Y')}*{trial_note}\n"
            f"Брендів: *{len(sub['brands'])}*\n\n"
            f"{brands_text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Змінити бренди", callback_data="change_brands")],
                [InlineKeyboardButton("🏠 Головна",        callback_data="main")],
            ])
        )


# ══════════════════════════════════════════
#  Текстовий пошук
# ══════════════════════════════════════════
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sub  = get_active_subscription(user.id)
    if not sub:
        await update.message.reply_text(
            "❌ Підписка не активна.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📦 Оформити підписку", callback_data="choose_brands_pay")
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

    shown = results[:15]
    msg   = f"🔍 Знайдено *{len(results)}* по «{query}»:\n\n"
    for supplier, row in shown:
        tag   = get_tag(row)
        sku   = str(row.get("sku") or row.get("name") or "?").strip()
        price = fmt_price(row)
        h     = row.get("height_cm")
        h_str = f" · {int(float(h))}см" if h else ""
        msg  += f"{tag}[{supplier}] `{sku}` — {price}{h_str}\n"
    if len(results) > 15:
        msg += f"\n_…ще {len(results)-15}. Уточніть запит._"

    await update.message.reply_text(
        msg, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Новий пошук", callback_data="search"),
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
async def cmd_activate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Використання: /activate <telegram_id>")
        return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Невірний telegram_id")
        return

    ok = activate_subscription(target_id)
    if ok:
        await update.message.reply_text(f"✅ Підписку для {target_id} активовано!")
        try:
            await ctx.bot.send_message(
                target_id,
                "🎉 *Вашу підписку активовано!*\n\nНатисніть /start щоб почати.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(f"❌ Не знайдено pending оплати для {target_id}")


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    payments = get_pending_payments()
    if not payments:
        await update.message.reply_text("✅ Немає очікуючих оплат")
        return
    msg = "⏳ *Очікують активації:*\n\n"
    for p in payments:
        name = f"@{p['username']}" if p['username'] else p['first_name']
        msg += (
            f"#{p['id']} · {name} (`{p['telegram_id']}`)\n"
            f"{len(p['brands'])} брендів · {p['amount']} грн\n"
            f"Активувати: `/activate {p['telegram_id']}`\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


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
            trial   = " (триал)" if u['amount'] == 0 else f" · {u['amount']}грн"
            msg += f"✅ {name}{trial} · до {expires}\n"
        else:
            msg += f"⬜ {name} · без підписки\n"
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
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("activate", cmd_activate))
    app.add_handler(CommandHandler("pending",  cmd_pending))
    app.add_handler(CommandHandler("users",    cmd_users))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
