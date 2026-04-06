# Live iGot QA Runner (Compliant)

This tool runs a **policy-safe live QA + audit workflow** for iGot-like course portals.

What it does:
- Uses Chrome with a persistent profile.
- Discovers courses and module candidates.
- Detects module type (`video`, `reading/pdf/slides`, `quiz`, `unknown`).
- Classifies blocked states:
  - `prerequisite_lock`
  - `timer_lock`
  - `technical_error`
  - `permission_issue`
- Captures evidence screenshots and structured reports.

What it does **not** do:
- No seek-to-end skipping.
- No timer bypass.
- No brute-force quiz answering.

## Quick Start (Run Live Now)

```powershell
cd "C:\Users\upsrl\OneDrive\Documents\New project\live_igot_qa"
.\start_live_qa.ps1
```

If PowerShell blocks script execution, run:

```powershell
powershell -ExecutionPolicy Bypass -File ".\start_live_qa.ps1"
```

## Manual Run

```powershell
cd "C:\Users\upsrl\OneDrive\Documents\New project\live_igot_qa"
pip install -r requirements.txt
python .\run_live_qa.py --base-url "https://igotkarmayogi.gov.in" --pause-for-quiz
```

## Strict Sequence Run (Module-by-Module)

```powershell
python .\run_live_qa.py --base-url "https://igotkarmayogi.gov.in" --start-url "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning" --max-courses 1 --max-modules 30 --strict-sequence --continue-on-error --loading-timeout-seconds 20 --profile-dir "$env:USERPROFILE\.igot_qa_profile_strict"
```

## Strict Sequence + Skip Exams + Run To End

```powershell
python .\run_live_qa.py --base-url "https://igotkarmayogi.gov.in" --start-url "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning" --max-courses 1 --max-modules 50 --strict-sequence --skip-assessments --auto-run-to-end --video-speed 2.0 --timer-lock-retry-seconds 120 --timer-lock-max-retries 3 --no-pause-for-quiz --continue-on-error --loading-timeout-seconds 20 --profile-dir "$env:USERPROFILE\.igot_qa_profile_strict"
```

## Useful Flags

```text
--max-courses <n>        Limit number of courses (0 = all)
--max-modules <n>        Limit modules per course (0 = all)
--strict-sequence        Enforce module-by-module order and pause if current module is not ticked
--skip-assessments       Skip quiz/final assessment modules and continue
--auto-run-to-end        Continue remaining modules without manual tick confirmation prompts
--timer-lock-retry-seconds <n> Wait seconds before retrying timer-locked modules in auto-run mode
--timer-lock-max-retries <n>   Retry cap per timer-locked module in auto-run mode
--video-speed <n>              Preferred compliant video speed (0.5 to 2.0)
--video-max-wait-seconds <n>   Max auto-wait per video module for tick/completion in auto-run mode
--start-url <url>        Start directly from specific page (useful if nav differs)
--course-url <url>       Process one course directly (skip dashboard discovery)
--window-width <n>       Chrome window width (default: 1920)
--window-height <n>      Chrome window height (default: 1080)
--login-zoom-percent <n> Auto zoom on login page (default: 80)
--loading-timeout-seconds <n> Max wait for spinner pages before marking blocked
--headless               Run Chrome headless
--slow-mo-ms <ms>        Slow down actions for debugging
--continue-on-error      Continue to next course when one fails
--profile-dir <path>     Persistent Chrome profile directory
--output-dir <path>      Report output directory (default: reports)
--no-pause-for-quiz      Do not pause for manual quiz review (not recommended)
```

## Login Screen Fix (OTP/Mobile Option Not Visible)

If the login options are clipped:

1. Run using the helper script (it now forces wide window + login zoom automatically):

```powershell
powershell -ExecutionPolicy Bypass -File ".\start_live_qa.ps1" -StartUrl "https://portal.igotkarmayogi.gov.in" -WindowWidth 1920 -WindowHeight 1080 -LoginZoomPercent 80
```

2. If still clipped, rerun with stronger zoom:

```powershell
python .\run_live_qa.py --base-url "https://igotkarmayogi.gov.in" --start-url "https://portal.igotkarmayogi.gov.in" --login-zoom-percent 67 --window-width 1920 --window-height 1080
```

## Output

Each run creates:

- `reports/<run_id>/run_report.jsonl`
- `reports/<run_id>/run_report.csv`
- `reports/<run_id>/run_summary.md`
- `reports/<run_id>/artifacts/*.png`
- `reports/<run_id>/artifacts/quiz_extract_*.json` (quiz assistive extracts)

## Reporting Format

Module line:

`Module: <name> | Status: done/blocked/partial | Evidence: <signal>`

Course line:

`Course: <name> | Status: completed/partial | Evidence: <completion signal>`
