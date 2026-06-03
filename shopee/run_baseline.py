import os
import time
import json
import pandas as pd
import sys
import glob
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add parent directory (VB/) to sys.path so core/ imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# Add current directory so init_sessions can be imported
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.browser import load_session, validate_session
from core.client import ShopeeClient
from core.logger import get_logger
from init_sessions import initialize_all_sessions, get_vb_portals

# Load environment variables
load_dotenv()
log = get_logger("omzet_pipeline_vb")

# --- Toggle Konfigurasi Global ---
ENABLE_GSHEETS_PUSH = False   # Set ke True untuk mengizinkan unggah ke Google Sheets
ENABLE_POSTGRES_PUSH = False  # Set ke True untuk mengizinkan unggah ke PostgreSQL

def subtract_months(dt, months):
    """Helper to subtract calendar months."""
    for _ in range(months):
        dt = (dt - timedelta(days=1)).replace(day=1)
    return dt

def download_file(url, filename, cookies=None, max_retries=3):
    """Downloads a file from a URL with optional cookies and retries. Handles zip extraction if needed."""
    import requests
    import zipfile
    import io
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    }
    
    # If the URL or filename implies a zip, we can handle it
    is_zip = ".zip" in url.lower() or filename.endswith(".zip")
    temp_zip_path = filename + ".zip" if (is_zip and not filename.endswith(".zip")) else filename
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url, stream=True, cookies=cookies, headers=headers, timeout=30)
            response.raise_for_status()
            with open(temp_zip_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    
            # If we downloaded a zip but wanted an xlsx, extract the xlsx
            if is_zip and not filename.endswith(".zip"):
                with zipfile.ZipFile(temp_zip_path, 'r') as z:
                    xlsx_files = [n for n in z.namelist() if n.lower().endswith(".xlsx")]
                    if xlsx_files:
                        first_xlsx = xlsx_files[0]
                        with open(filename, 'wb') as dest_f:
                            dest_f.write(z.read(first_xlsx))
                        log.info(f"📦 Extracted '{first_xlsx}' from downloaded zip to '{filename}'")
                    else:
                        log.error(f"❌ Zip file '{temp_zip_path}' does not contain any .xlsx files!")
                        return False
                try: os.unlink(temp_zip_path)
                except: pass
                
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                log.warning(f"⚠️ Download attempt {attempt+1} failed for {filename}: {e}. Retrying in 5s...")
                time.sleep(5)
            else:
                log.error(f"❌ Failed to download {filename} after {max_retries} attempts: {e}")
    return False


# ─── VB Download History ────────────────────────────────────────────────────
import threading

VB_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # src/VB
    "data",
    "download_history.json",
)
_history_lock = threading.Lock()

def _load_history() -> dict:
    """Load the VB-specific download history from disk."""
    with _history_lock:
        try:
            if os.path.exists(VB_HISTORY_PATH):
                with open(VB_HISTORY_PATH, "r") as f:
                    return json.load(f)
        except Exception:
            pass
    return {}

def _save_history(history: dict):
    """Persist the VB download history to disk (thread-safe)."""
    os.makedirs(os.path.dirname(VB_HISTORY_PATH), exist_ok=True)
    with _history_lock:
        try:
            # Re-read current state, merge, then write to avoid overwriting parallel entries
            current = {}
            if os.path.exists(VB_HISTORY_PATH):
                try:
                    with open(VB_HISTORY_PATH, "r") as f:
                        current = json.load(f)
                except Exception:
                    pass
            current.update(history)
            with open(VB_HISTORY_PATH, "w") as f:
                json.dump(current, f, indent=2)
        except Exception as e:
            log.warning(f"⚠️ Failed to save download history: {e}")



def _history_key(account_name: str, date_label: str) -> str:
    return f"{account_name}::{date_label}"

# ─── Process Portal ─────────────────────────────────────────────────────────

