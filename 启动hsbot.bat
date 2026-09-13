@echo off
rem ============================================================
rem  hsbot one-click launcher  (put NOTHING here, just double-click)
rem  - runs from this folder so config.yaml / data/ are found
rem  - console stays open: watcher logs + tracebacks show here
rem ============================================================
cd /d "%~dp0"
chcp 65001 >nul
title hsbot - Hearthstone monitor
python -m hsbot %*
echo.
echo [hsbot exited]
pause
