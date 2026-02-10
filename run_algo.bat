@echo off
REM Helper script to run Python scripts within the virtual environment on Windows

IF NOT EXIST "venv" (
    echo [ERROR] Virtual environment 'venv' not found.
    echo Please run 'python -m venv venv' and install requirements first.
    pause
    exit /b 1
)

IF "%~1"=="" (
    echo [ERROR] No script specified.
    echo Usage: run_algo.bat script_name.py
    echo Example: run_algo.bat run_strategy.py
    pause
    exit /b 1
)

SET SCRIPT_NAME=%~1

echo [INFO] Activating environment and running %SCRIPT_NAME%...
call venv\Scripts\activate.bat
python %SCRIPT_NAME%
call venv\Scripts\deactivate.bat
echo [INFO] Done.
pause
