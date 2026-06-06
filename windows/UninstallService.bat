@echo off
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo Please right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)

VirtualOnvifCamera.exe --uninstall-service

echo.
echo Virtual ONVIF Camera service is uninstalled.
pause
