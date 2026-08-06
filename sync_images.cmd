@echo off
rem Sync eyecatch images to GitHub. Registered in Task Scheduler (every 15 min).
rem You can also double-click this file to run it manually.
cd /d "%~dp0"
".venv\Scripts\pythonw.exe" -m src.sync_images
