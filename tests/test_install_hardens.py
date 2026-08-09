"""Новый сервер должен защищаться сам, без отдельной кнопки."""
import inspect

from bot.handlers import install


def test_install_calls_hardening() -> None:
    src = inspect.getsource(install)
    assert "hardening" in src, (
        "мастер установки не приводит новый сервер к эталону — "
        "каждый новый сервер поднимался бы с открытым паролем"
    )


def test_hardening_runs_after_vpn_is_up() -> None:
    """Порядок важен: фаервол строится от слушающих портов.

    Если привести сервер к эталону до подъёма VPN, порт VPN не будет
    слушать в этот момент и не попадёт в правила — сервер поднимется с
    VPN, к которому нельзя подключиться.
    """
    src = inspect.getsource(install)
    assert src.index("install_amneziawg") < src.index("hardening.harden")
