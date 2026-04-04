@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Starting CPBL Dashboard...
python server.py
pause
