"""
VB/core/browser.py
==================
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

import shutil
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from core.logger import get_logger

log = get_logger("browser_vb")

# ── Thread-local Session File Configuration ─────────────────────────────────────
import sys
import threading
sys.path.append(str(Path(__file__).resolve().parent.parent.parent / "src"))
from discord_notifier import send_discord_error

_thread_local = threading.local()

def get_session_file_path(account_name: str) -> Path:
    """Returns the dedicated session file path for a given account name."""
    session_dir = Path(__file__).resolve().parent.parent / "shopee" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / f"session_{account_name}.json"

def get_session_file() -> Path:
    if not hasattr(_thread_local, "session_file"):
        _thread_local.session_file = Path(__file__).resolve().parent.parent / "shopee" / "sessions" / "session_default.json"
    return _thread_local.session_file

def set_session_file(val):
    _thread_local.session_file = Path(val)

class ThreadLocalSessionFileProxy:
    def __getattr__(self, name):
        return getattr(get_session_file(), name)
        
    def __str__(self):
        return str(get_session_file())
        
    def __fspath__(self):
        return str(get_session_file())

    def __eq__(self, other):
        return get_session_file() == other

SESSION_FILE = ThreadLocalSessionFileProxy()

# Wrap the module class to intercept external writes to SESSION_FILE
class ModuleWrapper(sys.modules[__name__].__class__):
    @property
    def SESSION_FILE(self):
        return get_session_file()
        
    @SESSION_FILE.setter
    def SESSION_FILE(self, value):
        set_session_file(value)

sys.modules[__name__].__class__ = ModuleWrapper

# ── Constants ──────────────────────────────────────────────────────────────────
PARTNER_DASHBOARD    = "https://partner.shopee.co.id/food/dashboard"
TOKEN_TRIGGER_PAGE   = "https://partner.shopee.co.id/settings/shopee-food/business-hours-settings"
MERCHANT_SELECTOR_URL = "https://partner.shopee.co.id/food/dashboard"
VALIDATE_URL         = "https://api.partner.shopee.co.id/nb/mss/web-api/PartnerAccountServer/GetUserInfo"
SHOPEE_IMG_BASE      = "https://down-id.img.susercontent.com/file"

# Words that must NEVER be clicked — guard against accidental logout
LOGOUT_KEYWORDS = ["log out", "logout", "keluar", "sign out", "signout"]

# ── Helpers ────────────────────────────────────────────────────────────────────

def human_like_typing(element, text: str):
    element.send_keys(text)

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
    current = driver.current_url.lower()
    logged_out = (
        "/login" in current
        or "/authenticate/login" in current
        or "about:blank" in current
    )
    if not logged_out:
        return False

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

def _handle_onboarding_invitation(driver, timeout=15) -> bool:
    try:
        current_url = driver.current_url.lower()
        if "onboarding" not in current_url:
            return False

        page_info = driver.execute_script("""
            var allButtons = Array.from(document.querySelectorAll('button'));
            var gabungBtn = null;
            for (var btn of allButtons) {
                var text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                if (text.includes('gabung')) { gabungBtn = btn; break; }
            }
            var hasListItems = document.querySelectorAll(
                '.listItem, .merchant-item, li[class*="item"]'
            ).length > 0;
            return { hasGabung: !!gabungBtn, hasList: hasListItems };
        """)

        if not page_info or not page_info.get("hasGabung") or page_info.get("hasList"):
            return False

        log.info("📍 [ONBOARDING] Merchant invitation page detected. Clicking 'Gabung dengan Merchant'...")

        btn_xpath = "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung')]"
        gabung_btn = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, btn_xpath))
        )
        gabung_btn.click()
        log.info("  👉 Clicked 'Gabung dengan Merchant' button.")
        time.sleep(3)

        for _ in range(20):
            new_url = driver.current_url.lower()
            if "/food/dashboard" in new_url:
                log.info("  ✅ [ONBOARDING] Invitation accepted → Dashboard loaded.")
                return True
            if new_url != current_url:
                log.info(f"  ✅ [ONBOARDING] Invitation accepted → Redirected to: {driver.current_url}")
                return True
            time.sleep(1)

        log.warning("  ⚠️ [ONBOARDING] Gabung clicked but no redirect detected within 20s.")
        return True

    except Exception as e:
        log.warning(f"  ⚠️ [ONBOARDING] Failed to handle invitation page: {e}")
        return False

def _deliberate_logout_and_relogin(
    driver,
    username: str = None,
    password: str = None,
    phone:    str = None,
) -> bool:
    log.info("🔄 [LOGOUT-RELOGIN] Initiating deliberate logout for clean session recovery...")
    try:
        url_now = driver.current_url.lower()
        if "login" in url_now or "authenticate" in url_now:
            log.info("  🛡️ Browser is already on the login/authenticate page. Skipping UI dropdown logout.")
            log.info("  🌐 Attempting direct login preserving all cookies/storage...")
            if not (username and password) and not phone:
                log.warning("  ⚠️ No credentials provided — cannot complete login.")
                return False
            wait = WebDriverWait(driver, 30)
            login_ok = _perform_login(driver, wait, username=username, password=password, phone=phone)
            if login_ok:
                log.info("  ⏳ Menunggu pengalihan halaman setelah login recovery...")
                redirected_ok = False
                for _ in range(30):
                    curr_url = driver.current_url.lower()
                    if "onboarding" in curr_url or "merchant-selector" in curr_url or "dashboard" in curr_url:
                        redirected_ok = True
                        break
                    time.sleep(0.5)
                if redirected_ok:
                    log.info("  ✅ [LOGOUT-RELOGIN] Credential login succeeded directly from login page!")
                    return True
            return False

        if "/food/" not in driver.current_url and "/settings/" not in driver.current_url:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(3)

        profile_clicked = False
        for attempt in range(3):
            driver.execute_script("""
                document.querySelectorAll('.ant-notification, .ant-modal, .ant-notification-notice, .ant-message').forEach(el => el.remove());
            """)
            
            profile_el = driver.execute_script("""
                var profileEl = null;
                for (var sel of ['.merchantName', '.user-info', '.ant-dropdown-trigger', '.ant-dropdown-link']) {
                    var el = document.querySelector(sel);
                    if (el && el.offsetHeight > 0) {
                        profileEl = el;
                        break;
                    }
                }
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
                driver.execute_script("""
                    var el = arguments[0];
                    var ev1 = new MouseEvent('mouseover', { bubbles: true, cancelable: true });
                    var ev2 = new MouseEvent('mouseenter', { bubbles: true, cancelable: true });
                    var ev3 = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
                    var ev4 = new MouseEvent('click', { bubbles: true, cancelable: true });
                    var ev5 = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
                    el.dispatchEvent(ev1); el.dispatchEvent(ev2); el.dispatchEvent(ev3); el.dispatchEvent(ev4); el.dispatchEvent(ev5);
                """, profile_el)
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
                    var clickable = el.closest('li, button, a, [role="menuitem"], .ant-dropdown-menu-item') || el;
                    return clickable;
                }
            }
            return null;
        """)

        if not logout_el:
            log.warning("  ⚠️ 'Log Out' menu item not found in dropdown.")
            return False

        try:
            log.info("  👈 Clicking 'Log Out' menu item...")
            logout_el.click()
        except Exception:
            try:
                ActionChains(driver).move_to_element(logout_el).click().perform()
            except Exception as e:
                log.warning(f"  ⚠️ Selenium click failed: {e}. Trying JS click...")
                driver.execute_script("""
                    var el = arguments[0];
                    var ev1 = new MouseEvent('mouseover', { bubbles: true, cancelable: true });
                    var ev2 = new MouseEvent('mouseenter', { bubbles: true, cancelable: true });
                    var ev3 = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
                    var ev4 = new MouseEvent('click', { bubbles: true, cancelable: true });
                    var ev5 = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
                    el.dispatchEvent(ev1); el.dispatchEvent(ev2); el.dispatchEvent(ev3); el.dispatchEvent(ev4); el.dispatchEvent(ev5);
                """, logout_el)
        
        time.sleep(1.5)

        confirm_clicked = False
        for confirm_attempt in range(5):
            confirm_el = driver.execute_script("""
                var targets = ['log out', 'logout', 'keluar'];
                var modal = document.querySelector('.ant-modal-content, .ant-modal, .ant-dialog, .ant-modal-wrap');
                if (!modal) return null;
                
                var candidates = Array.from(modal.querySelectorAll('button, .ant-btn, [role="button"]'));
                for (var btn of candidates) {
                    var rect = btn.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    var text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    if (targets.some(function(k){ return text === k || text === ('confirm ' + k); })) {
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
            try:
                debug_dir = Path(__file__).resolve().parent.parent / "data" / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                ss_fail_path = debug_dir / "modal_fail_server.png"
                driver.save_screenshot(str(ss_fail_path))
                log.info(f"  📸 [DEBUG] Screenshot penyebab kegagalan klik disimpan di {ss_fail_path}")
            except Exception:
                pass
            
            log.info("  🛡️ Mengaktifkan 'Soft Session Kill' Fallback (Hanya hapus Cookie Sesi)...")
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except: pass
                
            try:
                auth_cookies = ['SPC_ST', 'SPC_U', 'SPC_T_ID', 'SPC_T_IV']
                for cookie_name in auth_cookies:
                    try: driver.delete_cookie(cookie_name)
                    except: pass
                driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                log.info("  ✅ Soft Session Kill dieksekusi. Sesi dibersihkan tanpa menghapus Device Fingerprint.")
                driver.refresh()
                time.sleep(3)
            except Exception as e:
                log.warning(f"  ⚠️ Soft Session Kill gagal: {e}")
                return False

        log.info("  ✅ Logout confirmed. Waiting for login page...")
        time.sleep(3)

        log.info("  🌐 Attempting Chrome profile auto-login...")
        driver.get(PARTNER_DASHBOARD)
        time.sleep(5)
        url_now = driver.current_url.lower()
        if "dashboard" in url_now or "merchant-selector" in url_now or "onboarding" in url_now:
            log.info("  ✅ [LOGOUT-RELOGIN] Auto-login via Chrome profile succeeded!")
            return True

        log.info("  ⚠️ Chrome profile auto-login failed — logging in with credentials...")
        if not (username and password) and not phone:
            log.warning("  ⚠️ No credentials provided — cannot complete login.")
            return False

        current = driver.current_url.lower()
        if "login" not in current and "authenticate" not in current:
            driver.get("https://partner.shopee.co.id/login")
            time.sleep(4)

        wait = WebDriverWait(driver, 30)
        login_ok = _perform_login(driver, wait, username=username, password=password, phone=phone)
        if not login_ok:
            log.error("  ❌ Credential login failed.")
            return False

        time.sleep(3)
        url_after = driver.current_url.lower()
        if "dashboard" in url_after or "merchant-selector" in url_after or "onboarding" in url_after:
            log.info("  ✅ [LOGOUT-RELOGIN] Credential login succeeded!")
            return True

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

def build_img_url(img_id: str) -> str:
    if not img_id: return ""
    return f"{SHOPEE_IMG_BASE}/{img_id}"

# ── Session Persistence ────────────────────────────────────────────────────────

def save_session(tob_token: str, entity_id: str, extra_cookies: dict = None):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "shopee_tob_token": tob_token,
        "shopee_tob_entity_id": entity_id,
        "saved_at": datetime.now().isoformat(),
        "extra_cookies": extra_cookies or {},
    }
    SESSION_FILE.write_text(json.dumps(payload, indent=2))
    log.debug(f"✅ Session saved to {SESSION_FILE}")

def load_session(account_name: str = None) -> dict | None:
    if account_name:
        set_session_file(get_session_file_path(account_name))
    if not SESSION_FILE.exists(): return None
    try:
        data = json.loads(SESSION_FILE.read_text())
        if data.get("shopee_tob_token"):
            log.info(f"📂 [SESSION] Found cached session (saved at {data.get('saved_at')})")
            return data
    except: pass
    return None

def validate_session(tob_token: str, entity_id: str) -> bool:
    log.debug("🔍 Validating saved session token...")
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
        if data.get("message") == "success" or data.get("code") == 0:
            log.info("✅ [SESSION] Saved session is still valid.")
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

def _kill_zombie_chrome_processes(profile_dir: Path):
    """Kills any running Chrome/ChromeDriver processes using the specified profile directory."""
    if os.name == "nt":
        try:
            import subprocess
            abs_path = str(profile_dir.resolve())
            ps_cmd = f"Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe' OR Name = 'chromedriver.exe'\" | Where-Object {{ $_.CommandLine -like '*{abs_path}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.warning(f"⚠️ Failed to kill zombie chrome processes: {e}")
    else:
        try:
            import subprocess
            abs_path = str(profile_dir.resolve())
            cmd = f"pkill -9 -f '{abs_path}'"
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

# ── Driver Initialization ──────────────────────────────────────────────────────

def _clean_chrome_profile_locks(account_name: str):
    """
    Removes stale Chrome lock files from the profile directory.
    Chrome leaves behind 'SingletonLock', 'SingletonSocket', and 'SingletonCookie'
    when it crashes — these prevent new instances from starting on the same profile.
    """
    import glob
    script_dir = Path(__file__).parent.parent
    profile_dir = script_dir / "data" / "chrome_profiles" / account_name
    stale_locks = ["SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"]
    cleaned = []
    for lock_name in stale_locks:
        lock_path = profile_dir / lock_name
        if lock_path.exists() or lock_path.is_symlink():
            try:
                lock_path.unlink()
                cleaned.append(lock_name)
            except Exception as ex:
                log.debug(f"  ⚠️ Could not remove lock '{lock_name}': {ex}")
    # Also remove per-profile SingletonLock inside the profile subdirectory
    for sub_lock in glob.glob(str(profile_dir / f"profile_{account_name}" / "SingletonLock")):
        try:
            Path(sub_lock).unlink()
            cleaned.append(f"profile/{Path(sub_lock).name}")
        except Exception:
            pass
    if cleaned:
        log.info(f"🧹 [BROWSER] Cleaned stale Chrome lock(s) for '{account_name}': {', '.join(cleaned)}")


def _nuke_chrome_profile(account_name: str):
    """
    Last-resort recovery: completely wipes the Chrome profile directory for an account.
    Used when lock-file cleanup alone cannot fix a corrupted profile that prevents
    the renderer from connecting (SessionNotCreatedException). The next launch will
    get a brand-new, clean profile and trigger a fresh browser login.
    """
    import shutil as _shutil
    script_dir = Path(__file__).parent.parent
    profile_dir = script_dir / "data" / "chrome_profiles" / account_name
    if not profile_dir.exists():
        return
    try:
        _shutil.rmtree(profile_dir)
        log.warning(f"💣 [BROWSER] Nuked corrupt Chrome profile for '{account_name}' at: {profile_dir}")
    except Exception as ex:
        log.error(f"❌ [BROWSER] Could not wipe Chrome profile for '{account_name}': {ex}")


def _init_driver(headless: bool, account_name: str = None):
    options = Options()
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # Renderer stability flags — prevent renderer from crashing on profile re-use
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--renderer-process-limit=1")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    
    script_dir = Path(__file__).parent.parent
    profile_dir = script_dir / "data" / "chrome_profiles" / account_name
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    options.add_argument(f"--profile-directory=profile_{account_name}")

    # Terminate leftover chrome processes that lock the profile
    _kill_zombie_chrome_processes(profile_dir)

    singleton_lock = profile_dir / "SingletonLock"
    if singleton_lock.exists() or singleton_lock.is_symlink():
        try:
            singleton_lock.unlink(missing_ok=True)
            log.info(f"🧹 Removed Chrome SingletonLock at {singleton_lock}")
        except Exception as e:
            log.warning(f"⚠️ Failed to remove SingletonLock: {e}")

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        log.warning(f"⚠️ Native Chrome init failed: {e}. Trying ChromeDriverManager fallback...")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.set_page_load_timeout(60)
    return driver

# ── OTP Retrieval Helper ──────────────────────────────────────────────────────

def get_otp_code(username: str, phone: str) -> str:
    # 1. Try Google Sheet OTP polling first (VB-specific context)
    try:
        from core.otp import get_latest_otp
        log.info("📡 Checking Google Sheet for OTP...")
        otp_code = get_latest_otp()
        if otp_code:
            log.info(f"✅ Found OTP from Google Sheet: {otp_code}")
            return otp_code
    except Exception as e:
        log.warning(f"⚠️ Failed to get OTP from Google Sheet: {e}")

    # 2. Check Discord mode
    discord_mode = os.getenv("OFD_DISCORD_MODE") == "1"
    if not discord_mode:
        if not sys.stdin.isatty():
            log.warning("⚠️ [OTP] Stdin is not a TTY (running in background/Docker). Cannot prompt for OTP via terminal. Waiting for Google Sheet OTP...")
            try:
                from core.otp import wait_for_otp
                otp_code = wait_for_otp(max_wait_seconds=60)
                if otp_code: return otp_code
            except: pass
            return ""
        try:
            return input(f"🔑 Masukkan 6-digit OTP (atau tekan Enter jika Anda mengisinya langsung di browser): ").strip()
        except EOFError:
            log.warning("⚠️ [OTP] Stdin reached EOF. Waiting 10 seconds...")
            time.sleep(10)
            return ""
    
    script_dir = Path(__file__).resolve().parent.parent
    data_dir = script_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    otp_file = data_dir / f"otp_request_{username}.json"
    
    request_data = {
        "status": "WAITING_OTP",
        "username": username,
        "phone": phone,
        "requested_at": datetime.now().isoformat()
    }
    
    try:
        otp_file.write_text(json.dumps(request_data, indent=2))
        print(f"DISCORD_OTP_REQUEST: {json.dumps(request_data)}", flush=True)
        log.info(f"Sent OTP request to Discord for: {username}")
    except Exception as e:
        log.error(f"Gagal menulis file request OTP: {e}")
        return ""
    
    log.info(f"⏳ [DISCORD] Menunggu input OTP dari Discord untuk akun {username}...")
    
    start_wait = time.time()
    while time.time() - start_wait < 86400:
        if otp_file.exists():
            try:
                data = json.loads(otp_file.read_text())
                if data.get("status") == "RECEIVED" and data.get("code"):
                    otp_code = str(data["code"]).strip()
                    log.info(f"✅ [DISCORD] OTP diterima dari Discord: {otp_code}")
                    otp_file.unlink(missing_ok=True)
                    return otp_code
            except Exception as e:
                log.error(f"Error membaca file OTP: {e}")
        time.sleep(2)
        
    log.warning(f"❌ [DISCORD] Timeout waiting for OTP for {username}")
    otp_file.unlink(missing_ok=True)
    return ""

# ── Login Logic ────────────────────────────────────────────────────────────────

def _perform_login(driver, wait, username: str = None, password: str = None, phone: str = None, is_retry: bool = False) -> bool:
    log.info("➡️  [AUTH] Starting login sequence...")
    if not phone and (not username or not password):
        raise Exception("Shopee credentials are not configured! Please check your configuration.")
    
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
        # Try specific CSS selectors first
        for sel in ["input[name='userName']", "input[placeholder*='handphone']", "input[placeholder*='Username']"]:
            try:
                el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
                if el.is_displayed():
                    user_input = el
                    break
            except:
                continue

        # If specific selectors fail, fall back to scanning all inputs
        if not user_input:
            try:
                inputs = driver.find_elements(By.CSS_SELECTOR, "input")
                for inp in inputs:
                    p = (inp.get_attribute("placeholder") or "").lower()
                    n = (inp.get_attribute("name") or "").lower()
                    t = (inp.get_attribute("type") or "").lower()
                    if inp.is_displayed() and ("user" in n or "phone" in n or "handphone" in p or "username" in p):
                        user_input = inp
                        break
            except:
                pass

        # Ultimate fallback to any text input
        if not user_input:
            try:
                el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
                if el.is_displayed():
                    user_input = el
            except:
                pass
        
        if not user_input:
            log.error(f"❌ Failed to find Username field. URL: {driver.current_url}")
            try:
                all_inps = driver.find_elements(By.TAG_NAME, "input")
                log.debug(f"  Found {len(all_inps)} input tags on page.")
                for i, el in enumerate(all_inps):
                    log.debug(f"    [{i}] name={el.get_attribute('name')} type={el.get_attribute('type')} placeholder={el.get_attribute('placeholder')} visible={el.is_displayed()}")
            except: pass
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

    time.sleep(3)
    try:
        error_texts = driver.execute_script("""
            var errs = Array.from(document.querySelectorAll('.shopee-form-item__error-message, .shopee-alert__title, .ant-message-custom-content span'));
            return errs.map(e => e.innerText).filter(t => t.length > 0);
        """)
        for err_text in error_texts:
            if "sandi" in err_text.lower() or "password" in err_text.lower() or "salah" in err_text.lower() or "nomor" in err_text.lower() or "username" in err_text.lower():
                log.error(f"❌ Login error detected: {err_text}")
                if is_retry:
                    send_discord_error("Shopee", username or phone, "WRONG_CREDENTIALS", f"Gagal login: {err_text}", phone)
                return False
            if "blokir" in err_text.lower() or "blocked" in err_text.lower() or "dibatasi" in err_text.lower():
                log.error(f"❌ Account block detected: {err_text}")
                if is_retry:
                    send_discord_error("Shopee", username or phone, "BLOCKED_ACCOUNT", f"Akun dibatasi/diblokir: {err_text}", phone)
                return False
    except: pass

    log.debug("  ⏳ Waiting for post-login redirect or OTP...")
    start_wait = time.time()
    while time.time() - start_wait < 300:
        current_url = driver.current_url.lower()
        if "onboarding" in current_url or "merchant-selector" in current_url or "dashboard" in current_url:
            break
            
        try:
            otp_input = None
            for sel in ["input.shopee-otp-input__input", ".shopee-otp-input input", "input[maxlength='6']"]:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed(): otp_input = el; break
                if otp_input: break

            is_verification_page = driver.execute_script("""
                var texts = [
                    "pilih cara verifikasi", "select verification method",
                    "pilih metode verifikasi", "verify to log in",
                    "verifikasi untuk masuk", "masukkan kode", "enter code",
                    "kode verifikasi", "verification code"
                ];
                var bodyText = (document.body.innerText || "").toLowerCase();
                return texts.some(function(t) { return bodyText.includes(t); });
            """)

            if otp_input or is_verification_page:
                log.warning(f"⚠️ [OTP REQUIRED] Akun '{username or phone}' memerlukan kode verifikasi OTP.")
                otp_code = get_otp_code(username, phone)
                if otp_code:
                    log.info(f"⌨️  [AUTH] Menginput OTP: {otp_code}")
                    try:
                        if otp_input:
                            otp_input.click()
                            otp_input.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
                            human_like_typing(otp_input, otp_code)
                            time.sleep(0.5)
                            otp_input.send_keys(Keys.ENTER)
                        else:
                            inputs = driver.find_elements(By.CSS_SELECTOR, "input")
                            for inp in inputs:
                                if inp.is_displayed():
                                    inp.send_keys(Keys.CONTROL + "a", Keys.BACKSPACE)
                                    human_like_typing(inp, otp_code)
                                    inp.send_keys(Keys.ENTER)
                                    break
                    except Exception as err:
                        log.warning(f"⚠️ Gagal memasukkan OTP ke elemen browser: {err}")
                    time.sleep(5)
                else:
                    log.info("ℹ️ Menunggu 10 detik untuk input langsung di browser...")
                    time.sleep(10)
        except Exception:
            pass

        try:
            btn_el = driver.find_element(By.XPATH, "//button[contains(., 'Lanjutkan') or contains(., 'Continue')] | //*[text()='Lanjutkan' or text()='Continue']")
            if btn_el.is_displayed():
                log.info("👉 [AUTH] Menemukan tombol 'Lanjutkan', mencoba mengklik...")
                try: btn_el.click()
                except: driver.execute_script("arguments[0].click();", btn_el)
                time.sleep(2)
        except Exception:
            pass

        time.sleep(2)

    current_url = driver.current_url.lower()
    if "onboarding" not in current_url and "merchant-selector" not in current_url and "dashboard" not in current_url:
        log.error(f"❌ [AUTH] Login did not redirect to dashboard and is still on: {current_url}. Aborting.")
        return False

    return True

# ── Merchant Switching ──────────────────────────────────────────────────────────

def auto_switch_merchant(driver, target_name, is_retry=False):
    log.info(f"🔄 [MERCHANT] Switching to: {target_name}...")
    try:
        driver.execute_script("document.querySelectorAll('.ant-spin, [class*=\"loading\"], .shopee-loading').forEach(el => el.remove());")
        wait = WebDriverWait(driver, 15)

        js_selector_click = """
            var listItems = document.querySelectorAll('.listItem, .merchant-item, li[class*="item"]');
            for (var i = 0; i < listItems.length; i++) {
                var el = listItems[i];
                var text = (el.innerText || el.textContent || "").trim();
                if (text.length > 0) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }
            }
            return false;
        """

        current_url = driver.current_url
        if "onboarding" in current_url or "merchant-selector" in current_url:
            log.debug(f"  📍 Detected Merchant Selector page (URL: {current_url}). Attempting to bypass...")
            time.sleep(3)
            
            for attempt in range(5):
                if driver.execute_script(js_selector_click):
                    log.debug(f"  ✅ Triggered selection on selector page. Waiting for dashboard or invitation...")
                    try:
                        pre_click_url = driver.current_url
                        
                        def _page_transitioned(d):
                            cur = d.current_url
                            if "/food/dashboard" in cur:
                                return True
                            if cur != pre_click_url:
                                return True
                            try:
                                btns = d.find_elements(By.XPATH, "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung')]")
                                if any(b.is_displayed() for b in btns): return True
                            except: pass
                            return False
                        
                        WebDriverWait(driver, 30).until(_page_transitioned)
                        time.sleep(3)
                        
                        if "/food/dashboard" not in driver.current_url:
                            if _handle_onboarding_invitation(driver):
                                time.sleep(3)
                        
                        if "/food/dashboard" in driver.current_url:
                            try:
                                actual_name = driver.find_element(By.CSS_SELECTOR, ".merchantName").text.strip().lower()
                                if target_name.lower() in actual_name:
                                    return True
                                else:
                                    log.info(f"  📍 Landed on dashboard as '{actual_name}'. Will switch to target now.")
                                    break 
                            except:
                                break
                    except: pass
                driver.execute_script("window.scrollBy(0, 300);")
                time.sleep(1)
            
            if "onboarding" in driver.current_url or "merchant-selector" in driver.current_url:
                raise Exception(f"Failed to bypass Merchant Selector page")

        if "/food/dashboard" not in driver.current_url:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(2)
        
        for switch_attempt in range(3):
            dropdown_opened = False
            try:
                actions = ActionChains(driver)
                profile_menu = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".merchantName")))
                actions.move_to_element(profile_menu).click().perform()
                time.sleep(1)
                
                quick_wait = WebDriverWait(driver, 3)
                try:
                    switch_trigger = quick_wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(text(), 'Pilih Merchant Lain') or contains(text(), 'Switch Merchant')]")))
                    actions.move_to_element(switch_trigger).click().perform()
                    dropdown_opened = True
                    time.sleep(1)
                except:
                    js_found = driver.execute_script("""
                        var spans = document.querySelectorAll('span, p, div');
                        for (var s of spans) {
                            var text = (s.innerText || '').trim();
                            if (text.includes('Pilih Merchant Lain') || text.includes('Switch Merchant')) {
                                s.click();
                                return true;
                            }
                        }
                        return false;
                    """)
                    if js_found:
                        dropdown_opened = True
                        time.sleep(1)
            except Exception as e:
                err_str = str(e)
                log.warning(f"  ⚠️ Failed to trigger merchant menu: {err_str}")
                if "TimeoutException" in err_str or "merchantName" not in driver.page_source:
                    log.warning("  ⚠️ [STALE SESSION] Elemen profil (.merchantName) tidak ditemukan. Sesi kemungkinan kedaluwarsa.")
                    return False
                    
                if switch_attempt == 2:
                    return False
                continue

            if not dropdown_opened:
                log.warning(f"  ⚠️ [STALE SESSION] Dropdown profil tidak terbuka setelah klik — sesi kemungkinan kedaluwarsa.")
                return False

            js_switch_script = """
                var targetName = arguments[0].toLowerCase().trim();
                var items = document.querySelectorAll('li.ant-menu-item, li[role="menuitem"], .ant-dropdown-menu-item, [class*="menu-item"]');
                for (var i = 0; i < items.length; i++) {
                    var text = (items[i].innerText || "").toLowerCase().trim();
                    if (text === targetName || text.includes(targetName)) {
                        items[i].scrollIntoView({block: 'center'});
                        items[i].click();
                        return true;
                    }
                }
                return false;
            """
            
            found_target = False
            for _ in range(5):
                if driver.execute_script(js_switch_script, target_name):
                    found_target = True
                    break
                try:
                    driver.execute_script("document.querySelectorAll('.ant-dropdown-menu, ul[role=\"menu\"], .ant-popover-inner-content').forEach(el => el.scrollTop += 600);")
                except: pass
                time.sleep(1)
                
            if found_target:
                log.debug(f"  ✅ Clicked {target_name} in menu.")
            else:
                log.warning(f"  ⚠️ Nama outlet '{target_name}' tidak ditemukan di dropdown (Attempt {switch_attempt+1}/3).")
                if switch_attempt == 2:
                    msg = f"Nama outlet '{target_name}' tidak terdaftar atau belum ditambahkan (invite) di akun Shopee ini."
                    log.error(f"❌ {msg}")
                    send_discord_error(
                        platform="Shopee", 
                        merchant=target_name, 
                        error_type="SYSTEM_ERROR", 
                        message=msg
                    )
                    raise ValueError(f"MERCHANT_NOT_FOUND: {target_name}")
                continue

            time.sleep(3)
            current_url = driver.current_url.lower()
            if "onboarding" in current_url:
                log.info("📍 [MERCHANT] Onboarding page detected after selecting merchant. Accepting invitation...")
                if _handle_onboarding_invitation(driver):
                    log.info("  ✅ Invitation accepted via helper.")
                    time.sleep(3)
                    if "/food/dashboard" not in driver.current_url:
                        try: WebDriverWait(driver, 15).until(lambda d: "/food/dashboard" in d.current_url)
                        except: pass
                else:
                    log.error("❌ Failed to accept onboarding invitation.")
                    if switch_attempt == 2: return False
                    continue

            try:
                log.info(f"  ⏳ Menunggu 5 detik melihat pembaruan nama menjadi {target_name} (Attempt {switch_attempt+1}/3)...")
                def is_name_updated(d):
                    try: return target_name.lower() in d.find_element(By.CSS_SELECTOR, ".merchantName").text.lower()
                    except: return False
                        
                WebDriverWait(driver, 5).until(is_name_updated)
                log.info(f"✅ [MERCHANT] Switched to: {target_name}")
                return True
            except:
                log.warning(f"⚠️ [MERCHANT] UI name belum berubah ke {target_name}.")
                if switch_attempt == 2:
                    log.warning(f"❌ [MERCHANT] Gagal melakukan switch ke {target_name} setelah 3x percobaan klik.")
                    if is_retry:
                        send_discord_error(
                            platform="Shopee", 
                            merchant=target_name, 
                            error_type="SYSTEM_ERROR", 
                            message=f"Dashboard tidak memuat profil outlet '{target_name}' meskipun sudah 3x dipilih di menu."
                        )
                    return False
    except Exception as e:
        if "MERCHANT_NOT_FOUND" in str(e): raise e
        log.error(f"❌ Auto-switch failed: {e}")
        return False

