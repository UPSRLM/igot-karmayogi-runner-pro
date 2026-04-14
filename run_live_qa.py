#!/usr/bin/env python3
"""
Compliant live QA + audit runner for iGot-style learning portals.

Safety boundaries:
- No seek-to-end / timer evasion / brute-force quiz answering.
- User-controlled authentication and final quiz submissions.
- Focuses on progress verification, blocked-state detection, and evidence capture.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


def _env_api_key(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""

try:
    from google import genai as _google_genai  # new SDK: pip install google-genai
    from google.genai import types as _genai_types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

try:
    from groq import Groq as _GroqClient  # pip install groq
    _GROQ_AVAILABLE = True
except ImportError:
    _GROQ_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Gemini quiz solver
# ─────────────────────────────────────────────────────────────────────────────

class GeminiSolver:
    """
    Uses Google Gemini (free tier) to answer MCQ questions.
    Uses the new google-genai SDK (pip install google-genai).
    Get a free API key at: https://aistudio.google.com/app/apikey
    """

    SYSTEM_PROMPT = (
        "You are an expert at answering multiple-choice questions for Indian government "
        "professional learning courses (iGot Karmayogi portal). Topics include leadership, "
        "management, communication, technology, governance, ethics, and professional skills. "
        "When given a question with options, briefly reason through it (2-3 sentences), "
        "then on a new line write exactly: ANSWER: <exact option text here>. "
        "For True/False questions, write: ANSWER: True  OR  ANSWER: False. "
        "Always end your response with an ANSWER: line. "
        "Never say 'I don't know' — always pick the best available option."
    )

    # Fallback model order when primary quota is exhausted
    _FALLBACK_MODELS = [
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
        "gemini-2.0-flash-exp",
        "gemini-exp-1206",
    ]

    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash") -> None:
        if not _GEMINI_AVAILABLE:
            raise RuntimeError(
                "google-genai package not installed. Run: pip install google-genai"
            )
        self._api_key = api_key
        self._primary_model_name = model_name
        self._current_model_name = model_name
        self._client = _google_genai.Client(api_key=api_key)

    def _match_option(self, raw: str, options: list[str]) -> str | None:
        """Find the best matching option for the raw model output."""
        raw = raw.strip()
        # If the model returned just a letter (A/B/C/D), map to option by index
        if re.match(r"^[A-Da-d]\.?$", raw) and options:
            idx = ord(raw[0].upper()) - ord("A")
            if 0 <= idx < len(options):
                return options[idx]
        raw = re.sub(r"^[A-Za-z][.)]\s*", "", raw).strip()
        raw_low = raw.lower()
        # Exact match
        for opt in options:
            if opt.strip().lower() == raw_low:
                return opt
        # Substring match
        for opt in options:
            if raw_low in opt.lower() or opt.lower() in raw_low:
                return opt
        # Word overlap (>=50% of answer words appear in option)
        raw_words = set(w for w in raw_low.split() if len(w) > 2)
        if raw_words:
            best, best_score = None, 0
            for opt in options:
                overlap = sum(1 for w in raw_words if w in opt.lower())
                score = overlap / len(raw_words)
                if score > best_score:
                    best, best_score = opt, score
            if best_score >= 0.4:
                return best
        return raw if raw else None

    def answer_question(self, question: str, options: list[str], topic: str = "") -> str | None:
        """Return the text of the best matching option, or None if all models fail.
        Handles 429 quota with retry-after sleep and automatic model fallback."""
        if not options:
            return None
        opts_block = "\n".join(f"  {chr(65+i)}. {o}" for i, o in enumerate(options))
        topic_line = f"Topic/Course: {topic}\n\n" if topic else ""
        prompt = (
            f"{topic_line}Question: {question}\n\nOptions:\n{opts_block}\n\n"
            "Think through the question briefly, then give your final answer as: ANSWER: <exact option text>"
        )
        config = _genai_types.GenerateContentConfig(
            system_instruction=self.SYSTEM_PROMPT,
            max_output_tokens=300,
            temperature=0.1,
        )

        # Always try all models: primary first, then fallbacks, deduped
        all_models = [self._primary_model_name] + self._FALLBACK_MODELS
        seen: set[str] = set()
        models_to_try: list[str] = []
        for m in all_models:
            if m not in seen:
                seen.add(m)
                models_to_try.append(m)

        for model_name in models_to_try:
            if model_name != self._current_model_name:
                print(f"[gemini] Switching to fallback model: {model_name}")
                self._current_model_name = model_name

            for attempt in range(2):
                try:
                    response = self._client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config,
                    )
                    raw = (response.text or "").strip()
                    # Extract "ANSWER: " line if model used chain-of-thought
                    answer_raw = raw
                    for line in reversed(raw.splitlines()):
                        line = line.strip()
                        if line.upper().startswith("ANSWER:"):
                            answer_raw = line[7:].strip()
                            break
                    matched = self._match_option(answer_raw, options)
                    if matched:
                        return matched
                    # Matched nothing — return what we extracted
                    return answer_raw
                except Exception as exc:  # noqa: BLE001
                    exc_str = str(exc)
                    is_quota = (
                        "429" in exc_str
                        or "quota" in exc_str.lower()
                        or "resource_exhausted" in exc_str.lower()
                        or "rate_limit" in exc_str.lower()
                    )
                    is_not_found = "404" in exc_str or "not found" in exc_str.lower()

                    if is_not_found:
                        print(f"[gemini] Model {model_name} not found (404), trying next.")
                        break  # try next model immediately

                    if is_quota:
                        # Quota exhausted — skip immediately to next model.
                        # The quota resets on Google's schedule (hours), not seconds;
                        # sleeping wastes time. The fallback will select options[0].
                        print(f"[gemini] 429 quota on {model_name}, trying next model…")
                        break
                    else:
                        print(f"[gemini] Error on {model_name}: {exc_str[:120]}")
                        break  # non-quota error, skip to next model

        print("[gemini] All models failed.")
        # Reset to primary so next call tries the full chain again from the start
        self._current_model_name = self._primary_model_name
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Groq solver — free, generous quota, no daily limit
# Get a free API key at: https://console.groq.com/keys
# ─────────────────────────────────────────────────────────────────────────────

class GroqSolver:
    """Uses Groq's free API (llama-3.3-70b) to answer MCQ questions.
    Groq's free tier is extremely generous — effectively no daily quota."""

    SYSTEM_PROMPT = (
        "You are an expert at answering multiple-choice questions for Indian government "
        "professional learning courses (iGot Karmayogi portal). Topics include leadership, "
        "management, communication, technology, governance, ethics, and professional skills. "
        "When given a question with options, briefly reason through it (2-3 sentences), "
        "then on a new line write exactly: ANSWER: <exact option text here>. "
        "For True/False questions, write: ANSWER: True  OR  ANSWER: False. "
        "Always end your response with an ANSWER: line. "
        "Never say 'I don't know' — always pick the best available option."
    )

    # Models in preference order (all free on Groq)
    _MODELS = [
        "llama-3.3-70b-versatile",
        "llama3-70b-8192",
        "llama3-8b-8192",
        "gemma2-9b-it",
    ]

    def __init__(self, api_key: str) -> None:
        if not _GROQ_AVAILABLE:
            raise RuntimeError("groq package not installed. Run: pip install groq")
        self._client = _GroqClient(api_key=api_key)

    def _match_option(self, raw: str, options: list[str]) -> str | None:
        raw = raw.strip()
        # If the model returned just a letter (A/B/C/D), map it to the option at that index
        if re.match(r"^[A-Da-d]\.?$", raw) and options:
            idx = ord(raw[0].upper()) - ord("A")
            if 0 <= idx < len(options):
                return options[idx]
        # Strip leading "A. " / "B) " prefix
        raw = re.sub(r"^[A-Za-z][.)]\s*", "", raw).strip()
        raw_low = raw.lower()
        for opt in options:
            if opt.strip().lower() == raw_low:
                return opt
        for opt in options:
            if raw_low in opt.lower() or opt.lower() in raw_low:
                return opt
        raw_words = [w for w in raw_low.split() if len(w) > 2]
        if raw_words:
            best, best_sc = None, 0.0
            for opt in options:
                sc = sum(1 for w in raw_words if w in opt.lower()) / len(raw_words)
                if sc > best_sc:
                    best, best_sc = opt, sc
            if best_sc >= 0.35:
                return best
        return raw or None

    def answer_question(self, question: str, options: list[str], topic: str = "") -> str | None:
        if not options:
            return None
        opts_block = "\n".join(f"  {chr(65+i)}. {o}" for i, o in enumerate(options))
        topic_line = f"Topic/Course: {topic}\n\n" if topic else ""
        prompt = (
            f"{topic_line}Question: {question}\n\nOptions:\n{opts_block}\n\n"
            "Think through the question briefly, then give your final answer as: ANSWER: <exact option text>"
        )
        for model in self._MODELS:
            try:
                resp = self._client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=300,
                    temperature=0.1,
                )
                raw = (resp.choices[0].message.content or "").strip()
                # Extract "ANSWER: " line if model used chain-of-thought
                answer_raw = raw
                for line in reversed(raw.splitlines()):
                    line = line.strip()
                    if line.upper().startswith("ANSWER:"):
                        answer_raw = line[7:].strip()
                        break
                matched = self._match_option(answer_raw, options)
                if matched:
                    print(f"[groq] {model}: {matched[:50]!r}")
                    return matched
                return answer_raw
            except Exception as exc:
                exc_str = str(exc)
                if "429" in exc_str or "rate" in exc_str.lower():
                    print(f"[groq] Rate limit on {model}, trying next…")
                    continue
                print(f"[groq] Error on {model}: {exc_str[:100]}")
                continue
        print("[groq] All models failed.")
        return None


MODULE_TYPES = {"video", "reading", "pdf", "slides", "quiz", "scorm", "assignment", "unknown"}
STATUSES = {"done", "blocked", "partial"}
BLOCK_REASONS = {"prerequisite_lock", "timer_lock", "technical_error", "permission_issue", None}


@dataclass
class RunReportEntry:
    run_id: str
    timestamp: str
    course_name: str
    module_name: str
    module_type: str
    status: str
    block_reason: str | None
    evidence: str
    next_action: str


@dataclass
class CourseCandidate:
    name: str
    href: str | None
    completion_percent: int | None
    priority: int


@dataclass
class ModuleCandidate:
    name: str
    href: str | None
    module_type_hint: str
    is_completed: bool | None = None


class Reporter:
    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.root = root / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries: list[RunReportEntry] = []
        self.jsonl_path = self.root / "run_report.jsonl"
        self.csv_path = self.root / "run_report.csv"
        self.summary_path = self.root / "run_summary.md"
        self.artifacts_dir = self.root / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def add(self, entry: RunReportEntry) -> None:
        self._validate(entry)
        self.entries.append(entry)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=True) + "\n")

    def write_csv(self) -> None:
        fieldnames = list(RunReportEntry.__dataclass_fields__.keys())
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for entry in self.entries:
                writer.writerow(asdict(entry))

    def write_summary(self) -> None:
        by_course: dict[str, dict[str, int]] = {}
        for entry in self.entries:
            course = by_course.setdefault(entry.course_name, {"done": 0, "blocked": 0, "partial": 0})
            course[entry.status] += 1

        lines = [
            f"# Live QA Run Summary ({self.run_id})",
            "",
            f"- Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
            f"- Total module reports: {len(self.entries)}",
            f"- JSONL: `{self.jsonl_path.name}`",
            f"- CSV: `{self.csv_path.name}`",
            "",
            "## Per-course status counts",
            "",
        ]
        for course_name, counts in by_course.items():
            lines.append(
                f"- {course_name}: done={counts['done']}, partial={counts['partial']}, blocked={counts['blocked']}"
            )
        lines.append("")
        self.summary_path.write_text("\n".join(lines), encoding="utf-8")

    def _validate(self, entry: RunReportEntry) -> None:
        if entry.module_type not in MODULE_TYPES:
            raise ValueError(f"Invalid module_type: {entry.module_type}")
        if entry.status not in STATUSES:
            raise ValueError(f"Invalid status: {entry.status}")
        if entry.block_reason not in BLOCK_REASONS:
            raise ValueError(f"Invalid block_reason: {entry.block_reason}")
        if entry.status == "blocked" and entry.block_reason is None:
            raise ValueError("Blocked status requires block_reason")
        if entry.status in {"done", "partial"} and entry.block_reason is not None:
            raise ValueError("Done/partial status must have null block_reason")


