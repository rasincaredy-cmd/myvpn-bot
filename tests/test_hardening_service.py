"""Разбор вывода сценария-эталона."""
from bot.services.hardening import SCRIPT_PATH, parse_check


def test_script_is_taken_from_repo() -> None:
    # Копия текста сценария в коде разъедется с самим сценарием.
    assert SCRIPT_PATH.is_file(), "сценарий-эталон не найден в репозитории"
    assert SCRIPT_PATH.name == "harden.sh"


def test_parse_all_green() -> None:
    out = (
        "=== проверка соответствия эталону ===\n"
        "собственный адрес: 1.2.3.4\n"
        "OK   вход по паролю выключен\n"
        "OK   фаервол включён\n"
        "\n"
        "ИТОГ: сервер соответствует эталону\n"
    )
    report = parse_check(out, 0)
    assert report.compliant is True
    assert report.failed == []
    assert "вход по паролю выключен" in report.ok


def test_parse_with_failures() -> None:
    out = (
        "OK   фаервол включён\n"
        "FAIL вход по паролю РАЗРЕШЁН\n"
        "FAIL банилки перебора нет\n"
        "ИТОГ: есть несоответствия (см. FAIL выше)\n"
    )
    report = parse_check(out, 1)
    assert report.compliant is False
    assert report.failed == ["вход по паролю РАЗРЕШЁН", "банилки перебора нет"]


def test_nonzero_exit_is_not_compliant_even_without_fail_lines() -> None:
    # Сценарий мог упасть до печати проверок — молча считать это
    # соответствием нельзя.
    report = parse_check("что-то пошло не так\n", 2)
    assert report.compliant is False


def test_ensure_bot_key_does_not_touch_password() -> None:
    # Пароль — единственный путь на сервер, пока ключ не доказан.
    # Его стирание на этом шаге лишает возможности откатиться.
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "ssh_password_enc" not in src, (
        "пароль нельзя трогать до подтверждённого входа по ключу"
    )


def test_ensure_bot_key_clears_stale_passphrase() -> None:
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "ssh_key_passphrase_enc" in src, (
        "фраза-пароль от старого ключа должна зануляться"
    )


def test_ensure_bot_key_commits_before_returning() -> None:
    """Ключ обязан лежать в базе ДО того, как гасится пароль.

    Следующим шагом `harden` выключает вход по паролю. Если ключ к этому
    моменту записан только в память сессии и бот упадёт (или сессия
    откатится) — пароль уже выключен, а ключа в базе нет: сервер потерян
    для бота навсегда.
    """
    import inspect

    from bot.services.hardening import ensure_bot_key

    src = inspect.getsource(ensure_bot_key)
    assert "commit" in src, "ключ не фиксируется в базе до выключения пароля"
