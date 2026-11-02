@echo off
cd /d "%~dp0"
docker compose -f compose.local-cloud.yaml up -d --build --wait
if errorlevel 1 exit /b 1
start http://localhost:8090
