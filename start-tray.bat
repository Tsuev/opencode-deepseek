@echo off
cd /d "%~dp0"
start "DeepSeek local bridge" /min pythonw.exe tray.py
