import os
import json
import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
import pandas as pd
from playwright.async_api import async_playwright
from dotenv import load_dotenv

import sys
from pathlib import Path
# Dynamically add potential paths for discord_notifier
_curr_path = Path(__file__).resolve()
_paths_to_check = [
    _curr_path.parent.parent,
    _curr_path.parent.parent.parent,
    _curr_path.parent.parent.parent / "src",
]
for _p in _paths_to_check:
    if _p.exists() and str(_p) not in sys.path:
        sys.path.append(str(_p))

try:
    from discord_notifier import send_discord_error
except ImportError:
    def send_discord_error(*args, **kwargs): pass

try:
    from filelock import FileLock
except ImportError:
    # Fallback jika filelock tidak terinstall — gunakan no-op context manager
    import contextlib
    class FileLock:
        def __init__(self, path, timeout=-1): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        @contextlib.contextmanager
        def acquire(self, *a, **kw): yield

# Load environment variables
load_dotenv(override=True)

import logging
import sys
try:
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
    from discord_notifier import send_discord_error
except ImportError:
    def send_discord_error(*args, **kwargs): pass

logger = logging.getLogger("GrabAuto")

class SessionStuckError(Exception):
    """Custom exception when API calls are stuck due to persistent network errors"""
    pass

class IncorrectCredentialsError(Exception):
    """Custom exception when login fails due to wrong username or password"""
    pass

def json_to_grab_csv_fallback(json_data, output_path):
    """Fallback adapter: Converts JSON data to standard Grab CSV format."""
    rows = []
    grabfood_types = ['grabfood', 'grabfood for one']
    
    for tx in json_data:
        if str(tx.get('transaction_type', '')).lower() not in grabfood_types or tx.get('transaction_status') == 'canceled':
            continue

        created_dt = datetime.strptime(tx['created_at'], "%Y-%m-%dT%H:%M:%SZ")
        updated_dt = datetime.strptime(tx['updated_at'], "%Y-%m-%dT%H:%M:%SZ")
        
        rows.append({
            "Created On": created_dt.strftime("%d %b %Y %I:%M %p"),
            "Updated On": updated_dt.strftime("%d %b %Y %I:%M %p"),
            "Long Order ID": tx.get('long_order_id', f"DUMMY-{tx['transaction_id']}"),
            "Category": tx.get('transaction_category', 'Payment').capitalize(),
            "Status": tx.get('transaction_status', 'Settled').capitalize(),
            "Net Sales": tx.get('net_total', 0)
        })
        
    df = pd.DataFrame(rows)
    df.to_csv(output_path, index=False)
    return output_path

import re

def validate_credentials(username, password):
    """
    Smarter and stricter credential validation to catch common human errors.
    Returns (is_valid, error_message)
    """
    if not username or not password:
        return False, "Username or password is empty"
        
    u = str(username).strip().replace('\xa0', '')
    p = str(password).strip().replace('\xa0', '')
    
    if not u or not p:
        return False, "Username or password contains only whitespace"
        
    # Placeholders check
    placeholders = {'-', '--', 'null', 'none', 'n/a', 'na', 'sandi', 'password', 'username', 'pengguna'}
    if u.lower() in placeholders or p.lower() in placeholders:
        return False, f"Credential contains a placeholder value (user: '{u}', pwd: '{p}')"
        
    # Check if username and password are identical
    if u.lower() == p.lower():
        return False, f"Username and Password are identical (likely copy-paste error): '{u}'"
        
    # Check if password is too short
    if len(p) < 6:
        return False, f"Password is too short (less than 6 characters): '{p}'"
        
    # We removed the check that prevents password from containing username 
    # because Grab VB credentials actually use this pattern (e.g. automationde1s / Automationde1s@)
        
    # Check if password looks like an email/username (usually contains @ or domain)
    email_pattern = r'[^@\s]+@[^@\s]+\.[^@\s]+'
    if re.search(email_pattern, p):
        return False, f"Password looks like an email address (likely copy-paste or swap error): '{p}'"
        
    # Domain specific rule: Superfood usernames end with 'superfood'
    if p.lower().endswith('superfood') and len(p) > 10:
        return False, f"Password looks like a Superfood merchant username (ends with 'superfood'): '{p}'"
        
    return True, ""


