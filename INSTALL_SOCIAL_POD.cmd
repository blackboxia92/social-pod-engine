@echo off
setlocal
cd /d "%~dp0"

echo.
echo Preparando Social Pod...
call :detect_python
if errorlevel 1 goto :python_missing

echo Python: OK
echo Instalando los componentes necesarios. Esto puede demorar unos minutos la primera vez.
"%SOCIAL_POD_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :install_error

"%SOCIAL_POD_PYTHON%" -m pip install -e .
if errorlevel 1 goto :install_error

"%SOCIAL_POD_PYTHON%" -c "import sqlalchemy, camoufox_pm, social_pod_engine"
if errorlevel 1 goto :install_error

echo.
echo Instalacion completada. Ahora hacé doble clic en START_SOCIAL_POD.cmd.
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

:install_error
echo.
echo No se pudo completar la instalacion. Verificá tu conexión a internet e intentá nuevamente.

:end
echo.
pause
endlocal
