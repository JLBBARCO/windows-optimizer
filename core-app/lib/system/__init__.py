"""Windows/system helpers: UAC elevation, diagnostics, processes, tools.

Elevation notes
---------------
``ShellExecuteW(..., 'runas', ...)`` only tells us that a process was created,
never that the new process really received an elevated token. That is why the
previous version could look like "the app keeps saying it is not admin": the
parent exited immediately after the UAC prompt and all the real information was
in another console window.

This module now:

* reports the real token state (elevated, elevation type, integrity level,
  user) so the log answers the question instead of guessing;
* uses ``ShellExecuteExW`` with ``SEE_MASK_NOCLOSEPROCESS`` so the parent waits
  for the elevated child and mirrors its exit code (the child is always
  ``python.exe`` plus ``main.py``, since the application is never packaged);
* forwards ``%WO_LOG_DIR%`` to the child, keeping both processes in one log;
* never prompts twice (``--elevated`` guard) and distinguishes a refused UAC
  prompt (``ERROR_CANCELLED``) from a real failure.
"""

import ctypes
import glob
import os
import subprocess
import sys
import time
from pathlib import Path

from lib import log

IS_WINDOWS = os.name == 'nt'
ELEVATED_FLAG = '--elevated'
EXPLORER_RESTART_DELAY_SECONDS = 2
EXPLORER_START_WAIT_SECONDS = 15

ERROR_CANCELLED = 1223

# Token information classes
_TOKEN_QUERY = 0x0008
_TokenElevationType = 18
_TokenElevation = 20
_TokenIntegrityLevel = 25

ELEVATION_TYPE_NAMES = {
    1: 'default (UAC disabled or no split token)',
    2: 'full (elevated)',
    3: 'limited (filtered admin token)',
}

INTEGRITY_NAMES = {
    0x0000: 'untrusted',
    0x1000: 'low',
    0x2000: 'medium',
    0x2100: 'medium plus',
    0x3000: 'high',
    0x4000: 'system',
}

if IS_WINDOWS:
    from ctypes import wintypes

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD),
            ('fMask', ctypes.c_ulong),
            ('hwnd', wintypes.HWND),
            ('lpVerb', wintypes.LPCWSTR),
            ('lpFile', wintypes.LPCWSTR),
            ('lpParameters', wintypes.LPCWSTR),
            ('lpDirectory', wintypes.LPCWSTR),
            ('nShow', ctypes.c_int),
            ('hInstApp', wintypes.HINSTANCE),
            ('lpIDList', ctypes.c_void_p),
            ('lpClass', wintypes.LPCWSTR),
            ('hkeyClass', wintypes.HKEY),
            ('dwHotKey', wintypes.DWORD),
            ('hIconOrMonitor', wintypes.HANDLE),
            ('hProcess', wintypes.HANDLE),
        ]

    _SEE_MASK_NOCLOSEPROCESS = 0x00000040
    _SEE_MASK_NOASYNC = 0x00000100
    _SW_SHOWNORMAL = 1
    _INFINITE = 0xFFFFFFFF


# ---------------------------------------------------------------- token state
def _open_process_token():
    handle = ctypes.wintypes.HANDLE()
    if not ctypes.windll.advapi32.OpenProcessToken(
        ctypes.windll.kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(handle),
    ):
        return None
    return handle


def _token_dword(info_class):
    if not IS_WINDOWS:
        return None
    handle = None
    try:
        handle = _open_process_token()
        if handle is None:
            return None
        value = ctypes.wintypes.DWORD()
        size = ctypes.wintypes.DWORD()
        ok = ctypes.windll.advapi32.GetTokenInformation(
            handle,
            info_class,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(size),
        )
        return int(value.value) if ok else None
    except Exception:
        return None
    finally:
        if handle is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass


def integrity_level():
    """Return the process integrity level RID (0x3000 = high) or ``None``."""
    if not IS_WINDOWS:
        return None
    handle = None
    try:
        handle = _open_process_token()
        if handle is None:
            return None

        size = ctypes.wintypes.DWORD(0)
        ctypes.windll.advapi32.GetTokenInformation(
            handle, _TokenIntegrityLevel, None, 0, ctypes.byref(size)
        )
        if not size.value:
            return None

        buffer = ctypes.create_string_buffer(size.value)
        if not ctypes.windll.advapi32.GetTokenInformation(
            handle, _TokenIntegrityLevel, buffer, size, ctypes.byref(size)
        ):
            return None

        # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label } -> Label.Sid
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]

        get_count = ctypes.windll.advapi32.GetSidSubAuthorityCount
        get_count.restype = ctypes.POINTER(ctypes.c_ubyte)
        get_count.argtypes = [ctypes.c_void_p]

        get_sub = ctypes.windll.advapi32.GetSidSubAuthority
        get_sub.restype = ctypes.POINTER(ctypes.wintypes.DWORD)
        get_sub.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]

        count = get_count(sid)[0]
        if count == 0:
            return None
        return int(get_sub(sid, count - 1)[0])
    except Exception:
        return None
    finally:
        if handle is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass


def is_admin():
    """True when the current process token is elevated."""
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, 'geteuid') else False

    elevated = _token_dword(_TokenElevation)
    if elevated is not None:
        return bool(elevated)

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevation_type():
    return _token_dword(_TokenElevationType)


def can_be_elevated():
    """True when a UAC prompt can realistically produce an elevated token."""
    if not IS_WINDOWS:
        return False
    # Type 3 = filtered admin token, a consent prompt is enough.
    # Type 1 = no split token (standard user or UAC disabled); the prompt will
    # ask for administrator credentials, which may still succeed.
    return elevation_type() in (1, 3)


def token_report():
    """Human readable token diagnostics, always written to the log."""
    if not IS_WINDOWS:
        return 'not running on Windows'

    level = integrity_level()
    elevation = elevation_type()

    if level is None:
        integrity_text = 'unknown'
    else:
        integrity_text = f'0x{level:04X} ({INTEGRITY_NAMES.get(level, "unknown")})'

    return (
        f'user={os.environ.get("USERNAME", "?")}'
        f' | elevated={is_admin()}'
        f' | elevation_type={elevation} ({ELEVATION_TYPE_NAMES.get(elevation, "unknown")})'
        f' | integrity={integrity_text}'
        f' | python={sys.version.split()[0]}'
        f' | pid={os.getpid()}'
        f' | argv={sys.argv[1:]}'
    )


# -------------------------------------------------------------- elevation flow
class ElevationOutcome:
    """Result of an elevation attempt."""

    def __init__(self, started=False, exit_code=None, refused=False, detail=''):
        self.started = started
        self.exit_code = exit_code
        self.refused = refused
        self.detail = detail


def _relaunch_arguments():
    arguments = []
    skip_next = False
    for argument in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if argument == ELEVATED_FLAG:
            continue
        if argument == '--log-dir':
            skip_next = True
            continue
        if argument.startswith('--log-dir='):
            continue
        arguments.append(argument)

    # The environment is not guaranteed to survive the elevation broker, so the
    # log folder is also passed explicitly: parent and child always share it.
    arguments.extend(['--log-dir', str(log.log_dir())])
    arguments.append(ELEVATED_FLAG)
    return arguments


def _relaunch_target():
    """Return ``(interpreter, parameters)`` used to relaunch through UAC."""
    arguments = _relaunch_arguments()
    params = ' '.join(
        [f'"{os.path.abspath(sys.argv[0])}"']
        + [f'"{argument}"' for argument in arguments]
    )
    return sys.executable, params


