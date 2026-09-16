# AGENTS.md

## Role

You are the coding agent for **Teloude**, an open-source Windows desktop backup application.

Teloude uses the user's personal Telegram account as a cloud backup backend through the Telegram MTProto API.

You are responsible for implementing the project according to `PROJECT_SPEC.md`.

`PROJECT_SPEC.md` is the primary product and architecture specification.

> Status: v1 is implementation-complete (phases 0-9) and the Windows release is
> built with one command — `powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1`
> (see `docs/installer_build_and_test.md`). Sections 29-30 below record the original
> bootstrap milestone and are kept for history; see `README.md`, `docs/CHANGELOG.md`
> and `docs/FINAL_REPORT.md` for the current state.

---

# 1. Before Doing Anything

Before implementing or modifying code:

1. Read `PROJECT_SPEC.md`.
2. Inspect the existing repository structure.
3. Inspect relevant existing files before changing them.
4. Understand the current architecture.
5. Do not assume that a feature already exists.
6. Do not silently change architectural decisions.

If the requested change conflicts with `PROJECT_SPEC.md`, identify the conflict before implementing it.

---

# 2. General Development Philosophy

Build Teloude as a real production application.

Priorities:

1. Correctness
2. Data safety
3. Reliability
4. Maintainability
5. Performance
6. UI quality

Do not optimize prematurely.

Do not create unnecessarily complicated abstractions.

Do not create giant files containing unrelated functionality.

Prefer small, focused modules.

---

# 3. Architecture Rules

Respect the architecture defined in `PROJECT_SPEC.md`.

The project should maintain clear separation between:

```text
UI
Application
Core
Infrastructure
```

The UI must not contain business logic.

The UI must not directly perform Telegram API operations.

The UI must not contain database SQL.

Business logic must not depend directly on PySide6 widgets.

Telegram-specific implementation must remain behind an abstraction layer.

---

# 4. Telegram Rules

Teloude uses Telegram MTProto.

Do NOT implement the application using the Telegram Bot API.

Do not invent Telegram API behavior.

When implementing Telegram-specific functionality:

1. Check the current official Telegram documentation.
2. Verify method names and parameters.
3. Verify current limits.
4. Handle RPC errors correctly.
5. Handle FloodWait/rate limiting.
6. Avoid unnecessary API calls.
7. Avoid abusive request patterns.

Official Telegram documentation:

```text
https://core.telegram.org/api
https://core.telegram.org/api/files
https://core.telegram.org/api/forum
https://core.telegram.org/api/obtaining_api_id
https://core.telegram.org/api/terms
```

Telegram behavior may change over time.

Do not permanently hard-code values that Telegram exposes dynamically.

---

# 5. Telegram Authentication

Authentication must use MTProto.

Expected flow:

```text
Phone number
    ↓
Verification code
    ↓
2FA password if enabled
    ↓
Authenticated session
```

Never log:

* phone verification codes
* 2FA passwords
* API secrets
* session secrets
* authentication tokens

Store sessions securely using Windows security mechanisms.

If authentication fails, provide a clear recoverable state.

---

# 6. File Safety

User files are sacred.

The application must NEVER automatically:

* delete local files
* move local files
* rename local files
* modify local files
* overwrite local files without explicit confirmation

Backup operations must be read-only with respect to source files.

When reading a file for hashing or upload, do not modify it.

---

# 7. Upload Rules

Upload one logical file at a time.

Internal chunk-level concurrency is allowed when useful.

Never load an entire large file into memory.

Use streaming/chunked file access.

The upload system must support:

* progress
* pause
* resume
* cancel
* retry
* network recovery
* crash recovery

Do not mark a file as successfully backed up until the operation has actually completed and the required metadata has been persisted.

---

# 8. Transfer State

Every important transfer state must be persisted.

A transfer should have an explicit state machine.

Possible states include:

```text
queued
scanning
hashing
uploading
paused
waiting_for_network
verifying
completed
failed
cancelled
```

Do not rely exclusively on in-memory state.

The application must be able to recover after:

* application crash
* Windows restart
* network failure
* temporary Telegram failure

---

# 9. Resume Behavior

Resume must be implemented carefully.

Telegram's temporary upload state may expire.

