# Windows Optimizer

Windows Optimizer is a Windows maintenance utility and companion website. The core application runs a curated list of maintenance commands with administrator privileges, while the website provides usage instructions, feature information, and links to published releases.

## Features

The current command set includes:

- Flushing the DNS resolver cache.
- Removing the current user's temporary files, skipping reparse points, locked items and the application's own files.
- Repairing the Windows component store with DISM.
- Running System File Checker (`sfc /scannow`).
- Checking the system disk with an online scan (`chkdsk C: /scan /forceofflinefix`), which queues repairs for the next boot instead of waiting for an interactive answer.
- Updating installed applications with `winget upgrade --all`.
- Closing Windows Explorer, clearing the icon cache and restarting the shell (dedicated internal workflow that always runs at the end of the plan).

The application also:

- Requests elevation through UAC when it is not already running as administrator, waits for the elevated process, verifies the resulting token, and reports the exact reason when elevation does not produce administrator rights.
- Runs every command in an isolated child process with streamed output, a hard watchdog and a full process-tree kill on timeout, so one command can never block or abort the rest of the plan.
- Decodes child output adaptively (UTF-16 for `sfc`/`DISM`, OEM code page for `cmd` built-ins).
- Sends PowerShell commands through `-EncodedCommand`, removing every quoting and escaping problem.
- Exports `%WO_PROTECTED_PATHS%` so cleanup commands never delete the running source tree, the Python interpreter or the log folder.
- Supports per-command timeouts, acceptable non-zero exit codes, optional commands, stdin or piped input automation, and detached GUI processes.
- Writes application events to `log.log` and captured terminal output to `terminal.log`, and prints a per-command execution report at the end of every run.
- Shows a final Yes/No restart prompt and runs `shutdown.exe /r /f /t 30` when confirmed.
- Creates a Desktop shortcut (`lib/shortcut`) that runs the one-line PowerShell launcher, so every double click always executes the newest release of the selected channel.

### Log location

- Running from a checkout: `log.log` and `terminal.log` are written to the repository root.
- Running through `run.ps1`: they are written to `%LOCALAPPDATA%\windows-optimizer\logs`, because the staging folder with the release sources is deleted when the run ends.
- `%WO_LOG_DIR%` overrides the destination folder (`run.ps1` exports it, and the elevated child inherits it).

## Project Structure

```text
core-app/
main.py                 Application entry point
json/commands.json      CMD and PowerShell command definitions
lib/json/               JSON loading helpers and plan lookup
lib/log/                File and terminal logging helpers
lib/runner/             Command normalization and supervised execution engine
lib/shortcut/           Desktop shortcut for the one-line PowerShell launcher
assets/icons/icon.ico   Application icon used by the Desktop shortcut
lib/system/             Elevation, process checks, self-protection and tool discovery
tests_local.py          Local sanity tests for the execution engine
run.ps1                 PowerShell launcher that runs the latest release directly from source
website/
index.html              Public website
css/style.css           Website styles
js/                     Header, footer, clipboard, and release scripts
api/releases.js         Vercel function that reads GitHub Releases
vercel.json              Hourly release refresh schedule
```

## Requirements

### Core application

