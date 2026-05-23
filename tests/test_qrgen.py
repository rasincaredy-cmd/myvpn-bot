"""Тесты генератора QR-кодов."""
from __future__ import annotations

from bot.services.qrgen import conf_to_qr_png


class TestQrGen:
    def test_returns_non_empty_png(self) -> None:
        png = conf_to_qr_png("[Interface]\nPrivateKey = X\n")
        assert isinstance(png, bytes)
        assert len(png) > 100
        # Магическая сигнатура PNG: 89 50 4E 47 0D 0A 1A 0A
        assert png.startswith(b"\x89PNG\r\n\x1a\n")

    def test_long_conf_does_not_crash(self) -> None:
        """Полноценный AmneziaWG-конфиг с обфускацией ~600 символов — должно влезать."""
        conf = (
            "[Interface]\n"
            "PrivateKey = " + "x" * 44 + "\n"
            "Address = 10.8.0.5/32\n"
            "DNS = 1.1.1.1, 1.0.0.1\n"
            "Jc = 5\nJmin = 50\nJmax = 80\nS1 = 40\nS2 = 60\n"
            "H1 = 12345\nH2 = 67890\nH3 = 11111\nH4 = 22222\n"
            "\n"
            "[Peer]\n"
            "PublicKey = " + "y" * 44 + "\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "Endpoint = 1.2.3.4:585\n"
            "PersistentKeepalive = 25\n"
        )
        png = conf_to_qr_png(conf)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 500
