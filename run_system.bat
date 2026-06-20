@echo off
title BMD Quant Platform Windows Control Panel
color 0B

set "LOCAL_PYTHON=D:\Miniconda3\envs\poc_quant\python.exe"
set "LOCAL_PIP=D:\Miniconda3\envs\poc_quant\Scripts\pip.exe"

if exist "%LOCAL_PYTHON%" (
    set "PYTHON_ENV_EXE=%LOCAL_PYTHON%"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        set "PYTHON_ENV_EXE="
    ) else (
        set "PYTHON_ENV_EXE=python"
    )
)

if exist "%LOCAL_PIP%" (
    set "PIP_EXE=%LOCAL_PIP%"
) else (
    where pip >nul 2>nul
    if errorlevel 1 (
        set "PIP_EXE="
    ) else (
        set "PIP_EXE=pip"
    )
)

:MENU
cls
echo =====================================================================
echo      BMD Quant Platform - Windows Control Panel
echo =====================================================================
echo.
echo     [1] Run Quant Orchestrator (main.py)
echo     [2] Run Automated Unit Tests (26 tests)
echo     [3] Install Package Requirements (pip)
echo     [4] Exit
echo.
echo =====================================================================
set "choice="
set /p choice="Enter option [1-4] (Default is 1): "

if "%choice%"=="" set choice=1
if "%choice%"=="1" goto RUN_SYSTEM
if "%choice%"=="2" goto RUN_TESTS
if "%choice%"=="3" goto INSTALL_REQ
if "%choice%"=="4" goto EXIT_PROG

echo Invalid choice! Please select 1, 2, 3, or 4.
pause
goto MENU

:RUN_SYSTEM
cls
echo =====================================================================
echo Starting Quant System (main.py)...
echo =====================================================================
echo.
if "%PYTHON_ENV_EXE%"=="" goto ERR_NO_PYTHON
"%PYTHON_ENV_EXE%" main.py
echo.
echo =====================================================================
echo System execution terminated.
echo =====================================================================
pause
goto MENU

:RUN_TESTS
cls
echo =====================================================================
echo Running 26 Automated Tests...
echo =====================================================================
echo.
if "%PYTHON_ENV_EXE%"=="" goto ERR_NO_PYTHON
"%PYTHON_ENV_EXE%" -m unittest discover -s Tests -p "test_*.py"
echo.
echo =====================================================================
echo Unit tests completed.
echo =====================================================================
pause
goto MENU

:INSTALL_REQ
cls
echo =====================================================================
echo Checking and Installing packages...
echo =====================================================================
echo.
if "%PYTHON_ENV_EXE%"=="" goto ERR_NO_PYTHON
if "%PIP_EXE%"=="" goto RUN_PIP_FALLBACK
"%PIP_EXE%" install -r requirements.txt
goto END_INSTALL

:RUN_PIP_FALLBACK
echo pip.exe not found at Scripts folder or PATH. Trying python -m pip...
"%PYTHON_ENV_EXE%" -m pip install -r requirements.txt

:END_INSTALL
echo.
echo =====================================================================
echo Requirements check completed.
echo =====================================================================
pause
goto MENU

:ERR_NO_PYTHON
color 0C
echo.
echo ERROR: Python environment interpreter was not found at local conda env
echo path: %LOCAL_PYTHON% or global system PATH.
echo.
echo Please ensure that conda environment 'poc_quant' is created or Python is in PATH.
echo.
pause
color 0B
goto MENU

:EXIT_PROG
cls
echo =====================================================================
echo Thank you for using BMD Quant Platform. Good trading!
echo =====================================================================
timeout /t 3 >nul
exit