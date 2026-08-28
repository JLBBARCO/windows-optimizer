"""Windows Optimizer - core application entry point.

Execution contract
------------------
Every command declared in ``json/commands.json`` is executed exactly once, in
declaration order, and no single command can prevent the others from running:

* each command runs in an isolated child process with streamed output, a hard
  watchdog and a process-tree kill on timeout (``lib/runner``);
* the Explorer icon-cache maintenance runs at the END of the plan, so a shell
  restart problem can no longer swallow the rest of the plan;
* the paths used by the application itself (the source tree staged by
  ``run.ps1``, the Python interpreter and the log folder) are exported through
  ``%WO_PROTECTED_PATHS%`` and excluded from the TEMP cleanup, which was the
  reason the process died mid-plan;
* the run always finishes with an explicit per-command report.

Command line flags
------------------
``--simulate``           resolve and report the plan without executing anything
``--no-explorer``        skip the Explorer icon-cache maintenance workflow
``--no-restart-prompt``  never show the "restart now?" dialog
``--only <name>``        run only commands whose name/command contains <name>
``--log-dir <path>``     write log.log/terminal.log into <path>
``--diagnose``           print the UAC/token diagnosis and exit (runs nothing)
``--no-elevate``         never request UAC (also via ``%WO_NO_ELEVATE%``)
``--elevated``           internal flag set when relaunching through UAC
``--create-shortcut``    create/refresh the Desktop shortcut and exit
``--remove-shortcut``    delete the Desktop shortcut and exit
``--no-shortcut``        do not touch the Desktop shortcut during a normal run
``--shortcut-branch``    ``main`` or ``beta``; defaults to ``%WO_BRANCH%``

Desktop shortcut
----------------
The shortcut created by ``lib/shortcut`` never points to a local copy of the
application: it runs ``irm <raw run.ps1 of the channel> | iex`` in PowerShell, so
every launch resolves the newest release (``main``) or pre-release (``beta``)
again. It is created by the NON elevated parent process, because the elevated
child may belong to a different user and therefore to a different Desktop.

Elevation
---------
When the process is not elevated the parent asks for UAC, then **waits** for the
elevated child and returns its exit code. Both processes share the same log
folder (``%WO_LOG_DIR%``), and the real token state (elevation type, integrity
level) is written to the log on every start, so "it says it is not admin" can be
diagnosed instead of guessed.
"""

import sys

from lib import json as json_lib
from lib import log, shortcut, system
from lib.runner import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    STATUS_WARNING,
    CommandResult,
    CommandRunner,
    CommandSpec,
)

DEFAULT_GROUP_ORDER = ['cmd', 'powershell']


