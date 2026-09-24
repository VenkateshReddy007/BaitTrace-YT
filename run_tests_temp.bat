@echo off
cd /d "e:\BaitTrace\YT Scam\Anti-YTScamify"
python -m pytest tests/ -v 2>&1
echo EXIT_CODE=%ERRORLEVEL%
