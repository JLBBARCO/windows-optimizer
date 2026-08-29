"""Desktop shortcut for the one-line PowerShell launcher.

What this module creates
-----------------------
A ``.lnk`` file on the user's Desktop whose target is Windows PowerShell and
whose argument list is exactly the documented one-liner::

    irm https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/<branch>/core-app/run.ps1 | iex

The shortcut therefore never points to a local copy of the application: every
double click fetches ``run.ps1`` again and that script resolves and runs the
newest release of the selected channel (``main`` -> latest release,
``beta`` -> latest pre-release).

Channel selection
-----------------
``resolve_branch()`` picks the branch in this order:

1. the explicit ``branch`` argument;
2. ``%WO_BRANCH%`` (exported by ``run.ps1`` before it starts the application, so
   a run started from the beta channel keeps producing a beta shortcut);
3. ``%WO_CHANNEL%`` (``release`` / ``pre-release``), used by the release
   workflow vocabulary;
4. ``main``.

Implementation notes
--------------------
* the Desktop folder is resolved through ``SHGetKnownFolderPath`` instead of
  ``%USERPROFILE%\\Desktop`` because OneDrive Known Folder Move relocates the
  Desktop and the naive path then points to an empty folder;
* the ``.lnk`` itself is written through the ``WScript.Shell`` COM object driven
  by a PowerShell child process sent with ``-EncodedCommand``. That keeps the
  module dependency free (no ``pywin32``/``comtypes``) and immune to quoting
  problems, exactly like ``lib/runner`` already does for PowerShell commands;
* ``run_as_admin`` patches bit ``0x20`` of the link flags byte at offset 21 of
  the ``.lnk`` binary format, which is what Explorer reads for the "Run as
  administrator" checkbox. It is off by default because the application already
  requests UAC by itself.

Usage
-----
From code::

    from lib import shortcut
    shortcut.ensure_shortcut()          # create only when missing
    shortcut.create_shortcut('beta')    # create/refresh the beta shortcut

From the command line (repository root, ``core-app`` as working directory)::

    py -m lib.shortcut --branch beta --force
"""

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:  # the module must stay importable when used as a standalone script
    from lib import log
except Exception:  # pragma: no cover - fallback used by ``py -m lib.shortcut``
    class _FallbackLog:
        @staticmethod
        def info(message):
            print(f'[INFO] {message}')

        @staticmethod
        def warning(message):
            print(f'[WARNING] {message}')

        @staticmethod
        def error(message):
            print(f'[ERROR] {message}')

    log = _FallbackLog()

IS_WINDOWS = os.name == 'nt'

REPOSITORY = 'JLBBARCO/windows-optimizer'
RAW_URL_TEMPLATE = (
    'https://raw.githubusercontent.com/' + REPOSITORY + '/{branch}/core-app/run.ps1'
)

DEFAULT_BRANCH = 'main'
SUPPORTED_BRANCHES = ('main', 'beta')

BRANCH_ENVIRONMENT_VARIABLE = 'WO_BRANCH'
CHANNEL_ENVIRONMENT_VARIABLE = 'WO_CHANNEL'
CHANNEL_TO_BRANCH = {
    'release': 'main',
    'stable': 'main',
    'main': 'main',
    'pre-release': 'beta',
    'prerelease': 'beta',
    'beta': 'beta',
}

SHORTCUT_NAMES = {
    'main': 'Windows Optimizer.lnk',
    'beta': 'Windows Optimizer (Beta).lnk',
}
SHORTCUT_DESCRIPTIONS = {
    'main': 'Run the latest Windows Optimizer release',
    'beta': 'Run the latest Windows Optimizer pre-release (beta channel)',
}

POWERSHELL_ARGUMENTS_PREFIX = ('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command')

# .lnk binary format: DWORD LinkFlags starts at offset 20, the
# RunAsAdministrator bit lives in the second byte of the following block.
_LNK_FLAGS_OFFSET = 21
_LNK_RUN_AS_ADMIN_BIT = 0x20


