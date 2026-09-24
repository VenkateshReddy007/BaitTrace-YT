@echo off
set PYTHONPATH=E:\BaitTrace\YT Scam\Anti-YTScamify
"E:\BaitTrace\YT Scam\Anti-YTScamify\baittrace-yt-sensor\venv\Scripts\python.exe" -m pytest "E:\BaitTrace\YT Scam\Anti-YTScamify\tests" -v --tb=short > "E:\BaitTrace\YT Scam\Anti-YTScamify\test_output.txt" 2>&1
echo Exit code: %ERRORLEVEL% >> "E:\BaitTrace\YT Scam\Anti-YTScamify\test_output.txt"
