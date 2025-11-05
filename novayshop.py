#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NovayShop (Render Edition) — Bot Telegram + NOWPayments (BTC) auto-crédit
- Dépôt EUR → facture BTC via NOWPayments → webhook → crédit auto
- Boutique: Formations + FICHES/CC (menus)
- Serveur aiohttp pour /nowpayments (Render Web Service)
Usage légal uniquement.
"""

import asyncio
import logging
import os
import re
import secrets
import string
from datetime import datetime
from pathlib import Path

import aiosqlite
import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ---------------- Config ----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "0"))
MIN_DEPOSIT_EUR = int(os.getenv("MIN_DEPOSIT_EUR", "200"))
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "")
SUPPORT_URL = os.getenv("SUPPORT_URL", "")
CUSTOM_MIN_EUR = int(os.getenv("CUSTOM_MIN_EUR", "1"))
PORT = int(os.getenv("PORT", "8080"))  # Render fournit automatiquement PORT

# NOWPayments
NOWPAY_API_INVOICE = "https://api.nowpayments.io/v1/invoice"
NOWPAY_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "")
PAY_CCY = os.getenv("NOWPAYMENTS_PAY_CURRENCY", "btc").lower()
NP_SUCCESS = os.getenv("NOWPAYMENTS_SUCCESS_URL", "https://t.me/")
NP_CANCEL  = os.getenv("NOWPAYMENTS_CANCEL_URL", "https://t.me/")

DB_PATH = Path("data/novayshop.db")
PRODUCTS_DIR = Path("storage/products")
# Ensure folders exist at runtime (Render or local)
PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)

if not BOT_TOKEN:
    raise SystemExit("[CONFIG] BOT_TOKEN manquant dans .env")
if not ADMIN_ID:
    raise SystemExit("[CONFIG] ADMIN_ID manquant dans .env")
if not NOWPAY_API_KEY:
    raise SystemExit("[CONFIG] NOWPAYMENTS_API_KEY manquante dans .env")

logging.basicConfig(level=logging.INFO)

# ---------------- Helpers ----------------
def ref_code(n: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "NV-" + "".join(secrets.choice(alphabet) for _ in range(n))

def eurofmt(cents: int) -> str:
    return f"{cents/100:.2f}€"

async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                balance_cents INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount_eur_cents INTEGER,
                amount_btc TEXT,
                txid TEXT,
                ref TEXT UNIQUE,
                status TEXT,
                created_at TEXT,
                approved_at TEXT,
                admin_note TEXT
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                price_eur_cents INTEGER,
                file_path TEXT,
                delivery_text TEXT
            );
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                product_id INTEGER,
                paid_eur_cents INTEGER,
                created_at TEXT
            );
            """
        )
        await db.commit()

async def seed_defaults():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM products") as cur:
            n = (await cur.fetchone())[0]
        if n == 0:
            items = [
                ("FICHES (Custom Spend)", 0, None, ""),
                ("CC (Custom Spend)", 0, None, ""),
                ("FORMA ALLO", 19900, None, "Formation ALLO (contenu à définir)"),
                ("FORMA IPHONE", 24900, None, "Formation iPhone (contenu à définir)"),
                ("FORMA REFUND", 29900, None, "Formation Refund (contenu à définir)"),
                ("FORMA LUXE", 39900, None, "Formation Luxe (contenu à définir)"),
            ]
            for t, p, f, d in items:
                await db.execute(
                    "INSERT INTO products(title, price_eur_cents, file_path, delivery_text) VALUES(?,?,?,?)",
                    (t, p, f, d),
                )
            await db.commit()

async def get_balance_cents(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance_cents FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def adjust_balance(user_id: int, delta_cents: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, first_name, username, balance_cents, created_at)\n"
            "VALUES(?, '', '', 0, ?) ON CONFLICT(user_id) DO NOTHING",
            (user_id, datetime.utcnow().isoformat()),
        )
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id=?", (delta_cents, user_id))
        await db.commit()

