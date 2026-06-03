"""
src/VB/shopee/init_sessions.py
==============================
Checks the authentication state of all 4 portals sequentially.
If a portal's session is missing or expired, it launches Selenium 
sequentially to allow OTP entry (polled from Google Sheet or manual fallback) 
and caches the active session token.
"""

import os
import sys
import json
from pathlib import Path

# Add parent directory (VB/) to sys.path so core/ imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.browser import get_session, load_session, validate_session
from core.logger import get_logger

log = get_logger("init_sessions")

CRED_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYSUnKOqk29LCktTxdb0wPLbWMbRaWRP3eC_UA4AwYod1FW6zDMhtLMC5ghIvot2B8upCDfBsn-TCP/pub?gid=565510790&single=true&output=csv"

def get_vb_portals() -> list:
    """
    Fetches the portal credentials dynamically from Google Sheets.
    Filters for Role == 'Owner'. Caches the results locally.
    Falls back to cached JSON if spreadsheet fetch fails.
    """
    import pandas as pd
    cred_file = Path(__file__).parent / "credentials_vb.json"
    portals = []
    
    log.info("🌐 Fetching portal credentials dynamically from oogle Sheets...")
    try:
        df = pd.read_csv(CRED_URL)
        owner_df = df[df['Role'].str.strip().str.lower() == 'owner'].copy()
        
        for _, row in owner_df.iterrows():
            portal_val = str(row.get('Portal', '')).strip()
            account_name = f"portal_{portal_val.lower()}"
            
            # Format phone number safely
            phone_val = row.get('Phone')
            if pd.isna(phone_val):
                phone_str = ""
            else:
                try:
                    phone_str = str(int(float(phone_val))).strip()
                except:
                    phone_str = str(phone_val).strip()
            
            portals.append({
                "account_name": account_name,
                "username": str(row.get('Username', '')).strip(),
                "password": str(row.get('Password', '')).strip(),
                "phone": phone_str,
                "merchant_name": str(row.get('Merchant Name', '')).strip()
            })
            
        if portals:
            # Cache the portals to credentials_vb.json
            with open(cred_file, "w") as f:
                json.dump({"portals": portals}, f, indent=2)
            log.info(f"💾 Saved {len(portals)} portal credentials to local cache: {cred_file.name}")
            return portals
    except Exception as e:
        log.warning(f"⚠️ Failed to fetch portals from Google Sheets: {e}")
        
    # Fallback to local cache
    if cred_file.exists():
        log.info(f"📂 Falling back to local cache: {cred_file.name}")
        try:
            with open(cred_file, "r") as f:
                cached = json.load(f)
                return cached.get("portals", [])
        except Exception as err:
            log.error(f"❌ Failed to read cache: {err}")
            
    return []

def initialize_all_sessions(headless_on_login=False, only_portal=None):
    """
    Loops through the configured portal accounts, checks their session status,
    and runs the Selenium login sequence sequentially if any account is unauthenticated.
    """
    portals = get_vb_portals()
    if not portals:
        log.error("❌ No portals retrieved (dynamic fetch and cache fallback both failed).")
        return False

    if only_portal:
        target = only_portal.strip().lower()
        if not target.startswith("portal_"):
            target = f"portal_{target}"
        portals = [p for p in portals if p["account_name"].lower() == target]
        if not portals:
            log.error(f"❌ Portal '{only_portal}' tidak ditemukan dalam daftar kredensial.")
            return False

    log.info("==================================================")
    log.info("🔑 Initializing & Verifying Shopee Sessions Sequentially")
    log.info(f"   Found {len(portals)} portal(s) in configuration.")
    log.info("==================================================")

    all_ok = True
    for i, portal in enumerate(portals):
        name = portal["account_name"]
        username = portal.get("username")
        password = portal.get("password")
        phone = portal.get("phone")
        merchant = portal.get("merchant_name")

        log.info(f"\n🔍 [{i+1}/{len(portals)}] Checking session for: {name} ({merchant})")

        # Check existing session cache
        saved = load_session(name)
        if saved and validate_session(saved["shopee_tob_token"], saved["shopee_tob_entity_id"]):
            log.info(f"✅ Sesi '{name}' aktif dan valid. Tidak perlu login ulang.")
            continue

        log.warning(f"⚠️ Sesi '{name}' kedaluwarsa atau belum ada. Memulai browser login...")
        
        # Sequentially launch login
        try:
            session = get_session(
                account_name=name,
                username=username or None,
                password=password or None,
                phone=phone or None,
                headless=headless_on_login, # Usually non-headless or headless depending on flag
                close_browser=True
            )
            if session:
                log.info(f"🎉 Sesi '{name}' berhasil didapatkan dan disimpan!")
            else:
                log.error(f"❌ Gagal masuk ke akun '{name}'. Silakan periksa kembali kredensial/OTP.")
                all_ok = False
        except Exception as e:
            log.error(f"❌ Error saat login '{name}': {e}")
            all_ok = False

    log.info("\n==================================================")
    if all_ok:
        log.info("🎉 SUCCESS: Semua portal target telah terautentikasi!")
    else:
        log.warning("⚠️ WARNING: Beberapa sesi gagal diinisialisasi. Periksa log di atas.")
    log.info("==================================================\n")
    return all_ok

if __name__ == "__main__":
    # If run directly, run non-headless so user can see and debug if necessary
    import argparse
    parser = argparse.ArgumentParser(description="Initialize Shopee VB Sessions")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--only", type=str, help="Only initialize session for a specific portal (e.g. portal_w or w)")
    args = parser.parse_args()
    
    initialize_all_sessions(headless_on_login=args.headless, only_portal=args.only)
