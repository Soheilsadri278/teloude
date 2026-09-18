# Teloude Project Specification

**Version:** 1.0
**Status:** Development
**Platform:** Windows 10/11
**Project Type:** Open-source desktop backup application

---

## 1. Project Overview

Teloude is a professional Windows desktop application for backing up and restoring files using the user's own Telegram account as cloud storage.

Teloude communicates with Telegram through the MTProto API, not the Telegram Bot API.

The application is designed primarily for personal backup.

### Primary operations

1. Local files/folders → Telegram backup
2. Telegram backup → Local restore

Teloude is a **backup application, not a synchronization application**.

The application must never automatically delete, move, rename, or modify local files.

---

# 2. Core Product Principles

The following principles are mandatory:

* Never automatically delete local files.
* Never automatically move local files.
* Never automatically rename local files.
* Never modify the contents of local files.
* Upload one logical file at a time.
* Large files must be transferred without loading the entire file into RAM.
* Transfers must support pause/resume.
* Transfer state must survive application restart.
* Temporary network failures must be recoverable.
* Telegram communication must be isolated behind a dedicated abstraction layer.
* UI code must not contain Telegram protocol logic.
* Database access must be isolated from UI code.
* Destructive cloud operations require explicit user confirmation.
* Telegram limits must be detected dynamically whenever possible.
* The application must remain usable when Telegram is temporarily unavailable.

---

# 3. Target Platform

Initial target:

* Windows 10
* Windows 11
* 64-bit

The first release should be distributed as a standalone Windows application.

End users should not need:

* Python
* pip
* a compiler
* a terminal
* development dependencies

The final application should be packaged as an installer.

---

# 4. Technology Stack

Preferred stack:

* Python 3.x
* PySide6 / Qt
* Telethon or another mature MTProto client
* SQLite
* Windows DPAPI / Windows Credential Manager
* PyInstaller or Nuitka
* Inno Setup for Windows installer

The Telegram implementation must be isolated behind application-level interfaces so the rest of the application does not depend directly on a specific Telegram library.

---

# 5. High-Level Architecture

The application should be divided into the following layers:

```text
UI
 ↓
Application Services
 ↓
Core Domain
 ↓
Infrastructure
 ├── Telegram
 ├── Database
 ├── Security
 ├── Filesystem
 └── Configuration
```

## UI Layer

Responsible for:

* Windows
* dialogs
* buttons
* progress indicators
* lists
* navigation
* previews
* notifications
* system tray

The UI must not implement:

* Telegram API calls
* database SQL logic
* upload algorithms
* restore algorithms
* authentication protocol logic

---

## Application Layer

Responsible for coordinating operations such as:

* starting a backup
* pausing a backup
* resuming a backup
* restoring files
* searching
* creating storage
* discovering existing storage
* handling application-level workflows

---

## Core Layer

Contains business logic independent from Qt and Telegram.

Examples:

```text
BackupManager
RestoreManager
TransferManager
DuplicateDetector
FileScanner
SearchService
PreviewService
StorageManager
```

---

## Infrastructure Layer

Contains implementations for:

```text
Telegram
SQLite
Filesystem
Windows security
Configuration
Logging
```

---

# 6. Repository Structure

The project should follow this structure:

```text
Teloude/
│
├── app/
│   ├── core/
│   │   ├── backup/
│   │   ├── restore/
│   │   ├── scanner/
│   │   ├── transfer/
│   │   └── preview/
│   │
│   ├── telegram/
│   │   ├── auth/
│   │   ├── client/
│   │   ├── storage/
│   │   ├── topics/
│   │   └── files/
│   │
│   ├── database/
│   ├── security/
│   ├── config/
│   └── application/
│
├── ui/
│   ├── windows/
│   ├── views/
│   ├── components/
│   ├── dialogs/
│   ├── models/
│   └── styles/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── docs/
├── installer/
├── scripts/
│
├── PROJECT_SPEC.md
├── AGENTS.md
├── README.md
├── pyproject.toml
└── .gitignore
```

Do not create one giant Python file containing the entire application.

---

# 7. Telegram Storage Architecture

Teloude uses the user's personal Telegram account.

Each Teloude storage is represented by a separate private Telegram supergroup configured as a forum.

Example:

```text
Teloude - Photography
Teloude - Documents
Teloude - Projects
Teloude - Archive
```

The storage must be private.

The application automatically creates the storage when requested by the user.

---

# 8. Forum Topic Structure

