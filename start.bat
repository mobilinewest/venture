@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m clients) || (python -m clients)
echo.
echo Программа остановлена. Если выше есть ошибка - пришлите её текст.
pause