def elevate_and_wait(wait=True):
    """Relaunch elevated and (by default) wait for the elevated child.

    Waiting matters: the user sees a single flow instead of a parent window
    that announces "Request administrator privileges" and closes while the real
    work happens somewhere else.
    """
    if not IS_WINDOWS:
        return ElevationOutcome(detail='elevation is only supported on Windows')

    if ELEVATED_FLAG in sys.argv:
        log.error(
            'This process was already relaunched for elevation but the token is '
            'still not elevated, so no second UAC prompt will be shown. '
            f'Token state: {token_report()}'
        )
        return ElevationOutcome(detail='already relaunched without elevation')

    if os.environ.get('WO_NO_ELEVATE'):
        return ElevationOutcome(detail='elevation disabled by WO_NO_ELEVATE')

    executable, params = _relaunch_target()
    # The elevated child inherits this environment, so both processes write to
    # the very same log folder.
    os.environ['WO_LOG_DIR'] = str(log.log_dir())

    working_dir = str(Path(os.path.abspath(sys.argv[0])).parent)

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
    info.hwnd = None
    info.lpVerb = 'runas'
    info.lpFile = executable
    info.lpParameters = params
    info.lpDirectory = working_dir
    info.nShow = _SW_SHOWNORMAL

    log.info(f'Requesting administrator privileges for: {executable} {params}')

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.GetLastError()
        if code == ERROR_CANCELLED:
            log.warning('The UAC prompt was refused or dismissed by the user')
            return ElevationOutcome(refused=True, detail='UAC refused')
        log.error(f'ShellExecuteExW failed to elevate (GetLastError={code})')
        return ElevationOutcome(detail=f'ShellExecuteExW error {code}')

    log.info('Administrator privileges granted; elevated process started')

    if not wait or not info.hProcess:
        return ElevationOutcome(started=True, detail='child not awaited')

    try:
        ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, _INFINITE)
        exit_code = ctypes.wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(
            info.hProcess, ctypes.byref(exit_code)
        )
        code = int(exit_code.value)
        log.info(f'Elevated process finished with exit code {code}')
        return ElevationOutcome(started=True, exit_code=code)
    except Exception as error:
        log.error(f'Error "{error}" while waiting for the elevated process')
        return ElevationOutcome(started=True, detail=str(error))
    finally:
        try:
            ctypes.windll.kernel32.CloseHandle(info.hProcess)
        except Exception:
            pass


def elevate():
    """Backwards compatible wrapper: True when an elevated child was started."""
    return elevate_and_wait(wait=False).started


