@echo off
setlocal
cd /d "%~dp0"
VirtualOnvifCamera.exe --start-service
pause