def _handle_merchant_selection(driver, active_id_forced=None, interactive=True):
    log.info("===========================================================================")
    try:
        active_id = active_id_forced
        if not active_id:
            _, active_id = extract_tokens_from_driver(driver)
            
        if active_id:
            log.info(f"📍 [MERCHANT] Active ID: {active_id}")
        
        all_found = {}
        all_merchants_data = {}
        try:
            api_response_path = Path(__file__).resolve().parent.parent / "API" / "response.json"
            if not api_response_path.exists():
                api_response_path = Path(__file__).resolve().parent.parent.parent / "src" / "shopee-omzet-automation" / "API" / "response.json"
            if api_response_path.exists():
                with open(api_response_path, "r") as f:
                    data = json.load(f)
                    for m in data.get("data", {}).get("selectMerchant", {}).get("merchantList", []):
                        all_merchants_data[m["merchantName"].lower()] = str(m["merchantId"])
        except: pass

        for attempt in range(10):
            log.debug(f"  📥 Scanning for merchants (Attempt {attempt+1}/10)...")
            scan_result = driver.execute_script("""
                var results = [];
                var items = document.querySelectorAll('.listItem, .merchant-item, li[class*="item"], li, [class*="merchant"], [class*="shop"]');
                for (var i = 0; i < items.length; i++) {
                    var el = items[i];
                    if (el.children.length > 3) continue;
                    var text = (el.innerText || "").trim().split('\\n')[0];
                    if (!text || text.length < 3 || text.length > 50) continue;
                    
                    var name_key = text.toLowerCase();
                    var generic = [
                        "akun", "pengaturan", "log out", "logout", "keluar", "halaman utama", "baru", "menu", "outlet", 
                        "shopeefood", "terapkan", "sembunyikan", "notifikasi", "pilih merchant lain", 
                        "pusat bantuan", "transaksi berhasil", "baris per halaman", "ringkasan toko", 
                        "nama toko", "jumlah total", "laporan saya", "penghasilan", "performa outlet", 
                        "periode transaksi", "ubah bahasa", "daftar merchant", "daftar di sini", 
                        "memulai bisnis baru?", "pilih merchant", "gabung dengan merchant", 
                        "buat merchant baru", "hubungi kami", "faq", "syarat & ketentuan",
                        "pusat edukasi seller"
                    ];
                    if (generic.some(g => name_key === g || name_key.includes(g))) continue;

                    let rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        results.push({ name: text, index: i });
                    }
                }
                return results;
            """)

            if scan_result:
                all_els = driver.find_elements(By.CSS_SELECTOR, '.listItem, .merchant-item, li[class*="item"], li, [class*="merchant"], [class*="shop"]')
                for r in scan_result:
                    name = r['name']
                    name_key = name.lower()
                    m_id = all_merchants_data.get(name_key) or "Unknown"
                    
                    if all_merchants_data and m_id == "Unknown":
                        continue
                        
                    generic_texts = [
                        "akun", "pengaturan", "log out", "logout", "keluar", "halaman utama", "baru", "menu", "outlet", 
                        "shopeefood", "terapkan", "sembunyikan", "notifikasi", "pilih merchant lain", 
                        "pusat bantuan", "transaksi berhasil", "baris per halaman", "ringkasan toko", 
                        "nama toko", "jumlah total", "laporan saya", "penghasilan", "performa outlet", 
                        "periode transaksi", "ubah bahasa", "daftar merchant", "daftar di sini", 
                        "memulai bisnis baru?", "pilih merchant", "gabung dengan merchant", 
                        "buat merchant baru", "hubungi kami", "faq", "syarat & ketentuan", 
                        "pusat edukasi seller"
                    ]
                    if m_id == "Unknown" and (len(name) < 4 or any(g == name_key or g in name_key for g in generic_texts) or "diupdate pada" in name_key):
                        continue
                        
                    if m_id != active_id and name not in all_found:
                        all_found[name] = {"name": name, "element": all_els[r['index']], "id": m_id}
            
            if len(all_found) >= 20: break
            driver.execute_script("document.querySelectorAll('div[class*=\"menu\"], ul[class*=\"menu\"], .ant-popover-content').forEach(el => el.scrollTop += 300);")
            time.sleep(1.5)

        merchants = list(all_found.values())
        if not merchants:
            if "/food/dashboard" in driver.current_url: return True
            log.warning("⚠️ No merchants found in scan.")
            return False

        print("\n" + "="*75 + f"\n  DAFTAR MERCHANT ({len(merchants)} ditemukan):\n" + "="*75)
        for i, m in enumerate(merchants, 1):
            print(f"  {i:2}. {m['name']} (ID: {m['id']})")
            
        if interactive:
            choice = input(f"\nPilih nomor (1-{len(merchants)}) atau Enter untuk lanjut: ").strip()
        else:
            log.info("⏭️  [MERCHANT] Mode otomatis (tanpa timeout), memilih secara otomatis...")
            if "/food/dashboard" not in driver.current_url:
                matched_idx = None
                if active_id_forced:
                    for i, m in enumerate(merchants):
                        if str(m["id"]) == str(active_id_forced):
                            matched_idx = i + 1
                            break
                            
                if matched_idx:
                    log.info(f"👉 Ditemukan indeks merchant yang cocok: {matched_idx} ({merchants[matched_idx-1]['name']})")
                    choice = str(matched_idx)
                else:
                    log.info("👉 [MERCHANT] Onboarding/Selector page detected. Automatically choosing the first merchant to proceed.")
                    choice = "1"
            else:
                choice = ""
            
        if not choice: return True
        
        idx = int(choice)-1
        if 0 <= idx < len(merchants):
            sel = merchants[idx]
            log.info(f"👉 Memilih: {sel['name']}")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", sel["element"])
            time.sleep(0.5)
            try: sel["element"].click()
            except: driver.execute_script("arguments[0].click();", sel["element"])
            
            log.info("  ⏳ Waiting for dashboard redirect...")
            WebDriverWait(driver, 30).until(EC.url_contains("/food/dashboard"))
            time.sleep(2)
            return True
        return False
    except Exception as e:
        log.error(f"Selection error: {e}")
        return False

