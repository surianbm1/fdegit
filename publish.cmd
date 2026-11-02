@echo off
setlocal
set "projectPython=%LOCALAPPDATA%\AgentWorkspaceManager\development\.venv\Scripts\python.exe"
if not exist "%projectPython%" (
  echo Publish failed: the project Python environment is missing.
  exit /b 1
)
"%projectPython%" -c "import sys, runpy; sys.path.insert(0, sys.argv[1]); runpy.run_module('app.local_sync', run_name='__main__')" "C:\Users\surya\OneDrive\Documents\ChatGPT\filegit"
exit /b %errorlevel%
