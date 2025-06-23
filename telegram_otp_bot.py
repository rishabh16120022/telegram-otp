# === Required Libraries ===
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import asyncio
from telethon import TelegramClient, events
import os, re, hmac, hashlib
from flask import Flask, request
import razorpay

# === Configuration ===
API_TOKEN = os.getenv("API_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME")
ACCOUNT_PRICE = 10  # This can stay hardcoded or be made env too
UPI_ID = os.getenv("UPI_ID")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# === Razorpay Setup ===
client_rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# === Database Setup ===
def init_db():
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, balance INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS purchases (
        user_id INTEGER,
        number TEXT,
        status TEXT,
        otp TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stock_log (
        phone TEXT PRIMARY KEY,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def get_balance(user_id):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 0

def add_balance(user_id, amount):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id, balance) VALUES (?, ?)", (user_id, 0))
    c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id))
    conn.commit()
    conn.close()

def deduct_balance(user_id, amount):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE id=?", (amount, user_id))
    conn.commit()
    conn.close()

def save_purchase(user_id, number):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("INSERT INTO purchases (user_id, number, status, otp) VALUES (?, ?, ?, ?)", (user_id, number, 'pending', ''))
    conn.commit()
    conn.close()

def get_pending_purchase(user_id):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("SELECT number, status FROM purchases WHERE user_id=? AND status='pending'", (user_id,))
    res = c.fetchone()
    conn.close()
    return res

def get_user_by_phone(phone):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM purchases WHERE number=? AND status='pending'", (phone,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else None

def set_otp(user_id, otp):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("UPDATE purchases SET otp=?, status='otp_received' WHERE user_id=? AND status='pending'", (otp, user_id))
    conn.commit()
    conn.close()

def cancel_purchase(user_id):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("UPDATE purchases SET status='cancelled' WHERE user_id=? AND status='pending'", (user_id,))
    conn.commit()
    conn.close()

def get_stock_summary():
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM stock_log")
    count = c.fetchone()[0]
    conn.close()
    return count

def add_to_stock(phone):
    with open("data/account_stock.txt", "a") as f:
        f.write(phone + "\n")
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO stock_log (phone) VALUES (?)", (phone,))
    conn.commit()
    conn.close()

# === OTP Listener ===
def set_otp_for_phone(phone, otp):
    user_id = get_user_by_phone(phone)
    if user_id:
        set_otp(user_id, otp)
        print(f"📲 OTP {otp} set for user {user_id} and number {phone}")

async def start_all_sessions():
    for session_file in os.listdir(SESSION_DIR):
        phone = session_file.replace(".session", "")
        client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH)

        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            text = event.raw_text
            match = re.search(r'(?:code is|Code:) (\d{5,6})', text)
            if match:
                otp = match.group(1)
                set_otp_for_phone(phone, otp)

        await client.start()
        print(f"✅ Listening for OTPs on {phone}")
        await client.run_until_disconnected()

# === Razorpay Webhook Server ===
rzp_app = Flask(__name__)

@rzp_app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    payload = request.data
    signature = request.headers.get("X-Razorpay-Signature")
    try:
        hmac_obj = hmac.new(WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256)
        if hmac.compare_digest(hmac_obj.hexdigest(), signature):
            data = request.json
            if data['event'] == 'payment.captured':
                payment = data['payload']['payment']['entity']
                amount = int(payment['amount']) // 100
                notes = payment['notes']
                user_id = int(notes.get("user_id"))
                add_balance(user_id, amount)
                print(f"✅ Added ₹{amount} to User ID {user_id}")
            return "", 200
    except Exception as e:
        print(f"Webhook Error: {e}")
        return "", 400

# === Telegram Bot Setup ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

main_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton("💰 Deposit", callback_data='deposit')],
    [InlineKeyboardButton("💼 My Balance", callback_data='balance')],
    [InlineKeyboardButton("📱 Get Account", callback_data='get_account')],
    [InlineKeyboardButton("👑 Owner", callback_data='owner')]
])

owner_panel = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton("🔐 Add Account", callback_data='add_account')],
    [InlineKeyboardButton("📦 Stock Summary", callback_data='stock')]
])

otp_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton("🔁 Get OTP", callback_data='get_otp')],
    [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
])

owner_login_state = {}

