@echo off
rem Tips Bot - spusti panel a otevre ho v prohlizeci (dvojklik).
cd /d "%~dp0"
if not exist ".venv\Scripts\tipsbot.exe" (
  echo Chybi instalace bota ^(.venv^). Nainstaluj ho podle README.
  pause
  exit /b 1
)
netstat -ano | find ":8000" | find "LISTENING" >nul
if not errorlevel 1 (
  echo Panel uz bezi, oteviram prohlizec.
  start "" http://127.0.0.1:8000
  exit /b 0
)
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:8000"
echo Panel bezi na http://127.0.0.1:8000 - toto okno nezavirej, zavrenim se panel vypne.
".venv\Scripts\tipsbot.exe" web
pause