Telegram forum topics are flat and cannot contain nested forum topics.

Therefore Teloude must emulate folder hierarchy.

Example local folder:

```text
Photography/
├── 2026/
│   ├── Wedding/
│   │   ├── IMG001.RAW
│   │   └── IMG002.RAW
│   │
│   └── Travel/
│       └── IMG003.JPG
```

Telegram topics may be represented as:

```text
Photography
2026
2026 / Wedding
2026 / Travel
```

The actual folder hierarchy must be stored in the local Teloude database.

Telegram topic names are therefore a presentation/storage mapping, not the authoritative filesystem hierarchy.

---

# 9. Storage Creation Workflow

When the user creates a storage:

1. Ask for storage name.
2. Create a private Telegram supergroup.
3. Convert/configure it as a forum.
4. Ensure Teloude has the required topic-management permissions.
5. Create the root topic.
6. Save Telegram identifiers in the local database.
7. Refresh storage metadata.
8. Show the storage in the Teloude UI.

Example:

```text
Create Storage

Name:
[ Photography ]

[ Create Storage ]
```

---

# 10. Storage Dashboard

Each storage should display useful information such as:

* Storage name
* Total backed-up size
* Number of files
* Number of folders
* Last backup
* Current transfer state
* Telegram connection state

Example:

```text
Photography

1,284 files
184.6 GB
42 folders

Last backup:
Today, 18:42

Status:
Up to date
```

---

# 11. Authentication

Teloude uses Telegram MTProto authentication.

Expected first-run flow:

```text
Open Teloude
     ↓
Connect Telegram
     ↓
Enter phone number
     ↓
Telegram verification code
     ↓
2FA password if enabled
     ↓
Connected
```

The user should not normally be required to manually enter Telegram API credentials.

Teloude must use its own properly registered Telegram API credentials.

API credentials must never be written to logs.

Authentication codes and 2FA passwords must never be written to logs.

The Telegram session must be stored securely using Windows security facilities such as DPAPI or Credential Manager.

If a session becomes invalid, Teloude must provide a clear re-authentication flow.

---

# 12. Telegram API Compliance

Teloude must use the official Telegram API/MTProto ecosystem correctly.

The application must:

* use its own registered API credentials
* clearly identify itself as using Telegram
* avoid abusive request patterns
* respect Telegram limits
* handle FloodWait and rate-limit errors
* avoid unnecessary API calls
* avoid spam-like behavior
* never attempt to bypass Telegram restrictions

The implementation must be checked against current official Telegram documentation when Telegram-specific behavior is implemented.

---

# 13. File Upload

The upload system must support arbitrary file types.

Examples:

```text
.jpg
.jpeg
.png
.webp
.raw
.cr2
.nef
.mp4
.mkv
.mov
.pdf
.zip
.7z
.exe
.iso
.psd
.docx
.xlsx
```

The application must not assume a specific MIME type or extension.

---

# 14. Large File Handling

Large files must be transferred using Telegram's MTProto file-upload mechanisms.

The application must:

* split files into appropriate parts
* avoid loading entire files into RAM
* support large files up to Telegram's currently permitted limit
* determine Telegram limits dynamically when possible
* handle Telegram errors cleanly
* maintain transfer state

Do not permanently hard-code a historical Telegram file-size limit.

---

# 15. Upload Performance

Telegram file uploads should use efficient part handling and multiple connections/requests where appropriate.

However, correctness is more important than maximum throughput.

The implementation must avoid:

* uncontrolled concurrency
* excessive memory usage
* unnecessary API requests
* event-loop blocking
* UI freezing

---

# 16. One Logical File at a Time

Teloude should process one logical file at a time.

This means:

```text
file A
████████████████████

file B
pending

file C
pending
```

Internal chunk-level parallelism is allowed for performance.

Multiple independent files should not be uploaded simultaneously unless the architecture explicitly changes this requirement in a future version.

---

# 17. Pause / Resume

Transfers must support:

* Pause
* Resume
* Cancel

Pause should stop the transfer as quickly as safely possible.

The application must persist enough information to recover transfer state after:

* application restart
* system restart
* temporary network loss
* application crash

---

# 18. Resume Semantics

For each active transfer, store information such as:

```text
file identity
storage ID
folder ID
topic ID
relative path
file size
uploaded bytes
transfer status
Telegram identifiers
timestamps
last error
```

The application should attempt to continue from the last valid transfer state.

