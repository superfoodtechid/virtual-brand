"""
src/VB/core/browser.py
======================
Custom Shopee Partner login and session management for VB.
Supports dynamic Chrome profile isolation and dynamic session file paths 
to enable concurrent/parallel execution of multiple accounts.
"""

import os
import json
import time
import random
from datetime import datetime
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core.logger import get_logger
from core.otp import get_latest_otp

log = get_logger("browser_vb")

# ── Constants ──────────────────────────────────────────────────────────────────
PARTNER_DASHBOARD = "https://partner.shopee.co.id/food/dashboard"
TOKEN_TRIGGER_PAGE = "https://partner.shopee.co.id/settings/shopee-food/business-hours-settings"
VALIDATE_URL = "https://api.partner.shopee.co.id/nb/mss/web-api/PartnerAccountServer/GetUserInfo"
SHOPEE_IMG_BASE = "https://down-id.img.susercontent.com/file"


# ── Helpers ────────────────────────────────────────────────────────────────────

def human_like_typing(element, text: str):
    element.send_keys(text)

def build_img_url(img_id: str) -> str:
    if not img_id: return ""
    return f"{SHOPEE_IMG_BASE}/{img_id}"


# ── Session Persistence ────────────────────────────────────────────────────────

def get_session_file_path(account_name: str) -> Path:
    """Returns the dedicated session file path for a given account name."""
    # Place session files inside src/VB/shopee/sessions/ directory
    session_dir = Path(__file__).parent.parent / "shopee" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / f"session_{account_name}.json"

def save_session(account_name: str, tob_token: str, entity_id: str, extra_cookies: dict = None):
    session_file = get_session_file_path(account_name)
    payload = {
        "shopee_tob_token": tob_token,
        "shopee_tob_entity_id": entity_id,
        "saved_at": datetime.now().isoformat(),
        "extra_cookies": extra_cookies or {},
    }
    session_file.write_text(json.dumps(payload, indent=2))
    log.info(f"✅ [SESSION] Saved session for '{account_name}' to {session_file.name}")

def load_session(account_name: str) -> dict | None:
    session_file = get_session_file_path(account_name)
    if not session_file.exists(): return None
    try:
        data = json.loads(session_file.read_text())
        if data.get("shopee_tob_token"):
            log.info(f"📂 [SESSION] Found cached session for '{account_name}' (saved at {data.get('saved_at')})")
            return data
    except Exception as e:
        log.debug(f"Failed to load session for '{account_name}': {e}")
    return None

