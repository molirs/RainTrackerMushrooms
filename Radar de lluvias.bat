@echo off
rem Doble clic para abrir el Radar de lluvias para setas.
rem En un PC nuevo la primera vez instala Python (si falta), las dependencias y un acceso directo.
chcp 65001 >nul
title Radar de lluvias para setas
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto dependencias

echo Primera ejecucion: preparando Python, tarda un par de minutos...
call :buscar_python
if defined PYCMD goto crear_venv

echo No se ha encontrado Python: lo instalo con winget (solo para este usuario)...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :buscar_python
if defined PYCMD goto crear_venv

echo.
echo No he podido instalar Python. Instalalo desde https://www.python.org/downloads/
echo marcando "Add python.exe to PATH" y vuelve a abrir este archivo.
pause
exit /b 1

:crear_venv
%PYCMD% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Error creando el entorno de Python en "%~dp0.venv".
    pause
    exit /b 1
)
del ".venv\.instalado" 2>nul
call :acceso_directo

:dependencias
if not exist ".venv\.instalado" (
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (
        echo.
        echo Error instalando dependencias. Revisa tu conexion a internet.
        pause
        exit /b 1
    )
    echo ok> ".venv\.instalado"
)

".venv\Scripts\python.exe" -m app.launcher %*
if errorlevel 1 pause
exit /b

rem ---- Busca un Python 3 que funcione (el "python" de la Microsoft Store es un acceso falso) ----
:buscar_python
set "PYCMD="
py -3 -c "import sys" >nul 2>nul && (set "PYCMD=py -3" & exit /b)
python -c "import sys" >nul 2>nul && (set "PYCMD=python" & exit /b)
rem Recien instalado con winget: aun no esta en el PATH de esta ventana
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~D\python.exe" set PYCMD="%%~D\python.exe"
)
exit /b

rem ---- Acceso directo en el escritorio con el icono de la app ----
:acceso_directo
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Radar de lluvias.lnk')); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0app\static\icono.ico'; $s.Save()" >nul 2>nul
exit /b