Telegram's temporary upload-part state may expire.

Therefore Teloude must not promise indefinite server-side resume.

If Telegram's temporary upload state is no longer valid, Teloude may need to restart the current file.

The user should not lose already completed files.

---

# 19. Crash Recovery

After restarting the application:

1. Load incomplete transfers.
2. Verify the local source file still exists.
3. Verify that the file has not unexpectedly changed.
4. Check whether Telegram-side transfer state can continue.
5. Resume if possible.
6. Restart the current file if necessary.
7. Continue with remaining files.

The application must never silently mark an incomplete file as successfully backed up.

---

# 20. Local File Safety

Teloude is a backup application.

It must never automatically:

* delete source files
* move source files
* rename source files
* modify source files

The source filesystem is treated as user-owned data.

Cloud cleanup must never imply local cleanup.

---

# 21. Duplicate Detection

Teloude should detect duplicate files.

The primary content identity mechanism should be SHA-256.

For performance, the application may first use a cheaper fingerprint such as:

```text
file size
modified time
partial hash
```

before calculating a full SHA-256 hash.

The UI must provide duplicate handling options:

```text
Skip
Upload Again
Cancel
```

There must also be:

```text
Apply to all
```

for the current operation.

The user must never be forced to overwrite local files automatically.

---

# 22. Rename Behavior

Local rename operations do not need to rename the corresponding Telegram file.

Teloude's database should preserve the relationship between the local file record and its backup metadata.

A later backup can detect that a file has been renamed or moved if the application can establish the relationship.

However, relocation tracking is not a mandatory v1 feature.

---

# 23. Cloud Deletion

Deleting a backed-up file from Telegram is allowed only through an explicit user action.

Before destructive operations, show a confirmation dialog.

Example:

```text
Delete cloud backup?

This will remove the Telegram backup.

Your local file will NOT be deleted.

[ Cancel ] [ Delete Backup ]
```

Never silently delete Telegram messages/files.

---

# 24. Restore

Restore must support:

* individual file
* multiple selected files
* folder
* complete storage

The user selects a local destination.

Teloude must recreate the folder hierarchy where appropriate.

Restore collision options should include:

```text
Skip
Overwrite
Keep both
Cancel
```

The exact UI may evolve, but destructive local overwrite must always be explicit.

---

# 25. Restore Integrity

After downloading a file, Teloude should verify integrity when sufficient metadata is available.

The application should be able to compare:

```text
expected size
actual size
expected hash
actual hash
```

A failed verification must not be reported as a successful restore.

---

# 26. Search

Teloude must provide global search.

Search should work across all known storages.

Searchable fields should include:

* filename
* relative path
* storage
* topic
* file type
* status

Search results should show information such as:

```text
Filename
Storage
Path
Size
Backup date
Status
```

Possible actions:

* Reveal local file
* Restore
* Locate in Teloude
* Open storage

A complete Telegram file browser is not required for v1.

---

# 27. Preview

Preview functionality is required.

At minimum:

### Images

Support common image formats.

### RAW images

Where a RAW file contains an embedded JPEG preview, use that preview when possible.

The application should not require full RAW decoding merely to display a thumbnail.

### Video

Generate or extract a thumbnail asynchronously.

Preview failures must never cause backup failures.

Preview generation must not block the UI.

---

# 28. Transfer Speed Limits

Users should be able to select:

```text
Unlimited
10 MB/s
5 MB/s
2 MB/s
Custom
```

The transfer throttling implementation must not block the UI.

The rate limiter should work cooperatively with the transfer engine.

---

# 29. System Tray

Closing the main window should not necessarily terminate the application.

Default behavior:

```text
Close window
     ↓
Minimize to system tray
     ↓
Backup continues
```

Tray menu:

```text
Open Teloude
Pause Backup
Resume Backup
View Progress
Exit
```

When the user chooses Exit, the application must safely stop active operations before terminating.

---

# 30. Database

Use SQLite.

The database should be hidden from normal users and treated as an internal implementation detail.

Database access must be centralized.

Use migrations.

Do not scatter raw SQL throughout UI classes.

Conceptual tables:

```text
storages
folders
files
telegram_messages
transfers
settings
schema_migrations
```

The exact schema may evolve.

---

# 31. Database Responsibilities

The database should store:

* storage metadata
* Telegram IDs
* folder hierarchy
* local file metadata
* backup relationships
* Telegram message relationships
* transfer state
* application settings
* schema version

