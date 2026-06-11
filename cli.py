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
import time
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
    state = "platform"
    
    platform = None
    scope_choice = None
    outlet = []
    branch = []
    shopee_merchant = []
    start_date = None
    end_date = None
    
    df_grab = None
    df_shopee = None
    def load_dfs():
        nonlocal df_grab, df_shopee
        if df_grab is not None and df_shopee is not None:
            return
            
        import pandas as pd
        import requests
        import io
        print(f"\n  {CYAN}[INFO] Mengunduh daftar portal VB terbaru dari Google Sheets...{RESET}")
        URL_GRAB = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYSUnKOqk29LCktTxdb0wPLbWMbRaWRP3eC_UA4AwYod1FW6zDMhtLMC5ghIvot2B8upCDfBsn-TCP/pub?gid=978201567&single=true&output=csv"
        URL_SHOPEE = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYSUnKOqk29LCktTxdb0wPLbWMbRaWRP3eC_UA4AwYod1FW6zDMhtLMC5ghIvot2B8upCDfBsn-TCP/pub?gid=565510790&single=true&output=csv"
        
        try:
            resp_grab = requests.get(URL_GRAB, timeout=30)
            resp_grab.raise_for_status()
            df_g = pd.read_csv(io.StringIO(resp_grab.text))
            if "Notes" in df_g.columns:
                df_g = df_g[~df_g["Notes"].astype(str).str.contains("restricted", na=False, case=False)]
            df_grab = df_g.dropna(subset=["Portal"])
            
            resp_shopee = requests.get(URL_SHOPEE, timeout=30)
            resp_shopee.raise_for_status()
            df_s = pd.read_csv(io.StringIO(resp_shopee.text))
            if "Notes" in df_s.columns:
                df_s = df_s[~df_s["Notes"].astype(str).str.contains("restricted", na=False, case=False)]
            if "Role" in df_s.columns:
                df_shopee = df_s[df_s["Role"].astype(str).str.strip().str.lower() == "owner"].dropna(subset=["Portal"])
            else:
                df_shopee = df_s.dropna(subset=["Portal"])
        except Exception as e:
            print(f"  {RED}[ERROR] Gagal mengunduh Google Sheets: {e}{RESET}")
            sys.exit(1)

    while True:
        if state == "platform":
            os.system('cls' if os.name == 'nt' else 'clear')
            banner()
            print(f"  {BOLD}Pilih platform:{RESET}")
            print(f"    {GREEN}[1]{RESET} Grab")
            print(f"    {MAGENTA}[2]{RESET} Shopee")
            print(f"    {CYAN}[3]{RESET} Kedua Platform (Grab + Shopee)")
            print(f"    {YELLOW}[4]{RESET} Keluar")
            print()
            
            choice = input(f"  {BOLD}Pilihan (1/2/3/4):{RESET} ").strip()
            if choice == "4":
                print("  Keluar.")
                sys.exit(0)
            elif choice in ("1", "2", "3"):
                platform_map = {"1": "grab", "2": "shopee", "3": "all"}
                platform = platform_map[choice]
                state = "scope"
            else:
                print(f"  {RED}Input tidak valid. Masukkan 1, 2, 3, atau 4.{RESET}")
                time.sleep(1)

        elif state == "scope":
            print(f"\n  {BOLD}Pilih cakupan outlet:{RESET}")
            print(f"    {GREEN}[1]{RESET} Pilih semua outlet")
            print(f"    {YELLOW}[2]{RESET} Pilih custom (Filter spesifik){RESET}")
            print(f"    {CYAN}[3]{RESET} Kembali ke pemilihan platform")
            print()
            
            scope_choice = input(f"  {BOLD}Pilihan (1/2/3):{RESET} ").strip()
            if scope_choice == "3":
                state = "platform"
            elif scope_choice == "1":
                outlet = []
                branch = []
                shopee_merchant = []
                state = "date"
            elif scope_choice == "2":
                load_dfs()
                if platform in ("grab", "all"):
                    state = "grab_outlet"
                else:
                    state = "shopee_merchant"
            else:
                print(f"  {RED}Input tidak valid. Masukkan 1, 2, atau 3.{RESET}")

        elif state == "grab_outlet":
            if df_grab is None or df_grab.empty:
                print(f"  {RED}[WARNING] Tidak ditemukan outlet Grab VB di Google Sheets.{RESET}")
                state = "scope"
                continue
                
            outlets_list = sorted(df_grab["Portal"].dropna().unique())
            print(f"\n  {BOLD}Pilih Portal Grab VB:{RESET}")
            for idx, o_name in enumerate(outlets_list):
                print(f"    {GREEN}[{idx + 1}]{RESET} {o_name}")
            print(f"    {CYAN}[b]{RESET} Kembali ke cakupan outlet")
            print()
            
            o_choices = input(f"  {BOLD}Pilih nomor portal Grab (contoh: 1,3 atau 'all' atau 'b'):{RESET} ").strip()
            if o_choices.lower() == "b":
                state = "scope"
            elif o_choices.lower() == "all":
                outlet = outlets_list
                if platform == "all":
                    state = "shopee_merchant"
                else:
                    state = "date"
            else:
                try:
                    indices = [int(x.strip()) for x in o_choices.split(",") if x.strip()]
                    if all(1 <= i <= len(outlets_list) for i in indices):
                        outlet = [outlets_list[i - 1] for i in indices]
                        if platform == "all":
                            state = "shopee_merchant"
                        else:
                            state = "date"
                    else:
                        print(f"  {RED}Pilihan tidak valid.{RESET}")
                except ValueError:
                    print(f"  {RED}Pilihan tidak valid.{RESET}")

        elif state == "shopee_merchant":
            if df_shopee is None or df_shopee.empty:
                print(f"  {RED}[WARNING] Tidak ditemukan portal Shopee VB di Google Sheets.{RESET}")
                if platform == "all":
                    state = "grab_outlet"
                else:
                    state = "scope"
                continue
                
            merchants = sorted(df_shopee["Portal"].dropna().unique())
            print(f"\n  {BOLD}Pilih Portal ShopeeFood VB:{RESET}")
            for idx, m_name in enumerate(merchants):
                print(f"    {GREEN}[{idx + 1}]{RESET} {m_name}")
            print(f"    {CYAN}[b]{RESET} Kembali ke menu sebelumnya")
            print()
            
            m_choices = input(f"  {BOLD}Pilih nomor portal Shopee (contoh: 1,2 atau 'all' atau 'b'):{RESET} ").strip()
            if m_choices.lower() == "b":
                if platform == "all":
                    state = "grab_outlet"
                else:
                    state = "scope"
            elif m_choices.lower() == "all":
                shopee_merchant = merchants
                state = "date"
            else:
                try:
                    indices = [int(x.strip()) for x in m_choices.split(",") if x.strip()]
                    if all(1 <= i <= len(merchants) for i in indices):
                        shopee_merchant = [merchants[i - 1] for i in indices]
                        state = "date"
                    else:
                        print(f"  {RED}Pilihan tidak valid.{RESET}")
                except ValueError:
                    print(f"  {RED}Pilihan tidak valid.{RESET}")

        elif state == "date":
            print()
            today = datetime.now()
            days_since_monday = today.weekday()
            recent_monday = today - timedelta(days=days_since_monday)
            previous_monday = recent_monday - timedelta(days=7)
            
            default_end = recent_monday.strftime("%Y-%m-%d")
            default_start = previous_monday.strftime("%Y-%m-%d")
            
            print(f"  {BOLD}Pilih rentang tanggal:{RESET}")
            print(f"    {GREEN}[1]{RESET} Gunakan 7 hari terakhir (Senin-Senin: {default_start} s/d {default_end})")
            print(f"    {YELLOW}[2]{RESET} Input manual")
            print(f"    {CYAN}[3]{RESET} Kembali ke menu sebelumnya")
            print()
            
            date_choice = input(f"  {BOLD}Pilihan (1/2/3):{RESET} ").strip()
            if date_choice == "3":
                if scope_choice == "1":
                    state = "scope"
                else:
                    if platform == "shopee":
                        state = "shopee_merchant"
                    elif platform == "grab":
                        state = "grab_outlet"
                    else: # all
                        state = "shopee_merchant"
            elif date_choice == "1":
                start_date = default_start
                end_date = default_end
                state = "confirm"
            elif date_choice == "2":
                print()
                start_input = input(f"  {BOLD}Start date (YYYY-MM-DD){RESET} [{default_start}] (atau ketik 'b' untuk kembali): ").strip()
                if start_input.lower() == 'b':
                    continue
                end_input   = input(f"  {BOLD}End date   (YYYY-MM-DD){RESET} [{default_end}] (atau ketik 'b' untuk kembali): ").strip()
                if end_input.lower() == 'b':
                    continue
                start_date = start_input or default_start
                end_date   = end_input or default_end
                state = "confirm"
            else:
                print(f"  {RED}Input tidak valid.{RESET}")

        elif state == "confirm":
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
            
            print(f"  {BOLD}Konfirmasi tindakan:{RESET}")
            print(f"    {GREEN}[1]{RESET} Lanjutkan")
            print(f"    {YELLOW}[2]{RESET} Kembali ke pemilihan tanggal")
            print(f"    {RED}[3]{RESET} Batal dan Keluar")
            print()
            
            confirm = input(f"  {BOLD}Pilihan (1/2/3):{RESET} ").strip()
            if confirm == "1":
                break
            elif confirm == "2":
                state = "date"
            elif confirm == "3":
                print("  Dibatalkan.")
                sys.exit(0)
            else:
                print(f"  {RED}Pilihan tidak valid.{RESET}")

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
