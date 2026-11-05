@echo off
chcp 65001 >nul
echo Smart Flight Finder baslatiliyor: http://localhost:8000
start "" "http://localhost:8000"
"C:\Users\canerm\AppData\Local\Python\pythoncore-3.14-64\python.exe" "%~dp0server.py"
pause
