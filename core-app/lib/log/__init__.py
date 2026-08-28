"""Logging utilities for Windows Optimizer.

The application always runs on the Python interpreter, directly from the source
tree of a release, so the log directory is resolved as:

1. ``%WO_LOG_DIR%`` when provided (``run.ps1`` exports it and the elevated child
   inherits it, so launcher, parent and child always share one folder);
2. ``%LOCALAPPDATA%\\windows-optimizer\\logs`` when the sources were staged by
   ``run.ps1``, because that staging folder is deleted when the run ends and the
   logs would disappear with it;
3. the repository root when running from a real checkout;
4. ``%LOCALAPPDATA%\\windows-optimizer\\logs`` as a last-resort fallback when the
   chosen folder cannot be written (typical when the elevated process runs as a
   different user, or when the repository lives inside OneDrive and the file is
   locked by the sync client).

Every line carries the process id, which is what makes an interleaved
parent/child log readable.
"""

import datetime
import os
import sys
import threading
from pathlib import Path

LOG_FILE_NAME = 'log.log'
TERMINAL_LOG_FILE_NAME = 'terminal.log'

_lock = threading.Lock()
_log_file = None
_terminal_log_file = None
_echo_to_console = True


def source_root():
    """Root of the source tree that is being executed."""
    return Path(__file__).resolve().parents[3]


def _local_app_log_dir():
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if base:
        return Path(base) / 'windows-optimizer' / 'logs'
    return Path(os.environ.get('TMPDIR', '/tmp')) / 'windows-optimizer-logs'


def is_ephemeral_source(root=None):
    """True when the sources come from the ``run.ps1`` staging folder.

    ``run.ps1`` expands the release sources into
    ``%LOCALAPPDATA%\\windows-optimizer\\src\\<tag>-<guid>`` and removes that folder
    when the run ends, so nothing durable may be written inside it.
    """
    text = str(root if root is not None else source_root()).replace('\\', '/').lower()
    return '/windows-optimizer/src/' in text + '/'


def _default_log_dir():
    env_dir = os.environ.get('WO_LOG_DIR')
    if env_dir:
        return Path(env_dir)

    root = source_root()
    if is_ephemeral_source(root):
        return _local_app_log_dir()

    # Real checkout: keep writing next to the repository root.
    return root


_log_dir = _default_log_dir()


def log_dir():
    return _log_dir


def log_paths():
    return _log_dir / LOG_FILE_NAME, _log_dir / TERMINAL_LOG_FILE_NAME


def configure(log_directory=None, echo_to_console=True):
    """(Re)configure the log destination. Safe to call more than once."""
    global _log_dir, _log_file, _terminal_log_file, _echo_to_console

    _echo_to_console = bool(echo_to_console)

    if log_directory is not None:
        new_dir = Path(log_directory)
        if new_dir != _log_dir:
            _close_files()
            _log_dir = new_dir

    _open_files()
    return log_paths()


def _close_files():
    global _log_file, _terminal_log_file
    for handle in (_log_file, _terminal_log_file):
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass
    _log_file = None
    _terminal_log_file = None


def _fallback_log_dir():
    return _local_app_log_dir()


def _try_open(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None, None

    handles = []
    for name in (LOG_FILE_NAME, TERMINAL_LOG_FILE_NAME):
        try:
            handles.append(open(directory / name, 'a+', encoding='utf-8'))
        except Exception:
            for handle in handles:
                try:
                    handle.close()
                except Exception:
                    pass
            return None, None
    return handles[0], handles[1]


def _open_files():
    """Open log handles lazily so an unwritable folder never breaks the app."""
    global _log_dir, _log_file, _terminal_log_file

    if _log_file is not None and _terminal_log_file is not None:
        return

    _close_files()

    primary, terminal = _try_open(_log_dir)
    if primary is None:
        fallback = _fallback_log_dir()
        if fallback != _log_dir:
            primary, terminal = _try_open(fallback)
            if primary is not None:
                _log_dir = fallback
                # Make the child processes inherit the folder that actually works.
                os.environ['WO_LOG_DIR'] = str(fallback)

    _log_file = primary
    _terminal_log_file = terminal


def _now():
    return datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')


def _write(handle, text):
    if handle is None:
        return
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    except Exception:
        # Never let logging abort the maintenance plan.
        pass


def _console(text):
    if not _echo_to_console:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


def _new_line():
    with _lock:
        _open_files()
        _write(_log_file, '\n')
    _console('\n')


def log(message, level='INFO'):
    now = _now()
    level = str(level).strip().upper()
    line = f'[{now}] [{level}] [pid {os.getpid()}] {message}\n'

    with _lock:
        _open_files()
        _write(_log_file, line)
    _console(line)


def info(message):
    log(message, 'INFO')


def warning(message):
    log(message, 'WARNING')


def error(message):
    log(message, 'ERROR')


def debug(message):
    if os.environ.get('WO_DEBUG'):
        log(message, 'DEBUG')


def terminal_output(message):
    """Persist raw child-process output into terminal.log."""
    if message is None:
        return

    text = str(message)
    if not text.strip():
        return

    with _lock:
        _open_files()
        buffer = []
        for raw_line in text.splitlines():
            content = raw_line.rstrip('\r\n').replace('\x00', '').strip()
            if content:
                buffer.append(f'[{_now()}] {content}\n')

        if buffer:
            _write(_terminal_log_file, ''.join(buffer))
            _console(''.join(buffer))
