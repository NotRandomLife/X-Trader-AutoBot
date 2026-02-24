@echo off
cd /d "%~dp0"
echo [X-Trader AutoBot] Avvio con console...
python app.pyw
echo.
echo Se la GUI non parte, controlla XTraderAutoBot_fatal.log
pause
