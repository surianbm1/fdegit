#!/usr/bin/env bash
set -euo pipefail
exec "$(cygpath -u "$LOCALAPPDATA")/AgentWorkspaceManager/development/.venv/Scripts/python.exe" -c "import sys, runpy; sys.path.insert(0, sys.argv[1]); runpy.run_module('app.local_sync', run_name='__main__')" 'C:\Users\surya\OneDrive\Documents\ChatGPT\filegit' pull
