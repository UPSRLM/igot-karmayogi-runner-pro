# iGot Karmayogi Runner Pro

**Policy-safe live QA & audit automation for iGot Karmayogi / iGOT-style learning portals.**

Built by **Saurabh Shukla** — [echonerve.com](https://echonerve.com)

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/automation-Playwright-45ba4b.svg)](https://playwright.dev/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![EchoNerve](https://img.shields.io/badge/community-Join%20EchoNerve%20%E2%86%92-FF6B35.svg)](https://echonerve.com/igot-karmayogi-runner-pro/)

---

> **👋 Free to use. One ask: join the community.**
>
> This tool is MIT-licensed and free forever. If it saves you time, please take 30 seconds to
> <a href="https://echonerve.com/igot-karmayogi-runner-pro/" target="_blank" rel="noopener"><strong>register a free EchoNerve account</strong></a> —
> it's where I write about AI systems, automation, and tools like this one. No cost, no spam.
>
> → <a href="https://echonerve.com/igot-karmayogi-runner-pro/" target="_blank" rel="noopener"><strong>echonerve.com/igot-karmayogi-runner-pro</strong></a>

---

## Why this exists

If you manage learning & development on iGOT Karmayogi (or any similarly built course portal), verifying that courses actually *work end-to-end* — videos play, PDFs open, quizzes load, completion ticks register — is tedious and easy to get wrong by hand.

**iGot Karmayogi Runner Pro** drives a real Chrome browser through your courses module-by-module, classifies what it finds, captures evidence screenshots, and produces a clean report you can hand to a QA lead, an L&D team, or your own changelog.

It is designed to be **policy-safe by default**:

- ✅ Uses a real, persistent Chrome profile — you log in once, like a normal learner
- ✅ Discovers courses and detects module type (`video`, `reading/pdf/slides`, `quiz`, `unknown`)
- ✅ Classifies blocked states clearly: `prerequisite_lock`, `timer_lock`, `technical_error`, `permission_issue`
- ✅ Captures evidence screenshots and structured JSON/CSV/Markdown reports per run
- 🚫 No seek-to-end video skipping
- 🚫 No timer bypass
- 🚫 No brute-force quiz answering

---

## What's in this repo

| Path | What it is |
|---|---|
| [`run_live_qa.py`](./run_live_qa.py) | The core QA runner — drives Chrome via Playwright, discovers/processes courses, writes reports |
| [`iGot_QA_Runner.bat`](./iGot_QA_Runner.bat) | One-click Windows launcher with a sensible "strict sequence, auto-run" preset |
| [`hosted_service/`](./hosted_service) | A minimal FastAPI service so you can run the QA worker as a hosted API (one job at a time, queued) |
| [`wordpress-plugin/igot-qa-runner-admin/`](./wordpress-plugin/igot-qa-runner-admin) | WordPress admin plugin that talks to the hosted API so non-technical staff can trigger runs |
| [`deploy/oracle/`](./deploy/oracle) | Bootstrap/deploy scripts for a free-tier Oracle Cloud VM |
| [`docs/`](./docs) | Extra setup guides and implementation notes |
| [`tests/`](./tests) | Test suite for the hosted API |
| `Dockerfile`, `docker-compose.yml` | Containerized hosted-service deployment |

---

## Quick Start

### 1. Prerequisites

- Python **3.11+**
- Google Chrome (or let Playwright install Chromium for you)
- Git

### 2. Clone and install

```bash
git clone https://github.com/UPSRLM/igot-karmayogi-runner-pro.git
cd igot-karmayogi-runner-pro

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

### 3. Run it

```bash
python run_live_qa.py --base-url "https://portal.igotkarmayogi.gov.in" --pause-for-quiz
```

On first run, a Chrome window opens with a persistent profile — log in normally (OTP/mobile/SSO). Your session is remembered for future runs via `--profile-dir`.

If no `--course-url` / `--start-url` is given, the runner drops into **interactive prompt mode** and asks you to paste course URLs one at a time.

---

## Windows One-Click Launcher

[`iGot_QA_Runner.bat`](./iGot_QA_Runner.bat) runs a strict, fully-automated sequence (50 modules, 16x video speed, auto-run to end, continue on error). Just double-click it.

Before running it, set your optional AI keys as environment variables (used for assistive quiz extraction only — never required):

```bat
setx IGOT_GROQ_API_KEY "your-groq-key"     # free at console.groq.com/keys
setx IGOT_GEMINI_API_KEY "your-gemini-key" # free at aistudio.google.com
```

> **Never commit real API keys.** Use environment variables or pass them with `--groq-api-key` / `--gemini-api-key` on the command line. See [`.env.example`](./.env.example).

---

## Full CLI Reference

### Example: strict module-by-module run

```bash
python run_live_qa.py \
  --base-url "https://portal.igotkarmayogi.gov.in" \
  --start-url "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning" \
  --max-courses 1 --max-modules 30 \
  --strict-sequence --continue-on-error \
  --loading-timeout-seconds 20 \
  --profile-dir "$HOME/.igot_qa_profile_strict"
```

### Example: strict sequence + skip exams + run to end

```bash
python run_live_qa.py \
  --base-url "https://portal.igotkarmayogi.gov.in" \
  --start-url "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning" \
  --max-courses 1 --max-modules 50 \
  --strict-sequence --skip-assessments --auto-run-to-end \
  --video-speed 4.0 --timer-lock-retry-seconds 120 --timer-lock-max-retries 3 \
  --no-pause-for-quiz --continue-on-error --loading-timeout-seconds 20 \
  --profile-dir "$HOME/.igot_qa_profile_strict"
```

---

## Login Screen Tip (OTP / Mobile Option Clipped)

If the login options are clipped on your screen, increase the window size and reduce the login zoom:

```bash
python run_live_qa.py \
  --base-url "https://igotkarmayogi.gov.in" \
  --start-url "https://portal.igotkarmayogi.gov.in" \
  --login-zoom-percent 67 --window-width 1920 --window-height 1080
```

---

## Output / Report Format

Each run creates a timestamped folder under `reports/`:

- `reports/<run_id>/run_report.jsonl` — line-delimited structured results
- `reports/<run_id>/run_report.csv` — spreadsheet-friendly summary
- `reports/<run_id>/run_summary.md` — human-readable summary
- `reports/<run_id>/artifacts/*.png` — evidence screenshots
- `reports/<run_id>/artifacts/quiz_extract_*.json` — assistive quiz extracts (when AI keys are configured)

Reporting lines look like:

```
Module: <name> | Status: done/blocked/partial | Evidence: <signal>
Course: <name> | Status: completed/partial | Evidence: <completion signal>
```

---

## Hosted API (FastAPI)

[`hosted_service/`](./hosted_service) wraps the runner in a small authenticated FastAPI service so you can trigger and monitor runs remotely — ideal for a VPS or subdomain like `igot.echonerve.com`.

```bash
cp .env.example .env   # fill in IGOT_SERVICE_TOKEN and friends
export IGOT_SERVICE_TOKEN="change-me"
python -m uvicorn hosted_service.main:app --host 0.0.0.0 --port 8080
```

Authenticated endpoints (`Authorization: Bearer <IGOT_SERVICE_TOKEN>`):

- `POST /api/runs` — queue a new run
- `GET /api/runs` — list runs
- `GET /api/runs/{run_id}` — run status
- `GET /api/runs/{run_id}/artifacts` — list evidence artifacts
- `GET /api/runs/{run_id}/artifacts/{artifact_path}` — fetch an artifact

```bash
curl -X POST http://localhost:8080/api/runs \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "start_url": "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning",
    "max_modules": 5,
    "strict_sequence": true,
    "auto_run_to_end": true,
    "groq_api_key": "user-supplied-key"
  }'
```

Run with Docker instead:

```bash
docker compose up --build
```

---

## WordPress Admin Integration

Install [`wordpress-plugin/igot-qa-runner-admin`](./wordpress-plugin/igot-qa-runner-admin) into `wp-content/plugins/`. It lets staff trigger runs and download evidence from wp-admin while keeping the API bearer token and AI keys server-side — never exposed to the browser, never stored in flash state.

---

## Cloud Deployment (Oracle Always Free)

For the cheapest realistic always-on deployment, see [`docs/ORACLE_ALWAYS_FREE_SETUP.md`](./docs/ORACLE_ALWAYS_FREE_SETUP.md) and the scripts in [`deploy/oracle/`](./deploy/oracle). Recommended shape: `VM.Standard.A1.Flex`, 2 OCPU / 12 GB RAM, Ubuntu 22.04/24.04, with the hosted FastAPI service behind nginx + TLS on a subdomain such as `igot.echonerve.com`.

---

## Testing

```bash
pytest tests/
```

---

## Contributing

Issues and pull requests are welcome — especially around new module-type detection, additional portal compatibility, and report formats. Please keep changes aligned with the policy-safe principles above (no skip/bypass automation).

---

## License

MIT — see [LICENSE](./LICENSE). Copyright © 2026 Saurabh Shukla / [EchoNerve](https://echonerve.com).

---

*If this tool helped you, <a href="https://echonerve.com/igot-karmayogi-runner-pro/" target="_blank" rel="noopener">join EchoNerve</a> — free, no strings.*
