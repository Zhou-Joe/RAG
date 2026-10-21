@echo off
setlocal EnableExtensions
title FOS-RAG Local Deployment

set "PROJECT_DIR=D:\Codex\RAG"
set "VENV_DIR=%PROJECT_DIR%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "MINERU_EXE=%VENV_DIR%\Scripts\mineru-api.exe"
set "REQUIREMENTS=%PROJECT_DIR%\requirements.txt"

set "MINERU_MODEL_SOURCE=modelscope"
set "MODELSCOPE_CACHE=D:\Codex\.cache\modelscope"
set "HF_HOME=D:\Codex\.cache\huggingface"
set "PIP_CACHE_DIR=D:\Codex\.cache\pip"

echo ============================================================
echo   FOS-RAG Local Deployment
echo   RAG:     http://127.0.0.1:8000/
echo   MinerU:  http://127.0.0.1:8888/health
echo ============================================================
echo.

if not exist "%PROJECT_DIR%\manage.py" (
  echo [ERROR] Project not found: %PROJECT_DIR%
  goto :failed
)

cd /d "%PROJECT_DIR%"

echo [1/6] Checking Python virtual environment...
if not exist "%PYTHON_EXE%" (
  where py.exe >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Python Launcher not found. Install Python 3.13 or newer first.
    goto :failed
  )
  echo       Creating virtual environment...
  py -3.13 -m venv "%VENV_DIR%"
  if errorlevel 1 goto :failed
)

echo [2/6] Checking project dependencies and MinerU...
"%PYTHON_EXE%" -c "import django,daphne,dotenv,httpx,langchain,langchain_openai,langchain_chroma,langgraph,chromadb,aiosqlite,openai,openpyxl,mineru" >nul 2>&1
if errorlevel 1 (
  echo       Missing dependencies. Installing now; the first run can take a while...
  "%PYTHON_EXE%" -m pip install --disable-pip-version-check -r "%REQUIREMENTS%"
  if errorlevel 1 goto :failed
) else (
  echo       Dependencies are ready.
)

if not exist "%MINERU_EXE%" (
  echo [ERROR] MinerU command was not created. Run this script again to repair dependencies.
  goto :failed
)

echo [3/6] Applying database migrations and collecting static files...
"%PYTHON_EXE%" manage.py migrate --noinput
if errorlevel 1 goto :failed
"%PYTHON_EXE%" manage.py collectstatic --noinput >nul
if errorlevel 1 goto :failed
"%PYTHON_EXE%" manage.py check
if errorlevel 1 goto :failed

echo [4/6] Starting MinerU OCR on 127.0.0.1:8888...
netstat -ano | findstr /R /C:":8888 .*LISTENING" >nul 2>&1
if errorlevel 1 (
  start "MinerU OCR - 8888" /min "%ComSpec%" /k ""%MINERU_EXE%" --host 127.0.0.1 --port 8888"
) else (
  echo       Port 8888 is already listening. Skipping duplicate start.
)

set /a MINERU_TRIES=0
:wait_mineru
curl.exe -fsS "http://127.0.0.1:8888/v1/health" >nul 2>&1
if not errorlevel 1 goto :mineru_ready
set /a MINERU_TRIES+=1
if %MINERU_TRIES% GEQ 45 goto :mineru_timeout
timeout /t 2 /nobreak >nul
goto :wait_mineru

:mineru_timeout
echo       [WARN] MinerU is still loading. FOS-RAG will start anyway.
goto :start_rag

:mineru_ready
echo       MinerU is ready.

:start_rag
echo [5/6] Starting FOS-RAG on 127.0.0.1:8000...
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>&1
if errorlevel 1 (
  start "FOS-RAG - 8000" /min "%ComSpec%" /k ""%PYTHON_EXE%" manage.py runserver 127.0.0.1:8000 --noreload"
) else (
  echo       Port 8000 is already listening. Skipping duplicate start.
)

set /a RAG_TRIES=0
:wait_rag
curl.exe -fsS "http://127.0.0.1:8000/" >nul 2>&1
if not errorlevel 1 goto :rag_ready
set /a RAG_TRIES+=1
if %RAG_TRIES% GEQ 20 goto :rag_timeout
timeout /t 1 /nobreak >nul
goto :wait_rag

:rag_timeout
echo [ERROR] FOS-RAG did not start in time. Check the FOS-RAG service window.
goto :failed

:rag_ready
echo [6/6] Deployment complete. Opening the browser...
start "" "http://127.0.0.1:8000/"
echo.
echo ============================================================
echo   Deployment succeeded
echo   FOS-RAG:  http://127.0.0.1:8000/
echo   MinerU:   http://127.0.0.1:8888/health
echo.
echo   Service windows are minimized. Keep them open while using FOS-RAG.
echo ============================================================
echo.
pause
exit /b 0

:failed
echo.
echo ============================================================
echo   Deployment failed. Review the error above.
echo ============================================================
echo.
pause
exit /b 1
