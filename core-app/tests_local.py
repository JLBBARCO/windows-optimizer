"""Local sanity tests for the execution engine (POSIX-friendly).

They validate the plan normalization and the supervision logic (streaming,
accepted exit codes, watchdog + kill, stdin feeding, adaptive decoding)
without needing Windows. Run: python tests_local.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ['WO_LOG_DIR'] = str(Path(__file__).resolve().parent / '.test-logs')

from lib import json as json_lib  # noqa: E402
from lib import log  # noqa: E402
from lib.runner import (  # noqa: E402
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_WARNING,
    STATUS_FAILED,
    CommandRunner,
    CommandSpec,
    decode_output,
)

log.configure(echo_to_console=False)

failures = []


def check(name, condition, extra=''):
    if condition:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name} {extra}')
        failures.append(name)


class PosixRunner(CommandRunner):
    """Maps the cmd shell to /bin/sh so the engine can be tested here."""

    def build(self, spec):
        if spec.input and spec.input_mode == 'pipe':
            payload = spec.input.replace('\n', '')
            return f'printf %s "{payload}" | {spec.command}', True
        return spec.command, True


def spec(command, **kwargs):
    payload = {'command': command}
    payload.update(kwargs)
    return CommandSpec(payload, group='test')


print('plan normalization')
data, path = json_lib.read_commands()
entries = data['plan']
check('commands.json readable', bool(entries), path)
check('all entries have a name', all(item.get('name') for item in entries))
check(
    'no explorer/iconcache command left in the JSON plan',
    not any(
        'iconcache' in item['command'].lower()
        or 'explorer.exe' in item['command'].lower()
        for item in entries
    ),
)
check(
    'every entry is optional (never aborts the plan)',
    all(item.get('optional') is True for item in entries),
)
normalized = [CommandSpec(item, group=item.get('group', '')) for item in entries]
check('all shells valid', all(s.shell in ('cmd', 'powershell') for s in normalized))
check('all timeouts positive', all(s.timeout > 0 for s in normalized))
winget = [s for s in normalized if 'winget' in s.command]
check('winget accepted exit codes parsed', bool(winget[0].accepted_exit_codes), winget[0].accepted_exit_codes)

print('runner supervision')
runner = PosixRunner()

result = runner.run(spec('echo hello-world', name='success'))
check('successful command reports OK', result.status == STATUS_OK, result.status)

result = runner.run(spec('exit 7', name='hard failure'))
check('non-zero exit reports FAILED', result.status == STATUS_FAILED, result.status)

result = runner.run(spec('exit 7', name='optional failure', optional=True))
check('optional failure reports WARNING', result.status == STATUS_WARNING, result.status)

result = runner.run(spec('exit 7', name='accepted code', accepted_exit_codes=[7]))
check('accepted exit code reports OK', result.status == STATUS_OK, result.status)

result = runner.run(spec('sleep 30', name='hang', timeout=2))
check('hanging command is killed by the watchdog', result.status == STATUS_TIMEOUT, result.status)

result = runner.run(spec('sleep 30', name='optional hang', timeout=2, optional=True))
check('optional hang degrades to WARNING', result.status == STATUS_WARNING, result.status)

result = runner.run(spec('read answer; test "$answer" = Y', name='stdin', input='Y\n'))
check('stdin is delivered', result.status == STATUS_OK, result.status)

result = runner.run(
    spec('cat', name='pipe input', input='Y\n', input_mode='pipe')
)
check('pipe input mode works', result.status == STATUS_OK, result.status)

# The interpreter itself generates the output: "seq" does not exist on Windows
# and the run there ended in [WinError 2] instead of testing the reader.
large_output_command = (
    f'"{sys.executable}" -c "'
    'import sys;sys.stdout.write(chr(10).join(str(i) for i in range(1, 20001)))"'
)
result = runner.run(spec(large_output_command, name='large output', timeout=60))
check('large output does not deadlock', result.status == STATUS_OK, result.status)

print('decoding')
check('utf-16 output decoded', 'sfc' in decode_output('sfc ok'.encode('utf-16-le')))
check('utf-8 output decoded', decode_output('conclu\u00eddo'.encode('utf-8')) == 'conclu\u00eddo')
check('latin-1 fallback decoded', 'XITO' in decode_output('\u00caXITO'.encode('latin-1')).upper())

print('plan execution end to end (simulated)')
import main as main_module  # noqa: E402

main_module.CommandRunner = CommandRunner
app = main_module.Application(['--simulate', '--no-restart-prompt'])
exit_code = app.run()
executed = [r for r in app.results if r.spec.group != 'internal']
internal = [r for r in app.results if r.spec.group == 'internal']
check('simulated run exits 0', exit_code == 0, exit_code)
check(
    f'all {len(entries)} JSON commands were planned',
    len(executed) == len(entries),
    f'{len(executed)} vs {len(entries)}',
)
check('explorer workflow appended at the end', len(internal) == 3, len(internal))
check('no failure in the report', not app._has_failure())

print('elevation diagnostics and single instance lock')
from lib import system  # noqa: E402

check('token report never raises', isinstance(system.token_report(), str))
check('is_admin returns a bool', isinstance(system.is_admin(), bool))
check('can_be_elevated is False off Windows', system.can_be_elevated() is False)

outcome = system.elevate_and_wait()
check('elevate_and_wait is safe off Windows', outcome.started is False, outcome.detail)

lock_path = Path(log.log_dir()) / 'test.lock'
if lock_path.exists():
    lock_path.unlink()
first = system.InstanceLock(lock_path)
second = system.InstanceLock(lock_path)
check('first instance acquires the lock', first.acquire() is True)
check('second concurrent instance is refused', second.acquire() is False)
first.release()
check('lock file removed on release', not lock_path.exists())
third = system.InstanceLock(lock_path)
check('lock is reusable after release', third.acquire() is True)
third.release()

lock_path.write_text('999999', encoding='utf-8')
fourth = system.InstanceLock(lock_path)
check('stale lock from a dead pid is reclaimed', fourth.acquire() is True)
fourth.release()

print('elevation gate in simulate mode')
guarded = main_module.Application(['--simulate', '--no-restart-prompt'])
check('simulation skips the admin gate', guarded._ensure_admin() is None)

child = main_module.Application(['--no-restart-prompt', '--elevated'])
check('elevated flag is detected', child.elevated_child is True)
check(
    'non elevated child continues instead of prompting again',
    child._ensure_admin() is None,
)

no_elevate = main_module.Application(['--no-restart-prompt', '--no-elevate'])
check('no-elevate flag is detected', no_elevate.no_elevate is True)
check('no-elevate continues without prompting', no_elevate._ensure_admin() is None)

print()
if failures:
    print(f'{len(failures)} test(s) failed: {failures}')
    sys.exit(1)
print('all tests passed')
