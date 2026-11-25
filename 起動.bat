@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    set "PYCMD=py -3"
    set "PYWCMD=pyw -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 goto missing
    set "PYCMD=python"
    set "PYWCMD=pythonw"
)
%PYCMD% -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto missing
rem First run only: install PySide6 / numpy into the vendor folder.
%PYCMD% -c "import sys;sys.path.insert(0,'vendor');import PySide6.QtMultimedia, numpy" >nul 2>nul
if errorlevel 1 (
    echo First-time setup: installing PySide6 and numpy. Internet is required...
    %PYCMD% -m pip install --disable-pip-version-check --target vendor "PySide6-Addons>=6.6" numpy
)
start "" %PYWCMD% launcher.py %*
exit /b 0
:missing
echo Python 3.10 or newer is required: https://www.python.org/downloads/windows/
pause
exit /b 1