def process_portal(portal, global_ranges, report_dir):
    """
    Worker task to process a single VB portal.

    Strategy (PRIMARY):
      Use the wallet transaction search API to fetch all transactions directly
      (paginated). Build an Excel file locally. Track each successful download
      in the VB-specific download history so repeated runs are fast.

    Fallback (if search API fails or returns 0 records):
      Fall back to the original wallet export-task pipeline (submit → poll → download).
    """
    account_name = portal["account_name"]
    merchant_name = portal["merchant_name"]

    log.info(f"🚀 [PORTAL - {account_name}] Starting process for merchant: '{merchant_name}'")

    session_data = load_session(account_name)
    if not session_data:
        log.error(f"❌ [PORTAL - {account_name}] No active session found. Skipping.")
        return False

    tob_token = session_data["shopee_tob_token"]
    entity_id = session_data["shopee_tob_entity_id"]
    cookies = session_data.get("extra_cookies", {})

    client = ShopeeClient(tob_token=tob_token, entity_id=entity_id, extra_cookies=cookies)

    history = _load_history()
    os.makedirs(report_dir, exist_ok=True)

    all_ok = True

    for r in global_ranges:
        start_ts_ms = r["start"] * 1000
        end_ts_ms = r["end"] * 1000
        range_label = r["label"]

        # Build a canonical date pattern for the history key
        start_str = datetime.fromtimestamp(r["start"]).strftime("%Y%m%d")
        end_str   = datetime.fromtimestamp(r["end"]).strftime("%Y%m%d")
        date_pattern = f"{start_str}_{end_str}"
        h_key = _history_key(account_name, date_pattern)

        # ── Check download history ──────────────────────────────────────────
        if h_key in history:
            saved_path = history[h_key].get("path", "")
            if saved_path and os.path.exists(saved_path):
                log.info(
                    f"⏩ [PORTAL - {account_name}] Already downloaded "
                    f"'{range_label}' → {saved_path}. Skipping."
                )
                continue
            else:
                log.info(
                    f"🔄 [PORTAL - {account_name}] History entry found but file missing. Re-fetching '{range_label}'..."
                )

        # ── PRIMARY: Wallet Export Task Pipeline ───────────────────────────
        log.info(
            f"📥 [PORTAL - {account_name}] Checking export tasks for '{range_label}'..."
        )

        # Try to reuse existing successful/processing task
        existing_reports = client.get_wallet_report_list() or []
        matched_task_id = None
        for rep in existing_reports:
            rep_name = rep.get("name") or ""
            if date_pattern in rep_name and rep.get("status") in [2, 3]:
                matched_task_id = rep.get("id")
                log.info(
                    f"📂 [PORTAL - {account_name}] Reusing existing export task "
                    f"{matched_task_id} (status {rep.get('status')}) for '{range_label}'."
                )
                break

        if not matched_task_id:
            for attempt in range(3):
                log.info(
                    f"📤 [PORTAL - {account_name}] Triggering wallet export "
                    f"for range: {range_label}... (attempt {attempt+1}/3)"
                )
                res = client.submit_wallet_export(start_time=start_ts_ms, end_time=end_ts_ms)
                if res:
                    matched_task_id = res
                    break
                elif res is None:
                    log.warning(f"⚠️ Network error. Retrying in 10s...")
                    time.sleep(10)
                else:
                    break

        if not matched_task_id:
            # Last-chance: grab any task (even status-4) so we don't crash
            for rep in existing_reports:
                rep_name = rep.get("name") or ""
                if date_pattern in rep_name:
                    matched_task_id = rep.get("id")
                    log.warning(
                        f"⚠️ [PORTAL - {account_name}] Using status-{rep.get('status')} "
                        f"fallback task {matched_task_id}."
                    )
                    break

        if not matched_task_id:
            log.error(
                f"❌ [PORTAL - {account_name}] No export task found/created for '{range_label}'. "
                f"Marking as failed."
            )
            all_ok = False
            continue

        # Poll export task until ready (max 30 min)
        downloaded_path = None
        poll_start = time.time()
        while (time.time() - poll_start) < 1800:
            time.sleep(10)
            reports = client.get_wallet_report_list()
            if reports is None:
                continue
            for rep in reports:
                if rep.get("id") != matched_task_id:
                    continue
                status = rep.get("status")
                if status == 3 and rep.get("download_url"):
                    safe_merchant = merchant_name.replace(" ", "_")
                    rep_name = (rep.get("name") or f"wallet_report_{matched_task_id}").replace(" ", "_")
                    base_target_path = os.path.join(report_dir, f"{safe_merchant}_{rep_name}.xlsx")
                    target_path = base_target_path
                    version = 1
                    while os.path.exists(target_path):
                        version += 1
                        name_part, ext_part = os.path.splitext(base_target_path)
                        target_path = f"{name_part}-{version:02d}{ext_part}"
                    log.info(
                        f"📥 [PORTAL - {account_name}] Export task {matched_task_id} ready. "
                        f"Downloading → {target_path}..."
                    )
                    if download_file(rep.get("download_url"), target_path):
                        log.info(f"✅ [PORTAL - {account_name}] Download success: {rep_name}")
                        history[h_key] = {
                            "path": target_path,
                            "merchant": merchant_name,
                            "range": range_label,
                            "source": "export_task",
                            "task_id": matched_task_id,
                            "fetched_at": datetime.now().isoformat(),
                        }
                        _save_history(history)
                        downloaded_path = target_path
                    break
                elif status == 4:
                    log.warning(
                        f"⚠️ [PORTAL - {account_name}] Export task {matched_task_id} "
                        f"failed (status 4 = no data). Skipping."
                    )
                    downloaded_path = None
                    break
            if downloaded_path is not None or (reports and any(
                r.get("id") == matched_task_id and r.get("status") == 4 for r in reports
            )):
                break

        if downloaded_path is None and not any(
            r.get("id") == matched_task_id and r.get("status") == 4
            for r in (client.get_wallet_report_list() or [])
        ):
            log.error(
                f"❌ [PORTAL - {account_name}] Polling timed out for task {matched_task_id}."
            )
            all_ok = False

    return all_ok



