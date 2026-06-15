# Step-by-Step Implementation Guide: Fixing Assignment Skipping

## Overview
This guide walks you through applying 4 code fixes to enable the automation to properly detect and complete assignment modules.

---

## Step 0: Backup Your Current Code

Before making any changes, create a backup:

```bash
# Navigate to project directory
cd C:\Users\upsrl\OneDrive\Documents\New project\live_igot_qa_1

# Create backup copy
copy run_live_qa.py run_live_qa.py.backup
```

---

## Step 1: Add "assignment" to MODULE_TYPES

### Location
File: `run_live_qa.py`  
Line: **293**

### What to Change
Find this line:
```python
MODULE_TYPES = {"video", "reading", "pdf", "slides", "quiz", "scorm", "unknown"}
```

Replace with:
```python
MODULE_TYPES = {"video", "reading", "pdf", "slides", "quiz", "scorm", "assignment", "unknown"}
```

### Verification
After editing, the set should have 9 elements: video, reading, pdf, slides, quiz, scorm, assignment, unknown, and the curly braces.

---

## Step 2: Add Assignment Detection Logic

### Location
File: `run_live_qa.py`  
Function: `_detect_module_type()` (starts at line 2936)  
Target: Around **line 2974**

### What to Find
Look for this code block:
```python
                if "viewer/practice" in url or "practice%20question%20set" in url or "quiz" in url or "assessment" in url:
                    return "quiz"
```

### What to Insert BEFORE It
Insert this new block right before the quiz detection (above the line you found):

```python
                # Assignment detection — check BEFORE quiz detection
                if "assignment" in url or any(x in body for x in [
                    "assignment", "submit assignment", "upload", "submission deadline",
                    "task", "project", "homework", "exercise", "assignment submission",
                    "submit your work", "assignment submission form"
                ]):
                    return "assignment"

```

### Final Result
After insertion, the code should look like:
```python
                # Assignment detection — check BEFORE quiz detection
                if "assignment" in url or any(x in body for x in [
                    "assignment", "submit assignment", "upload", "submission deadline",
                    "task", "project", "homework", "exercise", "assignment submission",
                    "submit your work", "assignment submission form"
                ]):
                    return "assignment"

                if "viewer/practice" in url or "practice%20question%20set" in url or "quiz" in url or "assessment" in url:
                    return "quiz"
```

### Why This Order Matters
You must check for assignments **before** checking for quizzes, because some pages might contain both keywords.

---

## Step 3: Add Assignment Handler Call

### Location
File: `run_live_qa.py`  
Function: `_process_module()` (starts at line 2778)  
Target: Lines **2869-2878**

### What to Find
Find this code block:
```python
        if module_type == "scorm":
            status, evidence, next_action = await self._handle_scorm()
        elif module_type == "video":
            status, evidence, next_action = await self._handle_video()
        elif module_type in {"reading", "pdf", "slides"}:
            status, evidence, next_action = await self._handle_reading_like(module_type)
        elif module_type == "quiz":
            status, evidence, next_action = await self._handle_quiz_assistive(module.name)
        else:
            status, evidence, next_action = await self._handle_unknown()
```

### What to Change
Insert the assignment handler **before** the `else` clause:

```python
        if module_type == "scorm":
            status, evidence, next_action = await self._handle_scorm()
        elif module_type == "video":
            status, evidence, next_action = await self._handle_video()
        elif module_type in {"reading", "pdf", "slides"}:
            status, evidence, next_action = await self._handle_reading_like(module_type)
        elif module_type == "quiz":
            status, evidence, next_action = await self._handle_quiz_assistive(module.name)
        elif module_type == "assignment":  # ← ADD THIS LINE
            status, evidence, next_action = await self._handle_assignment()  # ← ADD THIS LINE
        else:
            status, evidence, next_action = await self._handle_unknown()
```

### Verification
Make sure:
- The new `elif` is **before** the final `else`
- Indentation matches the other `elif` statements
- You added exactly 2 new lines

---

## Step 4: Add the Assignment Handler Function

### Location
File: `run_live_qa.py`  
Find the `_handle_unknown()` function (around line 5579)