The database is a local index/cache of the Telegram storage.

Telegram remains the remote source of backed-up data.

---

# 32. Telegram Message Metadata

Do not depend on Bot API-specific identifiers such as Bot API `file_id`.

Store the identifiers and metadata appropriate to MTProto.

Telegram message metadata should be treated as authoritative for locating backed-up files.

File references may expire and may need to be refreshed before downloading.

---

# 33. Multi-Computer Behavior

A user may install Teloude on another Windows PC and log into the same Telegram account.

The application should discover existing Teloude storages.

The local SQLite database is not automatically shared between computers.

Therefore the application must be capable of rebuilding or refreshing its local index from Telegram storage metadata when required.

---

# 34. Storage Selection

The user may have multiple storages.

Example:

```text
My Storages

Photography
Documents
Projects
Archive
```

During backup the user selects the destination storage.

There is no artificial limit on the number of storages imposed by Teloude.

---

# 35. Backup Flow

Typical backup flow:

```text
Select folder
     ↓
Scan filesystem
     ↓
Build file list
     ↓
Detect duplicates
     ↓
Create/update folder mapping
     ↓
Create required Telegram topics
     ↓
Upload files
     ↓
Persist transfer state
     ↓
Verify
     ↓
Mark backed up
```

The UI should clearly display progress.

Example:

```text
Backing up

Wedding / IMG_2841.CR3

████████████████░░░░  82%

1.84 GB / 2.24 GB

18.2 MB/s

[ Pause ] [ Cancel ]
```

---

# 36. Network Recovery

If the internet connection is lost:

```text
Uploading
     ↓
Connection lost
     ↓
Waiting for connection
     ↓
Connection restored
     ↓
Resume
```

The application should not immediately fail the entire backup because of a temporary network outage.

---

# 37. Error Handling

Errors must be:

* understandable
* actionable
* logged safely
* associated with the affected operation

Avoid exposing raw exceptions directly to normal users.

Example:

Bad:

```text
RPCError 420 FLOOD_WAIT_123
```

Better:

```text
Telegram temporarily limited this operation.

Teloude will retry automatically in about 2 minutes.
```

Technical details may still be available in a diagnostic/log view.

---

# 38. Logging

Use structured application logging.

Logs must never contain:

* Telegram authentication codes
* Telegram 2FA passwords
* API secrets
* session secrets
* sensitive file contents

Logs should contain enough information to diagnose:

* upload failures
* restore failures
* network errors
* database errors
* unexpected application states

---

# 39. Security

Security requirements:

* protect Telegram session credentials
* use Windows DPAPI or Credential Manager where appropriate
* never store authentication secrets in plaintext configuration
* never log credentials
* validate filesystem paths
* avoid unsafe path traversal during restore
* protect against accidental destructive operations

No additional client-side encryption-at-rest layer is required for v1.

Telegram's private storage is considered the cloud backup destination.

---

# 40. UI Principles

The UI should feel like a professional backup application.

Avoid:

* developer-style interfaces
* terminal-like UI
* unnecessary technical terminology
* clutter
* giant configuration screens

The main navigation should make common actions obvious.

Suggested sections:

```text
Overview
Storages
Backup
Restore
Search
Transfers
Settings
```

---

# 41. Overview Dashboard

The main screen should show:

```text
Teloude

Storage
────────────────────────
Photography       184 GB
Documents          42 GB
Projects          112 GB

Recent Activity
────────────────────────
IMG_2841.CR3       Complete
video.mp4          Uploading
document.pdf       Complete

[ Backup ] [ Restore ]
```

The exact visual design can evolve.

---

# 42. Responsiveness

The UI must remain responsive during:

* filesystem scanning
* hashing
* uploading
* downloading
* preview generation
* database operations
* Telegram requests

Long-running operations must not execute directly on the Qt UI thread.

---

# 43. Concurrency Model

Choose a consistent async/concurrency architecture.

The implementation may use:

* asyncio
* worker threads
* Qt signals
* background workers

but must avoid mixing concurrency models unnecessarily.

The chosen architecture must ensure:

* UI remains responsive
* Telegram client remains safe
* transfers can be paused
* cancellation is cooperative
* shutdown is predictable

---

# 44. Testing

Testing is mandatory.

At minimum:

### Unit tests

Test:

* configuration
* database migrations
* file scanning
* duplicate detection
* path handling
* transfer state machine
* speed limiter
* folder hierarchy mapping

