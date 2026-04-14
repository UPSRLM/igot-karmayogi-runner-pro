# Live iGot QA Runner (Compliant)

This repo contains the runnable local automation for compliant progress QA/audit workflows.

What this runner does:

- Opens iGot in Chrome with a persistent profile.
- Processes modules in strict sequence.
- Skips assessment/test modules when configured (for manual completion later).
- Waits for real completion signals and logs evidence.
- Produces JSONL/CSV/Markdown run reports.

What this runner does not do:

- No timer bypass.
- No fake completion.
- No brute-force answering.

## Files Included for Runtime

- `run_live_qa.py`
- `requirements.txt`
- `iGot_QA_Runner.bat`
- `.env.example`
- `.gitignore`

## Quick Start (Windows)

```powershell
cd "C:\Users\upsrl\OneDrive\Documents\New project\live_igot_qa_1"
pip install -r requirements.txt
playwright install chromium
.\iGot_QA_Runner.bat
```

## Manual Run Command

```powershell
python .\run_live_qa.py `
  --base-url "https://igotkarmayogi.gov.in" `
  --start-url "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning" `
  --max-modules 50 `
  --strict-sequence `
  --skip-assessments `
  --auto-run-to-end `
  --video-speed 2.0 `
  --video-max-wait-seconds 2400 `
  --timer-lock-retry-seconds 180 `
  --timer-lock-max-retries 4 `
  --timer-lock-max-wait-seconds 1200 `
  --no-pause-for-quiz `
  --continue-on-error `
  --loading-timeout-seconds 35 `
  --profile-dir "$env:USERPROFILE\.igot_qa_profile_strict"
```

## Useful Flags

```text
--strict-sequence
--skip-assessments
--auto-run-to-end
--video-speed <0.5..2.0>
--max-modules <n>
--profile-dir <path>
--loading-timeout-seconds <n>
--continue-on-error
```

## Output

Each run writes to `reports/<run_id>/`:

- `run_report.jsonl`
- `run_report.csv`
- `run_summary.md`
- `artifacts/*.png`

## Reporting Format

- `Module: <name> | Status: done/blocked/partial | Evidence: <signal>`
- `Course: <name> | Status: completed/partial | Evidence: <signal>`