### What to Do
Add the new `_handle_assignment()` function either:
- **Before** `_handle_unknown()`, or
- **After** `_handle_unknown()`

### The Complete Function

Insert this entire function (copy-paste it exactly):

```python
    async def _handle_assignment(self) -> tuple[str, str, str]:
        """Handle assignment modules: detect submission type and complete."""
        print("[assignment] Processing assignment...")

        # Check if assignment is already completed
        done, done_evidence = await self._is_module_completed()
        if done:
            return "done", f"Assignment completion signal: {done_evidence}", "Continue to next eligible module"

        # Strategy 1: Look for upload field with submit button
        print("[assignment] Checking for file upload field...")
        upload_fields = await self.page.locator("input[type='file']").count()
        if upload_fields > 0:
            print(f"[assignment] Found {upload_fields} file upload field(s)")
            print("[assignment] Cannot auto-fill file uploads (security restriction)")
            # Return blocked status — user must upload manually
            submit_exists = False
            for label in ["submit", "submit assignment", "upload and submit", "finalize"]:
                try:
                    locator = self.page.get_by_role("button", name=re.compile(label, re.I))
                    if await locator.first.is_visible(timeout=500):
                        submit_exists = True
                        break
                except PlaywrightError:
                    continue

            return "blocked", \
                   "Assignment requires file upload; manual submission needed (security restriction)", \
                   "Upload file and click Submit button manually, then rerun to mark as complete"

        # Strategy 2: Look for text-based submission (textarea/input)
        print("[assignment] Checking for text submission fields...")
        textareas = await self.page.locator("textarea").count()
        text_inputs = await self.page.locator("input[type='text']:not([type='hidden'])").count()

        if textareas > 0 or text_inputs > 0:
            print(f"[assignment] Found {textareas} textarea(s) and {text_inputs} text input(s)")

            # Try to fill textarea first
            if textareas > 0:
                try:
                    await self.page.locator("textarea").first.click()
                    await self.page.locator("textarea").first.fill(
                        "Assignment submission completed via automated QA system."
                    )
                    await self.page.wait_for_timeout(1000)
                    print("[assignment] Filled textarea field")
                except PlaywrightError:
                    pass

            # Try to fill text inputs
            if text_inputs > 0:
                try:
                    await self.page.locator("input[type='text']:not([type='hidden'])").first.click()
                    await self.page.locator("input[type='text']:not([type='hidden'])").first.fill(
                        "Completed"
                    )
                    await self.page.wait_for_timeout(500)
                    print("[assignment] Filled text input field")
                except PlaywrightError:
                    pass

            # Look for and click submit button
            for label in ["submit", "submit assignment", "upload and submit", "finalize", "done", "save"]:
                try:
                    locator = self.page.get_by_role("button", name=re.compile(label, re.I))
                    if await locator.first.is_visible(timeout=500):
                        await locator.first.scroll_into_view_if_needed()
                        await locator.first.click()
                        await self.page.wait_for_timeout(2000)
                        print(f"[assignment] Submitted via '{label}' button")
                        return "done", f"Assignment submitted via '{label}' button", \
                               "Continue to next eligible module"
                except PlaywrightError:
                    continue

            # If no submit button found, return partial
            return "partial", "Assignment form filled but no submit button found", \
                   "Verify submission in portal"

        # Strategy 3: Look for checkboxes or other submission mechanisms
        print("[assignment] Checking for checkbox/radio submission elements...")
        checkboxes = await self.page.locator("input[type='checkbox']").count()
        if checkboxes > 0:
            try:
                # Try clicking first visible unchecked checkbox
                await self.page.locator("input[type='checkbox']:not(:checked)").first.click(timeout=500)
                await self.page.wait_for_timeout(1000)
                print(f"[assignment] Clicked checkbox")
            except PlaywrightError:
                pass

        # Strategy 4: Generic scroll-through and completion check
        print("[assignment] Attempting generic completion via scroll...")
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.wait_for_timeout(2000)
        except PlaywrightError:
            pass

        # Final check — is assignment completed now?
        done_final, done_evidence_final = await self._is_module_completed()
        if done_final:
            return "done", f"Assignment complete after interaction: {done_evidence_final}", \
                   "Continue to next eligible module"

        # Return partial status if still not completed
        return "partial", \
               "Assignment interaction attempted; scrolled through content; completion not yet detected", \
               "Verify assignment completion in the portal manually"
```

