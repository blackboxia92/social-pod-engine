@echo off
setlocal
cd /d "%~dp0"

echo.
echo Social Pod iniciando...
call :detect_python
if errorlevel 1 goto :python_missing

echo Python: OK
"%SOCIAL_POD_PYTHON%" -c "import sqlalchemy, camoufox_pm, social_pod_engine" >nul 2>&1
if errorlevel 1 goto :dependencies_missing

"%SOCIAL_POD_PYTHON%" -m social_pod_engine.tools.windows_launcher
set "SOCIAL_POD_EXIT=%ERRORLEVEL%"
if not "%SOCIAL_POD_EXIT%"=="0" (
  echo.
  echo Social Pod no pudo iniciarse. Revisá el mensaje anterior.
)
goto :end

:detect_python
where py >nul 2>&1
if not errorlevel 1 (
  py -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set "SOCIAL_POD_PYTHON=py"
    exit /b 0
  )
)

for /f "delims=" %%P in ('where python 2^>nul') do (
  echo %%P | findstr /i /c:"\WindowsApps\" >nul
  if errorlevel 1 (
    "%%P" -c "import sys" >nul 2>&1
    if not errorlevel 1 (
      set "SOCIAL_POD_PYTHON=%%P"
      exit /b 0
    )
  )
)
exit /b 1

:python_missing
echo.
echo Python no esta instalado o no esta disponible.
echo Instalá Python 3.10 o superior y ejecutá este archivo nuevamente.
goto :end

:dependencies_missing
echo.
echo Faltan componentes de Social Pod.
echo Hacé doble clic en INSTALL_SOCIAL_POD.cmd una sola vez y luego volvé a intentar.

:end
echo.
pause
endlocal
