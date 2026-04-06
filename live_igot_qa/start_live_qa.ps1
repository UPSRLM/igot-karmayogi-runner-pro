param(
  [string]$BaseUrl = "https://igotkarmayogi.gov.in",
  [string]$StartUrl = "https://portal.igotkarmayogi.gov.in",
  [string]$CourseUrl = "",
  [string]$OutputDir = "reports",
  [int]$MaxCourses = 0,
  [int]$MaxModules = 0,
  [switch]$StrictSequence,
  [switch]$SkipAssessments,
  [switch]$AutoRunToEnd,
  [int]$TimerLockRetrySeconds = 120,
  [int]$TimerLockMaxRetries = 2,
  [int]$TimerLockMaxWaitSeconds = 900,
  [int]$WindowWidth = 1920,
  [int]$WindowHeight = 1080,
  [int]$LoginZoomPercent = 80,
  [double]$VideoSpeed = 2.0,
  [int]$VideoMaxWaitSeconds = 2400
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $scriptDir

Write-Host "Starting compliant live QA run..."
Write-Host "Base URL: $BaseUrl"
Write-Host "Start URL: $StartUrl"

try {
  python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Playwright not found. Installing..."
    pip install -r ".\requirements.txt"
  }

  $runArgs = @(
    ".\run_live_qa.py",
    "--base-url", "$BaseUrl",
    "--start-url", "$StartUrl",
    "--output-dir", "$OutputDir",
    "--max-courses", "$MaxCourses",
    "--max-modules", "$MaxModules",
    "--window-width", "$WindowWidth",
    "--window-height", "$WindowHeight",
    "--login-zoom-percent", "$LoginZoomPercent",
    "--video-speed", "$VideoSpeed",
    "--video-max-wait-seconds", "$VideoMaxWaitSeconds",
    "--pause-for-quiz"
  )

  if ($CourseUrl -and $CourseUrl.Trim().Length -gt 0) {
    $runArgs += @("--course-url", "$CourseUrl")
  }
  if ($StrictSequence) {
    $runArgs += @("--strict-sequence")
  }
  if ($SkipAssessments) {
    $runArgs += @("--skip-assessments")
  }
  if ($AutoRunToEnd) {
    $runArgs += @(
      "--auto-run-to-end",
      "--no-pause-for-quiz",
      "--timer-lock-retry-seconds", "$TimerLockRetrySeconds",
      "--timer-lock-max-retries", "$TimerLockMaxRetries",
      "--timer-lock-max-wait-seconds", "$TimerLockMaxWaitSeconds"
    )
  }

  python @runArgs
}
finally {
  Pop-Location
}
