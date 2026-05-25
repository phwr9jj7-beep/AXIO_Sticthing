@echo off
echo ===================================================
echo   AXIO Stitching Studio Launcher
echo ===================================================
cd /d "%~dp0"

set VENV_DIR=.venv

if not exist %VENV_DIR% (
    echo Local virtual environment not found. Creating one...
    py -3 -m venv %VENV_DIR% || python -m venv %VENV_DIR%
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment. Ensure Python 3 is installed.
        pause
        exit /b 1
    )
)

echo Verifying dependencies inside local environment...
%VENV_DIR%\Scripts\python.exe -m pip install PySide6 numpy scipy tifffile scikit-image opencv-python networkx pillow basicpy torch-dct --quiet

echo Launching AXIO Stitching Studio GUI...
%VENV_DIR%\Scripts\python.exe scripts\gui_stitch.py

if errorlevel 1 (
    echo [ERROR] AXIO Stitching Studio crashed or failed to start.
    pause
)
