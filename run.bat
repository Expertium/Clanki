@echo off
pushd "%~dp0"

set PYTHONWARNINGS=default
set PYTHONPYCACHEPREFIX=out\pycache
set ANKIDEV=1
set QTWEBENGINE_REMOTE_DEBUGGING=8080
set QTWEBENGINE_CHROMIUM_FLAGS=--remote-allow-origins=https://chrome-devtools-frontend.appspot.com,http://localhost:8080
set ANKI_API_PORT=40000
set ANKI_API_HOST=127.0.0.1

@if not defined PYENV set PYENV=out\pyenv
  
call tools\ninja pylib qt || exit /b 1
rem start the app under its own name, so Task Manager says Clanki and
rem this window waits for the app itself (spec ui.process-name)
set CLANKI_LAUNCHER=%PYENV%\Scripts\python.exe
if exist "%PYENV%\Scripts\Clanki.exe" set CLANKI_LAUNCHER=%PYENV%\Scripts\Clanki.exe
"%CLANKI_LAUNCHER%" tools\run.py %* || exit /b 1
popd