Therefore:

* persist local transfer state
* attempt to resume when possible
* detect invalid/expired Telegram-side state
* restart only the affected file when necessary
* never lose already completed files

Do not claim that Telegram uploads can always resume indefinitely from the exact byte.

---

# 10. Database Rules

Use SQLite.

Database access must be centralized.

Do not scatter SQL statements throughout UI classes.

Use migrations.

Database changes must be backwards-conscious.

Do not casually delete or rename database columns.

When changing the schema:

1. Create a migration.
2. Test the migration.
3. Test a fresh database.
4. Test an existing database upgrade.

Use transactions for related state changes.

---

# 11. Filesystem Rules

Use `pathlib` for filesystem operations.

Be careful with:

* Windows drive letters
* UNC paths
* Unicode filenames
* spaces
* long paths
* symbolic links
* inaccessible directories
* permission errors

Never assume filenames are ASCII.

Restore operations must protect against path traversal.

Never construct unsafe restore paths from untrusted remote filenames.

---

# 12. Duplicate Detection

Duplicate detection should use reliable file identity.

SHA-256 is the authoritative content hash.

For performance, a cheaper preliminary fingerprint may be used before calculating the complete hash.

Do not declare two files identical based only on filename.

Do not delete duplicates automatically.

The user must be given explicit choices:

```text
Skip
Upload Again
Cancel
```

Support:

```text
Apply to all
```

when appropriate.

---

# 13. Cloud Deletion

Deleting cloud backups is destructive.

Never delete Telegram backup messages automatically.

A cloud deletion operation must:

1. Be explicitly triggered by the user.
2. Clearly explain what will happen.
3. Require confirmation.
4. Never delete the corresponding local source file.

---

# 14. Restore Rules

Restore operations must be conservative.

Before overwriting an existing local file, ask the user.

Possible collision options:

```text
Skip
Overwrite
Keep both
Cancel
```

Do not silently overwrite user data.

After restore, verify file integrity when sufficient metadata exists.

A failed verification must not be presented as a successful restore.

---

# 15. Preview System

Preview generation is secondary to backup correctness.

Preview failures must not fail a backup.

Preview operations should run asynchronously.

Supported preview categories may include:

* common images
* embedded JPEG previews from RAW files
* video thumbnails

Do not decode large media files unnecessarily just to display a small thumbnail.

Cache previews where appropriate.

---

# 16. UI Rules

The UI must remain responsive.

Never perform long-running work directly on the Qt UI thread.

This includes:

* filesystem scanning
* hashing
* uploading
* downloading
* preview generation
* large database operations

Use an appropriate worker/async architecture.

Keep progress reporting granular enough to feel responsive.

---

# 17. UI Quality

The application should look like a professional desktop backup application.

Avoid:

* debug-style interfaces
* excessive technical terminology
* unnecessary dialogs
* cluttered layouts
* giant configuration screens

Prefer:

* clear hierarchy
* obvious actions
* meaningful empty states
* understandable errors
* consistent spacing
* consistent typography
* keyboard accessibility where practical

---

# 18. Error Handling

Never silently swallow important exceptions.

Do not use broad exception handling such as:

```python
except Exception:
    pass
```

unless there is a very specific and documented reason.

Errors should be:

* logged
* associated with the operation
* recoverable when possible
* understandable to users

Separate user-facing error messages from technical diagnostics.

---

# 19. Logging Rules

Use structured logging.

Logs must help diagnose:

* upload failures
* restore failures
* network failures
* database errors
* authentication problems
* unexpected states

Never log:

* Telegram verification codes
* Telegram 2FA passwords
* API secrets
* session secrets
* raw file contents

Avoid logging unnecessary personal information.

---

# 20. Configuration

Application configuration must be centralized.

Do not scatter configuration constants throughout the project.

Use a clear configuration layer.

Separate:

```text
application settings
user preferences
runtime state
secrets
```

Secrets must never be stored in normal plaintext configuration files.

---

# 21. Dependencies

Do not add a dependency just because it makes a small task easier.

Before adding a dependency:

1. Check whether the standard library can solve the problem.
2. Check whether an existing dependency already provides the functionality.
3. Consider package maintenance and security.
4. Consider Windows packaging implications.
5. Keep the dependency footprint reasonable.

