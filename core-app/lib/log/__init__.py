import datetime
import json
import sys
import threading
from pathlib import Path

_log_file_path = Path(__file__).resolve().parents[3] / 'log.log'
_terminal_log_file_path = Path(__file__).resolve().parents[3] / 'terminal.log'
_log_file = open(_log_file_path, 'a+', encoding='utf-8')
_terminal_log_file = open(_terminal_log_file_path, 'a+', encoding='utf-8')
_lock = threading.Lock()


def _now():
    return datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')


def _new_line():
    with _lock:
        _log_file.write('\n')
        _log_file.flush()


def log(message, level="INFO"):
    now = _now()
    level = str(level).strip().upper()

    with _lock:
        _log_file.write(f'[{now}] [{level}] {message}\n')
        _log_file.flush()


def info(message):
    log(message, 'INFO')


def warning(message):
    log(message, 'WARNING')


def error(message):
    log(message, 'ERROR')


def terminal_output(message):
    if message is None:
        return

    text = str(message)
    if not text:
        return

    with _lock:
        sys.stdout.write(text)
        sys.stdout.flush()

        for line in text.splitlines(keepends=True):
            content = line.rstrip('\r\n')
            if content:
                _terminal_log_file.write(f'[{_now()}] {content}\n')

        _terminal_log_file.flush()