# --------------------------------------------------------------------- channel
def resolve_branch(branch=None):
    """Return ``'main'`` or ``'beta'`` from an argument or the environment."""
    candidates = [
        branch,
        os.environ.get(BRANCH_ENVIRONMENT_VARIABLE),
        os.environ.get(CHANNEL_ENVIRONMENT_VARIABLE),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        normalized = str(candidate).strip().lower()
        normalized = CHANNEL_TO_BRANCH.get(normalized, normalized)
        if normalized in SUPPORTED_BRANCHES:
            return normalized
        log.warning(
            f'Ignoring unsupported channel/branch "{candidate}"; '
            f'expected one of {", ".join(SUPPORTED_BRANCHES)}'
        )

    return DEFAULT_BRANCH


def raw_script_url(branch=None):
    """Raw GitHub URL of ``run.ps1`` for the resolved branch."""
    return RAW_URL_TEMPLATE.format(branch=resolve_branch(branch))


def launcher_command(branch=None):
    """The exact PowerShell one-liner executed by the shortcut."""
    return f'irm {raw_script_url(branch)} | iex'


def powershell_path():
    """Absolute path of Windows PowerShell (always present on Windows)."""
    system_root = os.environ.get('SystemRoot') or r'C:\Windows'
    candidate = Path(system_root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    if candidate.is_file():
        return str(candidate)
    return 'powershell.exe'


def shortcut_arguments(branch=None):
    """Argument string given to ``powershell.exe`` by the shortcut."""
    command = launcher_command(branch)
    quoted = command.replace('"', '\\"')
    return ' '.join(POWERSHELL_ARGUMENTS_PREFIX) + f' "{quoted}"'


def shortcut_name(branch=None):
    return SHORTCUT_NAMES[resolve_branch(branch)]


# ----------------------------------------------------------------- filesystem
def _known_folder_desktop():
    """Desktop path from ``SHGetKnownFolderPath`` (OneDrive aware)."""
    if not IS_WINDOWS:
        return None

    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ('Data1', wintypes.DWORD),
            ('Data2', wintypes.WORD),
            ('Data3', wintypes.WORD),
            ('Data4', ctypes.c_ubyte * 8),
        ]

    # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
    folder_id = _GUID(
        0xB4BFCC3A,
        0xDB2C,
        0x424C,
        (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
    )

    pointer = ctypes.c_wchar_p()
    try:
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)
        )
        if result != 0 or not pointer.value:
            return None
        return Path(pointer.value)
    except Exception:
        return None
    finally:
        try:
            if pointer.value:
                ctypes.windll.ole32.CoTaskMemFree(pointer)
        except Exception:
            pass


def desktop_directory():
    """Best available Desktop folder, or ``None`` when none is usable."""
    candidates = []

    known = _known_folder_desktop()
    if known is not None:
        candidates.append(known)

    profile = os.environ.get('USERPROFILE')
    if profile:
        candidates.append(Path(profile) / 'Desktop')

    for variable in ('OneDrive', 'OneDriveCommercial', 'OneDriveConsumer'):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / 'Desktop')

    candidates.append(Path.home() / 'Desktop')

    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except Exception:
            continue

    # Nothing exists yet: try to create the most canonical candidate.
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue

    return None


def shortcut_path(branch=None, directory=None, name=None):
    folder = Path(directory) if directory else desktop_directory()
    if folder is None:
        return None
    return folder / (name or shortcut_name(branch))


def source_icon_path():
    """``core-app/assets/icons/icon.ico``, the icon shipped with the sources."""
    try:
        return Path(__file__).resolve().parents[2] / 'assets' / 'icons' / 'icon.ico'
    except Exception:
        return None


def persistent_icon_path():
    """Durable copy of the icon, outside the (temporary) source folder.

    ``run.ps1`` expands the release into
    ``%LOCALAPPDATA%\\windows-optimizer\\src\\<tag>-<guid>`` and deletes that folder
    when the run ends. A ``.lnk`` pointing its ``IconLocation`` inside it would
    lose the icon on the next Explorer refresh, so the file is cached one level
    up, in ``%LOCALAPPDATA%\\windows-optimizer\\icon.ico``.
    """
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if not base:
        return None
    return Path(base) / 'windows-optimizer' / 'icon.ico'


def ensure_icon_file():
    """Return a usable ``.ico`` path, caching the shipped icon when possible."""
    source = source_icon_path()
    target = persistent_icon_path()

    if source is not None and source.is_file():
        if target is None:
            return source
        try:
            same = (
                target.is_file()
                and target.stat().st_size == source.stat().st_size
                and target.stat().st_mtime >= source.stat().st_mtime
            )
            if not same:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(source), str(target))
            return target
        except Exception as error:
            log.warning(f'Could not cache the shortcut icon: {error}')
            return source

    if target is not None and target.is_file():
        return target
    return None


