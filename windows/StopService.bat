@echo off
setlocal
cd /d "%~dp0"
VirtualOnvifCamera.exe --stop-service
pause
