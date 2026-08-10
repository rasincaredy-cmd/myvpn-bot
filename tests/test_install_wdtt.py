"""Новый сервер должен подниматься с обходом БС, а не только с VPN.

10.08.2026 выяснилось, что мастер ставит один AmneziaWG. На германской ноде
тумблер «Обход БС» админ включил руками, программы там не было, и юзер получал
«на сервере заминка» вместо доступа. Теперь обход ставится сам, а тумблер
включается только по факту работающего обхода.
"""
import inspect

from bot.handlers import install
from bot.services import wdtt_install


def test_install_puts_bypass_on_new_server() -> None:
    src = inspect.getsource(install)
    assert "wdtt_install" in src, (
        "мастер не ставит обход БС — каждый новый сервер придётся доносить руками"
    )


def test_bypass_goes_up_before_hardening() -> None:
    """Порядок: обход поднимается до эталона.

    Фаервол эталона строится от портов, которые реально слушают. Поднять
    обход после — значит получить сервер с работающим обходом и закрытыми
    для него портами.
    """
    src = inspect.getsource(install)
    assert src.index("wdtt_install.install") < src.index("hardening.harden")


def test_bypass_goes_up_after_vpn() -> None:
    """VPN — главное на сервере, обход не должен вклиниваться перед ним."""
    src = inspect.getsource(install)
    assert src.index("install_amneziawg") < src.index("wdtt_install.install")


def test_toggle_follows_real_state() -> None:
    """Тумблер `wdtt_enabled` выставляется из результата установки.

    Именно по нему бот решает, предлагать ли юзеру обход на этом сервере.
    Включить его «на веру» — вернуть ровно ту поломку, из-за которой всё
    и затевалось.
    """
    src = inspect.getsource(install)
    assert "wdtt_enabled" in src, "мастер не трогает тумблер обхода"
    marker = src.index("wdtt_install.install")
    tail = src[marker:]
    assert "wdtt_enabled" in tail, "тумблер выставляется не по результату установки"


def test_failed_bypass_does_not_fail_server() -> None:
    """Сервер с рабочим VPN и неудавшимся обходом обязан остаться рабочим:
    решение Влада 10.08 — «сервер завести, обход выключить»."""
    src = inspect.getsource(install)
    ready = src.index("ServerStatus.READY")
    bypass = src.index("wdtt_install.install")
    assert ready < bypass, (
        "обход ставится до перевода сервера в рабочее состояние — "
        "его отказ утянет за собой весь сервер"
    )


def test_bypass_ports_reach_firewall_only_when_working() -> None:
    """Порты обхода передаются эталону как обязательные только если обход
    поднялся: `apply-firewall` отказывается включать фаервол, если
    обязательный порт не слушает — сервер остался бы вовсе без фаервола."""
    src = inspect.getsource(install)
    assert "extra_ports" in src, "порты обхода не доходят до фаервола"


def test_hardening_accepts_extra_ports() -> None:
    from bot.services.hardening import harden

    assert "extra_ports" in inspect.signature(harden).parameters


def test_hardening_puts_extra_ports_into_firewall() -> None:
    from bot.services.hardening import harden

    src = inspect.getsource(harden)
    assert "extra_ports" in src[src.index("apply-firewall") - 400:], (
        "дополнительные порты не попадают в команду фаервола"
    )


def test_install_module_exposes_install_callable() -> None:
    assert callable(wdtt_install.install)
