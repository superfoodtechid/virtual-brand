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
LOGOUT_KEYWORDS = ["log out", "logout", "keluar", "sign out", "signout"]


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


def _detect_and_recover_logout(driver) -> bool:
    """
    Safety-net: detects if the browser accidentally got logged out.
    Attempts re-entry using the existing Chrome profile cookies (no OTP needed).
    Returns True if recovery succeeded, False otherwise.
    """
    current = driver.current_url.lower()
    logged_out = (
        "/login" in current
        or "/authenticate/login" in current
        or "about:blank" in current
    )
    if not logged_out:
        return False  # Not logged out — nothing to do

    log.warning("⚠️  [LOGOUT-RECOVERY] Accidental logout detected! Trying to recover via Chrome profile...")
    try:
        driver.get(PARTNER_DASHBOARD)
        time.sleep(5)
        recovered_url = driver.current_url.lower()
        if "dashboard" in recovered_url or "merchant-selector" in recovered_url:
            log.info("✅ [LOGOUT-RECOVERY] Recovered without OTP — Chrome profile cookies still valid.")
            return True
    except Exception as err:
        log.warning(f"⚠️  [LOGOUT-RECOVERY] Recovery attempt failed: {err}")

    log.warning("⚠️  [LOGOUT-RECOVERY] Could not recover automatically — full re-login may be needed.")
    return False

