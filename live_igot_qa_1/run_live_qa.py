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


MODULE_TYPES = {"video", "reading", "pdf", "slides", "quiz", "unknown"}
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

    async def run(self) -> None:
        start_url = self.args.start_url or self.args.base_url
        await self._safe_goto(start_url)
        await self._ensure_login()
        self.course_list_url = self.page.url
        processed_course_keys: set[str] = set()

        def course_key(c: CourseCandidate) -> str:
            href_key = (c.href or "").split("?", 1)[0].strip().lower()
            return f"{self._normalize_for_match(c.name)}::{href_key}"

        if self.args.course_url:
            await self._process_course(
                CourseCandidate(
                    name="Direct Course",
                    href=self.args.course_url,
                    completion_percent=None,
                    priority=0,
                )
            )
            self.reporter.write_csv()
            self.reporter.write_summary()
            return
        if not self.args.start_url:
            await self._go_to_course_hub()
        courses = await self._discover_courses()
        if not courses:
            print("No courses discovered automatically. Open My Courses in Chrome, then press Enter to retry once.")
            self._wait_for_enter()
            # Re-open original list URL before retry to avoid being stuck on a module page.
            try:
                await self._safe_goto(self.course_list_url or start_url)
            except PlaywrightError:
                pass
            courses = await self._discover_courses()
            if not courses:
                print("Still no courses found. Manual fallback: open one target course page in Chrome, then press Enter.")
                self._wait_for_enter()
                await self._wait_for_course_content()
                current_course_name = await self._derive_current_course_name()
                current_url = self.page.url
                manual_course = CourseCandidate(
                    name=current_course_name,
                    href=current_url,
                    completion_percent=None,
                    priority=0,
                )
                manual_key = course_key(manual_course)
                try:
                    await self._process_course(manual_course)
                    processed_course_keys.add(manual_key)
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] Manual fallback course processing failed for '{manual_course.name}': {exc}")
                    if not self.args.continue_on_error:
                        raise

                # After one manual course, retry normal discovery so run can continue.
                try:
                    await self._safe_goto(self.course_list_url or start_url)
                except PlaywrightError:
                    pass
                courses = [c for c in await self._discover_courses() if course_key(c) not in processed_course_keys]
                if not courses:
                    print("No additional courses discovered after manual fallback.")
                    self.reporter.write_csv()
                    self.reporter.write_summary()
                    return

        max_courses = self.args.max_courses if self.args.max_courses > 0 else len(courses)
        for course in courses[:max_courses]:
            key = course_key(course)
            if key in processed_course_keys:
                continue
            try:
                await self._process_course(course)
                processed_course_keys.add(key)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] Course processing failed for '{course.name}': {exc}")
                if not self.args.continue_on_error:
                    raise

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
            self._wait_for_enter()

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

    async def _discover_courses(self) -> list[CourseCandidate]:
        try:
            raw = await self.page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const elements = Array.from(document.querySelectorAll(
                  ".course-card, [data-course-id], [class*='course'] a[href], a[href*='collectionType=Course'], a[href*='collectionId='][href*='batchId=']"
                ));
                for (const el of elements) {
                    const anchor = el.matches("a[href]") ? el : el.querySelector("a[href]");
                    const href = anchor ? anchor.href : null;
                    const textRaw = (el.innerText || anchor?.innerText || "").replace(/\\s+/g, " ").trim();
                    if (!textRaw) continue;
                    const text = textRaw.slice(0, 240);
                    const low = `${text} ${href || ""}`.toLowerCase();
                    const hasCourseHref = !!href && (
                        low.includes("collectiontype=course") ||
                        (low.includes("collectionid=") && low.includes("batchid="))
                    );
                    const hasCourseText =
                        low.includes("course") ||
                        low.includes("learning") ||
                        low.includes("path") ||
                        /\\b\\d{1,3}%\\b/.test(low);
                    const looksCourseish =
                        hasCourseHref ||
                        hasCourseText ||
                        !!el.dataset.courseId;
                    if (!looksCourseish) continue;
                    const key = `${href || ""}::${text.toLowerCase()}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({ name: text, href });
                }
                return out;
            }"""
            )
        except PlaywrightError:
            return []
        courses: list[CourseCandidate] = []
        for item in raw:
            name = self._clean_name(item.get("name", "Untitled Course"))
            href = item.get("href")
            completion_percent = self._extract_percent(name)
            if self._is_course_noise(name, href):
                continue
            if self._is_completed_text(name):
                continue
            priority = completion_percent if completion_percent is not None else 0
            candidate = CourseCandidate(name=name, href=href, completion_percent=completion_percent, priority=priority)
            courses.append(candidate)
            self.courses_index[name] = candidate

        # Sort higher completion first for quick wins.
        courses.sort(key=lambda c: c.priority, reverse=True)
        print(f"Discovered {len(courses)} incomplete/active course candidates.")
        return courses

    async def _process_course(self, course: CourseCandidate) -> None:
        print(f"\n=== Course: {course.name} ===")
        opened = False
        already_inside_course = await self._is_inside_course_player()
        # Prefer in-app click flow for SPA stability.
        if not self.args.course_url and not already_inside_course:
            opened = await self._open_course_from_list(course.name)
        if not opened and course.href and not already_inside_course:
            await self._safe_goto(course.href)
        await self._snap(f"course_{self._slug(course.name)}_landing")
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
            self._wait_for_enter()
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
        if self.args.strict_sequence:
            await self._run_strict_sequence(course.name, max_modules)
        else:
            for module in modules[:max_modules]:
                if module.is_completed is True:
                    print(f"Module: {module.name} already ticked, skipping.")
                    continue
                if await self._is_module_ticked(module.name):
                    print(f"Module: {module.name} already ticked (live check), skipping.")
                    continue
                await self._process_module(course.name, module)

        course_done, course_evidence = await self._is_course_completed()
        course_status = "completed" if course_done else "partial"
        print(f"Course: {course.name} | Status: {course_status} | Evidence: {course_evidence}")

    async def _run_strict_sequence(self, course_name: str, max_modules: int) -> None:
        print("Strict sequence mode active: processing modules in order with tick verification.")
        completed_in_run = 0
        guard_cycles = 0
        attempted_keys: set[str] = set()
        skipped_keys: set[str] = set()
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
                print("No modules visible right now. Open the course TOC/content, then press Enter.")
                self._wait_for_enter()
                modules = await self._discover_modules()
                if not modules:
                    print("Still no modules visible. Ending strict sequence for this course.")
                    break

            pending = [m for m in modules if m.is_completed is not True]
            if not pending:
                print("All visible modules are already ticked.")
                break

            module = None
            module_key = ""
            now_ts = time.time()
            for candidate in pending:
                key = f"{self._normalize_for_match(candidate.name)}::{(candidate.href or '').strip().lower()}"
                if key in attempted_keys or key in skipped_keys:
                    continue
                if candidate.is_completed is True:
                    skipped_keys.add(key)
                    continue
                if await self._is_module_ticked(candidate.name):
                    skipped_keys.add(key)
                    print(f"Strict sequence: '{candidate.name}' already ticked, skipping.")
                    continue
                if await self._looks_like_section_header_live(candidate.name):
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

                print(
                    "Assessment module detected. Complete this assessment manually, submit, then press Enter to verify tick."
                )
                self._wait_for_enter()
                ticked = await self._is_module_ticked(module.name)
                attempted_keys.add(module_key)
                status = "done" if ticked else "partial"
                evidence = (
                    "Assessment tick verified after manual completion"
                    if ticked
                    else "Assessment submitted/manual step done but tick not visible yet"
                )
                next_action = "Continue to next module" if ticked else "Refresh and verify assessment tick manually"
                self._record(
                    course_name=course_name,
                    module_name=module.name,
                    module_type="quiz",
                    status=status,
                    block_reason=None,
                    evidence=evidence,
                    next_action=next_action,
                )
                print(f"Module: {module.name} | Status: {status} | Evidence: {evidence}")
                if not ticked:
                    if continue_without_tick:
                        attempted_keys.add(module_key)
                        print("Strict sequence: assessment not ticked yet; continuing due run-to-end setting.")
                        continue
                    print("Strict sequence paused: assessment is not ticked yet.")
                    break
                completed_in_run += 1
                retry_after.pop(module_key, None)
                timer_attempts.pop(module_key, None)
                continue

            await self._process_module(course_name, module)
            ticked = await self._is_module_ticked(module.name)
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
                    print(f"Strict sequence: '{module.name}' not ticked; continuing due run-to-end setting.")
                    continue
                print("Strict sequence: tick not visible yet. Complete remaining required steps, then press Enter to re-check.")
                self._wait_for_enter()
                ticked = await self._is_module_ticked(module.name)
            if not ticked:
                attempted_keys.add(module_key)
                if latest_status == "blocked":
                    print(f"Strict sequence paused: '{module.name}' is blocked ({latest_block}).")
                else:
                    print(f"Strict sequence paused: '{module.name}' is not ticked yet.")
                break
            completed_in_run += 1
            attempted_keys.add(module_key)
            retry_after.pop(module_key, None)
            timer_attempts.pop(module_key, None)

    async def _discover_modules(self) -> list[ModuleCandidate]:
        if await self._is_likely_loading():
            return []
        sidebar_modules = await self._discover_sidebar_modules()
        if self.args.strict_sequence and len(sidebar_modules) <= 1:
            expanded = await self._expand_all_sections()
            if expanded:
                await self.page.wait_for_timeout(700)
                sidebar_modules = await self._discover_sidebar_modules()
        if sidebar_modules:
            print(f"Discovered {len(sidebar_modules)} module candidates (sidebar parser).")
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

    async def _expand_all_sections(self) -> bool:
        try:
            changed = await self.page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll(
                      "li, [role='treeitem'], [class*='item'], [class*='accordion'], [class*='toc']"
                    ));
                    let clicked = 0;
                    for (const node of nodes) {
                        const text = (node.innerText || "").replace(/\\s+/g, " ").trim().toLowerCase();
                        if (!text) continue;
                        const isLikelySection =
                          /\\b(phase|module|section|chapter|part)\\b/.test(text) &&
                          /\\b\\d+\\s*items?\\b/.test(text);
                        if (!isLikelySection) continue;
                        const plus =
                          node.querySelector("[aria-label*='expand']") ||
                          node.querySelector("[aria-label*='plus']") ||
                          node.querySelector("[class*='plus']") ||
                          node.querySelector("[class*='expand']") ||
                          node.querySelector("[data-icon*='plus']") ||
                          node.querySelector("[data-icon='add']");
                        const target = plus || node.querySelector("button, [role='button'], a[href]") || node;
                        if (!target) continue;
                        target.scrollIntoView({ block: "center", behavior: "instant" });
                        target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        clicked += 1;
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
                        t = t.replace(/\\b(check_circle|radio_button_checked|add|remove)\\b/gi, "");
                        t = t.replace(/\\s+\\d+h(\\s+\\d+m)?(\\s+\\d+s)?$/i, "");
                        t = t.replace(/\\s+\\d+m(\\s+\\d+s)?(\\s+[•·\\-]\\s+\\d+\\s*item[s]?)?$/i, "");
                        t = t.replace(/\\s+\\d+s$/i, "");
                        t = normalize(t);
                        return t.slice(0, 120);
                    };

                    const looksLeafRow = (text) => {
                        if (!text) return false;
                        if (text.length > 120) return false;
                        if (/\\b\\d+\\s*items?\\b/i.test(text)) return false;
                        if (/\\b(add|expand|collapse)\\b/i.test(text) && /\\b(phase|module|section|chapter|part)\\b/i.test(text)) return false;
                        if (/\\b\\d+\\.\\s/.test(text.replace(/^\\d+\\.\\s*/, ""))) return false;
                        return /^\\d+\\./.test(text) || /\\b\\d+m\\b|\\b\\d+s\\b/i.test(text) || /\\b\\d+\\s*questions?\\b/i.test(text);
                    };

                    const out = [];
                    const seen = new Set();
                    const nodes = Array.from(document.querySelectorAll("li, [role='treeitem'], [class*='item'], [class*='accordion'], [class*='toc']"));

                    for (const node of nodes) {
                        const rawText = normalize(node.innerText || "");
                        if (!looksLeafRow(rawText)) continue;
                        const childTreeItems = node.querySelectorAll("li, [role='treeitem']").length;
                        if (childTreeItems > 1) continue; // Likely section container with children.
                        const hasExpandToggle = !!node.querySelector(
                          "[aria-label*='expand'], [aria-label*='plus'], [class*='plus'], [class*='expand'], [data-icon*='plus'], [data-icon='add']"
                        );
                        if (hasExpandToggle && /\\b\\d+\\s*items?\\b/i.test(rawText)) continue;
                        const title = cleanTitle(rawText);
                        if (!title || title.length < 4) continue;
                        const rowHtml = (node.innerHTML || "").toLowerCase();
                        const done =
                            rowHtml.includes("check_circle") ||
                            rowHtml.includes("completed") ||
                            rowHtml.includes("aria-checked=\\"true\\"") ||
                            rowHtml.includes("aria-checked='true'");
                        const key = title.toLowerCase();
                        if (seen.has(key)) continue;
                        seen.add(key);

                        const anchor = node.querySelector("a[href]");
                        out.push({
                            name: title,
                            href: anchor ? anchor.href : null,
                            hint: "unknown",
                            done: done
                        });
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
        if re.search(r"\b\d+\s*items?\b", low) and re.search(r"\b(phase|module|section|chapter|part)\b", low):
            return True
        if re.match(r"^(phase|module|section|chapter|part)\s*\d+\b", low):
            return True
        if re.match(r"^\d+\.\s*(phase|module|section|chapter|part)\b", low):
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
        if await self._is_module_ticked(module.name):
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
                    self._record(
                        course_name=course_name,
                        module_name=module.name,
                        module_type=module.module_type_hint if module.module_type_hint in MODULE_TYPES else "unknown",
                        status="blocked",
                        block_reason="technical_error",
                        evidence="Module navigation failed: module click/link was not available",
                        next_action="Open module manually and rerun",
                    )
                    print(f"Module: {module.name} | Status: blocked | Evidence: Module navigation failed: module click/link was not available")
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

        if module_type == "video":
            status, evidence, next_action = await self._handle_video()
        elif module_type in {"reading", "pdf", "slides"}:
            status, evidence, next_action = await self._handle_reading_like(module_type)
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
            status = "partial"
            evidence = f"{evidence}; sidebar tick not visible yet"
            next_action = "Complete module fully and wait for tick update"
        elif status == "partial" and not self.args.auto_run_to_end:
            print("If this module is now complete in UI, press Enter to re-check tick.")
            self._wait_for_enter()
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

        await self._fast_navigate_next()

    async def _detect_module_type(self, hint: str) -> str:
        if hint in MODULE_TYPES and hint != "unknown":
            return hint
        try:
            url = self.page.url.lower()
            if "viewer/pdf" in url or ".pdf" in url:
                return "pdf"
            if "viewer/video" in url or "youtube.com" in url or "vimeo.com" in url:
                return "video"
            if "viewer/practice" in url or "practice%20question%20set" in url or "quiz" in url or "assessment" in url:
                return "quiz"
            if "viewer/slide" in url:
                return "slides"
            if await self.page.locator("video").count() > 0:
                return "video"
            if await self.page.locator("embed[type='application/pdf'], iframe[src*='.pdf'], .pdf-viewer").count() > 0:
                return "pdf"
            body = (await self._body_text()).lower()
            if any(x in body for x in ["quiz", "assessment", "question", "submit", "start assessment", "question set", "retakes"]):
                return "quiz"
            if any(x in body for x in ["slide", "deck"]):
                return "slides"
            if any(x in body for x in ["article", "reading", "chapter", "content"]):
                return "reading"
        except PlaywrightError:
            return "unknown"
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
        try:
            if getattr(self, "course_list_url", ""):
                await self._safe_goto(self.course_list_url)
        except PlaywrightError:
            pass

        query = self._course_query_name(course_name)
        candidates = [
            query,
            course_name,
        ]
        for q in candidates:
            if not q:
                continue
            try:
                locator = self.page.get_by_text(re.compile(re.escape(q), re.I))
                if await locator.first.is_visible(timeout=1000):
                    await locator.first.scroll_into_view_if_needed(timeout=1200)
                    await locator.first.click()
                    await self.page.wait_for_timeout(1500)
                    return True
            except PlaywrightError:
                continue

        # Last chance: try button/link controls near this label.
        for label in [r"continue learning", r"resume", r"start", r"open"]:
            try:
                ctl = self.page.get_by_role("button", name=re.compile(label, re.I))
                if await ctl.first.is_visible(timeout=700):
                    await ctl.first.click()
                    await self.page.wait_for_timeout(1200)
                    return True
            except PlaywrightError:
                continue
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
                    const loadingEls = Array.from(document.querySelectorAll(
                      "[class*='loading'], [class*='loader'], [class*='spinner'], [aria-busy='true']"
                    ));
                    const visibleLoaders = loadingEls.filter(el => {
                      const s = window.getComputedStyle(el);
                      return s && s.display !== "none" && s.visibility !== "hidden" && el.getBoundingClientRect().height > 4;
                    }).length;
                    const footerOnly = body.includes("hubs") && body.includes("support") && body.includes("about us");
                    const hasCourseSignals =
                      body.includes("module") || body.includes("lesson") || body.includes("quiz") || body.includes("course");
                    if (visibleLoaders > 0) return true;
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
        speed = max(0.5, min(float(requested_speed), 2.0))
        labels = [f"{speed:g}x"]

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
        if duration <= 0:
            wait_seconds = max(20, min(self.args.video_max_wait_seconds, int(self.args.video_observe_seconds * 3)))
        else:
            remaining = max(0.0, duration - current)
            wait_seconds = int(max(20, min(self.args.video_max_wait_seconds, (remaining / max(0.5, preferred_speed)) + 20)))

        started = time.time()
        while (time.time() - started) < wait_seconds:
            ticked = await self._is_module_ticked(module_name)
            if ticked:
                return True, "Sidebar tick detected during auto video wait"
            done, done_evidence = await self._is_module_completed()
            if done:
                return True, f"Completion signal during auto video wait: {done_evidence}"

            latest = self._latest_module_report(course_name, module_name)
            if latest and latest.block_reason in {"permission_issue", "technical_error"}:
                return False, f"Stopping auto wait due block state: {latest.block_reason}"

            await self._apply_video_speed(preferred_speed)
            await self.page.wait_for_timeout(5000)

        return False, f"Auto video wait timed out after {wait_seconds}s"

    async def _handle_video(self) -> tuple[str, str, str]:
        # Observe playback behavior and completion state; no skip/seek logic.
        requested_speed = max(0.5, min(float(self.args.video_speed), 2.0))
        speed_result = await self._apply_video_speed(requested_speed)
        metrics1 = await self._get_first_video_metrics()
        if not metrics1:
            return "blocked", "No video element detected", "Refresh module and retry detection"

        await self.page.wait_for_timeout(int(self.args.video_observe_seconds * 1000))
        await self._apply_video_speed(requested_speed)
        metrics2 = await self._get_first_video_metrics()
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"video completion signal: {done_evidence}", "Continue to next eligible module"
        if metrics2 and metrics2["currentTime"] > metrics1["currentTime"] + 0.5:
            evidence = (
                f"video playback progressing ({metrics1['currentTime']:.1f}s -> "
                f"{metrics2['currentTime']:.1f}s) at {metrics2.get('playbackRate', 1):.2f}x; "
                f"speed target {speed_result.get('target_label', '2x')}, "
                f"applied to {speed_result.get('adjusted', 0)}/{speed_result.get('videos', 0)} video element(s), "
                f"ui_click={speed_result.get('ui_clicked', False)}; "
                "completion not yet confirmed"
            )
            return "partial", evidence, "Allow playback to complete and rerun audit"
        return "blocked", "Video did not progress and no completion signal detected", "Capture logs and escalate technical issue"

    async def _handle_reading_like(self, module_type: str) -> tuple[str, str, str]:
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"{module_type} completion signal: {done_evidence}", "Continue to next eligible module"
        body = (await self._body_text()).lower()
        if any(x in body for x in ["page 1 of", "scroll", "next page", "continue reading"]):
            return "partial", f"{module_type} detected; completion signal not yet present", "Perform required progression and rerun audit"
        return "partial", f"{module_type} module open; no completion signal yet", "Review module requirements and continue"

    async def _handle_quiz_assistive(self, module_name: str) -> tuple[str, str, str]:
        if self.args.skip_assessments:
            return (
                "partial",
                "Assessment skipped by configuration (--skip-assessments)",
                "Complete this assessment manually later",
            )

        quiz_data = await self.page.evaluate(
            """() => {
                const questions = [];
                const qNodes = Array.from(document.querySelectorAll(".question, [class*='question'], fieldset"));
                for (const q of qNodes.slice(0, 50)) {
                    const qText = (q.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 500);
                    if (!qText) continue;
                    const options = Array.from(q.querySelectorAll("label, .option, [role='option']"))
                        .map(el => (el.innerText || "").replace(/\\s+/g, " ").trim())
                        .filter(Boolean)
                        .slice(0, 10);
                    questions.push({ question: qText, options });
                }
                return questions;
            }"""
        )
        artifact_file = self.reporter.artifacts_dir / f"quiz_extract_{self._slug(module_name)}.json"
        artifact_file.write_text(json.dumps({"module": module_name, "questions": quiz_data}, indent=2), encoding="utf-8")

        body = (await self._body_text()).lower()
        if any(x in body for x in ["passed", "you passed", "score: 100", "congratulations"]):
            return "done", "Quiz page shows pass/completion indicator", "Continue to next eligible module"
        if self.args.pause_for_quiz:
            print(
                "Quiz assistive mode: review generated quiz_extract artifact, answer manually in browser, "
                "submit, then press Enter to continue."
            )
            self._wait_for_enter()
            done, done_evidence = await self._is_module_completed()
            if done:
                return "done", f"Post-submit completion signal: {done_evidence}", "Continue to next eligible module"
        return "partial", "Quiz requires user-reviewed submission; no pass signal yet", "User completes quiz manually, then rerun audit"

    async def _handle_unknown(self) -> tuple[str, str, str]:
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"Generic completion signal: {done_evidence}", "Continue to next eligible module"
        return "partial", "Unknown module type open; no completion signal yet", "Review module manually and rerun audit"

    async def _fast_navigate_next(self) -> None:
        # Fast movement between eligible items while respecting portal logic.
        for label in ["Next", "Continue", "Next Module", "Proceed"]:
            locator = self.page.get_by_role("button", name=re.compile(label, re.I))
            try:
                if await locator.first.is_visible(timeout=400):
                    await locator.first.click()
                    await self.page.wait_for_timeout(600)
                    return
            except PlaywrightError:
                continue

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

    async def _is_module_ticked(self, module_name: str) -> bool:
        target = self._normalize_for_match(module_name)
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
                        const nodes = Array.from(document.querySelectorAll("li, [role='treeitem'], [class*='item'], [class*='accordion'], [class*='toc']"));
                        for (const node of nodes) {
                          const rawText = (node.innerText || "").replace(/\\s+/g, " ").trim();
                          if (!rawText || rawText.length > 140) continue;
                          const titleNorm = cleanTitle(rawText);
                          if (!titleNorm) continue;
                          const titleMatches = titleNorm.includes(targetNorm) || targetNorm.includes(titleNorm);
                          if (!titleMatches) continue;
                          const html = (node.innerHTML || "").toLowerCase();
                          const done =
                            html.includes("check_circle") ||
                            html.includes("completed") ||
                            html.includes("aria-checked=\\"true\\"") ||
                            html.includes("aria-checked='true'");
                          if (done) return true;
                        }
                        return false;
                    }""",
                    target,
                )
            )
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
        if not href and not re.search(r"\b\d{1,3}\s*%\b", low) and "course" not in low and "learning" not in low:
            return True
        return False

    @staticmethod
    def _is_module_noise(name: str, href: str | None) -> bool:
        low = name.strip().lower()
        compact = re.sub(r"[•·\-]", " ", low)
        compact = re.sub(r"\s+", " ", compact).strip()
        if low in {"next", "previous", "prev", "back", "close", "menu"}:
            return True
        if "faq" in low or "faqs" in low:
            return True
        if any(x in low for x in ["about content start discussion", "download app", "privacy policy", "hubs support"]):
            return True
        if "rating" in low or "ratings" in low:
            return True
        if "star star" in low or "star_half" in low:
            return True
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
    def _wait_for_enter() -> None:
        try:
            input()
        except EOFError:
            # Non-interactive execution environment.
            return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run compliant live QA + audit on iGot-like learning portal.")
    parser.add_argument("--base-url", default="https://igotkarmayogi.gov.in", help="Portal base URL")
    parser.add_argument("--start-url", default="", help="Optional direct URL to start from (skips auto hub navigation)")
    parser.add_argument("--course-url", default="", help="Optional direct course URL (skip course discovery)")
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
        default=2.0,
        help="Preferred compliant video playback speed (0.5 to 2.0)",
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
    parser.set_defaults(pause_for_quiz=True)
    return parser.parse_args()


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