def return_to_selector(driver) -> bool:
    log.debug("🔄 Opening merchant selector via UI menu (safe mode)...")
    try:
        if "/food/dashboard" not in driver.current_url:
            driver.get(PARTNER_DASHBOARD)
            time.sleep(3)

        wait    = WebDriverWait(driver, 10)
        actions = ActionChains(driver)

        profile_menu = None
        for sel in [".merchantName", ".user-info", "li.ant-menu-item:last-child"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    profile_menu = el
                    break
            except: continue

        if not profile_menu:
            log.warning("⚠️ Profile menu not found — using direct URL fallback.")
            driver.get(MERCHANT_SELECTOR_URL)
            return True

        try:
            actions.move_to_element(profile_menu).perform()
            time.sleep(1)
        except: pass

        safe_click_done = driver.execute_script("""
            var keywords = ['pilih merchant', 'switch merchant', 'ganti merchant'];
            var blacklist = ['log out', 'logout', 'keluar', 'sign out', 'signout'];

            var candidates = Array.from(document.querySelectorAll(
                'li.ant-menu-item, li[role="menuitem"], .ant-dropdown-menu-item, '
                + '[class*="menu-item"], span, div, a'
            ));

            for (var el of candidates) {
                var rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;

                var text = (el.innerText || '').trim().toLowerCase();
                if (!text) continue;

                if (blacklist.some(function(k){ return text.includes(k); })) continue;

                if (keywords.some(function(k){ return text.includes(k); })) {
                    el.click();
                    return true;
                }
            }
            return false;
        """)

        if safe_click_done:
            log.debug("  ✅ Clicked 'Pilih Merchant Lain' safely via JS scan.")
            time.sleep(2)
            return True

        log.warning("  ⚠️ 'Pilih Merchant Lain' not found in dropdown — using direct URL fallback.")
        driver.get(MERCHANT_SELECTOR_URL)
        time.sleep(3)
        return True

    except Exception as e:
        log.error(f"❌ return_to_selector failed: {e} — falling back to direct URL.")
        try: driver.get(MERCHANT_SELECTOR_URL)
        except: pass
        return True

# ── Core Session Retrieval ─────────────────────────────────────────────────────

def get_session(account_name: str = None, username: str = None, password: str = None, phone: str = None, 
                headless: bool = True, close_browser: bool = True, target_name: str = None, interactive: bool = True) -> dict | None:
    if account_name:
        set_session_file(get_session_file_path(account_name))
    else:
        set_session_file(Path(__file__).resolve().parent.parent / "shopee" / "sessions" / "session_default.json")

    saved = load_session()
    session_valid = False
    if saved:
        log.info(f"🔍 [SESSION] Validating saved session via API...")
        if validate_session(saved["shopee_tob_token"], saved["shopee_tob_entity_id"]):
            session_valid = True

    run_headless_now = headless
    login_needed = not session_valid

    for attempt in range(3):
        if attempt > 0:
            run_headless_now = False

        log.info(f"🌐 [BROWSER] Launching isolated browser for '{account_name}' (headless={run_headless_now}, attempt={attempt+1}/3)...")
        driver = _init_driver(headless=run_headless_now, account_name=account_name)
        wait = WebDriverWait(driver, 30)
        session_success = False

        try:
            driver = _init_driver(headless=run_headless_now, account_name=account_name)
            wait = WebDriverWait(driver, 30)
            driver.get(PARTNER_DASHBOARD)
            time.sleep(4)
            
            is_logged_in = False
            current_url = driver.current_url.lower()
            if ("dashboard" in current_url or "merchant-selector" in current_url or "onboarding" in current_url) and attempt == 0:
                log.info("✅ [SESSION] Browser is already logged in.")
                is_logged_in = True
            elif attempt == 0:
                if saved:
                    log.debug("🔍 Attempting to restore browser cookies...")
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
                        log.info("✅ [SESSION] Restored browser login from saved cookies.")
                        is_logged_in = True

            if not is_logged_in and attempt > 0:
                log.info(f"🔄 [SESSION] Attempt {attempt+1}: trying saved tokens before fresh login...")
                if saved and saved.get("shopee_tob_token"):
                    try:
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
                            log.info(f"✅ [SESSION] Restored from saved tokens on retry {attempt+1} — no fresh login needed.")
                            is_logged_in = True
                    except Exception as _cookie_err:
                        log.warning(f"  ⚠️ Cookie injection on retry failed: {_cookie_err}")

                if not is_logged_in:
                    log.info(f"⚠️ [SESSION] Saved tokens also invalid. Forcing fresh login (Attempt {attempt+1})...")
                    driver.delete_all_cookies()
                    driver.get("https://partner.shopee.co.id/login")
                    time.sleep(4)

            if not is_logged_in:
                if run_headless_now:
                    log.info("⚠️ [SESSION] Sesi tidak aktif. Membuka browser dengan antarmuka (headed) untuk login & OTP...")
                    driver.quit()
                    run_headless_now = False
                    driver = _init_driver(headless=False)
                    wait = WebDriverWait(driver, 30)
                    driver.get(PARTNER_DASHBOARD)
                    time.sleep(4)

                log.info("⚠️ [SESSION] Logging in...")
                if "/login" not in driver.current_url.lower() and "authenticate" not in driver.current_url.lower():
                    driver.get("https://partner.shopee.co.id/login")
                    time.sleep(5)
                
                current_url = driver.current_url.lower()
                if "login" in current_url or "authenticate" in current_url or "about:blank" in current_url:
                    success = _perform_login(driver, wait, username, password, phone, is_retry=(attempt == 2))
                    if not success:
                        log.error("❌ [AUTH] _perform_login failed.")
                        driver.quit()
                        continue
                    
                log.info("  ⏳ Menunggu pengalihan halaman setelah login...")
                redirected_ok = False
                for _ in range(30):
                    curr_url = driver.current_url.lower()
                    if "onboarding" in curr_url or "merchant-selector" in curr_url or "dashboard" in curr_url:
                        redirected_ok = True
                        break
                    time.sleep(0.5)

                if redirected_ok and ("onboarding" in driver.current_url.lower() or "merchant-selector" in driver.current_url.lower()):
                    log.info("📍 [SESSION] Detected Onboarding page. Checking page type...")
                    bypass_success = False
                    
                    if _handle_onboarding_invitation(driver):
                        time.sleep(3)
                        if "/food/dashboard" in driver.current_url:
                            log.info("  ✅ [SESSION] Invitation accepted during session init. Continuing...")
                            bypass_success = True
                    
                    if not bypass_success:
                        log.info("📍 [SESSION] Merchant selector detected. Selecting first available merchant...")
                        bypass_js = """
                            var loaders = document.querySelectorAll('.ant-spin, [class*="loading"], .shopee-loading, .ant-spin-nested-loading');
                            loaders.forEach(el => el.remove());
                            var target = document.querySelector('.listItem, .merchant-item, li[class*="item"], [class*="merchant-item"], .ant-list-item');
                            if (target) {
                                target.scrollIntoView({block: 'center'});
                                try { target.click(); } catch(e) {}
                                var clickEvent = new MouseEvent('click', {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window
                                });
                                target.dispatchEvent(clickEvent);
                                return true;
                            }
                            return false;
                        """
                        for _ in range(10):
                            if driver.execute_script(bypass_js):
                                log.debug("  ✅ Selection triggered via JS.")
                                try:
                                    log.debug("  ⏳ Waiting for redirect (either dashboard or onboarding)...")
                                    start_redirect_wait = time.time()
                                    redirected = False
                                    is_onboard_route = False
                                    
                                    while time.time() - start_redirect_wait < 15:
                                        curr_url = driver.current_url.lower()
                                        if "/food/dashboard" in curr_url:
                                            redirected = True
                                            break
                                        if "onboarding" in curr_url:
                                            is_onboard_route = True
                                            redirected = True
                                            break
                                        try:
                                            btns = driver.find_elements(By.XPATH, "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung') or contains(text(), 'Gabung')]")
                                            if any(b.is_displayed() for b in btns):
                                                is_onboard_route = True
                                                redirected = True
                                                break
                                        except: pass
                                        time.sleep(0.5)
                                    
                                    if is_onboard_route:
                                        log.info("📍 [SESSION] Onboarding page/modal detected. Accepting invitation...")
                                        try:
                                            btn_xpath = "//button[contains(., 'Gabung dengan Merchant') or contains(., 'Gabung') or contains(text(), 'Gabung')]"
                                            onboard_btn = WebDriverWait(driver, 10).until(
                                                EC.element_to_be_clickable((By.XPATH, btn_xpath))
                                            )
                                            onboard_btn.click()
                                            log.info("  👉 Clicked 'Gabung' button during session init onboarding")
                                            time.sleep(5)
                                        except Exception as err:
                                            log.warning(f"  ⚠️ Could not click Gabung button: {err}")
                                    
                                    wait.until(lambda d: "/food/dashboard" in d.current_url)
                                    log.debug("  ✅ Landed on dashboard.")
                                    bypass_success = True
                                    break
                                except Exception as e:
                                    log.warning(f"  ⚠️ Onboarding selector bypass attempt failed: {e}")
                            try:
                                container = driver.find_element(By.CSS_SELECTOR, ".ant-list-items, [role='list']")
                                driver.execute_script("arguments[0].scrollTop += 300;", container)
                            except: pass
                            time.sleep(1)
                    if bypass_success: time.sleep(2)

            if login_needed and headless and not run_headless_now:
                log.info("✅ [SESSION] Login berhasil. Menyimpan sesi dan beralih ke mode headless...")
                t, eid = _trigger_and_extract_tokens(driver)
                if not t:
                    log.warning("⚠️ [SESSION] Token extraction failed before transitioning to headless.")
                    driver.quit()
                    continue
                
                all_c = get_all_cookies_dict(driver)
                save_session(t, eid or "", extra_cookies=all_c)
                driver.quit()
                driver = None
                
                log.info("🌐 [SESSION] Membuka kembali browser dalam mode HEADLESS...")
                driver = _init_driver(headless=True)
                wait = WebDriverWait(driver, 30)
                driver.get("https://partner.shopee.co.id/")
                time.sleep(2)
                for name, value in all_c.items():
                    try: driver.add_cookie({"name": name, "value": value})
                    except: pass
                driver.get(PARTNER_DASHBOARD)
                time.sleep(4)
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
            if not eid and active_id and active_id != "None":
                log.info(f"⚠️ [SESSION] Token extraction returned empty entity_id. Using fallback active_id: {active_id}")
                eid = active_id
                
            if not t:
                log.warning("⚠️ Token extraction failed.")
                driver.quit()
                continue
                
            all_c = get_all_cookies_dict(driver)
            save_session(t, eid or "", extra_cookies=all_c)
            res = {"shopee_tob_token": t, "shopee_tob_entity_id": eid or "", "extra_cookies": all_c}
            if not close_browser: res["driver"] = driver
            session_success = True
            return res

        except Exception as e:
            log.error(f"❌ [BROWSER] Session error for '{account_name}' on attempt {attempt+1}: {e}")
        finally:
            if (close_browser or not session_success) and driver is not None:
                try: driver.quit()
                except: pass

    log.error("❌ Max login retries reached.")
    return None

def refresh_tokens(driver, account_name: str = None, fallback_entity_id: str = None) -> dict:
    if account_name:
        set_session_file(get_session_file_path(account_name))
    t, eid = _trigger_and_extract_tokens(driver)
    if not eid and fallback_entity_id and fallback_entity_id != "None":
        log.info(f"⚠️ [SESSION] refresh_tokens: Using fallback_entity_id: {fallback_entity_id}")
        eid = fallback_entity_id
    all_c = get_all_cookies_dict(driver)
    save_session(t, eid or "", extra_cookies=all_c)
    return {"shopee_tob_token": t, "shopee_tob_entity_id": eid or "", "extra_cookies": all_c}
