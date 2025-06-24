# === Required Libraries ===
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import sqlite3
import asyncio
from telethon import TelegramClient, events
import os, re, hmac, hashlib
from io import BytesIO
import qrcode

from flask import Flask, request
import razorpay

# === Configuration ===
def check_env_vars():
    required_vars = ["API_TOKEN", "API_ID", "API_HASH", "OWNER_ID", "OWNER_USERNAME"]
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        print("Please set these environment variables in the Secrets tab")
        exit(1)

check_env_vars()

API_TOKEN = os.getenv("API_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME")
ACCOUNT_PRICE = 45  # This can stay hardcoded or be made env too
UPI_ID = os.getenv("UPI_ID")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# === Razorpay Setup (Optional) ===
client_rzp = None
if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
    client_rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    print("✅ Razorpay client initialized")
else:
    print("⚠️ Razorpay credentials not found - webhook functionality disabled")

# === Database Setup ===
def init_db():
    os.makedirs("data", exist_ok=True)
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
    c.execute('''CREATE TABLE IF NOT EXISTS utr_requests (
        user_id INTEGER,
        utr TEXT,
        amount INTEGER,
        status TEXT,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    conn = sqlite3.connect("data/users.db", timeout=10)
    try:
        c = conn.cursor()
        c.execute("SELECT user_id FROM purchases WHERE number=? AND status='pending' ORDER BY rowid DESC LIMIT 1", (phone,))
        res = c.fetchone()
        return res[0] if res else None
    finally:
        conn.close()

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
    os.makedirs("data", exist_ok=True)
    with open("data/account_stock.txt", "a") as f:
        f.write(phone + "\n")
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO stock_log (phone) VALUES (?)", (phone,))
    conn.commit()
    conn.close()

def set_otp_for_phone(phone, otp):
    user_id = get_user_by_phone(phone)
    if user_id:
        conn = sqlite3.connect("data/users.db", timeout=10)
        try:
            c = conn.cursor()
            c.execute("UPDATE purchases SET otp=?, status='otp_received' WHERE user_id=? AND number=? AND status='pending'", (otp, user_id, phone))
            conn.commit()
            print(f"📲 OTP {otp} set for user {user_id} and number {phone}")
            
            # Notify user immediately
            asyncio.create_task(notify_user_otp_received(user_id, phone, otp))
        except Exception as e:
            print(f"Error setting OTP: {e}")
        finally:
            conn.close()

async def notify_user_otp_received(user_id, phone, otp):
    """Notify user immediately when OTP is received"""
    try:
        await bot.send_message(
            user_id,
            f"📱 **OTP Received!**\n"
            f"📞 Number: `{phone}`\n"
            f"🔢 OTP: `{otp}`\n\n"
            f"✅ Use this OTP for your verification.\n"
            f"🔒 Account will be automatically logged out after use.",
            parse_mode='Markdown'
        )
        
        # Auto logout after 5 minutes of OTP being received
        asyncio.create_task(auto_logout_after_delay(phone, 300))  # 5 minutes delay
        
    except Exception as e:
        print(f"Failed to notify user {user_id}: {e}")

async def auto_logout_after_delay(phone, delay):
    """Automatically logout session after delay"""
    await asyncio.sleep(delay)
    await logout_session(phone)
    print(f"🔒 Auto-logged out {phone} after {delay} seconds")

async def logout_session(phone):
    """Logout and disconnect a specific phone session"""
    try:
        # Disconnect active listener if exists
        if phone in active_listeners:
            client = active_listeners[phone]
            if client.is_connected():
                await client.log_out()
                await client.disconnect()
            del active_listeners[phone]
            print(f"🔒 Disconnected active listener for {phone}")
        
        # Also logout from session file
        session_path = f"{SESSION_DIR}/{phone}.session"
        if os.path.exists(session_path):
            client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
            await client.disconnect()
            
            # Remove session file
            os.remove(session_path)
            print(f"🔒 Logged out and removed session for {phone}")
        
    except Exception as e:
        print(f"Error logging out {phone}: {e}")

def save_utr_request(user_id, utr, amount):
    conn = sqlite3.connect("data/users.db")
    c = conn.cursor()
    c.execute("INSERT INTO utr_requests (user_id, utr, amount, status) VALUES (?, ?, ?, ?)", (user_id, utr, amount, 'pending'))
    conn.commit()
    conn.close()

async def start_otp_listener(phone):
    """Start OTP listener for a specific phone number"""
    # Prevent multiple listeners for the same phone
    if phone in active_listeners:
        print(f"⚠️ Listener already active for {phone}")
        return
    
    try:
        client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH, 
                              connection_retries=5, retry_delay=1)
        active_listeners[phone] = client
        
        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            try:
                text = event.raw_text
                # Improved OTP detection patterns
                patterns = [
                    r'(?:code is|Code:|OTP|verification code|login code)[\s:]*(\d{4,8})',
                    r'(\d{4,8})\s*(?:is your|code)',
                    r'Your code is\s*(\d{4,8})',
                    r'(\d{6})',  # 6-digit numbers
                    r'(\d{5})',  # 5-digit numbers
                    r'(\d{4})',  # 4-digit numbers
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, text, re.IGNORECASE)
                    if match:
                        otp = match.group(1)
                        set_otp_for_phone(phone, otp)
                        print(f"📲 OTP {otp} received for {phone} from message: {text[:50]}...")
                        break
            except Exception as e:
                print(f"Error processing message for {phone}: {e}")

        await client.start()
        print(f"✅ Started OTP listener for {phone}")
        
        # Run client in background
        async def run_client():
            try:
                await client.run_until_disconnected()
            except Exception as e:
                print(f"Client disconnected for {phone}: {e}")
            finally:
                if phone in active_listeners:
                    del active_listeners[phone]
        
        asyncio.create_task(run_client())
        
    except Exception as e:
        print(f"❌ Failed to start listener for {phone}: {e}")
        if phone in active_listeners:
            del active_listeners[phone]



# === Razorpay Webhook Server (Optional) ===
rzp_app = None
if WEBHOOK_SECRET and RAZORPAY_KEY_ID:
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
    
    print("✅ Webhook server initialized")
else:
    print("⚠️ Webhook server disabled - missing WEBHOOK_SECRET or Razorpay credentials")

# === Telegram Bot Setup ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Store user states (e.g., awaiting_utr, awaiting_amount)
user_states = {}

# Store active OTP listeners to prevent multiple instances
active_listeners = {}

main_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💰 Deposit", callback_data='deposit')],
    [InlineKeyboardButton(text="💼 My Balance", callback_data='balance')],
    [InlineKeyboardButton(text="📱 Get Account", callback_data='get_account')],
    [InlineKeyboardButton(text="👑 Owner", callback_data='owner')]
])

owner_panel = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔐 Add Account", callback_data='add_account')],
    [InlineKeyboardButton(text="📦 Stock Summary", callback_data='stock')]
])

otp_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔁 Get OTP", callback_data='get_otp')],
    [InlineKeyboardButton(text="❌ Cancel", callback_data='cancel')]
])

owner_login_state = {}

@dp.message(Command('start'))
async def start_cmd(message: types.Message):
    if message.from_user.id == OWNER_ID:
        await message.answer("👑 Owner Panel", reply_markup=owner_panel)
    else:
        await message.answer("Welcome to Telegram OTP Bot!", reply_markup=main_menu)

@dp.message(Command('ping'))
async def ping_cmd(message: types.Message):
    await message.answer("🟢 Bot is alive and running!")

@dp.callback_query()
async def callback_handler(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data

    if data == 'deposit':
        amount = 100
        qr_image_url = "https://i.postimg.cc/NFMtrgNh/Phone-Pe-QR-Bank-Of-Baroda-06101.png"

        # Update user state to await UTR
        user_states[user_id] = 'awaiting_utr'
        await callback_query.message.answer_photo(
            photo=qr_image_url,
            caption=f"💸 Pay ₹{amount} by scanning this PhonePe QR code 👇\n\n"
                    f"After payment, send the UTR number."
        )

    elif data == 'balance':
        bal = get_balance(user_id)
        await callback_query.message.answer(f"💼 Your Balance: ₹{bal}")

    elif data == 'owner':
        await callback_query.message.answer(f"👑 Owner: {OWNER_USERNAME}")

    elif data == 'get_account':
        bal = get_balance(user_id)
        if bal < ACCOUNT_PRICE:
            await callback_query.message.answer(f"❌ Insufficient balance.\n\n💰 You need at least ₹{ACCOUNT_PRICE} in your wallet to buy an account.\n📊 Current balance: ₹{bal}\n\n💸 Please deposit ₹{ACCOUNT_PRICE - bal} more to proceed.")
            return

        os.makedirs("data", exist_ok=True)
        try:
            with open("data/account_stock.txt", "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        if not lines:
            await callback_query.message.answer("📦 No account stock available.")
            return
        number = lines[0].strip()
        with open("data/account_stock.txt", "w") as f:
            f.writelines(lines[1:])
        deduct_balance(user_id, ACCOUNT_PRICE)
        save_purchase(user_id, number)
        
        # Start OTP listener for this number
        asyncio.create_task(start_otp_listener(number))
        
        await callback_query.message.answer(f"📞 Your Number: `{number}`\n\n🔄 OTP listener started. Use 'Get OTP' button to check for received OTPs.", parse_mode='Markdown', reply_markup=otp_menu)

    elif data == 'get_otp':
        pending = get_pending_purchase(user_id)
        if pending:
            number, status = pending
            
            # Check if OTP already received
            conn = sqlite3.connect("data/users.db", timeout=10)
            try:
                c = conn.cursor()
                c.execute("SELECT otp FROM purchases WHERE user_id=? AND number=? ORDER BY rowid DESC LIMIT 1", (user_id, number))
                result = c.fetchone()
                
                if result and result[0] and result[0].strip():
                    otp = result[0].strip()
                    await callback_query.message.answer(
                        f"📱 **OTP Received!**\n"
                        f"📞 Number: `{number}`\n"
                        f"🔢 OTP: `{otp}`\n\n"
                        f"✅ Use this OTP for your verification.",
                        parse_mode='Markdown'
                    )
                else:
                    # Start listener if not already active
                    if number not in active_listeners:
                        asyncio.create_task(start_otp_listener(number))
                        status_msg = "🔄 Starting OTP listener..."
                    else:
                        status_msg = "🔍 Listening for OTP..."
                    
                    await callback_query.message.answer(
                        f"⏳ **Waiting for OTP**\n"
                        f"📞 Number: `{number}`\n"
                        f"{status_msg}\n\n"
                        f"💡 The OTP will appear here automatically when received.\n"
                        f"🔄 Click 'Get OTP' again in a few seconds to check.",
                        parse_mode='Markdown'
                    )
            finally:
                conn.close()
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
                
                # Disconnect and logout the session
                await logout_session(number)
                
                await callback_query.message.answer("✅ Purchase canceled. Amount refunded.")
            else:
                # Account already used, logout session
                await logout_session(number)
                await callback_query.message.answer("❌ OTP already received. Cannot cancel. Account logged out from server.")
        else:
            await callback_query.message.answer("❌ No pending purchase found.")

    elif user_id == OWNER_ID and data == 'add_account':
        owner_login_state[user_id] = 'awaiting_phone'
        await callback_query.message.answer("📞 Send phone number to add (with +91)...")

    elif user_id == OWNER_ID and data == 'stock':
        count = get_stock_summary()
        await callback_query.message.answer(f"📦 Total Stock: {count} numbers")

    # Handle UTR approval/rejection (Owner only)
    elif user_id == OWNER_ID and data.startswith('approve_'):
        parts = data.split('_')
        target_user_id = int(parts[1])
        amount = int(parts[2])
        
        # Add balance to user
        add_balance(target_user_id, amount)
        
        # Update UTR request status
        conn = sqlite3.connect("data/users.db")
        c = conn.cursor()
        c.execute("UPDATE utr_requests SET status='approved' WHERE user_id=? AND status='pending'", (target_user_id,))
        conn.commit()
        conn.close()
        
        # Notify user
        try:
            await bot.send_message(target_user_id, f"✅ Payment approved! ₹{amount} added to your wallet.")
        except:
            pass
            
        # Update owner message
        await callback_query.message.edit_text(
            callback_query.message.text + f"\n\n✅ APPROVED - ₹{amount} added to user wallet"
        )

    elif user_id == OWNER_ID and data.startswith('reject_'):
        parts = data.split('_')
        target_user_id = int(parts[1])
        
        # Update UTR request status
        conn = sqlite3.connect("data/users.db")
        c = conn.cursor()
        c.execute("UPDATE utr_requests SET status='rejected' WHERE user_id=? AND status='pending'", (target_user_id,))
        conn.commit()
        conn.close()
        
        # Notify user
        try:
            await bot.send_message(target_user_id, "❌ Payment verification failed. Please contact support if you believe this is an error.")
        except:
            pass
            
        # Update owner message
        await callback_query.message.edit_text(
            callback_query.message.text + f"\n\n❌ REJECTED"
        )

@dp.message(lambda msg: msg.from_user.id == OWNER_ID)
async def owner_add_account_step(message: types.Message):
    state = owner_login_state.get(message.from_user.id)
    if state == 'awaiting_phone':
        phone = message.text.strip()
        client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            code_request = await client.send_code_request(phone)
            owner_login_state[message.from_user.id] = {
                'phone': phone, 
                'phone_code_hash': code_request.phone_code_hash
            }
            await message.answer("📨 Code sent. Enter OTP:")
        else:
            # Owner can always re-add accounts regardless of login status
            await message.answer("✅ Account added to stock (already logged in).")
            add_to_stock(phone)
            # Start OTP listener for this phone
            await start_otp_listener(phone)
            # Clear owner state since we're done
            owner_login_state.pop(message.from_user.id, None)
        await client.disconnect()
    elif isinstance(state, dict) and 'phone' in state and 'phone_code_hash' in state:
        code = message.text.strip()
        phone = state['phone']
        phone_code_hash = state['phone_code_hash']
        client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH)
        await client.connect()
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            await message.answer(f"✅ Account {phone} logged in and added to stock.")
            add_to_stock(phone)
            # Start OTP listener for this newly logged in phone
            await start_otp_listener(phone)
        except Exception as e:
            await message.answer(f"❌ Login failed: {e}")
        await client.disconnect()
        owner_login_state.pop(message.from_user.id, None)

@dp.message()
async def handle_utr_input(message: types.Message):
    user_id = message.from_user.id

    # Handle UTR submission
    if user_states.get(user_id) == 'awaiting_utr':
        utr = message.text.strip()
        if len(utr) < 8:  # Basic UTR validation
            await message.answer("❌ Invalid UTR. Please provide a valid UTR number.")
            return

        # Ask for amount
        user_states[user_id] = {'state': 'awaiting_amount', 'utr': utr}
        await message.answer("💰 Please enter the amount you paid:")

    elif isinstance(user_states.get(user_id), dict) and user_states[user_id].get('state') == 'awaiting_amount':
        try:
            amount = int(message.text.strip())
            utr = user_states[user_id]['utr']

            save_utr_request(user_id, utr, amount)

            # Notify owner with verification buttons
            try:
                verify_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Approve", callback_data=f'approve_{user_id}_{amount}'),
                        InlineKeyboardButton(text="❌ Reject", callback_data=f'reject_{user_id}')
                    ]
                ])
                await bot.send_message(
                    OWNER_ID,
                    f"🔔 New UTR Verification Request\n"
                    f"👤 User ID: {user_id}\n"
                    f"👤 Username: @{message.from_user.username or 'N/A'}\n"
                    f"🏦 UTR: `{utr}`\n"
                    f"💰 Amount: ₹{amount}",
                    parse_mode='Markdown',
                    reply_markup=verify_keyboard
                )
            except:
                pass

            await message.answer("✅ UTR submitted for verification. You'll be notified once approved.")
            user_states.pop(user_id, None)

        except ValueError:
            await message.answer("❌ Invalid amount. Please enter a valid number.")

@dp.message(Command('addbal'))
async def manual_add_balance(message: types.Message):
    try:
        amount = int(message.text.split()[1])
        add_balance(message.from_user.id, amount)
        await message.answer(f"✅ ₹{amount} added to your wallet.")
    except:
        await message.answer("❌ Usage: /addbal <amount>")

async def main():
    init_db()
    print("✅ Database initialized")
    
    # Start existing sessions only if they exist and are valid
    try:
        await start_existing_sessions()
    except Exception as e:
        print(f"⚠️ Error starting existing sessions: {e}")
    
    print("✅ Bot started successfully!")
    await dp.start_polling(bot, skip_updates=True)

async def start_existing_sessions():
    """Start OTP listeners for existing valid sessions only"""
    if not os.path.exists(SESSION_DIR):
        return
        
    for session_file in os.listdir(SESSION_DIR):
        if session_file.endswith(".session"):
            phone = session_file.replace(".session", "")
            session_path = f"{SESSION_DIR}/{phone}.session"
            
            # Check if session file is valid before starting
            try:
                # Check if session file is empty or corrupted
                if os.path.getsize(session_path) < 1024:  # Very small session files are likely corrupted
                    print(f"⚠️ Session file too small, removing: {phone}")
                    os.remove(session_path)
                    continue
                
                client = TelegramClient(session_path, API_ID, API_HASH)
                
                # Set a connection timeout to prevent hanging
                await asyncio.wait_for(client.connect(), timeout=10)
                
                if await client.is_user_authorized():
                    await client.disconnect()
                    await start_otp_listener(phone)
                    print(f"✅ Started listener for existing session: {phone}")
                else:
                    await client.disconnect()
                    print(f"⚠️ Session not authorized, removing: {phone}")
                    # Remove unauthorized session files to prevent future issues
                    try:
                        os.remove(session_path)
                    except:
                        pass
                    
            except asyncio.TimeoutError:
                print(f"⚠️ Connection timeout for session {phone}, removing")
                try:
                    os.remove(session_path)
                except:
                    pass
            except Exception as e:
                print(f"⚠️ Error checking session {phone}: {e}")
                # Remove problematic session files
                try:
                    os.remove(session_path)
                    print(f"🗑️ Removed problematic session file: {phone}")
                except:
                    pass
                continue

if __name__ == '__main__':
    asyncio.run(main())
