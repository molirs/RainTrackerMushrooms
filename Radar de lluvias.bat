@echo off
rem Doble clic para abrir el Radar de lluvias para setas.
chcp 65001 >nul
title Radar de lluvias para setas
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Primera ejecucion: preparando Python, tarda un par de minutos...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo No se ha encontrado Python. Instalalo desde https://www.python.org/downloads/
        echo marcando "Add python.exe to PATH" y vuelve a abrir este archivo.
        pause
        exit /b 1
    )
    del ".venv\.instalado" 2>nul
)

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
