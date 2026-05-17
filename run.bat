@echo off
REM Google Maps Lead Scraper — Windows launcher
cd /d "%~dp0"
echo Starting Google Maps Lead Scraper at http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
