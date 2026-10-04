@echo off
rem ---------------------------------------------------------------------------
rem  Build a single standalone Windows executable of Duplver.
rem
rem    build_windows.bat          -> dist\Duplver.exe       (with AI similarity)
rem    build_windows.bat lite     -> dist\Duplver-lite.exe  (no AI, small and quick to start)
rem
rem  Needs Python 3.10-3.12 (python.org installer) and internet for the first
rem  build. Everything is installed into a private .build-venv-* folder; your
rem  system Python is not touched. Set PYTHON=... to choose an interpreter.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"

set "VARIANT=%~1"
if "%VARIANT%"=="" set "VARIANT=full"
if /i not "%VARIANT%"=="full" if /i not "%VARIANT%"=="lite" (
    echo Usage: build_windows.bat [full^|lite]
    exit /b 2
)

set "PY=%PYTHON%"
if not defined PY for %%V in (3.12 3.11 3.10) do (
    if not defined PY py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
)
if not defined PY (
    echo Python 3.10-3.12 was not found. Install it from https://www.python.org/downloads/
    exit /b 1
)
echo Using %PY%

set "VENV=.build-venv-%VARIANT%"
if not exist "%VENV%\Scripts\python.exe" (
    %PY% -m venv "%VENV%" || goto :fail
)
set "VPY=%VENV%\Scripts\python.exe"

"%VPY%" -m pip install --upgrade pip || goto :fail
"%VPY%" -m pip install -r packaging\requirements-%VARIANT%.txt || goto :fail
"%VPY%" -m pip install --no-deps -e . || goto :fail

if /i "%VARIANT%"=="full" (
    "%VPY%" packaging\fetch_clip_weights.py || goto :fail
)

echo Running tests...
"%VPY%" -m unittest discover -s tests || goto :fail

set "DUPLVER_VARIANT=%VARIANT%"
"%VPY%" -m PyInstaller --noconfirm --clean --distpath dist --workpath build\%VARIANT% packaging\duplver.spec || goto :fail

set "EXE=dist\Duplver.exe"
if /i "%VARIANT%"=="lite" set "EXE=dist\Duplver-lite.exe"
echo.
echo Checking the executable...
"%EXE%" doctor || goto :fail
echo.
echo Done: %EXE%
echo Copy that single file anywhere and run it (double-click for guided mode).
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
exit /b 1
