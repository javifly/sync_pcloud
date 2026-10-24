@echo off
rem =====================================================================
rem  sync_pcloud.bat  -  Espejo carpeta local -> pCloud (lo local manda)
rem
rem  Uso:
rem    sync_pcloud.bat              sincroniza y hace pausa al final
rem                                 (la 1a vez te pide iniciar sesion)
rem    sync_pcloud.bat --simular    solo muestra lo que haria
rem    sync_pcloud.bat --auto       desatendido, sin preguntas ni pausa
rem                                 (para el Programador de tareas)
rem
rem  Codigos de salida: 0 ok | 1 errores en ficheros | 2 config/red
rem    3 abortado por seguridad | 4 ya habia otra ejecucion en marcha
rem    5 hay que iniciar sesion (ejecutalo a mano sin --auto)
rem  Con codigo 4 la ventana se cierra sola tras 10 segundos.
rem =====================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PAUSA=1"
for %%A in (%*) do if /i "%%~A"=="--auto" set "PAUSA=0"

rem --- Buscar un Python 3 que FUNCIONE de verdad ------------------------
call :buscar_python
if defined PY goto :python_ok

echo.
echo No se encuentra ningun Python 3 instalado en este equipo.
if "%PAUSA%"=="0" goto :sin_python
where winget >nul 2>&1
if errorlevel 1 goto :sin_python

choice /c SN /m "Quieres instalar Python 3.13 ahora con winget"
if errorlevel 2 goto :sin_python
echo.
echo Instalando Python 3.13 solo para tu usuario, puede tardar un poco...
winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
call :buscar_python
if defined PY goto :python_ok
echo.
echo Python se ha instalado pero esta ventana aun no lo ve.
echo Cierra esta ventana y vuelve a ejecutar sync_pcloud.bat
set "RC=2"
goto :fin

:sin_python
echo.
echo Instala Python 3 desde https://www.python.org/downloads/
echo y marca la casilla "Add python.exe to PATH" durante la instalacion.
echo O desde una consola:  winget install -e --id Python.Python.3.13
set "RC=2"
goto :fin

:python_ok
echo Usando Python: %PY%

rem --- Instalar 'requests' la primera vez si falta ---------------------
%PY% -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo Instalando la libreria requests...
    if exist "%~dp0requirements.txt" (
        %PY% -m pip install --user --disable-pip-version-check -r "%~dp0requirements.txt"
    ) else (
        %PY% -m pip install --user --disable-pip-version-check requests
    )
)

%PY% "%~dp0sync_pcloud.py" --config "%~dp0config.ini" %*
set "RC=%ERRORLEVEL%"

:fin
if not "%PAUSA%"=="1" goto :salir
rem Codigo 4 = ya habia otra ejecucion en marcha: no hay nada que leer,
rem cuenta atras de 10 s y la ventana se cierra sola (o pulsa una tecla).
if "%RC%"=="4" (
    echo.
    echo Esta ventana se cerrara sola en 10 segundos.
    timeout /t 10
    goto :salir
)
pause
:salir
exit /b %RC%


rem =====================================================================
rem  Subrutinas
rem =====================================================================
:buscar_python
set "PY="
call :probar py -3
if not defined PY call :probar python
if not defined PY call :probar python3
rem Instalaciones tipicas aunque no esten en el PATH:
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PY call :probar "%%~fD\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if not defined PY call :probar "%%~fD\python.exe"
exit /b 0

:probar
rem Solo se acepta si arranca y es Python 3.8 o superior. Asi se descartan
rem el lanzador "py" sin Python y el acceso directo de la Microsoft Store.
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0
