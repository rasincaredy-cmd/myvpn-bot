from __future__ import annotations

from io import BytesIO

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def conf_to_qr_png(conf: str) -> bytes:
    """Возвращает PNG-байты QR-кода для строки .conf."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(conf)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
