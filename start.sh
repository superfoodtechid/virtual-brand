#!/bin/bash
# Find Python executable
if [ -f ".venv/bin/python" ]; then
    python_bin=".venv/bin/python"
elif [ -f "../src/.venv/bin/python" ]; then
    python_bin="../src/.venv/bin/python"
else
    python_bin="python3"
fi
$python_bin cli.py
