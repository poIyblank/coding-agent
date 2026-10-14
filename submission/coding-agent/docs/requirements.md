# Где реализовано каждое требование

| Требование | Код | Проверки |
|---|---|---|
| Самостоятельный agent loop | `Runtime.drive`, `APIModel.complete` | HTTP-интеграция, demo full flow |
| Имена, описания, JSON Schema | `schema.py: tools_for_model`, `parse_arguments` | schemas, invalid schema/JSON, unknown tool |
| Несколько calls + исходные id | `accept`, `step`, `emit` | multiple calls, HTTP-протокол |
| Ошибка инструмента не роняет агент | `step` | tool error + успешное следующее чтение |
| Bash со stdout/stderr/exit code | `executor.py: run_bash` | demo, timeout, bounded output |
| Тесты не меняются | `read_only`, сравнение байтов в тестах | demo full flow, tests readonly, CLI |
| Safe/auto/удаление | `Policy.check`, `decide_action` | safe resume, denial, delete in auto |
| Остановка и сохранение на подтверждении | `wait_action`, `wait_plan` | CLI запускается заново между решениями |
| Policy отдельно от loop | `policy.py` | allow/deny, deny wins, forbidden cwd |
| Полная команда, аргументы и shell syntax | `Policy.command`, `SUPPORTED` | substitutions, redirects, `bash -c`, path args |
| Директории, `..`, абсолютный путь, ссылки | `Policy.path`, `_config_path` | escape, symlink, hardlink, denied subdir |
| Policy/state вне workspace | `Policy.__init__`, `Store.__init__` | config/state location, recheck after approval |
| Состояние по run_id | `storage.py`, `Runtime.create/load` | restore + CLI restart |
| Завершённые вызовы не повторяются | `calls`, `fingerprint` | same id cached, changed args protocol error |
| Started перед эффектом + результат после | `step`, `atomic_bytes` | crashes after charge/execute |
| Неопределённость и решения retry/fail/abort | `resolve`, `uncertain_diff` | crash recovery, abort, Bash cannot retry |
| План без эффектов до подтверждения | `Policy.check`, `set_plan` | plan blocks all side effects |
| Структура плана | `PLAN` schema | dependency cycle, reject/approve plan |
| Четыре общих бюджета | `require_budget`, `accept`, `step`, `emit` | каждый лимит, resume, crash after debit |
| Нехватка на всю пачку | preflight в `accept` | tool/side-effect batch all-or-nothing |
| Неизвестный размер результата | `result_cap`, `wire_result` | byte limit, Unicode, reserve rest of batch |
| Задачи и зависимости | `schedule`, `current_task` | wrong task id, dependency checks |
| Фактическое подтверждение результата | `verify_task` | cannot complete on model claim, real Bash |
| Три задачи и сохранение прогресса | `tasks`, `actual_result` | CLI restart между inspect/fix/test |
| Чеклист в stdout | `cli.checklist` | checklist test, CLI tasks |
| Создание/изменение/удаление в журнале | `journal.py`, `record_change` | creation/deletion, Bash-generated file |
| Проверка дерева до каждого эффекта | `step: before != expected_tree` | external change stops next side effect |
| Просмотр diff и откат после resume | `diff`, `rollback` | diff/rollback after restart |
| Конфликт до любых записей отката | preflight всех путей в `rollback` | two-file rollback conflict |
| Не трогать чужие файлы | journal of changed paths only | rollback preserves unrelated file |
| Разрешить uncertain перед откатом | `uncertain_record`, `resolve` | rollback blocked until adoption |
| Дополнительно: падение во время отката | persisted rollback intent | rollback crash recovery |
| Дополнительно: блокировка второго CLI | `Store.lock` | concurrent workspace lock |

Имена выше сокращены для читаемости; полные имена методов тестов находятся в
`tests/test_runtime.py` и `tests/test_integration.py`.

Текущая локальная проверка: 57 автоматических тестов прошли на Python 3.13 в macOS.
Bash внутри runtime запускал настоящий Python/unittest. HTTP-адаптер проверен
на локальном сервере с детерминированными ответами и в полном прогоне настоящей
Qwen3 Instruct: `real-accepted-20261014`, статус `completed`, оба исходных теста
прошли, тестовый файл не изменён. До утверждения первый план был отклонён с
просьбой прочитать тесты, после чего модель обновила план. Результат записан в
`examples/live-session.txt`. Платные внешние API не проверялись.
