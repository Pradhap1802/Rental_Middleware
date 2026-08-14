@echo off
title Uninstalling RentAsst Standalone Middleware Service
echo =======================================================
echo   RentAsst Middleware Windows Service Uninstaller
echo =======================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall_service.ps1"
echo.
pause
