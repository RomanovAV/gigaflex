# Краткая инструкция для тестирования на реальных задачах

Этот документ для коллег, которые хотят попробовать `gigalphex` на рабочих
задачах и быстро понять, какой сценарий считается нормальным.

## Что делает проект

`gigalphex` - это небольшой Python-раннер поверх GigaCode CLI. Он не заменяет
GigaCode, а задает ему рабочий цикл:

1. Принять обычный markdown-план, готовый Superpowers implementation plan или
   локальную OpenSpec change-директорию.
2. В обычном плане найти первый незавершенный раздел `### Task N:`,
   `### Iteration N:`, `### Задача N:` или `### Итерация N:`; в OpenSpec —
   следующую незавершённую группу из `tasks.md`.
3. Запустить GigaCode на выполнение ровно одного такого раздела.
4. Попросить GigaCode изменить код, обновить тесты, выполнить проверки,
   отметить готовые чекбоксы `[x]` или OpenSpec marker и сделать git-коммит.
5. Повторять, пока задачи не закончатся.
6. После полного выполнения запустить ревью: пять специализированных
   review-агентов, затем synthesis/fix-проход.

Главный артефакт работы — plan-файл с чекбоксами или OpenSpec `tasks.md`.
Текущий статус виден в self-contained HTML dashboard, а полный след исполнения
остаётся в progress log внутри `.gigalphex/progress/`.

## Перед первым запуском

Нужно, чтобы `gigacode` был установлен, залогинен и доступен в `PATH`:

```bash
command -v gigacode
gigacode --version
```

Из корня этого репозитория можно проверить сам `gigalphex`:

```bash
PYTHONPATH=python python3 -m unittest discover -s tests
PYTHONPATH=python python3 -m gigalphex.cli --init
```

Unit-тесты не запускают `gigacode` и не должны выполнять системные команды
перезагрузки. Если после этой команды машина или IDE/терминал перезапустились,
сохраните версию ОС, `python3 --version`, последнюю видимую строку вывода и
уточните, это был полный reboot ОС или перезапуск сессии/терминала. До
выяснения причины безопаснее повторять проверку в чистом клоне или отдельной
VM/контейнере.

Глобальные конфиг и шаблоны промптов автоматически создаются в
`~/.config/gigalphex/`. Если эта директория недоступна для записи, CLI создаёт
`.gigalphex/config` и `.gigalphex/prompts/` в текущем проекте. Если для
конкретного проекта нужны свои версии промптов, создайте локальные
переопределения:

```bash
PYTHONPATH=python python3 -m gigalphex.cli --init-prompts
```

Если тестируете `gigalphex` на другом проекте без установки пакета, запускайте
его из корня целевого проекта с абсолютным `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --init
```

## Рекомендуемый сценарий: план -> проверка -> выполнение

Один раз установите встроенный skill планирования:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --install-planning-skill
```

По умолчанию он устанавливается в
`~/.gigacode/skills/planning/SKILL.md`. Если ваша версия GigaCode использует
другой каталог, передайте `--skill-dir PATH`.

1. Сформулируйте задачу как обычный рабочий запрос:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --plan "добавить health check endpoint"
```

В обычном терминале команда запускает установленный в GigaCode skill
`planning`: он исследует репозиторий и задает уточняющие вопросы. После
создания файла завершите сессию GigaCode, чтобы GigaLphEx проверил и при
необходимости закоммитил план.

Для прежней одношаговой генерации без skill используйте:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --plan "добавить health check endpoint" --quick
```

При запуске без TTY, например в CI, quick-режим выбирается автоматически.

2. Откройте созданный файл в `docs/plans/`. Хороший план обычно содержит:

- краткие `Overview` и `Context` либо `Обзор` и `Контекст`;
- 2-6 независимых разделов `### Task N:` или `### Задача N:`;
- конкретные чекбоксы `- [ ] ...`;
- тесты и validation-команды внутри задач или в разделе `Validation`.

3. Если план слишком крупный или расплывчатый, отредактируйте его вручную.
   Это нормальный паттерн работы: план - не святыня, а контракт для агента.

4. Запустите выполнение:

В командах ниже замените имя plan-файла на тот файл, который был создан или
отредактирован у вас.

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli docs/plans/20260617-add-health-check-endpoint.md
```

По умолчанию раннер создаст или переключит git-ветку из имени плана. После
успешного полного прогона plan-файл будет перенесен в
`docs/plans/completed/`.

## Если проект уже использует OpenSpec или Superpowers

OpenSpec change запускается целиком:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  --openspec openspec/changes/add-search
```

GigaLphex изменяет чеклист `tasks.md`, а `proposal.md`, `design.md` и
`specs/**/*.md` передаёт агенту как read-only контекст. Каждая группа
`## N. ...` выполняется отдельной итерацией. Локализованные task-секции без
чекбоксов тоже поддерживаются: после выполнения появляется явный
`- [x] N. <название>` marker. Change автоматически не архивируется; успешный
прогон печатает команду `openspec archive <change-name>`.

Готовый Superpowers implementation plan можно передать как обычный plan-файл:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  docs/superpowers/plans/20260701-add-search.md
```

## Как понимать результат

Смотрите четыре места:

- `git log --oneline --decorate -10` - появились ли коммиты по задачам и ревью.
- `git status --short` - остались ли незакоммиченные изменения.
- `.gigalphex/progress/status-<run>.html` — live dashboard с фазами, задачами,
  активными сессиями, retries, временем и известными токенами.
- `.gigalphex/progress/progress-<run>.txt` — подробный transcript и диагностика.

Рядом также лежит `status-<run>.json` с тем же состоянием для интеграций и
`stats-<run>.json` со статистикой попыток.

Нормальный успешный прогон обычно выглядит так:

- каждая задача из плана отмечена `[x]`;
- по задачам есть один или несколько коммитов `feat: ...`;
- review-проход либо сделал `fix: address review findings`, либо завершился
  сигналом `<<<GIGALPHEX:REVIEW_DONE>>>`;
- в progress log есть `<<<GIGALPHEX:ALL_TASKS_DONE>>>` или понятный путь к
  завершению;
- рабочее дерево чистое или содержит только ожидаемые локальные файлы.

## Полезные режимы

Посмотреть промпты без запуска GigaCode:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --dry-run docs/plans/20260617-add-health-check-endpoint.md
```

Выполнить только задачи, без review-фазы:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli docs/plans/20260617-add-health-check-endpoint.md --tasks-only
```

Повторно запустить review текущей ветки относительно базы, сохранённой при
создании execution-ветки:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --review
```

Запустить review текущей ветки относительно явно указанной ветки:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --review --base-ref develop
```

Если сохранённой базы ещё нет (например, ветка создана старой версией
GigaLphex), один раз укажите `--base-ref`. GigaLphex разрешит его в точный SHA
и сохранит для следующих запусков. По умолчанию review стартует только из
чистого рабочего дерева. Если нужно ревьюить незакоммиченные staged, unstaged
или untracked изменения, запускайте review явно с `--allow-dirty`:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli --review --base-ref develop --allow-dirty
```

В этом режиме review-промпты включают `git status --short`, committed diff,
`git diff --cached` и обычный `git diff`, чтобы локальные изменения попали в
контекст ревью.

Review-агенты работают только на чтение и возвращают замечания. Исправления,
тесты или подходящую проверку артефактов и коммит `fix: address review findings`
выполняет только synthesis.
Каждому замечанию runner назначает стабильный идентификатор `F001`, `F002`, ...
и передаёт synthesis компактный список файлов из замечаний. Synthesis обязан
вернуть ровно одно машинно проверяемое решение `fixed`, `rejected`, `confirmed`
или `blocked` для каждого идентификатора. Пропуски, дубликаты, новые ID и
неполные ledger запускают узкий автоматический reconciliation-проход по тем же
findings и файлам. Поясняющий текст вокруг полного набора блоков удаляется
детерминированно. Если повторный ledger тоже невалиден, runner завершает прогон
с явной ошибкой review-протокола, не запуская заново пять review-агентов.
Если все агенты вернули `NO FINDINGS`, synthesis вообще не запускается.
Источником истины служит набор решений, а не сигнал
`<<<GIGALPHEX:REVIEW_DONE>>>`: полностью `rejected` ledger завершает review,
`fixed` и `confirmed` запускают следующий независимый проход, а `blocked`
останавливает прогон с указанной причиной.
Review-агенты используют `review_model`, а synthesis — `task_model`, как и
основная реализация.
После успешного review по умолчанию выполняется finalize-проход. Отключить его
можно флагом `--no-finalize`.