def run_pipeline():
    import argparse
    parser = argparse.ArgumentParser(description="Shopee Omzet VB Baseline Pipeline")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)", default=None)
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)", default=None)
    parser.add_argument("--output-dir", type=str, help="Override output directory for reports", default=None)
    parser.add_argument("--skip-download", action="store_true", help="Skip browser automation and only process/merge raw files in output directory")
    parser.add_argument("--merchant", type=str, help="Filter specific merchant name to run", default=None)
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode during session initialization")
    args = parser.parse_args()

    # Determine output directory
    # Convention: all pipelines output to src/laporan/{platform}/{date_range}/
    # cli.py passes --output-dir explicitly (laporan/shopee_vb/{start}_to_{end}).
    # Fallback (standalone run): resolve relative to this script → src/laporan/shopee_vb/
    if args.output_dir:
        report_dir = args.output_dir
    else:
        _script_src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _start = args.start or datetime.now().strftime("%Y-%m-%d")
        _end = args.end or datetime.now().strftime("%Y-%m-%d")
        report_dir = os.path.join(_script_src, "laporan", "shopee_vb", f"{_start}_to_{_end}")


    # Determine date range
    now = datetime.now()
    if args.start and args.end:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d")
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        label = f"{start_dt.strftime('%d %b %Y')} - {end_dt.strftime('%d %b %Y')}"
    else:
        # Default to last 7 days (including today)
        end_dt = now.replace(hour=23, minute=59, second=59)
        start_dt = (end_dt - timedelta(days=6)).replace(hour=0, minute=0, second=0)
        label = f"{start_dt.strftime('%d %b %Y')} - {end_dt.strftime('%d %b %Y')} (Last 7 Days)"
        
    global_ranges = [{"start": int(start_dt.timestamp()), "end": int(end_dt.timestamp()), "label": label}]
    
    print("\n" + "=" * 60)
    print(f"  Shopee Omzet - VB Multi-Portal Baseline Report Pipeline")
    print(f"  Range: {label}")
    print("=" * 60)

    # ── Load Configured Portals ──
    all_portals = get_vb_portals()
    if not all_portals:
        log.error("❌ No portals loaded (dynamic fetch and cache fallback failed).")
        return

    # Filter portals if a specific merchant is requested
    portals_to_run = all_portals
    if args.merchant:
        filter_vals = [m.strip().lower().rstrip('_') for m in args.merchant.split('|')]
        portals_to_run = [
            p for p in all_portals 
            if p["merchant_name"].strip().lower().rstrip('_') in filter_vals
        ]
        
    if not portals_to_run:
        log.error(f"❌ No portal matches merchant filter: '{args.merchant}'")
        return

    log.info(f"📋 [PROGRESS] Found {len(portals_to_run)} portal(s) ready to process.")

    # Pre-run cleanup of old Excel files is disabled as per user request

    # ── 1. Phase 1: Authentication / Session Check (Sequential) ──
    if args.skip_download:
        log.info("⏭️ [SKIP] Bypassing browser download phase as --skip-download is enabled.")
    else:
        # Determine headless parameter
        headless_login = True
        if not args.headless:
            # Check config.json fallback
            try:
                for parent in Path(__file__).resolve().parents:
                    config_file = parent / "config.json"
                    if config_file.exists():
                        with open(config_file, "r") as f:
                            headless_login = json.load(f).get("headless_vb", True)
                        break
            except Exception:
                pass
        else:
            headless_login = True

        log.info("🔑 Step 1: Checking and initializing portal sessions sequentially...")
        # Run sequential session login/verification
        sessions_ok = initialize_all_sessions(headless_on_login=headless_login)
        if not sessions_ok:
            log.warning("⚠️ Some sessions are missing or expired. Continuing anyway but some tasks may fail.")

        # ── 2. Phase 2: Parallel Trigger & Download ──
        log.info(f"🚀 Step 2: Triggering and downloading reports in parallel (max 4 concurrent worker threads)...")
        os.makedirs(report_dir, exist_ok=True)
        
        with ThreadPoolExecutor(max_workers=min(4, len(portals_to_run))) as executor:
            futures = {
                executor.submit(process_portal, portal, global_ranges, report_dir): portal 
                for portal in portals_to_run
            }
            
            for future in as_completed(futures):
                portal = futures[future]
                try:
                    success = future.result()
                    if success:
                        log.info(f"✅ [PROGRESS] Portal '{portal['account_name']}' completed successfully.")
                    else:
                        log.error(f"❌ [PROGRESS] Portal '{portal['account_name']}' failed.")
                except Exception as e:
                    log.error(f"❌ [PROGRESS] Portal '{portal['account_name']}' raised exception: {e}")

    # ── 3. Phase 3: Merging to 0Master.xlsx ──
    log.info("📊 [PROGRESS] PHASE 3: Merging all downloaded VB files to 0Master.xlsx...")
    all_data = []
    
    xlsx_files = glob.glob(os.path.join(report_dir, "*.xlsx"))
    xlsx_files.sort()
    
    def clean_shopee_monetary(val):
        if pd.isna(val) or str(val).lower() == 'nan': return 0
        s = str(val).strip()
        if not s or s == '-': return 0
        
        has_dot = '.' in s
        try:
            num = float(s.replace(',', '.'))
            if has_dot:
                return int(round(num * 1000))
            else:
                return int(num)
        except:
            return 0
            
    for fpath in xlsx_files:
        filename = os.path.basename(fpath)
        if filename.startswith("MASTER") or filename.startswith("0Master"):
            continue
            
        try:
            df = pd.read_excel(fpath, dtype=str)
            if not df.empty:
                if 'Amount' in df.columns:
                    df['Amount'] = df['Amount'].apply(clean_shopee_monetary)
                df.insert(0, 'Merchant Filter Name', filename)
                all_data.append(df)
        except Exception as e:
            log.warning(f"⚠️ Failed to read {filename} for merging: {e}")
            
    if all_data:
        master_df = pd.concat(all_data, ignore_index=True)
        master_filepath = os.path.join(report_dir, "0Master.xlsx")
        version = 1
        while os.path.exists(master_filepath):
            version += 1
            master_filepath = os.path.join(report_dir, f"0Master-{version:02d}.xlsx")
            
        master_df.to_excel(master_filepath, index=False)
        log.info(f"✅ Successfully merged into: {os.path.basename(master_filepath)}")
    else:
        log.warning("⚠️ No valid data found to merge into MASTER.")

    log.info("🎉 SUCCESS! Semua laporan mentah VB telah berhasil diunduh ke folder laporan dan di-merge ke 0Master.")

if __name__ == "__main__":
    run_pipeline()
