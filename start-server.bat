@echo off
rem Запуск локального моста DeepSeek -> OpenAI API (http://127.0.0.1:8000)
rem Окно должно оставаться открытым, пока мост нужен.
cd /d D:\deepseek-web-api\opencode-deepseek
python app.py
pause