### Integration tests

Test:

* Telegram abstraction
* storage creation
* topic creation
* upload
* download
* restore
* resume behavior

Telegram integration tests should be isolated and should not run unintentionally during normal unit-test execution.

---

# 45. Git Discipline

The repository must remain understandable.

Prefer commits such as:

```text
bootstrap project structure
add configuration system
add database layer
add telegram client abstraction
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

Do not mix unrelated changes into one massive commit.

---

# 46. Development Phases

Implementation should proceed incrementally.

## Phase 0: Repository Bootstrap

Implement only:

* project directories
* `pyproject.toml`
* package initialization
* configuration skeleton
* logging skeleton
* SQLite connection
* migration abstraction
* minimal PySide6 application
* basic Overview window
* `.gitignore`
* README
* tests for configuration/database initialization

Do NOT implement Telegram functionality yet.

---

## Phase 1: Telegram Foundation

Implement:

* Telegram client abstraction
* authentication
* secure session storage
* connection state
* reconnect handling

---

## Phase 2: Storage Management

Implement:

* storage creation
* private supergroup creation
* forum configuration
* root topic
* storage discovery
* storage statistics

---

## Phase 3: Filesystem Scanner

Implement:

* folder scanning
* file metadata
* hierarchy mapping
* duplicate detection
* hashing

---

## Phase 4: Upload Engine

Implement:

* MTProto upload
* chunking
* progress
* speed limits
* pause
* cancel
* retry
* persistent transfer state

---

## Phase 5: Resume and Recovery

Implement:

* restart recovery
* network recovery
* Telegram temporary-state handling
* integrity verification

---

## Phase 6: Restore

Implement:

* file restore
* folder restore
* full storage restore
* collision handling
* integrity verification

---

## Phase 7: Search and Preview

Implement:

* global search
* image previews
* RAW embedded JPEG previews
* video thumbnails

---

## Phase 8: System Tray and UX

Implement:

* system tray
* notifications
* polished dialogs
* transfer center
* improved dashboard

---

## Phase 9: Packaging

Implement:

* production configuration
* standalone executable
* Windows installer
* application icon
* upgrade handling
* uninstall behavior

---

## Phase 10: Release Hardening

Perform:

* security review
* crash testing
* interrupted-transfer testing
* network failure testing
* large-file testing
* database migration testing
* multi-computer testing
* installer testing
* documentation

---

# 47. Version 1 Definition of Done

Version 1 is complete when a user can:

1. Install Teloude on Windows.
2. Sign in with Telegram.
3. Create a Teloude storage.
4. Select a local folder.
5. Back up arbitrary files.
6. See progress.
7. Pause/resume.
8. Recover after application restart.
9. Recover after temporary network loss.
10. Detect duplicates.
11. Search backed-up files.
12. Restore files/folders.
13. Preview supported media.
14. Manage multiple storages.
15. Use the application from the system tray.
16. Uninstall Teloude safely.

---

# 48. Non-Goals for Version 1

Do not add these unless explicitly requested later:

* two-way synchronization
* automatic local deletion
* automatic local file modification
* scheduler
* mobile application
* macOS support
* Linux support
* Telegram Bot API backend
* public Telegram file browser
* nested Telegram forum topics
* mandatory client-side encryption layer
* unnecessary cloud providers

---

# 49. Engineering Rules

When implementing features:

1. Read this specification first.
2. Inspect the existing repository before changing architecture.
3. Prefer small modules.
4. Keep UI and business logic separate.
5. Keep Telegram behind an abstraction.
6. Persist important state.
7. Never silently discard user data.
8. Never silently perform destructive operations.
9. Handle failures explicitly.
10. Write tests for important business logic.
11. Avoid unnecessary dependencies.
12. Do not invent Telegram API behavior.
13. Verify Telegram-specific behavior against current official documentation.
14. Do not hard-code temporary Telegram limits when they can be queried dynamically.
15. Keep the application maintainable by another developer.

---

# 50. First Implementation Target

The first implementation milestone is intentionally small.

After the first milestone:

```text
Teloude starts
        ↓
Configuration loads
        ↓
Logging initializes
        ↓
SQLite initializes
        ↓
Database migration runs
        ↓
PySide6 main window opens
```

The application should display a basic Overview screen.

Telegram authentication, storage creation, upload, restore, search, and preview must NOT be implemented during this first milestone.

The goal is to establish a clean and testable foundation before adding Telegram functionality.
