@echo off
chcp 65001 >nul
rem Запуск бота дедлайнов на Windows. Двойной щелчок по этому файлу.
cd /d "%~dp0"
if not exist .env (
  echo Нет файла .env. Скопируйте .env.example в .env и впишите BOT_TOKEN.
  pause
  exit /b 1
)
if not exist .venv (
  echo Первый запуск: ставлю зависимости...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
:loop
echo Бот запущен. Не закрывайте это окно. Остановить: Ctrl+C.
.venv\Scripts\python bot.py
echo Бот остановился, перезапуск через 10 секунд...
timeout /t 10 >nul
goto loop
