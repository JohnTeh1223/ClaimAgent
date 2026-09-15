@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set PYTHONPATH=%~dp0code
python "%~dp0code\gui.py"
pause
