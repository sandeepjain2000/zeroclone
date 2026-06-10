@echo off

setlocal EnableExtensions

chcp 65001 >nul

cd /d "%~dp0"



REM Resume partial validation, then update DB, then refresh CVL SQLite views



set "PY=python"

where py >nul 2>&1 && set "PY=py -3"



echo.

echo ========================================

echo  Resume validation + update DB

echo ========================================

echo.



echo [1/2] Validate (resume same batch — no duplicate Apify run)...

%PY% validate_cycle.py --resume

set "VALID_ERR=%ERRORLEVEL%"

if "%VALID_ERR%"=="2" goto :fail



echo.

echo [2/2] Update database + refresh CVL validation views...

%PY% update_db_cycle.py

set "DB_ERR=%ERRORLEVEL%"

if "%DB_ERR%"=="0" goto :done

if "%VALID_ERR%"=="3" (

  echo.

  echo Validation still partial and DB not ready — re-run after Apify quota resets.

  pause

  exit /b 3

)

goto :fail



:done

echo.

echo Done.

pause

exit /b 0



:fail

echo Failed.

pause

exit /b 1


