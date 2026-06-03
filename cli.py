#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  VB REPORT — Unified Baseline Transaction Pipeline
  Grab & Shopee in one CLI
═══════════════════════════════════════════════════════════════
"""

import argparse
import sys
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Add parent directory to sys.path so core/ imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def normalize_date_string(date_str: str) -> str:
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Format tanggal tidak valid: '{date_str}'. Gunakan DD-MM-YYYY atau YYYY-MM-DD.")

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
MAGENTA = "\033[95m"
DIM    = "\033[2m"

def banner():
    FONT = {
        'V': ["██      ██", "██      ██", " ▀██▄▄██▀ ", "   ████   ", "    ██    "],
        'B': ["█████████▄", "██     ▀██", "█████████▀", "██     ▀██", "█████████▀"],
        'R': ["████████▄ ", "██     ▀██", "████████▀ ", "██     ▀██", "██      ██"],
        'E': ["██████████", "██        ", "███████   ", "██        ", "██████████"],
        'P': ["████████▄ ", "██     ▀██", "████████▀ ", "██        ", "██        "],
        'O': [" ▄██████▄ ", "██▀    ▀██", "██      ██", "██      ██", " ▀██████▀ "],
        'T': ["█████████ ", "    ██    ", "    ██    ", "    ██    ", "    ██    "]
    }

    def get_word_lines(word):
        widths = [len(FONT[char][0]) for char in word]
        letter_grids = []
        for char in word:
            grid = FONT[char]
            width = len(grid[0])
            comp_grid = [[' ' for _ in range(width + 1)] for _ in range(6)]
            for r in range(5):
                for c in range(width):
                    val = grid[r][c]
                    if val != ' ':
                        comp_grid[r][c] = val
            for r in range(5):
                for c in range(width):
                    val = grid[r][c]
                    if val != ' ':
                        if comp_grid[r+1][c+1] == ' ':
                            comp_grid[r+1][c+1] = '▒'
            letter_grids.append(comp_grid)
        return letter_grids, widths

    gradient_colors = [39, 44, 45, 51, 85, 87, 117, 122, 123, 153, 159, 195, 231]
    
    # Render VB
    t_grids, t_widths = get_word_lines("VB")
    t_total = sum(t_widths) + 1 * 2

    print(f"\033[90m=================================================================\033[0m")
    
    # Print VB
    for r in range(6):
        line = "              "
        curr_col = 0
        for l_idx, grid in enumerate(t_grids):
            width = len(grid[0])
            for c in range(width):
                char = grid[r][c]
                factor = curr_col / max(1, t_total - 1)
                color_idx = min(len(gradient_colors) - 1, max(0, int(factor * (len(gradient_colors) - 1))))
                color_code = gradient_colors[color_idx]
                if char == '▒':
                    line += "\033[38;5;238m█\033[0m"
                elif char != ' ':
                    line += f"\033[38;5;{color_code}m{char}\033[0m"
                else:
                    line += ' '
                curr_col += 1
            line += "  "
            curr_col += 2
        print(line)
        
    print(f"\033[90m=================================================================\033[0m")
    print()

def _resolve_python_executable() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    venv_python = os.path.join(base, ".venv", "bin", "python")
    if os.path.isfile(venv_python):
        return venv_python
    parent_venv = os.path.join(os.path.dirname(base), "src", ".venv", "bin", "python")
    if os.path.isfile(parent_venv):
        return parent_venv
    return sys.executable

def _resolve_output_dir(platform_name: str, start_date: str, end_date: str) -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(base, "laporan", f"{platform_name}_vb", f"{start_date}_to_{end_date}")
    os.makedirs(out, exist_ok=True)
    return out

def run_grab_vb(start_date: str, end_date: str, user_filter: str = None, outlet_filter: str = None, branch_filter: str = None):
    grab_dir = os.path.join(os.path.dirname(__file__), "grab")
    if not os.path.isdir(grab_dir):
        print(f"{RED}[ERROR]{RESET} Grab directory not found: {grab_dir}")
        return False

    output_dir = _resolve_output_dir("grab", start_date, end_date)
    import subprocess
    python_exe = _resolve_python_executable()
    cmd = [
        python_exe, "run_baseline.py",
        "--start-date", start_date,
        "--end-date", end_date,
        "--output-dir", output_dir,
    ]
    if user_filter: cmd.extend(["--user", user_filter])
    if outlet_filter: cmd.extend(["--outlet", outlet_filter])
    if branch_filter: cmd.extend(["--branch", branch_filter])

    print(f"\n{GREEN}{BOLD}▶ GRAB VB PIPELINE{RESET}")
    result = subprocess.run(cmd, cwd=grab_dir)
    return result.returncode == 0

def run_shopee_vb(start_date: str, end_date: str, merchant_filter: str = None):
    shopee_dir = os.path.join(os.path.dirname(__file__), "shopee")
    if not os.path.isdir(shopee_dir):
        print(f"{RED}[ERROR]{RESET} Shopee directory not found: {shopee_dir}")
        return False

    output_dir = _resolve_output_dir("shopee", start_date, end_date)
    import subprocess
    python_exe = _resolve_python_executable()
    cmd = [
        python_exe, "run_baseline.py",
        "--start", start_date,
        "--end", end_date,
        "--output-dir", output_dir,
    ]
    if merchant_filter: cmd.extend(["--merchant", merchant_filter])

    print(f"\n{MAGENTA}{BOLD}▶ SHOPEE VB PIPELINE{RESET}")
    result = subprocess.run(cmd, cwd=shopee_dir)
    return result.returncode == 0

def interactive_mode():
    os.system('cls' if os.name == 'nt' else 'clear')
    banner()

    print(f"  {BOLD}Pilih platform:{RESET}")
    print(f"    {GREEN}[1]{RESET} Grab")
    print(f"    {MAGENTA}[2]{RESET} Shopee")
    print(f"    {CYAN}[3]{RESET} Kedua Platform (Grab + Shopee)")
    print()

    while True:
        choice = input(f"  {BOLD}Pilihan (1/2/3):{RESET} ").strip()
        if choice in ("1", "2", "3"):
            break
        print(f"  {RED}Input tidak valid. Masukkan 1, 2, atau 3.{RESET}")

    platform_map = {"1": "grab", "2": "shopee", "3": "all"}
    platform = platform_map[choice]

    print(f"\n  {BOLD}Pilih cakupan outlet:{RESET}")
    print(f"    {GREEN}[1]{RESET} Pilih semua outlet")
    print(f"    {YELLOW}[2]{RESET} Pilih custom (Filter spesifik){RESET}")
    print()

    while True:
        scope_choice = input(f"  {BOLD}Pilihan (1/2):{RESET} ").strip()
        if scope_choice in ("1", "2"):
            break
        print(f"  {RED}Input tidak valid. Masukkan 1 atau 2.{RESET}")

    outlet = []
    branch = []
    shopee_merchant = []

    if scope_choice == "2":
        import pandas as pd
        import requests
        import io

        print(f"\n  {CYAN}[INFO] Mengunduh daftar merchant VB terbaru dari Google Sheets...{RESET}")
        CSV_URL_MAIN = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ3tLKBNXDqRgBw0mNhKZFxgvKx-JoiTDzm_s5Ix1cm7O6HCv4IvExOLR2HSRVaXSsx82V348mcr9X4/pub?gid=0&single=true&output=csv"
        try:
            resp_main = requests.get(CSV_URL_MAIN, timeout=30)
            resp_main.raise_for_status()
            df_main = pd.read_csv(io.StringIO(resp_main.text))
            df_vb = df_main[df_main["Role"].str.strip().str.lower() == "owner"] if "Role" in df_main.columns else df_main
        except Exception as e:
            print(f"  {RED}[ERROR] Gagal mengunduh Google Sheets: {e}{RESET}")
            sys.exit(1)

        # Grab Filters
        if platform in ("grab", "all"):
            df_grab = df_vb[df_vb["Aplikasi"].str.contains("Grab", na=False, case=False)] if not df_vb.empty else pd.DataFrame()
            if not df_grab.empty:
                outlets_list = sorted(df_grab["Nama Outlet"].dropna().unique())
                print(f"\n  {BOLD}Pilih Outlet Grab VB:{RESET}")
                for idx, o_name in enumerate(outlets_list):
                    print(f"    {GREEN}[{idx + 1}]{RESET} {o_name}")
                print()
                while True:
                    try:
                        o_choices = input(f"  {BOLD}Pilih nomor outlet Grab (contoh: 1,3 atau 'all'):{RESET} ").strip()
                        if o_choices.lower() == "all":
                            outlet = outlets_list
                            break
                        else:
                            indices = [int(x.strip()) for x in o_choices.split(",") if x.strip()]
                            if all(1 <= i <= len(outlets_list) for i in indices):
                                outlet = [outlets_list[i - 1] for i in indices]
                                break
                    except ValueError: pass
                    print(f"  {RED}Pilihan tidak valid.{RESET}")
            else:
                print(f"  {RED}[WARNING] Tidak ditemukan outlet Grab VB di Google Sheets.{RESET}")

        # Shopee Filters
        if platform in ("shopee", "all"):
            df_shopee = df_vb[df_vb["Aplikasi"].str.contains("Shopee", na=False, case=False)] if not df_vb.empty else pd.DataFrame()
            if not df_shopee.empty:
                merchants = sorted(df_shopee["Merchant Name"].dropna().unique())
                print(f"\n  {BOLD}Pilih Merchant ShopeeFood VB:{RESET}")
                for idx, m_name in enumerate(merchants):
                    print(f"    {GREEN}[{idx + 1}]{RESET} {m_name}")
                print()
                while True:
                    try:
                        m_choices = input(f"  {BOLD}Pilih nomor merchant Shopee (contoh: 1,2 atau 'all'):{RESET} ").strip()
                        if m_choices.lower() == "all":
                            shopee_merchant = merchants
                            break
                        else:
                            indices = [int(x.strip()) for x in m_choices.split(",") if x.strip()]
                            if all(1 <= i <= len(merchants) for i in indices):
                                shopee_merchant = [merchants[i - 1] for i in indices]
                                break
                    except ValueError: pass
                    print(f"  {RED}Pilihan tidak valid.{RESET}")
            else:
                print(f"  {RED}[WARNING] Tidak ditemukan merchant Shopee VB di Google Sheets.{RESET}")

    # Date Range Selection
    print()
    today = datetime.now()
    days_since_monday = today.weekday()
    recent_monday = today - timedelta(days=days_since_monday)
    previous_monday = recent_monday - timedelta(days=7)
    
    default_end = recent_monday.strftime("%Y-%m-%d")
    default_start = previous_monday.strftime("%Y-%m-%d")
    
    while True:
        date_choice = input(f"  {BOLD}Gunakan tanggal 7 hari terakhir (Senin-Senin: {default_start} s/d {default_end})? (y/n):{RESET} ").strip().lower()
        if date_choice in ("y", "yes", "n", "no"):
            break
        print(f"  {RED}Input tidak valid. Masukkan y atau n.{RESET}")

    if date_choice in ("y", "yes"):
        start_date = default_start
        end_date = default_end
    else:
        print()
        start_input = input(f"  {BOLD}Start date (YYYY-MM-DD){RESET} [{default_start}]: ").strip()
        end_input   = input(f"  {BOLD}End date   (YYYY-MM-DD){RESET} [{default_end}]: ").strip()
        start_date = start_input or default_start
        end_date   = end_input or default_end

    # Confirm
    print(f"\n  {CYAN}{'─'*50}{RESET}")
    print(f"  Platform : {BOLD}{platform}{RESET}")
    if scope_choice == "2":
        if outlet: print(f"  Grab Outlet : {BOLD}{outlet}{RESET}")
        if shopee_merchant: print(f"  Shopee Merchant : {BOLD}{shopee_merchant}{RESET}")
    else:
        print(f"  Outlet   : {BOLD}Semua Outlet VB{RESET}")
    print(f"  Start    : {BOLD}{start_date}{RESET}")
    print(f"  End      : {BOLD}{end_date}{RESET}")
    print(f"  {CYAN}{'─'*50}{RESET}")

    confirm = input(f"\n  {BOLD}Lanjutkan? (Y/n):{RESET} ").strip().lower()
    if confirm in ("n", "no"):
        sys.exit(0)

    return platform, start_date, end_date, outlet, branch, shopee_merchant

def main():
    parser = argparse.ArgumentParser(description="VB Report — Unified Baseline Transaction Pipeline")
    parser.add_argument("platform", nargs="?", default=None, help="Platform: grab, shopee, all")
    parser.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--user", type=str, default=None, help="Filter specific username (Grab only)")
    parser.add_argument("--outlet", type=str, default=None, help="Filter specific outlet name")
    parser.add_argument("--branch", type=str, default=None, help="Filter specific branch name")
    args = parser.parse_args()

    load_dotenv()

    if args.platform is None or args.start is None or args.end is None:
        platform, start_date, end_date, outlet, branch, shopee_merchant = interactive_mode()
    else:
        platform = args.platform.lower()
        start_date = args.start
        end_date = args.end
        outlet = [args.outlet] if args.outlet else []
        branch = [args.branch] if args.branch else []
        shopee_merchant = [args.outlet] if args.outlet else []
        banner()

    start_date = normalize_date_string(start_date)
    end_date = normalize_date_string(end_date)

    results = {}
    start_time = datetime.now()

    if platform in ("grab", "all"):
        o_str = "|".join(outlet) if outlet else None
        b_str = "|".join(branch) if branch else None
        results["Grab_VB"] = run_grab_vb(start_date, end_date, user_filter=args.user, outlet_filter=o_str, branch_filter=b_str)

    if platform in ("shopee", "all"):
        m_str = "|".join(shopee_merchant) if shopee_merchant else None
        results["Shopee_VB"] = run_shopee_vb(start_date, end_date, merchant_filter=m_str)

    elapsed = datetime.now() - start_time
    print(f"\n{CYAN}{BOLD}  SUMMARY{RESET}")
    print(f"  Duration: {int(elapsed.total_seconds() // 60)}m {int(elapsed.total_seconds() % 60)}s")
    for name, success in results.items():
        status = f"{GREEN}✓ SUCCESS{RESET}" if success else f"{RED}✗ FAILED{RESET}"
        print(f"  {name:10s} : {status}")

if __name__ == "__main__":
    main()