class Application:
    def __init__(self, argv=None):
        self.argv = list(argv if argv is not None else sys.argv[1:])
        self.simulate = '--simulate' in self.argv or '--dry-run' in self.argv
        self.skip_explorer = '--no-explorer' in self.argv
        self.skip_restart_prompt = '--no-restart-prompt' in self.argv
        self.only = self._read_option('--only')
        self.log_dir = self._read_option('--log-dir')
        self.elevated_child = system.ELEVATED_FLAG in self.argv
        self.no_elevate = '--no-elevate' in self.argv
        self.diagnose = '--diagnose' in self.argv
        self.create_shortcut_only = '--create-shortcut' in self.argv
        self.remove_shortcut_only = '--remove-shortcut' in self.argv
        self.skip_shortcut = '--no-shortcut' in self.argv
        self.shortcut_branch = self._read_option('--shortcut-branch')
        self.lock = None

        self.commands = {}
        self.commands_path = None
        self.results = []
        self.runner = CommandRunner(simulate=self.simulate)

    # --------------------------------------------------------------- lifecycle
    def run(self):
        log.configure(log_directory=self.log_dir)
        log._new_line()
        log.info('Start System')
        log.info(f'Log files: {log.log_paths()[0]} | {log.log_paths()[1]}')
        log.info(f'Token state: {system.token_report()}')
        log.info(f'Role: {"elevated child" if self.elevated_child else "parent"}')

        if self.diagnose:
            self._diagnose()
            log.info('End System')
            return 0

        if self.remove_shortcut_only:
            removed = shortcut.remove_shortcut(self.shortcut_branch)
            log.info('End System')
            return 0 if removed else 1

        if self.create_shortcut_only:
            created = shortcut.create_shortcut(self.shortcut_branch, overwrite=True)
            log.info('End System')
            return 0 if created else 1

        self._ensure_desktop_shortcut()

        if not self._load_commands():
            log.info('End System')
            return 1

        elevation_exit = self._ensure_admin()
        if elevation_exit is not None:
            log.info('End System')
            return elevation_exit

        if not self._acquire_lock():
            log.info('End System')
            return 3

        try:
            return self._run_plan()
        finally:
            if self.lock is not None:
                self.lock.release()

    def _run_plan(self):
        protected = system.export_protection_environment()
        log.info(f'Protected paths (never deleted): {protected}')

        specs = self._build_plan()
        if not specs:
            log.warning('No command to execute after normalization')
        else:
            log.info(f'Execution plan contains {len(specs)} command(s)')

        try:
            self._execute(specs)
            if not self.skip_explorer:
                self._explorer_icon_cache_workflow()
            else:
                log.info('Explorer icon cache workflow skipped by flag')
        finally:
            if not self.simulate:
                system.ensure_explorer_running(self.runner)
            self._report()

        if not self.simulate and not self.skip_restart_prompt:
            if system.ask_for_restart():
                system.restart_pc()

        log.info('End System')
        return 0 if not self._has_failure() else 2

    # ---------------------------------------------------------------- elevation
    def _ensure_admin(self):
        """Return an exit code when this process should stop, else ``None``."""
        if self.simulate:
            log.info('Simulation mode: administrator privileges are not required')
            return None

        if system.is_admin():
            log.info('Administrator privileges confirmed on this process')
            return None

        if self.elevated_child:
            # The UAC prompt was accepted but the child token is still limited.
            log.error(
                'This process was relaunched through UAC but the token is NOT elevated. '
                'The most common causes are: the account is a standard user (the UAC '
                'dialog asked for another administrator credential and that other user '
                'was elevated instead), a group policy filtering the token, or the '
                'launcher being started by a different user session.'
            )
            log.error(f'Token state of this non-elevated child: {system.token_report()}')
            log.warning(
                'Continuing WITHOUT administrator privileges: DISM, SFC, chkdsk and '
                'winget upgrades will very likely fail. To fix this, right-click the '
                'Desktop shortcut (or PowerShell) and choose "Run as administrator" '
                'while logged in with an administrator account.'
            )
            return None

        if self.no_elevate:
            log.warning(
                'Elevation disabled by --no-elevate: elevated commands may fail.'
            )
            return None

        if not system.can_be_elevated():
            log.warning(
                'The current token cannot be elevated through UAC on this account '
                f'({system.token_report()}). Continuing without administrator privileges.'
            )
            return None

        outcome = system.elevate_and_wait(wait=True)

        if outcome.refused:
            log.error(
                'Administrator privileges were denied at the UAC prompt, so the '
                'maintenance plan was not executed.'
            )
            return 4

        if outcome.started:
            log.info(
                'The maintenance plan ran in the elevated process; this parent process '
                'only supervised it.'
            )
            return outcome.exit_code if outcome.exit_code is not None else 0

        log.warning(
            f'Elevation was not possible ({outcome.detail}). Continuing without '
            'administrator privileges: commands that require elevation may fail.'
        )
        return None

    # ---------------------------------------------------------------- shortcut
    def _ensure_desktop_shortcut(self):
        """Create the Desktop shortcut when missing (parent process only)."""
        if self.skip_shortcut:
            log.info('Desktop shortcut handling skipped by --no-shortcut')
            return

        if self.simulate:
            log.info(
                'Simulation mode: Desktop shortcut would point to '
                f'"{shortcut.launcher_command(self.shortcut_branch)}"'
            )
            return

        if self.elevated_child:
            # The elevated process can belong to another user, whose Desktop is
            # not the one the user is looking at.
            log.info('Desktop shortcut handled by the parent process; skipping here')
            return

        try:
            shortcut.ensure_shortcut(self.shortcut_branch)
        except Exception as error:
            log.warning(f'Could not ensure the Desktop shortcut: {error}')

    def _diagnose(self):
        log.info('----- UAC diagnosis -----')
        log.info(f'Token state: {system.token_report()}')
        log.info(f'is_admin(): {system.is_admin()}')
        log.info(f'elevation_type(): {system.elevation_type()}')
        log.info(f'can_be_elevated(): {system.can_be_elevated()}')

        if system.is_admin():
            log.info('Diagnosis: this process IS elevated; the plan would run fully.')
        elif system.elevation_type() == 3:
            log.info(
                'Diagnosis: administrator account with a filtered token. A UAC consent '
                'prompt is enough and elevation should succeed.'
            )
        elif system.elevation_type() == 1:
            log.warning(
                'Diagnosis: no split token. Either UAC is disabled or this is a standard '
                'user account. In the second case the UAC dialog asks for ANOTHER '
                'administrator credential and the elevated process runs as that other '
                'user, which is why the plan can still report missing privileges.'
            )
        else:
            log.warning('Diagnosis: could not read the token elevation type.')
        log.info('-------------------------')

    def _acquire_lock(self):
        if self.simulate:
            return True
        self.lock = system.InstanceLock(log.log_dir() / 'windows-optimizer.lock')
        return self.lock.acquire()

    # ------------------------------------------------------------------- setup
    def _read_option(self, flag):
        if flag in self.argv:
            index = self.argv.index(flag)
            if index + 1 < len(self.argv):
                return self.argv[index + 1]
        for argument in self.argv:
            if argument.startswith(flag + '='):
                return argument.split('=', 1)[1]
        return None

    def _load_commands(self):
        try:
            self.commands, self.commands_path = json_lib.read_commands()
            log.info(f'Read commands.json ({self.commands_path})')
            return True
        except Exception as error:
            log.error(f'Error reading commands.json: {error}')
            return False

    def _iter_raw_commands(self):
        """Yield ``(group, raw_item)`` honouring the declared order."""
        data = self.commands

        for key in ('plan', 'commands'):
            entries = data.get(key)
            if isinstance(entries, list):
                for item in entries:
                    group = ''
                    if isinstance(item, dict):
                        group = str(item.get('group', key))
                    yield group or key, item
                return

        group_order = [
            group
            for group in (data.get('order') or DEFAULT_GROUP_ORDER)
            if isinstance(group, str)
        ]
        for group in sorted(data.keys()):
            if group in ('order', 'settings') or group.startswith('$'):
                continue
            if group not in group_order:
                group_order.append(group)

        for group in group_order:
            entries = data.get(group)
            if not isinstance(entries, list):
                continue
            for item in entries:
                yield group, item

    def _build_plan(self):
        placeholders = system.placeholders()
        log.info(f'Resolved winget command: {placeholders["winget"]}')

        specs = []
        for index, (group, raw) in enumerate(self._iter_raw_commands()):
            if not isinstance(raw, (dict, str)):
                log.error(
                    f'Skip invalid command item in group "{group}": expected object or string'
                )
                continue

            spec = CommandSpec(raw, group=group, index=index, placeholders=placeholders)

            if not spec.command:
                log.warning(f'Skip empty command in group "{group}"')
                continue

            if self._is_explorer_control_command(spec.command):
                log.warning(
                    f'Command "{spec.label}" is handled by the dedicated internal '
                    'Explorer maintenance workflow and was removed from the JSON plan.'
                )
                continue

            if self.only and self.only.lower() not in spec.label.lower():
                log.info(f'Skip command "{spec.label}" (filtered by --only {self.only})')
                continue

            specs.append(spec)

        return specs

    def _is_explorer_control_command(self, command):
        lower = command.strip().lower()
        return (
            'iconcache' in lower
            or lower.startswith('taskkill /f /im explorer.exe')
            or (lower.startswith('start ') and 'explorer.exe' in lower)
        )

    # --------------------------------------------------------------- execution
    def _execute(self, specs):
        for spec in specs:
            try:
                result = self.runner.run(spec)
            except KeyboardInterrupt:
                log.error(
                    f'Command "{spec.label}" was interrupted (KeyboardInterrupt) '
                    'and was skipped'
                )
                result = CommandResult(spec, STATUS_SKIPPED, detail='interrupted')
            except Exception as error:
                # Absolute last resort: a bug in the runner must not stop the plan.
                log.error(f'Unexpected error "{error}" running "{spec.label}"')
                result = CommandResult(spec, STATUS_FAILED, detail=str(error))
            self.results.append(result)

    def _explorer_icon_cache_workflow(self):
        log.info('Run dedicated Explorer icon cache maintenance workflow')

        steps = [
            (
                'Stop Explorer shell',
                'taskkill /f /im explorer.exe',
                30,
                {0, 128},
            ),
            (
                'Delete IconCache.db',
                'del /f /q "%LOCALAPPDATA%\\IconCache.db"',
                30,
                {0, 1},
            ),
            (
                'Delete Explorer IconCache_*.db files',
                'del /f /q "%LOCALAPPDATA%\\Microsoft\\Windows\\Explorer\\IconCache_*.db"',
                30,
                {0, 1},
            ),
        ]

        for label, command, timeout, accepted in steps:
            result = self.runner.run_raw(
                label, command, timeout, accepted_exit_codes=accepted, optional=True
            )
            self.results.append(result)

        if self.simulate:
            return

        if system.restart_explorer(self.runner):
            log.info('Dedicated Explorer maintenance workflow completed')
        else:
            log.error('Dedicated Explorer maintenance workflow could not restart Explorer')

    # ------------------------------------------------------------------ report
    def _has_failure(self):
        return any(
            result.status in (STATUS_FAILED, STATUS_TIMEOUT) for result in self.results
        )

    def _report(self):
        log.info('----- Execution report -----')
        counters = {
            STATUS_OK: 0,
            STATUS_WARNING: 0,
            STATUS_FAILED: 0,
            STATUS_TIMEOUT: 0,
            STATUS_SKIPPED: 0,
        }

        for result in self.results:
            counters[result.status] = counters.get(result.status, 0) + 1
            code = '-' if result.return_code is None else result.return_code
            log.info(f'[{result.status}] (exit={code}) {result.spec.label}')

        summary = ', '.join(f'{status}={count}' for status, count in counters.items())
        log.info(f'Totals: {summary}')
        log.info('----------------------------')


def main(argv=None):
    try:
        return Application(argv).run()
    except KeyboardInterrupt:
        log.error('System interrupted (KeyboardInterrupt) before finishing')
        return 1
    except Exception as error:
        log.error(f'Fatal error "{error}"')
        return 1


if __name__ == '__main__':
    sys.exit(main())