# ---------------- FSM ----------------
class DepositFlow(StatesGroup):
    amount = State()

class AdjustFlow(StatesGroup):
    target = State()
    delta = State()

class CustomSpendFlow(StatesGroup):
    amount = State()

# ---------------- Routers ----------------
router = Router()
admin_router = Router()

# ---------------- Keyboards ----------------
def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Dépôt", callback_data="deposit")
    kb.button(text="🛒 Boutique", callback_data="shop")
    kb.button(text="📦 Achats", callback_data="orders")
    kb.button(text="💰 Solde", callback_data="balance")
    kb.button(text="🛟 Support", callback_data="support")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Retour", callback_data="home")]])

def support_kb() -> InlineKeyboardMarkup:
    rows = []
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton(text="Contacter le support", url=SUPPORT_URL)])
    elif SUPPORT_HANDLE:
        rows.append([InlineKeyboardButton(text=f"@{SUPPORT_HANDLE}", url=f"https://t.me/{SUPPORT_HANDLE}")])
    rows.append([InlineKeyboardButton(text="⬅️ Retour", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ---------------- NOWPayments helper ----------------
async def create_np_invoice(amount_eur: float, user_id: int, ref: str) -> str:
    payload = {
        "price_amount": float(amount_eur),
        "price_currency": "eur",
        "pay_currency": PAY_CCY,        # btc
        "order_id": f"{user_id}:{ref}", # user + ref
        "success_url": NP_SUCCESS,
        "cancel_url": NP_CANCEL,
    }
    headers = {"x-api-key": NOWPAY_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.post(NOWPAY_API_INVOICE, json=payload, headers=headers) as r:
            data = await r.json()
            if r.status >= 300 or "invoice_url" not in data:
                logging.error("NOWPayments error %s: %s", r.status, data)
                raise RuntimeError("NOWPayments invoice error")
            return data["invoice_url"]

# ---------------- Public ----------------
@router.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, first_name, username, balance_cents, created_at)\n"
            "VALUES(?, ?, ?, 0, ?)\n"
            "ON CONFLICT(user_id) DO UPDATE SET first_name=?, username=?",
            (
                m.from_user.id,
                m.from_user.first_name or "",
                m.from_user.username or "",
                datetime.utcnow().isoformat(),
                m.from_user.first_name or "",
                m.from_user.username or "",
            ),
        )
        await db.commit()

    await state.clear()
    welcome = (
        "⚡️ *NOVAYSHOP* ⚡️\n"
        "*le bot préféré de ton calleur préféré*\n\n"
        "Bienvenue sur l’interface la plus clean du game.\n"
        "Ici tu peux :\n"
        "💳 Créditer ton solde (Crypto)\n"
        "🛒 Acheter Formations / Fiches / CC\n"
        "🎧 Avoir le support direct\n"
        "💰 Gérer ton solde instantanément\n\n"
        "Sélectionne une action ci-dessous ↓"
    )
    await m.answer(welcome, reply_markup=main_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    await c.message.edit_text("Choisis une action :", reply_markup=main_kb())
    await c.answer()

@router.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery):
    text = "Support disponible ci-dessous."
    if not (SUPPORT_URL or SUPPORT_HANDLE):
        text = "Support indisponible pour le moment."
    await c.message.edit_text(text, reply_markup=support_kb())
    await c.answer()

@router.callback_query(F.data == "balance")
async def cb_balance(c: CallbackQuery):
    bal = await get_balance_cents(c.from_user.id)
    await c.message.edit_text(f"💰 Solde: *{eurofmt(bal)}*", parse_mode="Markdown", reply_markup=main_kb())
    await c.answer()

# ---- Dépôt → crée une facture BTC via NOWPayments -----
@router.callback_query(F.data == "deposit")
async def cb_deposit(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(DepositFlow.amount)
    await c.message.edit_text(
        f"⚡ *Dépôt*\nMin: *{MIN_DEPOSIT_EUR}€* — entre le *montant EUR* à créditer.",
        parse_mode="Markdown",
        reply_markup=back_home_kb(),
    )
    await c.answer()

@router.message(DepositFlow.amount)
async def deposit_amount(m: Message, state: FSMContext):
    txt = m.text or ""
    nums = re.findall(r"\d+[\.,]?\d*", txt)
    if not nums:
        return await m.reply("Montant EUR ? ex: 250")
    amount_eur = float(nums[0].replace(",", "."))
    if amount_eur < MIN_DEPOSIT_EUR:
        return await m.reply(f"Min {MIN_DEPOSIT_EUR}€.")

    ref = ref_code()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deposits(user_id, amount_eur_cents, amount_btc, txid, ref, status, created_at) "
            "VALUES(?, ?, '', '', ?, 'pending', ?)",
            (m.from_user.id, int(round(amount_eur*100)), ref, datetime.utcnow().isoformat()),
        )
        await db.commit()

    try:
        invoice_url = await create_np_invoice(amount_eur, m.from_user.id, ref)
    except Exception as e:
        logging.exception("NP invoice failed")
        return await m.reply("Désolé, création de la facture impossible pour le moment. Réessaie plus tard.")

    await m.answer(
        "✅ *Facture générée*\n"
        f"Paye en BTC via ce lien sécurisé :\n{invoice_url}\n\n"
        "_Le solde sera crédité automatiquement après confirmation._",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )
    await state.clear()

# ---------------- Boutique ----------------
@router.callback_query(F.data == "shop")
async def cb_shop(c: CallbackQuery):
    await seed_defaults()
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Fiches — Catégorie", callback_data="cat:fiches")
    kb.button(text="💳 CC — Catégorie", callback_data="cat:cc")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title, price_eur_cents FROM products ORDER BY id ASC") as cur:
            rows = await cur.fetchall()

    # Masquer les placeholders "(Custom Spend)"
    for pid, title, price_cents in rows:
        if "(Custom Spend)" in title:
            continue
        icon = "📘" if "FORMA" in title.upper() else "🧩"
        kb.button(text=f"{icon} {title} — {eurofmt(price_cents)}", callback_data=f"buy:{pid}")
    kb.button(text="⬅️ Retour", callback_data="home")
    kb.adjust(1)
    await c.message.edit_text("**Boutique** — sélectionne une option :", parse_mode="Markdown", reply_markup=kb.as_markup())
    await c.answer()

@router.callback_query(F.data == "cat:cc")
async def cat_cc(c: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    levels = ["Classic", "Gold", "Platinum", "Premier", "Infinite"]
    for lv in levels:
        kb.button(text=f"💳 {lv}", callback_data=f"cclevel:{lv.lower()}")
    kb.button(text="⬅️ Retour", callback_data="shop")
    kb.adjust(2, 2, 1)
    await c.message.edit_text("💳 *CC* — choisis un *niveau* :", parse_mode="Markdown", reply_markup=kb.as_markup())
    await c.answer()

@router.callback_query(F.data.startswith("cclevel:"))
async def cc_level_choice(c: CallbackQuery, state: FSMContext):
    level = c.data.split(":", 1)[1]
    await state.set_state(CustomSpendFlow.amount)
    await state.update_data(custom_category="CC", cc_level=level)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Retour", callback_data="cat:cc")
    await c.message.edit_text(f"💳 *CC* — niveau *{level.capitalize()}* sélectionné.\n💶 Entre combien d’€ tu veux dépenser.", parse_mode="Markdown", reply_markup=kb.as_markup())
    await c.answer()

@router.callback_query(F.data == "cat:fiches")
async def cat_fiches(c: CallbackQuery, state: FSMContext):
    banks = [
        ("BNP Paribas","bnp"),
        ("Crédit Agricole","ca"),
        ("Société Générale","sg"),
        ("LCL","lcl"),
        ("Crédit Mutuel","cm"),
        ("CIC","cic"),
        ("Banque Populaire","bp"),
        ("Caisse d'Épargne","cde"),
        ("La Banque Postale","lbp"),
        ("Boursorama","brs"),
    ]
    kb = InlineKeyboardBuilder()
    for name, code in banks:
        kb.button(text=f"📚 {name}", callback_data=f"bank:{code}")
    kb.button(text="⬅️ Retour", callback_data="shop")
    kb.adjust(1)
    await c.message.edit_text("📚 *Fiches* — choisis une *banque française* :", parse_mode="Markdown", reply_markup=kb.as_markup())
    await c.answer()

@router.callback_query(F.data.startswith("bank:"))
async def fiches_bank_choice(c: CallbackQuery, state: FSMContext):
    code = c.data.split(":", 1)[1]
    bank_map = {
        "bnp": "BNP Paribas",
        "ca": "Crédit Agricole",
        "sg": "Société Générale",
        "lcl": "LCL",
        "cm": "Crédit Mutuel",
        "cic": "CIC",
        "bp": "Banque Populaire",
        "cde": "Caisse d'Épargne",
        "lbp": "La Banque Postale",
        "brs": "Boursorama",
    }
    name = bank_map.get(code, code.upper())
    await state.set_state(CustomSpendFlow.amount)
    await state.update_data(custom_category="FICHES", bank=name)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Retour", callback_data="cat:fiches")
    await c.message.edit_text(f"📚 *Fiches* — *{name}* sélectionnée.\n💶 Entre combien d’€ tu veux dépenser.", parse_mode="Markdown", reply_markup=kb.as_markup())
    await c.answer()

@router.message(CustomSpendFlow.amount)
async def custom_spend_amount(m: Message, state: FSMContext, bot: Bot):
    txt = (m.text or "").strip()
    nums = re.findall(r"\d+[\.,]?\d*", txt)
    if not nums:
        return await m.reply("Montant EUR ? ex: 50")
    amount_eur = float(nums[0].replace(",", "."))
    if amount_eur < CUSTOM_MIN_EUR:
        return await m.reply(f"Min {CUSTOM_MIN_EUR}€.")

    cents = int(round(amount_eur * 100))
    bal = await get_balance_cents(m.from_user.id)
    if bal < cents:
        return await m.reply(f"Solde insuffisant (solde actuel {eurofmt(bal)}). Fais un dépôt.")

    data = await state.get_data()
    category = data.get("custom_category", "CUSTOM")
    cc_level = data.get("cc_level")
    bank = data.get("bank")
    await adjust_balance(m.from_user.id, -cents)

    # Enregistrer un achat "custom spend" (placeholder)
    prod_title = f"{category} (Custom Spend)"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM products WHERE title=?", (prod_title,)) as cur:
            row = await cur.fetchone()
        if row:
            pid = row[0]
        else:
            await db.execute("INSERT INTO products(title, price_eur_cents, file_path, delivery_text) VALUES(?,?,?,?)", (prod_title, 0, None, ""))
            await db.commit()
            async with db.execute("SELECT id FROM products WHERE title=?", (prod_title,)) as cur2:
                pid = (await cur2.fetchone())[0]

        await db.execute(
            "INSERT INTO purchases(user_id, product_id, paid_eur_cents, created_at) VALUES(?,?,?,?)",
            (m.from_user.id, pid, cents, datetime.utcnow().isoformat()),
        )
        await db.commit()

    # Notifier l'admin (canal ou DM)
    try:
        info_parts = []
        if cc_level: info_parts.append(f"niveau: {cc_level}")
        if bank: info_parts.append(f"banque: {bank}")
        extra = (" | ".join(info_parts)) if info_parts else ""
        info_txt = f" ({extra})" if extra else ""
        text_admin = f"🔔 *Custom spend* — {category}{info_txt}\nUser: `{m.from_user.id}` @{m.from_user.username or '-'}\nMontant: *{amount_eur:.2f}€*"
        if ADMIN_CHANNEL_ID:
            await bot.send_message(ADMIN_CHANNEL_ID, text_admin, parse_mode="Markdown")
        else:
            await bot.send_message(ADMIN_ID, text_admin, parse_mode="Markdown")
    except Exception as e:
        logging.error("Admin custom spend notify failed: %s", e)

    # Rediriger vers le support
    sk = InlineKeyboardBuilder()
    if SUPPORT_URL or SUPPORT_HANDLE:
        url = SUPPORT_URL or f"https://t.me/{SUPPORT_HANDLE}"
        sk.button(text="🎧 Ouvrir le support", url=url)
    await m.answer(
        f"✅ {category} crédité(s) pour *{amount_eur:.2f}€*.\nPasse en privé avec le *support* pour préciser exactement ce que tu veux.",
        parse_mode="Markdown",
        reply_markup=sk.as_markup() if (SUPPORT_URL or SUPPORT_HANDLE) else None,
    )
    await state.clear()

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT title, price_eur_cents, file_path, delivery_text FROM products WHERE id=?", (pid,)) as cur:
            row = await cur.fetchone()
    if not row:
        await c.answer("Produit introuvable", show_alert=True)
        return

    title, price_cents, file_path, delivery_text = row
    bal = await get_balance_cents(c.from_user.id)

    kb = InlineKeyboardBuilder()
    if bal >= price_cents:
        kb.button(text="Payer", callback_data=f"pay:{pid}")
    kb.button(text="⬅️ Retour", callback_data="shop")

    txt = f"*{title}*\nPrix: {eurofmt(price_cents)}\nSolde: {eurofmt(bal)}\n" + ("OK pour payer." if bal >= price_cents else "Solde insuffisant.")
    await c.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb.as_markup())
    await c.answer()

@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery, bot: Bot):
    pid = int(c.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT title, price_eur_cents, file_path, delivery_text FROM products WHERE id=?", (pid,)) as cur:
            row = await cur.fetchone()
        if not row:
            await c.answer("Produit introuvable", show_alert=True)
            return
        title, price_cents, file_path, delivery_text = row

    bal = await get_balance_cents(c.from_user.id)
    if bal < price_cents:
        await c.answer("Solde insuffisant", show_alert=True)
        return

    await adjust_balance(c.from_user.id, -price_cents)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO purchases(user_id, product_id, paid_eur_cents, created_at) VALUES(?,?,?,?)",
            (c.from_user.id, pid, price_cents, datetime.utcnow().isoformat()),
        )
        await db.commit()

    delivered = False
    if file_path:
        fp = Path(file_path)
        if not fp.is_absolute():
            fp = PRODUCTS_DIR / fp
        if fp.exists():
            await bot.send_document(c.from_user.id, FSInputFile(fp), caption=f"Merci — {title}")
            delivered = True
    if delivery_text and not delivered:
        await bot.send_message(c.from_user.id, delivery_text)
        delivered = True

    if "FORMA" in title.upper():
        sk = InlineKeyboardBuilder()
        if SUPPORT_URL or SUPPORT_HANDLE:
            url = SUPPORT_URL or f"https://t.me/{SUPPORT_HANDLE}"
            sk.button(text="🎧 Ouvrir le support", url=url)
        await bot.send_message(
            c.from_user.id,
            "🎓 Ta *formation* est prête. Contacte le *support* pour la réception et l'accès.",
            parse_mode="Markdown",
            reply_markup=sk.as_markup() if (SUPPORT_URL or SUPPORT_HANDLE) else None,
        )

    await c.message.edit_text("✅ Achat confirmé. Regarde tes messages.", reply_markup=main_kb())
    await c.answer()

@router.callback_query(F.data == "orders")
async def cb_orders(c: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT p.title, pu.paid_eur_cents, pu.created_at FROM purchases pu JOIN products p ON p.id=pu.product_id WHERE pu.user_id=? ORDER BY pu.id DESC LIMIT 10",
            (c.from_user.id,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await c.message.edit_text("Aucun achat.", reply_markup=back_home_kb())
        await c.answer()
        return

    lines = [f"• {t} — {eurofmt(pc)} — {d[:16]}" for (t, pc, d) in rows]
    await c.message.edit_text("\n".join(lines), reply_markup=back_home_kb())
    await c.answer()

# ---------------- Admin ----------------
@admin_router.message(Command("admin"))
async def admin_panel(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.reply("Accès refusé.")
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Dépôts en attente", callback_data="admin:pending")
    kb.button(text="✏️ Ajuster solde", callback_data="admin:adjust")
    kb.adjust(1)
    await m.answer("⚡ *Admin*", reply_markup=kb.as_markup(), parse_mode="Markdown")

@admin_router.callback_query(F.data == "admin:pending")
async def admin_pending(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Non.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, amount_eur_cents, ref, txid, created_at, status FROM deposits WHERE status='pending' ORDER BY id ASC LIMIT 15"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await c.message.edit_text("Aucun dépôt en attente.", reply_markup=back_home_kb())
        return await c.answer()

    text = ["Dépôts en attente:"]
    for (uid, amt, ref, txid, created, status) in rows:
        text.append(f"• {uid} | {eurofmt(amt)} | ref:{ref} | {created[:16]} | {status}")
    await c.message.edit_text("\n".join(text), reply_markup=back_home_kb())
    await c.answer()

@admin_router.callback_query(F.data == "admin:adjust")
async def admin_adjust(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Non.")
    await state.set_state(AdjustFlow.target)
    await c.message.edit_text("ID utilisateur ?", reply_markup=back_home_kb())

@admin_router.message(AdjustFlow.target)
async def admin_adjust_target(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    import re as _re
    uid = _re.sub(r"\D", "", m.text or "")
    if not uid:
        return await m.reply("ID invalide")
    await state.update_data(target=int(uid))
    await state.set_state(AdjustFlow.delta)
    await m.answer("Montant en EUR (ex: +50 ou -10) ?")

@admin_router.message(AdjustFlow.delta)
async def admin_adjust_delta(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    import re as _re
    nums = _re.findall(r"-?\d+[\.,]?\d*", m.text or "")
    if not nums:
        return await m.reply("Montant invalide")
    delta_eur = float(nums[0].replace(",", "."))
    data = await state.get_data()
    uid = int(data.get("target"))
    await adjust_balance(uid, int(round(delta_eur * 100)))
    await state.clear()
    await m.answer("Solde ajusté.")

# ---------------- Webhook server (aiohttp) ----------------
bot_global: Bot | None = None

async def handle_nowpayments_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)

    status = str(data.get("payment_status", "")).lower()
    order_id = str(data.get("order_id", ""))  # "user_id:ref"
    price_amount = data.get("price_amount")

    if not order_id or price_amount is None:
        return web.json_response({"ok": True})

    if status in {"confirmed", "finished"}:
        try:
            user_id_str, ref = order_id.split(":", 1)
            user_id = int(user_id_str)
        except Exception:
            return web.json_response({"ok": True})

        cents = int(round(float(price_amount) * 100))

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT status FROM deposits WHERE ref=?", (ref,)) as cur:
                row = await cur.fetchone()
            if not row or row[0] != "pending":
                return web.json_response({"ok": True})
            await db.execute("UPDATE deposits SET status='approved', approved_at=? WHERE ref=?",
                             (datetime.utcnow().isoformat(), ref))
            await db.commit()

        await adjust_balance(user_id, cents)
        try:
            if bot_global:
                await bot_global.send_message(user_id, f"✅ Dépôt confirmé : +{eurofmt(cents)}")
        except Exception as e:
            logging.error("Notify user failed: %s", e)

    return web.json_response({"ok": True})

async def start_web_server():
    app = web.Application()
    app.router.add_post("/nowpayments", handle_nowpayments_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Webhook server started on port %s", PORT)

# ---------------- App ----------------
async def main():
    await init_db()
    await seed_defaults()

    global bot_global
    bot_global = Bot(BOT_TOKEN)

    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(admin_router)

    # Lancer le serveur webhook et le bot en parallèle
    await start_web_server()
    await dp.start_polling(bot_global)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("NovayShop stopped.")