Every dependency should have a clear reason.

---

# 22. Async / Concurrency

Choose one coherent concurrency architecture.

Do not randomly mix:

```text
asyncio
QThread
ThreadPoolExecutor
multiprocessing
```

without a clear reason.

Telegram operations must be safe.

The UI must remain responsive.

Cancellation must be cooperative.

Application shutdown must be predictable.

---

# 23. Testing

Every important piece of business logic should have tests.

Prioritize tests for:

* configuration
* database
* migrations
* file scanning
* hashing
* duplicate detection
* path handling
* folder mapping
* transfer state
* speed limiting
* restore collision handling

Telegram integration tests should be separated from ordinary unit tests.

Do not make normal unit tests depend on a live Telegram account.

---

# 24. Test Philosophy

Tests should verify behavior, not implementation details.

Prefer:

```text
Given
When
Then
```

style reasoning.

Example:

```text
Given a file exists
When a backup starts
Then the file is uploaded
And the local file remains unchanged
And the database records completion
```

Test failure cases, not only happy paths.

---

# 25. Git Discipline

Make small logical changes.

Prefer commits such as:

```text
bootstrap project structure
add configuration system
add database layer
add telegram abstraction
add telegram authentication
add storage creation
add upload engine
add resume support
add restore engine
add search
add preview system
add system tray
add installer
```

Avoid massive commits containing unrelated features.

Do not rewrite unrelated code just for stylistic reasons.

---

# 26. Agent Workflow

For every task:

### Step 1: Understand

Read the relevant specification and inspect the repository.

### Step 2: Plan

Determine:

* which files need changing
* what new modules are needed
* what tests are required
* whether the change affects architecture

### Step 3: Implement

Make the smallest clean implementation that satisfies the requirement.

### Step 4: Test

Run relevant tests.

### Step 5: Inspect

Review:

* errors
* warnings
* imports
* type issues
* edge cases
* unintended changes

### Step 6: Report

Explain briefly:

* what changed
* what was tested
* any remaining limitation

---

# 27. Do Not Over-Engineer

Do not create abstractions simply because they sound architecturally impressive.

Every abstraction must have a reason.

Avoid:

* unnecessary factories
* unnecessary dependency injection layers
* excessive interfaces
* giant generic frameworks
* speculative plugin systems

Build what Teloude currently needs while keeping clear extension points.

---

# 28. Do Not Under-Engineer Critical Systems

The following areas deserve careful engineering:

* file transfers
* resume state
* database transactions
* restore operations
* Telegram authentication
* security
* filesystem handling
* crash recovery

Do not use quick hacks in these areas.

---

# 29. First Task

The first coding task is ONLY repository bootstrap.

Implement:

```text
project structure
pyproject.toml
package initialization
configuration skeleton
logging skeleton
SQLite connection
migration abstraction
minimal PySide6 application
basic Overview window
tests
.gitignore
README.md
```

The application should be able to:

```text
start
 ↓
load configuration
 ↓
initialize logging
 ↓
open SQLite database
 ↓
run database migration
 ↓
open PySide6 window
```

The first milestone must NOT implement:

* Telegram login
* Telegram API communication
* Telegram storage creation
* forum topics
* file upload
* file restore
* search
* preview
* duplicate detection

Those features come later.

---

# 30. First Task Completion Criteria

The first milestone is complete only when:

* the project imports successfully
* the application starts
* SQLite initializes
* migrations execute
* the main window opens
* configuration loads
* logging works
* basic tests pass
* the repository structure matches the specification
* no Telegram functionality has been prematurely added

Do not continue to Phase 1 automatically.

Stop after completing the bootstrap milestone and report the result.

---

# 31. Important Rule

When uncertain, prefer:

```text
safe
explicit
testable
maintainable
```

over:

```text
fast
clever
implicit
fragile
```

The user's files and backup integrity are more important than development speed.

---

# 32. Final Instruction

Treat `PROJECT_SPEC.md` as the product specification.

Treat this file as the engineering instructions.

When these files are updated intentionally, follow the latest version.

Do not silently rewrite either document.

Do not implement future phases before the current milestone is complete unless explicitly instructed.
