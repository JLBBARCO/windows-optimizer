# Windows Optimizer

Windows Optimizer is a Windows maintenance utility and companion website. The core application runs a curated list of maintenance commands with administrator privileges, while the website provides usage instructions, feature information, and links to published releases.

## Features

The current command set includes:

- Closing and restarting Windows Explorer to rebuild the icon cache.
- Removing the current user's temporary files and Explorer icon-cache files.
- Running System File Checker (`sfc /scannow`).
- Repairing the Windows component store with DISM.
- Flushing the DNS resolver cache.
- Checking and repairing the system disk with `chkdsk /f /r`.
- Updating installed applications with `winget update --all`.

The application also:

- Requests elevation through UAC when it is not already running as administrator.
- Executes CMD and PowerShell command groups independently.
- Supports per-command timeouts, piped input, and detached GUI processes.
- Continues with subsequent commands when an individual command fails or is interrupted.
- Writes application events to `log.log` and captured terminal output to `terminal.log`.

## Project Structure

```text
core-app/
main.py                 Application entry point
json/commands.json      CMD and PowerShell command definitions
lib/json/               JSON loading helpers
lib/log/                File and terminal logging helpers
run.ps1                 PowerShell launcher placeholder
build.bat               Windows build script placeholder
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
- Python 3.10 or later recommended.
- Administrator access. The application requests UAC elevation automatically.
- Python dependencies from `requirements.txt` when using helpers that access external JSON. Install them with:

```powershell
py -m pip install -r requirements.txt
```

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

Review the commands before running the optimizer. Several operations modify system state, including deleting cache files, repairing the disk, repairing Windows system files, and updating installed applications. `chkdsk /f /r` and DISM may take a long time and can require a restart or additional Windows interaction.

## Command Configuration

Commands are data-driven through `core-app/json/commands.json`. Each command can define:

- `command`: the command line to execute.
- `shell`: `cmd` or `powershell`; commands without this field use their group runner.
- `input`: optional data sent to standard input.
- `input_mode`: currently `pipe` is used for commands such as `chkdsk`.
- `timeout`: maximum execution time in seconds; the default is 3600.
- `description`: human-readable maintenance information.

The `cmd` and `powershell` arrays are processed in order. A command failure is logged and does not prevent the next command from running.

## Website and Releases

The static website is deployed from `website/`. Its release table calls `/api/releases`, which fetches all non-draft Releases from `JLBBARCO/windows-optimizer`, selects a portable archive or executable when available, and returns the data normalized for the table. The frontend sorts releases by publication date, newest first.

`website/vercel.json` schedules the endpoint for hourly execution with the `0 * * * *` cron expression. Vercel's project root must be set to `website` so that both the static files and the serverless function are deployed together.

## Current Build Status

`core-app/run.ps1` and `core-app/build.bat` are present but currently empty. Running the Python entry point directly is the supported local execution path until those scripts are implemented.

## Contributing

1. Update `core-app/json/commands.json` when adding or changing maintenance commands.
2. Keep commands ordered and provide a timeout and description for operations that may take time.
3. Test changes on a non-production Windows machine with administrator permissions.
4. Update this README when the command set, launch process, or deployment configuration changes.