class GrabAPI:
    def __init__(self, page, username, password):
        self.page = page
        self.username = username
        self.password = password
        self.base_url = "https://merchant.grab.com"

    async def call_api(self, url, method="GET", params=None):
        """Call Grab API from within the page context to reuse session/headers"""
        # Construct URL with params if GET
        full_url = url
        if params and method == "GET":
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            full_url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
        
        js_code = f"""
        async () => {{
            try {{
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 15000);
                
                const response = await fetch("{full_url}", {{
                    method: "{method}",
                    signal: controller.signal,
                    headers: {{
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    }}
                }});
                clearTimeout(timeoutId);
                const status = response.status;
                const text = await response.text();
                try {{
                    return {{ status, data: JSON.parse(text) }};
                }} catch (e) {{
                    return {{ status, data: text }};
                }}
            }} catch (e) {{
                return {{ status: 0, error: e.toString() }};
            }}
        }}
        """
        
        for attempt in range(5):  # Increase to 5 attempts for network resilience
            try:
                # Wait for page to be relatively stable
                if self.page.is_closed():
                    return {"status": 0, "error": "Page closed"}
                
                res = await self.page.evaluate(js_code)
                
                # Handle cases where evaluate might return None
                if res is None:
                    res = {"status": 0, "error": "Evaluation returned None"}

                # Check if it's a network error from the JS side
                if res.get("status") == 0 and res.get("error"):
                    err_msg = res["error"].lower()
                    if "failed to fetch" in err_msg or "networkerror" in err_msg or "aborted" in err_msg:
                        if attempt < 4:
                            logger.info(f"  [Retry] Network error detected in JS fetch, retrying... ({attempt+1})")
                            # Capture screenshot for diagnosis
                            try:
                                os.makedirs("logs", exist_ok=True)
                                ss_path = f"logs/net_error_{self.username}_try{attempt+1}.png"
                                await self.page.screenshot(path=ss_path)
                            except: pass
                            await asyncio.sleep(3)
                            continue
                        else:
                            # If we hit the limit, raise SessionStuckError to trigger a full session refresh
                            raise SessionStuckError(f"Network stuck for {self.username} after 5 attempts")
                
                return res
            except SessionStuckError:
                raise # Re-raise to be caught by run_api_download_for_portal
            except Exception as e:
                err_msg = str(e).lower()
                if ("context was destroyed" in err_msg or "navigation" in err_msg or "network" in err_msg) and attempt < 4:
                    logger.info(f"  [Retry] Playwright execution error, retrying API call... ({attempt+1})")
                    await asyncio.sleep(2)
                    continue
                return {"status": 0, "error": str(e)}
        
        return {"status": 0, "error": "Max retries reached without successful response"}

    async def get_merchant_group_id(self):
        """GET /troy/user-profile/v1/merchant-selector"""
        url = f"{self.base_url}/troy/user-profile/v1/merchant-selector"
        resp = await self.call_api(url)
        status = resp.get("status")
        if status == 200:
            data = resp.get("data", {})
            merchants = data.get("merchants", [])
            if merchants:
                mgid = merchants[0].get("id")
                return mgid
        else:
            logger.warning(f"  [API] merchant-selector returned status {status}: {str(resp.get('data'))[:100]}")
        return None

    async def start_async_download(self, mgid, start_date, end_date):
        """GET /mex/finances/v1/async-transactions-download
        
        Grab API accepts YYYY-MM-DD format for 'from' and 'to' params.
        """
        url = f"{self.base_url}/mex/finances/v1/async-transactions-download"
        params = {
            "merchant_group_id": mgid,
            "store_ids": "all",
            "from": start_date,
            "to": end_date,
            "currency": "IDR"
        }
        resp = await self.call_api(url, params=params)
        if resp.get("status") == 200:
            data = resp.get("data", {})
            ref_id = data.get("data", {}).get("ref_id")
            if ref_id:
                return ref_id, None
            return None, f"No ref_id in 200 response: {data}"
        
        err = f"Status {resp.get('status')}: {resp.get('data') or resp.get('error')}"
        return None, err

    async def poll_for_download(self, mgid, ref_id, max_retries=60):
        """Wait for report to be ready"""
        url = f"{self.base_url}/mex/finances/v1/generated-report/{ref_id}"
        params = {
            "merchant_group_id": mgid,
            "currency": "IDR"
        }
        
        last_error = "Timeout"
        for i in range(max_retries):
            resp = await self.call_api(url, params=params)
            if resp.get("status") == 200:
                outer = resp.get("data") or {}
                inner = outer.get("data") or {}
                status = inner.get("status")
                if status == "SUCCESS":
                    urls = inner.get("urls") or []
                    for u in urls:
                        if u.get("name") == "url" and u.get("url"):
                            return u.get("url"), None
                    return None, "Status SUCCESS but no valid URL found"
                elif status == "FAILED":
                    return None, f"Report generation FAILED: {inner}"
                else:
                    # Still processing
                    pass
            else:
                last_error = f"API status {resp.get('status')}: {resp.get('data') or resp.get('error')}"
            
            await asyncio.sleep(5)
        
        return None, f"Timed out after {max_retries} retries. Last state: {last_error}"

    async def download_csv(self, download_url, filename):
        """Download CSV from URL using page context (to reuse cookies)"""
        try:
            # Use context.request to inherit cookies and headers from the active session
            response = await self.page.context.request.get(download_url, timeout=60000)
            if response.status == 200:
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                body = await response.body()
                with open(filename, 'wb') as f:
                    f.write(body)
                return True, None
            return False, f"HTTP {response.status}"
        except Exception as e:
            return False, str(e)

    async def execute_fallback(self, mgid, start_date, end_date):
        """Executes the S3 Insights bypass API strategy to replace V1+V2."""
        from datetime import datetime
        import json
        import uuid
        import requests
        
        logger.info(f"  [S3 Insights] Starting S3 bypass for {start_date} to {end_date}...")
        
        # 1. Fetch store list for payload (hanya butuh memanggil 1 kali untuk dapat semua store)
        stores_url = f"{self.base_url}/mex/finances/v1/transactions?merchant_group_id={mgid}&from={start_date}&to={start_date}&limit=50&offset=0&currency=IDR"
        st_resp = await self.call_api(stores_url)
        store_grab_ids = []
        if st_resp.get("status") == 200:
            for st in st_resp.get("data", {}).get("data", []):
                sid = st.get("store_id")
                if sid: store_grab_ids.append(sid)
                
        # Remove duplicates
        store_grab_ids = list(set(store_grab_ids))
        logger.info(f"  [S3 Insights] Found {len(store_grab_ids)} stores. Building payload...")
                
        # 2. Convert dates to ISO 8601
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_date_str = start_dt.strftime("%Y-%m-%dT00:00:00.000Z")
        end_date_str = end_dt.strftime("%Y-%m-%dT23:59:59.000Z")
        
        insights_url = f"{self.base_url}/mex-insights/download/v1/csv?merchant_group_id={mgid}&currency=IDR"
        payload = {
            "merchant_group_id": mgid,
            "parentEntityIds": [],
            "businessLines": [],
            "endDate": end_date_str,
            "queryNames": ["sales-performance-download"],
            "startDate": start_date_str,
            "storeGrabIDs": store_grab_ids,
            "storeZeusIDs": []
        }
        
        # 3. Post to Insights API directly using evaluate to bypass Playwright context limits
        res_json = await self.page.evaluate(f"""
        async (payloadObj) => {{
            const r = await fetch("{insights_url}", {{
                method: "POST",
                headers: {{
                    "accept": "application/json",
                    "content-type": "application/json",
                    "x-api-source": "mex-insign",
                    "x-currency": "IDR",
                    "x-merchant-id": "{mgid}"
                }},
                body: JSON.stringify(payloadObj)
            }});
            if (!r.ok) return {{ error: await r.text() }};
            return await r.json();
        }}
        """, payload)
        
        if not res_json or "ref_id" not in res_json:
            return None, f"Failed to get ref_id from S3 Insights API: {res_json}"
            
        ref_id = res_json.get("ref_id")
        logger.info(f"  [S3 Insights] Ref ID obtained: {ref_id}. Polling for generated CSV...")
        
        # 4. Poll for URL
        for i in range(40):
            await asyncio.sleep(5)
            p_url = f"{self.base_url}/mex-insights/download/v1/generated-insights/{ref_id}"
            p_data = await self.page.evaluate(f"""
            async () => {{
                const r = await fetch("{p_url}", {{
                    headers: {{
                        "accept": "application/json",
                        "x-api-source": "mex-insign",
                        "x-merchant-id": "{mgid}"
                    }}
                }});
                if (!r.ok) return {{ status: "HTTP_ERROR" }};
                return await r.json();
            }}
            """)
            
            status = p_data.get("status")
            if status == "SUCCESS":
                s3_url = p_data.get("s3_url")
                if not s3_url:
                    return None, "S3 Insights status SUCCESS but no s3_url provided."
                    
                job_id = uuid.uuid4().hex[:8]
                filename = f"downloads/grab_transactions_{mgid}_s3_{job_id}.csv"
                os.makedirs("downloads", exist_ok=True)
                
                # Using standard requests to fetch S3 (since it's a pre-signed URL)
                logger.info(f"  [S3 Insights] S3 Link found! Downloading to {filename}...")
                csv_req = requests.get(s3_url)
                if csv_req.status_code == 200:
                    with open(filename, 'w', encoding='utf-8') as f:
                        f.write(csv_req.text)
                    logger.info(f"  [S3 Insights] CSV successfully downloaded.")
                    return filename, None
                else:
                    return None, f"Failed to download from S3 URL. HTTP {csv_req.status_code}"
            elif status == "IN_PROGRESS":
                continue
            else:
                return None, f"S3 Polling failed with status: {status}"
                
        return None, "S3 Polling timeout after 40 attempts."

