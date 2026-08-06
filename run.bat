@echo off
cd /d "%~dp0"
pip install -r backend\requirements.txt
start /min cmd /c "uvicorn backend.main:app --host 127.0.0.1 --port 8000"
timeout /t 2 >nul
start http://127.0.0.1:8000
