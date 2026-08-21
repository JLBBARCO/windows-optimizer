import ctypes
import os
import subprocess
import sys
from pathlib import Path

from lib import json, log

class main():
    def __init__(self):
        log._new_line()
        log.info('Start System')
        self.commands = {}
        try:
            commands_path = Path(__file__).resolve().parent / 'json' / 'commands.json'
            self.commands = json.read_json(commands_path)
            log.info('Read commands.json')
        except Exception as error:
            log.error(f'Error reading commands.json: {error}')

        if self.commands and self._is_admin():
            self._run_commands()

        log.info('End System')


    def _is_admin(self):
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            is_admin = False

        if is_admin:
            return True

        if getattr(sys, 'frozen', False):
            executable = sys.executable
            params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
        else:
            executable = sys.executable
            params = " ".join(
                [f'"{os.path.abspath(sys.argv[0])}"']
                + [f'"{arg}"' for arg in sys.argv[1:]]
            )

        result = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', executable, params, None, 1
        )
        if result <= 32:
            raise OSError(f'Unable to elevate process (ShellExecuteW={result})')

        log.info('Request administrator privileges')
        sys.exit()


    def _run_commands(self):
        log.info('Run commands to CMD')
        self._run_command_group_safe('cmd', ['cmd.exe', '/d', '/c'])

        log.info('Run commands to PowerShell')
        self._run_command_group_safe(
            'powershell',
            [
                'powershell.exe',
                '-NoProfile',
                '-NonInteractive',
                '-ExecutionPolicy',
                'Bypass',
                '-Command',
            ],
        )


    def _run_command_group_safe(self, group, runner):
        # Garante que uma falha inesperada em um grupo inteiro (fora do
        # try/except por comando, ex.: um erro ao montar a lista de
        # comandos) nao impeca o outro grupo de rodar.
        try:
            self._run_command_group(group, runner)
        except KeyboardInterrupt:
            log.error(f'Group "{group}" was interrupted (KeyboardInterrupt) and was skipped')
        except Exception as error:
            log.error(f'Error "{error}" running command group "{group}"')

    def _run_command_group(self, group, runner):
        for item in self.commands.get(group, []):
            command = item.get('command', '').strip()
            if not command:
                log.warning(f'Skip empty command in group "{group}"')
                continue

            shell = str(item.get('shell', group)).strip().lower()
            input_data = item.get('input', '')
            input_mode = str(item.get('input_mode', 'stdin')).strip().lower()
            timeout = item.get('timeout', 3600)
            command_runner = self._resolve_runner(shell, runner)

            try:
                log.info(f'Start command "{command}"')

                if self._is_background_launch(command):
                    # Comandos como "start ... explorer.exe" iniciam um
                    # processo de longa duração (GUI) que, se herdar os
                    # pipes de stdout/stderr do subprocess.run(...), nunca
                    # os fecha -> subprocess.run() trava para sempre
                    # esperando EOF. Por isso disparamos com Popen,
                    # sem herdar pipes e sem esperar o processo terminar.
                    self._launch_detached(command_runner, command)
                    log.info(f'Run command "{command}"')
                    continue

                if input_data and input_mode == 'pipe':
                    result = subprocess.run(
                        f'cmd.exe /d /c {self._build_piped_command(command, input_data)}',
                        check=False,
                        encoding='oem',
                        errors='replace',
                        capture_output=True,
                        timeout=timeout,
                        # Isola o filho no proprio grupo de console. Sem isso,
                        # cmd.exe/del/taskkill/chkdsk/etc ficam no MESMO grupo
                        # de console do processo Python. Qualquer evento de
                        # interrupcao que chegue ao console (ex.: um utilitario
                        # nativo mexendo no modo do console, perda de foco,
                        # um dialogo do UAC) e transmitido para o grupo
                        # inteiro, inclusive para o proprio script Python,
                        # derrubando-o com "KeyboardInterrupt" mesmo sem o
                        # usuario ter apertado Ctrl+C.
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                else:
                    # IMPORTANTE: passamos a linha de comando como STRING,
                    # nao como lista. Comandos como o "del" ja contem aspas
                    # internas (ex.: del /f /q "%LOCALAPPDATA%\IconCache.db").
                    # Se usarmos uma lista (command_runner + [command]), o
                    # Python reescapa essas aspas via list2cmdline no Windows,
                    # gerando algo como: cmd.exe /d /c "del /f /q \"...\""
                    # que o cmd.exe nao consegue interpretar corretamente
                    # (erro "A sintaxe do nome do arquivo... esta incorreta").
                    # Como string, o Windows usa a linha de comando tal como
                    # foi escrita no commands.json.
                    full_cmdline = ' '.join(command_runner) + f' {command}'
                    result = subprocess.run(
                        full_cmdline,
                        check=False,
                        encoding='oem',
                        errors='replace',
                        capture_output=True,
                        input=input_data,
                        timeout=timeout,
                        # Ver comentario acima (bloco "pipe"): isola o filho
                        # do grupo de console do processo pai.
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    )

                log.terminal_output(result.stdout)
                log.terminal_output(result.stderr)

                if result.returncode == 0:
                    log.info(f'Run command "{command}"')
                else:
                    log.error(
                        f'Command "{command}" exited with code {result.returncode}: '
                        f'{(result.stderr or result.stdout).strip()[-1000:]}'
                    )
            except subprocess.TimeoutExpired:
                log.error(f'Command "{command}" exceeded timeout of {timeout} seconds')
            except KeyboardInterrupt:
                # KeyboardInterrupt herda de BaseException, nao de Exception,
                # entao NUNCA seria capturado pelo "except Exception" abaixo.
                # Registramos o evento, mas NAO propagamos (sem "raise"):
                # uma interrupcao pontual durante UM comando nao pode
                # cancelar os comandos seguintes nem os outros grupos
                # (ex.: sfc/DISM/chkdsk/winget). Cada comando e tratado
                # como independente; se o usuario realmente quiser abortar
                # tudo, fechar o terminal ainda funciona (isso nao pode
                # ser interceptado de dentro do Python de qualquer forma).
                log.error(
                    f'Command "{command}" was interrupted (KeyboardInterrupt) and was skipped'
                )
            except Exception as error:
                log.error(f'Error "{error}" to run the command "{command}"')


    def _resolve_runner(self, shell, fallback_runner):
        if shell == 'cmd':
            return ['cmd.exe', '/d', '/c']

        if shell == 'powershell':
            return [
                'powershell.exe',
                '-NoProfile',
                '-NonInteractive',
                '-ExecutionPolicy',
                'Bypass',
                '-Command',
            ]

        return fallback_runner


    def _is_background_launch(self, command):
        # Detecta comandos do tipo "start ..." que disparam um processo
        # GUI/longa duração e não devem ser aguardados nem ter stdout/
        # stderr capturados via pipe (ver comentário em _run_command_group).
        return command.strip().lower().startswith('start ')


    def _launch_detached(self, command_runner, command):
        full_cmdline = ' '.join(command_runner) + f' {command}'
        subprocess.Popen(
            full_cmdline,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )


    def _build_piped_command(self, command, input_data):
        escaped_input = self._escape_cmd_value(str(input_data).rstrip('\n'))
        return f'echo {escaped_input}| {command}'


    def _escape_cmd_value(self, value):
        return (
            value.replace('^', '^^')
            .replace('&', '^&')
            .replace('|', '^|')
            .replace('<', '^<')
            .replace('>', '^>')
        )


    def _restart_pc(self):
        try:
            subprocess.run(
                ['shutdown.exe', '/r', '/f', '/t', '30'],
                check=False,
            )
            log.info('Restart PC in 30 seconds')
        except Exception as error:
            log.error(f'Error "{error}" to restart PC')


if __name__ == '__main__':
    try:
        program = main()
    except KeyboardInterrupt:
        log.error('System interrupted (KeyboardInterrupt) before finishing')
        sys.exit(1)