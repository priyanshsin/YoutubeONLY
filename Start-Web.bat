@echo off
setlocal
cd /d "%~dp0"
MusicalBUNKER-Web.exe
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Startup failed. Check the error above and README.md.
pause
exit /b 1
