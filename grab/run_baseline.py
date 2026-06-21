import argparse
import asyncio
import io
import os
import shutil
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
import sys
import os

# --- Toggle Konfigurasi Global ---
ENABLE_GSHEETS_PUSH = False  # Set ke True untuk mengizinkan unggah ke Google Sheets

# Add current directory to sys.path to allow importing local grab_api_scraper
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

from grab_api_scraper import run_api_download_for_portal, validate_credentials

# --- Logging Setup ---
def setup_logger():
    os.makedirs("logs", exist_ok=True)
    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = f"logs/grab_run_{timestamp}.log"
    
    # Only clean up non-log files (like old screenshots)
    for f in Path("logs").glob("*"):
        if f.is_file() and not f.name.endswith(".log"):
            try: f.unlink()
            except: pass

    logger = logging.getLogger("GrabAuto")
    logger.setLevel(logging.INFO)
    # Clear existing handlers if any (for notebook/interactive environments)
    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File
    fh = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    return logger

log = setup_logger()

CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYSUnKOqk29LCktTxdb0wPLbWMbRaWRP3eC_UA4AwYod1FW6zDMhtLMC5ghIvot2B8upCDfBsn-TCP/pub?gid=978201567&single=true&output=csv"

async def run_all(date_start: str = None, date_end: str = None, output_dir: str = None, user_filter: str = None, outlet_filter: str = None, branch_filter: str = None, skip_download: bool = False):
    # Reload env just in case
    load_dotenv(override=True)
    
    log.info(f"Fetching merchant list from spreadsheet...")
    try:
        resp = requests.get(CSV_URL, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        
        # Drop rows where 'Notes' is 'Restricted' (case-insensitive)
        if "Notes" in df.columns:
            grab_df = df[~df["Notes"].astype(str).str.contains("restricted", na=False, case=False)]
        else:
            grab_df = df
        
        # We also need to drop rows where Portal/Username/Password are empty
        grab_df = grab_df.dropna(subset=["Portal", "Username", "Password"])
        
        portals = []
        for idx, row in grab_df.iterrows():
            user = row.get("Username")
            pwd = row.get("Password")
            
            if pd.notna(user) and pd.notna(pwd) and str(user).strip() != "-" and str(pwd).strip() != "-":
                u_str = str(user).strip()
                p_str = str(pwd).strip()
                outlet = row.get("Portal", "Unknown")
                branch = ""  # No branch in VB Grab
                
                # Apply custom outlet and branch filters internally
                if outlet_filter and str(outlet).strip().lower() != str(outlet_filter).strip().lower():
                    continue
                
                # Smart credential validation
                is_valid, err_msg = validate_credentials(u_str, p_str)
                if not is_valid:
                    log.warning(f"⚠️  [VALIDATION WARNING] Row #{idx+1} for '{outlet} ({branch})' has invalid credentials: {err_msg}")
                    
                portals.append({
                    "id": len(portals) + 1,
                    "outlet": outlet,
                    "branch": branch,
                    "user": u_str,
                    "pwd": p_str
                })

        
    except Exception as e:
        log.error(f"Failed to fetch or parse spreadsheet: {e}")
        return

    # Determine output directory
    if output_dir:
        laporan_dir = Path(output_dir)
    else:
        start_str = date_start or "all"
        end_str = date_end or "all"
        laporan_dir = Path("laporan") / f"{start_str}_{end_str}"
    
    log.info("="*60)
    log.info(f"  GRAB MULTI-PORTAL AUTOMATION ({len(portals)} portals)")
    
    unique_users = {}
    for p_info in portals:
        u = p_info["user"]
        if user_filter and user_filter.lower() not in u.lower():
            continue

        if u not in unique_users:
            unique_users[u] = {"pwd": p_info["pwd"], "portals": []}
        unique_users[u]["portals"].append(p_info)
    
    log.info(f"  Unique Accounts: {len(unique_users)}")
    log.info("="*60)
    
    # Auto-cleanup old CSV files for the active portals only
    # Auto-cleanup old CSV files is disabled as per user request
    
    if skip_download:
        log.info("⏭️ [SKIP] Bypassing browser download phase (Phases 1 & 2) as --skip-download is enabled.")
    else:
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            # Load headless setting and concurrency from config.json walk-up
            headless_env = True
            concurrency_limit = 1
            try:
                import json
                for parent in Path(__file__).resolve().parents:
                    config_file = parent / "config.json"
                    if config_file.exists():
                        with open(config_file, "r") as f:
                            config_data = json.load(f)
                            headless_env = config_data.get("headless_grab", True)
                            concurrency_limit = config_data.get("max_concurrency", 1)
                        break
            except Exception:
                pass
            browser = await p.chromium.launch(headless=headless_env)
            semaphore = asyncio.Semaphore(concurrency_limit)
            failures = []

            async def process_user(username, info, is_retry=False):
                password = info["pwd"]
                related_portals = info["portals"]
                first_outlet = related_portals[0]["outlet"]
                
                async with semaphore:
                    log.info(f"[ACCOUNT] Starting for: {username} ({first_outlet})")
                    try:
                        downloaded_file, err = await run_api_download_for_portal(
                            username, password, 
                            start_date=date_start, 
                            browser=browser,
                            is_retry=is_retry
                        )

                        if not downloaded_file:
                            log.error(f"  ✗ [ACCOUNT] {username} Failed: {err}")
                            failures.append({"user": username, "error": err, "outlets": [p["outlet"] for p in related_portals]})
                            return

                        for portal in related_portals:
                            portal_id = portal["id"]
                            outlet_name = f"{portal['outlet']} ({portal['branch']})"
                            laporan_dir.mkdir(parents=True, exist_ok=True)
                            
                            portal_safe_name = f"{portal['outlet']}_{portal['branch']}".replace("/", "_").replace("\\", "_")
                            
                            version = 1
                            dest_xlsx = laporan_dir / f"{portal_safe_name}.xlsx"
                            while dest_xlsx.exists():
                                version += 1
                                dest_xlsx = laporan_dir / f"{portal_safe_name}-{version:02d}.xlsx"
                            
                            try:
                                # Convert directly to XLSX
                                df_temp = pd.read_csv(downloaded_file, dtype=str)
                                df_temp.to_excel(dest_xlsx, index=False)
                                log.info(f"  ✓ [PORTAL {portal_id}] {outlet_name} — Saved to: {dest_xlsx.name}")
                            except Exception as e:
                                log.error(f"  ✗ [PORTAL {portal_id}] {outlet_name} — Failed to convert to excel: {e}")

                    except Exception as e:
                        log.error(f"  ✗ [ACCOUNT] {username} CRITICAL ERROR: {str(e)}")

            tasks = [process_user(u, info) for u, info in unique_users.items()]
            await asyncio.gather(*tasks)
            
            # --- Sequential Retry for Failed Accounts ---
            if failures:
                log.info("\n" + "="*60)
                log.info(f"  [RETRY] Attempting to re-run {len(failures)} failed accounts sequentially to resolve network/concurrency issues...")
                log.info("="*60)
                
                retry_failures = list(failures)
                failures.clear() # Clear so it only contains true failures after retry
                
                for f in retry_failures:
                    username = f["user"]
                    info = unique_users[username]
                    log.info(f"\n  [RETRY ACCOUNT] Re-running sequentially for: {username}")
                    await process_user(username, info, is_retry=True)
                    
            await browser.close()

        log.info("="*60)
        log.info("  ALL PORTALS FINISHED PROCESSING")
        if failures:
            log.info("-" * 60)
            log.info(f"  FAILED ACCOUNTS ({len(failures)}):")
            for f in failures:
                log.info(f"  - {f['user']}: {f['error']}")
        else:
            log.info("  ✓ ALL ACCOUNTS PROCESSED SUCCESSFULLY")
        log.info("="*60)

    # ── Merging Phase to 0Master.xlsx ──
    log.info("📊 Merging all downloaded VB files to 0Master.xlsx...")
    all_data = []
    
    valid_prefixes = []
    for p in portals:
        safe_name = f"{p['outlet']}_{p['branch']}".replace("/", "_").replace("\\", "_")
        valid_prefixes.append(safe_name)
        
    xlsx_files = sorted(laporan_dir.glob("*.xlsx"))
    for fpath in xlsx_files:
        if fpath.name.startswith("MASTER") or fpath.name.startswith("0Master"):
            continue
            
        if not any(fpath.name == f"{vp}.xlsx" or fpath.name.startswith(f"{vp}-") for vp in valid_prefixes):
            continue
            
        try:
            df = pd.read_excel(fpath, dtype=str)
            if not df.empty:
                df.insert(0, 'Portal Filter Name', fpath.stem)
                all_data.append(df)
        except Exception as e:
            log.warning(f"Failed to read {fpath.name} for merging: {e}")
            
    if all_data:
        master_df = pd.concat(all_data, ignore_index=True)
        
        # --- Terapkan Filter Baseline (Long Order ID & Status) ---
        working = master_df.copy()
        if "Long Order ID" in working.columns:
            valid_long_id = working["Long Order ID"].astype(str).str.strip()
            is_valid_order_id = (valid_long_id != "") & valid_long_id.str.contains(r'[^A-Za-z0-9]', regex=True, na=False)
        else:
            is_valid_order_id = pd.Series(True, index=working.index)
            
        if "Status" in working.columns:
            working["Status"] = working["Status"].fillna("").astype(str).str.strip().str.casefold()
            is_not_cancelled = working["Status"].ne("cancelled")
        else:
            is_not_cancelled = pd.Series(True, index=working.index)
            
        master_df = working.loc[is_valid_order_id & is_not_cancelled].copy()
        
        master_xlsx = laporan_dir / "0Master.xlsx"
        version = 1
        while master_xlsx.exists():
            version += 1
            master_xlsx = laporan_dir / f"0Master-{version:02d}.xlsx"
            
        master_df.to_excel(master_xlsx, index=False)
        log.info(f"✅ Successfully merged into: {master_xlsx.name}")
    else:
        log.warning("⚠️ No valid data found to merge into 0Master.")

    log.info("🎉 SUCCESS! Semua laporan mentah VB telah berhasil diunduh ke folder laporan dan di-merge.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Jalankan scraper Grab multi-portal dan hitung omzet."
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Filter awal (inklusif), format YYYY-MM-DD. Contoh: 2026-02-01",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Filter akhir (inklusif), format YYYY-MM-DD. Contoh: 2026-04-30",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory for reports.",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Filter specific username to run.",
    )
    parser.add_argument(
        "--outlet",
        default=None,
        help="Filter specific outlet name to run.",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Filter specific branch name to run.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip browser automation and only process/merge raw files.",
    )
    args = parser.parse_args()
    asyncio.run(run_all(
        date_start=args.start_date, 
        date_end=args.end_date, 
        output_dir=args.output_dir, 
        user_filter=args.user,
        outlet_filter=args.outlet,
        branch_filter=args.branch,
        skip_download=args.skip_download
    ))
