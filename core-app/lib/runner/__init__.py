"""Command execution engine.

Design goals (each one comes from a real failure found in log.log):

* One command can never block or abort the remaining commands: every command
  runs in its own child process, with streamed output, a hard watchdog and a
  full process-tree kill on timeout.
* Output is not captured with a single fixed codec. ``sfc``/``DISM`` emit
  UTF-16 while ``cmd`` built-ins emit the OEM code page, so decoding is
  adaptive.
* PowerShell commands are passed through ``-EncodedCommand`` (base64/UTF-16LE),
  which removes every quoting/escaping problem.
* Non-zero exit codes are declarative (``accepted_exit_codes``) and a command
  can be marked ``"optional": true`` so an expected failure is only a warning.
"""

import base64
import os
import subprocess
import threading
import time

from lib import log

IS_WINDOWS = os.name == 'nt'
DEFAULT_TIMEOUT_SECONDS = 3600
KILL_GRACE_SECONDS = 5
OEM_ENCODING = 'oem' if IS_WINDOWS else 'utf-8'

CMD_RUNNER = ['cmd.exe', '/d', '/c']
POWERSHELL_RUNNER = [
    'powershell.exe',
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy',
    'Bypass',
]

STATUS_OK = 'OK'
STATUS_WARNING = 'WARNING'
STATUS_FAILED = 'FAILED'
STATUS_TIMEOUT = 'TIMEOUT'
STATUS_SKIPPED = 'SKIPPED'


def _clean(value, default=''):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _normalize_timeout(value):
    try:
        timeout = int(value)
    except Exception:
        return DEFAULT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS


def _normalize_exit_codes(value):
    if value is None:
        return set()
    if isinstance(value, (int, str)):
        value = [value]
    codes = set()
    for item in value:
        try:
            codes.add(int(str(item).strip(), 0))
        except Exception:
            continue
    return codes


def _expand(text, placeholders):
    result = text
    for key, replacement in placeholders.items():
        result = result.replace('{' + key + '}', str(replacement))
    return result


def _escape_cmd_value(value):
    return (
        value.replace('^', '^^')
        .replace('&', '^&')
        .replace('|', '^|')
        .replace('<', '^<')
        .replace('>', '^>')
    )


class CommandSpec:
    """Normalized representation of one commands.json entry."""

    def __init__(self, raw, group='', index=0, placeholders=None):
        self.group = group or ''
        self.index = index
        self.raw = raw if isinstance(raw, dict) else {'command': raw}

        self.command = _clean(self.raw.get('command'))
        self.name = _clean(self.raw.get('name')) or _clean(self.raw.get('id'))
        self.description = _clean(self.raw.get('description'))

        shell = _clean(self.raw.get('shell'), self.group) or 'cmd'
        shell = shell.lower()
        if shell in ('ps', 'ps1', 'pwsh', 'powershell.exe'):
            shell = 'powershell'
        if shell in ('cmd.exe', 'bat', 'batch', ''):
            shell = 'cmd'
        if shell not in ('cmd', 'powershell'):
            shell = 'cmd'
        self.shell = shell

        self.enabled = self.raw.get('enabled', True) is not False
        self.optional = bool(self.raw.get('optional', False))
        self.timeout = _normalize_timeout(self.raw.get('timeout'))
        self.detached = bool(self.raw.get('detached', False))

        raw_input = self.raw.get('input')
        self.input = None if raw_input is None else str(raw_input)
        self.input_mode = (_clean(self.raw.get('input_mode'), 'stdin') or 'stdin').lower()

        self.accepted_exit_codes = _normalize_exit_codes(
            self.raw.get('accepted_exit_codes', self.raw.get('accepted_return_codes'))
        )

        if placeholders:
            self.command = _expand(self.command, placeholders)
            if self.input:
                self.input = _expand(self.input, placeholders)

    @property
    def label(self):
        return self.name or self.command

    def is_accepted(self, return_code):
        return return_code == 0 or return_code in self.accepted_exit_codes

    def __repr__(self):
        return f'<CommandSpec {self.shell}:{self.label!r}>'


class CommandResult:
    def __init__(self, spec, status, return_code=None, detail=''):
        self.spec = spec
        self.status = status
        self.return_code = return_code
        self.detail = detail

    @property
    def ok(self):
        return self.status in (STATUS_OK, STATUS_WARNING, STATUS_SKIPPED)


def decode_output(chunk):
    """Decode child output without knowing its code page in advance."""
    if not chunk:
        return ''

    # sfc.exe / DISM.exe write UTF-16LE into the pipe.
    if chunk.count(b'\x00') / max(len(chunk), 1) > 0.25:
        try:
            return chunk.decode('utf-16-le', errors='replace')
        except Exception:
            pass

    for encoding in ('utf-8', OEM_ENCODING, 'latin-1'):
        try:
            return chunk.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return chunk.decode('utf-8', errors='replace')


