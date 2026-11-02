$projectPython = Join-Path $env:LOCALAPPDATA 'AgentWorkspaceManager\development\.venv\Scripts\python.exe'
$implementation = 'C:\Users\surya\OneDrive\Documents\ChatGPT\filegit'
& $projectPython -c 'import sys, runpy; sys.path.insert(0, sys.argv[1]); runpy.run_module("app.local_sync", run_name="__main__")' $implementation
exit $LASTEXITCODE