async def perform_login(page, user, pwd, is_retry=False):
    """Robust login steps — clears cookies on mismatch and handles sticky 'Welcome back' pages."""
    CLEAN_LOGIN_URL = (
        "https://weblogin.grab.com/merchant/login"
        "?service_id=MEXUSERS&redirect=https%3A%2F%2Fmerchant.grab.com%2Fportal"
    )
    
    # Random stagger to avoid simultaneous hits
    import random
    stagger = random.uniform(1.0, 5.0)
    await asyncio.sleep(stagger)
    try:
        async def check_block_and_errors():
            # Check for Block Screen
            block_texts = [
                "temporarily blocked due to multiple invalid login attempts",
                "try again later",
                "coba lagi nanti",
                "diblokir sementara"
            ]
            page_content = await page.content()
            for text in block_texts:
                if text.lower() in page_content.lower():
                    os.makedirs("logs", exist_ok=True)
                    ss_path = f"logs/account_blocked_{user}.png"
                    await page.screenshot(path=ss_path)
                    logger.error(f"  ✗ [Login] Account blocked screen detected for {user}. Screenshot saved to {ss_path}.")
                    if is_retry:
                        send_discord_error(
                            platform="Grab", 
                            merchant=user, 
                            error_type="BLOCKED_ACCOUNT", 
                            message="Akun ditangguhkan (suspended) sementara oleh sistem Grab karena terlalu banyak percobaan salah."
                        )
                    raise IncorrectCredentialsError(f"Account is temporarily blocked due to multiple invalid login attempts.")

            # Check for Incorrect Credentials Error on screen
            error_texts = [
                "Make sure you have the right username",
                "attempts left",
                "Pastikan nama pengguna dan kata sandi",
                "kesempatan tersisa",
                "salah memasukkan password",
                "Kredensial tidak valid",
                "Username atau kata sandi",
                "We didn't recognize that",
                "Invalid credentials",
                "Wrong credentials",
                "Incorrect password",
                "Kata sandi salah",
                "Alamat email atau nomor telepon salah",
                "Please check your login details",
                "akun tidak ditemukan",
                "tidak dapat menemukan akun",
                "couldn't find an account",
                "account not found",
                "email tidak terdaftar",
                "detail tidak dikenali",
                "akun tidak dikenali",
                "not recognize that"
            ]
            for text in error_texts:
                if text.lower() in page_content.lower():
                    # Take screenshot of the exact failure
                    os.makedirs("logs", exist_ok=True)
                    ss_path = f"logs/incorrect_credentials_{user}.png"
                    await page.screenshot(path=ss_path)
                    logger.error(f"  ✗ [Login] Wrong credentials error screen detected for {user}. Screenshot saved to {ss_path}.")
                    if is_retry:
                        send_discord_error(
                            platform="Grab", 
                            merchant=user, 
                            error_type="WRONG_CREDENTIALS", 
                            message="Gagal login. Kombinasi Email/Username dan Kata Sandi tidak cocok, atau halaman mengindikasikan kredensial salah."
                        )
                    raise IncorrectCredentialsError(f"Incorrect username or password. Remaining attempts warning shown on page.")

        print(f"  [Login] Navigating to login page for {user}...")
        for attempt in range(3):
            try:
                # Use clean login URL directly to avoid most 'Welcome back' issues
                await page.goto(CLEAN_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception as nav_err:
                if attempt < 2:
                    logger.info(f"  [Login] Navigation error ({nav_err}), retrying... ({attempt+1})")
                    await asyncio.sleep(5)
                else:
                    raise nav_err

        await page.wait_for_timeout(3000)

        # Pre-emptively check for any blocks or lockout screens already visible
        await check_block_and_errors()

        # Check for block pages (anti-bot)
        content = await page.content()
        if "Attention Required" in await page.title() or "cloudflare" in content.lower() or "distil" in content.lower():
            logger.error(f"  ✗ [BLOCK] Detected anti-bot page for {user}.")
            await page.screenshot(path=f"blocked_{user}.png")
            if is_retry:
                send_discord_error("Grab", user, "BLOCKED_ACCOUNT", "Browser dihadang oleh proteksi keamanan Grab (Cloudflare/Anti-Bot). Tidak bisa melanjutkan login.", "")
            return False

        # --- Handle Sticky "Welcome back" / Saved Accounts page ---
        is_saved_accounts = "saved-accounts" in page.url
        welcome_back_locator = page.locator('h1:has-text("Welcome back"), h2:has-text("Welcome back"), div:has-text("Welcome back")')

        if is_saved_accounts or await welcome_back_locator.count() > 0:
            content_lower = (await page.content()).lower()
            if user.lower() in content_lower:
                logger.info(f"  [Login] Saved account matches {user}, clicking 'Continue'...")
                continue_btn = page.locator('button:has-text("Continue"), button:has-text("Lanjut")')
                if await continue_btn.count() > 0:
                    await continue_btn.first.click()
                    # Wait for either dashboard or password field
                    try:
                        await page.wait_for_selector('input[type="password"], .dashboard, .portal-content', timeout=10000)
                    except: pass
                    
                    if "login" not in page.url.lower() and "saved-accounts" not in page.url:
                        return True
            else:
                # IMPORTANT: If it's a mismatch, don't just click "another user", 
                # CLEAR COOKIES to force a fresh login form
                logger.info(f"  [Login] Saved account mismatch for {user}. Clearing cookies for fresh start...")
                await page.context.clear_cookies()
                await page.goto(CLEAN_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

        # Check again if page has changed to a block screen after cookie clear/navigation
        await check_block_and_errors()

        # --- Normal Login Flow ---
        user_selectors = [
            'input[type="email"]', 'input[name="email"]', 'input[type="text"]',
            'input[placeholder*="Email" i]', 'input[placeholder*="Username" i]',
            '#email', '#username',
        ]

        async def find_username_field():
            for sel in user_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=5000) and await el.is_enabled():
                        return el
                except: continue
            return None

        user_field = await find_username_field()
        if not user_field and "saved-accounts" in page.url:
            await page.goto(CLEAN_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            user_field = await find_username_field()

        if user_field:
            # Focus, clear, and fill with re-verification loop
            for fill_attempt in range(3):
                await user_field.click()
                await user_field.fill("")
                await user_field.fill(user)
                await page.wait_for_timeout(500)
                
                # Check value
                val = await user_field.input_value()
                if val.strip() == user.strip():
                    break
                
                # Alternate method if simple fill fails: keyboard typing simulation
                logger.warning(f"  [Login] Field value mismatch for {user} (got '{val}'), using keyboard simulation... ({fill_attempt+1})")
                await user_field.click()
                # Select all and delete
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(user, delay=50)
                await page.wait_for_timeout(500)
                
                val = await user_field.input_value()
                if val.strip() == user.strip():
                    break
                await page.wait_for_timeout(1000)

            # Click the Continue button explicitly to trigger event listeners properly,
            # or press Enter if button not found
            continue_btn = page.locator('button:has-text("Continue"), button:has-text("Lanjut")').first
            if await continue_btn.count() > 0 and await continue_btn.is_visible():
                await continue_btn.click()
            else:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(2500)

            # Check for Block Screen immediately after username submission
            await check_block_and_errors()

        # Password field
        pwd_selector = 'input[type="password"], #password'
        try:
            await page.wait_for_selector(pwd_selector, timeout=15000)
        except:
            continue_btns = page.locator('button:has-text("Continue"), button:has-text("Next"), button:has-text("Lanjut")')
            if await continue_btns.count() > 0:
                await continue_btns.first.click()
                try: await page.wait_for_selector(pwd_selector, timeout=10000)
                except: pass
        
        # Check again before password input to catch late lockout renders
        await check_block_and_errors()

        if await page.locator(pwd_selector).count() > 0:
            await page.fill(pwd_selector, pwd)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Enter")
            
            # Wait for a couple of seconds to see if error message or redirect happens
            await page.wait_for_timeout(3000)
            
            # Check for error elements/texts after password submission
            await check_block_and_errors()
                    
            try:

                await page.wait_for_url(lambda u: "login" not in u.lower() and "saved-accounts" not in u, timeout=20000)
                await page.wait_for_load_state("networkidle")
            except: pass

        
        return "login" not in page.url.lower() and "saved-accounts" not in page.url
    except IncorrectCredentialsError:
        raise
    except Exception as e:
        logger.error(f"  ✗ [Login] Failed: {e}")
        return False


async def run_api_download_for_portal(user, pwd, start_date: str = None, end_date: str = None, browser=None, is_retry=False):
    # Proactively validate credentials before proceeding to run session
    is_valid, err_msg = validate_credentials(user, pwd)
    if not is_valid:
        logger.error(f"  ✗ [Validation] Invalid credentials for {user}: {err_msg}")
        if is_retry:
            send_discord_error(
                platform="Grab", 
                merchant=user, 
                error_type="WRONG_CREDENTIALS", 
                message=f"Validasi ditolak sebelum masuk browser. Kredensial tidak logis/kosong: {err_msg}"
            )
        return None, f"Invalid credentials: {err_msg}"

    session_dir = "sessions"
    os.makedirs(session_dir, exist_ok=True)
    session_path = os.path.join(session_dir, f"{user}.json")

    p = None
    managed_browser = None

    # Date range — Grab API accepts YYYY-MM-DD format
    report_end   = end_date   or datetime.now().strftime("%Y-%m-%d")
    report_start = start_date or (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    logger.info(f"  [Date] Download range: {report_start} → {report_end} (YYYY-MM-DD)")

    # --- Outer loop for handling fatal download errors (refresh session if download fails) ---
    for run_attempt in range(2):
        # --- Session + Auth (done ONCE per run_attempt) ---
        context = None
        mgid = None
        page = None
        for auth_attempt in range(2):  # Allow at most 1 re-auth if session is stale
            try:
                if browser is None and managed_browser is None:
                    p_inst = await async_playwright().start()
                    headless_env = True
                    try:
                        from pathlib import Path
                        import json
                        for parent in Path(__file__).resolve().parents:
                            config_file = parent / "config.json"
                            if config_file.exists():
                                with open(config_file, "r") as f:
                                    headless_env = json.load(f).get("headless_grab", True)
                                break
                    except Exception:
                        pass
                    managed_browser = await p_inst.chromium.launch(
                        headless=headless_env,
                        args=[
                            "--disable-extensions",
                            "--disable-component-update"
                        ]
                    )
                    browser = managed_browser
                    p = p_inst
    
                storage_state = session_path if os.path.exists(session_path) and auth_attempt == 0 else None
                context = await browser.new_context(
                    storage_state=storage_state,
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
    
                if auth_attempt > 0:
                    logger.info(f"  [Action] Re-opening session for {user} (Auth attempt {auth_attempt + 1})...")
    
                is_on_login_page = True
                if storage_state is not None:
                    logger.info(f"  [Isolation] Checking session for {user}...")
                    try:
                        # Gunakan timeout yang lebih pendek (15 detik) agar tidak menggantung terlalu lama saat redirect
                        await page.goto("https://merchant.grab.com/dashboard", wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(1000)
                        
                        # Cek apakah sukses masuk ke dashboard / portal utama
                        current_url = page.url.lower()
                        if ("dashboard" in current_url or "portal" in current_url) and "login" not in current_url and "saved-accounts" not in current_url:
                            is_on_login_page = False
                    except Exception as e:
                        logger.warning(f"  [Isolation] Session check timed out or failed: {e}")
                else:
                    logger.info(f"  [Isolation] No saved session state found for {user}. Going straight to login...")
    
                api = GrabAPI(page, user, pwd)
                mgid = None
                
                # Hanya coba ambil mgid jika kita masih berada di halaman merchant
                if not is_on_login_page:
                    mgid = await api.get_merchant_group_id()
    
                if not mgid or is_on_login_page:
                    logger.info(f"  [Session] Not active or redirected to login. Logging in...")
                    if await perform_login(page, user, pwd, is_retry):
                        # Setelah login sukses, pastikan kita berada di merchant page sebelum ambil API
                        try:
                            await page.goto("https://merchant.grab.com/dashboard", wait_until="domcontentloaded", timeout=30000)
                            await page.wait_for_timeout(2000)
                        except: pass
                        
                        mgid = await api.get_merchant_group_id()
                        if mgid:
                            _lock = FileLock(f"{session_path}.lock", timeout=30)
                            with _lock:
                                await context.storage_state(path=session_path)
                            logger.info(f"  [Session] Login success, session saved.")
                        else:
                            logger.error(f"  ✗ [Session] Login success but failed to get MGID for {user}.")
                            os.makedirs("logs", exist_ok=True)
                            await page.screenshot(path=f"logs/auth_fail_mgid_{user}.png")
                            if is_retry:
                                send_discord_error("Grab", user, "SYSTEM_ERROR", "Login berhasil, tetapi sistem gagal mengidentifikasi Merchant Group ID (MGID). Kemungkinan akun tidak memiliki akses ke portal toko.", "")
                    else:
                        logger.error(f"  ✗ [Session] Login failed for {user}.")
                        os.makedirs("logs", exist_ok=True)
                        await page.screenshot(path=f"logs/login_fail_{user}.png")
                else:
                    _lock = FileLock(f"{session_path}.lock", timeout=30)
                    with _lock:
                        await context.storage_state(path=session_path)
    
                if mgid:
                    break  # Auth succeeded — exit auth retry loop
    
                # Auth failed — close context and try once more without saved session
                await context.close()
                context = None
                if auth_attempt >= 1:
                    if managed_browser:
                        await managed_browser.close()
                    if p:
                        await p.stop()
                    if is_retry:
                        send_discord_error("Grab", user, "LOGIN_FAILED", "Gagal login ke portal Grab. Tidak ada keterangan spesifik pada halaman (bisa jadi OTP/Timeout).", "")
                    return None, "Auth failed after 2 attempts"
    
            except IncorrectCredentialsError as ice:
                logger.error(f"  ✗ [Fatal Login Error] Aborting immediately for {user} to prevent lockout: {ice}")
                if context:
                    await context.close()
                if managed_browser:
                    await managed_browser.close()
                if p:
                    await p.stop()
                return None, f"Incorrect credentials: {ice}"
            except Exception as e:
                logger.error(f"  [Error] Auth attempt {auth_attempt + 1} failed for {user}: {e}")
                if context:
                    await context.close()
                context = None
                if auth_attempt >= 1:
                    if managed_browser:
                        await managed_browser.close()
                    if p:
                        await p.stop()
                    return None, str(e)
    
        if not mgid:
            if managed_browser:
                await managed_browser.close()
            if p:
                await p.stop()
            return None, "Auth failed"

        # --- Native Grab CSV Export as PRIMARY METHOD ---
        logger.info(f"  [Action] Executing Native S3 Download as PRIMARY method for {user}...")
        s3_filename = None
        last_dl_err = ""
        
        for dl_attempt in range(2):
            try:
                ref_id, err = await api.start_async_download(mgid, report_start, report_end)
                if not ref_id:
                    logger.warning(f"  [Download] start_async_download failed for {user}: {err}")
                    last_dl_err = f"Req err: {err}"
                    await asyncio.sleep(3)
                    continue

                download_url, err = await api.poll_for_download(mgid, ref_id)
                if not download_url:
                    logger.warning(f"  [Download] Polling failed for {user}: {err}")
                    last_dl_err = f"Poll err: {err}"
                    await asyncio.sleep(3)
                    continue

                job_id = uuid.uuid4().hex[:8]
                filename = f"downloads/grab_transactions_{user}_{job_id}.csv"
                success, err = await api.download_csv(download_url, filename)

                if not success:
                    os.makedirs("logs", exist_ok=True)
                    await page.screenshot(path=f"logs/download_fail_{user}.png")
                    logger.warning(f"  [Download] CSV download failed for {user}: {err}")
                    last_dl_err = f"DL err: {err}"
                    await asyncio.sleep(3)
                    continue

                # Success!
                s3_filename = filename
                break

            except SessionStuckError as se:
                logger.warning(f"  [Action] SessionStuck on S3 download for {user}: {se}")
                last_dl_err = f"Stuck: {se}"
                # Delete broken session file to force fresh login on next attempt
                if os.path.exists(session_path):
                    try: os.remove(session_path)
                    except: pass
                break
            except Exception as e:
                logger.error(f"  [Error] S3 Download attempt failed for {user}: {e}")
                last_dl_err = f"Ex: {e}"
                break
                
        if s3_filename:
            logger.info(f"  [Action] Native S3 Download Success! Returning generated CSV.")
            if context: await context.close()
            if managed_browser: await managed_browser.close()
            if p: await p.stop()
            return s3_filename, None
            
        logger.warning(f"  [Action] Primary S3 method failed for {user}: {last_dl_err}.")

        # If it failed due to a stuck session, don't bother with fallback, it will fail too.
        if "Stuck:" not in last_dl_err:
            logger.info(f"  [Action] Executing Fast API Extraction (V1+V2) as fallback method for {user}...")
            fast_filename = None
            fast_err = ""
            try:
                fast_filename, fast_err = await api.execute_fallback(mgid, report_start, report_end)
            except SessionStuckError as se:
                logger.warning(f"  [Action] SessionStuck during fallback for {user}: {se}")
                fast_err = f"Stuck fallback: {se}"
                if os.path.exists(session_path):
                    try: os.remove(session_path)
                    except: pass
            except Exception as e:
                logger.error(f"  [Error] Fallback attempt failed for {user}: {e}")
                fast_err = f"Fallback Ex: {e}"
            
            if fast_filename:
                logger.info(f"  [Action] Fast API Extraction Fallback Success! Returning generated CSV.")
                if context: await context.close()
                if managed_browser: await managed_browser.close()
                if p: await p.stop()
                return fast_filename, None
                
            logger.warning(f"  [Action] Fallback Fast API Extraction failed for {user}: {fast_err}.")
            last_dl_err += f" | Fallback Err: {fast_err}"
        else:
            logger.info(f"  [Action] Skipping fallback because session is stuck.")
        
        if run_attempt >= 1 and is_retry:
            send_discord_error("Grab", user, "DOWNLOAD_FAILED", f"Gagal mengunduh laporan S3 maupun Fallback API: {last_dl_err[:200]}", "")
            
        # If fallback also fails, then proceed to clean up and retry whole session if possible
        if context:
            await context.close()
            
        if run_attempt < 1:
            logger.info(f"  [Action] Refreshing session: Re-logging in after download failure... (Run attempt {run_attempt + 1})")
            continue # Try again from auth
        else:
            if managed_browser:
                await managed_browser.close()
            if p:
                await p.stop()
            return None, last_dl_err

    if managed_browser:
        await managed_browser.close()
    if p:
        await p.stop()
    return None, "Max account-level retries reached"

if __name__ == "__main__":
    async def main():
        load_dotenv()
        u, p = os.getenv("GRAB_USERNAME_PORTAL1"), os.getenv("GRAB_PASSWORD_PORTAL1")
        if u and p:
            res, err = await run_api_download_for_portal(u, p)
            print(f"Result: {res or err}")
    asyncio.run(main())
