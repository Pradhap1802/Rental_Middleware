@echo off
title Installing RentAsst Standalone Middleware Service
echo =======================================================
echo   RentAsst Middleware Windows Service Installer
echo =======================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_service.ps1"
echo.
pause