class CommandRunner:
    def __init__(self, simulate=False):
        self.simulate = simulate

    # ---------------------------------------------------------------- builders
    def build(self, spec):
        """Return ``(cmdline, uses_shell_string)`` for a spec."""
        if spec.shell == 'powershell':
            if IS_WINDOWS:
                encoded = base64.b64encode(spec.command.encode('utf-16-le')).decode('ascii')
                return POWERSHELL_RUNNER + ['-EncodedCommand', encoded], False
            return POWERSHELL_RUNNER + ['-Command', spec.command], False

        # cmd: keep one single string so %VARS%, pipes and redirection work.
        if spec.input and spec.input_mode == 'pipe':
            payload = _escape_cmd_value(spec.input.replace('\r', '').replace('\n', ''))
            return f'cmd.exe /d /c echo {payload}| {spec.command}', True

        return f'cmd.exe /d /c {spec.command}', True

    def describe(self, spec):
        if spec.shell == 'powershell':
            return ' '.join(POWERSHELL_RUNNER + ['-Command', spec.command])
        cmdline, _ = self.build(spec)
        return cmdline if isinstance(cmdline, str) else ' '.join(cmdline)

    # --------------------------------------------------------------------- run
    def run(self, spec):
        if not spec.command:
            log.warning(f'Skip empty command in group "{spec.group}"')
            return CommandResult(spec, STATUS_SKIPPED, detail='empty command')

        if not spec.enabled:
            log.info(f'Skip disabled command "{spec.label}"')
            return CommandResult(spec, STATUS_SKIPPED, detail='disabled')

        log.info(
            f'Start command "{spec.label}" '
            f'(shell={spec.shell}, timeout={spec.timeout}s, group={spec.group or "-"})'
        )
        log.debug(f'Resolved cmdline: {self.describe(spec)}')

        if self.simulate:
            log.info(f'[SIMULATE] {self.describe(spec)}')
            return CommandResult(spec, STATUS_OK, 0, detail='simulated')

        try:
            if spec.detached:
                self.launch_detached(spec)
                log.info(f'Run command "{spec.label}" (detached)')
                return CommandResult(spec, STATUS_OK, 0, detail='detached')
            return self._run_supervised(spec)
        except KeyboardInterrupt:
            log.error(f'Command "{spec.label}" was interrupted and was skipped')
            return CommandResult(spec, STATUS_SKIPPED, detail='interrupted')
        except Exception as error:
            log.error(f'Error "{error}" to run the command "{spec.label}"')
            return CommandResult(spec, STATUS_FAILED, detail=str(error))

    def run_raw(self, label, cmdline, timeout, accepted_exit_codes=None, optional=False):
        """Run an internal command that is not defined in commands.json."""
        spec = CommandSpec(
            {
                'name': label,
                'command': cmdline,
                'shell': 'cmd',
                'timeout': timeout,
                'optional': optional,
                'accepted_exit_codes': sorted(accepted_exit_codes or []),
            },
            group='internal',
        )
        return self.run(spec)

    def _popen_kwargs(self, uses_shell_string, with_stdin):
        kwargs = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.STDOUT,
            'stdin': subprocess.PIPE if with_stdin else subprocess.DEVNULL,
            'bufsize': 0,
        }
        if IS_WINDOWS:
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['shell'] = bool(uses_shell_string)
        return kwargs

    def _run_supervised(self, spec):
        cmdline, uses_shell_string = self.build(spec)
        with_stdin = bool(spec.input) and spec.input_mode != 'pipe'

        process = subprocess.Popen(
            cmdline, **self._popen_kwargs(uses_shell_string, with_stdin)
        )

        collected = []
        reader = threading.Thread(
            target=self._pump_output, args=(process, collected), daemon=True
        )
        reader.start()

        if with_stdin:
            self._feed_stdin(process, spec.input)

        deadline = time.time() + spec.timeout
        timed_out = False

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(remaining, 1.0))
                break
            except subprocess.TimeoutExpired:
                continue

        if timed_out:
            self._kill_tree(process)
            reader.join(timeout=KILL_GRACE_SECONDS)
            log.error(f'Command "{spec.label}" exceeded timeout of {spec.timeout} seconds')
            status = STATUS_WARNING if spec.optional else STATUS_TIMEOUT
            return CommandResult(spec, status, detail='timeout')

        reader.join(timeout=KILL_GRACE_SECONDS)
        return_code = process.returncode
        tail = ''.join(collected).strip()[-800:]

        if return_code == 0:
            log.info(f'Run command "{spec.label}"')
            return CommandResult(spec, STATUS_OK, return_code)

        if spec.is_accepted(return_code):
            log.info(
                f'Run command "{spec.label}" (exit code {return_code} is acceptable)'
            )
            return CommandResult(spec, STATUS_OK, return_code)

        message = f'Command "{spec.label}" exited with code {return_code}'
        if tail:
            message += f': {tail}'

        if spec.optional:
            log.warning(message + ' (optional command, execution continues)')
            return CommandResult(spec, STATUS_WARNING, return_code, tail)

        log.error(message)
        return CommandResult(spec, STATUS_FAILED, return_code, tail)

    def _pump_output(self, process, collected):
        stream = process.stdout
        if stream is None:
            return
        pending = b''
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                pending += chunk
                if len(pending) >= 1024 or b'\n' in pending or b'\r' in pending:
                    text = decode_output(pending)
                    pending = b''
                    collected.append(text)
                    log.terminal_output(text)
            if pending:
                text = decode_output(pending)
                collected.append(text)
                log.terminal_output(text)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _feed_stdin(self, process, data):
        def writer():
            try:
                process.stdin.write(data.encode('utf-8', errors='replace'))
                process.stdin.flush()
            except Exception:
                pass
            finally:
                try:
                    process.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=writer, daemon=True).start()

    def _kill_tree(self, process):
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ['taskkill', '/T', '/F', '/PID', str(process.pid)],
                    check=False,
                    capture_output=True,
                    timeout=20,
                )
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
        except Exception:
            pass

    def launch_detached(self, spec):
        cmdline, uses_shell_string = self.build(spec)
        kwargs = {
            'stdin': subprocess.DEVNULL,
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL,
            'close_fds': True,
        }
        if IS_WINDOWS:
            kwargs['creationflags'] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs['shell'] = bool(uses_shell_string)
        subprocess.Popen(cmdline, **kwargs)
