@echo off
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo Please right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)

VirtualOnvifCamera.exe --install-service
VirtualOnvifCamera.exe --start-service

echo.
echo Virtual ONVIF Camera service is installed and started.
echo Open http://localhost:8000/ to check the status page.
pause