@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    if message.from_user.id == OWNER_ID:
        await message.answer("👑 Owner Panel", reply_markup=owner_panel)
    else:
        await message.answer("Welcome to Telegram OTP Bot!", reply_markup=main_menu)

@dp.callback_query_handler()
async def callback_handler(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data

    if data == 'deposit':
        amount = 100
        payment = client_rzp.order.create({
            "amount": amount * 100,
            "currency": "INR",
            "payment_capture": 1,
            "notes": {"user_id": str(user_id)}
        })
        pay_url = f"https://rzp.io/i/{payment['id']}"
        await callback_query.message.answer(f"💸 Pay ₹{amount} here:\n{pay_url}")

    elif data == 'balance':
        bal = get_balance(user_id)
        await callback_query.message.answer(f"💼 Your Balance: ₹{bal}")

    elif data == 'owner':
        await callback_query.message.answer(f"👑 Owner: {OWNER_USERNAME}")

    elif data == 'get_account':
        bal = get_balance(user_id)
        if bal < ACCOUNT_PRICE:
            await callback_query.message.answer("❌ Insufficient balance.")
            return
        with open("data/account_stock.txt", "r") as f:
            lines = f.readlines()
        if not lines:
            await callback_query.message.answer("📦 No account stock available.")
            return
        number = lines[0].strip()
        with open("data/account_stock.txt", "w") as f:
            f.writelines(lines[1:])
        deduct_balance(user_id, ACCOUNT_PRICE)
        save_purchase(user_id, number)
        await callback_query.message.answer(f"📞 Your Number: `{number}`", parse_mode='Markdown', reply_markup=otp_menu)

    elif data == 'get_otp':
        pending = get_pending_purchase(user_id)
        if pending:
            number, status = pending
            otp = "(Waiting for OTP...)"
            await callback_query.message.answer(f"🔁 Waiting for OTP on `{number}`", parse_mode='Markdown')
        else:
            await callback_query.message.answer("❌ No pending purchase found.")

    elif data == 'cancel':
        pending = get_pending_purchase(user_id)
        if pending:
            number, status = pending
            if status == 'pending':
                cancel_purchase(user_id)
                add_balance(user_id, ACCOUNT_PRICE)
                with open("data/account_stock.txt", "a") as f:
                    f.write(number + "\n")
                await callback_query.message.answer("✅ Purchase canceled. Amount refunded.")
            else:
                await callback_query.message.answer("❌ OTP already received. Cannot cancel.")
        else:
            await callback_query.message.answer("❌ No pending purchase found.")

    elif user_id == OWNER_ID and data == 'add_account':
        owner_login_state[user_id] = 'awaiting_phone'
        await callback_query.message.answer("📞 Send phone number to add (with +91)...")

    elif user_id == OWNER_ID and data == 'stock':
        count = get_stock_summary()
        await callback_query.message.answer(f"📦 Total Stock: {count} numbers")

@dp.message_handler(lambda msg: msg.from_user.id == OWNER_ID)
async def owner_add_account_step(message: types.Message):
    state = owner_login_state.get(message.from_user.id)
    if state == 'awaiting_phone':
        phone = message.text.strip()
        owner_login_state[message.from_user.id] = {'phone': phone}
        client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            await message.answer("📨 Code sent. Enter OTP:")
        else:
            await message.answer("✅ Already logged in.")
            add_to_stock(phone)
        await client.disconnect()
    elif isinstance(state, dict) and 'phone' in state:
        code = message.text.strip()
        phone = state['phone']
        client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH)
        await client.connect()
        try:
            await client.sign_in(phone, code)
            await message.answer(f"✅ Account {phone} logged in and added to stock.")
            add_to_stock(phone)
        except Exception as e:
            await message.answer(f"❌ Login failed: {e}")
        await client.disconnect()
        owner_login_state.pop(message.from_user.id, None)

@dp.message_handler(commands=['addbal'])
async def manual_add_balance(message: types.Message):
    try:
        amount = int(message.text.split()[1])
        add_balance(message.from_user.id, amount)
        await message.answer(f"✅ ₹{amount} added to your wallet.")
    except:
        await message.answer("❌ Usage: /addbal <amount>")

if __name__ == '__main__':
    init_db()
    executor.start_polling(dp, skip_updates=True)
