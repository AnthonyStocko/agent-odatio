@echo off
cd /d "%~dp0"
start "" /b cmd /c "timeout /t 5 >nul & start http://localhost:8502"
venv\Scripts\python.exe -m streamlit run app.py