# ------------------------------------------------------- single instance guard
class InstanceLock:
    """Prevent two concurrent runs from fighting over the same machine.

    Overlapping runs were visible in log.log: a run started at 14:56 was still
    cleaning %TEMP% when another one started at 14:56:58, and both appended to
    the same files, producing an unreadable, apparently truncated log.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.acquired = False

    def acquire(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        for _ in range(2):
            try:
                handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(handle, 'w', encoding='utf-8') as file:
                    file.write(str(os.getpid()))
                self.acquired = True
                return True
            except FileExistsError:
                owner = self._read_owner()
                if owner and _pid_is_running(owner):
                    log.error(
                        f'Another Windows Optimizer instance is already running (pid {owner}). '
                        'Close it before starting a new run.'
                    )
                    return False
                log.warning(f'Removing stale lock file "{self.path}" (pid {owner})')
                try:
                    self.path.unlink()
                except Exception:
                    return False
            except Exception as error:
                log.warning(f'Could not create lock file "{self.path}": {error}')
                return True
        return False

    def _read_owner(self):
        try:
            return int(self.path.read_text(encoding='utf-8').strip())
        except Exception:
            return None

    def release(self):
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except Exception:
            pass
        self.acquired = False


def _pid_is_running(pid):
    if not pid:
        return False
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {int(pid)}', '/NH'],
                check=False,
                capture_output=True,
                timeout=15,
            )
            output = (result.stdout or b'').decode('utf-8', errors='replace')
            return str(int(pid)) in output
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------- processes
def is_process_running(process_name):
    if not IS_WINDOWS:
        return False
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {process_name}'],
            check=False,
            capture_output=True,
            timeout=15,
        )
        output = (result.stdout or b'').decode('utf-8', errors='replace').lower()
        if process_name.lower() not in output:
            output = (result.stdout or b'').decode('latin-1', errors='replace').lower()
        return process_name.lower() in output
    except Exception:
        return False


def wait_for_process(process_name, timeout_seconds):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_process_running(process_name):
            return True
        time.sleep(0.5)
    return False


# ------------------------------------------------------------ self protection
def protected_paths():
    """Paths that maintenance commands must never delete.

    The running source tree and the log folder are excluded from the cleanup
    commands: deleting either of them kills the plan in the middle of the run.
    """
    paths = []

    try:
        paths.append(log.source_root())
    except Exception:
        pass

    try:
        paths.append(Path(sys.executable).resolve())
    except Exception:
        pass

    try:
        paths.append(log.log_dir())
    except Exception:
        pass

    unique = []
    for path in paths:
        text = str(path)
        if text and text not in unique:
            unique.append(text)
    return unique


def export_protection_environment():
    paths = protected_paths()
    os.environ['WO_PROTECTED_PATHS'] = ';'.join(paths)
    return paths


# ----------------------------------------------------------------- tool paths
def resolve_winget():
    """Return a usable winget command.

    In an elevated context the ``WindowsApps`` execution alias is frequently
    missing from ``PATH``, and the alias under the *user* profile does not work
    for the SYSTEM/administrator context, so prefer the real package path.
    """
    if not IS_WINDOWS:
        return 'winget'

    patterns = [
        os.path.expandvars(
            r'%ProgramFiles%\WindowsApps\Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe\winget.exe'
        ),
        os.path.expandvars(
            r'%ProgramFiles%\WindowsApps\Microsoft.DesktopAppInstaller_*_x86__8wekyb3d8bbwe\winget.exe'
        ),
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return f'"{matches[-1]}"'

    try:
        probe = subprocess.run(
            ['where', 'winget.exe'], check=False, capture_output=True, timeout=15
        )
        for line in (probe.stdout or b'').decode('utf-8', errors='replace').splitlines():
            candidate = line.strip()
            if candidate and Path(candidate).is_file():
                return f'"{candidate}"'
    except Exception:
        pass

    return 'winget'


def placeholders():
    return {
        'winget': resolve_winget(),
        'system_drive': os.environ.get('SystemDrive', 'C:'),
        'windir': os.environ.get('WINDIR', r'C:\Windows'),
        'temp': os.environ.get('TEMP', ''),
        'log_dir': str(log.log_dir()),
    }


# ------------------------------------------------------------------- explorer
def restart_explorer(runner):
    log.info(f'Waiting {EXPLORER_RESTART_DELAY_SECONDS}s before restarting Explorer shell')
    time.sleep(EXPLORER_RESTART_DELAY_SECONDS)

    if not IS_WINDOWS:
        return True

    attempts = [
        ('cmd-start', 'start "" "%WINDIR%\\explorer.exe"'),
        ('explorer-direct', '"%WINDIR%\\explorer.exe"'),
        ('powershell-start-process', 'Start-Process explorer.exe'),
    ]

    for method_name, command in attempts:
        shell = 'powershell' if method_name.startswith('powershell') else 'cmd'
        try:
            from lib.runner import CommandSpec

            spec = CommandSpec(
                {
                    'name': f'Restart Explorer ({method_name})',
                    'command': command,
                    'shell': shell,
                    'timeout': 20,
                    'detached': True,
                    'optional': True,
                },
                group='internal',
            )
            runner.run(spec)
        except Exception as error:
            log.error(f'Explorer start method "{method_name}" failed: {error}')
            continue

        if wait_for_process('explorer.exe', EXPLORER_START_WAIT_SECONDS):
            log.info(f'Explorer shell restart confirmed via {method_name}')
            return True

        log.warning(
            f'Explorer was not detected after method "{method_name}"; trying next fallback.'
        )

    log.error('Explorer shell restart failed after all recovery methods')
    return False


def ensure_explorer_running(runner):
    if not IS_WINDOWS:
        return True
    if is_process_running('explorer.exe'):
        return True
    log.warning('Explorer is not running after command execution. Attempting recovery.')
    try:
        return restart_explorer(runner)
    except Exception as error:
        log.error(f'Error "{error}" while recovering explorer shell')
        return False


# -------------------------------------------------------------------- restart
def ask_for_restart():
    title = 'Windows Optimizer'
    message = (
        'Maintenance commands finished.\n\n'
        'Do you want to restart your computer now to apply all changes?'
    )

    if not IS_WINDOWS:
        log.info('Restart prompt skipped (not running on Windows)')
        return False

    try:
        response = ctypes.windll.user32.MessageBoxW(
            None, message, title, 0x00000004 | 0x00000020 | 0x00040000
        )
    except Exception as error:
        log.error(f'Error "{error}" while showing restart prompt')
        return False

    if response == 6:
        log.info('User accepted restart prompt')
        return True

    log.info('User declined restart prompt')
    return False


def restart_pc(delay_seconds=30):
    try:
        subprocess.run(
            ['shutdown.exe', '/r', '/f', '/t', str(delay_seconds)], check=False
        )
        log.info(f'Restart PC in {delay_seconds} seconds')
    except Exception as error:
        log.error(f'Error "{error}" to restart PC')
