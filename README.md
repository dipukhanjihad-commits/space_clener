# C: Starter Cleaner

A small, reviewable Windows utility that frees space on `C:` and can silently
clean a curated set of safe items a couple of minutes after every logon.

The point of this tool is not "delete everything" — it's **show you exactly
what it will remove, how much space it will free, and let you decide which
items are allowed to clean themselves at startup**. Everything else requires
a click.
┌──────────────────────────────────────────────────────────────┐
│ C: Starter Cleaner [System][Light][Dark] │
│ │
│ SPACE TO BE FREED │
│ 1.84 GB 3 items selected │
│ │
│ [ Scan ] [ Clean Selected ] ⚡ = auto at startup │
│ │
│ SYSTEM │
│ ☑ ⚡ User temp files 412 MB │
│ ☑ ⚡ Windows temp files (admin) 0 B │
│ ☑ ⚡ Windows Error Reporting (user) 8 MB │
│ … │
│ │
│ [ Start with Windows ] [ Auto-clean ⚡ items at startup ] │
│ Ready. │
└──────────────────────────────────────────────────────────────┘

text

## Features

- **Reviewable before deletion.** Every category is a checkbox with a live
  size. A running total shows exactly what you're about to free.
- **Tier-based safety.** Only items in the auto tier (see below) are eligible
  for silent startup cleanup. Everything else is manual, always.
- **Modern UI.** Rounded cards, accent colours, light / dark / system theme.
  Theme choice is remembered.
- **Tray icon.** Closing the window minimizes to tray. Right-click for
  Scan / Clean / Run auto-clean now / Quit.
- **Start-with-Windows.** Adds a tiny `.vbs` launcher to `shell:startup` so no
  console window flashes at logon.
- **Delayed auto-clean.** Waits 2.5 minutes after logon so logon churn is over.
  Runs at most once per boot.
- **Dry-run by default.** The first 3 startup runs are log-only. Check the log,
  then let it go live.
- **Process gates.** Skips a browser's cache if that browser is running — never
  force-kills anything.
- **Recent-file skip.** Temp folders skip files modified in the last 24 hours
  so an installer that's mid-run isn't broken.
- **Per-run cap.** Any category that would delete >20 GB in one pass is aborted
  and logged.
- **Failure auto-disable.** A category that fails on >20 % of files is disabled
  on future auto-runs.
- **Rotating log.** `%LOCALAPPDATA%\CStarterCleaner\cleanup.log`, 1 MB × 4.

## Safety model

The tool groups every cleanable location into three tiers. Only Tier A is
selectable for startup auto-clean.

### Tier A — safe to auto-delete

| Item                          | Needs admin | Gate                     |
|-------------------------------|-------------|--------------------------|
| User temp (`%TEMP%`)          | no          | skip files < 24 h old    |
| Windows temp                  | yes         | skip files < 24 h old    |
| WER queue (user + system)     | partial     | none                     |
| Crash dumps / minidumps       | partial     | none                     |
| Edge / Chrome cache           | no          | skip if browser running  |
| Firefox cache + startup cache | no          | skip if Firefox running  |

### Tier B — listed but manual-only

Windows Update download cache, Delivery Optimization cache, Explorer thumbnail
cache, Explorer `INetCache`. These either need services stopped or are held by
`explorer.exe` — clean manually when convenient.

### Never touched

`Prefetch`, `WinSxS`, restore points, `Windows.old`, the registry, `Downloads`,
`Documents`, `Desktop`, `Pictures`, `Videos`, any `AppData` app-data folder,
`pagefile.sys`, `hiberfil.sys`.

## Install

Requires **Python 3.9+** on Windows.

```powershell
git clone <this repo> c-starter-cleaner
cd c-starter-cleaner
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python c_cleaner.py
No admin rights are needed to launch. Items marked (admin required) in the
UI are simply skipped when not elevated.

Usage
Launch with python c_cleaner.py.

Click Scan. Wait a few seconds (browser caches can take longer).

Tick items you want cleaned; tick ⚡ on any Tier A item you want cleaned
automatically at startup.

Click Clean Selected. A confirmation dialog appears. Deletion is
permanent — no Recycle Bin.

To enable startup cleanup, turn on Start with Windows and
Auto-clean ⚡ items at startup.

The first three startup runs are dry runs — they log what they would
have freed but delete nothing. Check cleanup.log before the fourth run. To
extend or skip the dry runs, edit dry_runs_remaining in the config file.

Command line
text
python c_cleaner.py                 # normal UI
python c_cleaner.py --startup       # delayed auto-clean, tray only
python c_cleaner.py --now           # run auto-clean immediately (debug)
python c_cleaner.py --no-tray       # disable tray icon
Configuration
Stored at %LOCALAPPDATA%\CStarterCleaner\config.json. Edit only when the app
is closed. All keys and defaults:

Key	Meaning
clean_selected	item IDs pre-ticked for manual cleaning
auto_selected	item IDs pre-ticked for auto-clean (Tier A only)
start_with_windows	bool — reflects presence of the .vbs in shell:startup
theme	"system", "light", or "dark"
dry_runs_remaining	number of startup runs that will log-only
last_auto_boot	boot timestamp of the last auto-clean (one run per boot)
last_auto_ts	wall-clock time of the last auto-clean (shown in UI)
last_auto_freed	bytes freed last time
disabled_after_errors	item IDs auto-disabled after excessive failures
The item IDs are stable strings (user_temp, edge_cache, etc.) defined in
ITEMS in c_cleaner.py.

Logs
%LOCALAPPDATA%\CStarterCleaner\cleanup.log — rotates at 1 MB, keeps 3
backups. Every scan, clean, gate-skip, abort and failure is logged with a
timestamp. Use this to verify auto-runs before trusting them.

Building a standalone .exe
powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed ^
    --name "CStarterCleaner" ^
    --collect-all customtkinter ^
    c_cleaner.py
The .exe lands in dist\. When the app detects it's frozen it uses
sys.executable in the startup .vbs — no Python install required at runtime.

Uninstall
In the app, turn off Start with Windows (this removes the .vbs).

Delete the folder %LOCALAPPDATA%\CStarterCleaner (config + logs).

Delete CStarterCleaner.vbs from
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup if it's still
there.

Delete the app folder.

Limitations & known gotchas
Locked files. Explorer, Defender, and indexer hold some files open. A
handful of files will fail to delete on every run — this is expected and
logged, not a bug.

Browser cache while the browser is open. Gated — the item is skipped, not
force-killed. Close the browser to clean it.

Not elevated. Admin items are skipped silently rather than triggering
a UAC prompt at logon. Run manually as admin when you want them cleaned.

Windows.old. Not handled by this tool. Use cleanmgr or Storage Sense.

Registry cleaning. Not implemented on purpose. No benefit, real risk.

Antivirus / EDR. Some corporate AVs flag the auto-clean VBS launcher.
If you're on a managed machine, use the tool manually instead.

Fonts on non-Windows. The UI assumes Segoe UI. On other platforms,
edit the FONT_* constants at the top of c_cleaner.py.

License
MIT. No warranty — the tool deletes files permanently. Review the log
during the dry-run period before enabling live auto-clean.