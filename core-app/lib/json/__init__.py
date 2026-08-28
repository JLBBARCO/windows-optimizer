"""JSON helpers used to load the maintenance plan.

The application runs from the source tree of a release, so resources are always
resolved relative to ``core-app``. A ``commands.json`` placed next to the
application folder still wins over the bundled one, which is what allows a
custom plan without touching the repository copy.
"""

import json as _json
from pathlib import Path

COMMANDS_FILE_NAME = 'commands.json'


def read_json(file_path):
    with open(file_path, 'r', encoding='utf-8-sig') as handle:
        data = handle.read().strip()
        return _json.loads(data) if data else {}


def resource_dir():
    """Folder that holds the application resources (``core-app``)."""
    return Path(__file__).resolve().parents[2]


def candidate_command_files():
    """Ordered list of possible commands.json locations.

    A file placed beside ``core-app`` wins over the bundled one, so the plan can
    be customized without editing the repository copy.
    """
    base = resource_dir()

    candidates = [
        base.parent / COMMANDS_FILE_NAME,
        base / COMMANDS_FILE_NAME,
        base / 'json' / COMMANDS_FILE_NAME,
    ]

    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def read_commands():
    """Return ``(data, path)`` for the first readable commands.json."""
    errors = []
    for candidate in candidate_command_files():
        try:
            if candidate.is_file():
                return read_json(candidate), candidate
        except Exception as error:  # malformed file -> try the next candidate
            errors.append(f'{candidate}: {error}')

    detail = ' | '.join(errors) if errors else 'no candidate file found'
    raise FileNotFoundError(f'Unable to read {COMMANDS_FILE_NAME} ({detail})')


def read_external_json(file):
    url_path = (
        'https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/main/'
        f'core-app/json/{file}.json'
    )
    try:
        import requests

        response = requests.get(url_path, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None
