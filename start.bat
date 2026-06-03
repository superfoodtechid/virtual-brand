[ignoring loop detection]
@echo off
setlocal enabledelayedexpansion

echo ==================================================
echo [SETUP] Mempersiapkan lingkungan Python...
echo ==================================================

where uv >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo [INFO] Ditemukan tool 'uv'. Menggunakan uv untuk instalasi cepat...
    if not exist ".venv" (
        echo [INFO] Membuat virtual environment (.venv) dengan uv...
        uv venv
    )
    echo [INFO] Menginstal dependensi dari pyproject.toml...
    uv pip install -e .
    echo [INFO] Mengunduh browser Chromium untuk Playwright...
    uv run playwright install chromium
    echo [INFO] Menjalankan aplikasi...
    uv run python cli.py
) else (
    echo [INFO] 'uv' tidak ditemukan. Menggunakan Python standar...
    where python >nul 2>nul
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Python tidak ditemukan di sistem. Harap instal Python terlebih dahulu!
        pause
        exit /b 1
    )
    if not exist ".venv" (
        echo [INFO] Membuat virtual environment (.venv) dengan Python standar...
        python -m venv .venv
    )
    echo [INFO] Upgrade pip...
    .venv\Scripts\python.exe -m pip install --upgrade pip
    echo [INFO] Menginstal dependensi dari pyproject.toml...
    .venv\Scripts\python.exe -m pip install -e .
    echo [INFO] Mengunduh browser Chromium untuk Playwright...
    .venv\Scripts\python.exe -m playwright install chromium
    echo [INFO] Menjalankan aplikasi...
    .venv\Scripts\python.exe cli.py
)

pause
