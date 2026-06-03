[ignoring loop detection]
@echo off
setlocal enabledelayedexpansion

echo ==================================================
echo [SETUP] Mempersiapkan lingkungan Python (UV)...
echo ==================================================

where uv >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [INFO] 'uv' tidak ditemukan. Mencoba menginstal 'uv' melalui pip...
    where pip >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        pip install uv
    ) else (
        where python >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            python -m pip install uv
        ) else (
            echo [INFO] Mencoba menginstal 'uv' via PowerShell...
            powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
            set "PATH=%USERPROFILE%\.local\bin;%PATH%"
        )
    )
)

where uv >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Gagal menemukan atau menginstal 'uv'. Harap instal 'uv' secara manual: https://github.com/astral-sh/uv
    pause
    exit /b 1
)

echo [INFO] Sinkronisasi dependensi virtual environment menggunakan uv sync...
uv sync

echo [INFO] Mengunduh browser Chromium untuk Playwright...
uv run playwright install chromium

echo [INFO] Menjalankan aplikasi menggunakan uv run...
uv run python cli.py

pause
