@echo off
REM Restart the scalper backend: stop EVERY process listening on port 8000,
REM then start exactly ONE backend on http://127.0.0.1:8000 (paper trading).
REM Run it from the terminal/env you normally use for the backend (same python).
cd /d "%~dp0"
echo Stopping any backend on port 8000...
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do (set FOUND=1 & echo   killing PID %%p & taskkill /PID %%p /T /F)
if "%FOUND%"=="0" echo   nothing was listening on port 8000
timeout /t 3 >nul
netstat -ano | findstr /r /c:":8000 .*LISTENING" >/dev/null && (echo. & echo ERROR: port 8000 is still in use - the old backend could not be stopped. & echo Close it in the window where it runs ^(Ctrl+C^), or run this file as administrator. & pause & exit /b 1)
where python >/dev/null 2>/dev/null || (echo. & echo ERROR: python not found. Run this file from the terminal where you normally start the backend. & pause & exit /b 1)
echo Starting ONE backend on http://127.0.0.1:8000 ...
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo Backend stopped (exit code %ERRORLEVEL%).
pause
