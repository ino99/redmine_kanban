@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    where py >nul 2>nul
    if not errorlevel 1 (
        echo Creating virtual environment in .venv ...
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python was not found. Install Python 3.10 or later, then run this script again.
            exit /b 1
        )
        echo Creating virtual environment in .venv ...
        python -m venv .venv
    )
)

set "HAS_REQUIREMENTS="
for /f "usebackq tokens=* eol=#" %%L in ("requirements.txt") do (
    if not "%%L"=="" set "HAS_REQUIREMENTS=1"
)

if defined HAS_REQUIREMENTS (
    echo Installing Python requirements ...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 exit /b %ERRORLEVEL%
) else (
    echo No external Python requirements to install.
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example. Edit .env before connecting to Redmine.
)

if "%~1"=="" (
    set "APP_ARGS=--serve --port 8015"
) else (
    set "APP_ARGS=%*"
)

echo.
echo Starting Redmine Kanban with Windows Python.
echo Open http://127.0.0.1:8015/kanban.html in your browser.
echo Press Ctrl+C here to stop the server.
echo.

"%PYTHON_EXE%" redmine_issues.py %APP_ARGS%
