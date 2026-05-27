@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Resume partial validation, then update DB, then refresh CVL SQLite views

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

set "APPLY_VIEWS=%~dp0..\CVL\scripts\apply_validation_views.py"

echo.
echo ========================================
echo  Resume validation + update DB
echo ========================================
echo.

echo [1/3] Validate (resume)...
%PY% validate_cycle.py --resume
set "VALID_ERR=%ERRORLEVEL%"
if "%VALID_ERR%"=="2" goto :fail
if "%VALID_ERR%"=="3" (
  echo Still partial — run this batch again after quota resets.
  pause
  exit /b 3
)

echo.
echo [2/3] Update database...
%PY% update_db_cycle.py
if errorlevel 1 goto :fail

echo.
echo [3/3] Refresh validation views (CVL SQLite)...
%PY% "%APPLY_VIEWS%"
if errorlevel 1 goto :fail

echo.
echo Done.
pause
exit /b 0

:fail
echo Failed.
pause
exit /b 1
