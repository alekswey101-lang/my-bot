import logging
import random
import json
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)
import google.generativeai as genai

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

TOKEN      = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))

# ── Gemini AI ─────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=(
        "Ты помощник в игровом Telegram боте. "
        "Отвечай коротко, по-русски, дружелюбно. "
        "Можешь давать советы по играм (слоты, рулетка, сапёр, кубик). "
        "Не используй Markdown разметку в ответах."
    )
)

# ── База данных ───────────────────────────────────────────────────────────────
DB_FILE = "users.json"

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_db(db: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def get_user(user_id: int) -> dict:
    db = load_db()
    uid = str(user_id)
    if uid not in db:
        db[uid] = {
            "name": "Игрок",
            "balance": 100,
            "wins": 0,
            "losses": 0,
            "games_played": 0,
            "last_bonus": None,
            "banned": False,
        }
        save_db(db)
    return db[uid]

def update_user(user_id: int, data: dict):
    db = load_db()
    uid = str(user_id)
    db[uid].update(data)
    save_db(db)

def check_ban(user_id: int) -> bool:
    return get_user(user_id).get("banned", False)

# ── Сапёр ─────────────────────────────────────────────────────────────────────
BOARD_SIZE = 5
BOMB_COUNT = 5

def generate_board():
    board = [["safe"] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    for b in random.sample(range(BOARD_SIZE * BOARD_SIZE), BOMB_COUNT):
        r, c = divmod(b, BOARD_SIZE)
        board[r][c] = "bomb"
    return board

def build_game_keyboard(board, revealed: set, game_over: bool) -> InlineKeyboardMarkup:
    buttons = []
    for r in range(BOARD_SIZE):
        row = []
        for c in range(BOARD_SIZE):
            idx = r * BOARD_SIZE + c
            if idx in revealed:
                label = "💣" if board[r][c] == "bomb" else "✅"
            else:
                label = ("💣" if board[r][c] == "bomb" else "💰") if game_over else "⬛"
            cb = f"mine_{r}_{c}" if not game_over and idx not in revealed else "noop"
            row.append(InlineKeyboardButton(label, callback_data=cb))
        buttons.append(row)
    if not game_over:
        buttons.append([InlineKeyboardButton("💵 Забрать выигрыш", callback_data="mine_cashout")])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)

# ── Клавиатуры ────────────────────────────────────────────────────────────────
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Профиль",    callback_data="profile"),
         InlineKeyboardButton("💰 Баланс",     callback_data="balance")],
        [InlineKeyboardButton("🎮 Мини-игры",  callback_data="minigames")],
        [InlineKeyboardButton("🤖 Спросить ИИ", callback_data="ai_help")],
    ])

def minigames_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💣 Мины",         callback_data="info_mines"),
         InlineKeyboardButton("🎲 Кубик",        callback_data="info_dice")],
        [InlineKeyboardButton("🎰 Слоты",        callback_data="info_slots"),
         InlineKeyboardButton("🎡 Рулетка",      callback_data="info_roulette")],
        [InlineKeyboardButton("3️⃣ Тройка",       callback_data="info_triple"),
         InlineKeyboardButton("🃏 Карта",        callback_data="info_card")],
        [InlineKeyboardButton("🔙 Назад",        callback_data="main_menu")],
    ])

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]])

