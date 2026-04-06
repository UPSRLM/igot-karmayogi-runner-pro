# iGot Progress QA and Audit SOP (Compliant Fast-Execution Version)

This SOP replaces bypass-style completion instructions with a policy-safe workflow focused on progress validation, issue detection, and audit-quality reporting.

## Purpose

- Accelerate portal operations without bypassing learning safeguards.
- Produce deterministic status for each module and course.
- Capture evidence for every done or blocked outcome.

## Guardrails (Always On)

- No forced skipping logic (`seek-to-end`, timer evasion, brute-force quiz attempts, automated blind guessing).
- User performs authentication and approves final quiz submissions.
- Automation assists navigation, extraction, and verification only.

## Step 0 - Browser Setup (Diagnostics-Ready)

- Use Google Chrome only.
- Disable unstable extensions that interfere with page events or playback telemetry.
- Keep stable connectivity and avoid background network-heavy jobs.
- Open DevTools and keep the Network tab available for diagnostics and evidence capture.
- Enable screenshots and timestamped logging for every module transition.

## Step 1 - Discover Courses and Build Execution Queue

- Open `My Courses` / `My Learning`.
- Collect all enrolled, incomplete courses.
- Build queue entries with:
  - `course_name`
  - `completion_percent`
  - `blocked_dependencies` (if visible)
  - `priority` (higher priority for nearly complete courses)
- Start with the highest-priority eligible course.

## Step 2 - Video Modules (Validate, Monitor, Record)

- Open video module and verify it is playable.
- Record initial evidence:
  - video loaded
  - duration available
  - progress indicator visible
- Monitor valid completion signals:
  - playback completion event
  - module UI state updates to complete/eligible
- If completion signal is missing after valid playback:
  - mark as `blocked`
  - set `block_reason=technical_error`
  - capture screenshot + network evidence
  - continue to next eligible module

## Step 3 - Reading / PDF / Slides (Completion Signal Tracking)

- Open reading module and detect module type (`reading`, `pdf`, `slides`).
- Track required completion signals:
  - required page reach events
  - viewer completion indicator
  - module status update in sidebar/tree
- Do not use non-compliant "scroll-only to bypass" behavior.
- If completion state does not update despite valid interaction:
  - mark `blocked`
  - set `block_reason=technical_error`
  - capture evidence and continue.

## Step 4 - Quizzes (Assistive Mode Only)

- Extract question text and options for assistive analysis.
- Generate suggested reasoning/answer candidates for user review.
- Require user confirmation before selecting/submitting answers.
- After submit, log:
  - pass/fail status
  - score (if present)
  - attempt count
- On fail:
  - mark module `partial`
  - provide next-step guidance for user retry
  - do not brute-force or auto-cycle options.

## Step 5 - Navigation with Zero Idle Time (Compliant)

- After each module outcome, move immediately to the next eligible module.
- Respect prerequisites and mandatory waits.
- Prefer direct jumps from course sidebar/tree to incomplete eligible items.
- Skip informational popups only when not required for completion validation.

## Step 6 - Blocked State Taxonomy and Fallback Routing

Use only the following block reasons:

- `prerequisite_lock`: Module locked behind required predecessor.
- `timer_lock`: Platform timer not yet elapsed.
- `technical_error`: Playback, submit, or status-sync defect.
- `permission_issue`: Access denied or role limitation.

Fallback rules:

- `prerequisite_lock`: Queue dependency and move to other eligible modules.
- `timer_lock`: Schedule revisit time and continue with another module/course.
- `technical_error`: Capture evidence bundle and continue; escalate for support.
- `permission_issue`: Capture denial evidence and raise to admin.

## Step 7 - Course Completion Verification

- When all modules show complete/eligible-complete, verify:
  - course status = complete
  - certificate/badge generated (if applicable)
- Save proof artifacts:
  - status screenshot
  - completion artifact reference
  - timestamped final audit line
- Move to next queued course immediately.

## Reporting Format (Strict)

Module-level report line:

`Module: <name> | Status: done/blocked/partial | Evidence: <signal>`

Course-level report line:

`Course: <name> | Status: completed/partial | Evidence: <completion signal>`

## Standardized Run Report Object

```json
{
  "run_id": "string",
  "timestamp": "ISO-8601 datetime",
  "course_name": "string",
  "module_name": "string",
  "module_type": "video|reading|pdf|slides|quiz|unknown",
  "status": "done|blocked|partial",
  "block_reason": "prerequisite_lock|timer_lock|technical_error|permission_issue|null",
  "evidence": "string",
  "next_action": "string"
}
```

## Completion Evidence Rules

- `video`: playback completion event + module UI complete state.
- `reading/pdf/slides`: required page progression + module UI complete state.
- `quiz`: user-confirmed submission + pass/fail outcome recorded.

## Success Criteria

- No bypass actions are executed.
- Every module ends with deterministic status (`done`, `blocked`, or `partial`).
- Every blocked state has a retry/fallback path and evidence record.

## Assumptions and Defaults

- User handles authentication and final approval-sensitive actions.
- SOP prioritizes speed through efficient navigation and queueing, not shortcutting safeguards.
- Output quality is measured by auditability, reproducibility, and compliant progress tracking.
