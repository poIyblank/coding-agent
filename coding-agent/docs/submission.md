# Перед сдачей

Проект проверен 14 октября 2026 года на macOS с Python 3.13.

- Все 57 автоматических тестов прошли.
- Настоящая модель Qwen3 Instruct через Ollama завершила контрольную задачу.
- Модель исправила расчёт скидки и проверку диапазона, затем запустила Bash.
- Оба исходных теста прошли; `test_order.py` остался без изменений.
- Проверены подтверждения в safe, восстановление после сбоя, бюджеты,
  просмотр изменений, откат и конфликт при посторонней правке.

Результат настоящего прогона с выводом тестов и diff находится в
[examples/live-session.txt](../examples/live-session.txt). Соответствие пунктам
задания — в [requirements.md](requirements.md).

## Что осталось

Загрузить исходники в свой GitHub-репозиторий и отправить ссылку на его страницу.
В корне репозитория должны быть `README.md`, `policy.json`, папки `agent`,
`tests`, `workspace`, `docs` и `examples`. Скрытые `.env.example`, `.gitignore`
и `.github` тоже входят в проект. При загрузке через сайт GitHub используй
чистую папку из `submission/coding-agent`, а не всю рабочую папку с локальными
прогонами. Сам ZIP загружать вместо исходников не нужно.

В `workspace/order.py` оставлена исходная ошибка: это контрольный пример,
который агент должен исправить. Менять его перед сдачей не надо.

## Короткий текст для ответа на задание

Вместо скобок вставь свою ссылку:

> Репозиторий: [ссылка на GitHub].
>
> В README есть команды запуска и настройки. В проекте реализованы Plan Mode,
> подтверждение действий, policy, восстановление по run_id, бюджеты, задачи
> с зависимостями и откат. Прошли 57 автотестов. Контрольная задача также
> проверена с настоящей моделью через Ollama; результат — в examples/live-session.txt.

## Перед защитой

Сейчас новый прогон модели не нужен. Для подготовки прочитай
[explanation.md](explanation.md) и [defense.md](defense.md).

Быструю демонстрацию можно повторить без Ollama на отдельной копии. Следующие
команды создают новые папку и run_id, поэтому не пересекаются с прошлыми прогонами:

```bash
demo_copy=$(mktemp -d /tmp/coding-agent-demo.XXXXXX)
cp -R workspace/. "$demo_copy/"
demo_run="demo-$(date +%Y%m%d-%H%M%S)"
python3 -m agent start --workspace "$demo_copy" --model demo --mode safe --run-id "$demo_run"
python3 -m agent approve-plan "$demo_run"
python3 -m agent approve "$demo_run"
python3 -m agent approve "$demo_run"
python3 -m agent tasks "$demo_run"
python3 -m agent diff "$demo_run"
cmp workspace/test_order.py "$demo_copy/test_order.py"
python3 -m agent rollback "$demo_run"
cmp workspace/order.py "$demo_copy/order.py"
```

Это демонстрация runtime с заранее заданными ответами модели. Вызовы Bash,
изменения файлов и проверки результата в ней настоящие. Для демонстрации LLM
используй API-запуск из README на новой копии проекта.
