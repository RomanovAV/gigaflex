"""User choices for the ordinary CLI journey, separate from runner policy."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex

from .git import GitError, GitService
from .terminal import Choice, ask, ask_text, _display


@dataclass(frozen=True)
class Launch:
    directory: Path
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class StoppedRun:
    exit_code: int
    launch: Launch
    reason: str
    recovery: str = ""
    progress: str = ""
    needs_note: bool = False


def parse_continuation(command: str):
    """Decode our printed command into argv. Never execute it through a shell."""
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 5 or words[0] != 'cd' or words[2:4] != ['&&', 'gigaflex']:
        return None
    directory = Path(words[1])
    if not directory.is_absolute() or not directory.is_dir() or '--resume' not in words[4:]:
        return None
    return Launch(directory, tuple(words[4:]))


def _continuation_arguments(arguments, *, restart=False, note=None):
    result, skip = [], False
    for argument in arguments:
        if skip:
            skip = False
        elif argument == '--resume-note':
            skip = True
        elif argument not in {'--resume', '--restart'} and not argument.startswith('--resume-note='):
            result.append(argument)
    result.append('--restart' if restart else '--resume')
    if note:
        result.extend(('--resume-note', note))
    return tuple(result)


def recovery_choice(stopped: StoppedRun):
    print('\n' + _display(stopped.reason, 500))
    while True:
        action = ask('Что сделать дальше?', [
            Choice('resume', 'Причина устранена — продолжить', 'Сохранённая работа будет проверена и продолжена.'),
            Choice('note', 'Добавить пояснение и продолжить', 'Например, что исправлено или какое решение принято.'),
            Choice('details', 'Показать подробности', 'Причина остановки и расположение сохранённой работы.'),
            Choice('restart', 'Начать заново с текущего состояния',
                   'Сохранённая работа останется отдельно и не будет перенесена автоматически.'),
            Choice('exit', 'Выйти, сохранив работу', 'Продолжить можно позже.'),
        ], default='exit')
        if action in {None, 'exit'}:
            return None
        if action == 'details':
            print(_display(stopped.reason, 3000))
            if stopped.recovery:
                print('Сохранённая работа: ' + _display(stopped.recovery, 500))
            print('Подробный лог: ' + _display(stopped.progress, 500))
            continue
        note = None
        if action == 'note':
            note = ask_text('Что изменилось или что нужно учесть? Пустой ответ — назад.')
            if note is None:
                continue
        elif action == 'resume' and stopped.needs_note:
            note = 'Operator confirmed in the terminal menu that the reported cause has been resolved.'
        return Launch(stopped.launch.directory,
                      _continuation_arguments(stopped.launch.arguments, restart=action == 'restart', note=note))


def saved_runs(progress: Path):
    results = []
    for path in sorted(progress.glob('checkpoint-*.json')):
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            stopped = state.get('blocked', {})
            launch = parse_continuation(stopped.get('resume_command', ''))
            if launch:
                results.append((path.stem.removeprefix('checkpoint-'), launch))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return results


def choose_path(root: Path, *, openspec: bool):
    if openspec:
        candidates = sorted(path.parent for path in (root / 'openspec/changes').glob('*/tasks.md')
                            if path.parent.name != 'archive' and not path.parent.name.startswith('.'))
    else:
        candidates = sorted({path for directory in ('docs/plans', 'docs/superpowers/plans', 'plans')
                             for path in (root / directory).rglob('*.md') if 'completed' not in path.parts})
    choices = [Choice(str(path), str(path.relative_to(root))) for path in candidates]
    choices.extend((Choice('manual', 'Указать другой путь'), Choice('back', 'Назад')))
    selected = ask('Выберите OpenSpec change' if openspec else 'Выберите план', choices)
    if selected in {None, 'back'}:
        return None
    if selected == 'manual':
        value = ask_text('Путь к каталогу OpenSpec change:' if openspec else 'Путь к Markdown-плану:')
        return Path(value).expanduser().resolve() if value else None
    return Path(selected)


def startup_choice(directory: Path, progress: Path):
    git = GitService(directory)
    root = git.repo_root() if git.is_repo() else directory
    while True:
        stopped = saved_runs(progress)
        choices = ([Choice('resume', 'Продолжить остановленную работу', f'Доступно запусков: {len(stopped)}')]
                   if stopped else [])
        choices.extend([
            Choice('openspec', 'Выполнить OpenSpec change'),
            Choice('plan', 'Выполнить Markdown-план'),
            Choice('review', 'Проверить текущие изменения'),
            Choice('create', 'Создать план'),
            Choice('exit', 'Выйти'),
        ])
        action = ask('GigaFlex — что хотите сделать?', choices)
        if action in {None, 'exit'}:
            return None
        if action == 'resume':
            selected = ask('Какую работу продолжить?', [Choice(str(i), name, str(launch.directory))
                                                       for i, (name, launch) in enumerate(stopped)]
                           + [Choice('back', 'Назад')])
            if selected not in {None, 'back'}:
                return stopped[int(selected)][1]
        elif action in {'openspec', 'plan'}:
            path = choose_path(root, openspec=action == 'openspec')
            if path is not None:
                arguments = ('--openspec', str(path)) if action == 'openspec' else (str(path),)
                return Launch(directory, arguments)
        elif action == 'review':
            return Launch(directory, ('--review',))
        elif action == 'create':
            request = ask_text('Что нужно сделать? Опишите задачу для плана:')
            if request:
                return Launch(directory, ('--plan', request))


def choose_review_base(git: GitService):
    output = git.run('for-each-ref', '--format=%(refname:short)', 'refs/heads/', 'refs/remotes/').stdout
    references = [ref for ref in output.splitlines() if not ref.endswith('/HEAD')]
    references.sort(key=lambda ref: (ref not in {'main', 'master', 'develop'}, ref))
    while True:
        value = ask('С чем сравнить изменения?', [Choice('ref:' + ref, ref) for ref in references]
                    + [Choice('manual', 'Указать ветку или коммит'), Choice('cancel', 'Отмена')])
        if value in {None, 'cancel'}:
            return None
        if value == 'manual':
            value = ask_text('Ветка, тег или commit SHA для сравнения:')
            if not value:
                continue
        else:
            value = value.removeprefix('ref:')
        try:
            git.resolve_commit(value)
            return value
        except GitError as error:
            print(_display(str(error), 300))
