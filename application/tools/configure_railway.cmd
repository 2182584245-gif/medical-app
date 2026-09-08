@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHONPATH="
set "PYTHONHOME="
if not exist ".venv-server\Scripts\python.exe" (
    echo Server Python environment was not found. Nothing was changed.
    pause
    exit /b 2
)
echo Railway project token setup for medical-app, production only.
echo Paste the PROJECT token only into the hidden local prompt, never into chat.
echo This saves current-user encrypted configuration; it does not deploy or upload app data.
".venv-server\Scripts\python.exe" -m server.railway_connection configure
if errorlevel 1 (
    echo Configuration was not completed. Existing configuration was not overwritten.
    echo To deliberately replace a token, use the documented configure --replace command.
    pause
    exit /b 2
)
echo Running the read-only project and environment authorization check...
".venv-server\Scripts\python.exe" -m server.railway_connection check
if errorlevel 1 (
    echo Authorization check did not pass. No deployment or cloud data changes were requested.
    pause
    exit /b 3
)
echo Authorization check passed. Deployment is still a separate step.
pause
endlocal
