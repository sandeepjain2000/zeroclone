@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Full pipeline via run_cycle.py (extract -> validate -> DB + CVL views)
REM Email format is chosen automatically (CVL cascade order).
REM Usage:
REM   run_cycle.bat
REM   run_cycle.bat 500

set "LIMIT=500"
if not "%~1"=="" set "LIMIT=%~1"

set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

echo.
echo ========================================
echo  Email cycle  (2-probe per company, limit %LIMIT%)
echo  extract -^> validate -^> DB + views
echo ========================================
echo.

%PY% run_cycle.py --limit %LIMIT%
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo ========================================
  echo  Cycle finished successfully.
  echo ========================================
) else if "%RC%"=="3" (
  echo ========================================
  echo  Validation partial — run run_cycle_resume.bat if needed.
  echo ========================================
) else (
  echo ========================================
  echo  Cycle finished with exit code %RC%. Check logs above.
  echo ========================================
)
echo.
pause
exit /b %RC%
