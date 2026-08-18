# Как попробовать GigaLphex

Интерактивная версия этой инструкции:
[`how-to-try.html`](how-to-try.html).

Основной сценарий GigaLphex — автономное выполнение OpenSpec change. OpenSpec
задаёт контракт, а GigaLphex по очереди выполняет группы из `tasks.md`, проверяет
результат и создаёт отдельные коммиты. После всех задач весь накопленный change
проходит независимое ревью и финальную проверку.

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

## Основной путь: OpenSpec

Подходит, если проект уже использует OpenSpec. Подготовьте change-директорию с
`tasks.md` обычным OpenSpec-процессом и передайте ее целиком:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  --openspec openspec/changes/add-dark-mode
```

GigaLphex использует `proposal.md`, `design.md` и `specs/**/*.md` как read-only
контекст. Изменяемым чеклистом остаётся `tasks.md`: каждая группа `## N. ...`
выполняется как отдельная итерация и отдельный коммит. Если генератор создал
локализованные task-секции без чекбоксов, GigaLphex добавит под завершённой
секцией устойчивый marker `- [x] N. <название>`.

Перед началом runner сохраняет точный base commit. После выполнения всех групп
пять reviewer-агентов независимо проверяют общий diff change от этого commit до
текущего результата. Они читают изменённые файлы целиком и при необходимости
смотрят связанный код и тесты, но не проводят аудит всего проекта. Каждый
reviewer работает в отдельном временном worktree и только сообщает замечания.
Synthesis исправляет подтверждённые проблемы, после чего по умолчанию запускается
finalize.

Все существующие skills, rules, tools и настройки GigaCode продолжают работать
в каждой сессии: GigaLphex использует настроенный CLI и контекст целевого
проекта, а не заменяет командное окружение.

После успешного запуска GigaLphex покажет команду
`openspec archive <change-name>`, но не архивирует change автоматически. Сначала
проверьте итоговый diff и результаты в dashboard, затем выполните archive как
явный OpenSpec lifecycle step.

### Модели по этапам

Для разных фаз можно выбрать разные модели в `.gigalphex/config`:

```ini
[gigalphex]
plan_model =
task_model =
review_model =
finalize_model =
```

В OpenSpec-запуске `task_model` выполняет задачи и synthesis, `review_model`
используют read-only reviewers, а `finalize_model` — финальная проверка.
`plan_model` применяется при создании обычного плана через `--plan`. Если
настроенная модель недоступна, GigaLphex повторяет вызов с default-моделью
GigaCode.

## Дополнительный путь: Superpowers

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

## Дополнительный путь: обычный план

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

Без planning skill можно создать план one-shot вызовом с флагом `--quick`.
Такой план обычно содержит от двух до шести независимо выполняемых задач.
Также план можно написать вручную: каждая задача должна иметь заголовок
`### Task N:` или `### Задача N:` и чекбоксы `- [ ]`.

## Конфигурация

Команды выше намеренно не содержат необязательных параметров. GigaLphex читает
общие настройки из `~/.config/gigalphex/config`, а проектные переопределения —
из `.gigalphex/config`. Команда `--init` подготовит локальный config, если его
ещё нет. Для OpenSpec change отдельный planning skill не требуется.

## Где смотреть результат

- live dashboard — в `.gigalphex/progress/status-<run>.html`;
- тот же статус для интеграций — в `.gigalphex/progress/status-<run>.json`;
- подробный transcript — в `.gigalphex/progress/progress-<run>.txt`;
- статистика моделей, времени и токенов — в `.gigalphex/progress/stats-<run>.json`;
- созданные коммиты — через `git log --oneline`;
- оставшиеся изменения — через `git status --short`.