def validate_session(tob_token: str, entity_id: str) -> bool:
    log.debug("🔍 Validating session token...")
    headers = {
        "Cookie": f"shopee_tob_entity_id={entity_id}; shopee_tob_token={tob_token}",
        "x-merchant-token": tob_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        resp = requests.post(VALIDATE_URL, json={}, headers=headers, timeout=8)
        data = resp.json()
        # GetUserInfo returns message "success" or code 0 when valid
        if data.get("message") == "success" or data.get("code") == 0:
            log.info("✅ [SESSION] Session is valid.")
            return True
    except: pass
    return False


# ── Token Extraction ───────────────────────────────────────────────────────────

def extract_tokens_from_driver(driver) -> tuple:
    tob_token = None
    entity_id = None
    for c in driver.get_cookies():
        name = c["name"]
        val = c["value"]
        if name == "shopee_tob_token": 
            tob_token = val
        elif name.lower() in ["shopee_tob_entity_id", "shopee_foody_mid", "x-merchant-id", "spc_merchant_id", "merchant_id", "shopid", "shop_id"]:
            if val and not entity_id: entity_id = val
            
    if not entity_id:
        try: 
            api_js = """
            var done = arguments[arguments.length - 1];
            let token = document.cookie.split('; ').find(row => row.startsWith('shopee_tob_token='))?.split('=')[1];
            fetch('https://api.partner.shopee.co.id/nb/mss/web-api/PartnerAccountServer/GetUserInfo', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'x-merchant-token': token || ''
                },
                body: '{}',
                credentials: 'include'
            })
            .then(r => r.json())
            .then(j => done(j.data ? j.data.merchantId : null))
            .catch(() => done(null));
            """
            entity_id = driver.execute_async_script(api_js)
        except: pass

    if not entity_id:
        try: 
            entity_id = driver.execute_script("""
                let ids = [];
                for (let i = 0; i < localStorage.length; i++) {
                    let k = localStorage.key(i);
                    let v = localStorage.getItem(k);
                    if (/^\\d{6,12}$/.test(v)) ids.push(v);
                }
                let specific = localStorage.getItem('shopee_tob_entity_id') || 
                               localStorage.getItem('shopee_foody_mid') || 
                               localStorage.getItem('merchant_id') || 
                               localStorage.getItem('spc_merchant_id');
                if (specific) return specific;
                return ids[0] || null;
            """)
        except: pass
    
    return tob_token, (str(entity_id).strip() if entity_id else None)

def get_all_cookies_dict(driver) -> dict:
    return {c["name"]: c["value"] for c in driver.get_cookies()}

def _trigger_and_extract_tokens(driver) -> tuple:
    log.debug("  🔄 Triggering fresh token issuance...")
    try:
        try: driver.delete_cookie("shopee_tob_token")
        except: pass
        driver.get(TOKEN_TRIGGER_PAGE)
        for _ in range(10):
            tob_token, entity_id = extract_tokens_from_driver(driver)
            if tob_token: return tob_token, entity_id
            time.sleep(1)
    except: pass
    return extract_tokens_from_driver(driver)


# ── Driver Initialization ──────────────────────────────────────────────────────

def _init_driver(headless: bool, account_name: str):
    options = Options()
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    
    # Isolate Chrome data directories per account
    script_dir = Path(__file__).parent.parent
    profile_dir = script_dir / "data" / "chrome_profiles" / account_name
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    options.add_argument(f"--profile-directory=profile_{account_name}")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.set_page_load_timeout(60)
    return driver


# ── Login Logic ────────────────────────────────────────────────────────────────

def _perform_login(driver, wait, username: str = None, password: str = None, phone: str = None) -> bool:
    log.info("➡️  [AUTH] Starting login sequence...")
    if not username and not password and not phone:
        raise Exception("Shopee credentials are not configured! Please check your credentials file.")
    
    # Prioritize username/password. Phone is only used as fallback if username/password are absent.
    use_phone = phone and not (username and password)
    if use_phone:
        try:
            wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Log in dengan no. HP')]"))).click()
            time.sleep(1)
        except: pass
        phone_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='tel']")))
        phone_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
        human_like_typing(phone_input, phone)
        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Selanjutnya')]"))).click()
    else:
        time.sleep(2)
        user_input = None
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input")
            for inp in inputs:
                p = (inp.get_attribute("placeholder") or "").lower()
                n = (inp.get_attribute("name") or "").lower()
                t = (inp.get_attribute("type") or "").lower()
                if inp.is_displayed() and (t == "text" or "user" in n or "phone" in n or "handphone" in p or "username" in p):
                    user_input = inp
                    break
        except: pass

        if not user_input:
            for sel in ["input[name='userName']", "input[placeholder*='handphone']", "input[placeholder*='Username']", "input[type='text']"]:
                try:
                    el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
                    if el.is_displayed(): user_input = el; break
                except: continue
        
        if not user_input:
            log.error(f"❌ Failed to find Username field. URL: {driver.current_url}")
            raise Exception("Could not find Username input field")

        pass_input = None
        for sel in ["input[type='password']", "input[placeholder='Password']"]:
            try:
                el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
                if el.is_displayed(): pass_input = el; break
            except: continue
            
        if not pass_input: raise Exception("Could not find Password input field")

        user_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
        human_like_typing(user_input, username)
        pass_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
        human_like_typing(pass_input, password)
        
        login_btn = None
        for btn_sel in ["//button[contains(., 'Masuk') or contains(., 'Log In')]", "//button[@type='submit']"]:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, btn_sel)))
                if btn.is_displayed(): login_btn = btn; break
            except: continue

        if login_btn: login_btn.click()
        else: raise Exception("Could not find Login button")

    log.debug("  ⏳ Waiting for post-login redirect or OTP...")
    start_wait = time.time()
    otp_attempted = False
    last_resend_time = time.time()
    while time.time() - start_wait < 300:
        if "/authenticate/login" not in driver.current_url: break
        try:
            otp_input = None
            for sel in ["input.shopee-otp-input__input", ".shopee-otp-input input", "input[maxlength='6']"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed(): otp_input = el; break
                if otp_input: break

            if otp_input:
                log.warning(f"⚠️ [OTP REQUIRED] Akun '{username or phone}' memerlukan kode verifikasi OTP.")
                otp_code = input(f"🔑 Masukkan 6-digit OTP (atau tekan Enter jika Anda mengisinya langsung di browser): ").strip()
                if otp_code:
                    log.info(f"⌨️  [AUTH] Menginput OTP: {otp_code}")
                    try:
                        otp_input.click()
                        otp_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
                        human_like_typing(otp_input, otp_code)
                        time.sleep(0.5)
                        otp_input.send_keys(Keys.ENTER)
                    except Exception as err:
                        log.warning(f"⚠️ Gagal memasukkan OTP ke elemen browser: {err}")
                    time.sleep(5)
                else:
                    log.info("ℹ️ Menunggu 10 detik untuk input langsung di browser...")
                    time.sleep(10)
                otp_attempted = True
                
                # Check resend button if needed
                if time.time() - last_resend_time > 65:
                    try:
                        btns = driver.find_elements(By.XPATH, "//button[contains(., 'Kirim ulang') or contains(., 'Resend')]")
                        for b in btns:
                            if b.is_displayed() and not any(c.isdigit() for c in b.text):
                                b.click()
                                last_resend_time = time.time()
                                log.info("🔄 Mengirim ulang kode OTP...")
                                break
                    except: pass

            if otp_attempted or not otp_input:
                for cs in ["//button[contains(., 'Lanjutkan')]", "//button[contains(., 'Confirm')]", ".shopee-button--primary"]:
                    btns = driver.find_elements(By.XPATH, cs) if cs.startswith("//") else driver.find_elements(By.CSS_SELECTOR, cs)
                    for b in btns:
                        if b.is_displayed() and "ulang" not in b.text.lower():
                            b.click(); time.sleep(1); break
        except: pass
        time.sleep(2)

    return True


# ── Core Session Retrieval ─────────────────────────────────────────────────────

def get_session(account_name: str, username: str = None, password: str = None, phone: str = None, 
                headless: bool = True, close_browser: bool = True) -> dict | None:
    """
    Retrieves Shopee TOB tokens and cookies for a specific account.
    Loads from cache if valid. Performs browser login/OTP flow if necessary.
    """
    # 1. Attempt to load from cached session first
    saved = load_session(account_name)
    if saved:
        # Validate saved tokens via API
        if validate_session(saved["shopee_tob_token"], saved["shopee_tob_entity_id"]):
            return {
                "shopee_tob_token": saved["shopee_tob_token"],
                "shopee_tob_entity_id": saved["shopee_tob_entity_id"],
                "extra_cookies": saved.get("extra_cookies", {})
            }
        else:
            log.warning(f"⚠️ [SESSION] Cached session for '{account_name}' is invalid/expired. Refreshing via browser...")

    # 2. If cached session is missing or invalid, run Selenium login with retries
    for attempt in range(3):
        log.info(f"🌐 [BROWSER] Launching isolated browser for '{account_name}' (headless={headless}, attempt={attempt+1}/3)...")
        driver = _init_driver(headless=headless, account_name=account_name)
        wait = WebDriverWait(driver, 30)
        session_success = False

        try:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(4)
            
            # If attempt > 0, force a fresh login by clearing cookies
            if attempt > 0:
                log.info(f"⚠️ [SESSION] Forcing fresh login for '{account_name}' (Attempt {attempt+1})...")
                driver.delete_all_cookies()
                driver.get("https://partner.shopee.co.id/login")
                time.sleep(4)
            
            is_logged_in = False
            current_url = driver.current_url.lower()
            if ("dashboard" in current_url or "merchant-selector" in current_url) and attempt == 0:
                log.info(f"✅ [SESSION] Browser is already logged in for '{account_name}'.")
                is_logged_in = True
            elif attempt == 0:
                # Try injection of old cookies to restore Selenium state if possible
                if saved:
                    log.debug(f"🔍 Attempting to restore browser cookies for '{account_name}'...")
                    driver.add_cookie({"name": "shopee_tob_token", "value": saved["shopee_tob_token"]})
                    if saved.get("shopee_tob_entity_id"):
                        driver.add_cookie({"name": "shopee_tob_entity_id", "value": saved["shopee_tob_entity_id"]})
                    for n, v in saved.get("extra_cookies", {}).items():
                        try: driver.add_cookie({"name": n, "value": v})
                        except: pass
                    driver.refresh()
                    time.sleep(4)
                    current_url = driver.current_url.lower()
                    if "dashboard" in current_url or "merchant-selector" in current_url:
                        log.info(f"✅ [SESSION] Restored browser login from saved cookies for '{account_name}'.")
                        is_logged_in = True

            if not is_logged_in:
                log.info(f"⚠️ [SESSION] Logging in to '{account_name}'...")
                if "/login" not in driver.current_url.lower() and "authenticate" not in driver.current_url.lower():
                    driver.get("https://partner.shopee.co.id/login")
                    time.sleep(5)
                
                current_url = driver.current_url.lower()
                if "login" in current_url or "authenticate" in current_url or "about:blank" in current_url:
                    # Always prefer username/password; phone is only fallback
                    success = _perform_login(driver, wait, username, password, phone if not (username and password) else None)
                    if not success:
                        log.error(f"❌ [AUTH] _perform_login failed for '{account_name}'.")
                        driver.quit()
                        continue
                    
                time.sleep(3)
                # Handle merchant selector page (onboarding selector) by selecting first available
                if "onboarding" in driver.current_url or "merchant-selector" in driver.current_url:
                    log.info("📍 [SESSION] Onboarding/Selector page detected. Auto-selecting first merchant/portal...")
                    bypass_js = """
                        var loaders = document.querySelectorAll('.ant-spin, [class*="loading"], .shopee-loading, .ant-spin-nested-loading');
                        loaders.forEach(el => el.remove());
                        var target = document.querySelector('.merchantInfo, .ant-list-item, .shop-name');
                        if (target) {
                            target.scrollIntoView({block: 'center'});
                            target.click();
                            setTimeout(() => {
                                var btns = document.querySelectorAll('button');
                                for (var b of btns) {
                                    var bText = (b.innerText || "").toLowerCase();
                                    if (bText.includes('masuk') || bText.includes('konfirmasi') || bText.includes('lanjutkan') || bText.includes('ok')) {
                                        b.click();
                                    }
                                }
                            }, 500);
                            return true;
                        }
                        return false;
                    """
                    for loop_attempt in range(10):
                        if driver.execute_script(bypass_js):
                            try:
                                wait.until(lambda d: "/food/dashboard" in d.current_url)
                                log.debug("  ✅ Landed on dashboard.")
                                break
                            except: pass
                        time.sleep(1)
                    time.sleep(2)
            
            # Ensure we are on dashboard or settings to trigger tokens
            if "/food/dashboard" not in driver.current_url:
                driver.get(PARTNER_DASHBOARD)
                time.sleep(2)

            # 3. Final Token Extraction
            t, eid = _trigger_and_extract_tokens(driver)
            if not t:
                log.warning(f"⚠️ [SESSION] Token extraction failed for '{account_name}'.")
                driver.quit()
                continue
            
            all_c = get_all_cookies_dict(driver)
            save_session(account_name, t, eid or "", extra_cookies=all_c)
            res = {"shopee_tob_token": t, "shopee_tob_entity_id": eid or "", "extra_cookies": all_c}
            if not close_browser: res["driver"] = driver
            session_success = True
            return res

        except Exception as e:
            log.error(f"❌ [BROWSER] Session error for '{account_name}' on attempt {attempt+1}: {e}")
        finally:
            if (close_browser or not session_success) and driver is not None:
                try:
                    driver.quit()
                except Exception as e:
                    log.debug(f"Failed to quit driver: {e}")

    log.error(f"❌ [BROWSER] Max login retries reached for '{account_name}'.")
    return None

def refresh_tokens(driver, account_name: str) -> dict:
    t, eid = _trigger_and_extract_tokens(driver)
    all_c = get_all_cookies_dict(driver)
    save_session(account_name, t, eid or "", extra_cookies=all_c)
    return {"shopee_tob_token": t, "shopee_tob_entity_id": eid or "", "extra_cookies": all_c}
