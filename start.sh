[ignoring loop detection]
#!/bin/bash
set -e

echo "=================================================="
echo "[SETUP] Mempersiapkan lingkungan Python..."
echo "=================================================="

if command -v uv &> /dev/null; then
    echo "[INFO] Ditemukan tool 'uv'. Menggunakan uv untuk instalasi cepat..."
    if [ ! -d ".venv" ]; then
        echo "[INFO] Membuat virtual environment (.venv) dengan uv..."
        uv venv
    fi
    echo "[INFO] Menginstal dependensi..."
    uv pip install -e .
    echo "[INFO] Mengunduh browser Chromium untuk Playwright..."
    uv run playwright install chromium
    echo "[INFO] Menjalankan aplikasi..."
    uv run python cli.py
else
    echo "[INFO] 'uv' tidak ditemukan. Menggunakan Python standar..."
    if ! command -v python3 &> /dev/null; then
        echo "[ERROR] Python3 tidak ditemukan di sistem. Harap instal Python3 terlebih dahulu!"
        exit 1
    fi
    if [ ! -d ".venv" ]; then
        echo "[INFO] Membuat virtual environment (.venv)..."
        python3 -m venv .venv
    fi
    echo "[INFO] Upgrade pip..."
    .venv/bin/python -m pip install --upgrade pip
    echo "[INFO] Menginstal dependensi..."
    .venv/bin/python -m pip install -e .
    echo "[INFO] Mengunduh browser Chromium untuk Playwright..."
    .venv/bin/python -m playwright install chromium
    echo "[INFO] Menjalankan aplikasi..."
    .venv/bin/python cli.py
fi