def icon_location():
    """Icon used by the shortcut.

    ``core-app/assets/icons/icon.ico`` is the official icon; ``%WO_ICON%`` still
    overrides it, and the PowerShell icon remains the last resort so the
    shortcut is never created without one.
    """
    override = os.environ.get('WO_ICON')
    if override:
        return override if ',' in override else f'{override},0'

    icon = ensure_icon_file()
    if icon is not None:
        return f'{icon},0'

    return f'{powershell_path()},0'


# --------------------------------------------------------------------- writing
def _run_powershell(script):
    """Run a PowerShell snippet through ``-EncodedCommand``."""
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    completed = subprocess.run(
        [
            powershell_path(),
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy',
            'Bypass',
            '-EncodedCommand',
            encoded,
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )

    stdout = (completed.stdout or b'').decode('utf-8', errors='replace').strip()
    stderr = (completed.stderr or b'').decode('utf-8', errors='replace').strip()
    return completed.returncode, stdout, stderr


def _powershell_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _build_shortcut_script(target, arguments, destination, description, icon, working_dir):
    return '\n'.join(
        [
            "$ErrorActionPreference = 'Stop'",
            '$shell = New-Object -ComObject WScript.Shell',
            f'$link = $shell.CreateShortcut({_powershell_literal(destination)})',
            f'$link.TargetPath = {_powershell_literal(target)}',
            f'$link.Arguments = {_powershell_literal(arguments)}',
            f'$link.WorkingDirectory = {_powershell_literal(working_dir)}',
            f'$link.Description = {_powershell_literal(description)}',
            f'$link.IconLocation = {_powershell_literal(icon)}',
            '$link.WindowStyle = 1',
            '$link.Save()',
            '[Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null',
        ]
    )


def _mark_run_as_admin(destination):
    """Set the "Run as administrator" flag inside an existing ``.lnk``."""
    try:
        data = bytearray(Path(destination).read_bytes())
        if len(data) <= _LNK_FLAGS_OFFSET:
            return False
        if data[_LNK_FLAGS_OFFSET] & _LNK_RUN_AS_ADMIN_BIT:
            return True
        data[_LNK_FLAGS_OFFSET] |= _LNK_RUN_AS_ADMIN_BIT
        Path(destination).write_bytes(bytes(data))
        return True
    except Exception as error:
        log.warning(f'Could not set the "run as administrator" flag: {error}')
        return False


def create_shortcut(
    branch=None,
    directory=None,
    name=None,
    overwrite=True,
    run_as_admin=False,
):
    """Create the Desktop shortcut and return its path (or ``None`` on error).

    The shortcut runs ``irm <raw run.ps1 for branch> | iex`` in PowerShell.
    """
    resolved_branch = resolve_branch(branch)

    if not IS_WINDOWS:
        log.warning('Desktop shortcut creation is only supported on Windows')
        return None

    destination = shortcut_path(resolved_branch, directory=directory, name=name)
    if destination is None:
        log.error('Could not resolve the Desktop folder; shortcut was not created')
        return None

    if destination.exists() and not overwrite:
        log.info(f'Desktop shortcut already exists: {destination}')
        return destination

    target = powershell_path()
    arguments = shortcut_arguments(resolved_branch)
    working_dir = os.environ.get('SystemRoot') or r'C:\Windows'
    script = _build_shortcut_script(
        target=target,
        arguments=arguments,
        destination=destination,
        description=SHORTCUT_DESCRIPTIONS[resolved_branch],
        icon=icon_location(),
        working_dir=working_dir,
    )

    try:
        code, stdout, stderr = _run_powershell(script)
    except Exception as error:
        log.error(f'Error "{error}" while creating the Desktop shortcut')
        return None

    if code != 0 or not destination.exists():
        detail = stderr or stdout or f'exit code {code}'
        log.error(f'Desktop shortcut was not created ({detail})')
        return None

    if run_as_admin:
        _mark_run_as_admin(destination)

    log.info(f'Desktop shortcut created: {destination}')
    log.info(f'Shortcut command ({resolved_branch}): {launcher_command(resolved_branch)}')
    return destination


def shortcut_icon_of(destination):
    """Read the ``IconLocation`` stored inside an existing ``.lnk``."""
    if not IS_WINDOWS:
        return None
    script = '\n'.join(
        [
            "$ErrorActionPreference = 'Stop'",
            '$shell = New-Object -ComObject WScript.Shell',
            f'$link = $shell.CreateShortcut({_powershell_literal(destination)})',
            'Write-Output $link.IconLocation',
            '[Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null',
        ]
    )
    try:
        code, stdout, stderr = _run_powershell(script)
    except Exception:
        return None
    if code != 0:
        return None
    return stdout.strip()


def _same_icon(first, second):
    def normalize(value):
        text = str(value or '').strip().strip('"').replace('/', '\\')
        if text.endswith(',0'):
            text = text[:-2]
        return text.rstrip(',').lower()

    return normalize(first) == normalize(second)


def refresh_shortcut_icon(destination):
    """Repoint an existing shortcut to the current icon when it differs.

    Shortcuts created before ``core-app/assets/icons/icon.ico`` existed still
    show the PowerShell icon, and ``ensure_shortcut`` never rewrites a file that
    already exists, so the icon is checked (and only then fixed) on every run.
    """
    if not IS_WINDOWS or destination is None:
        return False

    expected = icon_location()
    current = shortcut_icon_of(destination)
    if current is None or _same_icon(current, expected):
        return False

    script = '\n'.join(
        [
            "$ErrorActionPreference = 'Stop'",
            '$shell = New-Object -ComObject WScript.Shell',
            f'$link = $shell.CreateShortcut({_powershell_literal(destination)})',
            f'$link.IconLocation = {_powershell_literal(expected)}',
            '$link.Save()',
            '[Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null',
        ]
    )
    try:
        code, stdout, stderr = _run_powershell(script)
    except Exception as error:
        log.warning(f'Could not update the shortcut icon: {error}')
        return False

    if code != 0:
        log.warning(f'Could not update the shortcut icon ({stderr or stdout})')
        return False

    log.info(f'Desktop shortcut icon updated to "{expected}"')
    return True


def ensure_shortcut(branch=None, directory=None, name=None, run_as_admin=False):
    """Create the shortcut only when it is missing. Returns its path or ``None``."""
    if not IS_WINDOWS:
        return None

    destination = shortcut_path(branch, directory=directory, name=name)
    if destination is not None and destination.exists():
        refresh_shortcut_icon(destination)
        return destination

    return create_shortcut(
        branch=branch,
        directory=directory,
        name=name,
        overwrite=False,
        run_as_admin=run_as_admin,
    )


def remove_shortcut(branch=None, directory=None, name=None):
    """Delete the shortcut when present. Returns ``True`` when it is gone."""
    destination = shortcut_path(branch, directory=directory, name=name)
    if destination is None:
        return False
    try:
        if destination.exists():
            destination.unlink()
            log.info(f'Desktop shortcut removed: {destination}')
        return True
    except Exception as error:
        log.error(f'Could not remove the Desktop shortcut "{destination}": {error}')
        return False


# ------------------------------------------------------------------------- CLI
def main(argv=None):
    arguments = list(argv if argv is not None else sys.argv[1:])

    def option(flag):
        if flag in arguments:
            index = arguments.index(flag)
            if index + 1 < len(arguments):
                return arguments[index + 1]
        for item in arguments:
            if item.startswith(flag + '='):
                return item.split('=', 1)[1]
        return None

    if '--help' in arguments or '-h' in arguments:
        print(__doc__)
        return 0

    branch = option('--branch')
    directory = option('--directory')
    name = option('--name')
    run_as_admin = '--run-as-admin' in arguments

    if '--remove' in arguments:
        return 0 if remove_shortcut(branch, directory=directory, name=name) else 1

    if '--print-command' in arguments:
        print(launcher_command(branch))
        return 0

    if '--force' in arguments:
        created = create_shortcut(
            branch,
            directory=directory,
            name=name,
            overwrite=True,
            run_as_admin=run_as_admin,
        )
    else:
        created = ensure_shortcut(
            branch, directory=directory, name=name, run_as_admin=run_as_admin
        )

    return 0 if created else 1


if __name__ == '__main__':
    sys.exit(main())
