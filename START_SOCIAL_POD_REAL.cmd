@echo off
setlocal
cd /d "%~dp0"

echo.
echo ==================================================
echo SOCIAL POD - MODO REAL
echo ==================================================
echo.
echo ADVERTENCIA:
echo Este modo puede ejecutar publicaciones reales
echo en cuentas conectadas.
echo.
set /p "SOCIAL_POD_CONFIRMACION=Escribi REAL para continuar. Cualquier otro valor cancelara: "
if /i not "%SOCIAL_POD_CONFIRMACION%"=="REAL" goto :cancelled

set "SOCIAL_POD_EXECUTION_ENABLED=true"
echo.
echo Social Pod iniciando...
echo Modo: REAL
call :detect_python
if errorlevel 1 goto :python_missing

echo Python: OK
"%SOCIAL_POD_PYTHON%" -c "import sqlalchemy, camoufox_pm, social_pod_engine" >nul 2>&1
if errorlevel 1 goto :dependencies_missing

"%SOCIAL_POD_PYTHON%" -m social_pod_engine.tools.windows_launcher --mode real
set "SOCIAL_POD_EXIT=%ERRORLEVEL%"
if not "%SOCIAL_POD_EXIT%"=="0" (
  echo.
  echo Social Pod no pudo iniciarse. Revisa el mensaje anterior.
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

:cancelled
echo.
echo Inicio cancelado. No se habilito ejecucion real.
goto :end

:python_missing
echo.
echo Python no esta instalado o no esta disponible.
echo Instala Python 3.10 o superior y ejecuta este archivo nuevamente.
goto :end

:dependencies_missing
echo.
echo Faltan componentes de Social Pod.
echo Hace doble clic en INSTALL_SOCIAL_POD.cmd una sola vez y luego volve a intentar.

:end
echo.
pause
endlocal
