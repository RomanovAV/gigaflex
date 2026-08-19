# Диагностика shell approval: GigaCode или GigaFlex

> Исторический сценарий регрессии для GigaCode 26.5.17. Он остаётся полезен
> именно для диагностики non-interactive shell approval; актуальные способы
> запуска OpenSpec, dashboard и общий обзор CLI описаны в `README.md` и
> `docs/real-task-testing-guide.md`.

Сценарий предназначен для быстрой проверки 22 июня 2026 года. Он последовательно
проверяет сам GigaCode, запуск с захваченным выводом, Python executor и полный
цикл GigaFlex.

Ожидаемое время: 10–15 минут.

## 0. Подготовка

Укажите путь к актуальному checkout GigaFlex:

```bash
export GIGAFLEX_REPO="$HOME/work/gigaflex"
export CHECK_ROOT="$HOME/work/gigaflex-approval-check-20260622"
export RESULTS="$CHECK_ROOT/results"
mkdir -p "$RESULTS"
```

Проверьте, что используется нужная версия кода:

```bash
git -C "$GIGAFLEX_REPO" rev-parse HEAD | tee "$RESULTS/gigaflex-commit.txt"
git -C "$GIGAFLEX_REPO" status --short | tee "$RESULTS/gigaflex-status.txt"
command -v gigacode | tee "$RESULTS/gigacode-path.txt"
gigacode --version 2>&1 | tee "$RESULTS/gigacode-version.txt"
python3 --version 2>&1 | tee "$RESULTS/python-version.txt"
```

Если `git status` показывает незакоммиченные изменения, запишите это в итоговый
отчёт. Не переключайте коммиты посреди сценария.

Используемый тестовый prompt:

```bash
export SHELL_PROMPT='Вызови run_shell_command ровно один раз: выполни pwd. Затем напечатай строку SHELL_OK и полученный путь. Не используй другие инструменты.'
```

## 1. Проверить фактическую команду GigaFlex

Из любой директории выполните:

```bash
PYTHONPATH="$GIGAFLEX_REPO/python" python3 - <<'PY' | tee "$RESULTS/effective-command.txt"
from gigaflex.config import load_config
from gigaflex.executor import GigaCodeExecutor

cfg = load_config()
print("configured args:", cfg.gigacode_args)
print("resolved args:", cfg.resolved_args)
print("effective command:", GigaCodeExecutor(
    command=cfg.gigacode_command,
    args=cfg.args_for_phase("task"),
).command_line())
PY
```

Ожидаемая команда оканчивается на:

```text
--approval-mode=auto-edit --allowed-tools run_shell_command -p '<prompt>'
```

`-p '<prompt>'` должен находиться после массива `--allowed-tools`.

## 2. Проверить GigaCode напрямую в терминале

```bash
gigacode \
  --approval-mode=auto-edit \
  --allowed-tools run_shell_command \
  -p "$SHELL_PROMPT"
echo "DIRECT_EXIT=$?"
```

Успешный результат:

- нет `requires user approval`;
- напечатан `SHELL_OK`;
- показан текущий каталог;
- exit code равен `0`.

Если здесь появляется approval warning, остановите сценарий. Проблема
воспроизводится без GigaFlex: проверять нужно установку, версию, настройки или
корпоративную политику GigaCode на этом ноутбуке.

Запишите результат вручную:

```bash
printf '%s\n' 'PASS или FAIL, краткое описание' > "$RESULTS/02-direct-terminal.txt"
```

## 3. Проверить GigaCode с захваченным stdout

Этот запуск ближе к тому, как GigaFlex читает вывод дочернего процесса:

```bash
gigacode \
  --approval-mode=auto-edit \
  --allowed-tools run_shell_command \
  -p "$SHELL_PROMPT" \
  > "$RESULTS/03-direct-captured.log" 2>&1
echo "CAPTURED_EXIT=$?" | tee "$RESULTS/03-direct-captured-exit.txt"
cat "$RESULTS/03-direct-captured.log"
```

Интерпретация:

- шаг 2 прошёл, шаг 3 дал approval warning — GigaCode зависит от TTY или режима
  захвата вывода; GigaFlex потребуется PTY-режим;
- оба шага дали warning — проблема ниже уровня GigaFlex;
- оба шага прошли — продолжайте.

## 4. Запустить встроенную диагностику GigaFlex

Из пустой тестовой директории:

```bash
mkdir -p "$CHECK_ROOT/diagnose"
cd "$CHECK_ROOT/diagnose"
PYTHONPATH="$GIGAFLEX_REPO/python" \
  GIGAFLEX_DIAGNOSTIC_PROMPT="$SHELL_PROMPT" \
  python3 -m gigaflex.diagnose \
  2>&1 | tee "$RESULTS/04-diagnose.log"
```

Диагностика сравнивает:

1. прямой subprocess с унаследованным терминалом;
2. subprocess с захваченным stdout;
3. `GigaCodeExecutor`.

В конце команда напечатает каталог с детальными логами. Скопируйте его:

```bash
cp -R .gigaflex/diagnostics "$RESULTS/diagnostics"
```

Если первые два варианта успешны, а `GigaCodeExecutor` нет, проблема находится
в интеграционном коде GigaFlex.

## 5. Проверить полный цикл на минимальном репозитории

Создайте независимый репозиторий без Maven, Gradle и внешних зависимостей:

```bash
rm -rf "$CHECK_ROOT/smoke-repo"
mkdir -p "$CHECK_ROOT/smoke-repo/docs/plans"
cd "$CHECK_ROOT/smoke-repo"

git init
git config user.email "test@example.com"
git config user.name "GigaFlex Approval Check"

printf '# Approval smoke repository\n' > README.md

PYTHONPATH="$GIGAFLEX_REPO/python" \
  python3 -m gigaflex.cli --init
```

Создайте `docs/plans/20260622-approval-smoke.md`:

```bash
cat > docs/plans/20260622-approval-smoke.md <<'EOF'
# Plan: Shell approval smoke test

## Overview
Verify that GigaCode can execute shell commands through GigaFlex.

## Context
This repository has no build tools or external dependencies.

### Task 1: Verify shell execution
- [ ] Run `pwd` through the shell tool.
- [ ] Create `SHELL_OK.md` containing the returned working directory.
- [ ] Run `git status --short`.
- [ ] Mark completed items and commit the changes.

## Validation
- test -s SHELL_OK.md
- git status --short
EOF
```

Зафиксируйте исходное состояние:

```bash
git add README.md .gitignore .gigaflex/config docs/plans/20260622-approval-smoke.md
git commit -m "test: initialize approval smoke repository"
```

Запустите только task-фазу:

```bash
set -o pipefail
PYTHONPATH="$GIGAFLEX_REPO/python" \
  python3 -m gigaflex.cli \
  docs/plans/20260622-approval-smoke.md \
  --tasks-only \
  --no-branch \
  --no-move-plan \
  2>&1 | tee "$RESULTS/05-gigaflex-smoke.log"
echo "GIGAFLEX_EXIT=$?" | tee "$RESULTS/05-gigaflex-smoke-exit.txt"
```

Соберите состояние:

```bash
git log --oneline --decorate -5 | tee "$RESULTS/05-git-log.txt"
git status --short | tee "$RESULTS/05-git-status.txt"
cat SHELL_OK.md 2>&1 | tee "$RESULTS/05-shell-ok.txt"
cat docs/plans/20260622-approval-smoke.md \
  | tee "$RESULTS/05-plan-after.txt"
cp .gigaflex/progress/progress-20260622-approval-smoke.txt \
  "$RESULTS/05-progress.log"
```

Успешный результат:

- startup-команда имеет вид
  `gigacode --approval-mode=auto-edit --allowed-tools run_shell_command -p '<prompt>'`;
- progress log содержит этапы `event=prepared`, `event=started`,
  `event=first_output` и `event=finished`;
- approval warning отсутствует;
- существует непустой `SHELL_OK.md`;
- чекбоксы задачи отмечены;
- создан новый commit;
- команда завершилась без `TASK_FAILED`.

## 6. Итоговая классификация

| Результат | Где проблема |
|---|---|
| Прямой шаг 2 падает | GigaCode, настройки или политика ноутбука |
| Шаг 2 проходит, шаг 3 падает | GigaCode требует TTY при захваченном выводе |
| Шаги 2–3 проходят, executor в шаге 4 падает | `GigaCodeExecutor` |
| Шаг 4 проходит, минимальный smoke падает | orchestration/prompt GigaFlex |
| Минимальный smoke проходит, рабочий проект падает | окружение или инструменты рабочего проекта |
| Всё проходит | регрессия устранена |

Maven-ошибки, сетевые ограничения и `EPERM` в рабочем проекте оценивайте только
после успешного минимального smoke-теста. Они не доказывают проблему approval.

## 7. Что прислать для разбора

Архивируйте только результаты этого запуска:

```bash
cd "$CHECK_ROOT"
tar -czf gigaflex-approval-results-20260622.tar.gz results
echo "$CHECK_ROOT/gigaflex-approval-results-20260622.tar.gz"
```

Нужны:

- commit GigaFlex;
- версия и путь GigaCode;
- результаты шагов 2 и 3;
- `04-diagnose.log`;
- `05-gigaflex-smoke.log`;
- `05-progress.log`.

Не используйте старые progress-логи при классификации этого запуска.

Если approval warning появился, в `05-progress.log` дополнительно должны быть
`event=approval_warning_detected`, `event=terminating
reason=approval_unavailable` и `event=retry_stopped`. Полный prompt в
диагностических строках отсутствует; для сверки используется только
`prompt_chars`.
