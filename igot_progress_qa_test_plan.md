# iGot Progress QA and Audit Test Plan

This test plan validates the compliant SOP and reporting model.

## Global Acceptance Gates

- No bypass behavior executed.
- Every module ends in `done`, `blocked`, or `partial`.
- Every blocked result includes valid `block_reason`, `evidence`, and `next_action`.
- Report entries validate against `igot_run_report.schema.json`.

## Scenario 1 - Normal Path (Video + Reading + Quiz)

### Setup

- One course with three modules: video, reading, quiz.
- User has full permissions.

### Steps

- Run discovery and queueing.
- Complete video with valid playback completion.
- Complete reading module with required progression signal.
- Run quiz in assistive mode and submit with user confirmation.

### Expected Results

- Three module reports created with deterministic statuses.
- At least one `done` report for each module type.
- Course report emitted as `completed` with completion evidence.

## Scenario 2 - Timer-Locked Module

### Setup

- Course contains a module with mandatory wait timer.

### Steps

- Attempt module before timer elapses.
- Detect timer lock and route to another eligible module.
- Revisit after wait window.

### Expected Results

- First attempt logged as `blocked` with `block_reason=timer_lock`.
- Revisit action present in `next_action`.
- Later attempt resolves to `done` or documented follow-up block.

## Scenario 3 - Prerequisite-Locked Module

### Setup

- Module B is locked until Module A is complete.

### Steps

- Attempt Module B first.
- Detect prerequisite lock.
- Complete Module A.
- Return to Module B.

### Expected Results

- Initial Module B report: `blocked` with `prerequisite_lock`.
- Queue/dependency update captured.
- Subsequent Module B run proceeds without lock if prerequisite satisfied.

## Scenario 4 - Quiz Fail with Assistive Retry

### Setup

- Quiz pass threshold intentionally not met on first user-approved attempt.

### Steps

- Run assistive mode and submit with user confirmation.
- Observe fail status.
- Provide retry guidance and rerun assistive flow.

### Expected Results

- First quiz report is `partial` with fail evidence.
- No brute-force or automated blind answer cycling occurs.
- Retry path documented in `next_action`.

## Scenario 5 - Completion Verification and Evidence Capture

### Setup

- Course modules all completed.

### Steps

- Verify course completion status page.
- Verify certificate/badge availability (if applicable).
- Emit final course-level report.

### Expected Results

- Completion artifact reference captured.
- Final course report status is `completed` (or `partial` if artifact missing).
- Evidence contains explicit completion signal.

## Regression Checks

- Invalid `block_reason` values are rejected by schema validation.
- `status=blocked` without `block_reason` fails validation.
- `status=done` or `partial` with non-null `block_reason` fails validation.
