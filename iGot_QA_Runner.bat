@echo off
title iGot QA Runner
cd /d "%~dp0"

set "PROFILE_DIR=%USERPROFILE%\.igot_qa_profile_strict"
set "START_URL=https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning"

python "run_live_qa.py" ^
    --base-url "https://igotkarmayogi.gov.in" ^
    --start-url "%START_URL%" ^
    --max-modules 50 ^
    --strict-sequence ^
    --skip-assessments ^
    --auto-run-to-end ^
    --video-speed 2.0 ^
    --video-max-wait-seconds 2400 ^
    --timer-lock-retry-seconds 180 ^
    --timer-lock-max-retries 4 ^
    --timer-lock-max-wait-seconds 1200 ^
    --no-pause-for-quiz ^
    --continue-on-error ^
    --loading-timeout-seconds 35 ^
    --profile-dir "%PROFILE_DIR%"

echo.
echo ================================================
echo  iGot QA Runner finished. Press any key to exit.
echo ================================================
pause >nul
