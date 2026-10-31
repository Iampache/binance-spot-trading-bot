@echo off
setlocal EnableExtensions DisableDelayedExpansion
title TRBT - Live Spot
set "BOT_EXIT_CODE=1"

rem Explorer opens this batch file in Command Prompt. Always use this checkout,
rem even when the file is launched from a shortcut or a different directory.
pushd "%~dp0"
if errorlevel 1 goto folder_error

if not exist ".venv\Scripts\python.exe" goto missing_venv
if not exist ".venv\Scripts\activate.bat" goto missing_venv
if not exist "telegram_assistant.py" goto missing_bot
if not exist ".env" goto missing_config

call ".venv\Scripts\activate.bat"
if errorlevel 1 goto activation_error

rem Credentials stay in the user's local .env; never embed or echo them here.
set "PYTHON_DOTENV_DISABLED="
set "USE_TESTNET=false"
set "ALLOW_LIVE_TRADING=true"
set "AUTO_SCAN=true"

echo Launching TRBT - Live Spot.
echo Automatic scanning: enabled.
echo Order approval: required in Telegram before every new buy.
echo Leave this window open while TRBT is running. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -u "telegram_assistant.py"
set "BOT_EXIT_CODE=%ERRORLEVEL%"
echo.
echo TRBT stopped. Exit code: %BOT_EXIT_CODE%
goto finish

:missing_venv
echo ERROR: This checkout needs its own Windows .venv.
echo Follow the Windows installation steps in README.md, then try again.
goto finish

:missing_bot
echo ERROR: telegram_assistant.py is missing from this checkout.
goto finish

:missing_config
echo ERROR: Local .env is missing. Configure it here before starting Live Spot mode.
echo Never put API keys or Telegram tokens inside this batch file.
goto finish

:activation_error
echo ERROR: The virtual environment could not be activated.
goto finish

:folder_error
echo ERROR: Cannot open the folder containing this launcher.
goto done

:finish
popd
:done
echo.
echo Press any key to close this window.
pause >nul
exit /b %BOT_EXIT_CODE%