def _deliberate_logout_and_relogin(
    driver,
    username: str = None,
    password: str = None,
    phone:    str = None,
) -> bool:
    """
    Intentional recovery strategy for when merchant cannot be detected.

    Flow:
      1. Click the profile area  →  open dropdown
      2. Click 'Log Out' from the dropdown
      3. Click the confirmation 'Log Out' button
      4. Try Chrome profile auto-login (fast path, no OTP)
      5. Fallback: enter credentials (username/password) via _perform_login()
      Returns True if back on the portal, False on complete failure.
    """
    log.info("🔄 [LOGOUT-RELOGIN] Initiating deliberate logout for clean session recovery...")
    try:
        # ── Step 1: Navigate to a page that has the profile dropdown ───
        if "/food/" not in driver.current_url and "/settings/" not in driver.current_url:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(3)

        # ── Step 2: Open the profile/merchantName dropdown with retries ───
        profile_clicked = False
        for attempt in range(3):
            # Dismiss any blocking overlays/notifications
            driver.execute_script("""
                document.querySelectorAll('.ant-notification, .ant-modal, .ant-notification-notice, .ant-message').forEach(el => el.remove());
            """)
            
            # Find the WebElement via JS returning it
            profile_el = driver.execute_script("""
                var profileEl = null;
                // 1. Try specific CSS selectors first
                for (var sel of ['.merchantName', '.user-info', '.ant-dropdown-trigger', '.ant-dropdown-link']) {
                    var el = document.querySelector(sel);
                    if (el && el.offsetHeight > 0) {
                        profileEl = el;
                        break;
                    }
                }
                // 2. Search for element containing "Admin:"
                if (!profileEl) {
                    var elements = Array.from(document.querySelectorAll('span, p, div, li, a'));
                    for (var el of elements) {
                        var text = (el.innerText || '').trim();
                        if (text.includes('Admin:') && text.length < 30 && el.offsetHeight > 0) {
                            profileEl = el;
                            break;
                        }
                    }
                }
                // 3. Fallback to last .ant-dropdown-trigger
                if (!profileEl) {
                    var triggers = Array.from(document.querySelectorAll('.ant-dropdown-trigger, .ant-dropdown-link'));
                    if (triggers.length > 0) {
                        profileEl = triggers[triggers.length - 1];
                    }
                }
                return profileEl;
            """)
            
            if profile_el:
                log.info(f"  📍 Found profile menu element (Attempt {attempt+1}). Dispatching JS click...")
                # Dispatch JS events
                driver.execute_script("""
                    var el = arguments[0];
                    var ev1 = new MouseEvent('mouseover', { bubbles: true, cancelable: true });
                    var ev2 = new MouseEvent('mouseenter', { bubbles: true, cancelable: true });
                    var ev3 = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
                    var ev4 = new MouseEvent('click', { bubbles: true, cancelable: true });
                    var ev5 = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
                    el.dispatchEvent(ev1);
                    el.dispatchEvent(ev2);
                    el.dispatchEvent(ev3);
                    el.dispatchEvent(ev4);
                    el.dispatchEvent(ev5);
                """, profile_el)
                time.sleep(1.5)
                
                # Check if dropdown is visible (ignoring hidden parents)
                has_dropdown = driver.execute_script("""
                    var targets = ['log out', 'logout', 'keluar'];
                    var candidates = Array.from(document.querySelectorAll('li, span, div, a'));
                    for (var el of candidates) {
                        var rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        if (el.closest('.ant-dropdown-hidden, [style*="display: none"], [style*="visibility: hidden"]')) continue;
                        var text = (el.innerText || '').trim().toLowerCase();
                        if (targets.some(function(k){ return text.includes(k); })) {
                            return true;
                        }
                    }
                    return false;
                """)
                
                if not has_dropdown:
                    log.info("  ⚠️ JS click did not reveal dropdown. Retrying with Selenium native ActionChains hover/click...")
                    try:
                        actions = ActionChains(driver)
                        actions.move_to_element(profile_el).perform()
                        time.sleep(0.5)
                        actions.click(profile_el).perform()
                        time.sleep(1.5)
                        
                        has_dropdown = driver.execute_script("""
                            var targets = ['log out', 'logout', 'keluar'];
                            var candidates = Array.from(document.querySelectorAll('li, span, div, a'));
                            for (var el of candidates) {
                                var rect = el.getBoundingClientRect();
                                if (rect.width === 0 || rect.height === 0) continue;
                                if (el.closest('.ant-dropdown-hidden, [style*="display: none"], [style*="visibility: hidden"]')) continue;
                                var text = (el.innerText || '').trim().toLowerCase();
                                if (targets.some(function(k){ return text.includes(k); })) {
                                    return true;
                                }
                            }
                            return false;
                        """)
                    except Exception as e:
                        log.warning(f"  ⚠️ ActionChains failed: {e}")
                
                if has_dropdown:
                    log.info("  ✅ Dropdown is now visible.")
                    profile_clicked = True
                    break
                else:
                    log.warning("  ⚠️ Dropdown menu elements not visible yet. Retrying...")
            else:
                log.warning(f"  ⚠️ Profile element not found on page (Attempt {attempt+1}). Retrying...")
            time.sleep(1.5)

        if not profile_clicked:
            log.warning("  ⚠️ Profile element or dropdown could not be opened.")
            return False

        # ── Step 3: Find and click 'Log Out' in the dropdown ────────────
        logout_el = driver.execute_script("""
            var targets = ['log out', 'logout', 'keluar'];
            var candidates = Array.from(document.querySelectorAll(
                'li.ant-menu-item, li[role="menuitem"], .ant-dropdown-menu-item,'
                + '[class*="menu-item"], span, div, a'
            ));
            for (var el of candidates) {
                var rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                if (el.closest('.ant-dropdown-hidden, [style*="display: none"], [style*="visibility: hidden"]')) continue;
                
                var text = (el.innerText || '').trim().toLowerCase();
                if (targets.some(function(k){ return text === k; })) {
                    // Walk up to the closest interactive wrapper (e.g. li or .ant-dropdown-menu-item)
                    var clickable = el.closest('li, button, a, [role="menuitem"], .ant-dropdown-menu-item') || el;
                    return clickable;
                }
            }
            return null;
        """)

        if not logout_el:
            log.warning("  ⚠️ 'Log Out' menu item not found in dropdown.")
            return False

        # Click it using Selenium
        try:
            log.info("  👈 Clicking 'Log Out' menu item...")
            logout_el.click()
        except Exception:
            # Fallback to ActionChains
            try:
                ActionChains(driver).move_to_element(logout_el).click().perform()
            except Exception as e:
                log.warning(f"  ⚠️ Selenium click failed: {e}. Trying JS MouseEvents as fallback...")
                driver.execute_script("""
                    var el = arguments[0];
                    var ev1 = new MouseEvent('mouseover', { bubbles: true, cancelable: true });
                    var ev2 = new MouseEvent('mouseenter', { bubbles: true, cancelable: true });
                    var ev3 = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
                    var ev4 = new MouseEvent('click', { bubbles: true, cancelable: true });
                    var ev5 = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
                    el.dispatchEvent(ev1); el.dispatchEvent(ev2); el.dispatchEvent(ev3); el.dispatchEvent(ev4); el.dispatchEvent(ev5);
                """, logout_el)
        
        time.sleep(1.5)  # Wait for confirmation dialog

        # ── Step 4: Click the 'Log Out' confirmation button with retries ────
        confirm_clicked = False
        for confirm_attempt in range(5):
            confirm_el = driver.execute_script("""
                var targets = ['log out', 'logout', 'keluar'];
                // ONLY look inside modal containers
                var modal = document.querySelector('.ant-modal-content, .ant-modal, .ant-dialog, .ant-modal-wrap');
                if (!modal) return null;
                
                var candidates = Array.from(modal.querySelectorAll('button, .ant-btn, [role="button"]'));
                for (var btn of candidates) {
                    var rect = btn.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    if (targets.some(function(k){ return text === k || text === ('confirm ' + k); })) {
                        // Walk up to the closest clickable element (e.g. button or .ant-btn)
                        var clickable = btn.closest('button, [role="button"], a, .ant-btn') || btn;
                        return clickable;
                    }
                }
                return null;
            """)
            
            if confirm_el:
                log.info(f"  📍 Found confirmation button on Attempt {confirm_attempt+1}. Clicking...")
                try:
                    confirm_el.click()
                except Exception as e:
                    log.warning(f"  ⚠️ Selenium click failed: {e}. Trying ActionChains...")
                    try:
                        ActionChains(driver).move_to_element(confirm_el).click().perform()
                    except Exception as e2:
                        log.warning(f"  ⚠️ ActionChains click failed: {e2}. Trying JS click...")
                        driver.execute_script("arguments[0].click();", confirm_el)
                
                time.sleep(2)
                # Verify if modal is gone
                modal_present = driver.execute_script("""
                    var modal = document.querySelector('.ant-modal-content, .ant-modal, .ant-dialog, .ant-modal-wrap');
                    return !!(modal && modal.offsetHeight > 0);
                """)
                if not modal_present:
                    log.info("  ✅ Modal disappeared. Logout confirmed.")
                    confirm_clicked = True
                    break
                else:
                    log.warning("  ⚠️ Modal is still present after click. Retrying...")
            else:
                log.warning(f"  ⚠️ Confirmation button/modal not found yet (Attempt {confirm_attempt+1}). Retrying...")
                time.sleep(1.5)

        if not confirm_clicked:
            log.warning("  ⚠️ Confirmation 'Log Out' button could not be clicked via UI.")
            
            # --- DEBUG SCREENSHOT JIKA KLIK GAGAL ---
            try:
                import os
                debug_dir = os.path.join(str(Path(__file__).resolve().parent.parent), "data", "debug")
                os.makedirs(debug_dir, exist_ok=True)
                ss_fail_path = os.path.join(debug_dir, "modal_fail_server.png")
                driver.save_screenshot(ss_fail_path)
                log.info(f"  📸 [DEBUG] Screenshot penyebab kegagalan klik disimpan di {ss_fail_path}")
            except Exception as e:
                pass
            # ----------------------------------------
            
            log.info("  🛡️ Mengaktifkan 'Soft Session Kill' Fallback (Hanya hapus Cookie Sesi)...")
            try:
                # Escape the modal just in case
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                pass
                
            try:
                # Hanya hapus cookie autentikasi utama yang menandakan status login
                auth_cookies = ['SPC_ST', 'SPC_U', 'SPC_T_ID', 'SPC_T_IV']
                for cookie_name in auth_cookies:
                    try:
                        driver.delete_cookie(cookie_name)
                    except:
                        pass
                
                # Bersihkan cache JWT / state auth dari LocalStorage
                driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                
                # JANGAN hapus SPC_F atau SPC_EC (Cookie Device Fingerprint) agar tidak trigger OTP!
                
                log.info("  ✅ Soft Session Kill dieksekusi. Sesi dibersihkan tanpa menghapus Device Fingerprint.")
                driver.refresh()
                time.sleep(3)
            except Exception as e:
                log.warning(f"  ⚠️ Soft Session Kill gagal: {e}")
                return False

        log.info("  ✅ Logout confirmed. Waiting for login page...")
        time.sleep(3)

        # ── Step 5a: Try Chrome profile auto-login (fast path) ──────────
        log.info("  🌐 Attempting Chrome profile auto-login...")
        driver.get(PARTNER_DASHBOARD)
        time.sleep(5)
        url_now = driver.current_url.lower()
        if "dashboard" in url_now or "merchant-selector" in url_now or "onboarding" in url_now:
            log.info("  ✅ [LOGOUT-RELOGIN] Auto-login via Chrome profile succeeded!")
            return True

        # ── Step 5b: Fallback — login dengan kredensial ────────────────
        log.info("  ⚠️ Chrome profile auto-login failed — logging in with credentials...")
        if not (username and password) and not phone:
            log.warning("  ⚠️ No credentials provided — cannot complete login.")
            return False

        # Navigate to login page if not already there
        current = driver.current_url.lower()
        if "login" not in current and "authenticate" not in current:
            driver.get("https://partner.shopee.co.id/login")
            time.sleep(4)

        wait = WebDriverWait(driver, 30)
        login_ok = _perform_login(driver, wait, username=username, password=password, phone=phone)
        if not login_ok:
            log.error("  ❌ Credential login failed.")
            return False

        # Wait for dashboard or merchant selector after login
        time.sleep(3)
        url_after = driver.current_url.lower()
        if "dashboard" in url_after or "merchant-selector" in url_after or "onboarding" in url_after:
            log.info("  ✅ [LOGOUT-RELOGIN] Credential login succeeded!")
            return True

        # Handle merchant-selector page if redirected there post-login
        for _ in range(10):
            url_after = driver.current_url.lower()
            if "dashboard" in url_after or "merchant-selector" in url_after or "onboarding" in url_after:
                log.info("  ✅ [LOGOUT-RELOGIN] Logged in and on portal.")
                return True
            time.sleep(1)

        log.warning(f"  ⚠️ [LOGOUT-RELOGIN] Unexpected URL after credential login: {driver.current_url}")
        return False

    except Exception as e:
        log.error(f"  ❌ [LOGOUT-RELOGIN] Failed: {e}")
        return False

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
    session_valid = False
    if saved:
        log.info(f"🔍 [SESSION] Validating saved session for '{account_name}' via API...")
        if validate_session(saved["shopee_tob_token"], saved["shopee_tob_entity_id"]):
            session_valid = True

    run_headless_now = headless
    login_needed = not session_valid

    # 2. If cached session is missing or invalid, run Selenium login with retries
    for attempt in range(3):
        # We start with the configured headless mode to check if browser is already logged in
        if attempt > 0:
            # If attempt > 0, it means the first check failed and login/OTP is required, so we force headed
            run_headless_now = False

        log.info(f"🌐 [BROWSER] Launching isolated browser for '{account_name}' (headless={run_headless_now}, attempt={attempt+1}/3)...")
        driver = _init_driver(headless=run_headless_now, account_name=account_name)
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
                # If we are currently headless, but need login/OTP, relaunch headed!
                if run_headless_now:
                    log.info(f"⚠️ [SESSION] Sesi untuk '{account_name}' tidak aktif. Membuka browser dengan antarmuka (headed) untuk login & OTP...")
                    driver.quit()
                    run_headless_now = False
                    driver = _init_driver(headless=False, account_name=account_name)
                    wait = WebDriverWait(driver, 30)
                    driver.get(PARTNER_DASHBOARD)
                    time.sleep(4)

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
            
            # --- TRANSITION HEADED -> HEADLESS AFTER LOGIN ---
            if login_needed and headless and not run_headless_now:
                log.info(f"✅ [SESSION] Login berhasil untuk '{account_name}'. Menyimpan sesi dan beralih ke mode headless...")
                t, eid = _trigger_and_extract_tokens(driver)
                if not t:
                    log.warning(f"⚠️ [SESSION] Token extraction failed before transitioning to headless.")
                    driver.quit()
                    continue
                
                all_c = get_all_cookies_dict(driver)
                save_session(account_name, t, eid or "", extra_cookies=all_c)
                
                # Close headed browser
                driver.quit()
                driver = None
                
                # Launch headless browser
                log.info(f"🌐 [SESSION] Membuka kembali browser dalam mode HEADLESS...")
                driver = _init_driver(headless=True, account_name=account_name)
                wait = WebDriverWait(driver, 30)
                
                # Load cookies
                driver.get("https://partner.shopee.co.id/")
                time.sleep(2)
                for name, value in all_c.items():
                    try:
                        driver.add_cookie({"name": name, "value": value})
                    except:
                        pass
                driver.get(PARTNER_DASHBOARD)
                time.sleep(4)
                
                # Reset flags so subsequent steps run in headless
                login_needed = False
                run_headless_now = True

            # Ensure we are on dashboard or settings to trigger tokens
            if "/food/dashboard" not in driver.current_url:
                driver.get(PARTNER_DASHBOARD)
                time.sleep(2)

            # --- DETECT UNKNOWN MERCHANT / STUCK DASHBOARD ---
            active_id = None
            active_name = "Unknown Merchant"
            try:
                api_js = '''
                var done = arguments[arguments.length - 1];
                let token = document.cookie.split('; ').find(row => row.startsWith('shopee_tob_token='))?.split('=')[1];
                fetch('https://api.partner.shopee.co.id/nb/mss/web-api/PartnerAccountServer/GetUserInfo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'x-merchant-token': token || '' },
                    credentials: 'include'
                })
                .then(r => r.json())
                .then(j => done(j.data || null))
                .catch(() => done(null));
                '''
                driver.set_script_timeout(10)
                user_data = driver.execute_async_script(api_js)
                if user_data:
                    active_id = str(user_data.get("merchantId") or "")
                    active_name = user_data.get("merchantName") or "Unknown Merchant"
            except: pass
            
            if active_name == "Unknown Merchant":
                log.info(f"🔄 [SESSION] Unknown merchant detected for '{account_name}' — initiating logout/relogin recovery...")
                recovered = _deliberate_logout_and_relogin(
                    driver,
                    username=username,
                    password=password,
                    phone=phone,
                )
                if not recovered:
                    log.warning(f"⚠️ [SESSION] Recovery failed for '{account_name}'. Will attempt token extraction anyway.")
                else:
                    log.info(f"✅ [SESSION] Successfully recovered clean session for '{account_name}'.")

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