def play_again_keyboard(game_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Играть снова", callback_data=game_cb)],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])

# ── Вспомогательные ───────────────────────────────────────────────────────────
def parse_bet(args, user_id: int):
    u = get_user(user_id)
    if not args:
        return None, "❌ Укажи ставку. Пример: `/slot 100`"
    try:
        bet = int(args[-1])
    except ValueError:
        return None, "❌ Ставка должна быть числом."
    if bet <= 0:
        return None, "❌ Ставка должна быть больше 0."
    if bet > u["balance"]:
        return None, f"❌ Недостаточно монет! Баланс: *{u['balance']}*"
    return bet, None

def apply_result(user_id: int, bet: int, won: bool, multiplier: float = 2.0):
    u = get_user(user_id)
    if won:
        win = int(bet * multiplier)
        update_user(user_id, {
            "balance": u["balance"] + win,
            "wins": u["wins"] + 1,
            "games_played": u["games_played"] + 1,
        })
        return win, u["balance"] + win
    else:
        update_user(user_id, {
            "balance": u["balance"] - bet,
            "losses": u["losses"] + 1,
            "games_played": u["games_played"] + 1,
        })
        return 0, u["balance"] - bet

# ── КОМАНДЫ ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if check_ban(user.id):
        await update.message.reply_text("🚫 Вы заблокированы.")
        return
    get_user(user.id)
    update_user(user.id, {"name": user.first_name})
    await update.message.reply_text(
        f"👋 Привет, *{user.first_name}*!\n\n"
        f"Добро пожаловать в игровой бот!\n"
        f"Тебе выдано *100 монет* для старта 🪙\n\n"
        f"🎮 *Игры:*\n"
        f"• `/mines 100` — 💣 Сапёр\n"
        f"• `/dice 4 100` — 🎲 Кубик\n"
        f"• `/slot 100` — 🎰 Слоты\n"
        f"• `/roulette 100` → `/spin` — 🎡 Рулетка\n"
        f"• `/triple 100` — 3️⃣ Тройка\n"
        f"• `/card 1 100` — 🃏 Угадай карту\n\n"
        f"🤖 `/ai [вопрос]` — спросить ИИ\n"
        f"💰 `/bonus` — ежедневный бонус\n",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    u = get_user(user_id)
    last_bonus = u.get("last_bonus")
    if last_bonus:
        last_time = datetime.fromisoformat(last_bonus)
        next_time = last_time + timedelta(hours=24)
        if datetime.now() < next_time:
            remaining = next_time - datetime.now()
            h, rem = divmod(int(remaining.total_seconds()), 3600)
            m = rem // 60
            await update.message.reply_text(
                f"⏳ Бонус уже получен!\nСледующий через: *{h}ч {m}мин*",
                parse_mode="Markdown"
            )
            return
    new_balance = u["balance"] + 50
    update_user(user_id, {"balance": new_balance, "last_bonus": datetime.now().isoformat()})
    await update.message.reply_text(
        f"🎁 Вы получили *50* монет!\nБаланс: *{new_balance}* монет 🪙\n\n_Следующий бонус через 24 часа_",
        parse_mode="Markdown", reply_markup=main_menu_keyboard(),
    )

# 🤖 ИИ
async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text(
            "🤖 Напиши вопрос после команды!\n\nПример: `/ai как выиграть в рулетку?`",
            parse_mode="Markdown"
        )
        return

    thinking = await update.message.reply_text("🤖 Думаю...")
    try:
        response = gemini_model.generate_content(text)
        answer = response.text[:4000]
        await thinking.edit_text(f"🤖 *ИИ отвечает:*\n\n{answer}", parse_mode="Markdown")
    except Exception as e:
        await thinking.edit_text("❌ Ошибка ИИ. Попробуй позже.")

# 💣 Мины
async def game_mines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    bet, err = parse_bet(context.args, user_id)
    if err:
        await update.message.reply_text(err + "\nПример: `/mines 100`", parse_mode="Markdown")
        return
    board = generate_board()
    context.bot_data.setdefault("sessions", {})[user_id] = {
        "board": board, "bet": bet, "revealed": set(),
    }
    update_user(user_id, {"balance": get_user(user_id)["balance"] - bet})
    safe = BOARD_SIZE * BOARD_SIZE - BOMB_COUNT
    await update.message.reply_text(
        f"💣 *Сапёр!*\n\nСтавка: *{bet}* монет\nОткрой все *{safe}* клеток → *{bet*2}* монет!",
        parse_mode="Markdown",
        reply_markup=build_game_keyboard(board, set(), False),
    )

# 🎲 Кубик
async def game_dice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    args = context.args
    u = get_user(user_id)
    if len(args) == 2:
        try:
            guess, bet = int(args[0]), int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Формат: `/dice 4 100`", parse_mode="Markdown"); return
        if not 1 <= guess <= 6:
            await update.message.reply_text("❌ Число от 1 до 6."); return
        if bet <= 0 or bet > u["balance"]:
            await update.message.reply_text(f"❌ Неверная ставка. Баланс: *{u['balance']}*", parse_mode="Markdown"); return
        roll = random.randint(1, 6)
        emoji = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣"][roll-1]
        win, nb = apply_result(user_id, bet, roll == guess, 5.0)
        text = f"🎲 Выпало: {emoji}\n\n" + (f"🎉 *Угадал! +{win} монет (×5)*\nБаланс: *{nb}*" if win else f"😢 Не угадал (ставил {guess})\nПотеряно: *{bet}*. Баланс: *{nb}*")
    elif len(args) == 1:
        try:
            bet = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ Формат: `/dice 100`", parse_mode="Markdown"); return
        if bet <= 0 or bet > u["balance"]:
            await update.message.reply_text(f"❌ Неверная ставка.", parse_mode="Markdown"); return
        roll = random.randint(1, 6)
        emoji = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣"][roll-1]
        win, nb = apply_result(user_id, bet, roll >= 4, 1.8)
        text = f"🎲 Выпало: {emoji}\n\n" + (f"🎉 *Выиграл {win} монет! (×1.8)*\nБаланс: *{nb}*" if win else f"😢 Не повезло!\nПотеряно: *{bet}*. Баланс: *{nb}*")
    else:
        await update.message.reply_text("🎲 `/dice [1-6] [ставка]` или `/dice [ставка]`", parse_mode="Markdown"); return
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=play_again_keyboard("info_dice"))

# 🎰 Слоты
SLOT_SYMBOLS = ["🍒","🍋","🍊","🍇","⭐","💎"]

async def game_slots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    bet, err = parse_bet(context.args, user_id)
    if err:
        await update.message.reply_text(err + "\nПример: `/slot 100`", parse_mode="Markdown"); return
    update_user(user_id, {"balance": get_user(user_id)["balance"] - bet})
    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    line = " ".join(reels)
    u = get_user(user_id)
    if reels[0] == reels[1] == reels[2]:
        mult = 5.0 if reels[0] == "💎" else 3.0
        win = int(bet * mult)
        nb = u["balance"] + win
        update_user(user_id, {"balance": nb, "wins": u["wins"]+1, "games_played": u["games_played"]+1})
        text = f"🎰 {line}\n\n🎉 *ДЖЕКПОТ! +{win} монет (×{mult})!*\nБаланс: *{nb}*"
    elif len(set(reels)) == 2:
        win = int(bet * 1.6)
        nb = u["balance"] + win
        update_user(user_id, {"balance": nb, "wins": u["wins"]+1, "games_played": u["games_played"]+1})
        text = f"🎰 {line}\n\n🎉 *Выиграл {win} монет! (×1.6)*\nБаланс: *{nb}*"
    else:
        update_user(user_id, {"losses": u["losses"]+1, "games_played": u["games_played"]+1})
        text = f"🎰 {line}\n\n😢 *Проиграл {bet} монет.*\nБаланс: *{u['balance']}*"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=play_again_keyboard("info_slots"))

