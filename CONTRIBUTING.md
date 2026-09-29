# Участие в разработке

Репозиторий ещё не опубликован. Лицензия проекта — MIT, текст в `LICENSE`. Вклад в код предполагается на тех же условиях.

## Что проверять перед правкой

- Схема профиля остаётся версии 1. Новые поля допустимы только так, чтобы старый профиль читался.
- Неизвестный ключ JSON — ошибка.
- Не добавляйте `eval` и `exec`.
- Проверку TLS по умолчанию не отключайте.
- Секреты не кладите в профили, примеры и журнал.
- Обычные тесты не ходят на внешние сайты. Локальный сервер — `127.0.0.1`.
- Живой Chromium помечайте `@pytest.mark.browser`. Обычный запуск такие тесты не берёт.
- Окно остаётся на PySide6.

## Тесты

Linux:

```bash
.venv/bin/python -m pip install -e ".[dev,desktop,curl]"
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
```

Windows (PowerShell):

```powershell
.venv\Scripts\python -m pip install -e ".[dev,desktop,curl]"
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python -m pytest --basetemp=.pytest-tmp
```

Дополнение `browser` и `playwright install chromium` для обычного прогона не нужны.

Сценарий `.github/workflows/tests.yml` повторяет эту установку на Ubuntu и Windows для Python 3.10 и 3.12. На GitHub он ещё не выполнялся.
