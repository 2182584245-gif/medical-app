@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHONPATH="
if not exist ".venv-server\Scripts\python.exe" (
    echo Server Python environment was not found. Nothing was changed.
    pause
    exit /b 2
)
echo Local Supabase setup. This does not upload app data or create cloud tables.
echo Paste connection details only into the hidden local prompts, not into chat.
".venv-server\Scripts\python.exe" -m server.cloud_connection configure
if errorlevel 1 (
    echo Configuration was not completed. Existing configuration was not overwritten.
    pause
    exit /b 2
)
echo Running a read-only database connection check...
".venv-server\Scripts\python.exe" -m server.cloud_connection check
if errorlevel 1 (
    echo Connection check did not pass. No cloud tables or records were changed.
    pause
    exit /b 3
)
echo Read-only check passed. Desktop synchronization is still disabled.
pause
endlocal