# 🎡 Рулетка
async def game_roulette_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    bet, err = parse_bet(context.args, user_id)
    if err:
        await update.message.reply_text(err + "\nПример: `/roulette 100`", parse_mode="Markdown"); return
    context.user_data["roulette_bet"] = bet
    update_user(user_id, {"balance": get_user(user_id)["balance"] - bet})
    await update.message.reply_text(
        f"🎡 *Рулетка*\n\nСтавка: *{bet}* монет\nНапиши `/spin` чтобы крутить!",
        parse_mode="Markdown"
    )

async def game_roulette_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    bet = context.user_data.get("roulette_bet")
    if not bet:
        await update.message.reply_text("❌ Сначала: `/roulette 100`", parse_mode="Markdown"); return
    context.user_data.pop("roulette_bet", None)
    pool = ([("🔴 Красное", 2.0)] * 14 + [("⚫ Чёрное", 2.0)] * 14 +
            [("🟢 Зелёное", 14.0)] * 1 + [("💀 Банкрот", 0.0)] * 3)
    name, mult = random.choice(pool)
    u = get_user(user_id)
    if mult == 0:
        update_user(user_id, {"losses": u["losses"]+1, "games_played": u["games_played"]+1})
        text = f"🎡 Выпало: *{name}*\n\n😢 Потеряно *{bet}* монет!\nБаланс: *{u['balance']}*"
    else:
        win = int(bet * mult)
        nb = u["balance"] + win
        update_user(user_id, {"balance": nb, "wins": u["wins"]+1, "games_played": u["games_played"]+1})
        text = f"🎡 Выпало: *{name}*\n\n🎉 Выиграл *{win}* монет! (×{mult})\nБаланс: *{nb}*"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=play_again_keyboard("info_roulette"))