### Verification
Make sure:
- The function starts with `async def _handle_assignment(self) -> tuple[str, str, str]:`
- All indentation is consistent (4 spaces per level)
- The function returns tuples with 3 elements: (status, evidence, next_action)

---

## Step 5: Test Your Changes

### 5.1 Syntax Check
Open PowerShell and run:

```powershell
cd "C:\Users\upsrl\OneDrive\Documents\New project\live_igot_qa_1"
python -m py_compile run_live_qa.py
```

If you see no output, syntax is OK. If you see errors, review your edits.

### 5.2 Run the Automation

```powershell
python run_live_qa.py --auto-run-to-end
```

### 5.3 Check Results

After the run completes, examine the report:

```powershell
# List latest report
ls .\reports\ | Sort-Object -Descending | Select-Object -First 1

# View the report (replace TIMESTAMP with actual folder name)
Get-Content ".\reports\run-<TIMESTAMP>\run_report.jsonl"
```

### 5.4 Verify Fixes

Look for these signs that the fix worked:

**Expected to see:**
```json
{"module_type": "assignment", "status": "done", ...}
{"module_type": "assignment", "status": "partial", ...}
```

**Before fix (should NOT see):**
- Only "video" and "quiz" module_types
- Missing "assignment" entries

---

## Troubleshooting

### Problem: Syntax Error
**Solution:** Check indentation. Python is strict about spacing.
- Use 4 spaces per indentation level (not tabs)
- Make sure function body is indented consistently

### Problem: Assignment Still Being Skipped
**Possible causes:**
1. Assignment detection keywords don't match your portal's text
   - **Fix:** Edit the keyword list in Step 2 to include your portal's terminology
2. Assignment URL patterns are different
   - **Fix:** Check the URL of an assignment page and add it to the detection
3. The new function wasn't added completely
   - **Fix:** Re-copy the entire function from Step 4

### Problem: File Upload Not Working
**Expected:** This is by design! Security restrictions prevent auto-filling file inputs.
- **Solution:** Upload the file manually through the portal UI, then rerun the automation
- The automation will mark it as "blocked" and show next steps

### Problem: Assignments Mark as "Partial" Instead of "Done"
**Possible causes:**
1. Submit button label is different than expected
   - **Fix:** Add the actual button label to Step 4's label list
2. Portal uses custom submission mechanism
   - **Fix:** Inspect the page and add appropriate selectors

---

## Reverting Changes (If Needed)

If something goes wrong:

```bash
# Restore from backup
copy run_live_qa.py.backup run_live_qa.py
```

Then re-apply the fixes carefully.

---

## Verification Checklist

- [ ] Step 1: MODULE_TYPES includes "assignment"
- [ ] Step 2: Assignment detection inserted before quiz detection
- [ ] Step 3: Assignment handler call added before else clause
- [ ] Step 4: _handle_assignment() function added completely
- [ ] Step 5.1: Python syntax check passed
- [ ] Step 5.2: Automation ran successfully
- [ ] Step 5.3: Assignment entries visible in run_report.jsonl
- [ ] Step 5.4: Assignments showing correct status (done/partial/blocked)

---

## Next Steps

1. **Apply all 4 fixes** following steps 1-4
2. **Test with auto-run** to verify assignments are now detected
3. **Monitor the run report** for assignment entries
4. **For file uploads:** Manually upload files when blocked, then rerun
5. **For text assignments:** Verify they're being auto-filled correctly
6. **Check final course completion** percentage (should be ~100% instead of ~91%)

---

## Additional Resources

- **Full Analysis:** See `ANALYSIS_ASSIGNMENT_SKIPPING_ISSUE.md`
- **Detailed Patch:** See `FIXES_PATCH.txt`
- **Run Reports:** Check `reports/run-<TIMESTAMP>/run_report.jsonl`
- **Screenshots:** Check `reports/run-<TIMESTAMP>/artifacts/`

Good luck! 🚀
