@echo off
rem Sync eyecatch images to GitHub. Registered in Task Scheduler (every 15 min).
rem You can also double-click this file to run it manually.
rem "start /wait" is required: cmd does not wait for pythonw.exe (a GUI app)
rem on its own, so without it the task would end before the sync finishes.
cd /d "%~dp0"
start /wait "" ".venv\Scripts\pythonw.exe" -m src.sync_images
