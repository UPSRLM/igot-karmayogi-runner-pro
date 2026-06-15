@echo off
title iGot QA Runner
cd /d "%~dp0"

rem Set your AI keys as environment variables before running this script,
rem or pass --groq-api-key / --gemini-api-key on the command line.
rem   setx IGOT_GROQ_API_KEY "your-groq-key"
rem   setx IGOT_GEMINI_API_KEY "your-gemini-key"
rem Get a free Groq key at https://console.groq.com/keys
rem Get a free Gemini key at https://aistudio.google.com

python "run_live_qa.py" ^
    --max-modules 50 ^
    --strict-sequence ^
    --auto-run-to-end ^
    --video-speed 16.0 ^
    --video-max-wait-seconds 2400 ^
    --no-pause-for-quiz ^
    --continue-on-error ^
    --loading-timeout-seconds 35 ^
    --profile-dir "%USERPROFILE%\.igot_qa_profile_strict"

echo.
echo ================================================
echo  iGot QA Runner finished. Press any key to exit.
echo ================================================
pause >nul
