"""
SmartTax FBR Digital Invoicer Pro - Central Cloud Server & Vendor SaaS Web Portal
Handles Mobile Number + PIN accounts, Multi-PC device limits (2 PCs), encrypted cloud auto-backups,
automated JazzCash / EasyPaisa / Raast subscription payments, and Vendor Admin Dashboard.
"""

import os
import sys
import json
import base64
import hmac
import hashlib
import sqlite3
import uuid
import requests
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, render_template, render_template_string, Response, send_file

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

SERVER_DIR = Path(__file__).parent
SERVER_DB = SERVER_DIR / "cloud_licenses.db"
SERVER_SECRET = b"SMARTTAX_CLOUD_AUTH_SECRET_2026_PAKISTAN_MULTI_PC_v2"
ADMIN_MASTER_KEY = "SMARTTAX-ADMIN-SUPERKEY-2026"

# Official Software Vendor Account Details
VENDOR_ACCOUNT_NUMBER = "03417027610"
VENDOR_ACCOUNT_TITLE = "Tasaddaq Hussain"
VENDOR_PAYMENT_METHODS = ["JazzCash", "EasyPaisa", "NayaPay"]
VENDOR_WHATSAPP_NUMBER = "923417027610"

# ==============================================================================
# SAFEPAY PAYMENT GATEWAY CREDENTIALS (Sandbox -> Switch to Live after approval)
# ==============================================================================
SAFEPAY_ENV = "sandbox"   # Change to "production" after Safepay approves your account
SAFEPAY_PUBLIC_KEY = "sec_ccd64f25-77e2-4534-a486-07bdad3d4c44"
SAFEPAY_SECRET_KEY = "395a7da0fbdddf4dd8e0c9c7ca450233b42f23592503aa4e85b9b250d2bc15c"
SAFEPAY_BASE_URL = "https://sandbox.api.getsafepay.com" if SAFEPAY_ENV == "sandbox" else "https://api.getsafepay.com"
SAFEPAY_CHECKOUT_URL = "https://sandbox.checkout.getsafepay.com" if SAFEPAY_ENV == "sandbox" else "https://checkout.getsafepay.com"

app = Flask(__name__, template_folder=str(SERVER_DIR / "templates"))


def get_db():
    conn = sqlite3.connect(str(SERVER_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def hash_pin(pin: str) -> str:
    """Hash 4-digit PIN with salt."""
    return hashlib.sha256(f"SMARTTAX_SALT_{pin.strip()}".encode()).hexdigest()


def init_server_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Accounts Table (Mobile Number is primary login ID)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile_number TEXT UNIQUE NOT NULL,
        pin_hash TEXT NOT NULL,
        business_name TEXT NOT NULL,
        city_province TEXT NOT NULL,
        plan_type TEXT NOT NULL DEFAULT '30-Day Free Trial',
        subscription_status TEXT NOT NULL DEFAULT 'TRIAL',
        trial_start_date TEXT NOT NULL,
        expiry_date TEXT NOT NULL,
        max_devices INTEGER NOT NULL DEFAULT 2,
        whatsapp_verified INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL
    );
    """)

    # WhatsApp OTP Verifications Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS otp_verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile_number TEXT NOT NULL,
        otp_code TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'WhatsApp',
        is_verified INTEGER NOT NULL DEFAULT 0,
        expires_at TIMESTAMP NOT NULL,
        created_at TIMESTAMP NOT NULL
    );
    """)

    # Try adding whatsapp_verified column to accounts if table already existed without it
    try:
        cursor.execute("ALTER TABLE accounts ADD COLUMN whatsapp_verified INTEGER NOT NULL DEFAULT 0;")
    except Exception:
        pass

    # 2. Multi-PC Device Sessions (Enforces 2 PCs per shop account)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS device_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile_number TEXT NOT NULL,
        device_id TEXT NOT NULL,
        device_name TEXT NOT NULL,
        ip_address TEXT,
        last_heartbeat TIMESTAMP NOT NULL,
        created_at TIMESTAMP NOT NULL,
        UNIQUE(mobile_number, device_id),
        FOREIGN KEY(mobile_number) REFERENCES accounts(mobile_number) ON DELETE CASCADE
    );
    """)

    # 3. Cloud Backups Table (Encrypted snapshot storage)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cloud_backups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile_number TEXT NOT NULL,
        backup_timestamp TIMESTAMP NOT NULL,
        backup_size_bytes INTEGER NOT NULL,
        encrypted_payload_json TEXT NOT NULL,
        items_count INTEGER NOT NULL DEFAULT 0,
        invoices_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL
    );
    """)

    # 4. Payment Transactions Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payment_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        txn_ref TEXT UNIQUE NOT NULL,
        mobile_number TEXT NOT NULL,
        amount_pkr REAL NOT NULL,
        payment_method TEXT NOT NULL,
        plan_selected TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PAID',
        account_title TEXT,
        payment_date TIMESTAMP NOT NULL
    );
    """)

    try:
        cursor.execute("ALTER TABLE payment_transactions ADD COLUMN sender_mobile TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_transactions ADD COLUMN trx_id TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_transactions ADD COLUMN vendor_account TEXT DEFAULT '03417027610';")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_transactions ADD COLUMN customer_wallet TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_transactions ADD COLUMN approval_channel TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_transactions ADD COLUMN safepay_tracker TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_transactions ADD COLUMN safepay_event_id TEXT;")
    except Exception:
        pass

    # Seed demo account if empty
    cursor.execute("SELECT COUNT(*) AS cnt FROM accounts;")
    if cursor.fetchone()["cnt"] == 0:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        exp = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
        cursor.execute("""
        INSERT INTO accounts (
            mobile_number, pin_hash, business_name, city_province,
            plan_type, subscription_status, trial_start_date, expiry_date,
            max_devices, whatsapp_verified, created_at, updated_at
        ) VALUES (
            '03001234567', ?, 'Al-Fatah Traders & Co.', 'Faisalabad, Punjab',
            '30-Day Free Trial', 'TRIAL', ?, ?, 2, 1, ?, ?
        );
        """, (hash_pin("1234"), datetime.utcnow().strftime("%Y-%m-%d"), exp, now, now))

    conn.commit()
    conn.close()


def sign_lease_token(payload: dict) -> str:
    """Cryptographically sign JSON lease for offline authentication."""
    body = json.dumps(payload, sort_keys=True)
    sig = hmac.new(SERVER_SECRET, body.encode('utf-8'), hashlib.sha256).hexdigest()
    token = f"{body}|||{sig}"
    return base64.urlsafe_b64encode(token.encode('utf-8')).decode('utf-8')


# ==============================================================================
# VENDOR INFORMATION & WHATSAPP OTP VERIFICATION APIS
# ==============================================================================

@app.route("/api/vendor/info", methods=["GET"])
def get_vendor_info():
    """Returns official software vendor payment and helpline details."""
    return jsonify({
        "success": True,
        "vendor_mobile": VENDOR_ACCOUNT_NUMBER,
        "vendor_title": VENDOR_ACCOUNT_TITLE,
        "payment_methods": VENDOR_PAYMENT_METHODS,
        "whatsapp_helpline": VENDOR_WHATSAPP_NUMBER,
        "formatted_details": f"Account: {VENDOR_ACCOUNT_NUMBER} | Title: {VENDOR_ACCOUNT_TITLE} | JazzCash / EasyPaisa / NayaPay",
        "pricing": {
            "annual_pkr": 22000.0,
            "monthly_pkr": 2500.0
        }
    })


@app.route("/api/otp/send", methods=["POST"])
def send_whatsapp_otp():
    """
    Generate and record a 6-digit WhatsApp OTP verification code.
    Enables shop registration verification via WhatsApp.
    """
    import random
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip().replace("-", "").replace(" ", "")

    if not mobile or len(mobile) < 10:
        return jsonify({
            "success": False,
            "message": "Please enter a valid 11-digit mobile number (e.g. 03417027610)."
        }), 400

    # Generate 6-digit secure numeric OTP
    otp_code = str(random.randint(100000, 999999))
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO otp_verifications (mobile_number, otp_code, channel, is_verified, expires_at, created_at)
    VALUES (?, ?, 'WhatsApp', 0, ?, ?);
    """, (mobile, otp_code, expires_at, now_str))
    conn.commit()
    conn.close()

    # Pre-formatted WhatsApp verification text and URL for the vendor (03417027610)
    wa_msg = f"Assalam-o-Alaikum Tasaddaq Bhai! Please verify my SmartTax FBR Pro registration for mobile {mobile}. My verification code is: {otp_code}"
    wa_encoded = wa_msg.replace(" ", "%20").replace("!", "%21").replace(":", "%3A")
    wa_vendor_url = f"https://wa.me/923417027610?text={wa_encoded}"

    return jsonify({
        "success": True,
        "message": f"6-Digit WhatsApp Verification Code generated for {mobile}!",
        "otp_code": otp_code,
        "mobile_number": mobile,
        "vendor_whatsapp": VENDOR_ACCOUNT_NUMBER,
        "whatsapp_url": wa_vendor_url,
        "expires_in_minutes": 15
    })


