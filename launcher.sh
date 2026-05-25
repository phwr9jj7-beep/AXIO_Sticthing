#!/bin/bash
echo "==================================================="
echo "  AXIO Stitching Studio Launcher"
echo "==================================================="
cd "$(dirname "$0")"

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Local virtual environment not found. Creating one..."
    python3 -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to create virtual environment. Ensure python3-venv is installed."
        read -p "Press enter to exit..."
        exit 1
    fi
fi

echo "Verifying dependencies inside local environment..."
"$VENV_DIR/bin/pip" install PySide6 numpy scipy tifffile scikit-image opencv-python networkx pillow basicpy torch-dct --quiet

echo "Launching AXIO Stitching Studio GUI..."
"$VENV_DIR/bin/python" scripts/gui_stitch.py

if [ $? -ne 0 ]; then
    echo "[ERROR] AXIO Stitching Studio crashed or failed to start."
    read -p "Press enter to exit..."
fi
