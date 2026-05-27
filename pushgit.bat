@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo =========================
echo Git Status
echo =========================
git status
if errorlevel 1 goto :fail

echo.
set /p msg="Enter commit message: "
if "%msg%"=="" set "msg=update project files"

git add .
if errorlevel 1 goto :fail

git commit -m "%msg%"
if errorlevel 1 (
  echo.
  echo No new commit created ^(nothing to commit or commit failed^).
)

echo.
echo =========================
echo Push Options
echo =========================
echo 1. Normal push
echo 2. Force-with-lease push (history rewrite)
set /p pushMode="Choose 1 or 2 [1]: "
if "%pushMode%"=="" set "pushMode=1"

if "%pushMode%"=="2" (
  git push -u origin main --force-with-lease
) else (
  git push -u origin main
)
if errorlevel 1 goto :fail

echo.
echo =========================
echo Push Complete
echo =========================
pause
exit /b 0

:fail
echo.
echo =========================
echo Operation failed.
echo =========================
pause
exit /b 1
