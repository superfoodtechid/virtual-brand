#!/bin/bash
set -e

# Change directory to script's directory
cd "$(dirname "$0")"

echo "=================================================="
echo "[SETUP] Mempersiapkan lingkungan Python (UV)..."
echo "=================================================="

if ! command -v uv &> /dev/null; then
    echo "[INFO] 'uv' tidak ditemukan. Mencoba menginstal 'uv'..."
    if command -v pip3 &> /dev/null; then
        pip3 install uv
    elif command -v pip &> /dev/null; then
        pip install uv
    elif command -v python3 &> /dev/null; then
        python3 -m pip install uv
    else
        echo "[INFO] Menginstal 'uv' via standalone installer..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
fi

if ! command -v uv &> /dev/null; then
    echo "[ERROR] Gagal menemukan atau menginstal 'uv'. Harap instal 'uv' secara manual."
    exit 1
fi

echo "[INFO] Sinkronisasi dependensi virtual environment menggunakan uv sync..."
uv sync

echo "[INFO] Mengunduh browser Chromium untuk Playwright..."
uv run python -m playwright install chromium

echo "[INFO] Menjalankan aplikasi menggunakan uv run..."
uv run python cli.py
