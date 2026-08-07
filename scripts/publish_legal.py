"""Публикация юридических документов на telegra.ph.

Первый запуск создаёт аккаунт и печатает access_token — его нужно положить в
.env как TELEGRAPH_TOKEN, иначе страницы потом не отредактировать. При
последующих запусках с TELEGRAPH_TOKEN и --path страница обновляется, а не
создаётся заново (адрес не меняется — он уже будет прописан в кнопках бота).

Запуск:
    python scripts/publish_legal.py docs/legal/privacy.md
    python scripts/publish_legal.py docs/legal/terms.md --path Moschata-...-08-05
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegra.ph"


def _call(method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(
        {
            k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
            for k, v in params.items()
        }
    ).encode()
    with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=30) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise SystemExit(f"telegra.ph: {payload.get('error')}")
    return payload["result"]


def md_to_nodes(text: str) -> tuple[str, list]:
    """Очень простой конвертер: '# ' — заголовок страницы, '## ' — h3,
    остальное — абзацы. Списков в документах не делаем намеренно."""
    title = ""
    nodes: list = []
    for raw in text.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        if block.startswith("# ") and not title:
            title = block[2:].strip()
            continue
        if block.startswith("## "):
            nodes.append({"tag": "h3", "children": [block[3:].strip()]})
            continue
        nodes.append({"tag": "p", "children": [block.replace("\n", " ")]})
    return title, nodes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--path", default=None, help="path существующей страницы")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAPH_TOKEN", "")
    if not token:
        account = _call(
            "createAccount",
            {"short_name": "Moschata", "author_name": "Moschata VPN"},
        )
        token = account["access_token"]
        print("СОХРАНИ В .env:  TELEGRAPH_TOKEN=" + token)

    title, nodes = md_to_nodes(args.source.read_text(encoding="utf-8"))
    params = {
        "access_token": token,
        "title": title,
        "author_name": "Moschata VPN",
        "content": nodes,
        "return_content": False,
    }
    if args.path:
        params["path"] = args.path
        page = _call("editPage", params)
    else:
        page = _call("createPage", params)
    print("URL:", page["url"])
    print("path:", page["path"])


if __name__ == "__main__":
    main()
