# Как попробовать GigaLphex

Интерактивная версия этой инструкции:
[`how-to-try.html`](how-to-try.html).

GigaLphex выполняет задачу по частям: берет очередной раздел плана, запускает
GigaCode, проверяет результат, отмечает выполненные пункты и создает коммит.
После всех задач он запускает ревью.

## Подготовка

Нужны git-репозиторий и установленный, авторизованный `gigacode`. Команды ниже
запускайте из корня проекта, который хотите изменить:

[Скачать GigaLphex с GitHub](https://github.com/RomanovAV/gigalphex/archive/refs/heads/main.zip).

```bash
command -v gigacode
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --init
```

Перед первым запуском лучше выбрать небольшую задачу с понятным результатом и
тестами. Рабочее дерево должно быть чистым.

## Путь 1: OpenSpec

Подходит, если проект уже использует OpenSpec. Подготовьте change-директорию с
`tasks.md` обычным OpenSpec-процессом и передайте ее целиком:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  --openspec openspec/changes/add-dark-mode
```

GigaLphex использует `proposal.md`, `design.md` и `specs/**/*.md` как контекст,
а чекбоксы в `tasks.md` — как выполняемые задачи. После завершения он покажет
команду `openspec archive <change-name>`, но не архивирует change автоматически.
Если генератор создал локализованные task-секции без чекбоксов, GigaLphex тоже
сможет их выполнить и добавит под завершённой секцией устойчивый marker
`- [x] N. <название>`.

## Путь 2: Superpowers

Готовый implementation plan из Superpowers можно выполнить напрямую:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  docs/superpowers/plans/2026-07-01-add-dark-mode.md
```

Если есть только design spec, сначала установите skill-конвертер:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  --install-superpowers-converter-skill
```

Затем попросите GigaCode преобразовать spec в план:

```text
Use the superpowers-to-gigalphex skill to convert
docs/superpowers/specs/2026-07-01-add-dark-mode.md into docs/plans/add-dark-mode.md.
```

Получившийся файл запускается как обычный план.

## Путь 3: обычный план

Это самый простой путь, если OpenSpec и Superpowers в проекте не используются.
Один раз установите skill планирования:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  --install-planning-skill
```

Создайте план из текстового запроса:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  --plan "добавить health check endpoint"
```

Проверьте созданный файл в `docs/plans/` и запустите его:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  docs/plans/20260723-add-health-check-endpoint.md
```

Без planning skill можно сразу создать одношаговый план флагом `--quick`.
Также план можно написать вручную: каждая задача должна иметь заголовок
`### Task N:` или `### Задача N:` и чекбоксы `- [ ]`.

## Конфигурация

Команды выше намеренно не содержат необязательных параметров. GigaLphex читает
общие настройки из `~/.config/gigalphex/config`, а проектные переопределения —
из `.gigalphex/config`. Команда `--init` подготовит локальный config, если его
ещё нет.

## Где смотреть результат

- live dashboard — в `.gigalphex/progress/status-<run>.html`;
- тот же статус для интеграций — в `.gigalphex/progress/status-<run>.json`;
- подробный transcript — в `.gigalphex/progress/progress-<run>.txt`;
- созданные коммиты — через `git log --oneline`;
- оставшиеся изменения — через `git status --short`.
