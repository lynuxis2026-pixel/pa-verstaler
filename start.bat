@echo off
cd /d "%~dp0"

echo ====================================
echo   PA Verstaler  -  Theologisch vertaler
echo ====================================
echo.

REM Controleer of streamlit beschikbaar is
where streamlit >nul 2>&1
if %errorlevel% neq 0 (
    echo Streamlit niet gevonden. Installeren...
    pip install -r requirements.txt
    echo.
)

echo App starten op http://localhost:8501
echo Sluit dit venster om de app te stoppen.
echo.

streamlit run app.py --server.headless false --browser.gatherUsageStats false