Не переключать текущую ветку автоматически:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli docs/plans/20260617-add-health-check-endpoint.md --no-branch
```

Запустить в отдельном git worktree, чтобы меньше трогать текущий checkout:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli docs/plans/20260617-add-health-check-endpoint.md --worktree
```

Разрешить запуск с грязным рабочим деревом, если это осознанно:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli docs/plans/20260617-add-health-check-endpoint.md --allow-dirty
```

Включить корпоративную Jira-политику веток и коммитов:

```bash
PYTHONPATH=/path/to/gigalphex/python python3 -m gigalphex.cli \
  docs/plans/20260617-add-health-check-endpoint.md \
  --jira-task PROJ-123
```

Runner создаст ветку `feature/PROJ-123-...` и автоматически добавит
`PROJ-123 ` к новым commit subjects, если агент не сделал этого сам.

После каждого недемо-запуска смотрите путь в строке `statistics:`. CLI печатает
абсолютный путь к JSON-файлу статистики, например
`/repo/.gigalphex/progress/stats-review.json`; при interrupt файл всё равно
должен остаться со статусом `interrupted`.

Если выбранная через `*_model` модель отвечает `Model not found`, GigaLphex
повторяет тот же prompt без `--model` и использует default-модель GigaCode в
последующих вызовах. Событие fallback видно в progress log и dashboard.

## Паттерны хороших задач

Лучше всего подходят задачи, где можно быстро проверить результат:

- добавить маленькую фичу с тестами;
- исправить конкретный баг;
- улучшить обработку ошибки;
- обновить документацию вместе с поведением;
- сделать небольшой рефакторинг с сохранением тестов.

Плохо подходят для первого теста:

- задачи без понятного критерия готовности;
- большие архитектурные переделки на десятки файлов;
- задачи, где нельзя запускать тесты локально;
- изменения, требующие секретов, внешних доступов или ручных approvals;
- задачи, где нужно править файлы вне workspace GigaCode.

## Как писать plan-файл вручную

Минимальный формат:

```md
# Plan: Add health check endpoint

## Overview
Add a small endpoint that reports service health.

## Context
Inspect the web routing module and existing endpoint tests before editing.

### Task 1: Add endpoint
- [ ] Add the health check route.
- [ ] Add or update focused tests.
- [ ] Run the relevant test command.
- [ ] Commit the change.

### Task 2: Update documentation
- [ ] Document the endpoint.
- [ ] Run documentation or smoke validation if available.
- [ ] Commit the change.

## Validation
- run the service test suite
```

План можно полностью написать по-русски, включая заголовки:

```md
# План: Добавить health check endpoint

## Обзор
Добавить небольшой endpoint, сообщающий о состоянии сервиса.

## Контекст
Перед изменениями изучить роутинг и существующие тесты endpoint-ов.

### Задача 1: Добавить endpoint
- [ ] Добавить health check route.
- [ ] Добавить или обновить сфокусированные тесты.
- [ ] Запустить подходящую команду тестов.
- [ ] Сделать коммит.

## Проверка
- запустить тесты сервиса
```

Каждый `### Task N:` или `### Задача N:` должен быть независимо
коммитабельным. Если задача просит
сделать коммит, чекбокс должен становиться `[x]` только после успешного
`git commit`.

## Что фиксировать в обратной связи

Для полезного отчета достаточно коротко записать:

- какой проект и какая задача тестировались;
- команду запуска;
- ссылку или путь к plan-файлу;
- путь к progress log;
- что получилось хорошо;
- где агент застрял, ошибся или сделал лишнее;
- какие команды проверки запускались и чем завершились.

Особенно ценны кейсы, где план был хороший, но выполнение сломалось: такие
примеры помогают улучшать промпты, retry/timeout-настройки и контракт с
GigaCode.
