@echo off
echo ============================================
echo   GPS Tracker - Setup
echo ============================================
echo.
echo [1/3] Installing Python dependencies...
pip install -r requirements.txt
echo.
echo [2/3] Checking for cloudflared...
where cloudflared >nul 2>&1
if %errorlevel% neq 0 (
    echo cloudflared not found. Downloading...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe' -UseBasicParsing"
    echo Downloaded cloudflared.exe to current directory.
) else (
    echo cloudflared found!
)
echo.
echo [3/3] Creating output directory...
if not exist output mkdir output
echo.
echo ============================================
echo   Setup complete!
echo   Run: python app.py
echo ============================================
pause