# 3️⃣ Тройка
async def game_triple_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    bet, err = parse_bet(context.args, user_id)
    if err:
        await update.message.reply_text(err + "\nПример: `/triple 100`", parse_mode="Markdown"); return
    dice = [random.randint(1, 6) for _ in range(3)]
    total = sum(dice)
    emoji = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣"]
    ds = " ".join(emoji[d-1] for d in dice)
    win, nb = apply_result(user_id, bet, total >= 11, 1.9)
    text = (f"3️⃣ {ds}\nСумма: *{total}*\n\n" +
            (f"🎉 Выиграл *{win}* монет! (×1.9)\nБаланс: *{nb}*" if win else f"😢 Потеряно *{bet}* монет.\nБаланс: *{nb}*"))
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=play_again_keyboard("info_triple"))

# 🃏 Карта
CARDS = {1:"Туз (A)",2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",10:"10",11:"Валет (J)",12:"Дама (Q)",13:"Король (K)"}

async def game_card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if check_ban(user_id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("🃏 Формат: `/card [1-13] [ставка]`\nПример: `/card 1 100`", parse_mode="Markdown"); return
    try:
        guess, bet = int(args[0]), int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Формат: `/card 1 100`", parse_mode="Markdown"); return
    if not 1 <= guess <= 13:
        await update.message.reply_text("❌ Число от 1 до 13."); return
    u = get_user(user_id)
    if bet <= 0 or bet > u["balance"]:
        await update.message.reply_text(f"❌ Неверная ставка. Баланс: *{u['balance']}*", parse_mode="Markdown"); return
    card = random.randint(1, 13)
    win, nb = apply_result(user_id, bet, card == guess, 13.0)
    text = (f"🃏 Загадана *{CARDS[card]}*\n\n" +
            (f"🎉 Угадал! *+{win} монет (×13)*\nБаланс: *{nb}*" if win else f"😢 Не угадал (ставил {CARDS[guess]})\nПотеряно: *{bet}*. Баланс: *{nb}*"))
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=play_again_keyboard("info_card"))

# ── АДМИН ─────────────────────────────────────────────────────────────────────
def is_admin(uid): return uid == ADMIN_ID

async def admin_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа."); return
    try:
        tid, amount = int(context.args[0]), int(context.args[1])
    except:
        await update.message.reply_text("⚠️ /give <id> <сумма>"); return
    nb = get_user(tid)["balance"] + amount
    update_user(tid, {"balance": nb})
    await update.message.reply_text(f"✅ Выдано *{amount}* → `{tid}`. Баланс: *{nb}*", parse_mode="Markdown")

async def admin_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа."); return
    try:
        tid, amount = int(context.args[0]), int(context.args[1])
    except:
        await update.message.reply_text("⚠️ /take <id> <сумма>"); return
    nb = max(0, get_user(tid)["balance"] - amount)
    update_user(tid, {"balance": nb})
    await update.message.reply_text(f"✅ Забрано *{amount}* у `{tid}`. Баланс: *{nb}*", parse_mode="Markdown")

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа."); return
    try:
        tid = int(context.args[0])
    except:
        await update.message.reply_text("⚠️ /ban <id>"); return
    get_user(tid); update_user(tid, {"banned": True})
    await update.message.reply_text(f"🚫 `{tid}` заблокирован.", parse_mode="Markdown")

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа."); return
    try:
        tid = int(context.args[0])
    except:
        await update.message.reply_text("⚠️ /unban <id>"); return
    update_user(tid, {"banned": False})
    await update.message.reply_text(f"✅ `{tid}` разблокирован.", parse_mode="Markdown")

async def admin_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа."); return
    db = load_db()
    if not db:
        await update.message.reply_text("Нет игроков."); return
    lines = ["👥 *Игроки:*\n"]
    for uid, u in db.items():
        s = "🚫" if u.get("banned") else "✅"
        lines.append(f"{s} `{uid}` {u.get('name','?')} | 💰{u['balance']} | 🎮{u['games_played']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Нет доступа."); return
    try:
        tid = int(context.args[0])
    except:
        await update.message.reply_text("⚠️ /reset <id>"); return
    update_user(tid, {"wins": 0, "losses": 0, "games_played": 0})
    await update.message.reply_text(f"✅ Статистика `{tid}` сброшена.", parse_mode="Markdown")

# ── КНОПКИ ────────────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    user_id = user.id

    if check_ban(user_id):
        await query.edit_message_text("🚫 Вы заблокированы.")
        return

    if data == "noop":
        return

    elif data == "main_menu":
        await query.edit_message_text("🏠 *Главное меню*\nВыбери действие:",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())

    elif data == "profile":
        u = get_user(user_id)
        await query.edit_message_text(
            f"👤 *Профиль*\n\n🔹 {user.first_name}\n"
            f"💰 Баланс: *{u['balance']}*\n🎮 Игр: {u['games_played']}\n"
            f"✅ Побед: {u['wins']}\n❌ Поражений: {u['losses']}",
            parse_mode="Markdown", reply_markup=back_keyboard())

    elif data == "balance":
        u = get_user(user_id)
        await query.edit_message_text(
            f"💰 *Баланс*\n\nУ вас: *{u['balance']}* монет 🪙\n\n/bonus — бонус раз в 24ч",
            parse_mode="Markdown", reply_markup=back_keyboard())

    elif data == "minigames":
        await query.edit_message_text(
            "🎮 *Мини-игры*\n\n"
            "• `/mines 100` • `/dice 4 100` • `/slot 100`\n"
            "• `/roulette 100` → `/spin` • `/triple 100` • `/card 1 100`",
            parse_mode="Markdown", reply_markup=minigames_keyboard())

    elif data == "ai_help":
        await query.edit_message_text(
            "🤖 *ИИ помощник*\n\n"
            "Используй команду `/ai [вопрос]`\n\n"
            "Примеры:\n"
            "• `/ai как выиграть в слоты?`\n"
            "• `/ai объясни правила сапёра`\n"
            "• `/ai какая игра выгоднее?`",
            parse_mode="Markdown", reply_markup=back_keyboard())

    elif data.startswith("info_"):
        hints = {
            "info_mines":    "💣 `/mines [ставка]` — Сапёр (×2)",
            "info_dice":     "🎲 `/dice [1-6] [ставка]` — угадай (×5)\n`/dice [ставка]` — высокое/низкое (×1.8)",
            "info_slots":    "🎰 `/slot [ставка]` — Слоты (×1.6 / ×3 / ×5)",
            "info_roulette": "🎡 `/roulette [ставка]` → `/spin`",
            "info_triple":   "3️⃣ `/triple [ставка]` — сумма ≥11 (×1.9)",
            "info_card":     "🃏 `/card [1-13] [ставка]` — угадай (×13)",
        }
        await query.edit_message_text(hints.get(data, "?"),
            parse_mode="Markdown", reply_markup=back_keyboard())

    # ── Сапёр ──────────────────────────────────────────────────────────────
    elif data.startswith("mine_") and data != "mine_cashout":
        parts = data.split("_")
        r, c = int(parts[1]), int(parts[2])
        sessions = context.bot_data.get("sessions", {})
        if user_id not in sessions:
            await query.answer("Игра не найдена. /mines 100", show_alert=True); return
        session = sessions[user_id]
        board, bet, revealed = session["board"], session["bet"], session["revealed"]
        idx = r * BOARD_SIZE + c
        if idx in revealed: return
        revealed.add(idx)
        if board[r][c] == "bomb":
            u = get_user(user_id)
            update_user(user_id, {"losses": u["losses"]+1, "games_played": u["games_played"]+1})
            del sessions[user_id]
            await query.edit_message_text(
                f"💥 *БОМБА!*\n\nСтавка *{bet}* сгорела 😢\nБаланс: *{get_user(user_id)['balance']}*",
                parse_mode="Markdown", reply_markup=build_game_keyboard(board, revealed, True))
            return
        safe = BOARD_SIZE * BOARD_SIZE - BOMB_COUNT
        if len(revealed) == safe:
            win = bet * 2
            u = get_user(user_id)
            nb = u["balance"] + win
            update_user(user_id, {"balance": nb, "wins": u["wins"]+1, "games_played": u["games_played"]+1})
            del sessions[user_id]
            await query.edit_message_text(
                f"🏆 *ПОБЕДА!*\n\nВыигрыш: *{win}* монет (×2) 🎉\nБаланс: *{nb}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎮 Снова", callback_data="info_mines")],
                    [InlineKeyboardButton("🏠 Меню", callback_data="main_menu")],
                ]))
            return
        session["revealed"] = revealed
        await query.edit_message_text(
            f"💣 Ставка: *{bet}* → выигрыш *{bet*2}*\nОткрыто: *{len(revealed)}* / {safe}",
            parse_mode="Markdown", reply_markup=build_game_keyboard(board, revealed, False))

    elif data == "mine_cashout":
        sessions = context.bot_data.get("sessions", {})
        if user_id not in sessions:
            await query.answer("Игра не найдена.", show_alert=True); return
        session = sessions[user_id]
        bet, revealed = session["bet"], session["revealed"]
        safe = BOARD_SIZE * BOARD_SIZE - BOMB_COUNT
        win = int(bet + bet * (len(revealed) / safe)) if revealed else bet
        u = get_user(user_id)
        nb = u["balance"] + win
        update_user(user_id, {"balance": nb, "wins": u["wins"]+1, "games_played": u["games_played"]+1})
        del sessions[user_id]
        await query.edit_message_text(
            f"💵 *Выигрыш забран!*\n\nОткрыто: *{len(revealed)}* из {safe}\nПолучено: *{win}* монет\nБаланс: *{nb}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎮 Снова", callback_data="info_mines")],
                [InlineKeyboardButton("🏠 Меню", callback_data="main_menu")],
            ]))

# ── ЗАПУСК ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("bonus",    bonus))
    app.add_handler(CommandHandler("ai",       ai_cmd))
    app.add_handler(CommandHandler("mines",    game_mines_cmd))
    app.add_handler(CommandHandler("dice",     game_dice_cmd))
    app.add_handler(CommandHandler("slot",     game_slots_cmd))
    app.add_handler(CommandHandler("roulette", game_roulette_cmd))
    app.add_handler(CommandHandler("spin",     game_roulette_spin))
    app.add_handler(CommandHandler("triple",   game_triple_cmd))
    app.add_handler(CommandHandler("card",     game_card_cmd))
    app.add_handler(CommandHandler("give",     admin_give))
    app.add_handler(CommandHandler("take",     admin_take))
    app.add_handler(CommandHandler("ban",      admin_ban))
    app.add_handler(CommandHandler("unban",    admin_unban))
    app.add_handler(CommandHandler("players",  admin_players))
    app.add_handler(CommandHandler("reset",    admin_reset))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ Бот с ИИ запущен!")
    app.run_polling()
