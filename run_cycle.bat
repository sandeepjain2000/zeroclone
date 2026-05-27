@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Full pipeline: extract -> validate -> update DB -> refresh CVL SQLite views
REM Email format is chosen automatically (CVL cascade order).
REM Usage:
REM   run_cycle.bat
REM   run_cycle.bat 500

set "LIMIT=500"
if not "%~1"=="" set "LIMIT=%~1"

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

set "APPLY_VIEWS=%~dp0..\CVL\scripts\apply_validation_views.py"

echo.
echo ========================================
echo  Email cycle  (2-probe per company, limit %LIMIT%)
echo ========================================
echo.

echo [1/4] Extract...
%PY% extract_cycle.py --limit %LIMIT%
set "EXT_ERR=%ERRORLEVEL%"
if "%EXT_ERR%"=="2" (
  echo Pending validation exists - continuing to validate step.
) else if not "%EXT_ERR%"=="0" goto :fail

echo.
echo [2/4] Validate (Apify)...
%PY% validate_cycle.py
set "VALID_ERR=%ERRORLEVEL%"
if "%VALID_ERR%"=="2" goto :fail
if "%VALID_ERR%"=="3" goto :partial

echo.
echo [3/4] Update database...
%PY% update_db_cycle.py
if errorlevel 1 goto :fail

echo.
echo [4/4] Refresh validation views (CVL SQLite)...
%PY% "%APPLY_VIEWS%"
if errorlevel 1 goto :fail

echo.
echo ========================================
echo  Cycle finished successfully.
echo ========================================
echo.
pause
exit /b 0

:partial
echo.
echo ========================================
echo  Validation stopped early (partial / quota).
echo  Run:  run_cycle_resume.bat
echo ========================================
echo.
pause
exit /b 3

:fail
echo.
echo ========================================
echo  Cycle failed. Check logs\ and output above.
echo ========================================
echo.
pause
exit /b 1