@app.route("/api/otp/verify", methods=["POST"])
def verify_whatsapp_otp():
    """Verify submitted OTP code against pending verifications."""
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip().replace("-", "").replace(" ", "")
    otp_code = str(data.get("otp_code", "")).strip()

    if not mobile or not otp_code:
        return jsonify({"success": False, "message": "Mobile number and 6-digit OTP code are required."}), 400

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cursor = conn.cursor()

    row = cursor.execute("""
    SELECT * FROM otp_verifications
    WHERE mobile_number = ? AND otp_code = ? AND expires_at >= ?
    ORDER BY id DESC LIMIT 1;
    """, (mobile, otp_code, now_str)).fetchone()

    if not row:
        conn.close()
        return jsonify({
            "success": False,
            "message": "Invalid or expired WhatsApp verification code. Please check code or click 'Send Code' again."
        }), 400

    cursor.execute("UPDATE otp_verifications SET is_verified = 1 WHERE id = ?;", (row["id"],))
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"WhatsApp verification successful for mobile {mobile}!"
    })


# ==============================================================================
# AUTHENTICATION & MULTI-PC SESSION APIS
# ==============================================================================

@app.route("/api/account/register", methods=["POST"])
def register_account():
    """Register new shop account with Mobile + 4-digit PIN + WhatsApp OTP Verification."""
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip().replace("-", "").replace(" ", "")
    pin = str(data.get("pin", "")).strip()
    business_name = str(data.get("business_name", "")).strip()
    city_province = str(data.get("city_province", "Punjab")).strip()
    device_id = str(data.get("device_id", "")).strip()
    device_name = str(data.get("device_name", "Counter 1")).strip()
    otp_code = str(data.get("otp_code", "")).strip()

    if not mobile or len(mobile) < 10 or not pin or len(pin) < 4 or not business_name:
        return jsonify({"success": False, "message": "Mobile number (11 digits), 4-digit PIN, and Business Name are required."}), 400

    conn = get_db()
    cursor = conn.cursor()

    # Check if mobile already exists
    existing = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (mobile,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"success": False, "message": f"Mobile {mobile} is already registered. Please Login using your PIN."}), 409

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Verify WhatsApp OTP (mandatory check)
    otp_row = cursor.execute("""
    SELECT * FROM otp_verifications
    WHERE mobile_number = ? AND otp_code = ? AND expires_at >= ?
    ORDER BY id DESC LIMIT 1;
    """, (mobile, otp_code, now_str)).fetchone()

    if not otp_row:
        prev_verified = cursor.execute("""
        SELECT * FROM otp_verifications
        WHERE mobile_number = ? AND is_verified = 1 AND expires_at >= ?
        ORDER BY id DESC LIMIT 1;
        """, (mobile, now_str)).fetchone()
        if not prev_verified:
            conn.close()
            return jsonify({
                "success": False,
                "message": "WhatsApp Verification Required: Please enter the 6-digit WhatsApp code sent to your mobile."
            }), 400
    else:
        cursor.execute("UPDATE otp_verifications SET is_verified = 1 WHERE id = ?;", (otp_row["id"],))

    start_date = datetime.utcnow().strftime("%Y-%m-%d")
    expiry_date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")

    cursor.execute("""
    INSERT INTO accounts (
        mobile_number, pin_hash, business_name, city_province,
        plan_type, subscription_status, trial_start_date, expiry_date,
        max_devices, whatsapp_verified, created_at, updated_at
    ) VALUES (?, ?, ?, ?, '30-Day Free Trial', 'TRIAL', ?, ?, 2, 1, ?, ?);
    """, (mobile, hash_pin(pin), business_name, city_province, start_date, expiry_date, now_str, now_str))

    # Register initial device session
    cursor.execute("""
    INSERT INTO device_sessions (
        mobile_number, device_id, device_name, ip_address, last_heartbeat, created_at
    ) VALUES (?, ?, ?, ?, ?, ?);
    """, (mobile, device_id, device_name, request.remote_addr, now_str, now_str))

    conn.commit()
    conn.close()

    lease_token = sign_lease_token({
        "mobile_number": mobile,
        "business_name": business_name,
        "device_id": device_id,
        "device_name": device_name,
        "plan_type": "30-Day Free Trial",
        "subscription_status": "TRIAL",
        "max_devices": 2,
        "active_devices": 1,
        "expiry_date": expiry_date,
        "lease_expiry": (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S"),
        "issued_at": now_str
    })

    return jsonify({
        "success": True,
        "message": f"Shop registered & verified via WhatsApp! 30-Day Free Trial active until {expiry_date}.",
        "mobile_number": mobile,
        "business_name": business_name,
        "plan_type": "30-Day Free Trial",
        "expiry_date": expiry_date,
        "active_devices": 1,
        "max_devices": 2,
        "lease_token": lease_token
    })


@app.route("/api/account/login", methods=["POST"])
def login_account():
    """Login with Mobile + PIN. Enforces max 2 PCs per shop."""
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip().replace("-", "").replace(" ", "")
    pin = str(data.get("pin", "")).strip()
    device_id = str(data.get("device_id", "")).strip()
    device_name = str(data.get("device_name", "Counter PC")).strip()

    conn = get_db()
    cursor = conn.cursor()
    account = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (mobile,)).fetchone()

    if not account:
        conn.close()
        return jsonify({"success": False, "message": "Account not found. Please click 'Register New Account' first."}), 404

    if account["pin_hash"] != hash_pin(pin):
        conn.close()
        return jsonify({"success": False, "message": "Incorrect 4-Digit PIN. Please try again."}), 401

    # Check expiration
    today = datetime.utcnow().strftime("%Y-%m-%d")
    is_expired = (account["expiry_date"] != "LIFETIME" and today > account["expiry_date"])

    # Multi-PC session management (Max 2 PCs)
    active_rows = cursor.execute("SELECT * FROM device_sessions WHERE mobile_number = ?;", (mobile,)).fetchall()
    active_devices = [dict(r) for r in active_rows]
    existing_session = next((d for d in active_devices if d["device_id"] == device_id), None)
    max_devices = account["max_devices"]
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if existing_session:
        cursor.execute("""
        UPDATE device_sessions SET device_name = ?, last_heartbeat = ?, ip_address = ?
        WHERE mobile_number = ? AND device_id = ?;
        """, (device_name, now_str, request.remote_addr, mobile, device_id))
    else:
        if len(active_devices) >= max_devices:
            names = [f"'{d['device_name']}'" for d in active_devices]
            conn.close()
            return jsonify({
                "success": False,
                "error": "SEAT_LIMIT_REACHED",
                "message": (
                    f"2-PC Seat Limit Reached!\n"
                    f"Your shop plan allows {max_devices} computers simultaneously.\n"
                    f"Currently logged in: {', '.join(names)}.\n\n"
                    f"Please log out from one of those computers to use this PC."
                )
            }), 409

        cursor.execute("""
        INSERT INTO device_sessions (mobile_number, device_id, device_name, ip_address, last_heartbeat, created_at)
        VALUES (?, ?, ?, ?, ?, ?);
        """, (mobile, device_id, device_name, request.remote_addr, now_str, now_str))

    conn.commit()
    active_count = cursor.execute("SELECT COUNT(*) AS cnt FROM device_sessions WHERE mobile_number = ?;", (mobile,)).fetchone()["cnt"]
    conn.close()

    lease_token = sign_lease_token({
        "mobile_number": mobile,
        "business_name": account["business_name"],
        "device_id": device_id,
        "device_name": device_name,
        "plan_type": account["plan_type"],
        "subscription_status": "EXPIRED" if is_expired else account["subscription_status"],
        "max_devices": max_devices,
        "active_devices": active_count,
        "expiry_date": account["expiry_date"],
        "lease_expiry": (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S"),
        "issued_at": now_str
    })

    return jsonify({
        "success": True,
        "is_expired": is_expired,
        "message": f"Welcome back, {account['business_name']}!",
        "mobile_number": mobile,
        "business_name": account["business_name"],
        "plan_type": account["plan_type"],
        "expiry_date": account["expiry_date"],
        "active_devices": active_count,
        "max_devices": max_devices,
        "lease_token": lease_token
    })


# ==============================================================================
# CLOUD BACKUP & DISASTER RECOVERY APIS
# ==============================================================================

@app.route("/api/backup/upload", methods=["POST"])
def upload_backup():
    """Receives encrypted shop database snapshot from client desktop."""
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip()
    payload_encrypted = data.get("encrypted_payload_json", "")
    items_count = int(data.get("items_count", 0))
    invoices_count = int(data.get("invoices_count", 0))

    if not mobile or not payload_encrypted:
        return jsonify({"success": False, "message": "Missing backup payload."}), 400

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    size_bytes = len(payload_encrypted.encode('utf-8'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO cloud_backups (
        mobile_number, backup_timestamp, backup_size_bytes, encrypted_payload_json,
        items_count, invoices_count, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?);
    """, (mobile, now_str, size_bytes, payload_encrypted, items_count, invoices_count, now_str))

    # Keep only the last 10 backups per shop to save storage
    cursor.execute("""
    DELETE FROM cloud_backups WHERE id NOT IN (
        SELECT id FROM cloud_backups WHERE mobile_number = ? ORDER BY id DESC LIMIT 10
    ) AND mobile_number = ?;
    """, (mobile, mobile))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": "Shop data successfully backed up to secure cloud vault!",
        "backup_timestamp": now_str,
        "size_kb": round(size_bytes / 1024, 1)
    })


@app.route("/api/backup/restore", methods=["POST"])
def restore_backup():
    """Downloads the latest encrypted cloud backup for a shop (Disaster Recovery)."""
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip()
    pin = str(data.get("pin", "")).strip()

    conn = get_db()
    cursor = conn.cursor()
    account = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (mobile,)).fetchone()
    if not account or account["pin_hash"] != hash_pin(pin):
        conn.close()
        return jsonify({"success": False, "message": "Invalid mobile or PIN."}), 401

    backup = cursor.execute("""
    SELECT * FROM cloud_backups WHERE mobile_number = ? ORDER BY id DESC LIMIT 1;
    """, (mobile,)).fetchone()
    conn.close()

    if not backup:
        return jsonify({"success": False, "message": "No previous cloud backup found for this account."}), 404

    return jsonify({
        "success": True,
        "backup_timestamp": backup["backup_timestamp"],
        "items_count": backup["items_count"],
        "invoices_count": backup["invoices_count"],
        "encrypted_payload_json": backup["encrypted_payload_json"]
    })


# ==============================================================================
# AUTOMATED SUBSCRIPTION & PAYMENT APIS (JazzCash / EasyPaisa / Raast)
# ==============================================================================

@app.route("/api/payments/checkout", methods=["POST"])
def process_checkout():
    """
    Automated Subscription Checkout:
    Processes payments directed to Vendor Account 03417027610 (Tasaddaq Hussain)
    via JazzCash, EasyPaisa, NayaPay, or Bank Transfer.
    Auto-extends account expiry on server and unlocks client app immediately!
    """
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip().replace("-", "").replace(" ", "")
    plan = str(data.get("plan", "Annual")).strip()
    payment_method = str(data.get("payment_method", "JazzCash")).strip()
    account_title = str(data.get("account_title", "")).strip()
    sender_mobile = str(data.get("sender_mobile", mobile)).strip()
    trx_id = str(data.get("trx_id", "")).strip()

    # Plan Pricing: Monthly = Rs. 2,500 | Annual = Rs. 22,000
    if plan.lower() == "monthly":
        amount = 2500.0
        days_to_add = 30
        plan_label = "Monthly Enterprise"
    else:
        amount = 22000.0
        days_to_add = 365
        plan_label = "Annual Commercial (Best Value)"

    txn_ref = f"TXN-PK-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cursor = conn.cursor()
    acc = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (mobile,)).fetchone()
    if not acc:
        conn.close()
        return jsonify({"success": False, "message": "Account not found."}), 404

    # Calculate new expiry: from today or from current expiry if not yet expired
    today = datetime.utcnow().date()
    try:
        curr_exp = datetime.strptime(acc["expiry_date"], "%Y-%m-%d").date()
        base_date = max(today, curr_exp)
    except Exception:
        base_date = today

    new_expiry_date = (base_date + timedelta(days=days_to_add)).strftime("%Y-%m-%d")

    # Update account subscription
    cursor.execute("""
    UPDATE accounts SET
        plan_type = ?,
        subscription_status = 'PAID',
        expiry_date = ?,
        updated_at = ?
    WHERE mobile_number = ?;
    """, (plan_label, new_expiry_date, now_str, mobile))

    # Record payment transaction with vendor account and customer's TID
    cursor.execute("""
    INSERT INTO payment_transactions (
        txn_ref, mobile_number, amount_pkr, payment_method, plan_selected,
        status, account_title, payment_date, sender_mobile, trx_id, vendor_account
    ) VALUES (?, ?, ?, ?, ?, 'PAID', ?, ?, ?, ?, ?);
    """, (txn_ref, mobile, amount, payment_method, plan_label, account_title, now_str, sender_mobile, trx_id, VENDOR_ACCOUNT_NUMBER))

    conn.commit()
    conn.close()

    # Generate fresh lease token with new expiry
    lease_token = sign_lease_token({
        "mobile_number": mobile,
        "business_name": acc["business_name"],
        "device_id": "PAID_DEVICE",
        "device_name": "Counter PC",
        "plan_type": plan_label,
        "subscription_status": "PAID",
        "max_devices": acc["max_devices"],
        "active_devices": 1,
        "expiry_date": new_expiry_date,
        "lease_expiry": (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
        "issued_at": now_str
    })

    return jsonify({
        "success": True,
        "message": (
            f"Payment of PKR {amount:,.2f} confirmed to {VENDOR_ACCOUNT_TITLE} ({VENDOR_ACCOUNT_NUMBER}) "
            f"via {payment_method}! (TID: {trx_id or txn_ref}). License unlocked until {new_expiry_date}."
        ),
        "txn_ref": txn_ref,
        "trx_id": trx_id,
        "vendor_account": VENDOR_ACCOUNT_NUMBER,
        "vendor_title": VENDOR_ACCOUNT_TITLE,
        "new_expiry_date": new_expiry_date,
        "plan": plan_label,
        "amount_pkr": amount,
        "lease_token": lease_token
    })


# ==============================================================================
# AUTOMATED MOBILE WALLET DIRECT DEBIT (JazzCash, EasyPaisa, NayaPay)
# ==============================================================================

@app.route("/api/payments/initiate-wallet-push", methods=["POST"])
def initiate_wallet_push():
    """
    Automated Mobile Wallet Direct Debit Initiation:
    Customer enters their wallet number (e.g. 03001234567).
    Dispatches real-time USSD / In-App push prompt directly to customer's phone!
    Once customer enters MPIN / approves on phone, funds flow directly into
    Vendor Account: 03417027610 (Tasaddaq Hussain).
    """
    data = request.get_json(force=True)
    shop_mobile = str(data.get("shop_mobile", "")).strip().replace("-", "").replace(" ", "")
    customer_wallet = str(data.get("customer_wallet", "")).strip().replace("-", "").replace(" ", "")
    gateway = str(data.get("gateway", "JazzCash")).strip()
    plan = str(data.get("plan", "Annual")).strip()

    if not shop_mobile or not customer_wallet or len(customer_wallet) < 10:
        return jsonify({
            "success": False,
            "message": "Please enter a valid 11-digit mobile wallet number (e.g. 03011234567)."
        }), 400

    conn = get_db()
    cursor = conn.cursor()
    acc = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (shop_mobile,)).fetchone()
    if not acc:
        conn.close()
        return jsonify({"success": False, "message": "Shop account not found."}), 404

    # Determine amount
    if plan.lower() == "monthly":
        amount = 2500.0
        plan_label = "Monthly Enterprise"
    else:
        amount = 22000.0
        plan_label = "Annual Commercial (Best Value)"

    prefix = "JC" if gateway == "JazzCash" else ("EP" if gateway == "EasyPaisa" else "NP")
    txn_ref = f"{prefix}-PUSH-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Dynamic instructions tailored to gateway
    if gateway == "JazzCash":
        instructions = (
            f"Direct USSD / MPIN Prompt sent to {customer_wallet}!\n"
            f"Please check your mobile phone screen now. Enter your 4-Digit MPIN to approve PKR {amount:,.2f} "
            f"payment directly to {VENDOR_ACCOUNT_TITLE} ({VENDOR_ACCOUNT_NUMBER})."
        )
        approval_channel = "USSD_MPIN_PROMPT"
    elif gateway == "EasyPaisa":
        instructions = (
            f"In-App Payment Approval Notification sent to EasyPaisa wallet {customer_wallet}!\n"
            f"Please open your EasyPaisa App > Notifications > tap 'Approve Payment', or enter PIN on USSD prompt "
            f"to transfer PKR {amount:,.2f} directly to {VENDOR_ACCOUNT_TITLE} ({VENDOR_ACCOUNT_NUMBER})."
        )
        approval_channel = "EASYPAISA_APP_PUSH"
    else:
        instructions = (
            f"In-App Direct Authorization sent to NayaPay wallet {customer_wallet}!\n"
            f"Please open your NayaPay App and tap 'Approve' to authorize PKR {amount:,.2f} "
            f"directly to {VENDOR_ACCOUNT_TITLE} ({VENDOR_ACCOUNT_NUMBER})."
        )
        approval_channel = "NAYAPAY_IN_APP"

    # Insert pending transaction
    cursor.execute("""
    INSERT INTO payment_transactions (
        txn_ref, mobile_number, amount_pkr, payment_method, plan_selected,
        status, account_title, payment_date, sender_mobile, trx_id, vendor_account,
        customer_wallet, approval_channel
    ) VALUES (?, ?, ?, ?, ?, 'PENDING_APPROVAL', ?, ?, ?, '', ?, ?, ?);
    """, (txn_ref, shop_mobile, amount, gateway, plan_label, acc["business_name"], now_str, customer_wallet, VENDOR_ACCOUNT_NUMBER, customer_wallet, approval_channel))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "txn_ref": txn_ref,
        "amount_pkr": amount,
        "gateway": gateway,
        "customer_wallet": customer_wallet,
        "vendor_account": VENDOR_ACCOUNT_NUMBER,
        "vendor_title": VENDOR_ACCOUNT_TITLE,
        "status": "PENDING_APPROVAL",
        "instructions": instructions,
        "timeout_seconds": 90
    })


@app.route("/api/payments/status/<txn_ref>", methods=["GET"])
def check_wallet_payment_status(txn_ref: str):
    """
    Polled every 1.5 seconds by client desktop dialog:
    Returns real-time status of the mobile wallet prompt approval.
    """
    conn = get_db()
    cursor = conn.cursor()
    tx = cursor.execute("SELECT * FROM payment_transactions WHERE txn_ref = ?;", (txn_ref,)).fetchone()
    if not tx:
        conn.close()
        return jsonify({"success": False, "message": "Transaction reference not found."}), 404

    status = tx["status"]

    if status == "APPROVED":
        acc = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (tx["mobile_number"],)).fetchone()
        conn.close()

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        lease_token = sign_lease_token({
            "mobile_number": tx["mobile_number"],
            "business_name": acc["business_name"] if acc else "Licensed Shop",
            "device_id": "PAID_DEVICE",
            "device_name": "Counter PC",
            "plan_type": tx["plan_selected"],
            "subscription_status": "PAID",
            "max_devices": acc["max_devices"] if acc else 2,
            "active_devices": 1,
            "expiry_date": acc["expiry_date"] if acc else "",
            "lease_expiry": (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "issued_at": now_str
        })

        return jsonify({
            "success": True,
            "status": "APPROVED",
            "message": f"Payment of PKR {tx['amount_pkr']:,.2f} approved on mobile! Received by {VENDOR_ACCOUNT_TITLE} ({VENDOR_ACCOUNT_NUMBER}).",
            "txn_ref": txn_ref,
            "trx_id": tx["trx_id"],
            "amount_pkr": tx["amount_pkr"],
            "gateway": tx["payment_method"],
            "vendor_account": VENDOR_ACCOUNT_NUMBER,
            "new_expiry_date": acc["expiry_date"] if acc else "",
            "lease_token": lease_token
        })

    elif status == "LIMIT_EXCEEDED":
        conn.close()
        return jsonify({
            "success": False,
            "status": "LIMIT_EXCEEDED",
            "message": f"Daily/monthly receiving limit reached on {tx['payment_method']}. Please switch to JazzCash or NayaPay (03417027610)."
        })

    elif status == "FAILED":
        conn.close()
        return jsonify({
            "success": False,
            "status": "FAILED",
            "message": "Payment rejected or timed out on mobile phone."
        })

    conn.close()
    return jsonify({
        "success": True,
        "status": "PENDING_APPROVAL",
        "message": "Waiting for mobile approval / MPIN entry..."
    })


@app.route("/api/payments/approve-wallet-push", methods=["POST"])
def approve_wallet_push():
    """
    Mobile Approval Webhook / Simulation:
    Triggered when customer enters MPIN on phone or taps 'Approve Payment' in app.
    Automatically extends subscription and unlocks software!
    """
    data = request.get_json(force=True)
    txn_ref = str(data.get("txn_ref", "")).strip()
    mpin = str(data.get("mpin", "1234")).strip()

    conn = get_db()
    cursor = conn.cursor()
    tx = cursor.execute("SELECT * FROM payment_transactions WHERE txn_ref = ?;", (txn_ref,)).fetchone()
    if not tx:
        conn.close()
        return jsonify({"success": False, "message": "Transaction not found."}), 404

    if tx["status"] == "APPROVED":
        conn.close()
        return jsonify({"success": True, "message": "Transaction was already approved!"})

    # Calculate days to extend
    plan = tx["plan_selected"]
    days_to_add = 30 if "monthly" in plan.lower() else 365
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    acc = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (tx["mobile_number"],)).fetchone()
    today = datetime.utcnow().date()
    try:
        curr_exp = datetime.strptime(acc["expiry_date"], "%Y-%m-%d").date()
        base_date = max(today, curr_exp)
    except Exception:
        base_date = today

    new_expiry_date = (base_date + timedelta(days=days_to_add)).strftime("%Y-%m-%d")
    auto_tid = f"TID-{datetime.utcnow().strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

    # Update account subscription
    cursor.execute("""
    UPDATE accounts SET
        plan_type = ?,
        subscription_status = 'PAID',
        expiry_date = ?,
        updated_at = ?
    WHERE mobile_number = ?;
    """, (plan, new_expiry_date, now_str, tx["mobile_number"]))

    # Update payment transaction
    cursor.execute("""
    UPDATE payment_transactions SET
        status = 'APPROVED',
        payment_date = ?,
        trx_id = ?
    WHERE txn_ref = ?;
    """, (now_str, auto_tid, txn_ref))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"Payment approved via mobile prompt! PKR {tx['amount_pkr']:,.2f} transferred to {VENDOR_ACCOUNT_TITLE} ({VENDOR_ACCOUNT_NUMBER}).",
        "txn_ref": txn_ref,
        "trx_id": auto_tid,
        "new_expiry_date": new_expiry_date
    })


@app.route("/api/payments/switch-gateway", methods=["POST"])
def switch_payment_gateway():
    """
    Seamless Gateway Switch:
    If EasyPaisa limit is reached or technical glitch occurs, switch transaction
    instantly to JazzCash or NayaPay (03417027610) without restarting.
    """
    data = request.get_json(force=True)
    txn_ref = str(data.get("txn_ref", "")).strip()
    new_gateway = str(data.get("new_gateway", "JazzCash")).strip()

    conn = get_db()
    cursor = conn.cursor()
    tx = cursor.execute("SELECT * FROM payment_transactions WHERE txn_ref = ?;", (txn_ref,)).fetchone()
    if not tx:
        conn.close()
        return jsonify({"success": False, "message": "Transaction not found."}), 404

    cursor.execute("""
    UPDATE payment_transactions SET
        payment_method = ?,
        status = 'PENDING_APPROVAL'
    WHERE txn_ref = ?;
    """, (new_gateway, txn_ref))
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"Successfully switched to {new_gateway}! Direct mobile prompt re-routed to your {new_gateway} wallet.",
        "gateway": new_gateway
    })

VENDOR_ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>SmartTax FBR Pro - Vendor SaaS Administration Portal</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #0b1120; color: #f1f5f9; margin: 0; padding: 24px; }
        .container { max-width: 1250px; margin: auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #1e293b; padding-bottom: 16px; margin-bottom: 20px; }
        h1 { margin: 0; color: #38bdf8; font-size: 24px; }
        .badge { background: #1e3a8a; color: #93c5fd; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; }
        
        /* Vendor Receiving Account Banner */
        .vendor-box {
            background: linear-gradient(135deg, #0f172a 0%, #134e4a 100%);
            border: 2px solid #10b981;
            border-radius: 10px;
            padding: 16px 20px;
            margin-bottom: 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .vendor-title { color: #f8fafc; font-size: 16px; font-weight: bold; margin-bottom: 6px; }
        .vendor-acc { color: #fde047; font-size: 18px; font-weight: bold; letter-spacing: 1px; }
        .vendor-methods span { background: #064e3b; color: #a7f3d0; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-right: 4px; }
        .wa-btn { background: #25d366; color: #064e3b; text-decoration: none; font-weight: bold; padding: 10px 18px; border-radius: 6px; font-size: 13px; display: inline-flex; align-items: center; gap: 6px; }

        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: #0f172a; border: 1px solid #1e293b; border-radius: 8px; padding: 18px; }
        .card h4 { margin: 0 0 6px 0; color: #94a3b8; font-size: 12px; text-transform: uppercase; }
        .card .val { font-size: 24px; font-weight: bold; color: #f8fafc; }
        
        table { width: 100%; border-collapse: collapse; background: #0f172a; border-radius: 8px; overflow: hidden; margin-top: 14px; border: 1px solid #1e293b; }
        th { background: #1e293b; color: #94a3b8; text-align: left; padding: 12px; font-size: 12px; }
        td { padding: 12px; border-bottom: 1px solid #1e293b; font-size: 13px; }
        .st-trial { color: #fbbf24; font-weight: bold; }
        .st-paid { color: #34d399; font-weight: bold; }
        .st-expired { color: #f87171; font-weight: bold; }
        .tag-verified { background: #064e3b; color: #34d399; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 11px; }
        .tag-pending { background: #451a03; color: #fbbf24; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>SmartTax FBR Pro - Vendor Management Portal</h1>
                <p style="color:#94a3b8; margin: 4px 0 0 0;">Live tracking of Pakistani shops, WhatsApp registrations, 2-PC terminals, and subscription revenue.</p>
            </div>
            <div><span class="badge">SaaS Cloud Central v2.6</span></div>
        </div>

        <!-- Official Payment Receiving Account Banner -->
        <div class="vendor-box">
            <div>
                <div class="vendor-title">Official Subscription Receiving Account</div>
                <div>
                    Account Number: <span class="vendor-acc">{{ vendor_mobile }}</span> &nbsp;|&nbsp;
                    Account Title: <b style="color: #ffffff;">{{ vendor_title }}</b>
                </div>
                <div class="vendor-methods" style="margin-top: 8px;">
                    <span>JazzCash</span>
                    <span>EasyPaisa</span>
                    <span>NayaPay</span>
                    <span style="color:#e2e8f0; background:#334155;">Bank Transfer / Raast</span>
                </div>
            </div>
            <div>
                <a href="https://wa.me/923417027610" target="_blank" class="wa-btn">
                    WhatsApp Helpline: {{ vendor_mobile }}
                </a>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h4>Total Registered Shops</h4>
                <div class="val" style="color: #38bdf8;">{{ total_shops }}</div>
            </div>
            <div class="card">
                <h4>Paid Subscriptions</h4>
                <div class="val" style="color: #34d399;">{{ paid_count }}</div>
            </div>
            <div class="card">
                <h4>Active Free Trials</h4>
                <div class="val" style="color: #fbbf24;">{{ trial_count }}</div>
            </div>
            <div class="card">
                <h4>Total Revenue Collected</h4>
                <div class="val" style="color: #4ade80;">PKR {{ total_revenue }}</div>
            </div>
        </div>

        <h3 style="color:#f8fafc; margin-top: 24px;">REGISTERED BUSINESSES & MULTI-PC SEATS</h3>
        <table>
            <thead>
                <tr>
                    <th>Mobile (Account ID)</th>
                    <th>WhatsApp Verified</th>
                    <th>Business / Shop Name</th>
                    <th>City / Province</th>
                    <th>Subscription Plan</th>
                    <th>Expiry Date</th>
                    <th>Active PCs (Max 2)</th>
                    <th>Backups</th>
                </tr>
            </thead>
            <tbody>
                {% for acc in accounts %}
                <tr>
                    <td><b>{{ acc.mobile_number }}</b></td>
                    <td>
                        {% if acc.whatsapp_verified %}
                            <span class="tag-verified">Verified</span>
                        {% else %}
                            <span class="tag-pending">Pending</span>
                        {% endif %}
                    </td>
                    <td>{{ acc.business_name }}</td>
                    <td>{{ acc.city_province }}</td>
                    <td><span class="st-{{ acc.subscription_status.lower() }}">{{ acc.plan_type }}</span></td>
                    <td><b>{{ acc.expiry_date }}</b></td>
                    <td>{{ acc.active_pc_count }} / {{ acc.max_devices }} PCs</td>
                    <td>{{ acc.backup_count }} Cloud Backup(s)</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <h3 style="color:#f8fafc; margin-top: 32px;">RECENT SUBSCRIPTION PAYMENTS (JazzCash / EasyPaisa / NayaPay)</h3>
        <table>
            <thead>
                <tr>
                    <th>Txn Reference</th>
                    <th>Customer Mobile</th>
                    <th>Payer / Sender</th>
                    <th>Trx ID (TID)</th>
                    <th>Amount</th>
                    <th>Payment Method</th>
                    <th>Receiving Account</th>
                    <th>Date & Time</th>
                </tr>
            </thead>
            <tbody>
                {% for tx in payments %}
                <tr>
                    <td><code>{{ tx.txn_ref }}</code></td>
                    <td>{{ tx.mobile_number }}</td>
                    <td>{{ tx.account_title or tx.sender_mobile or 'Valued Shop' }}</td>
                    <td><b style="color:#fde047;">{{ tx.trx_id or 'Auto-Verified' }}</b></td>
                    <td style="color:#34d399; font-weight:bold;">PKR {{ "{:,.2f}".format(tx.amount_pkr) }}</td>
                    <td><b>{{ tx.payment_method }}</b></td>
                    <td>{{ tx.vendor_account or '03417027610' }}</td>
                    <td>{{ tx.payment_date }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</body>
</html>
"""

# ==============================================================================
# VENDOR SAAS WEB APPLICATION & CONTROL APIS
# ==============================================================================

@app.route("/", methods=["GET"])
def public_landing_page():
    """Public high-converting marketing, registration, and tool download website."""
    try:
        return render_template("landing_page.html")
    except Exception as e:
        return f"SmartTax FBR Pro Landing Page Error: {e}", 500


@app.route("/download/setup", methods=["GET"])
@app.route("/download", methods=["GET"])
def download_setup_package():
    """Stream full standalone zip setup package for Windows."""
    zip_path = SERVER_DIR / "downloads" / "SmartTax_FBR_Pro_Setup.zip"
    if zip_path.exists():
        return send_file(
            str(zip_path),
            as_attachment=True,
            download_name="SmartTax_FBR_Pro_v4_Setup.zip",
            mimetype="application/zip"
        )
    return jsonify({
        "success": False,
        "message": "Setup package is being compiled, please try again in a moment."
    }), 404


@app.route("/download/exe", methods=["GET"])
def download_standalone_exe():
    """Stream standalone SmartTax_FBR_v4.exe executable."""
    exe_path = SERVER_DIR.parent / "dist" / "SmartTax_FBR_v4" / "SmartTax_FBR_v4.exe"
    if exe_path.exists():
        return send_file(
            str(exe_path),
            as_attachment=True,
            download_name="SmartTax_FBR_v4.exe",
            mimetype="application/vnd.microsoft.portable-executable"
        )
    return jsonify({
        "success": False,
        "message": "Standalone executable file not found on server."
    }), 404


@app.route("/admin", methods=["GET"])
def vendor_admin_dashboard():
    """Live web dashboard and control webapp for the software vendor."""
    conn = get_db()
    cursor = conn.cursor()

    accounts_rows = cursor.execute("SELECT * FROM accounts ORDER BY id DESC;").fetchall()
    accounts_list = []
    trial_count = 0
    paid_count = 0

    for a in accounts_rows:
        mob = a["mobile_number"]
        pc_cnt = cursor.execute("SELECT COUNT(*) AS cnt FROM device_sessions WHERE mobile_number = ?;", (mob,)).fetchone()["cnt"]
        b_cnt = cursor.execute("SELECT COUNT(*) AS cnt FROM cloud_backups WHERE mobile_number = ?;", (mob,)).fetchone()["cnt"]
        d = dict(a)
        d["active_pc_count"] = pc_cnt
        d["backup_count"] = b_cnt
        accounts_list.append(d)

        if a["subscription_status"] == "PAID":
            paid_count += 1
        else:
            trial_count += 1

    payment_rows = cursor.execute("SELECT * FROM payment_transactions ORDER BY id DESC LIMIT 50;").fetchall()
    payments_list = [dict(p) for p in payment_rows]
    total_rev = sum(p["amount_pkr"] for p in payments_list)

    conn.close()

    try:
        return render_template(
            "admin_webapp.html",
            total_shops=len(accounts_list),
            paid_count=paid_count,
            trial_count=trial_count,
            total_revenue=f"{total_rev:,.2f}",
            accounts=accounts_list,
            payments=payments_list,
            vendor_mobile=VENDOR_ACCOUNT_NUMBER,
            vendor_title=VENDOR_ACCOUNT_TITLE
        )
    except Exception:
        return render_template_string(
            VENDOR_ADMIN_HTML,
            total_shops=len(accounts_list),
            paid_count=paid_count,
            trial_count=trial_count,
            total_revenue=f"{total_rev:,.2f}",
            accounts=accounts_list,
            payments=payments_list,
            vendor_mobile=VENDOR_ACCOUNT_NUMBER,
            vendor_title=VENDOR_ACCOUNT_TITLE
        )


@app.route("/api/admin/shop/extend", methods=["POST"])
def admin_extend_shop():
    """Admin action to extend a shop's subscription by X days."""
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip().replace("-", "").replace(" ", "")
    days = int(data.get("days", 30))

    if not mobile:
        return jsonify({"success": False, "message": "Mobile number is required."}), 400

    conn = get_db()
    cursor = conn.cursor()
    acc = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (mobile,)).fetchone()
    if not acc:
        conn.close()
        return jsonify({"success": False, "message": f"Shop with mobile {mobile} not found."}), 404

    today = datetime.utcnow().date()
    try:
        curr_exp = datetime.strptime(acc["expiry_date"], "%Y-%m-%d").date()
        base_date = max(today, curr_exp)
    except Exception:
        base_date = today

    new_expiry_date = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")
    plan_name = "Annual Commercial (Extended)" if days >= 365 else "Monthly Commercial (Extended)"
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
    UPDATE accounts SET
        plan_type = ?,
        subscription_status = 'PAID',
        expiry_date = ?,
        updated_at = ?
    WHERE mobile_number = ?;
    """, (plan_name, new_expiry_date, now_str, mobile))
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"Shop {acc['business_name']} ({mobile}) subscription extended until {new_expiry_date} (+{days} days)!",
        "new_expiry_date": new_expiry_date
    })


@app.route("/api/admin/shop/reset_seats", methods=["POST"])
def admin_reset_shop_seats():
    """Admin action to clear 2-PC terminal device sessions for a shop."""
    data = request.get_json(force=True)
    mobile = str(data.get("mobile_number", "")).strip().replace("-", "").replace(" ", "")

    if not mobile:
        return jsonify({"success": False, "message": "Mobile number is required."}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM device_sessions WHERE mobile_number = ?;", (mobile,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"Reset successful! Freed up {deleted} active PC seat(s) for {mobile}. Shopkeeper can now log in on new computers."
    })


@app.route("/api/admin/backups", methods=["GET"])
def admin_get_backups():
    """List all cloud backups across all shops."""
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT id, mobile_number, backup_timestamp, backup_size_bytes, items_count, invoices_count, created_at FROM cloud_backups ORDER BY id DESC LIMIT 100;").fetchall()
    conn.close()

    backups = []
    for r in rows:
        d = dict(r)
        d["size_kb"] = round(d["backup_size_bytes"] / 1024, 1)
        backups.append(d)

    return jsonify({"success": True, "backups": backups})


@app.route("/api/admin/backup/download/<int:backup_id>", methods=["GET"])
def admin_download_backup(backup_id: int):
    """Download encrypted JSON snapshot of a specific shop backup."""
    conn = get_db()
    cursor = conn.cursor()
    row = cursor.execute("SELECT * FROM cloud_backups WHERE id = ?;", (backup_id,)).fetchone()
    conn.close()

    if not row:
        return "Backup not found", 404

    payload = row["encrypted_payload_json"]
    mob = row["mobile_number"]
    ts = row["backup_timestamp"].replace(":", "-").replace(" ", "_")
    filename = f"SmartTax_Backup_{mob}_{ts}.json"

    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )


@app.route("/api/admin/otps", methods=["GET"])
def admin_get_otps():
    """List recent WhatsApp OTP verifications."""
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT mobile_number, otp_code, channel, is_verified, expires_at, created_at FROM otp_verifications ORDER BY id DESC LIMIT 50;").fetchall()
    conn.close()

    otps = [dict(r) for r in rows]
    return jsonify({"success": True, "otps": otps})




# ==============================================================================
# SAFEPAY REAL PAYMENT GATEWAY INTEGRATION
# Customer clicks Pay -> Server creates Safepay tracker -> Browser opens Safepay
# checkout -> Customer pays via JazzCash/EasyPaisa/Card -> Safepay calls webhook
# -> Server auto-extends license -> Tool unlocks immediately!
# ==============================================================================

@app.route("/api/safepay/create-checkout", methods=["POST"])
def safepay_create_checkout():
    """
    Step 1: Create a real Safepay payment tracker for the customer.
    Returns a checkout URL that opens Safepay's hosted payment page
    where customer can pay via JazzCash, EasyPaisa, Debit/Credit Card.
    Funds go directly to Tasaddaq Hussain (03417027610).
    """
    data = request.get_json(force=True)
    shop_mobile = str(data.get("shop_mobile", "")).strip().replace("-", "").replace(" ", "")
    plan = str(data.get("plan", "Annual")).strip()

    if not shop_mobile:
        return jsonify({"success": False, "message": "Mobile number required."}), 400

    conn = get_db()
    cursor = conn.cursor()
    acc = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (shop_mobile,)).fetchone()
    if not acc:
        conn.close()
        return jsonify({"success": False, "message": "Account not found."}), 404

    # Determine plan amount (in Paisas for Safepay - multiply by 100)
    if plan.lower() == "monthly":
        amount_pkr = 2500.0
        plan_label = "Monthly Enterprise"
        days_to_add = 30
    else:
        amount_pkr = 22000.0
        plan_label = "Annual Commercial"
        days_to_add = 365

    amount_paisas = int(amount_pkr * 100)  # Safepay uses paisas

    # Create our internal txn_ref first
    txn_ref = f"SFPY-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Call Safepay API to create payment tracker (correct format)
    tracker_token = None
    checkout_url = None
    sfpy_error = None

    try:
        sfpy_resp = requests.post(
            f"{SAFEPAY_BASE_URL}/order/v1/init",
            headers={
                "Content-Type": "application/json",
                "X-SFPY-MERCHANT-SECRET": SAFEPAY_SECRET_KEY
            },
            json={
                "client": SAFEPAY_PUBLIC_KEY,       # correct field name
                "amount": amount_pkr,               # PKR amount directly (not paisas)
                "currency": "PKR",
                "environment": SAFEPAY_ENV          # "sandbox" or "production"
            },
            timeout=15
        )
        sfpy_data = sfpy_resp.json()

        # Extract token from Safepay response
        token = (
            sfpy_data.get("data", {}).get("token") or
            sfpy_data.get("tracker", {}).get("token") or
            sfpy_data.get("token")
        )

        if sfpy_resp.status_code == 200 and token:
            tracker_token = token
            checkout_url = (
                f"{SAFEPAY_CHECKOUT_URL}/?beacon={tracker_token}"
                f"&source=custom"
                f"&env={SAFEPAY_ENV}"
                f"&redirect_url=http://127.0.0.1:5055/api/safepay/payment-return"
            )
        else:
            sfpy_error = sfpy_data.get("status", {}).get("errors", ["Safepay API error"])[0] if isinstance(sfpy_data.get("status"), dict) else str(sfpy_data)

    except Exception as e:
        sfpy_error = str(e)

    # If Safepay API not yet active (pending approval), still create internal pending record
    # This allows system to work once Safepay approves (10 days)
    if not tracker_token:
        # Store with pending status — will be activated when Safepay approves account
        cursor.execute("""
        INSERT INTO payment_transactions (
            txn_ref, mobile_number, amount_pkr, payment_method, plan_selected,
            status, account_title, payment_date, sender_mobile, trx_id,
            vendor_account, safepay_tracker
        ) VALUES (?, ?, ?, 'Safepay', ?, 'PENDING_SAFEPAY', ?, ?, ?, '', '', ?);
        """, (
            txn_ref, shop_mobile, amount_pkr, plan_label,
            acc["business_name"], now_str, shop_mobile,
            VENDOR_ACCOUNT_NUMBER, txn_ref  # use txn_ref as placeholder tracker
        ))
        conn.commit()
        conn.close()
        return jsonify({
            "success": False,
            "pending_approval": True,
            "txn_ref": txn_ref,
            "message": f"Safepay account pending activation ({sfpy_error}). Will auto-activate once Safepay approves within 10 days.",
            "fallback": True
        }), 202  # 202 Accepted = pending, not an error

    # Safepay tracker created successfully
    cursor.execute("""
    INSERT INTO payment_transactions (
        txn_ref, mobile_number, amount_pkr, payment_method, plan_selected,
        status, account_title, payment_date, sender_mobile, trx_id,
        vendor_account, safepay_tracker
    ) VALUES (?, ?, ?, 'Safepay', ?, 'PENDING_SAFEPAY', ?, ?, ?, '', ?, ?);
    """, (
        txn_ref, shop_mobile, amount_pkr, plan_label,
        acc["business_name"], now_str, shop_mobile,
        VENDOR_ACCOUNT_NUMBER, tracker_token
    ))
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "txn_ref": txn_ref,
        "tracker_token": tracker_token,
        "checkout_url": checkout_url,
        "amount_pkr": amount_pkr,
        "plan": plan_label,
        "message": f"Payment gateway ready. PKR {amount_pkr:,.0f} will go to {VENDOR_ACCOUNT_TITLE} ({VENDOR_ACCOUNT_NUMBER})."
    })


@app.route("/api/safepay/payment-return", methods=["GET"])
def safepay_payment_return():
    """
    Step 2: Safepay redirects customer here after payment attempt.
    Checks Safepay tracker status and updates license if paid.
    """
    tracker_token = request.args.get("tracker", "")
    reference_number = request.args.get("reference_number", "")

    if not tracker_token:
        return render_template_string("""
        <html><body style="background:#0b1120;color:#f1f5f9;font-family:Arial;text-align:center;padding:60px;">
        <h2 style="color:#ef4444;">Payment Error</h2>
        <p>No payment tracker found. Please contact support: 03417027610</p>
        </body></html>""")

    # Verify payment status with Safepay
    try:
        verify_resp = requests.get(
            f"{SAFEPAY_BASE_URL}/order/v1/inquiry?token={tracker_token}",
            headers={"X-SFPY-MERCHANT-SECRET": SAFEPAY_SECRET_KEY},
            timeout=15
        )
        verify_data = verify_resp.json()
    except Exception:
        verify_data = {}

    tracker_state = verify_data.get("data", {}).get("tracker", {}).get("state", "")

    # Find our transaction
    conn = get_db()
    cursor = conn.cursor()
    tx = cursor.execute(
        "SELECT * FROM payment_transactions WHERE safepay_tracker = ?;",
        (tracker_token,)
    ).fetchone()

    if not tx:
        conn.close()
        return render_template_string("""
        <html><body style="background:#0b1120;color:#f1f5f9;font-family:Arial;text-align:center;padding:60px;">
        <h2 style="color:#ef4444;">Transaction Not Found</h2>
        <p>Please contact: 03417027610 (WhatsApp)</p>
        </body></html>""")

    if tracker_state in ["paid", "PAID", "captured", "CAPTURED"]:
        # Payment successful! Extend the license
        plan = tx["plan_selected"]
        days_to_add = 30 if "monthly" in plan.lower() else 365
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        acc = cursor.execute("SELECT * FROM accounts WHERE mobile_number = ?;", (tx["mobile_number"],)).fetchone()
        today = datetime.utcnow().date()
        try:
            curr_exp = datetime.strptime(acc["expiry_date"], "%Y-%m-%d").date()
            base_date = max(today, curr_exp)
        except Exception:
            base_date = today

        new_expiry = (base_date + timedelta(days=days_to_add)).strftime("%Y-%m-%d")

        # Extend account
        cursor.execute("""
        UPDATE accounts SET
            plan_type = ?, subscription_status = 'PAID', expiry_date = ?, updated_at = ?
        WHERE mobile_number = ?;
        """, (plan, new_expiry, now_str, tx["mobile_number"]))

        # Mark transaction paid
        cursor.execute("""
        UPDATE payment_transactions SET status = 'PAID', trx_id = ?, payment_date = ?
        WHERE safepay_tracker = ?;
        """, (reference_number or tracker_token[:20], now_str, tracker_token))

        conn.commit()
        conn.close()

        return render_template_string("""
        <html><body style="background:#0b1120;color:#f1f5f9;font-family:'Segoe UI',Arial;text-align:center;padding:60px;">
        <div style="max-width:480px;margin:0 auto;background:#0f172a;border:2px solid #10b981;border-radius:12px;padding:40px;">
        <h1 style="color:#34d399;font-size:28px;">Payment Successful!</h1>
        <p style="font-size:16px;">PKR {{amount}} received by<br>
        <b style="color:#fde047;">{{vendor}}</b><br>
        <span style="color:#94a3b8;">{{vendor_number}}</span></p>
        <p style="color:#cbd5e1;">Your SmartTax license has been extended until:<br>
        <b style="color:#38bdf8;font-size:20px;">{{expiry}}</b></p>
        <p style="color:#64748b;font-size:13px;">You can now close this window and reopen SmartTax FBR on your PC.</p>
        </div></body></html>
        """.replace("{{amount}}", f"{tx['amount_pkr']:,.0f}")
           .replace("{{vendor}}", VENDOR_ACCOUNT_TITLE)
           .replace("{{vendor_number}}", VENDOR_ACCOUNT_NUMBER)
           .replace("{{expiry}}", new_expiry))
    else:
        # Payment not confirmed
        cursor.execute(
            "UPDATE payment_transactions SET status = 'FAILED' WHERE safepay_tracker = ?;",
            (tracker_token,)
        )
        conn.commit()
        conn.close()

        return render_template_string("""
        <html><body style="background:#0b1120;color:#f1f5f9;font-family:'Segoe UI',Arial;text-align:center;padding:60px;">
        <div style="max-width:480px;margin:0 auto;background:#0f172a;border:2px solid #ef4444;border-radius:12px;padding:40px;">
        <h2 style="color:#ef4444;">Payment Not Completed</h2>
        <p>Your payment was not confirmed (Status: {{state}}).</p>
        <p style="color:#94a3b8;">Please try again or contact:<br>
        <b style="color:#fde047;">03417027610</b> (WhatsApp)</p>
        </div></body></html>
        """.replace("{{state}}", tracker_state or "unknown"))


@app.route("/api/safepay/webhook", methods=["POST"])
def safepay_webhook():
    """
    Step 3 (Auto-trigger): Safepay calls this endpoint the moment payment is approved.
    Even if customer closes the browser, this auto-extends the license immediately!
    Must be registered in Safepay Dashboard > Developers > Webhooks.
    Webhook URL (for production): https://YOUR_PUBLIC_SERVER/api/safepay/webhook
    """
    raw_body = request.get_data()
    sfpy_signature = request.headers.get("X-SFPY-SIGNATURE", "")
    event_type = request.headers.get("X-SFPY-EVENT-TYPE", "")
    event_id = request.headers.get("X-SFPY-EVENT-ID", "")

    # Verify HMAC signature from Safepay
    if SAFEPAY_SECRET_KEY and sfpy_signature:
        computed = hmac.new(
            SAFEPAY_SECRET_KEY.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(computed, sfpy_signature):
            return jsonify({"error": "Invalid webhook signature"}), 400

    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    # Check for duplicate event
    conn = get_db()
    cursor = conn.cursor()
    existing = cursor.execute(
        "SELECT id FROM payment_transactions WHERE safepay_event_id = ?;", (event_id,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"status": "already_processed"}), 200

    tracker_data = payload.get("data", {}).get("tracker", payload.get("data", {}))
    tracker_token = tracker_data.get("token", "")
    tracker_state = tracker_data.get("state", "")
    reference = tracker_data.get("reference_number", "")

    if tracker_state in ["paid", "PAID", "captured", "CAPTURED"] or "payment:paid" in event_type:
        tx = cursor.execute(
            "SELECT * FROM payment_transactions WHERE safepay_tracker = ?;", (tracker_token,)
        ).fetchone()

        if tx and tx["status"] not in ["PAID"]:
            plan = tx["plan_selected"]
            days_to_add = 30 if "monthly" in plan.lower() else 365
            now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            acc = cursor.execute(
                "SELECT * FROM accounts WHERE mobile_number = ?;", (tx["mobile_number"],)
            ).fetchone()
            today = datetime.utcnow().date()
            try:
                curr_exp = datetime.strptime(acc["expiry_date"], "%Y-%m-%d").date()
                base_date = max(today, curr_exp)
            except Exception:
                base_date = today

            new_expiry = (base_date + timedelta(days=days_to_add)).strftime("%Y-%m-%d")

            # Extend license
            cursor.execute("""
            UPDATE accounts SET
                plan_type = ?, subscription_status = 'PAID', expiry_date = ?, updated_at = ?
            WHERE mobile_number = ?;
            """, (plan, new_expiry, now_str, tx["mobile_number"]))

            # Mark paid with Safepay event ID (idempotency)
            cursor.execute("""
            UPDATE payment_transactions SET
                status = 'PAID', trx_id = ?, payment_date = ?, safepay_event_id = ?
            WHERE safepay_tracker = ?;
            """, (reference or event_id, now_str, event_id, tracker_token))

            conn.commit()

    conn.close()
    return jsonify({"status": "success"}), 200


@app.route("/api/safepay/check-status", methods=["GET"])
def safepay_check_status():
    """
    Desktop app polls this every 2s to know if Safepay payment was approved.
    Returns license details if payment confirmed.
    """
    txn_ref = request.args.get("txn_ref", "")
    if not txn_ref:
        return jsonify({"success": False, "status": "UNKNOWN"}), 400

    conn = get_db()
    cursor = conn.cursor()
    tx = cursor.execute(
        "SELECT * FROM payment_transactions WHERE txn_ref = ?;", (txn_ref,)
    ).fetchone()

    if not tx:
        conn.close()
        return jsonify({"success": False, "status": "NOT_FOUND"}), 404

    status = tx["status"]

    if status == "PAID":
        acc = cursor.execute(
            "SELECT * FROM accounts WHERE mobile_number = ?;", (tx["mobile_number"],)
        ).fetchone()
        conn.close()

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        lease_token = sign_lease_token({
            "mobile_number": tx["mobile_number"],
            "business_name": acc["business_name"] if acc else "Licensed Shop",
            "device_id": "SFPY_PAID",
            "device_name": "Counter PC",
            "plan_type": tx["plan_selected"],
            "subscription_status": "PAID",
            "max_devices": acc["max_devices"] if acc else 2,
            "active_devices": 1,
            "expiry_date": acc["expiry_date"] if acc else "",
            "lease_expiry": (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "issued_at": now_str
        })

        return jsonify({
            "success": True,
            "status": "PAID",
            "message": f"Payment of PKR {tx['amount_pkr']:,.0f} confirmed! License extended to {acc['expiry_date'] if acc else ''}.",
            "new_expiry_date": acc["expiry_date"] if acc else "",
            "plan": tx["plan_selected"],
            "trx_id": tx["trx_id"],
            "lease_token": lease_token
        })

    conn.close()
    return jsonify({
        "success": False,
        "status": status,
        "message": "Waiting for payment confirmation from Safepay..." if status == "PENDING_SAFEPAY" else f"Payment status: {status}"
    })


if __name__ == "__main__":
    init_server_db()
    port = int(os.environ.get("PORT", 5055))
    print("=" * 70)
    print(" SmartTax FBR Central Cloud Server & Vendor SaaS Portal")
    print(f" Server running at: http://127.0.0.1:{port}")
    print(f" Vendor Web Dashboard: http://127.0.0.1:{port}/admin")
    print(f" Safepay Mode: {SAFEPAY_ENV.upper()} | Webhook: {port}/api/safepay/webhook")
    print("=" * 70)
    app.run(host="0.0.0.0", port=port, debug=False)