- Windows 10 or later.
- **Python 3.10 or later is mandatory**: the application is never compiled, it always runs on the interpreter. Install it with `winget install --id Python.Python.3.12 --source winget` or from [python.org](https://www.python.org/downloads/windows/).
- Administrator access. The application requests UAC elevation automatically.
- No dependency needs to be installed to run the maintenance plan: the code uses only the standard library. `requirements.txt` lists optional packages used by helpers that read external JSON:

```powershell
py -m pip install -r requirements.txt
```

### Releases

The `Publish Core App Release` workflow (`.github/workflows/release-core-app.yml`) only resolves the version in `.github/version.json` and publishes the release (`main`) or pre-release (`beta`). It builds nothing: `run.ps1` downloads the source archive of the published tag. Releases must keep being published, because the launcher resolves the tag through the GitHub API.

### Website

- A Vercel project configured with `website` as its root directory.
- Internet access for GitHub Releases, Font Awesome, Google Fonts, and the contact JSON used by the footer.

## Run the Core Application

From the repository root, run:

```powershell
py .\core-app\main.py
```

The application reads `core-app/json/commands.json` relative to its own location, so it can also be started from inside `core-app`:

```powershell
cd .\core-app
py .\main.py
```

The public website exposes these PowerShell commands:

```powershell
# Stable branch
irm https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/main/core-app/run.ps1 | iex

# Beta branch
irm https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/beta/core-app/run.ps1 | iex
```

The branch inside that URL selects the channel, and `run.ps1` resolves the corresponding GitHub Release:

| Branch in the URL | Release used by the launcher |
| --- | --- |
| `main` | newest published release (`prerelease = false`) |
| `beta` | newest published pre-release (`prerelease = true`) |

Because the script is piped into `iex` it has no path of its own, so the branch is detected from the first source that can prove it: `-Branch`, `%WO_BRANCH%`, the invocation line, the command line of the current PowerShell process (this is what makes the Desktop shortcut work), the session history, and finally a SHA-256 fingerprint of the running script compared with the raw `run.ps1` of both branches. When nothing proves the channel, `main` is used.

### Direct execution from the source code

The application is not compiled and no release carries an `.exe` asset. `run.ps1`:

1. resolves the release of the channel through the GitHub API;
2. downloads the source archive of that exact tag **into memory**;
3. expands only `core-app/` into `%LOCALAPPDATA%\windows-optimizer\src\<tag>` (never `%TEMP%`, which the maintenance plan cleans);
4. runs `python core-app\main.py` in the current console and returns its exit code;
5. deletes the staging folder when the run ends.

This requires a Python 3.10+ interpreter (`py -3`, `python` or `python3`) on `PATH`. When none is found the launcher stops with exit code `1` and prints the installation command; there is no executable fallback. Launcher switches:

- `-Branch main|beta`: force the channel.
- any remaining arguments are forwarded to the application (for example `--simulate`).

Switches only work when the script is invoked as a script or scriptblock, for example:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/main/core-app/run.ps1))) -Branch beta --simulate
```

`%WO_BRANCH%` (`main`/`beta`) and `%WO_CHANNEL%` (`release`/`pre-release`) work with the plain one-liner as well.

### Desktop shortcut

`core-app/lib/shortcut/__init__.py` creates a Desktop shortcut whose target is Windows PowerShell and whose arguments are exactly the one-liner above:

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/<branch>/core-app/run.ps1 | iex"
```

- The shortcut never points to a local copy of the application, so it always runs the newest release of its channel.
- `main` creates `Windows Optimizer.lnk`, `beta` creates `Windows Optimizer (Beta).lnk`, and the channel comes from `%WO_BRANCH%`/`%WO_CHANNEL%` (exported by `run.ps1`) or from `--shortcut-branch`.
- The Desktop folder is resolved with `SHGetKnownFolderPath`, so OneDrive Known Folder Move is handled correctly.
- The `.lnk` is written through the `WScript.Shell` COM object driven by a `-EncodedCommand` PowerShell child process: no `pywin32` dependency and no quoting problems.
- The shortcut is created by the **non elevated** parent process, because the elevated child may belong to another user and therefore to another Desktop.
- The icon is `core-app/assets/icons/icon.ico` (generated from `src/favicon/favicon.svg`, sizes 16 to 256). Because `run.ps1` deletes its staging folder at the end of every run, the file is cached in `%LOCALAPPDATA%\windows-optimizer\icon.ico` and the `.lnk` points there; `%WO_ICON%` overrides it and the PowerShell icon is still the last-resort fallback. An existing shortcut created with the old icon is repointed automatically on the next run.

Manual usage:

```powershell
py .\core-app\main.py --create-shortcut               # create or refresh it, then exit
py .\core-app\main.py --create-shortcut --shortcut-branch beta
py .\core-app\main.py --remove-shortcut               # delete it, then exit
py .\core-app\main.py --no-shortcut                   # normal run without touching it
cd .\core-app; py -m lib.shortcut --branch beta --force
```

Review the commands before running the optimizer. Several operations modify system state, including deleting cache files, repairing the disk, repairing Windows system files, and updating installed applications. `chkdsk` and DISM may take a long time and can require a restart or additional Windows interaction.

When all commands complete, the app displays a restart confirmation dialog in English:

- `Yes`: schedules restart with `shutdown.exe /r /f /t 30`.
- `No`: exits normally without restarting.

## Command Configuration

Commands are data-driven through `core-app/json/commands.json`. The `plan` array is executed in order; the legacy `cmd` and `powershell` arrays are still accepted. Each command can define:

- `name`: friendly label used in the logs and in the final report.
- `command`: the command line to execute. Placeholders `{winget}`, `{system_drive}`, `{windir}`, `{temp}` and `{log_dir}` are resolved at runtime.
- `shell`: `cmd` or `powershell`; commands without this field use their group name.
- `input`: optional data sent to standard input.
- `input_mode`: `stdin` (default) or `pipe`.
- `timeout`: maximum execution time in seconds; the default is 3600.
- `optional`: when `true`, a failure or timeout is logged as a warning and never marks the run as failed.
- `accepted_exit_codes`: non-zero exit codes treated as success (decimal or hexadecimal).
- `enabled`: set to `false` to keep an entry documented without running it.
- `detached`: `true` for fire-and-forget GUI launches.
- `description`: human-readable maintenance information.

Command line flags:

- `--simulate` (or `--dry-run`): resolve and report the plan without executing anything.
- `--no-explorer`: skip the Explorer icon-cache workflow.
- `--no-restart-prompt`: never show the restart dialog.
- `--only <text>`: run only commands whose name or command line contains `<text>`.
- `--log-dir <path>`: write `log.log` and `terminal.log` into `<path>`.
- `--diagnose`: print the UAC/token diagnosis and exit without running any command.
- `--no-elevate`: never request UAC (same as setting `%WO_NO_ELEVATE%`).
- `--elevated`: internal flag used when relaunching through UAC.
- `--create-shortcut`: create or refresh the Desktop shortcut and exit.
- `--remove-shortcut`: delete the Desktop shortcut and exit.
- `--no-shortcut`: do not create the Desktop shortcut during a normal run.
- `--shortcut-branch <main|beta>`: channel used by the shortcut; defaults to `%WO_BRANCH%`, then `main`.

## Administrator Privileges and UAC

Accepting the UAC prompt does not guarantee that the new process received an elevated token, so the application no longer assumes it:

- On every start the real token state is logged: user, `TokenElevation`, `TokenElevationType` (1 = no split token, 2 = full/elevated, 3 = filtered admin token), the integrity level (`0x3000` = high), the Administrators group membership of the effective token and, when a query fails, the Win32 error code (`token_query_error`).
- Every `advapi32`/`kernel32` call used for this has an explicit `ctypes` prototype. Without them the 64-bit handles were truncated, `GetTokenInformation` failed and the log printed `elevation_type=None | integrity=unknown`.
- When the token state cannot be read, elevation is **requested anyway**: the UAC dialog is the only reliable authority. Giving up in this situation was what made DISM fail with error `740` and SFC with error `1` while no prompt was ever shown.
- Elevation uses `ShellExecuteExW` with `SEE_MASK_NOCLOSEPROCESS`, so the parent **waits** for the elevated child and returns its exit code. Previously the parent logged `Request administrator privileges` and ended immediately, which looked like a failure even when the child was working correctly.
- The log folder is shared by both processes through `--log-dir` and `%WO_LOG_DIR%`, and every line carries the process id, so an interleaved parent/child log stays readable.
- A refused prompt is detected through `ERROR_CANCELLED` (1223) and reported as a denial with exit code 4, instead of silently continuing.
- The `--elevated` flag is never requested twice: if the relaunched process is still not elevated, the log explains the likely cause (standard user account, token filtered by policy, or a different user session) instead of prompting again.
- Only one instance can run at a time. A lock file in the log folder (with a stale-PID check) prevents two overlapping runs from fighting over `%TEMP%`, Explorer and the log files. A second instance exits with code 3.

Exit codes: `0` success, `1` startup error, `2` at least one command failed, `3` another instance is running, `4` UAC denied.

To collect the diagnosis without changing anything:

```powershell
py .\core-app\main.py --diagnose
```

Explorer shell maintenance (stop Explorer, clear icon cache files, and start Explorer again) is handled by a dedicated internal workflow in `core-app/main.py` with restart validation and fallback strategies. These Explorer-specific steps are no longer required in `commands.json`.

For interactive commands, provide `input` in the command item. The app automatically sends this input so the command can continue unattended. Prefer non-interactive alternatives when they exist: a piped answer such as `Y` depends on the Windows display language, which is why `chkdsk /f /r` was replaced by `chkdsk C: /scan /forceofflinefix`.

Cleanup commands must honour `%WO_PROTECTED_PATHS%` (a `;` separated list). Deleting everything inside `%TEMP%` without that filter can destroy files in use and kill the process in the middle of the plan.

Every command is processed in order and isolated: a failure, a timeout or an interruption is logged and does not prevent the next command from running.

## Website and Releases

The static website is deployed from `website/`. Its release table calls `/api/releases`, which fetches all non-draft Releases from `JLBBARCO/windows-optimizer` and returns the data normalized for the table: since there are no binary assets, the link points to the release page (the source archive is exposed as `source_zip`). The frontend sorts releases by publication date, newest first.

`website/vercel.json` schedules the endpoint for hourly execution with the `0 * * * *` cron expression. Vercel's project root must be set to `website` so that both the static files and the serverless function are deployed together.

## Local Tests

`core-app/tests_local.py` validates the plan normalization and the supervision logic (streamed output, accepted exit codes, watchdog and process kill, stdin feeding, adaptive decoding). It runs on Windows and on POSIX systems:

```powershell
py .\core-app\tests_local.py
```

## Contributing

1. Update `core-app/json/commands.json` when adding or changing maintenance commands.
2. Keep commands ordered and provide a timeout and description for operations that may take time.
3. Test changes on a non-production Windows machine with administrator permissions.
4. Update this README when the command set, launch process, or deployment configuration changes.