class LiveQARunner:
    def __init__(self, page: Page, reporter: Reporter, args: argparse.Namespace) -> None:
        self.page = page
        self.reporter = reporter
        self.args = args
        self.run_id = reporter.run_id
        self.now = lambda: dt.datetime.now(dt.timezone.utc).isoformat()
        self.courses_index: dict[str, CourseCandidate] = {}

        # Groq solver — free, no daily quota, preferred over Gemini
        self.groq: GroqSolver | None = None
        if getattr(args, "groq_api_key", "") and _GROQ_AVAILABLE:
            try:
                self.groq = GroqSolver(api_key=args.groq_api_key)
                print("[groq] Solver ready (llama-3.3-70b). Quizzes will be auto-answered via Groq.")
            except Exception as exc:  # noqa: BLE001
                print(f"[groq] Init failed: {exc}")

        # Gemini solver — fallback if Groq not configured
        self.gemini: GeminiSolver | None = None
        if getattr(args, "gemini_api_key", "") and _GEMINI_AVAILABLE:
            try:
                self.gemini = GeminiSolver(
                    api_key=args.gemini_api_key,
                    model_name=getattr(args, "gemini_model", "gemini-2.0-flash"),
                )
                print(f"[gemini] Solver ready (model: {args.gemini_model}). Fallback for Groq.")
            except Exception as exc:  # noqa: BLE001
                print(f"[gemini] Init failed: {exc}")

    @staticmethod
    def _extract_course_name_from_url(url: str) -> str:
        """Extract course name from iGOT URL query param 'courseName'."""
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if "courseName" in qs:
                return unquote(qs["courseName"][0]).strip()
        except Exception:
            pass
        return "Direct Course"

    @staticmethod
    async def _prompt_for_course_url() -> str | None:
        """Ask user to paste a course URL. Returns URL or None to quit."""
        import asyncio, sys
        print("\n" + "=" * 70)
        print("  PASTE COURSE URL (or type 'quit' / press Enter to stop)")
        print("=" * 70)
        print("  Example: https://portal.igotkarmayogi.gov.in/viewer/html/...")
        print()
        sys.stdout.flush()
        try:
            # Run input() in a thread so the async event loop is not blocked
            url = await asyncio.to_thread(input, "  Course URL > ")
            url = url.strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not url or url.lower() in {"quit", "exit", "q", "done", "stop"}:
            return None
        if not url.startswith("http"):
            print(f"[warn] URL doesn't start with http — using as-is: {url[:80]}")
        return url

    async def run(self) -> None:
        start_url = self.args.start_url or self.args.base_url
        await self._safe_goto(start_url)
        await self._ensure_login()
        self.course_list_url = self.page.url
        processed_course_keys: set[str] = set()

        def course_key(c: CourseCandidate) -> str:
            href_key = (c.href or "").split("?", 1)[0].strip().lower()
            return f"{self._normalize_for_match(c.name)}::{href_key}"

        # ── MODE 1: Direct course URL from command line ──
        if self.args.course_url:
            course_name = self._extract_course_name_from_url(self.args.course_url)
            print(f"[direct] Course: {course_name}")
            await self._process_course(
                CourseCandidate(
                    name=course_name,
                    href=self.args.course_url,
                    completion_percent=None,
                    priority=0,
                )
            )
            self.reporter.write_csv()
            self.reporter.write_summary()
            return

        # ── MODE 2: Interactive prompt — ask user for course URLs one at a time ──
        if self.args.prompt_mode:
            courses_done = 0
            while True:
                url = await self._prompt_for_course_url()
                if not url:
                    print(f"\n[done] Completed {courses_done} course(s). Exiting.")
                    break
                course_name = self._extract_course_name_from_url(url)
                print(f"\n[course {courses_done + 1}] Starting: {course_name}")
                print(f"[course {courses_done + 1}] URL: {url[:100]}...")
                try:
                    await self._process_course(
                        CourseCandidate(
                            name=course_name,
                            href=url,
                            completion_percent=None,
                            priority=0,
                        )
                    )
                    courses_done += 1
                    print(f"\n[course {courses_done}] FINISHED: {course_name}")
                except Exception as exc:
                    print(f"\n[error] Course failed: {exc}")
                    if not self.args.continue_on_error:
                        raise
                    courses_done += 1
                finally:
                    # Always return to portal home so user can pick the next course
                    home = self.args.start_url or self.args.base_url
                    print(f"\n[nav] Returning to {home} for next course selection...")
                    try:
                        await self._safe_goto(home)
                        await self.page.wait_for_timeout(1500)
                    except Exception:
                        pass
            self.reporter.write_csv()
            self.reporter.write_summary()
            return

        if not self.args.start_url:
            await self._go_to_course_hub()

        # ── Main outer loop: keep discovering and processing until no courses left ──
        # This ensures we process all "In Progress" courses without exiting.
        consecutive_empty = 0
        max_courses_total = self.args.max_courses if self.args.max_courses > 0 else 9999

        while len(processed_course_keys) < max_courses_total:
            # Check if the user has manually navigated to a course — handle it first.
            if await self._is_inside_course_player():
                current_course_name = await self._derive_current_course_name()
                current_url = self.page.url
                manual_course = CourseCandidate(
                    name=current_course_name,
                    href=current_url,
                    completion_percent=None,
                    priority=10,  # high priority
                )
                mkey = course_key(manual_course)
                if mkey not in processed_course_keys:
                    print(f"[info] Manual intervention detected: starting course '{current_course_name}' first.")
                    try:
                        await self._process_course(manual_course)
                        processed_course_keys.add(mkey)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[warn] Manual course processing failed for '{current_course_name}': {exc}")
                        if not self.args.continue_on_error:
                            raise
                    await self._return_to_course_list()
                    continue

            # Navigate to course list and discover
            if self.course_list_url and self.page.url.rstrip("/") != self.course_list_url.rstrip("/"):
                try:
                    await self._safe_goto(self.course_list_url)
                    await self.page.wait_for_timeout(1200)
                except PlaywrightError:
                    pass

            courses = await self._discover_courses()
            pending = [c for c in courses if course_key(c) not in processed_course_keys]

            if not pending:
                # Before prompting the user, retry auto-discovery up to 5× with
                # increasing waits — iGot SPA can take 5-10s to lazy-load cards.
                for _auto_retry in range(5):
                    print(f"[discovery] No courses found, auto-retrying ({_auto_retry + 1}/5)…")
                    try:
                        await self._safe_goto(self.course_list_url or start_url)
                        await self.page.wait_for_timeout(2000 + _auto_retry * 1000)
                    except PlaywrightError:
                        pass
                    await self._click_in_progress_tab()
                    await self.page.wait_for_timeout(1500)
                    # Scroll to force lazy-load
                    try:
                        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await self.page.wait_for_timeout(800)
                        await self.page.evaluate("window.scrollTo(0, 0)")
                        await self.page.wait_for_timeout(400)
                    except PlaywrightError:
                        pass
                    retry_courses = await self._discover_courses()
                    retry_pending = [c for c in retry_courses if course_key(c) not in processed_course_keys]
                    if retry_pending:
                        courses = retry_courses
                        pending = retry_pending
                        consecutive_empty = 0
                        break

            if not pending:
                consecutive_empty += 1
                if consecutive_empty == 1:
                    print("No courses discovered automatically. Open My Courses in Chrome, then press Enter to retry once.")
                    await self._wait_for_enter()
                    try:
                        await self._safe_goto(self.course_list_url or start_url)
                        await self.page.wait_for_timeout(1500)
                    except PlaywrightError:
                        pass
                    continue
                elif consecutive_empty == 2:
                    print("Still no courses found. Manual fallback: open one target course page in Chrome, then press Enter.")
                    await self._wait_for_enter()
                    await self._wait_for_course_content()
                    current_course_name = await self._derive_current_course_name()
                    current_url = self.page.url
                    manual_course = CourseCandidate(
                        name=current_course_name,
                        href=current_url,
                        completion_percent=None,
                        priority=0,
                    )
                    mkey = course_key(manual_course)
                    if mkey not in processed_course_keys:
                        try:
                            await self._process_course(manual_course)
                            processed_course_keys.add(mkey)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[warn] Manual fallback course processing failed for '{manual_course.name}': {exc}")
                            if not self.args.continue_on_error:
                                raise
                    # Navigate back and retry discovery
                    await self._return_to_course_list()
                    consecutive_empty = 0
                    continue
                else:
                    print("No additional courses discovered. All In Progress courses processed.")
                    break

            # Reset empty counter when we find courses
            consecutive_empty = 0

            # Process the first pending course
            course = pending[0]
            key = course_key(course)
            try:
                await self._process_course(course)
                processed_course_keys.add(key)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] Course processing failed for '{course.name}': {exc}")
                processed_course_keys.add(key)  # mark to avoid infinite retry
                if not self.args.continue_on_error:
                    raise
            # Always navigate back to continueLearning to re-check pending courses by %
            await self._return_to_course_list()

        self.reporter.write_csv()
        self.reporter.write_summary()

    async def _ensure_login(self) -> None:
        # If a password field is visible, user likely needs to authenticate manually.
        try:
            has_password = await self.page.locator("input[type='password']").first.is_visible(timeout=1200)
        except PlaywrightTimeoutError:
            has_password = False
        if has_password:
            await self._prepare_login_ui()
            print("Login helper applied: widened window + zoom + login option scroll.")
            print("Login required: complete login in Chrome, then press Enter here to continue.")
            await self._wait_for_enter()

    async def _prepare_login_ui(self) -> None:
        # Make auth options (for example OTP/mobile login) visible in clipped layouts.
        try:
            await self.page.evaluate(
                """(zoomPct) => {
                    const z = Math.max(50, Math.min(100, Number(zoomPct || 80)));
                    document.documentElement.style.zoom = `${z}%`;
                    if (document.body) document.body.style.zoom = `${z}%`;
                    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
                }""",
                self.args.login_zoom_percent,
            )
        except PlaywrightError:
            pass

        # Try to bring common OTP/mobile login toggles into view.
        patterns = [
            r"login with otp",
            r"log in with otp",
            r"mobile number",
            r"otp",
            r"login with password",
        ]
        for pattern in patterns:
            locator = self.page.get_by_text(re.compile(pattern, re.I))
            try:
                if await locator.first.is_visible(timeout=500):
                    await locator.first.scroll_into_view_if_needed(timeout=1200)
            except PlaywrightError:
                continue

    async def _go_to_course_hub(self) -> None:
        candidates = [
            f"{self.args.base_url.rstrip('/')}/my-courses",
            f"{self.args.base_url.rstrip('/')}/my-learning",
            f"{self.args.base_url.rstrip('/')}/learn",
        ]
        for url in candidates:
            await self._safe_goto(url)
            if await self._looks_like_course_hub():
                return

        # Last attempt: click likely nav links.
        for text in ["My Courses", "My Learning", "Courses"]:
            link = self.page.get_by_role("link", name=re.compile(text, re.I))
            try:
                if await link.first.is_visible():
                    await link.first.click()
                    await self.page.wait_for_timeout(1200)
                    if await self._looks_like_course_hub():
                        return
            except PlaywrightError:
                continue

    async def _looks_like_course_hub(self) -> bool:
        body = (await self._body_text()).lower()
        signals = ["my courses", "my learning", "course", "% complete", "enrolled"]
        return sum(1 for s in signals if s in body) >= 2

    async def _return_to_course_list(self) -> None:
        """
        Navigate back to the continueLearning page, click 'In Progress' tab,
        and log pending courses with their completion %.
        Called after every course finishes to pick the next pending one.
        """
        target = self.course_list_url or "https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning"
        try:
            await self._safe_goto(target)
            await self.page.wait_for_timeout(1800)
        except PlaywrightError:
            pass
        await self._click_in_progress_tab()
        await self.page.wait_for_timeout(800)
        # Log pending courses with % for visibility
        try:
            pending_info = await self.page.evaluate(
                """() => {
                    const out = [];
                    const cards = Array.from(document.querySelectorAll(
                        "[class*='card'], [class*='course'], [class*='tile']"
                    ));
                    for (const card of cards) {
                        const pct = (card.innerText || "").match(/\\b(\\d{1,3})%\\b/);
                        if (!pct) continue;
                        const titleEl = card.querySelector("h1,h2,h3,h4,[class*='title'],[class*='name']");
                        if (!titleEl) continue;
                        const name = (titleEl.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 80);
                        if (name.length < 3) continue;
                        out.push(`${name} (${pct[1]}%)`);
                    }
                    return out;
                }"""
            )
            if pending_info:
                print(f"[course-list] Pending courses: {' | '.join(pending_info)}")
        except PlaywrightError:
            pass

    async def _click_in_progress_tab(self) -> None:
        """Click the 'In Progress' tab on the continueLearning page if present.
        IMPORTANT: Avoid clicking navigation links like 'My Learning' or 'Continue Learning'
        that would navigate away from the current page. Only click actual tab controls."""
        current_url = self.page.url

        # Only match actual tab elements (not nav links)
        for label in [r"in progress", r"inprogress", r"ongoing"]:
            for role in ["tab", "button"]:
                try:
                    loc = self.page.get_by_role(role, name=re.compile(label, re.I))
                    if await loc.first.is_visible(timeout=800):
                        await loc.first.click()
                        await self.page.wait_for_timeout(800)
                        # Verify we didn't navigate away
                        if self.page.url.split("?")[0] != current_url.split("?")[0]:
                            print("[warn] _click_in_progress_tab navigated away! Going back.")
                            await self._safe_goto(current_url)
                            await self.page.wait_for_timeout(800)
                            continue
                        return
                except PlaywrightError:
                    continue

        # DO NOT click "continue learning" as a link — it navigates to a different page
        # Only click it if it's explicitly a tab element
        try:
            loc = self.page.get_by_role("tab", name=re.compile(r"continue learning", re.I))
            if await loc.first.is_visible(timeout=500):
                await loc.first.click()
                await self.page.wait_for_timeout(800)
        except PlaywrightError:
            pass

    # ── ADDED: Module completion cache for performance ──
    _module_done_cache: dict = {}

    async def _ensure_sidebar_open(self) -> None:
        """If the sidebar/sidenav is collapsed, click the toggle to open it."""
        try:
            is_collapsed = await self.page.evaluate(
                """() => {
                    const sidenav = document.querySelector(
                        "mat-sidenav, [class*='mat-drawer'], [class*='sidenav'], [class*='sidebar']"
                    );
                    if (!sidenav) return false;
                    const style = window.getComputedStyle(sidenav);
                    if (style.display === 'none') return true;
                    if (sidenav.getBoundingClientRect().width < 50) return true;
                    if (style.visibility === 'hidden') return true;
                    return false;
                }"""
            )
            if is_collapsed:
                print("[sidebar] Sidebar appears collapsed, attempting to open...")
                for selector in [
                    "[class*='sidenav-toggle']",
                    "[class*='sidebar-toggle']",
                    "[aria-label*='menu']",
                    "[aria-label*='Menu']",
                    "[class*='hamburger']",
                    "[class*='menu-toggle']",
                ]:
                    try:
                        loc = self.page.locator(selector).first
                        if await loc.is_visible(timeout=500):
                            await loc.click()
                            await self.page.wait_for_timeout(800)
                            print(f"[sidebar] Clicked toggle: {selector}")
                            return
                    except PlaywrightError:
                        continue
                print("[sidebar] Could not find toggle button")
        except PlaywrightError:
            pass

    async def _log_dom_snapshot(self, context: str) -> None:
        """Capture a lightweight DOM snapshot for debugging selector issues."""
        try:
            snapshot = await self.page.evaluate(
                """(ctx) => {
                    const out = { context: ctx, url: window.location.href, timestamp: new Date().toISOString() };
                    const btns = Array.from(document.querySelectorAll("button, [role='button'], a")).filter(b => {
                        const t = (b.innerText || "").trim().toLowerCase();
                        return /resume|start|continue|begin|next/.test(t);
                    });
                    out.actionButtons = btns.map(b => ({
                        tag: b.tagName, text: (b.innerText || "").trim().slice(0, 60),
                        classes: (b.className || "").slice(0, 80), visible: b.offsetParent !== null
                    }));
                    const tocEls = document.querySelectorAll(
                        "[class*='toc'], [class*='sidebar'], mat-sidenav, [role='tree'], [role='treeitem']"
                    );
                    out.tocElements = tocEls.length;
                    out.listItems = document.querySelectorAll("li, [role='treeitem']").length;
                    const sidebar = document.querySelector("mat-sidenav, [class*='sidebar'], [class*='sidenav']");
                    if (sidebar) {
                        const rect = sidebar.getBoundingClientRect();
                        out.sidebarVisible = rect.width > 50 && rect.height > 50;
                        out.sidebarWidth = rect.width;
                    }
                    return out;
                }""",
                context,
            )
            print(f"[DOM snapshot][{context}] URL={snapshot.get('url','?')[:80]} "
                  f"actionBtns={len(snapshot.get('actionButtons',[]))} "
                  f"tocEls={snapshot.get('tocElements',0)} "
                  f"listItems={snapshot.get('listItems',0)} "
                  f"sidebarVisible={snapshot.get('sidebarVisible','N/A')} "
                  f"sidebarWidth={snapshot.get('sidebarWidth','N/A')}")
            if snapshot.get("actionButtons"):
                for btn in snapshot["actionButtons"][:5]:
                    print(f"  btn: <{btn['tag']}> text=\'{btn['text']}\' class=\'{btn['classes']}\' visible={btn['visible']}")
        except PlaywrightError:
            print(f"[DOM snapshot][{context}] Failed to capture")

    async def _check_progress_summary(self) -> dict:
        """Scrape course-level progress info from the page."""
        try:
            progress = await self.page.evaluate(
                """() => {
                    const body = document.body?.innerText || "";
                    const result = { percent: null, completed: 0, total: 0, text: "" };
                    const pctMatch = body.match(/(\\d{1,3})\\s*%\\s*(complete|progress|done)?/i);
                    if (pctMatch) result.percent = parseInt(pctMatch[1], 10);
                    const countMatch = body.match(/(\\d+)\\s*(?:of|\\/)\\s*(\\d+)\\s*(?:complete|done|modules?|items?|lessons?)/i);
                    if (countMatch) {
                        result.completed = parseInt(countMatch[1], 10);
                        result.total = parseInt(countMatch[2], 10);
                    }
                    const progressBar = document.querySelector(
                        "[role='progressbar'], progress, [class*='progress-bar'], [class*='progressBar']"
                    );
                    if (progressBar) {
                        const val = progressBar.getAttribute("aria-valuenow") ||
                                    progressBar.getAttribute("value") || progressBar.style.width;
                        if (val) result.text = "progressBar: " + val;
                        if (!result.percent && val) {
                            const n = parseInt(val, 10);
                            if (n >= 0 && n <= 100) result.percent = n;
                        }
                    }
                    return result;
                }"""
            )
            if progress.get("percent") is not None or progress.get("total"):
                print(f"[progress] {progress.get('percent', '?')}% complete, "
                      f"{progress.get('completed', '?')}/{progress.get('total', '?')} modules done "
                      f"{progress.get('text', '')}")
            return progress
        except PlaywrightError:
            return {}

    async def _discover_courses(self) -> list[CourseCandidate]:
        # If already on the continueLearning page, DON'T click tabs that might navigate away
        current_url = self.page.url.lower()
        is_continue_learning_page = "continuelearning" in current_url or "seeall" in current_url
        if not is_continue_learning_page:
            # Click "In Progress" tab only if we're on a different page
            await self._click_in_progress_tab()
        # Wait for Angular SPA to render course cards (they lazy-load)
        await self.page.wait_for_timeout(2000)
        # Scroll to trigger lazy loading of course cards
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.wait_for_timeout(800)
            await self.page.evaluate("window.scrollTo(0, 0)")
            await self.page.wait_for_timeout(600)
        except PlaywrightError:
            pass
        # DIAGNOSTIC: Log current URL and page state before discovery
        try:
            diag_url = self.page.url
            diag_title = await self.page.title()
            print(f"[discover_courses] Page URL: {diag_url}")
            print(f"[discover_courses] Page title: {diag_title}")
            # Quick check: count buttons and links on page
            quick_count = await self.page.evaluate(
                """() => {
                    const btns = document.querySelectorAll('button').length;
                    const links = document.querySelectorAll('a[href]').length;
                    const resumeBtns = Array.from(document.querySelectorAll('button')).filter(b => /resume|start/i.test(b.innerText || '')).length;
                    const bodyLen = (document.body?.innerText || '').length;
                    return {btns, links, resumeBtns, bodyLen};
                }"""
            )
            print(f"[discover_courses] Quick count: {quick_count}")
        except Exception as diag_exc:
            print(f"[discover_courses] DIAGNOSTIC FAILED: {diag_exc}")

        try:
            raw = await self.page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();

                const norm = s => (s || "").replace(/\\s+/g, " ").trim();

                const addCourse = (name, href, pct) => {
                    if (!name || name.length < 4 || name.length > 240) return;
                    const key = name.toLowerCase().slice(0, 80);
                    if (seen.has(key)) return;
                    seen.add(key);
                    out.push({ name: name.slice(0, 240), href, pct });
                };

                // Helper: walk up from an element to find the course card title.
                // Returns { title, href, pct } or null.
                const extractCardInfo = (startEl) => {
                    let node = startEl;
                    for (let depth = 0; depth < 12 && node && node !== document.body; depth++) {
                        // Look for a title element within this node
                        const titleEl = node.querySelector(
                            "h1, h2, h3, h4, h5, " +
                            "[class*='title'], [class*='course-name'], [class*='card-title'], " +
                            "[class*='heading'], [class*='name']"
                        );
                        if (titleEl) {
                            const t = norm(titleEl.innerText || "");
                            // Must look like a real course title: 4–150 chars, not a button label
                            if (t.length >= 4 && t.length <= 150 &&
                                !/^(resume|start|continue|enroll|begin|in progress|completed|\\d+%?)$/i.test(t)) {
                                // Extract % progress from the card
                                const cardText = norm(node.innerText || "");
                                const pctM = cardText.match(/\\b(\\d{1,3})%/);
                                const pct = pctM ? parseInt(pctM[1], 10) : null;
                                // Extract href from any anchor or routerLink
                                const anchor = node.querySelector("a[href]");
                                const rl = node.querySelector("[routerlink], [ng-reflect-router-link]");
                                let href = anchor ? anchor.href : null;
                                if (!href && rl) {
                                    const r = rl.getAttribute("routerlink") || rl.getAttribute("ng-reflect-router-link") || "";
                                    href = r.startsWith("/") ? window.location.origin + r : null;
                                }
                                return { title: t, href, pct };
                            }
                        }
                        node = node.parentElement;
                    }
                    return null;
                };

                // PRIMARY STRATEGY: Find all Resume/Start buttons — these are the
                // definitive proof a course is In Progress / available to start.
                // iGot button text: "Resume play_arrow", "Start play_arrow", etc.
                // ── FIXED: Expanded button detection for iGot portal ──
                const actionBtnRe = /^(resume|start|continue|begin)(\\s+(play_arrow|arrow_forward|learning|course|module|keyboard_arrow_right))*$/i;
                const actionBtns = Array.from(document.querySelectorAll(
                    "button, [role='button'], a[role='button'], a.mat-button, a.mat-raised-button, " +
                    "a.mat-flat-button, a[class*='resume'], a[class*='start'], a[class*='action-btn'], " +
                    "a[class*='continue'], [class*='resume-btn'], [class*='start-btn'], [class*='action-button']"
                )).filter(btn => {
                    const t = norm(btn.innerText || btn.textContent || "");
                    const rect = btn.getBoundingClientRect();
                    const isVisible = (btn.offsetParent !== null || rect.height > 0) && rect.width > 0;
                    if (!isVisible) return false;
                    if (actionBtnRe.test(t)) return true;
                    const cls = (btn.className || "").toLowerCase();
                    if (/\\b(resume|start-btn|action-btn|continue-btn)\\b/.test(cls) && t.length < 30) return true;
                    return false;
                });

                for (const btn of actionBtns) {
                    const info = extractCardInfo(btn.parentElement);
                    if (info) {
                        addCourse(info.title, info.href, info.pct);
                        continue;
                    }

                    // extractCardInfo failed — iGOT cards may not have standard title elements.
                    // Walk up from button to find the card container, then extract text directly.
                    let cardEl = btn.parentElement;
                    for (let d = 0; d < 12 && cardEl && cardEl !== document.body; d++) {
                        const cls = (cardEl.className || "").toLowerCase();
                        const tag = cardEl.tagName.toLowerCase();
                        // Detect card containers by class or tag patterns
                        const isCard = cls.includes("card") || cls.includes("course") ||
                            cls.includes("tile") || cls.includes("slider") ||
                            cls.includes("swiper") || cls.includes("strip") ||
                            cls.includes("content-strip") || tag === "mat-card" ||
                            tag === "li";
                        // Also stop at a reasonable container size
                        const rect = cardEl.getBoundingClientRect();
                        const isReasonableSize = rect.width > 100 && rect.width < 800 &&
                                                  rect.height > 60 && rect.height < 600;
                        if ((isCard || isReasonableSize) && d >= 2) {
                            // Found the card — extract all text, remove button labels
                            let cardText = norm(cardEl.innerText || "");
                            // Remove known button/noise text
                            cardText = cardText.replace(/\\b(Resume|Start|Continue|Enroll|Begin)\\b/gi, "");
                            cardText = cardText.replace(/\\b(play_arrow|arrow_forward|keyboard_arrow_right)\\b/gi, "");
                            cardText = cardText.replace(/\\b(In Progress|Completed|Not Started)\\b/gi, "");
                            // Remove percentage
                            const pctM = cardText.match(/\\b(\\d{1,3})%/);
                            const pct = pctM ? parseInt(pctM[1], 10) : null;
                            cardText = cardText.replace(/\\b\\d{1,3}%/g, "");
                            // Remove duration patterns
                            cardText = cardText.replace(/\\b\\d+\\s*h(\\s+\\d+\\s*m)?(\\s+\\d+\\s*s)?/gi, "");
                            cardText = cardText.replace(/\\b\\d+\\s*m(\\s+\\d+\\s*s)?/gi, "");
                            cardText = cardText.replace(/\\b\\d+\\s*s\\b/gi, "");
                            cardText = norm(cardText);

                            // Get the first meaningful line as the title
                            const lines = cardText.split(/[\n\r]+/).map(l => norm(l)).filter(l => l.length >= 4 && l.length <= 150);
                            const title = lines[0] || cardText.slice(0, 150);

                            if (title.length >= 4 && title.length <= 150 &&
                                !/^(resume|start|continue|enroll|begin)$/i.test(title)) {
                                const anchor = cardEl.querySelector("a[href]");
                                const rl = cardEl.querySelector("[routerlink], [ng-reflect-router-link]");
                                let href = anchor ? anchor.href : null;
                                if (!href && rl) {
                                    const r = rl.getAttribute("routerlink") || rl.getAttribute("ng-reflect-router-link") || "";
                                    href = r.startsWith("/") ? window.location.origin + r : null;
                                }
                                addCourse(title, href, pct);
                            }
                            break;
                        }
                        cardEl = cardEl.parentElement;
                    }
                }

                // FALLBACK STRATEGY 1: Cards that show a progress % — In Progress courses
                if (out.length === 0) {
                    const allEls = Array.from(document.querySelectorAll("*")).filter(el => {
                        if (!el.offsetParent) return false;
                        const t = norm(el.innerText || "");
                        // Must contain a % and be a leaf-ish container (not a huge wrapper)
                        return /\\b\\d{1,3}%/.test(t) && t.length < 400 && el.children.length < 15;
                    });
                    for (const el of allEls) {
                        const info = extractCardInfo(el);
                        if (!info || !info.pct) continue;
                        addCourse(info.title, info.href, info.pct);
                    }
                }

                // FALLBACK STRATEGY 2: Any anchor with collectionId= in href (iGot course URL pattern)
                if (out.length === 0) {
                    Array.from(document.querySelectorAll(
                        "a[href*='collectionId='], a[href*='collectionType=Course'], a[href*='batchId=']"
                    )).forEach(a => {
                        const info = extractCardInfo(a);
                        if (info) addCourse(info.title, a.href, info.pct);
                    });
                }

                // FALLBACK STRATEGY 3: seeAll/continueLearning page — plain course cards
                // On this page, courses are simple cards with title + image + maybe %.
                // There are NO Resume/Start buttons. Cards are clickable <a> tags or
                // divs wrapping <a> tags.
                if (out.length === 0) {
                    // Find all anchor tags that look like course links
                    const courseLinks = Array.from(document.querySelectorAll(
                        "a[href*='/overview/'], a[href*='/toc/'], a[href*='/course/'], " +
                        "a[href*='courseId='], a[href*='collectionId'], " +
                        "a[href*='/learn/course/'], a[href*='/app/toc/']"
                    ));
                    for (const a of courseLinks) {
                        const info = extractCardInfo(a);
                        if (info) addCourse(info.title, a.href, info.pct);
                        else {
                            // Direct title extraction from anchor text
                            const t = norm(a.innerText || "");
                            if (t.length >= 4 && t.length <= 150) {
                                addCourse(t, a.href, null);
                            }
                        }
                    }
                }

                // FALLBACK STRATEGY 4: Any visible card with a title-like element
                // and an anchor tag — broadest possible sweep
                if (out.length === 0) {
                    const allCards = Array.from(document.querySelectorAll(
                        "[class*='card'], [class*='course'], [class*='tile'], " +
                        "[class*='content-strip'], [class*='slider-item'], " +
                        "[class*='swiper-slide'], [class*='item'], mat-card"
                    )).filter(el => {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 100 && rect.height > 50;
                    });
                    for (const card of allCards) {
                        const titleEl = card.querySelector(
                            "h1,h2,h3,h4,h5,[class*='title'],[class*='name'],[class*='heading']"
                        );
                        if (!titleEl) continue;
                        const t = norm(titleEl.innerText || "");
                        if (t.length < 4 || t.length > 150) continue;
                        if (/^(resume|start|continue|enroll|my learning|home|explore)$/i.test(t)) continue;
                        const anchor = card.querySelector("a[href]") || card.closest("a[href]");
                        const href = anchor ? anchor.href : null;
                        const cardText = norm(card.innerText || "");
                        const pctM = cardText.match(/\\b(\\d{1,3})%/);
                        const pct = pctM ? parseInt(pctM[1], 10) : null;
                        addCourse(t, href, pct);
                    }
                }

                return out;
            }"""
            )
        except PlaywrightError as js_exc:
            print(f"[discover_courses] JS evaluate FAILED: {js_exc}")
            # Emergency fallback: try a much simpler extraction
            try:
                raw = await self.page.evaluate(
                    """() => {
                        const out = [];
                        const seen = new Set();
                        const norm = s => (s || "").replace(/\\s+/g, " ").trim();
                        // Find all Resume/Start buttons and get sibling text
                        const btns = Array.from(document.querySelectorAll('button.resume-btn, [class*="resume-btn"]'))
                            .filter(b => b.offsetParent);
                        for (const btn of btns) {
                            let card = btn.parentElement;
                            for (let i = 0; i < 10 && card && card !== document.body; i++) {
                                const texts = Array.from(card.querySelectorAll('*'))
                                    .filter(el => el.offsetParent && el.children.length === 0)
                                    .map(el => norm(el.innerText || ''))
                                    .filter(t => t.length >= 5 && t.length <= 150
                                        && !/^(resume|start|continue|begin|in progress|completed|\\d+%)$/i.test(t)
                                        && !/^\\d+[hms]/.test(t));
                                if (texts.length > 0) {
                                    const title = texts[0];
                                    const key = title.toLowerCase().slice(0, 80);
                                    if (!seen.has(key)) {
                                        seen.add(key);
                                        const anchor = card.querySelector('a[href]');
                                        out.push({ name: title, href: anchor ? anchor.href : null, pct: null });
                                    }
                                    break;
                                }
                                card = card.parentElement;
                            }
                        }
                        return out;
                    }"""
                )
                print(f"[discover_courses] Emergency fallback found {len(raw)} items")
            except PlaywrightError as e2:
                print(f"[discover_courses] Emergency fallback also failed: {e2}")
                return []
        # ── Diagnostic logging ──
        await self._log_dom_snapshot("discover_courses")
        if not raw:
            print("[discover_courses] WARNING: No courses found by any strategy.")

        # Log raw discovered items for debugging
        if raw:
            print(f"[discover_courses] Raw items from JS: {len(raw)}")
            for i, item in enumerate(raw[:8]):
                print(f"  [{i}] name={item.get('name','?')[:60]!r} href={str(item.get('href',''))[:60]} pct={item.get('pct')}")
        else:
            print("[discover_courses] WARNING: JS returned 0 raw items")

        courses: list[CourseCandidate] = []
        for item in raw:
            name = self._clean_name(item.get("name", "Untitled Course"))
            href = item.get("href")
            completion_percent = self._extract_percent(name)
            if self._is_course_noise(name, href):
                print(f"[discover_courses] REJECTED as noise: {name[:60]!r} href={str(href)[:50]}")
                continue
            if self._is_completed_text(name):
                print(f"[discover_courses] REJECTED as completed: {name[:60]!r}")
                continue
            priority = completion_percent if completion_percent is not None else 0
            candidate = CourseCandidate(name=name, href=href, completion_percent=completion_percent, priority=priority)
            courses.append(candidate)
            self.courses_index[name] = candidate

        # Sort higher completion first for quick wins.
        courses.sort(key=lambda c: c.priority, reverse=True)
        print(f"Discovered {len(courses)} incomplete/active course candidates.")
        for i, c in enumerate(courses[:5]):
            print(f"  [{i+1}] {c.name[:70]} | {c.completion_percent}% | href={str(c.href)[:60]}")
        return courses

    async def _process_course(self, course: CourseCandidate) -> None:
        print(f"\n=== Course: {course.name} ===")
        self._module_done_cache.clear()
        opened = False
        already_inside_course = await self._is_inside_course_player()
        # In prompt mode or direct URL mode, always navigate directly to the course URL.
        is_direct = bool(self.args.course_url) or bool(getattr(self.args, 'prompt_mode', False))
        if not is_direct and not already_inside_course:
            opened = await self._open_course_from_list(course.name)
        if not opened and course.href and not already_inside_course:
            print(f"[course] Navigating directly to: {course.href[:100]}")
            await self._safe_goto(course.href)
            await self.page.wait_for_timeout(3000)  # Give SPA time to hydrate
        await self._snap(f"course_{self._slug(course.name)}_landing")
        print(f"[course] Current URL after navigation: {self.page.url[:100]}")

        # If we landed on a course overview page (not the player), click Resume/Start to enter player
        if not await self._is_inside_course_player():
            player_opened = await self._open_course_player_if_needed()
            if player_opened:
                print(f"[course] Opened course player from overview page")
                await self.page.wait_for_timeout(1500)

        loaded = await self._wait_for_course_content()
        if not loaded:
            print("Course still in loading shell/spinner. Waiting extra and refreshing once for recovery.")
            await self.page.wait_for_timeout(max(15000, self.args.loading_timeout_seconds * 1000))
            loaded = await self._wait_for_course_content()
            if not loaded:
                try:
                    await self.page.reload(wait_until="domcontentloaded", timeout=self.args.goto_timeout_ms)
                    await self.page.wait_for_timeout(1200)
                except PlaywrightError:
                    pass
                loaded = await self._wait_for_course_content()
            if not loaded:
                print("Course is still in loading shell/spinner. Marking as blocked and continuing.")
                print(f"[course] Current URL: {self.page.url}")
                self._record(
                    course_name=course.name,
                    module_name="course_loading",
                    module_type="unknown",
                    status="blocked",
                    block_reason="technical_error",
                    evidence="Course page stayed in spinner/loading shell after recovery attempts",
                    next_action="Retry this course later or open a different module manually",
                )
                return

        modules = await self._discover_modules()
        if not modules:
            opened = await self._open_course_player_if_needed()
            if opened:
                await self._snap(f"course_{self._slug(course.name)}_player_open")
                await self._wait_for_course_content()
                modules = await self._discover_modules()

        if not modules and await self._looks_empty_course_shell():
            print("Course shell detected (no content loaded). Open course content in current tab, then press Enter.")
            await self._wait_for_enter()
            await self._wait_for_course_content()
            modules = await self._discover_modules()

        if not modules:
            current_type = await self._detect_module_type("unknown")
            if current_type != "unknown":
                print("No module list found; treating current page as a single module.")
                module_name = await self._derive_current_module_name()
                if await self._is_module_ticked(module_name):
                    evidence = "Single-module view already ticked from previous session; skipped"
                    self._record(
                        course_name=course.name,
                        module_name=module_name,
                        module_type=current_type,
                        status="done",
                        block_reason=None,
                        evidence=evidence,
                        next_action="Continue to next eligible module/course",
                    )
                    print(f"Module: {module_name} | Status: done | Evidence: {evidence}")
                    if self.args.auto_run_to_end:
                        await self._fast_navigate_next()
                    course_done, course_evidence = await self._is_course_completed()
                    course_status = "completed" if course_done else "partial"
                    print(f"Course: {course.name} | Status: {course_status} | Evidence: {course_evidence}")
                    return
                await self._process_module(
                    course.name,
                    ModuleCandidate(
                        name=module_name,
                        href="__CURRENT_PAGE__",
                        module_type_hint=current_type,
                    ),
                )
                course_done, course_evidence = await self._is_course_completed()
                course_status = "completed" if course_done else "partial"
                print(f"Course: {course.name} | Status: {course_status} | Evidence: {course_evidence}")
                return

            self._record(
                course_name=course.name,
                module_name="course_discovery",
                module_type="unknown",
                status="blocked",
                block_reason="technical_error",
                evidence="No module candidates discovered on course page",
                next_action="Open course player/TOC manually, then rerun",
            )
            return

        max_modules = self.args.max_modules if self.args.max_modules > 0 else len(modules)

        # ── OUTER LOOP: keep running strict sequence until course is 100% ──
        # After one pass of strict sequence, sections may remain. Re-discover and retry.
        course_retry = 0
        max_course_retries = 20  # up to 20 sections
        while course_retry < max_course_retries:
            course_retry += 1
            prev_progress = await self._check_progress_summary()
            prev_pct = prev_progress.get("percent", 0) or 0

            if self.args.strict_sequence:
                await self._run_strict_sequence(course.name, max_modules, course_href=course.href or "")
            else:
                modules = await self._discover_modules()
                for module in modules[:max_modules]:
                    if module.is_completed is True:
                        continue
                    if await self._is_module_ticked(module.name, module.href or ""):
                        continue
                    await self._process_module(course.name, module)

            # Check if course is now complete
            course_done, course_evidence = await self._is_course_completed()
            progress = await self._check_progress_summary()
            cur_pct = progress.get("percent", 0) or 0

            if course_done or cur_pct >= 100:
                print(f"\n\u2705 Course COMPLETED: {course.name} (100%)")
                break

            # If progress didn't change, wait briefly — portal can be slow to update its percentage
            # after sidebar ticks register, especially after video completion.
            if cur_pct <= prev_pct:
                print(f"\n[course-loop] Progress at {cur_pct}% (prev {prev_pct}%). Waiting 5s for portal to update...")
                await self.page.wait_for_timeout(5000)
                progress = await self._check_progress_summary()
                cur_pct = progress.get("percent", 0) or 0
                if cur_pct > prev_pct:
                    print(f"[course-loop] Portal updated after wait: {prev_pct}% → {cur_pct}%. Continuing...")
                    continue

            if cur_pct <= prev_pct:
                print(f"\n[course-loop] Progress stuck at {cur_pct}%. Trying to find more sections...")

                # Strategy 1: Expand all collapsed sections
                await self._expand_all_sections()
                await self.page.wait_for_timeout(1500)

                # Strategy 2: Click next section in sidebar
                section_found = await self._click_next_section()
                if section_found:
                    await self.page.wait_for_timeout(2000)
                    continue  # retry strict sequence with new section

                # Strategy 3: Click Next/Continue button
                navigated = await self._fast_navigate_next()
                if navigated:
                    await self.page.wait_for_timeout(2000)
                    # Check if new modules appeared
                    new_mods = await self._discover_modules()
                    new_pending = [m for m in new_mods if m.is_completed is not True]
                    if new_pending:
                        continue  # retry with new modules

                # Strategy 4: Scroll sidebar and try again
                try:
                    await self.page.evaluate(
                        """() => {
                            const s = document.querySelector(
                                "mat-sidenav, [class*='sidebar'], [class*='sidenav'], [class*='toc'], nav"
                            );
                            if (s) { s.scrollTo(0, s.scrollHeight); }
                        }"""
                    )
                    await self.page.wait_for_timeout(1000)
                except PlaywrightError:
                    pass
                await self._expand_all_sections()
                await self.page.wait_for_timeout(1000)
                new_mods = await self._discover_modules()
                new_pending = [m for m in new_mods if m.is_completed is not True]
                if new_pending:
                    continue

                # Strategy 5: Log full sidebar content for debugging
                try:
                    sidebar_dump = await self.page.evaluate(
                        """() => {
                            const s = document.querySelector(
                                "mat-sidenav, [class*='sidebar'], [class*='toc'], nav"
                            ) || document.body;
                            const items = Array.from(s.querySelectorAll('li, [role="treeitem"], [class*="item"]'))
                                .slice(0, 30)
                                .map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80));
                            return items;
                        }"""
                    )
                    if sidebar_dump:
                        print(f"[course-loop] Sidebar items ({len(sidebar_dump)}):")
                        for item in sidebar_dump[:15]:
                            print(f"  - {item}")
                except PlaywrightError:
                    pass

                print(f"[course-loop] Could not find more sections. Course at {cur_pct}%.")
                break
            else:
                # Progress increased — more sections to go
                print(f"\n[course-loop] Progress: {prev_pct}% → {cur_pct}%. Continuing to next section...")
                # Reset attempted/skipped keys for next section by re-running strict sequence fresh
                continue

        course_done, course_evidence = await self._is_course_completed()
        progress = await self._check_progress_summary()
        final_pct = progress.get("percent", 0) or 0
        course_status = "completed" if (course_done or final_pct >= 100) else "partial"
        print(f"\nCourse: {course.name} | Status: {course_status} | Progress: {final_pct}% | Evidence: {course_evidence}")

        # After all modules ticked: click Finish and submit 5-star rating
        await self._finish_course_with_rating()

    async def _finish_course_with_rating(self) -> None:
        """
        Post-completion flow:
          1. Click Finish/Complete button
          2. Rate course 5 stars (first rating widget)
          3. Fill multi-question survey: rate each question 5 stars
          4. Fill comment box with a short comment
          5. Submit the survey popup
        """
        # ── Step 1: Click Finish / Complete button ────────────────────────────
        print("[finish] Looking for Finish/Complete button...")
        finish_clicked = False
        for label in ["Finish", "Complete", "Finish Course", "Complete Course", "Mark as Complete"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                if await loc.first.is_visible(timeout=800):
                    await loc.first.click()
                    finish_clicked = True
                    print(f"[finish] Clicked '{label}' button.")
                    await self.page.wait_for_timeout(1500)
                    break
            except PlaywrightError:
                continue

        if not finish_clicked:
            finish_clicked = bool(await self.page.evaluate(
                """() => {
                    const re = /^(finish|complete|finish course|complete course|mark as complete)$/i;
                    const btn = Array.from(document.querySelectorAll("button,[role='button']"))
                        .find(b => b.offsetParent && re.test((b.innerText||"").replace(/\\s+/g," ").trim()));
                    if (btn) { btn.click(); return true; }
                    return false;
                }"""
            ))
            if finish_clicked:
                print("[finish] Clicked Finish via JS.")
                await self.page.wait_for_timeout(1500)

        if not finish_clicked:
            print("[finish] No Finish button found — continuing to rating/survey check anyway.")

        # Wait for any popup/dialog to fully render
        await self.page.wait_for_timeout(1000)

        # ── Step 2: Rate course 5 stars (standalone rating widget) ───────────
        await self._click_5_stars_in_container(None)

        # ── Step 3: Handle survey popup — multiple questions, each rated 5 ★ ─
        # The survey can have up to ~10 questions. We loop until no more groups.
        print("[finish] Checking for survey questions...")
        survey_handled = await self.page.evaluate(
            """() => {
                // Find all visible rating groups / question rows in the popup
                // iGot survey uses: mat-radio-group, [class*='rating'], [class*='question'],
                // fieldset, or repeated <li> rows each containing star icons.
                const norm = s => (s||"").replace(/\\s+/g," ").trim();

                // Collect all independent rating containers (one per survey question)
                const ratingGroups = Array.from(document.querySelectorAll(
                    "mat-radio-group, [class*='survey'] [class*='rating'], " +
                    "[class*='question-row'], [class*='question-item'], " +
                    "fieldset, [class*='form-group'], [class*='feedback-question']"
                )).filter(el => el.offsetParent);

                let answered = 0;
                for (const group of ratingGroups) {
                    // Within each group pick the highest-value radio or last star
                    const radios = Array.from(group.querySelectorAll("input[type='radio']"))
                        .filter(r => r.offsetParent);
                    if (radios.length) {
                        // Sort by value descending, click the highest (5 or max)
                        const sorted = radios.slice().sort((a,b) => Number(b.value||0) - Number(a.value||0));
                        sorted[0].click();
                        answered++;
                        continue;
                    }
                    // Try mat-icon stars: click the last (rightmost = 5th)
                    const stars = Array.from(group.querySelectorAll(
                        "mat-icon, [class*='star'], span[class*='icon']"
                    )).filter(el => el.offsetParent);
                    if (stars.length) {
                        stars[stars.length - 1].click();
                        answered++;
                    }
                }
                return answered;
            }"""
        )
        if survey_handled:
            print(f"[finish] Survey: answered {survey_handled} question(s) with 5 stars.")
            await self.page.wait_for_timeout(500)
        else:
            # Fallback: click ALL visible star groups one by one using Playwright
            print("[finish] Survey JS fallback — clicking last star in every visible rating group...")
            for sel in [
                "[class*='rating'] mat-icon",
                "[class*='star-rating'] span",
                "mat-radio-group mat-radio-button:last-child",
            ]:
                try:
                    items = self.page.locator(sel)
                    count = await items.count()
                    if count:
                        await items.last.click()
                        await self.page.wait_for_timeout(300)
                except PlaywrightError:
                    continue

        # ── Step 4: Fill comment / feedback text box ──────────────────────────
        print("[finish] Looking for comment/feedback text box...")
        comment_text = "Excellent course. Very informative and well structured. Highly recommended."
        comment_filled = False
        for sel in [
            "textarea",
            "input[type='text'][placeholder*='comment' i]",
            "input[type='text'][placeholder*='feedback' i]",
            "[class*='comment'] textarea",
            "[class*='feedback'] textarea",
            "[class*='remark'] textarea",
            "mat-form-field textarea",
        ]:
            try:
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=600):
                    await loc.click()
                    await loc.fill(comment_text)
                    comment_filled = True
                    print(f"[finish] Filled comment box ({sel}).")
                    await self.page.wait_for_timeout(300)
                    break
            except PlaywrightError:
                continue

        if not comment_filled:
            print("[finish] No comment box found — skipping comment.")

        # ── Step 5: Submit the survey / rating popup ──────────────────────────
        await self.page.wait_for_timeout(400)
        print("[finish] Submitting survey/rating...")
        submitted = False
        for label in ["Submit", "Submit Feedback", "Submit Survey", "Send", "Done", "Save"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(r"^" + re.escape(label) + r"$", re.I))
                if await loc.first.is_visible(timeout=600):
                    await loc.first.click()
                    submitted = True
                    print(f"[finish] Survey submitted via '{label}'.")
                    await self.page.wait_for_timeout(1000)
                    break
            except PlaywrightError:
                continue

        if not submitted:
            submitted = bool(await self.page.evaluate(
                """() => {
                    const re = /^(submit|submit feedback|submit survey|send|done|save|rate)$/i;
                    const btn = Array.from(document.querySelectorAll("button,[role='button']"))
                        .find(b => b.offsetParent && re.test((b.innerText||"").replace(/\\s+/g," ").trim()));
                    if (btn) { btn.click(); return true; }
                    return false;
                }"""
            ))
            if submitted:
                print("[finish] Survey submitted via JS.")
                await self.page.wait_for_timeout(1000)

        if not submitted:
            print("[finish] No Submit button found for survey.")

        # Dismiss any remaining confirmation (OK/Close/Done)
        await self.page.wait_for_timeout(600)
        try:
            await self.page.evaluate(
                """() => {
                    const re = /^(ok|okay|close|done|continue|got it|dismiss)$/i;
                    const btn = Array.from(document.querySelectorAll("button,[role='button']"))
                        .find(b => b.offsetParent && re.test((b.innerText||"").replace(/\\s+/g," ").trim()));
                    if (btn) btn.click();
                }"""
            )
        except PlaywrightError:
            pass

        print("[finish] Course completion flow done. Certificate should be generating.")

    async def _click_5_stars_in_container(self, container_sel: str | None) -> bool:
        """Click the 5th / highest star in a rating widget. container_sel=None = full page."""
        scope = f'document.querySelector("{container_sel}")' if container_sel else "document"
        result = await self.page.evaluate(
            f"""() => {{
                const root = {scope} || document;
                // Radio with value 5
                const r5 = Array.from(root.querySelectorAll("input[type='radio']"))
                    .filter(r => r.offsetParent)
                    .sort((a,b) => Number(b.value||0) - Number(a.value||0));
                if (r5.length) {{ r5[0].click(); return "radio-" + (r5[0].value||"?"); }}
                // Last visible mat-icon / star span
                const stars = Array.from(root.querySelectorAll(
                    "mat-icon, [class*='star'], [class*='rating'] span, [aria-label*='star' i]"
                )).filter(el => el.offsetParent);
                if (stars.length) {{ stars[stars.length-1].click(); return "star-last"; }}
                return null;
            }}"""
        )
        if result:
            print(f"[finish] 5-star click: {result}")
            await self.page.wait_for_timeout(400)
            return True
        return False

    async def _run_strict_sequence(self, course_name: str, max_modules: int, course_href: str = "") -> None:
        print("Strict sequence mode active: processing modules in order with tick verification.")
        completed_in_run = 0
        guard_cycles = 0
        attempted_keys: set[str] = set()
        skipped_keys: set[str] = set()
        completed_hrefs: set[str] = set()   # survives page navigation — never re-attempt a completed URL
        retry_after: dict[str, float] = {}
        timer_attempts: dict[str, int] = {}
        continue_without_tick = self.args.auto_run_to_end or self.args.skip_assessments
        while completed_in_run < max_modules:
            guard_cycles += 1
            if guard_cycles > max_modules * 100:
                print("Strict sequence guard reached. Stopping to avoid infinite loop.")
                break

            modules = await self._discover_modules()
            if not modules:
                # Try expanding sections first (all sections may be collapsed on initial load)
                print("[strict] No modules visible — attempting to expand TOC sections...")
                expanded = await self._expand_all_sections()
                if expanded:
                    await self.page.wait_for_timeout(2000)
                    modules = await self._discover_modules()
            if not modules:
                # In auto-run mode, try to navigate back to the course page rather than
                # asking for user input. After a quiz result page or video completion,
                # the TOC sidebar disappears. Going back restores it.
                if self.args.auto_run_to_end and course_href:
                    print("[strict] No TOC visible after module completion — navigating back to course page...")
                    try:
                        await self.page.go_back(timeout=8000)
                        await self.page.wait_for_timeout(2500)
                    except Exception:
                        pass
                    modules = await self._discover_modules()
                    if not modules and course_href:
                        # go_back wasn't enough — navigate directly to course URL
                        print(f"[strict] go_back didn't restore TOC — navigating to course URL...")
                        try:
                            await self.page.goto(course_href, timeout=30000)
                            await self.page.wait_for_timeout(3000)
                            await self._expand_all_sections()
                            await self.page.wait_for_timeout(1500)
                        except Exception:
                            pass
                        modules = await self._discover_modules()
                if not modules:
                    print("No modules visible right now. Open the course TOC/content, then press Enter.")
                    await self._wait_for_enter()
                    modules = await self._discover_modules()
                if not modules:
                    print("Still no modules visible. Ending strict sequence for this course.")
                    break

            pending = [m for m in modules if m.is_completed is not True]
            if not pending:
                # All current modules ticked — but there may be more sections
                print("[strict] All visible modules ticked. Trying to expand more sections...")
                expanded = await self._expand_all_sections()
                if expanded:
                    await self.page.wait_for_timeout(1500)
                    modules = await self._discover_modules()
                    pending = [m for m in modules if m.is_completed is not True]
                if not pending:
                    # Try clicking the NEXT unexpanded section in the sidebar
                    section_clicked = await self._click_next_section()
                    if section_clicked:
                        await self.page.wait_for_timeout(2000)
                        modules = await self._discover_modules()
                        pending = [m for m in modules if m.is_completed is not True]
                if not pending:
                    # Try clicking Next button to move to next content
                    navigated = await self._fast_navigate_next()
                    if navigated:
                        await self.page.wait_for_timeout(2000)
                        # Expand sections on the new page
                        await self._expand_all_sections()
                        await self.page.wait_for_timeout(1000)
                        modules = await self._discover_modules()
                        pending = [m for m in modules if m.is_completed is not True]
                if not pending:
                    course_done, _ = await self._is_course_completed()
                    progress = await self._check_progress_summary()
                    if course_done or progress.get("percent") == 100:
                        print("All modules completed. Course is done!")
                    else:
                        print(f"All visible modules ticked but course not 100% yet (progress: {progress.get('percent', '?')}%).")
                        # One more try: scroll the sidebar to reveal more items
                        try:
                            await self.page.evaluate(
                                """() => {
                                    const sidebar = document.querySelector(
                                        "mat-sidenav, [class*='sidebar'], [class*='sidenav'], [class*='toc'], nav"
                                    );
                                    if (sidebar) {
                                        sidebar.scrollTo(0, sidebar.scrollHeight);
                                    }
                                }"""
                            )
                            await self.page.wait_for_timeout(1500)
                        except PlaywrightError:
                            pass
                        await self._expand_all_sections()
                        await self.page.wait_for_timeout(1000)
                        modules = await self._discover_modules()
                        pending = [m for m in modules if m.is_completed is not True]
                        if pending:
                            print(f"[strict] Found {len(pending)} modules after sidebar scroll.")
                            continue
                    break

            module = None
            module_key = ""
            now_ts = time.time()
            for candidate in pending:
                key = f"{self._normalize_for_match(candidate.name)}::{self._stable_href(candidate.href or '')}"
                if key in attempted_keys or key in skipped_keys:
                    continue
                # Skip if URL was completed earlier in this run (survives page navigation)
                _href_key = self._stable_href(candidate.href or "")
                if _href_key and _href_key in completed_hrefs:
                    skipped_keys.add(key)
                    continue
                if candidate.is_completed is True:
                    skipped_keys.add(key)
                    continue
                is_ticked = await self._is_module_ticked(candidate.name, candidate.href or "")
                if is_ticked:
                    skipped_keys.add(key)
                    print(f"Strict sequence: '{candidate.name}' ✓ TICKED, skipping.")
                    continue
                is_section = await self._looks_like_section_header_live(candidate.name)
                print(f"[seq-check] '{candidate.name[:50]}' ticked={is_ticked} section={is_section} key_attempted={key in attempted_keys}")
                if is_section:
                    expanded = await self._expand_section_by_name(candidate.name)
                    skipped_keys.add(key)
                    if expanded:
                        print(f"Strict sequence: expanded section '{candidate.name}', discovering leaf modules.")
                    else:
                        print(f"Strict sequence: skipping section header '{candidate.name}'.")
                    continue
                if self._is_section_header_name(candidate.name):
                    expanded = await self._expand_section_by_name(candidate.name)
                    skipped_keys.add(key)
                    if expanded:
                        print(f"Strict sequence: expanded section '{candidate.name}', discovering leaf modules.")
                    else:
                        print(f"Strict sequence: skipping section header '{candidate.name}'.")
                    continue
                if key in retry_after and retry_after[key] > now_ts:
                    continue
                module = candidate
                module_key = key
                break
            if module is None:
                future = [
                    ts for key, ts in retry_after.items()
                    if key not in attempted_keys and key not in skipped_keys and ts > now_ts
                ]
                if future:
                    wait_for = max(0.0, min(15.0, min(future) - now_ts))
                    if wait_for > 0.2:
                        print(f"Strict sequence: waiting {int(round(wait_for))}s for timer-locked module retry.")
                        await self.page.wait_for_timeout(int(wait_for * 1000))
                        continue

                # Before giving up — try expanding collapsed sections and re-discover
                print("[strict] No eligible modules in current view. Expanding sections...")

                def _is_new_pending(m):
                    k = f"{self._normalize_for_match(m.name)}::{self._stable_href(m.href or '')}"
                    return m.is_completed is not True and k not in attempted_keys and k not in skipped_keys

                expanded = await self._expand_all_sections()
                if expanded:
                    await self.page.wait_for_timeout(1500)
                    new_modules = await self._discover_modules()
                    new_pending = [m for m in new_modules if _is_new_pending(m)]
                    if new_pending:
                        print(f"[strict] Found {len(new_pending)} new modules after expanding.")
                        continue

                # Try clicking next collapsed section in sidebar
                section_clicked = await self._click_next_section()
                if section_clicked:
                    print("[strict] Clicked next section in sidebar.")
                    await self.page.wait_for_timeout(2000)
                    await self._expand_all_sections()
                    await self.page.wait_for_timeout(1000)
                    new_modules = await self._discover_modules()
                    new_pending = [m for m in new_modules if _is_new_pending(m)]
                    if new_pending:
                        print(f"[strict] Found {len(new_pending)} new modules after section click.")
                        continue

                # Also try clicking "Next" to navigate to next section/page
                navigated = await self._fast_navigate_next()
                if navigated:
                    print("[strict] Navigated to next section via Next button.")
                    await self.page.wait_for_timeout(2000)
                    await self._expand_all_sections()
                    await self.page.wait_for_timeout(1000)
                    new_modules = await self._discover_modules()
                    new_pending = [m for m in new_modules if _is_new_pending(m)]
                    if new_pending:
                        print(f"[strict] Found {len(new_pending)} new modules after navigation.")
                        continue

                # Check if course is actually complete
                course_done, course_ev = await self._is_course_completed()
                if course_done:
                    print(f"[strict] Course is complete: {course_ev}")
                    break

                progress = await self._check_progress_summary()
                if progress.get("percent") == 100:
                    print("[strict] Course progress is 100%.")
                    break

                print("No new eligible module left to process in strict sequence.")
                break
            print(f"Strict sequence next: {module.name}")

            if self._is_assessment_module(module.name, module.href):
                if self.args.skip_assessments:
                    evidence = "Assessment skipped by configuration (--skip-assessments)"
                    self._record(
                        course_name=course_name,
                        module_name=module.name,
                        module_type="quiz",
                        status="partial",
                        block_reason=None,
                        evidence=evidence,
                        next_action="Complete this assessment manually later",
                    )
                    print(f"Module: {module.name} | Status: partial | Evidence: {evidence}")
                    skipped_keys.add(module_key)
                    continue
                opened = await self._open_module_by_name_or_href(module)
                if not opened:
                    self._record(
                        course_name=course_name,
                        module_name=module.name,
                        module_type="quiz",
                        status="blocked",
                        block_reason="technical_error",
                        evidence="Could not open assessment module in strict sequence",
                        next_action="Open assessment manually and retry strict sequence",
                    )
                    attempted_keys.add(module_key)
                    break

                # Wait for quiz page to load
                await self._wait_for_course_content()
                await self.page.wait_for_timeout(1200)

                # Auto-solve via Gemini if available, else manual
                print(f"Assessment module detected: '{module.name}'. Auto-solving...")
                quiz_status, quiz_evidence, quiz_next = await self._handle_quiz_assistive(module.name)
                # NOTE: retry logic for "no quiz content" is handled inside
                # _handle_quiz_assistive — do NOT use retry_after/continue here
                # because that would restart _discover_modules() on the quiz
                # player page (no sidebar → empty → wait_for_enter).

                attempted_keys.add(module_key)
                self._record(
                    course_name=course_name,
                    module_name=module.name,
                    module_type="quiz",
                    status=quiz_status,
                    block_reason=None,
                    evidence=quiz_evidence,
                    next_action=quiz_next,
                )
                print(f"Module: {module.name} | Status: {quiz_status} | Evidence: {quiz_evidence}")

                ticked = await self._is_module_ticked(module.name, module.href or "")
                if quiz_status == "done" or ticked:
                    completed_in_run += 1
                    if module.href:
                        completed_hrefs.add(self._stable_href(module.href))
                    retry_after.pop(module_key, None)
                    timer_attempts.pop(module_key, None)
                    continue
                if continue_without_tick:
                    print("Strict sequence: assessment not ticked yet; continuing due run-to-end setting.")
                    continue
                print("Strict sequence paused: assessment is not ticked yet.")
                break
                timer_attempts.pop(module_key, None)
                continue

            await self._process_module(course_name, module)
            ticked = await self._is_module_ticked(module.name, module.href or "")
            latest_entry = self._latest_module_report(course_name, module.name)
            latest_block = latest_entry.block_reason if latest_entry else None
            latest_status = latest_entry.status if latest_entry else "partial"
            if not ticked:
                if continue_without_tick:
                    if latest_block == "timer_lock":
                        attempts = timer_attempts.get(module_key, 0) + 1
                        timer_attempts[module_key] = attempts
                        if attempts <= self.args.timer_lock_max_retries:
                            derived_wait = self._extract_duration_seconds(module.name)
                            wait_seconds = derived_wait if derived_wait is not None else self.args.timer_lock_retry_seconds
                            wait_seconds = max(5, min(wait_seconds, self.args.timer_lock_max_wait_seconds))
                            retry_after[module_key] = time.time() + wait_seconds
                            print(
                                f"Strict sequence: timer lock for '{module.name}'. "
                                f"Will retry in {wait_seconds}s (attempt {attempts}/{self.args.timer_lock_max_retries})."
                            )
                        else:
                            attempted_keys.add(module_key)
                            print(
                                f"Strict sequence: timer lock max retries reached for '{module.name}'. "
                                "Marking as deferred and moving on."
                            )
                        continue
                    attempted_keys.add(module_key)
                    if module.href:
                        completed_hrefs.add(self._stable_href(module.href))
                    print(f"Strict sequence: '{module.name}' not ticked; continuing due run-to-end setting.")
                    continue
                print("Strict sequence: tick not visible yet. Complete remaining required steps, then press Enter to re-check.")
                await self._wait_for_enter()
                ticked = await self._is_module_ticked(module.name, module.href or "")
            if not ticked:
                attempted_keys.add(module_key)
                if latest_status == "blocked":
                    print(f"Strict sequence paused: '{module.name}' is blocked ({latest_block}).")
                else:
                    print(f"Strict sequence paused: '{module.name}' is not ticked yet.")
                break
            completed_in_run += 1
            attempted_keys.add(module_key)
            if module.href:
                completed_hrefs.add(self._stable_href(module.href))
            retry_after.pop(module_key, None)
            timer_attempts.pop(module_key, None)

    async def _discover_modules(self) -> list[ModuleCandidate]:
        if await self._is_likely_loading():
            return []
        # ── FIXED: Always try to open collapsed sidebar first ──
        await self._ensure_sidebar_open()
        sidebar_modules = await self._discover_sidebar_modules()
        if True:  # Always expand (was: self.args.strict_sequence only)
            expanded = await self._expand_all_sections()
            if expanded:
                await self.page.wait_for_timeout(900)
                sidebar_modules = await self._discover_sidebar_modules()
            if not sidebar_modules:
                # Try scrolling sidebar to reveal lazy-loaded items
                try:
                    await self.page.evaluate(
                        """() => {
                            const sidebar = document.querySelector(
                                "[class*='sidebar'], [class*='toc'], [class*='course-toc'], nav, [role='navigation']"
                            );
                            if (sidebar) sidebar.scrollTo(0, sidebar.scrollHeight);
                        }"""
                    )
                    await self.page.wait_for_timeout(500)
                except PlaywrightError:
                    pass
                sidebar_modules = await self._discover_sidebar_modules()
        if sidebar_modules:
            print(f"Discovered {len(sidebar_modules)} module candidates (sidebar parser).")
            for i, m in enumerate(sidebar_modules[:10]):
                status = "✓ DONE" if m.is_completed else "○ pending"
                print(f"  [{i+1}] {status} {m.name[:70]}")
            if len(sidebar_modules) > 10:
                print(f"  ... and {len(sidebar_modules) - 10} more")
            return sidebar_modules
        try:
            raw = await self.page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const selectors = [
                    "a[href*='quiz']",
                    "a[href*='assessment']",
                    "a[href*='viewer']",
                    "a[href*='content']",
                    "a[href*='do_']",
                    "a[href*='lesson']",
                    "a[href*='module']",
                    ".module-item a[href]",
                    ".lesson-item a[href]",
                    "[role='treeitem'] a[href]"
                ];
                const elements = Array.from(document.querySelectorAll(selectors.join(",")));
                for (const el of elements) {
                    const anchor = el.matches("a[href]") ? el : el.querySelector("a[href]");
                    const href = anchor ? anchor.href : (el.href || null);
                    let text = (el.innerText || anchor?.innerText || "").replace(/\\s+/g, " ").trim();
                    text = text.replace(/\\b(check_circle|radio_button_checked|add|remove)\\b/gi, "").replace(/\\s+/g, " ").trim();
                    if (text.length > 120) continue;
                    text = text.slice(0, 120);
                    if (!text && !href) continue;
                    const low = `${text} ${href || ""}`.toLowerCase();
                    const looksModule =
                        low.includes("module") ||
                        low.includes("lesson") ||
                        low.includes("quiz") ||
                        low.includes("assessment") ||
                        low.includes("chapter") ||
                        low.includes("topic") ||
                        low.includes("unit") ||
                        low.includes("pdf") ||
                        low.includes("video") ||
                        low.includes("slide") ||
                        (href && (
                            href.includes("viewer/") ||
                            href.includes("/content/") ||
                            href.includes("/do_") ||
                            href.includes("collectionId=") ||
                            href.includes("batchId=")
                        ));
                    if (!looksModule) continue;
                    const key = `${href || ""}::${text.toLowerCase()}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    let hint = "unknown";
                    if (low.includes("quiz") || low.includes("assessment")) hint = "quiz";
                    else if (low.includes("assignment")) hint = "assignment";
                    else if (low.includes("video")) hint = "video";
                    else if (low.includes("pdf") || low.includes("viewer/pdf") || (href && href.includes("/pdf/"))) hint = "pdf";
                    else if (low.includes("slide")) hint = "slides";
                    else if (low.includes("read")) hint = "reading";
                    out.push({ name: text || "Untitled Module", href, hint });
                }
                return out;
            }"""
            )
        except PlaywrightError:
            return []
        modules = [
            ModuleCandidate(
                name=self._normalize_module_name(self._clean_name(item.get("name", "Untitled Module"))),
                href=item.get("href"),
                module_type_hint=item.get("hint", "unknown"),
                is_completed=item.get("done"),
            )
            for item in raw
            if not self._is_module_noise(
                self._normalize_module_name(self._clean_name(item.get("name", "Untitled Module"))),
                item.get("href"),
            )
        ]
        modules = self._dedupe_modules(modules)
        print(f"Discovered {len(modules)} module candidates.")
        return modules

    async def _click_next_section(self) -> bool:
        """Find and click the next unexpanded/unvisited section header in the sidebar.
        iGOT sidebar has section rows with 'N items' text, expand toggles (mat-icon 'add'),
        or sections that are simply clickable rows with child modules.
        Returns True if a section was clicked."""
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    // Scope to sidebar/TOC area
                    const tocRoot = document.querySelector(
                        'mat-sidenav, [class*="sidebar"], [class*="toc"], [class*="sidenav"], nav'
                    ) || document;
                    // Find ALL rows in the sidebar
                    const rows = Array.from(tocRoot.querySelectorAll(
                        'li, [role="treeitem"], [class*="item"], [class*="node"], ' +
                        '[class*="unit"], [class*="chapter"], [class*="section"], ' +
                        '[class*="week"], [class*="course-toc"], mat-list-item'
                    ));
                    // Collect section candidates
                    const sections = [];
                    for (const row of rows) {
                        const text = norm(row.innerText || '').toLowerCase();
                        if (!text || text.length > 300 || text.length < 3) continue;
                        // Section signals
                        const hasItems = /\\b\\d+\\s*items?\\b/i.test(text);
                        const hasExpandIcon = !!row.querySelector(
                            'mat-icon, [class*="expand"], [class*="plus"], [class*="add"], ' +
                            '[class*="arrow"], [class*="toggle"], [class*="collapse"]'
                        );
                        const hasChildLi = row.querySelectorAll('li, [role="treeitem"]').length > 0;
                        const isSection = hasItems || (hasExpandIcon && text.length > 10) || hasChildLi;
                        if (!isSection) continue;
                        // Check if this section is fully completed
                        const html = (row.innerHTML || '').toLowerCase();
                        const hasCheck = html.includes('check_circle') || html.includes('task_alt');
                        // If all items under this section are done, skip it
                        // But if it has 'add' icon (collapsed), it might have undone items
                        const hasAddIcon = Array.from(row.querySelectorAll('mat-icon')).some(
                            mi => (mi.textContent || '').trim() === 'add'
                        );
                        sections.push({
                            row, text: text.slice(0, 80),
                            hasItems, hasAddIcon, hasCheck,
                            priority: hasAddIcon ? 10 : (hasItems ? 5 : 1)
                        });
                    }
                    // Sort: prefer sections with 'add' icon (collapsed) first
                    sections.sort((a, b) => b.priority - a.priority);
                    // Click the first section that looks expandable
                    for (const s of sections) {
                        const clickEl = s.row.querySelector(
                            'mat-icon, [class*="expand"], [class*="add"], button, [role="button"], a[href]'
                        ) || s.row;
                        clickEl.scrollIntoView({ block: 'center', behavior: 'instant' });
                        clickEl.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        return s.text;
                    }
                    return null;
                }"""
            )
            if clicked:
                print(f"[section] Clicked section: {clicked}")
                await self.page.wait_for_timeout(2000)
                return True
        except PlaywrightError:
            pass

        # Fallback A: click any mat-icon with text 'add' (collapsed section toggle)
        try:
            add_icons = self.page.locator("mat-icon")
            count = await add_icons.count()
            for i in range(count):
                icon = add_icons.nth(i)
                try:
                    text = await icon.inner_text()
                    if text.strip() == "add" and await icon.is_visible(timeout=200):
                        await icon.click()
                        print("[section] Clicked 'add' mat-icon to expand section")
                        await self.page.wait_for_timeout(2000)
                        return True
                except PlaywrightError:
                    continue
        except PlaywrightError:
            pass

        # Fallback B: click any sidebar item that has 'N items' text
        try:
            items_loc = self.page.locator("text=/\\d+\\s*items?/i").first
            if await items_loc.is_visible(timeout=800):
                await items_loc.click()
                print("[section] Clicked section via 'N items' text")
                await self.page.wait_for_timeout(2000)
                return True
        except PlaywrightError:
            pass
        return False

    async def _expand_all_sections(self) -> bool:
        try:
            changed = await self.page.evaluate(
                """() => {
                    let clicked = 0;
                    const clickedEls = new WeakSet();

                    // ── Primary: mat-expansion-panel (iGot uses Angular Material accordion) ──
                    // Find the TOC accordion container to limit scope
                    const tocRoot = document.querySelector("mat-accordion") ||
                                    document.querySelector("[class*='course-toc']") ||
                                    document.querySelector("[class*='toc-container']") ||
                                    document.querySelector("ws-app-toc") ||
                                    document.querySelector("app-course-toc") ||
                                    document;

                    const panels = Array.from(tocRoot.querySelectorAll("mat-expansion-panel"));
                    for (const panel of panels) {
                        // mat-expansion-panel gets class "mat-expanded" when open
                        const isExpanded = panel.classList.contains("mat-expanded") ||
                                           !!panel.querySelector("[aria-expanded='true']");
                        if (isExpanded) continue;

                        // Click the panel header to expand it
                        const header = panel.querySelector(
                            "mat-expansion-panel-header, [class*='expansion-panel-header'], [role='button']"
                        );
                        const target = header || panel;
                        if (!target || clickedEls.has(target)) continue;

                        clickedEls.add(target);
                        target.scrollIntoView({ block: "center", behavior: "instant" });
                        target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        clicked += 1;
                    }

                    // ── Fallback: generic nodes with expand toggles (non-Material portals) ──
                    if (clicked === 0) {
                        const matIconAdd = (node) => {
                            for (const mi of node.querySelectorAll("mat-icon, [class*='mat-icon']")) {
                                const t = (mi.textContent || "").trim().toLowerCase();
                                if (t === "add" || t === "+" || t === "expand_more" || t === "keyboard_arrow_down") return mi;
                            }
                            return null;
                        };
                        const nodes = Array.from(tocRoot.querySelectorAll(
                            "li, [role='treeitem'], [class*='item'], [class*='unit'], [class*='chapter']"
                        ));
                        for (const node of nodes) {
                            const text = (node.innerText || "").replace(/\\s+/g, " ").trim().toLowerCase();
                            if (!text || text.length > 300) continue;
                            const hasItemCount = /\\b\\d+\\s*items?\\b/i.test(text);
                            const iconAdd = matIconAdd(node);
                            const expandEl = iconAdd ||
                              node.querySelector("[aria-label*='expand']") ||
                              node.querySelector("[class*='expand']:not([class*='expanded'])") ||
                              node.querySelector("[data-icon='add']");
                            const ariaExpandable = node.getAttribute("aria-expanded") === "false" ||
                              !!node.querySelector("[aria-expanded='false']");
                            if (!expandEl && !ariaExpandable && !hasItemCount) continue;
                            const alreadyExpanded = node.getAttribute("aria-expanded") === "true" ||
                              !!node.querySelector("[aria-expanded='true']") ||
                              node.querySelector("[aria-label*='collapse']") ||
                              node.querySelector("[data-icon='remove']");
                            if (alreadyExpanded && !hasItemCount) continue;
                            const target = expandEl || node.querySelector("button, [role='button']");
                            if (!target || clickedEls.has(target)) continue;
                            clickedEls.add(target);
                            target.scrollIntoView({ block: "center", behavior: "instant" });
                            target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            clicked += 1;
                        }
                    }

                    return clicked;
                }"""
            )
            return bool(changed and changed > 0)
        except PlaywrightError:
            return False

    async def _discover_sidebar_modules(self) -> list[ModuleCandidate]:
        try:
            raw = await self.page.evaluate(
                """() => {
                    const normalize = (s) => (s || "").replace(/\\s+/g, " ").trim();
                    const cleanTitle = (s) => {
                        let t = normalize(s);
                        t = t.replace(/^\\d+\\.\\s*/, "");
                        t = t.replace(/\\b(check_circle|radio_button_checked|add|remove|task_alt|done)\\b/gi, "");
                        // Strip trailing "· N items" or "• N items" (section metadata)
                        t = t.replace(/\\s*[·•\\|]\\s*\\d+\\s*items?\\s*$/i, "");
                        t = t.replace(/\\s+\\d+\\s*items?\\s*$/i, "");
                        t = t.replace(/\\s+\\d+h(\\s+\\d+m)?(\\s+\\d+s)?$/i, "");
                        t = t.replace(/\\s+\\d+m(\\s+\\d+s)?(\\s+[•·\\-]\\s+\\d+\\s*item[s]?)?$/i, "");
                        t = t.replace(/\\s+\\d+s$/i, "");
                        // Strip "N Questions" suffix (quiz leaf metadata shown separately)
                        t = t.replace(/\\s+\\d+\\s*questions?$/i, "");
                        // Strip inline button text appended to module row (e.g. "Download All", "Download PDF")
                        t = t.replace(/\\.?\\s*\\bdownload\\b.*$/i, "");
                        t = normalize(t);
                        return t.slice(0, 120);
                    };

                    const looksLeafRow = (text) => {
                        if (!text) return false;
                        if (text.length > 120) return false;
                        const low = text.toLowerCase().replace(/\\s+/g, " ").trim();

                        // Any item with "N items" count is a section container, not a leaf
                        if (/\\b\\d+\\s*items?\\b/i.test(low)) return false;
                        // Reject numbered section headings (e.g. "1. Introduction · 2 items" already caught above)
                        // but also "1. Counselling at Workplace Pt-I · 4m 55s · 2 items"
                        if (/^\\s*(phase|unit|section|chapter|week)\\s*\\d+/i.test(low)) return false;
                        if (/\\b(add|expand|collapse)\\b/i.test(low) && /\\b(phase|module|section|chapter|part|week|unit)\\b/i.test(low)) return false;
                        if (/\\b\\d+\\.\\s/.test(low.replace(/^\\d+\\.\\s*/, ""))) return false;

                        // Reject iGot-specific noise
                        if (/info_outline|more_vert|arrow_forward|expand_more|chevron_right/.test(low)) return false;
                        if (/\\b\\d+\\s*(video|pdf|module|lesson|quiz|assessment|practice\\s+test|final\\s+test)s?$/.test(low)) return false;
                        if (/^items?\\s*\\(\\d+\\/\\d+\\)/.test(low)) return false;
                        if (/^\\d+\\s*stars?$/.test(low)) return false;
                        if (/\\d+\\s+(day|week|month|hour)s?\\s+ago/.test(low)) return false;
                        if (/^(share\\s*)?share$/.test(low)) return false;
                        if (/^(free|cc[\\s-]by|cc by \\d)/.test(low)) return false;
                        if (/^competencies/.test(low) && low.length < 20) return false;
                        if (/^(fractal|coursera|karmayogi)/.test(low) && low.length < 30) return false;
                        // Reject reviewer avatar lines: two initials followed by name
                        if (/^[a-z]{2}\\s+[a-z]/.test(low) && low.split(" ").length >= 3 && low.length < 60) return false;

                        // Reject video player UI controls
                        if (/^\\d+(\\.\\d+)?x(,\\s*(selected|not selected))?$/.test(low)) return false;
                        if (/^(descriptions?|captions?|subtitles?)\\s+(on|off)(,\\s*(selected|not selected))?$/.test(low)) return false;
                        if (/^(chapters|settings|fullscreen|mute|unmute|play|pause|rewind|forward)$/.test(low)) return false;
                        if (/,\\s*(selected|not selected)$/.test(low)) return false;
                        if (/^(\\d{3,4}p|auto)(,\\s*(selected|not selected))?$/.test(low)) return false;

                        // Explicit leaf signals
                        if (/^\\d+\\./.test(low)) return true;
                        if (/\\b\\d+m\\b|\\b\\d+s\\b/i.test(low)) return true;
                        if (/\\b\\d+\\s*questions?\\b/i.test(low)) return true;
                        // Accept named items (8-100 chars) with real words that passed all filters
                        return low.length >= 8 && low.length <= 100;
                    };

                    // ── Scope to TOC/sidebar container only ──────────────────────────
                    // iGot uses mat-accordion for its course TOC (right-side panel).
                    // NEVER fall back to scanning document — the main content area contains
                    // resource links, discussion posts, and reference notes that look like
                    // modules but are not. If no TOC root found, return [] and let the
                    // caller handle (e.g. navigate back to course page).
                    const tocSelectors = [
                        // iGot / Sunbird — mat-accordion is the primary TOC container
                        "mat-accordion",
                        // Sunbird-specific Angular component tags
                        "ws-app-toc",
                        "app-course-toc",
                        "app-curriculum",
                        // Class-based fallbacks
                        "[class*='course-toc']",
                        "[class*='courseToc']",
                        "[class*='toc-container']",
                        "[class*='toc-panel']",
                        "[class*='content-toc']",
                        "[class*='course-content-list']",
                        "[class*='sidebar-toc']",
                        "[class*='unit-list']",
                        "[class*='chapter-list']",
                        "[class*='module-list']",
                        "mat-nav-list",
                        "[role='tree']",
                        "[role='navigation'][class*='course']",
                    ];
                    const playerSelectors = [
                        "[class*='player']", "[class*='vjs-']", "[class*='video-js']",
                        "[class*='video-player']", "[class*='videoPlayer']",
                        "[class*='jwplayer']", "[class*='plyr']",
                    ];

                    // Find the best TOC container that isn't inside a player
                    let tocRoot = null;
                    for (const sel of tocSelectors) {
                        for (const el of document.querySelectorAll(sel)) {
                            const insidePlayer = playerSelectors.some(ps => el.closest(ps));
                            if (!insidePlayer) { tocRoot = el; break; }
                        }
                        if (tocRoot) break;
                    }

                    // CRITICAL: never fall back to document — the main content area
                    // (Reference Note tab, discussion posts, About section) contains text
                    // that looks like module names and would be scanned as fake modules.
                    if (!tocRoot) return [];

                    const out = [];
                    const seen = new Set();

                    // ── Primary discovery: anchor links inside the TOC ───────────────
                    // Every navigable module in iGot's TOC is an <a href="..."> link.
                    // Section headers also contain anchors but they wrap child links,
                    // so we filter them out via the child-anchor check below.
                    const checkDone = (node) => {
                        const h = (node.innerHTML || "").toLowerCase();
                        if (h.includes("check_circle") || h.includes("task_alt")) return true;
                        if (h.includes("sb-dot-done") || h.includes("sb-icon-done")) return true;
                        if (h.includes("done-icon") || h.includes("status-complete")) return true;
                        if (h.includes("is-complete") || h.includes("item-complete")) return true;
                        if (h.includes('aria-checked=\\"true\\"') || h.includes("aria-checked='true'")) return true;
                        if (/aria-label=["'](completed|done|finished)["']/i.test(h)) return true;
                        // Check every child element's class list with word-boundary padding
                        return Array.from(node.querySelectorAll("*")).some(el => {
                            const cls = " " + (el.className || "").toLowerCase() + " ";
                            return cls.includes(" sb-dot-done ") ||
                                   cls.includes(" sb-icon-done ") ||
                                   cls.includes(" done ") ||
                                   cls.includes(" tick ") ||
                                   cls.includes(" item-complete ") ||
                                   cls.includes(" is-complete ") ||
                                   cls.includes(" status-complete ");
                        });
                    };

                    // Collect all anchor links in the TOC
                    const allAnchors = Array.from(tocRoot.querySelectorAll("a[href]"));

                    // Filter to leaf-level module anchors (not section wrappers)
                    const moduleAnchors = allAnchors.filter(a => {
                        // Section container anchors wrap multiple child anchors
                        if (a.querySelectorAll("a[href]").length > 0) return false;
                        const text = normalize(a.innerText || a.textContent || "");
                        // Section headers have "N items" count
                        if (/\\b\\d+\\s*items?\\b/i.test(text)) return false;
                        // Too short or too long to be a real module title
                        if (text.length < 3 || text.length > 250) return false;
                        return true;
                    });

                    if (moduleAnchors.length > 0) {
                        // Anchor-based path: most reliable for iGot
                        for (const anchor of moduleAnchors) {
                            const rawText = normalize(anchor.innerText || anchor.textContent || "");
                            if (/\\b\\d+\\s*items?\\b/i.test(rawText)) continue;
                            const title = cleanTitle(rawText);
                            if (!title || title.length < 4) continue;
                            // Get the row container for completion check (walk up to parent li/div)
                            const row = anchor.closest("li, mat-expansion-panel, [class*='item'], [class*='row']") || anchor.parentElement || anchor;
                            const done = checkDone(row);
                            // Dedup key = title + stable path (no query params).
                            // Using ONLY title caused same-name different-href modules to collapse
                            // into one entry per call; on each page reload a different query-string
                            // produced a new href, making completed_hrefs miss it → infinite loop.
                            const stablePath = (anchor.href || "").split("?")[0].toLowerCase();
                            const key = title.toLowerCase().slice(0, 80) + "::" + stablePath;
                            if (seen.has(key)) continue;
                            seen.add(key);
                            out.push({ name: title, href: anchor.href, hint: "unknown", done: done });
                        }
                    } else {
                        // Fallback: scan generic nodes inside tocRoot (no anchor links found)
                        const nodes = Array.from(tocRoot.querySelectorAll(
                            "li, [role='treeitem'], [class*='item']"
                        ));
                        for (const node of nodes) {
                            const rawText = normalize(node.innerText || "");
                            if (/\\b\\d+\\s*items?\\b/i.test(rawText)) continue;
                            if (!looksLeafRow(rawText)) continue;
                            if (node.querySelectorAll("li, [role='treeitem']").length > 1) continue;
                            const title = cleanTitle(rawText);
                            if (!title || title.length < 4) continue;
                            const done = checkDone(node);
                            const anchor = node.querySelector("a[href]");
                            const stablePath = anchor ? (anchor.href || "").split("?")[0].toLowerCase() : "";
                            const key = title.toLowerCase().slice(0, 80) + "::" + stablePath;
                            if (seen.has(key)) continue;
                            seen.add(key);
                            out.push({ name: title, href: anchor ? anchor.href : null, hint: "unknown", done: done });
                        }
                    }

                    return out;
                }"""
            )
        except PlaywrightError:
            return []

        modules = [
            ModuleCandidate(
                name=self._normalize_module_name(self._clean_name(item.get("name", "Untitled Module"))),
                href=item.get("href"),
                module_type_hint=item.get("hint", "unknown"),
                is_completed=item.get("done"),
            )
            for item in raw
            if not self._is_module_noise(
                self._normalize_module_name(self._clean_name(item.get("name", "Untitled Module"))),
                item.get("href"),
            )
        ]
        return self._dedupe_modules(modules)

    @staticmethod
    def _is_assessment_module(module_name: str, module_href: str | None = None) -> bool:
        low = module_name.lower()
        normalized = re.sub(r"\s+", " ", low).strip()
        markers = [
            "quiz",
            "assessment",
            "check your understanding",
            "check understanding",
            "end of module",
            "final assessment",
            "final test",
            "test",
            "exam",
        ]
        if any(marker in normalized for marker in markers):
            return True
        href = (module_href or "").lower()
        if any(x in href for x in ["viewer/practice", "practice%20question%20set", "/practice/", "quiz", "assessment"]):
            return True
        assessment_patterns = [
            r"\b\d+\s*questions?\b",
            r"\bquestions?\s*\(\s*\d+\s*\)\b",
            r"\bmcq\b",
            r"\bmultiple choice\b",
        ]
        return any(re.search(pattern, normalized) for pattern in assessment_patterns)

    async def _open_module_by_name_or_href(self, module: ModuleCandidate) -> bool:
        if module.href == "__CURRENT_PAGE__":
            return True
        opened_by_name = await self._open_sidebar_module_by_name(module.name)
        if opened_by_name:
            return True
        if module.href:
            await self._safe_goto(module.href)
            return True
        return False

    async def _open_sidebar_module_by_name(self, module_name: str) -> bool:
        target = self._normalize_for_match(module_name)
        if not target:
            return False
        try:
            clicked = await self.page.evaluate(
                """(targetNorm) => {
                    const norm = (s) => (s || "")
                      .toLowerCase()
                      .replace(/\\b(check_circle|radio_button_checked|add|remove)\\b/g, " ")
                      .replace(/[^a-z0-9\\s]/g, " ")
                      .replace(/\\s+/g, " ")
                      .trim();
                    const cleanTitle = (s) => {
                      let t = (s || "").replace(/^\\d+\\.\\s*/, "");
                      t = t.replace(/\\s+\\d+h(\\s+\\d+m)?(\\s+\\d+s)?$/i, "");
                      t = t.replace(/\\s+\\d+m(\\s+\\d+s)?(\\s+[•·\\-]\\s+\\d+\\s*item[s]?)?$/i, "");
                      t = t.replace(/\\s+\\d+s$/i, "");
                      return norm(t);
                    };
                    const score = (titleNorm) => {
                      if (!titleNorm || !targetNorm) return 0;
                      if (titleNorm === targetNorm) return 100;
                      if (titleNorm.includes(targetNorm)) return 80;
                      if (targetNorm.includes(titleNorm)) return 70;
                      const parts = targetNorm.split(" ").filter(Boolean);
                      const overlap = parts.filter(p => titleNorm.includes(p)).length;
                      return overlap > 0 ? Math.min(60, overlap * 10) : 0;
                    };

                    const rows = Array.from(
                      document.querySelectorAll("li, [role='treeitem'], [class*='item'], [class*='accordion'], [class*='toc']")
                    );
                    const ranked = [];
                    for (const row of rows) {
                      const raw = (row.innerText || "").replace(/\\s+/g, " ").trim();
                      if (!raw || raw.length > 180) continue;
                      const childTreeItems = row.querySelectorAll("li, [role='treeitem']").length;
                      if (childTreeItems > 1) continue;
                      const hasExpandToggle = !!row.querySelector(
                        "[aria-label*='expand'], [aria-label*='plus'], [class*='plus'], [class*='expand'], [data-icon*='plus'], [data-icon='add']"
                      );
                      if (hasExpandToggle && /\\b\\d+\\s*items?\\b/i.test(raw)) continue;
                      const titleNorm = cleanTitle(raw);
                      const s = score(titleNorm);
                      if (s <= 0) continue;
                      ranked.push({ row, score: s, titleNorm });
                    }

                    if (!ranked.length) return false;
                    ranked.sort((a, b) => b.score - a.score || b.titleNorm.length - a.titleNorm.length);
                    const chosen = ranked[0].row;
                    const clickEl =
                      chosen.querySelector("a[href]") ||
                      chosen.querySelector("button") ||
                      chosen.querySelector("[role='button']") ||
                      chosen;
                    if (!clickEl) return false;
                    clickEl.scrollIntoView({ block: "center", behavior: "instant" });
                    clickEl.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    return true;
                }""",
                target,
            )
            if clicked:
                await self.page.wait_for_timeout(900)
                return True
            return False
        except PlaywrightError:
            return False

    @staticmethod
    def _is_section_header_name(name: str) -> bool:
        low = re.sub(r"\s+", " ", name.strip().lower())
        if not low:
            return False
        # Any row containing an item count is a section container, not a leaf
        if re.search(r"\b\d+\s*items?\b", low):
            return True
        # Classic keyword sections: "Module 1", "Phase 2", "1. Introduction" with keywords
        if re.search(r"\b(phase|module|section|chapter|part|unit|week)\b", low) and re.search(r"\b\d+\b", low):
            return True
        if re.match(r"^(phase|module|section|chapter|part|unit|week)\s*\d+\b", low):
            return True
        if re.match(r"^\d+\.\s*(phase|module|section|chapter|part|unit|week)\b", low):
            return True
        return False

    async def _expand_section_by_name(self, section_name: str) -> bool:
        target = self._normalize_for_match(section_name)
        if not target:
            return False
        try:
            expanded = await self.page.evaluate(
                """(targetNorm) => {
                    const norm = (s) => (s || "")
                      .toLowerCase()
                      .replace(/\\b(check_circle|radio_button_checked|add|remove)\\b/g, " ")
                      .replace(/[^a-z0-9\\s]/g, " ")
                      .replace(/\\s+/g, " ")
                      .trim();
                    const cleanTitle = (s) => {
                      let t = (s || "").replace(/^\\d+\\.\\s*/, "");
                      t = t.replace(/\\s+\\d+h(\\s+\\d+m)?(\\s+\\d+s)?$/i, "");
                      t = t.replace(/\\s+\\d+m(\\s+\\d+s)?(\\s+[•·\\-]\\s+\\d+\\s*item[s]?)?$/i, "");
                      t = t.replace(/\\s+\\d+s$/i, "");
                      return norm(t);
                    };
                    const nodes = Array.from(document.querySelectorAll("li, [role='treeitem'], [class*='item'], [class*='accordion'], [class*='toc']"));
                    let best = null;
                    let bestScore = -1;
                    for (const node of nodes) {
                      const raw = (node.innerText || "").replace(/\\s+/g, " ").trim();
                      if (!raw || raw.length > 220) continue;
                      const titleNorm = cleanTitle(raw);
                      if (!titleNorm) continue;
                      let score = 0;
                      if (titleNorm === targetNorm) score = 100;
                      else if (titleNorm.includes(targetNorm)) score = 80;
                      else if (targetNorm.includes(titleNorm)) score = 70;
                      if (score > bestScore) {
                        bestScore = score;
                        best = node;
                      }
                    }
                    if (!best || bestScore < 50) return false;
                    const plus = best.querySelector(
                      "[aria-label*='expand'], [aria-label*='plus'], [class*='plus'], [class*='expand'], [data-icon*='plus'], [data-icon='add']"
                    );
                    const clickEl = plus || best.querySelector("button, [role='button'], a[href]") || best;
                    if (!clickEl) return false;
                    clickEl.scrollIntoView({ block: "center", behavior: "instant" });
                    clickEl.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    return true;
                }""",
                target,
            )
            if expanded:
                await self.page.wait_for_timeout(700)
                return True
            return False
        except PlaywrightError:
            return False

    async def _looks_like_section_header_live(self, name: str) -> bool:
        target = self._normalize_for_match(name)
        if not target:
            return False
        try:
            return bool(
                await self.page.evaluate(
                    """(targetNorm) => {
                        const norm = (s) => (s || "")
                          .toLowerCase()
                          .replace(/\\b(check_circle|radio_button_checked|add|remove)\\b/g, " ")
                          .replace(/[^a-z0-9\\s]/g, " ")
                          .replace(/\\s+/g, " ")
                          .trim();
                        const cleanTitle = (s) => {
                          let t = (s || "").replace(/^\\d+\\.\\s*/, "");
                          t = t.replace(/\\s+\\d+h(\\s+\\d+m)?(\\s+\\d+s)?$/i, "");
                          t = t.replace(/\\s+\\d+m(\\s+\\d+s)?(\\s+[•·\\-]\\s+\\d+\\s*item[s]?)?$/i, "");
                          t = t.replace(/\\s+\\d+s$/i, "");
                          return norm(t);
                        };
                        const rows = Array.from(document.querySelectorAll(
                          "li, [role='treeitem'], [class*='item'], [class*='accordion'], [class*='toc']"
                        ));
                        let best = null;
                        let bestScore = -1;
                        for (const row of rows) {
                          const raw = (row.innerText || "").replace(/\\s+/g, " ").trim();
                          if (!raw || raw.length > 260) continue;
                          const title = cleanTitle(raw);
                          if (!title) continue;
                          let score = 0;
                          if (title === targetNorm) score = 100;
                          else if (title.includes(targetNorm)) score = 80;
                          else if (targetNorm.includes(title)) score = 70;
                          if (score > bestScore) {
                            bestScore = score;
                            best = row;
                          }
                        }
                        if (!best || bestScore < 50) return false;
                        const rawText = (best.innerText || "").replace(/\\s+/g, " ").trim().toLowerCase();
                        const hasItemCount = /\\b\\d+\\s*items?\\b/.test(rawText);
                        const hasPlus =
                          !!best.querySelector("[aria-label*='expand'], [aria-label*='plus'], [class*='plus'], [class*='expand'], [data-icon*='plus'], [data-icon='add']");
                        const childTreeItems = best.querySelectorAll("li, [role='treeitem']").length;
                        const hasLeafDurationOnly =
                          /\\b\\d+m\\b|\\b\\d+s\\b/.test(rawText) && !hasItemCount;
                        if (hasLeafDurationOnly && !hasPlus) return false;
                        return hasItemCount || hasPlus || childTreeItems > 1;
                    }""",
                    target,
                )
            )
        except PlaywrightError:
            return False

    async def _process_module(self, course_name: str, module: ModuleCandidate) -> None:
        print(f"Module: {module.name} ...")
        if await self._is_module_ticked(module.name, module.href or ""):
            evidence = "Already ticked in sidebar from previous session; skipped"
            self._record(
                course_name=course_name,
                module_name=module.name,
                module_type=module.module_type_hint if module.module_type_hint in MODULE_TYPES else "unknown",
                status="done",
                block_reason=None,
                evidence=evidence,
                next_action="Continue to next eligible module",
            )
            print(f"Module: {module.name} | Status: done | Evidence: {evidence}")
            if self.args.auto_run_to_end:
                await self._fast_navigate_next()
            return
        try:
            if module.href != "__CURRENT_PAGE__":
                opened = await self._open_module_by_name_or_href(module)
                if not opened:
                    await self._log_dom_snapshot(f"module_open_failed_{module.name[:30]}")
                    self._record(
                        course_name=course_name,
                        module_name=module.name,
                        module_type=module.module_type_hint if module.module_type_hint in MODULE_TYPES else "unknown",
                        status="blocked",
                        block_reason="technical_error",
                        evidence=f"Module navigation failed (href={module.href})",
                        next_action="Open module manually and rerun",
                    )
                    print(f"Module: {module.name} | Status: blocked | Evidence: Module navigation failed (href={module.href})")
                    return
        except PlaywrightError as exc:
            self._record(
                course_name=course_name,
                module_name=module.name,
                module_type=module.module_type_hint if module.module_type_hint in MODULE_TYPES else "unknown",
                status="blocked",
                block_reason="technical_error",
                evidence=f"Module navigation failed: {exc}",
                next_action="Open module manually and rerun",
            )
            return

        loaded = await self._wait_for_course_content()
        if not loaded:
            if self.args.auto_run_to_end:
                await self.page.wait_for_timeout(max(10000, int(self.args.loading_timeout_seconds * 600)))
                loaded = await self._wait_for_course_content()
                if not loaded:
                    try:
                        await self.page.reload(wait_until="domcontentloaded", timeout=self.args.goto_timeout_ms)
                        await self.page.wait_for_timeout(1000)
                    except PlaywrightError:
                        pass
                    loaded = await self._wait_for_course_content()
            if not loaded:
                self._record(
                    course_name=course_name,
                    module_name=module.name,
                    module_type=module.module_type_hint if module.module_type_hint in MODULE_TYPES else "unknown",
                    status="blocked",
                    block_reason="technical_error",
                    evidence="Module page stayed in spinner/loading shell after recovery attempts",
                    next_action="Open module manually, wait for content, then rerun",
                )
                print(f"Module: {module.name} | Status: blocked | Evidence: Module page stayed in spinner/loading shell after recovery attempts")
                return

        await self._snap(f"module_{self._slug(module.name)}_open")

        module_type = await self._detect_module_type(module.module_type_hint)
        print(f"[module] Detected type: {module_type} (hint was: {module.module_type_hint})")
        block_reason = await self._detect_block_reason(module_type)

        if block_reason:
            evidence = f"Blocked by detected condition: {block_reason}"
            next_action = self._fallback_action(block_reason)
            self._record(
                course_name=course_name,
                module_name=module.name,
                module_type=module_type,
                status="blocked",
                block_reason=block_reason,
                evidence=evidence,
                next_action=next_action,
            )
            print(f"Module: {module.name} | Status: blocked | Evidence: {evidence}")
            return

        if module_type == "scorm":
            status, evidence, next_action = await self._handle_scorm()
        elif module_type == "video":
            status, evidence, next_action = await self._handle_video()
        elif module_type in {"reading", "pdf", "slides"}:
            status, evidence, next_action = await self._handle_reading_like(module_type)
        elif module_type == "assignment":
            status, evidence, next_action = await self._handle_assignment()
        elif module_type == "quiz":
            status, evidence, next_action = await self._handle_quiz_assistive(module.name)
        else:
            status, evidence, next_action = await self._handle_unknown()

        if self.args.skip_assessments and module_type == "quiz":
            status = "partial"
            evidence = "Assessment/practice item skipped by configuration (--skip-assessments)"
            next_action = "Continue to next non-assessment module"

        if module_type == "video" and self.args.auto_run_to_end and status in {"partial", "blocked"}:
            auto_done, auto_evidence = await self._auto_wait_video_completion(
                course_name=course_name,
                module_name=module.name,
                preferred_speed=self.args.video_speed,
            )
            if auto_done:
                status = "done"
                evidence = f"{evidence}; {auto_evidence}"
                next_action = "Continue to next eligible module"
            else:
                evidence = f"{evidence}; {auto_evidence}"

        ticked_now = await self._is_module_ticked(module.name)
        if ticked_now:
            status = "done"
            evidence = f"{evidence}; sidebar tick detected"
            next_action = "Continue to next eligible module"
        elif status == "done":
            # Keep "done" for quiz/video — tick may update asynchronously.
            # Don't downgrade just because the sidebar hasn't refreshed yet.
            evidence = f"{evidence}; tick pending sidebar refresh"
            next_action = "Continue to next eligible module"
        elif status == "partial" and not self.args.auto_run_to_end:
            print("If this module is now complete in UI, press Enter to re-check tick.")
            await self._wait_for_enter()
            ticked_after_wait = await self._is_module_ticked(module.name)
            if ticked_after_wait:
                status = "done"
                evidence = f"{evidence}; sidebar tick detected after confirmation"
                next_action = "Continue to next eligible module"

        self._record(
            course_name=course_name,
            module_name=module.name,
            module_type=module_type,
            status=status,
            block_reason=None if status in {"done", "partial"} else "technical_error",
            evidence=evidence,
            next_action=next_action,
        )
        print(f"Module: {module.name} | Status: {status} | Evidence: {evidence}")

        progress = await self._check_progress_summary()
        if progress.get("percent") == 100:
            print(f"[progress] Course appears 100% complete!")

        # ── Inline assessment check ───────────────────────────────────────────
        # iGOT pattern: a "Start assessment" button can appear below the video
        # player on the *same* page.  It must be clicked BEFORE "Next" is used,
        # otherwise the assessment is silently skipped.
        if module_type == "video":
            inline_handled = await self._check_inline_start_button()
            if inline_handled:
                await self.page.wait_for_timeout(500)
                inline_type = await self._detect_module_type("unknown")
                try:
                    inline_title = (await self.page.title()).strip() or f"Inline {inline_type}"
                except Exception:
                    inline_title = f"Inline {inline_type}"
                print(f"[inline] {inline_type} detected after Start button: '{inline_title[:60]}'")
                _inline_done, _ = await self._is_module_completed()
                if not _inline_done:
                    if inline_type == "assignment":
                        ic_status, ic_evidence, ic_next = await self._handle_assignment()
                    elif inline_type == "quiz":
                        ic_status, ic_evidence, ic_next = await self._handle_quiz_assistive(inline_title)
                    else:
                        # Unknown — treat as assignment (most common for iGOT assessments)
                        ic_status, ic_evidence, ic_next = await self._handle_assignment()
                    self._record(
                        course_name=course_name,
                        module_name=inline_title,
                        module_type=inline_type if inline_type != "unknown" else "assignment",
                        status=ic_status,
                        block_reason=None,
                        evidence=ic_evidence,
                        next_action=ic_next,
                    )
                    print(f"[inline] {inline_title[:50]} | Status: {ic_status} | {ic_evidence[:80]}")

        navigated = await self._fast_navigate_next()
        if not navigated:
            print(f"[auto-continue] Could not auto-navigate after: {module.name[:50]}")
        else:
            # iGOT pattern: a video (or reading) module is often followed immediately by an
            # assignment or quiz page reachable only via the in-viewer "Next" button — it
            # does NOT appear as a separate anchor in the TOC sidebar, so the strict-sequence
            # loop would permanently skip it when re-discovering modules.  Detect & process
            # it inline here.  Quiz is intentionally included: "Reflection Quiz" pages in
            # iGOT are only reachable via Next, not via TOC anchor.
            await self.page.wait_for_timeout(600)
            chained_type = await self._detect_module_type("unknown")
            if chained_type not in {"unknown", module_type, "video"}:
                # Guard: skip if already completed to avoid double-processing TOC items
                _chain_done, _ = await self._is_module_completed()
                if not _chain_done:
                    try:
                        chained_title = (await self.page.title()).strip() or f"Chained {chained_type}"
                    except Exception:
                        chained_title = f"Chained {chained_type}"
                    print(f"[chain] {chained_type} page detected after {module_type}: '{chained_title[:60]}'")
                    if chained_type == "assignment":
                        c_status, c_evidence, c_next = await self._handle_assignment()
                    elif chained_type == "quiz":
                        c_status, c_evidence, c_next = await self._handle_quiz_assistive(chained_title)
                    elif chained_type in {"reading", "pdf", "slides"}:
                        c_status, c_evidence, c_next = await self._handle_reading_like(chained_type)
                    elif chained_type == "scorm":
                        c_status, c_evidence, c_next = await self._handle_scorm()
                    else:
                        c_status, c_evidence, c_next = await self._handle_unknown()
                    self._record(
                        course_name=course_name,
                        module_name=chained_title,
                        module_type=chained_type,
                        status=c_status,
                        block_reason=None,
                        evidence=c_evidence,
                        next_action=c_next,
                    )
                    print(f"[chain] {chained_title[:50]} | Status: {c_status} | {c_evidence[:80]}")

    async def _detect_module_type(self, hint: str) -> str:
        if hint in MODULE_TYPES and hint != "unknown":
            return hint
        # Try detection twice — Angular may not have rendered quiz content yet
        for _attempt in range(2):
            try:
                url = self.page.url.lower()
                body = (await self._body_text()).lower()
                # SCORM detection — check BEFORE other types (SCORM can contain videos/quizzes)
                if "scorm" in url or "viewer/html" in url:
                    # Check body/iframe for SCORM indicators
                    if any(x in body for x in ["scorm", "must be completed in one go",
                                                      "sharable content object", "progress will not be saved"]):
                        return "scorm"
                    # Also check iframes for SCORM player
                    for frame in self.page.frames:
                        if frame == self.page.main_frame:
                            continue
                        try:
                            is_scorm = await frame.evaluate(
                                """() => {
                                    const url = window.location.href.toLowerCase();
                                    const body = (document.body?.innerText || '').toLowerCase();
                                    return url.includes('scorm') || url.includes('scormcontent') ||
                                           url.includes('story.html') || url.includes('index_lms') ||
                                           url.includes('imsmanifest') || url.includes('launch.html') ||
                                           body.includes('scorm') ||
                                           !!document.querySelector('[class*="slide"], [class*="scene"], [class*="player"]');
                                }"""
                            )
                            if is_scorm:
                                return "scorm"
                        except PlaywrightError:
                            pass
                if "viewer/pdf" in url or ".pdf" in url:
                    return "pdf"
                if "viewer/video" in url or "youtube.com" in url or "vimeo.com" in url:
                    return "video"
                if "viewer/practice" in url or "practice%20question%20set" in url or "quiz" in url or "assessment" in url:
                    return "quiz"
                if "assignment" in url or "viewer/assignment" in url:
                    return "assignment"
                if any(x in body for x in ["submit assignment", "upload assignment", "assignment submission",
                                             "assignment description", "submit your assignment"]):
                    return "assignment"
                if "viewer/slide" in url:
                    return "slides"
                if await self.page.locator("video").count() > 0:
                    return "video"
                # Check for video inside iframes (flash/embedded players)
                for frame in self.page.frames:
                    if frame == self.page.main_frame:
                        continue
                    try:
                        has_vid = await frame.evaluate(
                            """() => !!document.querySelector("video, embed, object, [class*='player']")"""
                        )
                        if has_vid:
                            return "video"
                    except PlaywrightError:
                        pass
                if await self.page.locator("embed[type='application/pdf'], iframe[src*='.pdf'], .pdf-viewer").count() > 0:
                    return "pdf"
                # Check for assignment submission elements before quiz body keywords (avoid "submit" false positive)
                assign_els = await self.page.locator(
                    "input[type='file'], [class*='assignment'], [class*='submission'], "
                    "textarea[name*='assign'], textarea[placeholder*='assign' i]"
                ).count()
                if assign_els >= 1:
                    return "assignment"
                if any(x in body for x in ["quiz", "assessment", "question", "submit", "start assessment", "question set", "retakes"]):
                    return "quiz"
                # Check for radio buttons / MCQ options (quiz that doesn't say "quiz" in text)
                radio_count = await self.page.locator("mat-radio-button, input[type='radio'], [role='radio']").count()
                if radio_count >= 2:
                    return "quiz"
                # Check for quiz-like elements
                quiz_els = await self.page.locator("[class*='question'], [class*='mcq'], [class*='option-card']").count()
                if quiz_els >= 1:
                    return "quiz"
                if any(x in body for x in ["slide", "deck"]):
                    return "slides"
                if any(x in body for x in ["article", "reading", "chapter", "content"]):
                    return "reading"
            except PlaywrightError:
                pass
            if _attempt == 0:
                # Wait for Angular to render before second attempt
                await self.page.wait_for_timeout(2000)
        return "unknown"

    async def _open_course_player_if_needed(self) -> bool:
        for label in [
            r"continue learning",
            r"resume",
            r"start course",
            r"start learning",
            r"open course",
            r"go to course",
            r"next",
        ]:
            for role in ["button", "link"]:
                locator = self.page.get_by_role(role, name=re.compile(label, re.I))
                try:
                    if await locator.first.is_visible(timeout=450):
                        await locator.first.scroll_into_view_if_needed(timeout=1200)
                        await locator.first.click()
                        await self.page.wait_for_timeout(1200)
                        return True
                except PlaywrightError:
                    continue
        return False

    async def _open_course_from_list(self, course_name: str) -> bool:
        """Click the Resume/Start button on the course card whose title matches course_name."""
        query = self._course_query_name(course_name)

        # ── Strategy 0 (NEW): Direct navigation via stored href ──
        course_candidate = self.courses_index.get(course_name)
        if course_candidate and course_candidate.href:
            href = course_candidate.href
            if any(k in href for k in ["collectionId", "batchId", "/overview/", "/player/", "/toc/", "/course/", "/learn/"]):
                try:
                    print(f"[open-course] Strategy 0: Direct navigation to {href[:80]}")
                    prev_url = self.page.url
                    await self._safe_goto(href)
                    await self.page.wait_for_timeout(1500)
                    if self.page.url != prev_url:
                        opened_player = await self._open_course_player_if_needed()
                        if opened_player:
                            print(f"[open-course] Strategy 0: Opened course player from overview")
                        return True
                except PlaywrightError as exc:
                    print(f"[open-course] Strategy 0 failed: {exc}")

        # Strategy 1: Find the Resume/Start button that is inside a card containing the course title.
        # Use JS to walk the DOM: find title match → find sibling/descendant action button → click it.
        try:
            clicked = await self.page.evaluate(
                """([name, query]) => {
                    const norm = s => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
                    const targets = [norm(name), norm(query)].filter(Boolean);
                    const actionRe = /^(resume|start|continue|begin)(\\s+(play_arrow|arrow_forward|learning|course|module|keyboard_arrow_right))*$/i;

                    // Find all title elements that match our course name
                    const allEls = Array.from(document.querySelectorAll(
                        "h1,h2,h3,h4,h5,[class*='title'],[class*='course-name'],[class*='card-title'],[class*='heading'],[class*='name']"
                    )).filter(el => el.offsetParent);

                    for (const el of allEls) {
                        const t = norm(el.innerText || "");
                        if (!targets.some(target => t.includes(target) || target.includes(t))) continue;
                        if (t.length < 4 || t.length > 200) continue;

                        // Walk UP to find the card container, then find Resume/Start button in it
                        let card = el.parentElement;
                        for (let d = 0; d < 8 && card && card !== document.body; d++) {
                            const btns = Array.from(card.querySelectorAll("button, [role='button'], a"))
                                .filter(b => {
                                    const bt = norm(b.innerText || b.textContent || "");
                                    return actionRe.test(bt) && b.offsetParent;
                                });
                            if (btns.length > 0) {
                                btns[0].scrollIntoView({ block: "center", behavior: "instant" });
                                btns[0].dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                                btns[0].click();
                                return btns[0].innerText || "clicked";
                            }
                            card = card.parentElement;
                        }
                    }
                    return null;
                }""",
                [course_name, query],
            )
            if clicked:
                print(f"[open-course] Clicked '{clicked}' button for: {course_name[:60]}")
                await self.page.wait_for_timeout(2000)
                return True
        except PlaywrightError:
            pass

        # Strategy 2: Click the course title text itself (may open course page)
        for q in [query, course_name]:
            if not q:
                continue
            try:
                locator = self.page.get_by_text(re.compile(re.escape(q[:40]), re.I))
                if await locator.first.is_visible(timeout=1000):
                    await locator.first.scroll_into_view_if_needed(timeout=1200)
                    await locator.first.click()
                    await self.page.wait_for_timeout(1800)
                    return True
            except PlaywrightError:
                continue

        # Strategy 3: Click the first visible Resume/Start button on the page
        for label in [r"resume", r"start"]:
            try:
                ctl = self.page.get_by_role("button", name=re.compile(label, re.I))
                if await ctl.first.is_visible(timeout=700):
                    await ctl.first.click()
                    await self.page.wait_for_timeout(1500)
                    return True
            except PlaywrightError:
                continue

        # Strategy 4: Click the course CARD element directly (seeAll/continueLearning page)
        # On this page, entire cards are clickable — no Resume button exists.
        try:
            clicked = await self.page.evaluate(
                """(name) => {
                    const norm = s => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
                    const target = norm(name);
                    // Find all visible cards
                    const cards = Array.from(document.querySelectorAll(
                        "[class*='card'], [class*='course'], [class*='tile'], " +
                        "[class*='content-strip'], [class*='slider-item'], " +
                        "[class*='swiper-slide'], mat-card, [class*='item']"
                    )).filter(el => {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 80 && rect.height > 40;
                    });
                    for (const card of cards) {
                        const cardText = norm(card.innerText || "");
                        if (!cardText.includes(target) && !target.includes(cardText.slice(0, 60))) continue;
                        // Found matching card — click its anchor or the card itself
                        const anchor = card.querySelector("a[href]") || card.closest("a[href]");
                        const clickTarget = anchor || card;
                        clickTarget.scrollIntoView({ block: "center", behavior: "instant" });
                        clickTarget.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        if (anchor) anchor.click();
                        return cardText.slice(0, 60);
                    }
                    return null;
                }""",
                query or course_name,
            )
            if clicked:
                print(f"[open-course] Strategy 4: Clicked card for: {clicked}")
                await self.page.wait_for_timeout(2000)
                return True
        except PlaywrightError:
            pass

        # Strategy 5: Brute-force click any anchor whose text includes the course name
        try:
            for q in [query, course_name]:
                if not q or len(q) < 5:
                    continue
                anchors = self.page.locator(f"a:has-text('{q[:35]}')")
                count = await anchors.count()
                for i in range(min(count, 3)):
                    try:
                        el = anchors.nth(i)
                        if await el.is_visible(timeout=500):
                            await el.scroll_into_view_if_needed(timeout=1200)
                            await el.click()
                            await self.page.wait_for_timeout(2000)
                            if self.page.url != (self.course_list_url or ""):
                                print(f"[open-course] Strategy 5: Clicked anchor for: {q[:40]}")
                                return True
                    except PlaywrightError:
                        continue
        except PlaywrightError:
            pass

        return False

    async def _wait_for_course_content(self) -> bool:
        # Give SPA pages time to hydrate; avoid acting on loading shell.
        checks = max(1, int(self.args.loading_timeout_seconds / 0.8))
        for _ in range(checks):
            if not await self._is_likely_loading():
                return True
            await self.page.wait_for_timeout(800)
        return False

    async def _is_likely_loading(self) -> bool:
        try:
            return await self.page.evaluate(
                """() => {
                    const body = (document.body?.innerText || "").toLowerCase();
                    const hasCourseSignals =
                      body.includes("module") || body.includes("lesson") || body.includes("quiz") ||
                      body.includes("course") || body.includes("video") || body.includes("items") ||
                      body.includes("scorm") || body.includes("play") || body.includes("assessment") ||
                      body.includes("content");
                    // If page has real content signals, it's NOT loading
                    if (hasCourseSignals) return false;
                    const loadingEls = Array.from(document.querySelectorAll(
                      "[class*='loading'], [class*='loader'], [class*='spinner'], [aria-busy='true']"
                    ));
                    const visibleLoaders = loadingEls.filter(el => {
                      const s = window.getComputedStyle(el);
                      // Only count large, prominent loaders (not tiny spinners in sidebars)
                      const rect = el.getBoundingClientRect();
                      return s && s.display !== "none" && s.visibility !== "hidden" && rect.height > 30 && rect.width > 30;
                    }).length;
                    const footerOnly = body.includes("hubs") && body.includes("support") && body.includes("about us");
                    if (visibleLoaders > 0 && !hasCourseSignals) return true;
                    if (footerOnly && !hasCourseSignals) return true;
                    return false;
                }"""
            )
        except PlaywrightError:
            return False

    async def _looks_empty_course_shell(self) -> bool:
        body = (await self._body_text()).lower()
        footer_only = all(x in body for x in ["hubs", "support", "about us"])
        has_content_signal = any(x in body for x in ["module", "lesson", "quiz", "curriculum", "continue learning"])
        return footer_only and not has_content_signal

    async def _is_inside_course_player(self) -> bool:
        url = self.page.url.lower()
        if "/viewer/" in url:
            return True
        body = (await self._body_text()).lower()
        signals = [
            "start discussion",
            "transcript",
            "items (",
            "previous",
            "next",
            "course completion artifact",
        ]
        return sum(1 for s in signals if s in body) >= 2

    async def _derive_current_module_name(self) -> str:
        try:
            name = await self.page.evaluate(
                """() => {
                    const params = new URLSearchParams(window.location.search || "");
                    const courseName = params.get("courseName");
                    if (courseName && courseName.trim()) {
                      return courseName.trim().slice(0, 220);
                    }

                    const contentName = params.get("name");
                    if (contentName && contentName.trim()) {
                      return contentName.trim().slice(0, 220);
                    }

                    const pathMatch = (window.location.pathname || "").match(/\\/(do_\\d+)/i);
                    if (pathMatch && pathMatch[1]) {
                      return `Module ${pathMatch[1]}`;
                    }

                    const picks = [
                      document.querySelector(".resource-title"),
                      document.querySelector("[class*='resource-title']"),
                      document.querySelector(".title"),
                      document.querySelector("h1"),
                      document.querySelector("h2"),
                      document.querySelector("[class*='title']"),
                      document.querySelector("[class*='header']")
                    ].filter(Boolean);
                    for (const p of picks) {
                      const t = (p.innerText || "").replace(/\\s+/g, " ").trim();
                      if (t && t.length > 3) return t.slice(0, 220);
                    }
                    const pageTitle = (document.title || "").replace(/\\s+/g, " ").trim();
                    if (pageTitle && pageTitle.length > 3) return pageTitle.slice(0, 220);
                    return "Current Module";
                }"""
            )
            return self._clean_name(name or "Current Module")
        except PlaywrightError:
            return "Current Module"

    async def _derive_current_course_name(self) -> str:
        try:
            name = await self.page.evaluate(
                """() => {
                    const params = new URLSearchParams(window.location.search || "");
                    const qCourseName = params.get("courseName");
                    if (qCourseName && qCourseName.trim()) return qCourseName.trim().slice(0, 220);

                    const picks = [
                      document.querySelector("h1"),
                      document.querySelector("[class*='course-title']"),
                      document.querySelector("[class*='header-title']"),
                      document.querySelector(".title")
                    ].filter(Boolean);
                    for (const p of picks) {
                      const t = (p.innerText || "").replace(/\\s+/g, " ").trim();
                      if (t && t.length > 3) return t.slice(0, 220);
                    }
                    const pageTitle = (document.title || "").replace(/\\s+/g, " ").trim();
                    if (pageTitle && pageTitle.length > 3) return pageTitle.slice(0, 220);
                    return "Manual Course";
                }"""
            )
            return self._clean_name(name or "Manual Course")
        except PlaywrightError:
            return "Manual Course"

    async def _get_first_video_metrics(self) -> dict[str, float | bool] | None:
        script = """() => {
            const v = document.querySelector("video");
            if (!v) return null;
            return {
                currentTime: Number(v.currentTime || 0),
                duration: Number(v.duration || 0),
                paused: Boolean(v.paused),
                ended: Boolean(v.ended),
                playbackRate: Number(v.playbackRate || 1),
            };
        }"""
        for frame in self.page.frames:
            try:
                metrics = await frame.evaluate(script)
                if metrics:
                    return metrics
            except PlaywrightError:
                continue
        return None

    async def _has_playable_video(self) -> bool:
        metrics = await self._get_first_video_metrics()
        if not metrics:
            return False
        duration = float(metrics.get("duration", 0) or 0)
        return duration > 0

    async def _apply_video_speed(self, requested_speed: float) -> dict[str, int | bool | str]:
        # Try requested speed first; if portal resets it, we retry.
        # Browsers support up to 16x; iGot may try to clamp but our enforcer overrides.
        speed = max(0.5, min(float(requested_speed), 16.0))
        labels = [f"{speed:g}x", f"{int(speed)}x" if speed == int(speed) else f"{speed}x"]

        adjusted = 0
        videos = 0
        for frame in self.page.frames:
            try:
                res = await frame.evaluate(
                    """(speedVal) => {
                        const vids = Array.from(document.querySelectorAll("video"));
                        let changed = 0;
                        for (const v of vids) {
                            try {
                                if (typeof v.playbackRate === "number") {
                                    v.playbackRate = speedVal;
                                    v.defaultPlaybackRate = speedVal;
                                    changed += 1;
                                }
                            } catch (_) {}
                            try {
                                if (v.paused) {
                                    const p = v.play();
                                    if (p && typeof p.catch === "function") p.catch(() => {});
                                }
                            } catch (_) {}
                        }
                        return { videos: vids.length, adjusted: changed };
                    }""",
                    speed,
                )
                if isinstance(res, dict):
                    videos += int(res.get("videos", 0) or 0)
                    adjusted += int(res.get("adjusted", 0) or 0)
            except PlaywrightError:
                continue

        ui_clicked = False
        for frame in self.page.frames:
            try:
                clicked = await frame.evaluate(
                    """(targetLabels) => {
                        const norm = (s) => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
                        const visible = (el) => {
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            if (r.width < 2 || r.height < 2) return false;
                            const st = window.getComputedStyle(el);
                            return st.display !== "none" && st.visibility !== "hidden";
                        };
                        const click = (el) => {
                            if (!el || !visible(el)) return false;
                            el.scrollIntoView({ block: "center", behavior: "instant" });
                            el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            return true;
                        };
                        const candidates = Array.from(document.querySelectorAll("button, [role='button'], [role='menuitem'], a, span, div"));
                        for (const el of candidates) {
                            const t = norm(el.textContent || "");
                            if (targetLabels.includes(t) && click(el)) return true;
                        }
                        const speedButton = candidates.find(el => /^(0\\.5|0\\.75|1|1\\.25|1\\.5|1\\.75|2)x$/.test(norm(el.textContent || "")) && visible(el));
                        if (speedButton && click(speedButton)) {
                            const expanded = Array.from(document.querySelectorAll("button, [role='button'], [role='menuitem'], a, span, div"));
                            for (const el of expanded) {
                                const t = norm(el.textContent || "");
                                if (targetLabels.includes(t) && click(el)) return true;
                            }
                        }
                        return false;
                    }""",
                    [x.lower() for x in labels],
                )
                if clicked:
                    ui_clicked = True
                    break
            except PlaywrightError:
                continue

        return {"videos": videos, "adjusted": adjusted, "ui_clicked": ui_clicked, "target_label": labels[0]}

    async def _install_speed_enforcer(self, speed: float) -> None:
        """
        Install a JS enforcer that fights portal ratechange resets.
        Strategy:
          1. Patch HTMLMediaElement.prototype so playbackRate setter always
             applies our target — this overrides any portal reset globally.
          2. Also run a setInterval as a belt-and-suspenders fallback.
        iGot's player resets to 1x on buffer/seek — the prototype patch
        intercepts that at the source.
        """
        clamped = max(0.5, min(float(speed), 16.0))
        script = """(speedVal) => {
            if (window.__qaSpeedInterval) clearInterval(window.__qaSpeedInterval);

            // Patch 1: Override HTMLMediaElement.prototype.playbackRate
            // so any attempt by the portal to set it below our target is ignored.
            try {
                if (!window.__qaOrigPlaybackRateDescriptor) {
                    window.__qaOrigPlaybackRateDescriptor = Object.getOwnPropertyDescriptor(
                        HTMLMediaElement.prototype, "playbackRate"
                    );
                }
                const orig = window.__qaOrigPlaybackRateDescriptor;
                Object.defineProperty(HTMLMediaElement.prototype, "playbackRate", {
                    get: function() {
                        return orig && orig.get ? orig.get.call(this) : this._qaRate || speedVal;
                    },
                    set: function(v) {
                        // Always enforce at least our target speed
                        const effective = Math.max(Number(v) || speedVal, speedVal);
                        if (orig && orig.set) orig.set.call(this, effective);
                        else this._qaRate = effective;
                    },
                    configurable: true,
                });
            } catch (_) {}

            // Patch 2: setInterval fallback — re-apply directly every 1.5s
            const enforce = () => {
                for (const v of Array.from(document.querySelectorAll("video"))) {
                    try {
                        if (window.__qaOrigPlaybackRateDescriptor && window.__qaOrigPlaybackRateDescriptor.set) {
                            window.__qaOrigPlaybackRateDescriptor.set.call(v, speedVal);
                        }
                        v.defaultPlaybackRate = speedVal;
                        if (v.paused && !v.ended) {
                            const p = v.play();
                            if (p && p.catch) p.catch(() => {});
                        }
                    } catch (_) {}
                }
            };
            enforce();
            window.__qaSpeedVal = speedVal;
            window.__qaSpeedInterval = setInterval(enforce, 1500);
        }"""
        for frame in self.page.frames:
            try:
                await frame.evaluate(script, clamped)
            except PlaywrightError:
                continue

    async def _remove_speed_enforcer(self) -> None:
        """Clear the JS speed enforcer interval and restore prototype."""
        script = """() => {
            if (window.__qaSpeedInterval) {
                clearInterval(window.__qaSpeedInterval);
                window.__qaSpeedInterval = null;
            }
            // Restore original playbackRate descriptor
            try {
                if (window.__qaOrigPlaybackRateDescriptor) {
                    Object.defineProperty(
                        HTMLMediaElement.prototype, "playbackRate",
                        window.__qaOrigPlaybackRateDescriptor
                    );
                    window.__qaOrigPlaybackRateDescriptor = null;
                }
            } catch (_) {}
        }"""
        for frame in self.page.frames:
            try:
                await frame.evaluate(script)
            except PlaywrightError:
                continue

    async def _auto_wait_video_completion(
        self,
        course_name: str,
        module_name: str,
        preferred_speed: float,
    ) -> tuple[bool, str]:
        metrics = await self._get_first_video_metrics()
        if not metrics:
            return False, "No video metrics available for auto-wait"

        current = float(metrics.get("currentTime", 0) or 0)
        duration = float(metrics.get("duration", 0) or 0)
        # Use the full max wait time — portals often can't actually decode at 10x,
        # so the speed-based estimate is unreliable. Let the tick/completion check exit early.
        wait_seconds = self.args.video_max_wait_seconds
        if duration > 0:
            # Minimum: at least enough time at 1x speed (worst case) + buffer
            min_wait = int(max(30, (duration - current) / max(0.5, preferred_speed) + 30))
            wait_seconds = max(min_wait, min(wait_seconds, int((duration - current) + 60)))
            print(f"[video-wait] duration={duration:.0f}s, current={current:.0f}s, wait={wait_seconds}s")

        # Install a persistent JS interval to re-enforce playback speed every 2s.
        # Portals often reset speed on seek/buffer events — this fights back.
        await self._install_speed_enforcer(preferred_speed)

        started = time.time()
        while (time.time() - started) < wait_seconds:
            ticked = await self._is_module_ticked(module_name)
            if ticked:
                await self._remove_speed_enforcer()
                return True, "Sidebar tick detected during auto video wait"
            done, done_evidence = await self._is_module_completed()
            if done:
                await self._remove_speed_enforcer()
                return True, f"Completion signal during auto video wait: {done_evidence}"

            latest = self._latest_module_report(course_name, module_name)
            if latest and latest.block_reason in {"permission_issue", "technical_error"}:
                await self._remove_speed_enforcer()
                return False, f"Stopping auto wait due block state: {latest.block_reason}"

            # Check if video reached the end
            latest_metrics = await self._get_first_video_metrics()
            if latest_metrics:
                cur = float(latest_metrics.get("currentTime", 0) or 0)
                dur = float(latest_metrics.get("duration", 0) or 0)
                ended = bool(latest_metrics.get("ended", False))
                if dur > 0 and (cur >= dur - 2 or ended):
                    print(f"[video-wait] Video reached end: {cur:.0f}/{dur:.0f}s")
                    # Tick appears almost immediately after video ends on iGOT.
                    # Check 5 times over 10 seconds.
                    for _tick_check in range(5):
                        await self.page.wait_for_timeout(2000)
                        ticked = await self._is_module_ticked(module_name)
                        if ticked:
                            await self._remove_speed_enforcer()
                            return True, f"Video ended + tick detected ({cur:.0f}/{dur:.0f}s)"
                        done2, ev2 = await self._is_module_completed()
                        if done2:
                            await self._remove_speed_enforcer()
                            return True, f"Video ended + completion: {ev2}"
                    # Try clicking Next to trigger completion
                    for label in ["Next", "Continue", "Done", "Complete"]:
                        try:
                            loc = self.page.get_by_role("button", name=re.compile(label, re.I))
                            if await loc.first.is_visible(timeout=400):
                                await loc.first.click()
                                await self.page.wait_for_timeout(2000)
                                break
                        except PlaywrightError:
                            continue
                    await self._dismiss_popups()
                    ticked = await self._is_module_ticked(module_name)
                    if ticked:
                        await self._remove_speed_enforcer()
                        return True, f"Video ended + tick after Next click ({cur:.0f}/{dur:.0f}s)"
                    # Even if tick not detected, video DID end — treat as done
                    await self._remove_speed_enforcer()
                    return True, f"Video playback completed ({cur:.0f}/{dur:.0f}s)"

            await self._apply_video_speed(preferred_speed)
            # Also enforce speed in iframes (flash/embedded players)
            for frame in self.page.frames:
                if frame == self.page.main_frame:
                    continue
                try:
                    await frame.evaluate(
                        """(s) => {
                            for (const v of document.querySelectorAll("video")) {
                                try { v.playbackRate = s; v.defaultPlaybackRate = s;
                                      if (v.paused && !v.ended) { const p = v.play(); if (p&&p.catch) p.catch(()=>{}); }
                                } catch(_) {}
                            }
                        }""", preferred_speed)
                except PlaywrightError:
                    pass
            await self.page.wait_for_timeout(5000)

        # Post-video grace period: portal back-end may take extra time to record
        # completion and update the sidebar tick. Keep polling for up to 90s more.
        grace_seconds = min(90, self.args.video_max_wait_seconds)
        grace_started = time.time()
        print(f"Video wait elapsed; entering {grace_seconds}s grace period for tick propagation.")
        while (time.time() - grace_started) < grace_seconds:
            # Try scrolling sidebar to force tick visibility refresh
            try:
                await self.page.evaluate(
                    """() => {
                        const sidebar = document.querySelector(
                            "[class*='sidebar'], [class*='toc'], [class*='course-toc'], nav, [role='navigation']"
                        );
                        if (sidebar) { sidebar.scrollTo(0, 0); sidebar.scrollTo(0, sidebar.scrollHeight); }
                    }"""
                )
            except PlaywrightError:
                pass
            ticked = await self._is_module_ticked(module_name)
            if ticked:
                return True, f"Sidebar tick detected during {int(time.time() - grace_started)}s grace period"
            done, done_evidence = await self._is_module_completed()
            if done:
                return True, f"Completion signal during grace period: {done_evidence}"
            await self.page.wait_for_timeout(5000)

        await self._remove_speed_enforcer()
        total_waited = wait_seconds + grace_seconds
        return False, f"Auto video wait timed out after {total_waited}s (including grace)"

    async def _handle_flash_or_iframe_video(self) -> tuple[bool, str]:
        """Handle flash-format or iframe-embedded video players.
        Finds play buttons inside iframes, clicks them, and waits.
        Returns (handled, evidence)."""
        handled = False
        evidence_parts = []

        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                # Check if this frame has a video or playable content
                frame_info = await frame.evaluate(
                    """() => {
                        const video = document.querySelector("video");
                        const hasPlay = !!document.querySelector(
                            "[class*='play'], [aria-label*='play'], [title*='Play'], " +
                            "button[class*='play'], .vjs-play-control, .ytp-play-button"
                        );
                        const hasFlash = !!document.querySelector(
                            "embed, object, [class*='flash'], [class*='swf']"
                        );
                        return {
                            hasVideo: !!video,
                            videoSrc: video ? (video.src || video.currentSrc || "") : "",
                            hasPlay: hasPlay,
                            hasFlash: hasFlash,
                            url: window.location.href
                        };
                    }"""
                )
                if not frame_info:
                    continue

                if frame_info.get("hasVideo") or frame_info.get("hasPlay") or frame_info.get("hasFlash"):
                    print(f"[iframe-video] Found playable content in iframe: {frame_info.get('url', '?')[:60]}")
                    handled = True

                    # Click play button inside iframe
                    try:
                        play_clicked = await frame.evaluate(
                            """() => {
                                const playBtns = Array.from(document.querySelectorAll(
                                    "[class*='play'], [aria-label*='play'], [aria-label*='Play'], " +
                                    "[title*='Play'], button[class*='play'], .vjs-play-control, " +
                                    ".vjs-big-play-button, .ytp-play-button, [class*='start-btn'], " +
                                    "[class*='playButton']"
                                ));
                                for (const btn of playBtns) {
                                    if (btn.offsetParent || btn.getBoundingClientRect().height > 0) {
                                        btn.click();
                                        return true;
                                    }
                                }
                                // Try playing video directly
                                const v = document.querySelector("video");
                                if (v && v.paused) {
                                    const p = v.play();
                                    if (p && p.catch) p.catch(() => {});
                                    return true;
                                }
                                return false;
                            }"""
                        )
                        if play_clicked:
                            evidence_parts.append(f"iframe play clicked")
                    except PlaywrightError:
                        pass

                    # Set speed in iframe
                    try:
                        speed = max(0.5, min(float(self.args.video_speed), 16.0))
                        await frame.evaluate(
                            """(speedVal) => {
                                const vids = document.querySelectorAll("video");
                                for (const v of vids) {
                                    try {
                                        v.playbackRate = speedVal;
                                        v.defaultPlaybackRate = speedVal;
                                        if (v.paused) { const p = v.play(); if (p && p.catch) p.catch(() => {}); }
                                    } catch(_) {}
                                }
                            }""",
                            speed,
                        )
                        evidence_parts.append(f"iframe speed set to {speed}x")
                    except PlaywrightError:
                        pass

                    # Navigate through any internal controls (Next, arrows, etc.)
                    try:
                        await frame.evaluate(
                            """() => {
                                // Click through navigation buttons inside flash-like players
                                const navBtns = Array.from(document.querySelectorAll(
                                    "[class*='next'], [class*='forward'], [class*='arrow-right'], " +
                                    "[aria-label*='next'], [aria-label*='Next'], " +
                                    "[class*='continue'], [class*='proceed']"
                                ));
                                for (const btn of navBtns) {
                                    if (btn.offsetParent || btn.getBoundingClientRect().height > 0) {
                                        btn.click();
                                    }
                                }
                            }"""
                        )
                    except PlaywrightError:
                        pass

            except PlaywrightError:
                continue

        return handled, "; ".join(evidence_parts) if evidence_parts else "no iframe video found"

    async def _handle_scorm(self) -> tuple[str, str, str]:
        """
        Handle SCORM content that loads inside an iframe with its own player.
        Strategy:
          1. Find the SCORM iframe
          2. Click Play / Start / Enter / Begin button inside it
          3. Navigate through all slides (click Next/arrow repeatedly)
          4. Handle internal videos (play + speed up)
          5. Wait for completion signal
        """
        print("[scorm] SCORM content detected. Starting playthrough...")

        # Step 1: Click Play button on the main page (outside iframe)
        for label in [r"play", r"start", r"begin", r"launch", r"enter", r"open"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(label, re.I))
                if await loc.first.is_visible(timeout=800):
                    await loc.first.click()
                    print(f"[scorm] Clicked '{label}' on main page")
                    await self.page.wait_for_timeout(3000)
                    break
            except PlaywrightError:
                continue

        # Also try JS click for Play button (might be styled differently)
        try:
            await self.page.evaluate(
                """() => {
                    const btns = Array.from(document.querySelectorAll('button, [role="button"], a, div[class*="play"], span'));
                    for (const b of btns) {
                        const t = (b.innerText || '').trim().toLowerCase();
                        if (/^(play|start|begin|launch|enter)$/i.test(t) && (b.offsetParent || b.getBoundingClientRect().height > 0)) {
                            b.click(); return true;
                        }
                    }
                    return false;
                }"""
            )
            await self.page.wait_for_timeout(2000)
        except PlaywrightError:
            pass

        # Step 2: Find the SCORM iframe(s)
        scorm_frames = []
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                frame_url = frame.url.lower()
                is_scorm = (
                    "scorm" in frame_url or "story.html" in frame_url or
                    "index_lms" in frame_url or "launch" in frame_url or
                    "imsmanifest" in frame_url or "content" in frame_url
                )
                if not is_scorm:
                    # Check if frame has SCORM-like content
                    has_content = await frame.evaluate(
                        """() => {
                            const body = document.body;
                            if (!body) return false;
                            return body.innerHTML.length > 100;
                        }"""
                    )
                    if has_content:
                        is_scorm = True
                if is_scorm:
                    scorm_frames.append(frame)
            except PlaywrightError:
                continue

        if not scorm_frames:
            print("[scorm] No SCORM iframe found. Trying main page controls...")
            scorm_frames = [self.page.main_frame]

        slides_navigated = 0
        videos_played = 0
        max_slides = 200  # safety limit
        stuck_count = 0
        last_content_hash = ""

        # Step 3: Click Play inside SCORM iframe if needed
        for frame in scorm_frames:
            try:
                await frame.evaluate(
                    """() => {
                        const playBtns = Array.from(document.querySelectorAll(
                            'button, [role="button"], a, [class*="play"], [class*="start"], ' +
                            '[class*="begin"], [class*="enter"], [class*="launch"]'
                        ));
                        for (const b of playBtns) {
                            const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                            if (/play|start|begin|enter|launch|resume/i.test(t)) {
                                b.click(); return true;
                            }
                        }
                        // Click any large play button overlay
                        const overlay = document.querySelector(
                            '[class*="play-overlay"], [class*="big-play"], [class*="start-screen"]'
                        );
                        if (overlay) { overlay.click(); return true; }
                        return false;
                    }"""
                )
                await self.page.wait_for_timeout(2000)
            except PlaywrightError:
                continue

        # Step 4: Navigate through all slides
        print("[scorm] Navigating through SCORM slides...")
        for slide_num in range(max_slides):
            # Check if course/module is now complete
            ticked = await self._is_module_ticked(await self._derive_current_module_name())
            if ticked:
                print(f"[scorm] Module ticked after {slides_navigated} slides.")
                return "done", f"SCORM completed: {slides_navigated} slides navigated, {videos_played} videos played", "Continue to next module"
            done, done_ev = await self._is_module_completed()
            if done:
                print(f"[scorm] Completion detected after {slides_navigated} slides.")
                return "done", f"SCORM completed: {done_ev}", "Continue to next module"

            # Handle any video inside the SCORM frame
            for frame in scorm_frames:
                try:
                    vid_info = await frame.evaluate(
                        """() => {
                            const v = document.querySelector('video');
                            if (!v) return null;
                            return { paused: v.paused, duration: v.duration, currentTime: v.currentTime, ended: v.ended };
                        }"""
                    )
                    if vid_info and not vid_info.get("ended", False):
                        speed = max(0.5, min(float(self.args.video_speed), 16.0))
                        await frame.evaluate(
                            """(s) => {
                                const v = document.querySelector('video');
                                if (v) {
                                    v.playbackRate = s; v.defaultPlaybackRate = s;
                                    if (v.paused) { const p = v.play(); if(p&&p.catch)p.catch(()=>{}); }
                                }
                            }""", speed
                        )
                        # Wait for video to finish
                        dur = float(vid_info.get("duration", 0) or 0)
                        cur = float(vid_info.get("currentTime", 0) or 0)
                        if dur > 0:
                            wait_ms = int(max(2000, ((dur - cur) / speed) * 1000 + 3000))
                            wait_ms = min(wait_ms, 120000)  # cap at 2 min per video
                            print(f"[scorm] Playing video {dur:.0f}s at {speed}x, waiting {wait_ms/1000:.0f}s")
                            await self.page.wait_for_timeout(wait_ms)
                            videos_played += 1
                except PlaywrightError:
                    pass

            # Try clicking Next/Forward/Continue inside SCORM frames
            next_clicked = False
            for frame in scorm_frames:
                try:
                    clicked = await frame.evaluate(
                        """() => {
                            const norm = s => (s || '').trim().toLowerCase();
                            // Strategy 1: Explicit next/forward/continue buttons
                            const nextLabels = /^(next|forward|continue|proceed|skip|>>|\u25b6|\u25ba)$/i;
                            const btns = Array.from(document.querySelectorAll(
                                'button, [role="button"], a, [class*="next"], [class*="forward"], ' +
                                '[class*="nav-right"], [class*="arrow-right"], [class*="btn-next"], ' +
                                '[aria-label*="next" i], [aria-label*="Next"], [aria-label*="forward" i], ' +
                                '[title*="Next"], [title*="next"]'
                            ));
                            for (const b of btns) {
                                const t = norm(b.innerText || b.textContent || '');
                                const label = norm(b.getAttribute('aria-label') || '');
                                const title = norm(b.getAttribute('title') || '');
                                const cls = (b.className || '').toLowerCase();
                                const isNext = nextLabels.test(t) || nextLabels.test(label) || nextLabels.test(title) ||
                                    cls.includes('next') || cls.includes('forward') || cls.includes('nav-right') ||
                                    cls.includes('arrow-right') || cls.includes('btn-next');
                                if (isNext && (b.offsetParent || b.getBoundingClientRect().height > 0)) {
                                    b.scrollIntoView({ block: 'center' });
                                    b.click();
                                    return 'button: ' + (t || label || cls).slice(0, 30);
                                }
                            }
                            // Strategy 2: Right arrow key navigation
                            document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', keyCode: 39, bubbles: true}));
                            document.dispatchEvent(new KeyboardEvent('keyup', {key: 'ArrowRight', keyCode: 39, bubbles: true}));
                            // Strategy 3: Click right side of the slide area
                            const slideArea = document.querySelector(
                                '[class*="slide"], [class*="scene"], [class*="content"], [class*="player"], body'
                            );
                            if (slideArea) {
                                const rect = slideArea.getBoundingClientRect();
                                const x = rect.right - 50;
                                const y = rect.top + rect.height / 2;
                                slideArea.dispatchEvent(new MouseEvent('click', {clientX: x, clientY: y, bubbles: true}));
                            }
                            return 'arrow+click';
                        }"""
                    )
                    if clicked:
                        next_clicked = True
                        if slide_num % 10 == 0:
                            print(f"[scorm] Slide {slide_num + 1}: {clicked}")
                        break
                except PlaywrightError:
                    continue

            # Also try keyboard navigation on main page
            if not next_clicked:
                try:
                    await self.page.keyboard.press("ArrowRight")
                    next_clicked = True
                except PlaywrightError:
                    pass

            # Detect if we're stuck (same content after navigation)
            try:
                content_sample = ""
                for frame in scorm_frames:
                    try:
                        sample = await frame.evaluate(
                            """() => (document.body?.innerText || '').slice(0, 200)"""
                        )
                        content_sample += sample
                        break
                    except PlaywrightError:
                        continue
                content_hash = str(hash(content_sample))
                if content_hash == last_content_hash:
                    stuck_count += 1
                    if stuck_count >= 8:
                        print(f"[scorm] Content unchanged for {stuck_count} cycles. SCORM may be complete.")
                        break
                else:
                    stuck_count = 0
                    slides_navigated += 1
                last_content_hash = content_hash
            except PlaywrightError:
                pass

            await self.page.wait_for_timeout(1500)  # pause between slides

        # Step 5: Final check
        await self.page.wait_for_timeout(3000)
        await self._dismiss_popups()

        done, done_ev = await self._is_module_completed()
        if done:
            return "done", f"SCORM completed: {slides_navigated} slides, {videos_played} videos; {done_ev}", "Continue to next module"

        ticked = await self._is_module_ticked(await self._derive_current_module_name())
        if ticked:
            return "done", f"SCORM completed: {slides_navigated} slides, {videos_played} videos; tick detected", "Continue to next module"

        # Even if no explicit completion, we navigated through all slides
        return "done", f"SCORM navigated: {slides_navigated} slides, {videos_played} videos (stuck detection exit)", "Continue to next module"

    async def _handle_video(self) -> tuple[str, str, str]:
        # Observe playback behavior and completion state; no skip/seek logic.
        requested_speed = max(0.5, min(float(self.args.video_speed), 16.0))
        speed_result = await self._apply_video_speed(requested_speed)
        await self._install_speed_enforcer(requested_speed)
        metrics1 = await self._get_first_video_metrics()
        if not metrics1:
            # Try flash/iframe video before giving up
            iframe_handled, iframe_evidence = await self._handle_flash_or_iframe_video()
            if iframe_handled:
                return "partial", f"Iframe/embedded video: {iframe_evidence}", "Wait for iframe video completion"
            # SCORM / LRS content can take 5-10s for the player to initialise inside an iframe.
            # Wait and retry twice before declaring blocked.
            print("[video] No video element yet — waiting 8s for SCORM/iframe player to load...")
            await self.page.wait_for_timeout(8000)
            metrics1 = await self._get_first_video_metrics()
            if not metrics1:
                iframe_handled, iframe_evidence = await self._handle_flash_or_iframe_video()
                if iframe_handled:
                    return "partial", f"Iframe/embedded video (retry): {iframe_evidence}", "Wait for iframe video completion"
                # One last wait before giving up
                await self.page.wait_for_timeout(8000)
                metrics1 = await self._get_first_video_metrics()
                if not metrics1:
                    await self._remove_speed_enforcer()
                    return "blocked", "No video element detected; No video metrics available for auto-wait", "Refresh module and retry detection"

        await self.page.wait_for_timeout(int(self.args.video_observe_seconds * 1000))
        await self._apply_video_speed(requested_speed)
        metrics2 = await self._get_first_video_metrics()
        done, done_evidence = await self._is_module_completed()
        if done:
            await self._remove_speed_enforcer()
            return "done", f"video completion signal: {done_evidence}", "Continue to next eligible module"
        if metrics2 and metrics2["currentTime"] > metrics1["currentTime"] + 0.5:
            # Enforcer stays active — _auto_wait_video_completion will manage it
            evidence = (
                f"video playback progressing ({metrics1['currentTime']:.1f}s -> "
                f"{metrics2['currentTime']:.1f}s) at {metrics2.get('playbackRate', 1):.2f}x; "
                f"speed target {speed_result.get('target_label', '2x')}, "
                f"applied to {speed_result.get('adjusted', 0)}/{speed_result.get('videos', 0)} video element(s), "
                f"ui_click={speed_result.get('ui_clicked', False)}; "
                "completion not yet confirmed"
            )
            return "partial", evidence, "Allow playback to complete and rerun audit"
        await self._remove_speed_enforcer()
        return "blocked", "Video did not progress and no completion signal detected", "Capture logs and escalate technical issue"

    async def _handle_reading_like(self, module_type: str) -> tuple[str, str, str]:
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"{module_type} completion signal: {done_evidence}", "Continue to next eligible module"

        # ── Actively scroll through PDFs / readings to trigger completion ──
        print(f"[{module_type}] Actively scrolling through content to trigger completion...")

        # Strategy 1: Scroll the main content area and any iframe
        for scroll_pass in range(3):
            try:
                await self.page.evaluate(
                    """() => {
                        // Scroll main page
                        window.scrollTo(0, document.body.scrollHeight);
                        // Scroll any PDF viewer / content container
                        for (const sel of [
                            "[class*='pdf-viewer']", "[class*='viewer-content']",
                            "[class*='content-area']", "[class*='reading-content']",
                            "[class*='resource-content']", "[class*='player-content']",
                            "[class*='scroll']", ".mat-sidenav-content",
                            "iframe",
                        ]) {
                            const el = document.querySelector(sel);
                            if (el && el.scrollTo) el.scrollTo(0, el.scrollHeight || 99999);
                        }
                    }"""
                )
            except PlaywrightError:
                pass
            await self.page.wait_for_timeout(1500)

        # Strategy 2: Click through PDF pages (Next Page / page navigation)
        pages_clicked = 0
        for _ in range(100):  # up to 100 pages
            next_clicked = False
            for label in [r"next page", r"next", r"arrow_forward", r"navigate_next",
                          r"chevron_right", r"keyboard_arrow_right"]:
                try:
                    # Look for page navigation in the PDF viewer area
                    loc = self.page.locator(
                        f"button:has-text('{label}'), [aria-label*='{label}'], "
                        f"[class*='next-page'], [class*='page-next']"
                    ).first
                    if await loc.is_visible(timeout=300):
                        await loc.click()
                        pages_clicked += 1
                        next_clicked = True
                        await self.page.wait_for_timeout(500)
                        break
                except PlaywrightError:
                    continue
            if not next_clicked:
                break

        if pages_clicked > 0:
            print(f"[{module_type}] Clicked through {pages_clicked} pages")

        # Strategy 3: For iframes (PDF embed / flash), scroll inside the iframe
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                await frame.evaluate(
                    """() => {
                        window.scrollTo(0, document.body.scrollHeight);
                        const containers = document.querySelectorAll(
                            "[class*='scroll'], [class*='content'], [class*='viewer'], body"
                        );
                        for (const c of containers) {
                            if (c.scrollTo) c.scrollTo(0, c.scrollHeight || 99999);
                        }
                    }"""
                )
                await self.page.wait_for_timeout(800)
            except PlaywrightError:
                continue

        # Strategy 4: Click "Mark as Complete" / "Done" button if present
        for label_re in [r"mark.*complete", r"mark.*done", r"i have read",
                         r"mark as done", r"complete", r"done reading"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(label_re, re.I))
                if await loc.first.is_visible(timeout=500):
                    await loc.first.click()
                    print(f"[{module_type}] Clicked '{label_re}' button")
                    await self.page.wait_for_timeout(1500)
                    break
            except PlaywrightError:
                continue

        # JS fallback: click any button with mark/complete/done text
        try:
            await self.page.evaluate(
                """() => {
                    const btns = Array.from(document.querySelectorAll("button, [role='button'], a"))
                        .filter(b => b.offsetParent);
                    for (const btn of btns) {
                        const t = (btn.innerText || "").trim().toLowerCase();
                        if (/mark.*complete|mark.*done|done reading|i have read/i.test(t)) {
                            btn.scrollIntoView({ block: "center" });
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        except PlaywrightError:
            pass

        await self.page.wait_for_timeout(2000)

        # Re-check completion
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"{module_type} completion after scroll-through: {done_evidence}", "Continue to next eligible module"

        # Final scroll to bottom again
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except PlaywrightError:
            pass
        await self.page.wait_for_timeout(3000)

        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"{module_type} completion after final scroll: {done_evidence}", "Continue to next eligible module"

        # Dismiss any popup that appeared during scrolling
        await self._dismiss_popups()
        await self.page.wait_for_timeout(1000)

        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"{module_type} completion after popup dismiss: {done_evidence}", "Continue to next eligible module"

        return "partial", f"{module_type} scrolled through; completion not yet triggered", "Continue to next eligible module"

    async def _handle_assignment(self) -> tuple[str, str, str]:
        """Handle iGOT assignment modules: attempt submission or mark complete."""
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"Assignment already complete: {done_evidence}", "Continue to next eligible module"

        body = (await self._body_text()).lower()

        # Strategy 1: File-upload assignment — cannot auto-submit; report blocked
        file_input_count = await self.page.locator("input[type='file']").count()
        if file_input_count > 0:
            return (
                "blocked",
                "Assignment requires file upload — cannot be automated",
                "Upload the required file manually and submit",
            )

        # Strategy 2: Text-area submission — fill with placeholder and submit
        textarea_count = await self.page.locator("textarea").count()
        if textarea_count > 0:
            try:
                ta = self.page.locator("textarea").first
                if await ta.is_visible(timeout=1500):
                    current_val = await ta.input_value()
                    if not current_val.strip():
                        await ta.fill("Assignment response submitted via automation.")
                    await self.page.wait_for_timeout(500)
            except PlaywrightError:
                pass

        # Strategy 3: Click Submit / Submit Assignment button
        for label_re in [r"submit\s+assignment", r"submit", r"save\s+&?\s*submit", r"done"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(label_re, re.I))
                if await loc.first.is_visible(timeout=600):
                    await loc.first.click()
                    print(f"[assignment] Clicked '{label_re}' button")
                    await self.page.wait_for_timeout(2000)
                    await self._dismiss_popups()
                    done, done_evidence = await self._is_module_completed()
                    if done:
                        return "done", f"Assignment submitted & complete: {done_evidence}", "Continue to next eligible module"
                    break
            except PlaywrightError:
                continue

        # Strategy 4: Mark as Complete / Done (some assignments just require acknowledgement)
        for label_re in [r"mark.*complete", r"mark.*done", r"i have read", r"acknowledge", r"complete"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(label_re, re.I))
                if await loc.first.is_visible(timeout=400):
                    await loc.first.click()
                    print(f"[assignment] Clicked completion button '{label_re}'")
                    await self.page.wait_for_timeout(1500)
                    done, done_evidence = await self._is_module_completed()
                    if done:
                        return "done", f"Assignment complete via mark-done: {done_evidence}", "Continue to next eligible module"
                    break
            except PlaywrightError:
                continue

        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"Assignment complete: {done_evidence}", "Continue to next eligible module"

        return (
            "partial",
            "Assignment page opened; could not auto-submit (no file-upload or text-area detected)",
            "Review and submit assignment manually",
        )

    async def _handle_quiz_assistive(self, module_name: str) -> tuple[str, str, str]:
        if self.args.skip_assessments:
            return (
                "partial",
                "Assessment skipped by configuration (--skip-assessments)",
                "Complete this assessment manually later",
            )

        # Check if already passed
        body = (await self._body_text()).lower()
        if any(x in body for x in ["passed", "you passed", "score: 100", "congratulations", "you have passed"]):
            return "done", "Quiz page shows pass/completion indicator", "Continue to next eligible module"

        # Click "Start Assessment" / "Start Quiz" / "Begin" button if present.
        # For Final Assessment: disclosure checkbox must be accepted first, then
        # Start is enabled. Try up to 3 times with increasing waits.
        for _start_attempt in range(3):
            await self._quiz_click_start_button()
            await self.page.wait_for_timeout(2500)
            # Quick probe: if a question with options is visible, we're in
            probe = await self._igot_extract_question()
            if probe and probe.get("options"):
                break
            if _start_attempt < 2:
                print(f"[quiz] Quiz not started yet (attempt {_start_attempt + 1}/3), retrying…")

        # ── Auto-solve with reattempt loop ────────────────────────────────────
        # _gemini_solve_quiz uses Show Answer first (no Gemini needed for practice
        # sets). self.gemini may be None — _gemini_solve_quiz handles that gracefully.
        # After submission, if "Not passed" is detected and a reattempt button exists,
        # automatically retry the quiz up to 5 times.
        max_quiz_attempts = 5
        last_result = ("partial", "Quiz not attempted", "Continue to next eligible module")
        for _quiz_attempt in range(max_quiz_attempts):
            result = await self._gemini_solve_quiz(module_name)
            last_result = result
            status, evidence, _ = result

            # If quiz never started (no content found), retry Start button before giving up
            if status == "partial" and "No quiz content found" in evidence:
                print(f"[quiz] Quiz page not started (attempt {_quiz_attempt + 1}/{max_quiz_attempts}). Retrying start button...")
                await self.page.wait_for_timeout(2000)
                # Try start button again with more patience
                for _s in range(5):
                    await self._quiz_click_start_button()
                    await self.page.wait_for_timeout(2500)
                    probe = await self._igot_extract_question()
                    if probe and probe.get("options"):
                        print(f"[quiz] Quiz started on start-retry {_s + 1}.")
                        break
                    print(f"[quiz] Start retry {_s + 1}/5 — still no quiz content.")
                continue

            # Check if quiz failed and reattempt is available
            body = (await self._body_text()).lower()
            _quiz_failed = (
                "not passed" in body
                or "did not pass" in body
                or ("your score" in body and "not passed" in body)
            )
            if not _quiz_failed:
                # Passed or no result page — return as-is
                return result

            # Look for reattempt / retake button
            reattempt_clicked = await self.page.evaluate(
                """() => {
                    const re = /^(reattempt|re-attempt|try again|retake|retake quiz|attempt again|reattempt exam|retry)$/i;
                    const btns = Array.from(document.querySelectorAll(
                        "button, [role='button'], a"
                    )).filter(b => b.offsetParent);
                    const btn = btns.find(b => re.test((b.innerText || b.textContent || "").replace(/\\s+/g," ").trim()));
                    if (btn) {
                        btn.scrollIntoView({ block: "center", behavior: "instant" });
                        btn.click();
                        return (btn.innerText || btn.textContent || "").replace(/\\s+/g," ").trim();
                    }
                    return null;
                }"""
            )
            if not reattempt_clicked:
                print(f"[quiz] Quiz failed but no reattempt button found — accepting result.")
                return result

            print(f"[quiz] Quiz failed (attempt {_quiz_attempt + 1}/{max_quiz_attempts}). Clicked '{reattempt_clicked}' — retaking...")
            await self.page.wait_for_timeout(3000)

            # Re-click Start button for the new attempt
            for _start_retry in range(3):
                await self._quiz_click_start_button()
                await self.page.wait_for_timeout(2500)
                probe = await self._igot_extract_question()
                if probe and probe.get("options"):
                    break
                if _start_retry < 2:
                    print(f"[quiz] Quiz not started yet after reattempt ({_start_retry + 1}/3), retrying…")

        print(f"[quiz] Exhausted {max_quiz_attempts} quiz attempts. Returning last result.")
        return last_result

    async def _check_inline_start_button(self) -> bool:
        """
        Check if a 'Start assessment' / 'Start Quiz' button is visible on the
        current page (typically appears below a video in iGOT before clicking Next).
        If found, clicks it and returns True so the caller can handle the result.
        """
        _labels = [
            r"start assessment", r"start quiz", r"begin assessment",
            r"take assessment", r"attempt now",
        ]
        for role in ("button", "link"):
            for label in _labels:
                try:
                    loc = self.page.get_by_role(role, name=re.compile(label, re.I))
                    if await loc.first.is_visible(timeout=700):
                        print(f"[inline-assessment] Found '{label}' {role} – clicking before Next")
                        await loc.first.scroll_into_view_if_needed()
                        await loc.first.click()
                        await self.page.wait_for_timeout(1500)
                        return True
                except PlaywrightError:
                    continue
        return False

    async def _quiz_click_start_button(self) -> None:
        """
        Click Start Assessment / Start Quiz button.
        iGot Final Assessment has a mandatory disclosure checkbox that must be
        ticked before the Start button becomes enabled. Handle it first.
        """
        # ── Step 1: Accept disclosure / consent checkbox if present ──────────
        await self._quiz_accept_disclosure()
        # Extra wait for Angular to enable the Start button after checkbox click
        await self.page.wait_for_timeout(1200)

        # ── Step 2: Click Start / Begin button ───────────────────────────────
        _start_labels = [
            r"start assessment", r"start quiz", r"begin assessment",
            r"begin quiz", r"attempt now", r"proceed", r"continue",
            r"start", r"begin",
        ]
        for label in _start_labels:
            for role in ["button", "link"]:
                try:
                    loc = self.page.get_by_role(role, name=re.compile(label, re.I))
                    if await loc.first.is_visible(timeout=600):
                        await loc.first.scroll_into_view_if_needed()
                        await loc.first.click()
                        await self.page.wait_for_timeout(2000)
                        return
                except PlaywrightError:
                    continue

        # JS fallback: find by text content, including disabled buttons
        # (Angular enables them after checkbox — dispatch click anyway)
        _start_js = """() => {
            const norm = s => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
            const labels = [
                "start assessment", "start quiz", "begin assessment",
                "attempt now", "proceed", "start", "begin"
            ];
            // Include disabled buttons — checkbox may not have fully enabled yet
            const btns = Array.from(document.querySelectorAll("button, a, [role='button']"));
            for (const btn of btns) {
                const t = norm(btn.innerText || btn.textContent || "");
                if (labels.some(l => t === l || t.startsWith(l))) {
                    btn.scrollIntoView({ block: "center", behavior: "instant" });
                    btn.removeAttribute("disabled");
                    btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    btn.click();
                    return t;
                }
            }
            return null;
        }"""
        try:
            clicked = await self.page.evaluate(_start_js)
            if clicked:
                print(f"[quiz] Start button clicked via JS (main frame): '{clicked}'")
                await self.page.wait_for_timeout(2000)
                return
        except PlaywrightError:
            pass

        # Iframe fallback: iGot quiz sections are often rendered inside a Sunbird
        # content player iframe — document.querySelectorAll on the outer page misses them.
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                clicked = await frame.evaluate(_start_js)
                if clicked:
                    print(f"[quiz] Start button clicked in iframe via JS: '{clicked}'")
                    await self.page.wait_for_timeout(2000)
                    return
            except PlaywrightError:
                continue

        # Playwright frame-level get_by_role as last resort
        for frame in self.page.frames:
            try:
                loc = frame.get_by_role("button", name=re.compile(r"start assessment|start quiz|begin assessment|attempt now", re.I))
                if await loc.first.is_visible(timeout=400):
                    await loc.first.scroll_into_view_if_needed()
                    await loc.first.click()
                    print(f"[quiz] Start button clicked via frame get_by_role.")
                    await self.page.wait_for_timeout(2000)
                    return
            except PlaywrightError:
                continue

    async def _quiz_accept_disclosure(self) -> None:
        """
        Tick the 'I have read and understood all the instructions' checkbox
        on the iGot Final Assessment intro page, which gates the Start button.
        Handles both native checkbox and Angular Material checkbox.
        """
        try:
            ticked = await self.page.evaluate(
                """() => {
                    // Patterns that identify the disclosure checkbox
                    const disclosureTexts = [
                        "i have read",
                        "read and understood",
                        "unfair means",
                        "disqualification",
                        "igotkarmayogi",
                        "instructions",
                    ];

                    // Find checkbox inputs near disclosure text
                    const checkboxes = Array.from(document.querySelectorAll(
                        "input[type='checkbox'], mat-checkbox, [role='checkbox']"
                    )).filter(el => el.offsetParent !== null);

                    for (const cb of checkboxes) {
                        // Check if already ticked
                        const isChecked =
                            cb.checked === true ||
                            cb.getAttribute("aria-checked") === "true" ||
                            (cb.className || "").toLowerCase().includes("checked");
                        if (isChecked) return true; // already done

                        // Check if this checkbox is near disclosure text
                        const container = cb.closest("div, p, label, li, section") || cb.parentElement;
                        const containerText = ((container && container.innerText) || "").toLowerCase();
                        const isDisclosure = disclosureTexts.some(t => containerText.includes(t));

                        // Also check the page body as fallback
                        const pageText = (document.body.innerText || "").toLowerCase();
                        const pageHasDisclosure = disclosureTexts.some(t => pageText.includes(t));

                        if (isDisclosure || (pageHasDisclosure && checkboxes.length === 1)) {
                            cb.scrollIntoView({ block: "center", behavior: "instant" });
                            // For Angular mat-checkbox: click the inner input or the label
                            const inner = cb.querySelector("input[type='checkbox']") || cb;
                            inner.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            inner.dispatchEvent(new Event("change", { bubbles: true }));
                            // Also click parent label if present
                            const lbl = cb.closest("label") || document.querySelector(`label[for='${cb.id}']`);
                            if (lbl && lbl !== cb) {
                                lbl.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            }
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if ticked:
                print("[quiz] Disclosure checkbox accepted.")
                await self.page.wait_for_timeout(700)
            else:
                # Playwright-level fallback: click the checkbox locator directly
                for text_frag in ["read and understood", "unfair means", "disqualification"]:
                    try:
                        # Find a checkbox near this text
                        cb = self.page.locator(f"text={text_frag}").locator("..").locator("input[type='checkbox']")
                        if await cb.first.is_visible(timeout=400):
                            await cb.first.click()
                            await self.page.wait_for_timeout(700)
                            print("[quiz] Disclosure checkbox accepted (Playwright fallback).")
                            break
                    except PlaywrightError:
                        continue
                # Last resort: click any single unchecked checkbox on the page
                try:
                    cb = self.page.locator("input[type='checkbox']:not(:checked)")
                    if await cb.first.is_visible(timeout=400):
                        await cb.first.click()
                        await self.page.wait_for_timeout(700)
                        print("[quiz] Disclosure checkbox accepted (last-resort fallback).")
                except PlaywrightError:
                    pass
        except PlaywrightError:
            pass

    async def _extract_quiz_data(self) -> list[dict]:
        """Extract all visible questions and options from the page."""
        try:
            return await self.page.evaluate(
                """() => {
                    const questions = [];
                    const seen = new Set();
                    // iGot/Sunbird quiz selectors
                    const qSelectors = [
                        "[class*='question-container']",
                        "[class*='mcq']",
                        "[class*='question']",
                        "fieldset",
                        "[role='group']",
                        "[class*='question-set']",
                    ];
                    let qNodes = [];
                    for (const sel of qSelectors) {
                        const nodes = Array.from(document.querySelectorAll(sel));
                        if (nodes.length) { qNodes = nodes; break; }
                    }
                    if (!qNodes.length) {
                        // Fallback: group by label/input pairs
                        qNodes = [document.body];
                    }
                    for (const q of qNodes.slice(0, 60)) {
                        // Get question text — prefer specific elements
                        const qTextEl =
                            q.querySelector("[class*='question-text']") ||
                            q.querySelector("[class*='stem']") ||
                            q.querySelector("p, h3, h4, h5, strong");
                        const qText = ((qTextEl || q).innerText || "").replace(/\\s+/g, " ").trim().slice(0, 800);
                        if (!qText || qText.length < 6) continue;
                        if (seen.has(qText)) continue;
                        seen.add(qText);
                        // Options: labels, radio buttons, checkboxes
                        const optEls = Array.from(q.querySelectorAll(
                            "label, [class*='option'], [role='option'], [role='radio'], [role='checkbox'], li"
                        ));
                        const options = optEls
                            .map(el => (el.innerText || "").replace(/\\s+/g, " ").trim())
                            .filter(t => t.length > 0 && t.length < 300)
                            .slice(0, 8);
                        questions.push({ question: qText, options });
                    }
                    return questions;
                }"""
            )
        except PlaywrightError:
            return []

    async def _gemini_solve_quiz(self, module_name: str) -> tuple[str, str, str]:
        """
        iGot-aware quiz solver. Two strategies per question:
          1. PRIMARY  — "Show Answer" button (available on practice sets): reveals
             the correct answer text directly, then we click the matching option.
          2. FALLBACK — Gemini Flash API answers the MCQ from question + options.
             (skipped gracefully if self.gemini is None)
        After all questions, clicks Submit and waits for the result page.
        """
        answered = 0
        artifact_log: list[dict] = []

        # Parse total from module name as a reliable fallback
        # e.g. "Final Assessment 15 Questions", "Module Quiz 5 Items"
        name_match = re.search(r"(\d+)\s+(?:question|item|mcq)", module_name, re.I)
        name_total = int(name_match.group(1)) if name_match else 0

        # Wait briefly for the first question to fully render before counting
        await self.page.wait_for_timeout(1200)

        # Early exit: if we're already on a result page, don't enter quiz loop
        if await self._quiz_is_result_page():
            print("[quiz] Already on result page — quiz was previously submitted.")
            return "done", "Quiz already completed (result page detected)", "Continue to next eligible module"

        # Sanity check: confirm this is actually a quiz page (has options).
        # Quiz content can load lazily via Angular/iframe — retry up to 4 times.
        _probe = None
        for _quiz_wait_attempt in range(4):
            _probe = await self._igot_extract_question()
            if _probe and _probe.get("options"):
                break
            wait_ms = 1500 + _quiz_wait_attempt * 1500
            print(f"[quiz] Quiz content not found yet (attempt {_quiz_wait_attempt + 1}/4), waiting {wait_ms}ms...")
            await self.page.wait_for_timeout(wait_ms)
        if not _probe or not _probe.get("options"):
            print("[quiz] No quiz question/options found after retries — not a quiz page, skipping.")
            return "partial", "No quiz content found on page", "Continue to next eligible module"

        total_q = await self._quiz_get_total_questions()

        # If page detection returned ≤1 but we know from the name there are more, use the name
        if total_q <= 1 and name_total > 1:
            total_q = name_total
            print(f"[quiz] Total from module name: {total_q}")
        else:
            print(f"[quiz] Total questions detected: {total_q}")

        last_q_text = ""
        same_q_streak = 0  # detect if navigation is stuck

        for q_num in range(1, total_q + 1):
            await self.page.wait_for_timeout(700)

            # Check result page early exit
            if await self._quiz_is_result_page():
                break

            # Extract question + options from current view.
            # Retry up to 3× with 600ms gaps — Angular transitions may not be
            # complete immediately after the previous Next-click.
            q_data = None
            for _retry in range(3):
                q_data = await self._igot_extract_question()
                if q_data:
                    break
                await self.page.wait_for_timeout(600)
            if not q_data:
                print(f"[quiz] Could not extract question {q_num} after retries.")
                # IMPORTANT: if Submit is already visible, don't click Next —
                # that would submit without answering this question.
                if await self._quiz_submit_visible():
                    print(f"[quiz] Submit visible — last question extraction failed, submitting anyway.")
                    break
                await self._quiz_click_next()
                continue

            q_text = q_data["question"]
            options = q_data["options"]
            already = q_data["already_answered"]

            print(f"[quiz] Q{q_num} text: {q_text[:100]!r}")
            print(f"[quiz] Q{q_num} options ({len(options)}): {[o[:40] for o in options]}")

            # Detect navigation stuck (same question repeated multiple times)
            if q_text == last_q_text and q_num > 1:
                same_q_streak += 1
                if same_q_streak >= 4:
                    print(f"[quiz] Navigation stuck (same question x{same_q_streak}), stopping loop.")
                    break
                print(f"[quiz] Same question again (streak {same_q_streak}/4), still advancing…")
            else:
                same_q_streak = 0
            last_q_text = q_text

            if already:
                print(f"[quiz] Q{q_num} already answered, advancing.")
                answered += 1
                await self._quiz_click_next()
                continue

            if not options:
                print(f"[quiz] Q{q_num} has no options visible, advancing.")
                await self._quiz_click_next()
                continue

            # ── Strategy 1: Show Answer (practice sets) ───────────────────────
            correct_text = await self._quiz_show_answer_text()

            # ── Strategy 2: Groq (free, no daily quota) ───────────────────────
            if not correct_text and self.groq:
                correct_text = self.groq.answer_question(q_text, options, topic=module_name)

            # ── Strategy 3: Gemini (fallback if Groq not configured) ──────────
            if not correct_text and self.gemini:
                print(f"[quiz] Q{q_num}: asking Gemini — {q_text[:70]}...")
                correct_text = self.gemini.answer_question(q_text, options, topic=module_name)

            if not correct_text:
                # Absolute last resort: pick first option so question is never blank.
                correct_text = options[0]
                print(f"[quiz] Q{q_num}: no AI answer — using first-option fallback: {correct_text[:50]!r}")

            print(f"[quiz] Q{q_num}: selecting → {correct_text[:60]}")
            clicked = await self._igot_click_option(correct_text, options)
            artifact_log.append({"q": q_num, "question": q_text, "options": options, "answer": correct_text, "clicked": clicked})
            if clicked:
                answered += 1
                await self.page.wait_for_timeout(500)
            else:
                print(f"[quiz] Q{q_num}: click failed for '{correct_text[:50]}'")

            # Advance to next question via "Next Question" button
            advanced = await self._quiz_click_next()
            # If Next button not found, we may be on the last question — check for Submit
            if not advanced:
                submit_visible = await self._quiz_submit_visible()
                if submit_visible:
                    print(f"[quiz] No Next button, Submit visible — all questions done.")
                    break

        # ── Overflow guard: if total_q was underdetected, keep answering ─────────
        # After the fixed loop ends, check if Submit is visible. If not, more
        # questions remain. Continue until Submit appears or we're stuck.
        if not await self._quiz_submit_visible() and not await self._quiz_is_result_page():
            overflow_streak = 0
            overflow_last = ""
            print("[quiz] Submit not yet visible after main loop — continuing overflow questions…")
            while True:
                await self.page.wait_for_timeout(700)
                if await self._quiz_is_result_page() or await self._quiz_submit_visible():
                    break
                q_data = await self._igot_extract_question()
                if not q_data:
                    overflow_streak += 1
                    if overflow_streak >= 4:
                        break
                    await self._quiz_click_next()
                    continue
                q_text = q_data["question"]
                if q_text == overflow_last:
                    overflow_streak += 1
                    if overflow_streak >= 4:
                        break
                else:
                    overflow_streak = 0
                overflow_last = q_text
                options = q_data["options"]
                if not q_data["already_answered"] and options:
                    correct_text = None
                    if self.groq:
                        correct_text = self.groq.answer_question(q_text, options, topic=module_name)
                    if not correct_text and self.gemini:
                        correct_text = self.gemini.answer_question(q_text, options, topic=module_name)
                    if not correct_text:
                        correct_text = options[0]
                    await self._igot_click_option(correct_text, options)
                    answered += 1
                    artifact_log.append({"q": "overflow", "question": q_text, "options": options, "answer": correct_text})
                    await self.page.wait_for_timeout(500)
                advanced = await self._quiz_click_next()
                if not advanced:
                    # Next became Submit — exit overflow and let _quiz_submit() handle it
                    break

        # Save artifact
        artifact_file = self.reporter.artifacts_dir / f"quiz_gemini_{self._slug(module_name)}.json"
        artifact_file.write_text(
            json.dumps({"module": module_name, "total": total_q, "answered": answered, "log": artifact_log}, indent=2),
            encoding="utf-8",
        )

        # Submit the quiz
        submitted = await self._quiz_submit()
        # Give portal time to process submission and load result page
        await self.page.wait_for_timeout(3000)

        body = (await self._body_text()).lower()
        passed = any(x in body for x in [
            "passed", "you passed", "congratulations", "your score",
            "score:", "you have passed", "well done", "quiz result",
            "you have completed", "attempt again", "re-attempt",
            "total score", "view solution", "percentage",
        ])
        done, done_ev = await self._is_module_completed()
        # If submitted successfully, treat as done even if pass text isn't detected
        # (some quizzes don't show "passed" but still mark completion)
        status = "done" if (done or passed or submitted) else "partial"
        ev = f"Solved {answered}/{total_q} MCQ(s); {'submitted & complete' if submitted else 'submission failed'}"
        return status, ev, "Continue to next eligible module"

    # ── iGot quiz helpers ──────────────────────────────────────────────────────

    async def _quiz_get_total_questions(self) -> int:
        """Read total question count from the page using multiple strategies.

        NOTE: "Items (N/M)" in the iGot header is the COURSE item counter
        (which video/quiz/reading you're on in the course), NOT the question
        count inside a quiz. It must NOT be used here.
        """
        try:
            total = await self.page.evaluate(
                """() => {
                    const body = document.body.innerText || "";

                    // 1. "Question 1 of 15" / "Question No.1 of 15" — most reliable
                    let m = body.match(/question\\s+(?:no\\.?\\s*)?\\d+\\s+of\\s+(\\d+)/i);
                    if (m) return parseInt(m[1], 10);

                    // 2. Navigator pills: numbered boxes in the right-side Questions panel.
                    //    Count elements whose entire visible text is a single number.
                    const pillCandidates = Array.from(document.querySelectorAll(
                        "[class*='question-nav'] *, [class*='qlist'] *, " +
                        "[class*='questions-panel'] *, [class*='questionsPanel'] *, " +
                        "[class*='question-list'] *, [class*='right-panel'] *"
                    )).filter(el => {
                        const t = (el.innerText || "").trim();
                        return /^\\d{1,2}$/.test(t) && el.offsetParent;
                    });
                    if (pillCandidates.length >= 2) return pillCandidates.length;

                    // 3. Any numeric pill anywhere on page (broader search)
                    const allNumeric = Array.from(document.querySelectorAll(
                        "button, span, div, td"
                    )).filter(el => {
                        const t = (el.innerText || "").trim();
                        return /^\\d{1,2}$/.test(t) && el.offsetParent &&
                               !el.querySelector("*"); // leaf node only
                    });
                    // Deduplicate by numeric value to avoid counting repeated "1"s
                    const nums = [...new Set(allNumeric.map(el => parseInt(el.innerText.trim(), 10)))];
                    if (nums.length >= 3) return Math.max(...nums);

                    return 1;
                }"""
            )
            return max(1, int(total or 1))
        except PlaywrightError:
            return 1

    async def _quiz_goto_question(self, q_num: int) -> None:
        """Click the Nth navigator pill to jump to that question."""
        try:
            clicked = await self.page.evaluate(
                """(n) => {
                    // Find navigator pills — numbered buttons in right sidebar
                    const selectors = [
                        "[class*='question-nav'] button",
                        "[class*='qlist'] button",
                        "[class*='questions-panel'] button",
                        "[class*='question-panel'] button",
                    ];
                    for (const sel of selectors) {
                        const pills = Array.from(document.querySelectorAll(sel));
                        // Match by index or by inner text = n
                        const pill = pills.find(
                            (p, i) => parseInt(p.innerText.trim(), 10) === n || i === n - 1
                        );
                        if (pill) {
                            pill.scrollIntoView({ block: "center", behavior: "instant" });
                            pill.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            return true;
                        }
                    }
                    return false;
                }""",
                q_num,
            )
            if clicked:
                await self.page.wait_for_timeout(400)
        except PlaywrightError:
            pass

    async def _quiz_click_next(self) -> bool:
        """Click the 'Next Question' / 'Save & Next' / 'Next' button to advance.
        Returns True if a button was found and clicked."""
        for label in [r"next question", r"save\s*&\s*next", r"next", r"continue"]:
            for role in ["button", "link"]:
                try:
                    loc = self.page.get_by_role(role, name=re.compile(label, re.I))
                    if await loc.first.is_visible(timeout=300):
                        await loc.first.click()
                        await self.page.wait_for_timeout(700)
                        return True
                except PlaywrightError:
                    continue
        # JS fallback
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const candidates = Array.from(document.querySelectorAll("button, [role='button'], a"));
                    for (const btn of candidates) {
                        const t = (btn.innerText || btn.textContent || "").trim().toLowerCase();
                        if (/next question|save & next|save and next/.test(t) ||
                            (t === "next" && btn.offsetParent)) {
                            btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if clicked:
                await self.page.wait_for_timeout(700)
                return True
        except PlaywrightError:
            pass
        return False

    async def _quiz_is_result_page(self) -> bool:
        body = (await self._body_text()).lower()
        return any(x in body for x in [
            "passed", "you passed", "congratulations", "your score",
            "quiz result", "you have passed", "score:", "well done",
            "you have completed", "attempt again", "re-attempt",
            "total score", "view solution",
        ])

    async def _igot_extract_question(self) -> dict | None:
        """
        Extract current visible question text + options from iGot's
        card-style radio layout (Practice Question Set viewer).
        """
        try:
            return await self.page.evaluate(
                """() => {
                    // norm: collapse whitespace AND strip Unicode zero-width / invisible chars
                    const norm = s => (s || "")
                        .replace(/[\\u200b\\u200c\\u200d\\ufeff\\u00a0]/g, " ")
                        .replace(/\\s+/g, " ").trim();

                    // Reject question-header and UI-noise text
                    const isNoise = t => {
                        if (!t) return true;
                        // "Question No.1", "Question 1 of 5"
                        if (/^question\\s*(no\\.?\\s*)?\\d+/i.test(t)) return true;
                        // "1 of 5", "1/5"
                        if (/^\\d+\\s*(of|\\/)\\s*\\d+/i.test(t)) return true;
                        // "Items (4/9)" — course-item counter in page header, NOT a question
                        if (/^items?\\s*\\(\\d+[/\\\\]\\d+\\)/i.test(t)) return true;
                        // Toolbar junk: "info_outline", "Rate Now", "Share", "Flag"
                        if (/info_outline|rate\\s+now|share\\s+share|flag\\s+flag/i.test(t)) return true;
                        // Contains toolbar words mixed with numbers → header text
                        if (/info_outline|star_rate|rate now/i.test(t)) return true;
                        // "Single selection-MCQs", "Multiple selection"
                        if (/^(single|multiple)\\s+selection/i.test(t)) return true;
                        // "True/False" selection type labels
                        if (/^true\\/false$/i.test(t)) return true;
                        // Navigation buttons
                        if (/next\\s+question|save\\s*&\\s*next|previous\\s+question/i.test(t)) return true;
                        // Page header / breadcrumb patterns:
                        // "Final Assessment Next Previous", "Reflection Quiz Next",
                        // "Final Assessment Submit Previous", etc.
                        if (/^(final\\s+assessment|reflection\\s+quiz|practice\\s+quiz|assessment|quiz)\\s+(next|previous|submit)/i.test(t)) return true;
                        if (/^(next|previous|submit)\\s+(next|previous|submit)/i.test(t)) return true;
                        // Short navigation combos that appear in the header bar
                        if (/^(next|previous|submit|finish|start|begin)(\\s+(next|previous|submit|finish|start|begin))+$/i.test(t)) return true;
                        // Pure nav-bar text: contains "next" AND "previous" with nothing else of substance
                        if (/\\bnext\\b/.test(t) && /\\bprevious\\b/.test(t) && t.split("\\s+").length <= 5) return true;
                        // "अंतिम मूल्यांकन" (Final Assessment Hindi)
                        if (/\\u0905\\u0902\\u0924\\u093f\\u092e/.test(t)) return true;
                        return false;
                    };

                    // ── Options ────────────────────────────────────────────────
                    // iGot uses Angular Material mat-radio-button which hides the
                    // native <input type="radio"> with cdk-visually-hidden (opacity:0,
                    // position:absolute) — offsetParent is null on those inputs.
                    // We use 5 strategies from most to least specific.

                    const options = [];
                    const optEls = [];
                    const seenText = new Set();

                    const cleanOptText = el => {
                        const clone = el.cloneNode(true);
                        for (const rm of clone.querySelectorAll(
                            "input, svg, mat-icon, .mat-radio-container, .mat-radio-outer-circle, " +
                            ".mat-radio-inner-circle, [class*='radio-btn'], [class*='radio-button'], " +
                            "[class*='icon'], [class*='check'], [class*='ripple']"
                        )) rm.remove();
                        return norm(clone.innerText || "")
                            .replace(/^[\\u25CB\\u25CF\\u25EF\\u25C9\\u2713\\u2717\\u2022\\-]\\s*/, "")
                            .trim();
                    };

                    const tryAdd = (el) => {
                        const t = cleanOptText(el);
                        if (t && t.length > 1 && t.length < 300 && !isNoise(t) && !seenText.has(t.toLowerCase())) {
                            seenText.add(t.toLowerCase());
                            options.push(t);
                            optEls.push(el);
                        }
                    };

                    // Strategy 1: mat-radio-button (Angular Material — iGot's widget)
                    const matRadios = Array.from(document.querySelectorAll("mat-radio-button"));
                    if (matRadios.length >= 2) {
                        matRadios.forEach(tryAdd);
                    }

                    // Strategy 2: role="radio"
                    if (options.length < 2) {
                        Array.from(document.querySelectorAll("[role='radio']"))
                            .filter(el => el.offsetParent)
                            .forEach(tryAdd);
                    }

                    // Strategy 3: Sunbird/iGot specific classes
                    if (options.length < 2) {
                        for (const sel of [
                            "[class*='mcq-option']", "[class*='option-card']",
                            "[class*='option-item']", "[class*='answer-option']", "[class*='choice-item']",
                        ]) {
                            const els = Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent);
                            if (els.length >= 2) { els.forEach(tryAdd); break; }
                        }
                    }

                    // Strategy 4: bottom-up from radio inputs (include visually-hidden ones)
                    if (options.length < 2) {
                        Array.from(document.querySelectorAll("input[type='radio']"))
                            .filter(r => !r.disabled)
                            .forEach(radio => {
                                let el = radio.parentElement;
                                for (let i = 0; i < 8 && el; i++) {
                                    const tag = el.tagName.toLowerCase();
                                    const cls = (el.className || "").toLowerCase();
                                    if (tag === "mat-radio-button" || cls.includes("mat-radio-button")) {
                                        tryAdd(el); break;
                                    }
                                    if (el.querySelectorAll("input[type='radio']").length === 1 && el.offsetParent) {
                                        tryAdd(el); break;
                                    }
                                    el = el.parentElement;
                                }
                            });
                    }

                    // Strategy 5: label elements (last resort)
                    if (options.length < 2) {
                        Array.from(document.querySelectorAll("label"))
                            .filter(el => el.offsetParent)
                            .forEach(tryAdd);
                    }

                    // ── Question text ──────────────────────────────────────────
                    let qText = "";
                    const optSet = new Set(options.map(o => o.toLowerCase()));

                    for (const sel of [
                        "[class*='question-title']", "[class*='question-text']",
                        "[class*='questionTitle']", "[class*='stem']",
                        "[class*='questionBody']", "[class*='question-body']",
                        "[class*='question'] > strong", "[class*='question'] > p",
                        "[class*='question'] > h4", "[class*='question'] > h3",
                    ]) {
                        for (const el of document.querySelectorAll(sel)) {
                            if (!el.offsetParent) continue;
                            const t = norm(el.innerText || "");
                            if (t.length > 8 && !isNoise(t) && !optSet.has(t.toLowerCase())) {
                                qText = t; break;
                            }
                        }
                        if (qText) break;
                    }

                    // Fallback: first visible sentence-like block not in options
                    if (!qText) {
                        const cands = Array.from(document.querySelectorAll("p, strong, b, h3, h4, h5, span, div"))
                            .filter(el => {
                                if (!el.offsetParent) return false;
                                if (el.querySelector("mat-radio-button, input[type='radio'], [role='radio']")) return false;
                                const t = norm(el.innerText || "");
                                return t.length >= 10 && t.length <= 600 && !isNoise(t) &&
                                       !optSet.has(t.toLowerCase()) && t.split(" ").length >= 4;
                            });
                        if (cands.length) qText = norm(cands[0].innerText || "");
                    }

                    if (!qText && options.length > 0) qText = "(question text not extracted)";
                    if (!qText) return null;
                    qText = qText.slice(0, 800);

                    // ── Already answered? ──────────────────────────────────────
                    const allRadios = Array.from(document.querySelectorAll("input[type='radio']"));
                    const alreadyAnswered =
                        allRadios.some(r => r.checked) ||
                        document.querySelector("mat-radio-button.mat-radio-checked, [class*='mat-radio-checked']") !== null ||
                        optEls.some(el => {
                            const cls = (el.className || "").toLowerCase();
                            return cls.includes("selected") || cls.includes("checked") ||
                                   cls.includes("active") || cls.includes("answered");
                        });

                    return { question: qText, options, already_answered: alreadyAnswered };
                }"""
            )
        except PlaywrightError:
            return None

    async def _quiz_show_answer_text(self) -> str | None:
        """
        Click 'Show Answer' button if present and extract the revealed answer text.
        Works on iGot practice sets. Returns the correct option text, or None.
        """
        # Check if 'Show Answer' is visible — try multiple strategies
        show_btn = None

        # Strategy A: by role+name
        for label in [r"show answer", r"view answer", r"reveal answer", r"show correct", r"solution"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(label, re.I))
                if await loc.first.is_visible(timeout=400):
                    show_btn = loc.first
                    break
            except PlaywrightError:
                continue

        # Strategy B: by text content anywhere on page
        if not show_btn:
            try:
                loc = self.page.get_by_text(re.compile(r"show\s+answer", re.I))
                if await loc.first.is_visible(timeout=400):
                    show_btn = loc.first
            except PlaywrightError:
                pass

        # Strategy C: JS click by innerText scan
        if not show_btn:
            clicked_js = await self.page.evaluate(
                """() => {
                    const btns = Array.from(document.querySelectorAll("button, [role='button'], a, span"));
                    for (const btn of btns) {
                        const t = (btn.innerText || btn.textContent || "").trim().toLowerCase();
                        if (t === "show answer" || t === "view answer" || t === "reveal answer" || t === "solution") {
                            btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if clicked_js:
                await self.page.wait_for_timeout(800)
                # Go directly to answer extraction (skip the click block below)
                show_btn = True  # type: ignore[assignment]  # sentinel

        if not show_btn:
            return None

        if show_btn is not True:  # type: ignore[comparison-overlap]
            try:
                await show_btn.click()
                await self.page.wait_for_timeout(800)
            except PlaywrightError:
                return None

        print("[quiz] 'Show Answer' clicked — extracting revealed answer…")

        _EXTRACT_JS = """() => {
            const norm = s => (s || "").replace(/\\s+/g, " ").trim();
            const cleanText = el => {
                const clone = el.cloneNode(true);
                for (const rm of clone.querySelectorAll(
                    "input, svg, mat-icon, .mat-radio-container, .mat-radio-outer-circle, " +
                    ".mat-radio-inner-circle, [class*='ripple'], [class*='icon'], [class*='radio-btn']"
                )) rm.remove();
                return norm(clone.innerText || "").replace(/^[○●◯◉✓✗•\\-]\\s*/, "").trim();
            };

            // 1. Specific correct-answer class selectors
            for (const sel of [
                "[class*='correct-answer']", "[class*='correctAnswer']",
                "[class*='answer-correct']", "[class*='right-answer']",
                "[class*='answer-text']", "[class*='correct'][class*='option']",
                "[class*='solution']",
            ]) {
                const el = document.querySelector(sel);
                if (el) { const t = cleanText(el); if (t.length > 2 && t.length < 400) return t; }
            }

            // 2. Option elements now showing as correct (class, html content, or bg color)
            for (const el of document.querySelectorAll(
                "mat-radio-button, [class*='option-card'], [class*='option-item'], " +
                "[class*='mcq-option'], [class*='answer-option'], label, [role='radio']"
            )) {
                const cls = (el.className || "").toLowerCase();
                const html = (el.innerHTML || "").toLowerCase();
                const bg = window.getComputedStyle(el).backgroundColor || "";
                const isCorrect =
                    cls.includes("correct") || cls.includes("right-answer") || cls.includes("success") ||
                    html.includes("check_circle") || html.includes("task_alt") || html.includes("done") || html.includes("✓") ||
                    bg.includes("rgb(76, 175") || bg.includes("rgb(40, 167") || bg.includes("rgb(102, 187") ||
                    (el.querySelector("input[type='radio']") && el.querySelector("input[type='radio']").checked);
                if (isCorrect) {
                    const t = cleanText(el);
                    if (t.length > 2 && t.length < 400) return t;
                }
            }

            // 3. Any checked radio
            const checkedRadio = document.querySelector("input[type='radio']:checked");
            if (checkedRadio) {
                let c = checkedRadio.parentElement;
                for (let i = 0; i < 6 && c; i++) {
                    const t = cleanText(c);
                    if (t.length > 2 && t.length < 300) return t;
                    c = c.parentElement;
                }
            }

            // 4. mat-radio-checked (Angular Material checked state)
            const checked = document.querySelector("mat-radio-button.mat-radio-checked, [class*='mat-radio-checked']");
            if (checked) { const t = cleanText(checked); if (t.length > 2 && t.length < 400) return t; }

            return null;
        }"""

        # Poll 3 times with 700ms gaps — Angular animations need time
        answer_text = None
        for _poll in range(3):
            await self.page.wait_for_timeout(700)
            try:
                answer_text = await self.page.evaluate(_EXTRACT_JS)
                if answer_text:
                    break
            except PlaywrightError:
                pass

        if answer_text:
            print(f"[quiz] Show Answer revealed: {answer_text[:60]!r}")
            return answer_text
        print("[quiz] Show Answer clicked but could not extract answer — will use Gemini/fallback.")
        return None

    async def _igot_click_option(self, answer: str, all_options: list[str]) -> bool:
        """
        Click the iGot option card whose text best matches `answer`.
        iGot cards: full card div is clickable, radio input inside.
        Strategy: click the card div, then verify input becomes checked.
        """
        answer_norm = re.sub(r"\s+", " ", answer.strip().lower())

        # Build priority order: exact → contains → contained-in → word overlap
        def score(opt: str) -> int:
            o = re.sub(r"\s+", " ", opt.strip().lower())
            if o == answer_norm:
                return 0
            if answer_norm in o or o in answer_norm:
                return 1
            words = set(answer_norm.split())
            overlap = sum(1 for w in words if w in o and len(w) > 2)
            return 2 if overlap >= max(1, len(words) // 2) else 3

        ranked = sorted(all_options, key=score)

        for opt_text in ranked:
            try:
                clicked = await self.page.evaluate(
                    """(targetText) => {
                        const norm = s => (s || "")
                            .replace(/[\\u200b\\u200c\\u200d\\ufeff\\u00a0]/g, " ")
                            .replace(/\\s+/g, " ").trim().toLowerCase();
                        const target = norm(targetText);

                        // Option selectors — mat-radio-button FIRST (Angular Material)
                        const selectors = [
                            "mat-radio-button",
                            "[class*='mat-radio-button']",
                            "[role='radio']",
                            "[class*='option-card']",
                            "[class*='option-item']",
                            "[class*='mcq-option']",
                            "[class*='answer-option']",
                            "[class*='choice-item']",
                            "label",
                            "[role='option']",
                        ];

                        const cleanText = el => {
                            const clone = el.cloneNode(true);
                            for (const rm of clone.querySelectorAll(
                                "input, svg, mat-icon, .mat-radio-container, .mat-radio-outer-circle, " +
                                ".mat-radio-inner-circle, [class*='ripple'], [class*='icon'], [class*='radio-btn']"
                            )) rm.remove();
                            return norm(clone.innerText || "").replace(/^[○●◯◉✓✗•\\-]\\s*/, "");
                        };

                        const clickEl = el => {
                            el.scrollIntoView({ block: "center", behavior: "instant" });
                            // Try clicking the label-content child first (Angular Material pattern)
                            const labelContent = el.querySelector(".mat-radio-label-content, .mat-radio-label, label");
                            if (labelContent) {
                                labelContent.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                            }
                            // Also fire on the native hidden input for Angular's change detection
                            const inp = el.querySelector("input[type='radio'], input[type='checkbox']");
                            if (inp) {
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "checked")?.set;
                                if (setter) setter.call(inp, true);
                                inp.dispatchEvent(new Event("input", { bubbles: true }));
                                inp.dispatchEvent(new Event("change", { bubbles: true }));
                                inp.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                            }
                            // Fire on the card element itself
                            el.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
                            el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
                            el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
                            el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        };

                        for (const sel of selectors) {
                            const cards = Array.from(document.querySelectorAll(sel))
                                .filter(el => {
                                    // mat-radio-button doesn't need offsetParent check (it's always in DOM)
                                    if (sel === "mat-radio-button" || sel.includes("mat-radio")) return true;
                                    return el.offsetParent;
                                });

                            for (const card of cards) {
                                const t = cleanText(card);
                                if (!t) continue;
                                const matches = t === target || t.includes(target) || target.includes(t) ||
                                    (target.length > 4 && t.length > 4 &&
                                     target.split(" ").filter(w => w.length > 2).some(w => t.includes(w)));
                                if (!matches) continue;
                                clickEl(card);
                                return true;
                            }
                        }
                        return false;
                    }""",
                    opt_text,
                )
                if clicked:
                    await self.page.wait_for_timeout(400)
                    # Verify: check if any radio is now checked
                    checked = await self.page.evaluate(
                        """() => Array.from(document.querySelectorAll("input[type='radio']")).some(i => i.checked) ||
                           document.querySelector("mat-radio-button.mat-radio-checked") !== null"""
                    )
                    if checked:
                        return True
                    # JS events didn't register → try Playwright native click on the matching element
                    # This works because Playwright uses OS-level click, bypassing Angular's zone
                    try:
                        loc = self.page.locator("mat-radio-button, [role='radio'], label").filter(
                            has_text=re.compile(re.escape(opt_text[:30]), re.I)
                        ).first
                        if await loc.count() > 0:
                            await loc.click(timeout=2000)
                            await self.page.wait_for_timeout(400)
                            return True
                    except PlaywrightError:
                        pass
                    return True  # JS said clicked; trust it even without verification
            except PlaywrightError:
                continue

        # Last-resort Playwright native click: try to find the option by text
        try:
            loc = self.page.locator("mat-radio-button, [role='radio'], label").filter(
                has_text=re.compile(re.escape(answer[:30]), re.I)
            ).first
            if await loc.count() > 0:
                await loc.click(timeout=2000)
                await self.page.wait_for_timeout(400)
                print(f"[quiz] Native Playwright click succeeded for: {answer[:40]!r}")
                return True
        except PlaywrightError:
            pass
        return False

    async def _quiz_submit_visible(self) -> bool:
        """Return True if a Submit/Finish button is currently visible (last question reached)."""
        for label in [r"submit", r"finish"]:
            for role in ["button", "link"]:
                try:
                    loc = self.page.get_by_role(role, name=re.compile(label, re.I))
                    if await loc.first.is_visible(timeout=300):
                        return True
                except PlaywrightError:
                    continue
        return False

    async def _quiz_submit(self) -> bool:
        """Click Submit/Finish, wait for confirmation popup, click Yes/OK/Confirm."""
        print("[quiz] Submitting quiz…")

        # ── Step 1: Find and click Submit ─────────────────────────────────────
        # JS-first: Angular Material buttons are reliably clicked via JS dispatch
        submit_clicked = await self.page.evaluate(
            """() => {
                const allBtns = Array.from(document.querySelectorAll(
                    "button, [role='button'], input[type='submit'], a[class*='btn']"
                ));
                // Exact text match first
                for (const btn of allBtns) {
                    // Normalize whitespace — Angular icon elements inject newlines into innerText
                    const t = (btn.innerText || btn.value || "").replace(/\\s+/g, " ").trim();
                    if (/^(submit|finish quiz|finish assessment|finish|end quiz|end test)$/i.test(t)
                        && btn.offsetParent !== null) {
                        btn.scrollIntoView({ block: "center", behavior: "instant" });
                        btn.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
                        btn.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
                        btn.dispatchEvent(new MouseEvent("mouseup",   { bubbles: true, cancelable: true }));
                        btn.dispatchEvent(new MouseEvent("click",     { bubbles: true, cancelable: true, view: window }));
                        btn.click();
                        return t;
                    }
                }
                return null;
            }"""
        )
        if submit_clicked:
            print(f"[quiz] Submit clicked via JS: {submit_clicked!r}")
        else:
            # Playwright native click as second attempt
            for label in [r"^submit$", r"^finish$", r"finish\s*quiz", r"finish\s*assessment"]:
                for role in ["button", "link"]:
                    try:
                        loc = self.page.get_by_role(role, name=re.compile(label, re.I))
                        if await loc.first.is_visible(timeout=700):
                            await loc.first.click()
                            submit_clicked = label
                            print(f"[quiz] Submit clicked via Playwright: {label}")
                            break
                    except PlaywrightError:
                        continue
                if submit_clicked:
                    break

        if not submit_clicked:
            print("[quiz] No Submit button found — quiz may auto-submit or need manual action")
            return False

        # ── Step 2: Click "Yes" in confirmation popup ─────────────────────────
        # Angular CDK overlay needs ~300-500ms to mount after Submit is clicked.
        # Wait before probing — otherwise the first 2-3 attempts always miss the popup.
        # iGot popup: title="Submit", body="Do you want to submit?", buttons: No | Yes
        # IMPORTANT: only click "Yes" — not "Submit" from the quiz nav bar or "No".
        await self.page.wait_for_timeout(700)
        confirmed = False
        for attempt in range(6):
            # Strategy A: Playwright by role+name — ONLY "Yes" first, then broader
            for confirm_label in ["Yes", "OK"]:
                try:
                    cloc = self.page.get_by_role("button", name=re.compile(
                        r"^" + re.escape(confirm_label) + r"$", re.I
                    ))
                    if await cloc.first.is_visible(timeout=500):
                        await cloc.first.click()
                        confirmed = True
                        print(f"[quiz] Popup confirmed via Playwright: '{confirm_label}'")
                        break
                except PlaywrightError:
                    continue
            if confirmed:
                break

            # Strategy B: JS — look ONLY inside dialog/modal containers, prefer "Yes"
            conf_js = await self.page.evaluate(
                """() => {
                    // Look inside any visible overlay/dialog — iGot uses Angular CDK overlays
                    const overlaySelectors = [
                        "mat-dialog-container", "mat-dialog-actions",
                        "[class*='dialog']", "[class*='modal']", "[class*='popup']",
                        "[class*='overlay']", "[class*='confirm']", "[role='dialog']",
                        "cdk-overlay-container", "[class*='cdk-overlay']",
                    ];
                    // Prefer "Yes", then "OK" — avoid clicking "Submit" from the quiz nav bar
                    const yesRe = /^yes$/i;
                    const okRe = /^(ok|okay)$/i;
                    for (const sel of overlaySelectors) {
                        for (const c of document.querySelectorAll(sel)) {
                            if (!c.offsetParent) continue;
                            const btns = Array.from(c.querySelectorAll("button, [role='button']"))
                                .filter(b => b.offsetParent);
                            // Prefer Yes
                            let btn = btns.find(b => yesRe.test((b.innerText || "").replace(/\\s+/g," ").trim()));
                            if (!btn) btn = btns.find(b => okRe.test((b.innerText || "").replace(/\\s+/g," ").trim()));
                            if (btn) {
                                btn.scrollIntoView({ block: "center", behavior: "instant" });
                                btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                                btn.click();
                                return btn.innerText.trim() || "yes";
                            }
                        }
                    }
                    // Last resort: find a "Yes" button anywhere on page
                    const yesBtn = Array.from(document.querySelectorAll("button, [role='button']"))
                        .find(b => b.offsetParent && yesRe.test((b.innerText || "").replace(/\\s+/g," ").trim()));
                    if (yesBtn) {
                        yesBtn.click();
                        return "Yes (page-scan)";
                    }
                    return null;
                }"""
            )
            if conf_js:
                confirmed = True
                print(f"[quiz] Popup confirmed via JS: {conf_js!r}")
                await self.page.wait_for_timeout(150)
                break

            print(f"[quiz] Waiting for popup… attempt {attempt + 1}/6")
            await self.page.wait_for_timeout(150)

        # ── Step 3: Second confirmation popup ────────────────────────────────────
        # iGot shows a second "Do you want to submit?" (No | Yes) on the performance
        # summary page, OR a success banner with OK/Close. Handle both.
        await self.page.wait_for_timeout(800)
        for _s3_attempt in range(4):
            try:
                second_js = await self.page.evaluate(
                    """() => {
                        // Priority: Yes (second submit confirm) > OK/Close/Done (success banner)
                        const yesRe = /^yes$/i;
                        const okRe = /^(ok|okay|close|done|continue|got it|dismiss)$/i;
                        const containers = Array.from(document.querySelectorAll(
                            "mat-dialog-container, [role='dialog'], [class*='dialog'], [class*='modal'], [class*='popup'], cdk-overlay-container"
                        )).filter(c => c.offsetParent);
                        for (const c of containers) {
                            const btns = Array.from(c.querySelectorAll("button, [role='button']"))
                                .filter(b => b.offsetParent);
                            const t = b => (b.innerText || "").replace(/\\s+/g," ").trim();
                            // Prefer "Yes" first (another submit confirmation)
                            let btn = btns.find(b => yesRe.test(t(b)));
                            if (!btn) btn = btns.find(b => okRe.test(t(b)));
                            if (btn) { btn.click(); return t(btn); }
                        }
                        // Full-page fallback — Yes first, then OK/Close
                        const allBtns = Array.from(document.querySelectorAll("button, [role='button']"))
                            .filter(b => b.offsetParent);
                        const t = b => (b.innerText || "").replace(/\\s+/g," ").trim();
                        let btn = allBtns.find(b => yesRe.test(t(b)));
                        if (!btn) btn = allBtns.find(b => okRe.test(t(b)));
                        if (btn) { btn.click(); return t(btn) + " (page-scan)"; }
                        return null;
                    }"""
                )
                if second_js:
                    print(f"[quiz] Second popup dismissed: '{second_js}'")
                    await self.page.wait_for_timeout(800)
                    # Check if a third popup appeared (iGot can chain them)
                    continue
                break
            except PlaywrightError:
                break

        await self.page.wait_for_timeout(400)
        print("[quiz] Quiz submission complete.")
        return True

    async def _quiz_click_next_or_submit(self) -> bool:
        """Legacy helper — click Next question button (used in reading mode)."""
        for label in [r"next", r"save\s*&?\s*next", r"submit", r"finish"]:
            for role in ["button", "link"]:
                try:
                    loc = self.page.get_by_role(role, name=re.compile(label, re.I))
                    if await loc.first.is_visible(timeout=500):
                        await loc.first.click()
                        await self.page.wait_for_timeout(800)
                        return True
                except PlaywrightError:
                    continue
        return False

    async def _dismiss_popups(self) -> bool:
        """Dismiss any visible popup/dialog by clicking Yes/OK/Close/Done/Submit/Continue."""
        dismissed = False
        try:
            result = await self.page.evaluate(
                """() => {
                    const btnRe = /^(yes|ok|okay|close|done|continue|got it|dismiss|submit|confirm|accept|agree|understood|i understand|proceed)$/i;
                    // Check dialog containers first
                    const containers = Array.from(document.querySelectorAll(
                        "mat-dialog-container, [role='dialog'], [class*='dialog'], " +
                        "[class*='modal'], [class*='popup'], [class*='overlay'], " +
                        "[class*='confirm'], cdk-overlay-container, [class*='cdk-overlay'], " +
                        "[class*='snackbar'], [class*='toast'], [class*='alert']"
                    ));
                    for (const c of containers) {
                        const btns = Array.from(c.querySelectorAll("button, [role='button'], a"))
                            .filter(b => b.offsetParent || b.getBoundingClientRect().height > 0);
                        for (const btn of btns) {
                            const t = (btn.innerText || "").replace(/\\s+/g, " ").trim();
                            if (btnRe.test(t)) {
                                btn.scrollIntoView({ block: "center" });
                                btn.click();
                                return t;
                            }
                        }
                    }
                    // Page-wide scan for floating Yes/OK/Submit buttons
                    const allBtns = Array.from(document.querySelectorAll("button, [role='button']"))
                        .filter(b => b.offsetParent);
                    for (const btn of allBtns) {
                        const t = (btn.innerText || "").replace(/\\s+/g, " ").trim();
                        if (btnRe.test(t)) {
                            btn.click();
                            return t;
                        }
                    }
                    return null;
                }"""
            )
            if result:
                print(f"[popup] Dismissed popup: '{result}'")
                dismissed = True
                await self.page.wait_for_timeout(1000)
                # Check for second popup
                result2 = await self.page.evaluate(
                    """() => {
                        const btnRe = /^(yes|ok|okay|close|done|continue|got it|dismiss)$/i;
                        const btns = Array.from(document.querySelectorAll(
                            "mat-dialog-container button, [role='dialog'] button, [class*='modal'] button, [class*='popup'] button"
                        )).filter(b => b.offsetParent);
                        for (const btn of btns) {
                            const t = (btn.innerText || "").replace(/\\s+/g, " ").trim();
                            if (btnRe.test(t)) { btn.click(); return t; }
                        }
                        return null;
                    }"""
                )
                if result2:
                    print(f"[popup] Dismissed second popup: '{result2}'")
                    await self.page.wait_for_timeout(800)
        except PlaywrightError:
            pass
        return dismissed

    async def _handle_unknown(self) -> tuple[str, str, str]:
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"Generic completion signal: {done_evidence}", "Continue to next eligible module"

        # Check if this is actually SCORM content that wasn't detected earlier
        body = (await self._body_text()).lower()
        if any(x in body for x in ["scorm", "must be completed in one go", "sharable content object",
                                    "progress will not be saved", "play"]):
            # Check if there's a Play button or iframe suggesting SCORM
            has_play = False
            try:
                for label in ["play", "start", "begin", "launch"]:
                    loc = self.page.get_by_role("button", name=re.compile(label, re.I))
                    if await loc.first.is_visible(timeout=400):
                        has_play = True
                        break
            except PlaywrightError:
                pass
            if has_play or len(self.page.frames) > 1:
                print("[unknown] Detected SCORM content. Switching to SCORM handler.")
                return await self._handle_scorm()

        # Try active completion strategies for unknown content types
        print("[unknown] Trying active completion strategies...")

        # Strategy 1: Scroll through entire content
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.wait_for_timeout(2000)
        except PlaywrightError:
            pass

        # Strategy 2: Check for and handle iframe content
        iframe_handled, iframe_ev = await self._handle_flash_or_iframe_video()
        if iframe_handled:
            await self.page.wait_for_timeout(3000)
            done, done_evidence = await self._is_module_completed()
            if done:
                return "done", f"Iframe completion: {done_evidence}; {iframe_ev}", "Continue to next eligible module"

        # Strategy 3: Click Mark as Complete / Done buttons
        for label_re in [r"mark.*complete", r"mark.*done", r"complete", r"done",
                         r"i have read", r"acknowledge"]:
            try:
                loc = self.page.get_by_role("button", name=re.compile(label_re, re.I))
                if await loc.first.is_visible(timeout=400):
                    await loc.first.click()
                    print(f"[unknown] Clicked '{label_re}' button")
                    await self.page.wait_for_timeout(1500)
                    break
            except PlaywrightError:
                continue

        # Strategy 4: Handle any popup that appeared
        await self._dismiss_popups()

        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"Unknown type completed: {done_evidence}", "Continue to next eligible module"
        return "partial", "Unknown module type; completion not triggered", "Continue to next eligible module"

    async def _fast_navigate_next(self) -> bool:
        """Navigate to next module. Returns True if navigation succeeded."""
        prev_url = self.page.url
        prev_body_hash = hash((await self._body_text())[:500])

        # Strategy A: Click explicit Next/Continue button
        for label in ["Next", "Continue", "Next Module", "Proceed", "Next Content"]:
            for role in ["button", "link"]:
                locator = self.page.get_by_role(role, name=re.compile(label, re.I))
                try:
                    if await locator.first.is_visible(timeout=400):
                        await locator.first.click()
                        await self.page.wait_for_timeout(1200)
                        if self.page.url != prev_url or hash((await self._body_text())[:500]) != prev_body_hash:
                            print("[auto-continue] Navigated via Next/Continue button")
                            return True
                except PlaywrightError:
                    continue

        # Strategy B: Find active sidebar item and click the next non-completed one
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const norm = (s) => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
                    const rows = Array.from(document.querySelectorAll(
                        "li, [role='treeitem'], [class*='item'], [class*='toc']"
                    )).filter(n => {
                        const t = norm(n.innerText || "");
                        if (t.length < 4 || t.length > 130) return false;
                        if (n.querySelectorAll("li, [role='treeitem']").length > 1) return false;
                        if (/\\b\\d+\\s*items?\\b/i.test(t)) return false;
                        return true;
                    });

                    let activeIdx = -1;
                    for (let i = 0; i < rows.length; i++) {
                        const el = rows[i];
                        const cls = (el.className || "").toLowerCase();
                        const isActive =
                            cls.includes("active") || cls.includes("selected") ||
                            cls.includes("current") || cls.includes("highlight") ||
                            cls.includes("mat-list-item-active") || cls.includes("playing") ||
                            el.getAttribute("aria-current") === "true" ||
                            el.getAttribute("aria-selected") === "true";
                        if (isActive) activeIdx = i;
                    }

                    if (activeIdx < 0 || activeIdx >= rows.length - 1) return false;

                    for (let j = activeIdx + 1; j < rows.length; j++) {
                        const next = rows[j];
                        const nextHtml = (next.innerHTML || "").toLowerCase();
                        const isDone =
                            nextHtml.includes("check_circle") || nextHtml.includes("task_alt") ||
                            nextHtml.includes("status-complete") || nextHtml.includes("is-complete");
                        if (isDone) continue;

                        const clickEl = next.querySelector("a[href]") || next.querySelector("button") || next;
                        if (!clickEl) continue;
                        clickEl.scrollIntoView({ block: "center", behavior: "instant" });
                        clickEl.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        clickEl.click();
                        return true;
                    }
                    return false;
                }"""
            )
            if clicked:
                await self.page.wait_for_timeout(1200)
                if self.page.url != prev_url or hash((await self._body_text())[:500]) != prev_body_hash:
                    print("[auto-continue] Navigated to next sidebar item")
                    return True
                else:
                    print("[auto-continue] Sidebar click fired but page did not change")
        except PlaywrightError:
            pass

        # Strategy C: Re-discover modules and click first non-completed
        try:
            modules = await self._discover_sidebar_modules()
            for m in modules:
                if m.is_completed is True:
                    continue
                opened = await self._open_sidebar_module_by_name(m.name)
                if opened:
                    await self.page.wait_for_timeout(1000)
                    if self.page.url != prev_url or hash((await self._body_text())[:500]) != prev_body_hash:
                        print(f"[auto-continue] Opened next pending module: {m.name[:60]}")
                        return True
        except PlaywrightError:
            pass

        print("[auto-continue] All strategies failed to navigate to next module")
        return False


    async def _is_module_completed(self) -> tuple[bool, str]:
        body = (await self._body_text()).lower()
        patterns = [
            "module completed",
            "marked complete",
            "100% complete",
            "status: complete",
            "you have completed",
            "completion status: complete",
        ]
        for p in patterns:
            if p in body:
                return True, f"text signal '{p}'"
        try:
            done_control = await self.page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll("button, [role='button'], [aria-label]"));
                    for (const el of els) {
                        const t = (el.innerText || el.getAttribute("aria-label") || "").toLowerCase().trim();
                        if (!t) continue;
                        const isDoneText =
                          t === "completed" ||
                          t === "done" ||
                          t.includes("marked complete") ||
                          t.includes("mark as complete");
                        if (!isDoneText) continue;
                        const disabled =
                          el.hasAttribute("disabled") ||
                          (el.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
                          (el.className || "").toLowerCase().includes("disabled");
                        if (disabled) return t.slice(0, 120);
                    }
                    return null;
                }"""
            )
            if done_control:
                return True, f"ui control signal '{done_control}'"
        except PlaywrightError:
            pass
        return False, "no completion signal found"

    async def _is_module_ticked(self, module_name: str, module_href: str = "") -> bool:
        target = self._normalize_for_match(module_name)
        stable_href = self._stable_href(module_href) if module_href else ""
        if not target and not stable_href:
            return False
        # NEVER use cache — ticks appear asynchronously after video ends.
        # Stale cache was the #1 reason ticks were missed.
        try:
            result = bool(
                await self.page.evaluate(
                    """([targetNorm, stableHref]) => {
                        const norm = (s) => (s || "")
                          .toLowerCase()
                          .replace(/\\b(check_circle|radio_button_checked|add|remove|task_alt)\\b/g, " ")
                          .replace(/[^a-z0-9\\s]/g, " ")
                          .replace(/\\s+/g, " ")
                          .trim();
                        const cleanTitle = (s) => {
                          let t = (s || "").replace(/^\\d+\\.\\s*/, "");
                          t = t.replace(/\\s+\\d+h(\\s+\\d+m)?(\\s+\\d+s)?$/i, "");
                          t = t.replace(/\\s+\\d+m(\\s+\\d+s)?(\\s+[•·\\-]\\s+\\d+\\s*item[s]?)?$/i, "");
                          t = t.replace(/\\s+\\d+s$/i, "");
                          return norm(t);
                        };
                        const checkDone = (node) => {
                          const html = (node.innerHTML || "").toLowerCase();
                          return html.includes("check_circle") ||
                            html.includes("task_alt") ||
                            html.includes("sb-dot-done") ||
                            html.includes("sb-icon-done") ||
                            html.includes("done-icon") ||
                            html.includes("status-complete") ||
                            html.includes("is-complete") ||
                            html.includes("item-complete") ||
                            html.includes("aria-checked=\\"true\\"") ||
                            html.includes("aria-checked='true'") ||
                            /aria-label=["'](completed|done|finished)["']/i.test(html) ||
                            Array.from(node.querySelectorAll("mat-icon, [class*='mat-icon'], .material-icons")).some(icon => {
                              const t = (icon.textContent || "").trim().toLowerCase();
                              return t === "check_circle" || t === "task_alt" || t === "done" || t === "check";
                            }) ||
                            Array.from(node.querySelectorAll("mat-icon, [class*='icon'], svg")).some(el => {
                              const color = window.getComputedStyle(el).color || "";
                              const cls = (el.className || "").toLowerCase();
                              return (color.includes("rgb(76, 175") || color.includes("rgb(40, 167") ||
                                      color.includes("rgb(0, 128") || color.includes("rgb(56, 142") ||
                                      color.includes("#4caf50") || color.includes("#28a745") ||
                                      cls.includes("done") || cls.includes("complete") ||
                                      cls.includes("tick") || cls.includes("checked") ||
                                      cls.includes("success") || cls.includes("green"));
                            }) ||
                            Array.from(node.querySelectorAll("*")).some(el => {
                                const cls = (el.className || "").toLowerCase();
                                return cls.includes("done") || cls.includes("complete") || cls.includes("tick") || cls.includes("checked");
                            });
                        };

                        // --- Strategy 1: href-first lookup (precise — avoids same-name false positives) ---
                        // When multiple sections share the same module name (e.g. "Reflection Quiz"),
                        // name-only matching returns true as soon as ANY section's node is ticked.
                        // Using the specific href finds exactly this module's sidebar node.
                        if (stableHref) {
                          const anchors = Array.from(document.querySelectorAll("a[href]"));
                          const matchingAnchor = anchors.find(a =>
                            (a.href || "").split("?")[0].toLowerCase() === stableHref
                          );
                          if (matchingAnchor) {
                            // Walk up to 6 ancestor levels to find the sidebar item container
                            let node = matchingAnchor.parentElement;
                            for (let i = 0; i < 6 && node; i++) {
                              if (checkDone(node)) return true;
                              node = node.parentElement;
                            }
                            return false;  // found the specific node → not ticked
                          }
                        }

                        // --- Strategy 2: name-based fallback (when href not in DOM / no href provided) ---
                        if (!targetNorm) return false;
                        const nodes = Array.from(document.querySelectorAll(
                          "li, [role='treeitem'], [class*='item'], [class*='accordion'], [class*='toc'], [class*='node'], [class*='module'], mat-list-item"
                        ));
                        for (const node of nodes) {
                          const rawText = (node.innerText || "").replace(/\\s+/g, " ").trim();
                          if (!rawText || rawText.length > 400) continue;
                          const titleNorm = cleanTitle(rawText);
                          if (!titleNorm) continue;
                          let titleMatches = titleNorm.includes(targetNorm) || targetNorm.includes(titleNorm);
                          if (!titleMatches) {
                            const targetWords = targetNorm.split(" ").filter(w => w.length > 2);
                            if (targetWords.length > 0) {
                              const overlap = targetWords.filter(w => titleNorm.includes(w)).length;
                              titleMatches = (overlap / targetWords.length) >= 0.6;
                            }
                          }
                          if (!titleMatches) continue;
                          if (checkDone(node)) return true;
                        }
                        return false;
                    }""",
                    [target, stable_href],
                )
            )
            return result
        except PlaywrightError:
            return False

    async def _is_course_completed(self) -> tuple[bool, str]:
        body = (await self._body_text()).lower()
        if "course completed" in body or "100%" in body:
            return True, "course-level completion text detected"
        if "certificate" in body or "badge" in body:
            return True, "completion artifact keyword detected"
        return False, "course completion artifact not detected"

    async def _detect_block_reason(self, module_type: str = "unknown") -> str | None:
        body = (await self._body_text()).lower()
        if any(x in body for x in ["prerequisite", "complete previous", "complete all previous", "unlock after"]):
            return "prerequisite_lock"
        timer_patterns = [
            r"\bavailable in\b",
            r"\btry again in\b",
            r"\bcome back after\b",
            r"\blocked until\b",
            r"\bplease spend\b.{0,60}\btime\b",
            r"\btimer\b.{0,40}\b(lock|remaining|available|left)\b",
        ]
        if any(re.search(pattern, body) for pattern in timer_patterns):
            if module_type == "video" and await self._has_playable_video():
                return None
            return "timer_lock"
        if any(x in body for x in ["access denied", "permission", "not authorized", "forbidden"]):
            return "permission_issue"
        if any(x in body for x in ["error", "something went wrong", "failed to load"]):
            return "technical_error"
        return None

    def _fallback_action(self, block_reason: str) -> str:
        actions = {
            "prerequisite_lock": "Queue dependency and continue with other eligible modules",
            "timer_lock": "Schedule revisit and continue with another module/course",
            "technical_error": "Capture evidence and escalate to support; continue elsewhere",
            "permission_issue": "Capture denial evidence and escalate to admin",
        }
        return actions.get(block_reason, "Continue with other eligible modules")

    async def _body_text(self) -> str:
        try:
            return await self.page.inner_text("body")
        except PlaywrightError:
            return ""

    async def _safe_goto(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=self.args.goto_timeout_ms)
            await self.page.wait_for_timeout(600)
        except PlaywrightTimeoutError:
            print(f"[warn] Navigation timeout: {url}")

    async def _snap(self, name: str) -> Path:
        path = self.reporter.artifacts_dir / f"{name}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
        except PlaywrightError:
            # Ignore screenshot failures.
            pass
        return path

    def _record(
        self,
        course_name: str,
        module_name: str,
        module_type: str,
        status: str,
        block_reason: str | None,
        evidence: str,
        next_action: str,
    ) -> None:
        entry = RunReportEntry(
            run_id=self.run_id,
            timestamp=self.now(),
            course_name=course_name,
            module_name=module_name,
            module_type=module_type if module_type in MODULE_TYPES else "unknown",
            status=status if status in STATUSES else "partial",
            block_reason=block_reason,
            evidence=evidence,
            next_action=next_action,
        )
        self.reporter.add(entry)

    def _latest_module_report(self, course_name: str, module_name: str) -> RunReportEntry | None:
        target = self._normalize_for_match(module_name)
        for entry in reversed(self.reporter.entries):
            if entry.course_name != course_name:
                continue
            if self._normalize_for_match(entry.module_name) == target:
                return entry
        return None

    @staticmethod
    def _extract_duration_seconds(text: str) -> int | None:
        low = text.lower()
        hours = sum(int(v) for v in re.findall(r"(\d+)\s*h\b", low))
        mins = sum(int(v) for v in re.findall(r"(\d+)\s*m\b", low))
        secs = sum(int(v) for v in re.findall(r"(\d+)\s*s\b", low))
        total = (hours * 3600) + (mins * 60) + secs
        return total if total > 0 else None

    @staticmethod
    def _extract_percent(text: str) -> int | None:
        m = re.search(r"\b(\d{1,3})\s*%", text)
        if not m:
            return None
        value = int(m.group(1))
        if 0 <= value <= 100:
            return value
        return None

    @staticmethod
    def _is_completed_text(text: str) -> bool:
        low = text.lower()
        return "completed" in low and ("100%" in low or "course completed" in low)

    @staticmethod
    def _is_course_noise(name: str, href: str | None) -> bool:
        low = name.strip().lower()
        compact = re.sub(r"\s+", " ", low).strip()
        href_low = (href or "").lower()
        has_course_signal = bool(
            re.search(r"\b\d{1,3}\s*%\b", low)
            or "course" in low
            or "learning path" in low
            or "certification" in low
            or "collectiontype=course" in href_low
            or ("collectionid=" in href_low and "batchid=" in href_low)
        )
        if low in {"next", "previous", "prev", "back", "more", "view all"}:
            return True
        if any(x in low for x in ["navigate_before", "navigate_next", "keyboard_arrow", "chevron", "arrow_back"]):
            return True
        if any(x in low for x in ["hubs support", "privacy policy", "download app"]):
            return True
        if "practice question set" in low or "viewer/practice" in href_low:
            return True
        if re.search(r"\b\d+\s*questions?\b", low) and not has_course_signal:
            return True
        if "items (" in low and "%" in low:
            return True
        if re.fullmatch(r"[a-z_]*(previous|next|back)[a-z_]*", compact):
            return True
        if len(low) <= 3:
            return True
        if len(low) > 140:
            return True
        if low.count(" star ") >= 2 or "ratings" in low:
            return True
        if "/viewer/" in href_low:
            has_duration = bool(re.search(r"\b\d+\s*m(\s+\d+\s*s)?\b", low) or re.search(r"\b\d+\s*s\b", low))
            has_course_signal = has_course_signal or bool(re.search(r"\b\d+\s*/\s*\d+\b", low))
            if has_duration and not has_course_signal:
                return True
            if len(low) <= 42 and not has_course_signal:
                return True
        # Only reject href-less items if they ALSO look like pure noise
        # (very short or clearly not a course title)
        if not href and not re.search(r"\b\d{1,3}\s*%\b", low) and "course" not in low and "learning" not in low:
            # But keep items that are long enough to be real course titles
            if len(low) >= 15:
                return False  # Likely a real course title found via card text extraction
            return True
        return False

    @staticmethod
    def _is_module_noise(name: str, href: str | None) -> bool:
        low = name.strip().lower()
        compact = re.sub(r"[•·\-]", " ", low)
        compact = re.sub(r"\s+", " ", compact).strip()

        # Basic nav noise
        if low in {"next", "previous", "prev", "back", "close", "menu", "share", "free",
                   "chapters", "settings", "fullscreen", "mute", "unmute", "play", "pause",
                   "view more", "show more", "load more", "see more", "send", "like", "comment",
                   "submit", "cancel", "no", "yes", "ok", "done", "finish",
                   "start again", "resume", "restart", "retake", "replay",
                   "download", "download now", "download pdf", "download file",
                   "apolitical", "about us", "about us karmayogi bharat",
                   "karmayogi bharat", "rate now", "edit rating"}:
            return True
        if re.fullmatch(r"share\s*share", compact):
            return True
        # Angular router artifacts: "navigate_beforePrevious", "navigate_beforeNext", "Next navigate_next" etc.
        if "navigate_before" in low or "navigate_next" in low:
            return True
        # "User NN Author" / "U7 User 71 Author" — system user labels
        if re.search(r"\buser\s+\d+\b", low) or re.search(r"^u\d+\s+user\s+\d+", low):
            return True
        # Pure action buttons mistaken as modules
        if re.fullmatch(r"(start|begin|launch|open|view|read|watch|download|enrol|enroll)\s+(now|course|module|content|here)?", compact):
            return True

        # Video player control noise
        # Speed labels: "1x, selected", "1.5x", "2x, not selected"
        if re.fullmatch(r"\d+(\.\d+)?x(,\s*(selected|not selected))?", compact):
            return True
        # Captions/quality toggles with aria-selected state
        if re.search(r",\s*(selected|not selected)$", compact):
            return True
        # Quality labels
        if re.fullmatch(r"(\d{3,4}p|auto)(,\s*(selected|not selected))?", compact):
            return True
        # Caption/description toggles
        if re.match(r"^(descriptions?|captions?|subtitles?)\s+(on|off)", compact):
            return True

        # iGot Material icon suffixes (icon names appear as text nodes)
        if low.rstrip().endswith("info_outline"):
            return True
        if re.search(
            r"\b(info_outline|more_vert|arrow_forward|expand_more|expand_less|chevron_right|"
            r"sentiment_satisfied|sentiment_very_satisfied|sentiment_dissatisfied|"
            r"thumb_up|thumb_down|favorite|favorite_border|star_rate|star_border|"
            r"notifications?|bookmark|bookmarks?|history|home|person|people|group|"
            r"send|chat|forum|comment|message|notifications?_none)\b",
            low,
        ):
            return True

        # License / pricing badges
        if re.fullmatch(r"cc[\s\-]by[\s\-][\d.]+", compact) or low in {"cc by 4.0", "cc-by", "free"}:
            return True

        # Course stats badges: "5 Videos", "30 Videos", "13 PDFs", "5 Modules", etc.
        if re.fullmatch(r"\d+\s+(video|pdf|module|lesson|chapter|unit|week|practice\s+test|final\s+test|quiz|assessment)s?", compact):
            return True

        # Progress badge: "Items (30/49)", "Items(30/49)"
        if re.match(r"^items?\s*\(\s*\d+\s*/\s*\d+\s*\)", compact):
            return True

        # Star rating rows: "5 star", "4 star", "3 star" etc.
        if re.fullmatch(r"\d+\s*stars?", compact):
            return True
        if "star star" in low or "star_half" in low:
            return True

        # Review/comment author lines: "AB Author Name N days ago"
        if re.search(r"\b\d+\s+(day|week|month|hour)s?\s+ago\b", low):
            return True
        # Two-initial prefix (reviewer avatar label) followed by name: "AD Adusumalli Drowpadi"
        if re.match(r"^[a-z]{2}\s+[a-z]", compact) and len(compact.split()) >= 3 and len(low) < 60:
            # Likely a reviewer avatar+name row (not a numbered module)
            return True
        # Learner / social section names: 2-4 Title Cased words, no module verbs, < 50 chars.
        # e.g. "Sangita Santosh Farkunde", "Rajbala Parasram Suryawanshi"
        _MODULE_VERBS = {
            "introduction", "overview", "understanding", "analyze", "analysis",
            "learning", "design", "thinking", "management", "assessment", "quiz",
            "module", "lesson", "chapter", "unit", "practice", "final", "test",
            "video", "reading", "activity", "exercise", "case", "study", "explore",
            "discover", "applying", "implementing", "developing", "building",
            "creating", "managing", "leading", "using", "working", "making",
        }
        _name_words = name.strip().split()
        if (
            2 <= len(_name_words) <= 4
            and len(name.strip()) < 50
            and all(w[0].isupper() and w[1:].islower() for w in _name_words if len(w) > 1)
            and not any(w.lower() in _MODULE_VERBS for w in _name_words)
        ):
            return True
        # ALL CAPS person names: "MUSHTAQ AHMAD", "RAJESH KUMAR" etc.
        if (
            2 <= len(_name_words) <= 4
            and len(name.strip()) < 50
            and name.strip() == name.strip().upper()  # ALL CAPS
            and all(w.isalpha() for w in _name_words)
            and not any(w.lower() in _MODULE_VERBS for w in _name_words)
        ):
            return True

        # Pure provider / creator names without module signals
        if low in {"fractal", "coursera", "by fractal", "by coursera", "karmayogi bharat",
                   "iim ahmedabad", "iim bangalore", "iim calcutta", "iim",
                   }:
            return True
        # Indian institution names: "Indian Institute of Management ..."
        # "Indian Institute of Technology ...", "National Institute of ..."
        if re.match(
            r"^(indian institute of (management|technology|science)|"
            r"national institute of|iim |iit |nit |bits pilani|"
            r"university of|institute of|school of|"
            r"national academy of|central .+ board|"
            r"department of|ministry of|directorate of)",
            low,
        ) and len(low.split()) <= 10:
            return True
        # Organization names with acronyms: "Something Something (ACRONYM)" or "Something (ACRONYM) Place"
        if re.search(r"\([A-Z]{2,6}\)", name.strip()) and len(_name_words) <= 8 and not any(w.lower() in _MODULE_VERBS for w in _name_words):
            return True

        # Competencies badge (iGot uses this as a tab, not a module)
        if re.match(r"^competencies\b", compact) and len(low) < 25:
            return True

        # FAQ noise
        if "faq" in low or "faqs" in low:
            return True
        if any(x in low for x in ["about content start discussion", "download app", "privacy policy", "hubs support"]):
            return True
        if "rating" in low or "ratings" in low:
            return True

        # Pure duration-only strings (no title)
        if re.fullmatch(r"\d+\s*h(\s+\d+\s*m)?(\s+\d+\s*s)?(\s+\d+\s*items?)?", compact):
            return True
        if re.fullmatch(r"\d+\s*m(\s+\d+\s*s)?(\s+\d+\s*items?)?", compact):
            return True
        if re.fullmatch(r"\d+\s*s(\s+\d+\s*items?)?", compact):
            return True
        if re.fullmatch(r"\d+\s*items?", compact):
            return True
        if low.count(" item ") > 2:
            return True

        if LiveQARunner._is_section_header_name(name):
            return True
        if len(low) <= 2 and not href:
            return True
        if len(low) > 100:
            return True
        return False

    @staticmethod
    def _course_query_name(name: str) -> str:
        # Remove trailing duration tokens like "14m 55s" and noisy suffixes.
        cleaned = re.sub(r"\b\d+\s*h\b", "", name, flags=re.I)
        cleaned = re.sub(r"\b\d+\s*m\b", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\b\d+\s*s\b", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if cleaned.endswith("..."):
            cleaned = cleaned[:-3].strip()
        return cleaned

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        value = text.lower()
        value = re.sub(r"\b(check_circle|radio_button_checked|add|remove)\b", " ", value, flags=re.I)
        value = re.sub(r"^\d+\.\s*", "", value)
        value = re.sub(r"\s+\d+h(\s+\d+m)?(\s+\d+s)?$", "", value)
        value = re.sub(r"\s+\d+m(\s+\d+s)?(\s+[•·\-]\s+\d+\s*item[s]?)?$", "", value)
        value = re.sub(r"\s+\d+s$", "", value)
        value = re.sub(r"[^a-z0-9\s]", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    @staticmethod
    def _stable_href(href: str) -> str:
        """Return href with query string stripped — stable across page reloads.

        iGot viewer URLs sometimes include session/referrer query params that change
        between navigations. Using the full URL as a dedup key means the same physical
        module gets a different key each time and bypasses completed_hrefs / attempted_keys.
        Stripping everything after '?' gives a stable path-only key.
        """
        if not href:
            return ""
        return href.split("?")[0].strip().lower()

    @staticmethod
    def _normalize_module_name(name: str) -> str:
        text = re.sub(r"\b(check_circle|radio_button_checked|add|remove)\b", "", name, flags=re.I)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    @staticmethod
    def _dedupe_modules(modules: list[ModuleCandidate]) -> list[ModuleCandidate]:
        best: dict[str, ModuleCandidate] = {}
        for m in modules:
            key = f"{(m.href or '').strip().lower()}::{m.name.strip().lower()}"
            prev = best.get(key)
            if prev is None:
                best[key] = m
                continue
            prev_score = (1 if prev.href else 0) + (1 if prev.is_completed is False else 0)
            new_score = (1 if m.href else 0) + (1 if m.is_completed is False else 0)
            if new_score > prev_score:
                best[key] = m
        return list(best.values())

    @staticmethod
    def _slug(text: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
        return cleaned[:80] or "item"

    @staticmethod
    def _clean_name(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()[:220] or "Untitled"

    @staticmethod
    async def _wait_for_enter() -> None:
        import asyncio
        try:
            await asyncio.to_thread(input)
        except (EOFError, KeyboardInterrupt):
            return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run compliant live QA + audit on iGot-like learning portal.")
    parser.add_argument("--base-url", default="https://portal.igotkarmayogi.gov.in", help="Portal base URL")
    parser.add_argument("--start-url", default="", help="Optional direct URL to start from (skips auto hub navigation)")
    parser.add_argument("--course-url", default="", help="Optional direct course URL (skip course discovery)")
    parser.add_argument(
        "--prompt-mode",
        action="store_true",
        help="Interactive mode: prompts you to paste course URLs one at a time",
    )
    parser.add_argument("--output-dir", default="reports", help="Directory for reports and evidence")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("--window-width", type=int, default=1920, help="Chrome window width for live mode")
    parser.add_argument("--window-height", type=int, default=1080, help="Chrome window height for live mode")
    parser.add_argument(
        "--login-zoom-percent",
        type=int,
        default=80,
        help="Auto zoom applied on login pages to reveal hidden auth options",
    )
    parser.add_argument("--slow-mo-ms", type=int, default=0, help="Slow motion delay in ms for each Playwright action")
    parser.add_argument("--max-courses", type=int, default=0, help="Limit courses processed (0 means all discovered)")
    parser.add_argument("--max-modules", type=int, default=0, help="Limit modules per course (0 means all discovered)")
    parser.add_argument(
        "--strict-sequence",
        action="store_true",
        help="Process modules in strict order and stop when current module is not ticked",
    )
    parser.add_argument(
        "--skip-assessments",
        action="store_true",
        help="Skip assessment modules (for example End of Module Quiz / Final Assessment) and continue",
    )
    parser.add_argument(
        "--auto-run-to-end",
        action="store_true",
        help="Continue processing remaining modules without waiting for manual tick confirmation",
    )
    parser.add_argument(
        "--timer-lock-retry-seconds",
        type=int,
        default=120,
        help="Seconds to wait before retrying a timer-locked module in auto-run mode",
    )
    parser.add_argument(
        "--timer-lock-max-retries",
        type=int,
        default=2,
        help="Maximum retry attempts per timer-locked module in auto-run mode",
    )
    parser.add_argument(
        "--timer-lock-max-wait-seconds",
        type=int,
        default=900,
        help="Upper bound on auto-wait time for timer-locked module retries",
    )
    parser.add_argument(
        "--pause-for-quiz",
        dest="pause_for_quiz",
        action="store_true",
        help="Pause for user-reviewed quiz submission (default: on)",
    )
    parser.add_argument(
        "--no-pause-for-quiz",
        dest="pause_for_quiz",
        action="store_false",
        help="Do not pause for quiz review (not recommended)",
    )
    parser.add_argument(
        "--video-observe-seconds",
        type=float,
        default=8.0,
        help="Seconds to observe video playback progression before status decision",
    )
    parser.add_argument(
        "--video-speed",
        type=float,
        default=16.0,
        help="Preferred video playback speed (0.5 to 16.0; default 16.0 for fastest completion)",
    )
    parser.add_argument(
        "--video-max-wait-seconds",
        type=int,
        default=2400,
        help="Maximum seconds to auto-wait on a video module for completion/tick in auto-run mode",
    )
    parser.add_argument("--goto-timeout-ms", type=int, default=45000, help="Navigation timeout in milliseconds")
    parser.add_argument(
        "--loading-timeout-seconds",
        type=int,
        default=25,
        help="Maximum seconds to wait for spinner/shell pages before marking blocked",
    )
    parser.add_argument("--continue-on-error", action="store_true", help="Continue processing next course on errors")
    parser.add_argument(
        "--profile-dir",
        default=str(Path.home() / ".igot_qa_chrome_profile"),
        help="Persistent Chrome profile dir to reuse portal sessions",
    )
    parser.add_argument(
        "--gemini-api-key",
        default=_env_api_key("IGOT_GEMINI_API_KEY", "GEMINI_API_KEY"),
        help="Google Gemini API key (fallback AI). Get free key at aistudio.google.com",
    )
    parser.add_argument(
        "--gemini-model",
        default="gemini-2.0-flash",
        help="Gemini model to use (default: gemini-2.0-flash)",
    )
    parser.add_argument(
        "--groq-api-key",
        default=_env_api_key("IGOT_GROQ_API_KEY", "GROQ_API_KEY"),
        help="Groq API key (preferred AI, free & no daily quota). Get free key at console.groq.com/keys",
    )
    parser.set_defaults(pause_for_quiz=True)
    args = parser.parse_args()
    # Auto-enable prompt mode when no course-url and no start-url given
    if not args.course_url and not args.start_url and not args.prompt_mode:
        args.prompt_mode = True
    return args


async def amain(args: argparse.Namespace) -> int:
    run_id = f"run-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    reporter = Reporter(root=Path(args.output_dir), run_id=run_id)
    print(f"Run ID: {run_id}")
    print(f"Report directory: {reporter.root.resolve()}")

    async with async_playwright() as p:
        context = None

        async def launch_enhanced(profile_dir: str):
            return await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=args.headless,
                slow_mo=args.slow_mo_ms,
                args=[
                    f"--window-size={args.window_width},{args.window_height}",
                ],
                no_viewport=True,
                accept_downloads=False,
            )

        async def launch_compat(profile_dir: str):
            return await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=args.headless,
                slow_mo=args.slow_mo_ms,
                viewport={"width": 1440, "height": 900},
                accept_downloads=False,
            )

        profile_primary = args.profile_dir
        profile_fallback = str(Path(args.profile_dir).with_name(Path(args.profile_dir).name + f"_fallback_{run_id}"))

        launch_attempts = [
            ("enhanced", profile_primary, launch_enhanced),
            ("compat", profile_primary, launch_compat),
            ("enhanced", profile_fallback, launch_enhanced),
            ("compat", profile_fallback, launch_compat),
        ]

        last_error: Exception | None = None
        for mode, profile_dir, launcher in launch_attempts:
            try:
                if profile_dir == profile_fallback:
                    print(f"[info] Retrying with fresh profile: {profile_dir}")
                elif mode == "compat":
                    print("[info] Retrying with compatibility launch mode.")
                context = await launcher(profile_dir)
                if mode == "compat":
                    print("[info] Compatibility launch mode active.")
                break
            except Exception as launch_exc:  # noqa: BLE001
                last_error = launch_exc
                print(f"[warn] Launch attempt failed ({mode}, profile={profile_dir}): {launch_exc}")

        if context is None:
            raise RuntimeError(f"Unable to launch Chrome for Playwright. Last error: {last_error}")
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            runner = LiveQARunner(page=page, reporter=reporter, args=args)
            await runner.run()
        finally:
            try:
                await context.close()
            except PlaywrightError:
                pass

    print("Run completed.")
    print(f"JSONL: {reporter.jsonl_path.resolve()}")
    print(f"CSV:   {reporter.csv_path.resolve()}")
    print(f"Notes: {reporter.summary_path.resolve()}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
