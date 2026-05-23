"""FSM установки AmneziaWG на VPS."""
from __future__ import annotations

import contextlib
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Document, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import ServerStatus
from bot.filters.admin import AdminFilter
from bot.keyboards.inline import (
    CB_INSTALL,
    cancel_only,
    install_auth_method,
    install_confirm,
    main_menu,
)
from bot.loader import bot
from bot.services import amnezia
from bot.services.crypto import encrypt
from bot.services.ssh import SSHClient, SSHCredentials, SSHError
from bot.states.install import InstallStates
from bot.texts import t
from bot.utils.validators import (
    is_valid_host,
    is_valid_port,
    is_valid_server_name,
    is_valid_ssh_user,
)

router = Router(name="install")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

# Ed25519 < 1 KB, RSA-4096 ≈ 3 KB — 16 KB с запасом, заодно защита от мусора.
_MAX_KEY_BYTES = 16 * 1024


async def _safe_delete(message: Message) -> None:
    with contextlib.suppress(TelegramBadRequest, Exception):
        await message.delete()


async def _start_install(target: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(InstallStates.name)
    await target.answer(t.install_intro, reply_markup=cancel_only())


@router.message(Command("install"))
async def cmd_install(message: Message, state: FSMContext) -> None:
    await _start_install(message, state)


@router.callback_query(F.data == f"{CB_INSTALL}:start")
async def cb_install_start(call: CallbackQuery, state: FSMContext) -> None:
    await _start_install(call.message, state)
    await call.answer()


# --- Шаги -------------------------------------------------------------------

@router.message(InstallStates.name, F.text)
async def step_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not is_valid_server_name(name):
        await message.answer(
            "Имя должно начинаться с буквы/цифры, 2–32 символа, "
            "буквы/цифры/<code>-_</code>. Введи ещё раз:"
        )
        return
    await state.update_data(name=name)
    await state.set_state(InstallStates.host)
    await message.answer(t.install_ask_host, reply_markup=cancel_only())


@router.message(InstallStates.host, F.text)
async def step_host(message: Message, state: FSMContext) -> None:
    host = message.text.strip()
    if not is_valid_host(host):
        await message.answer("Невалидный IP/домен. Введи ещё раз:")
        return
    await state.update_data(host=host)
    await state.set_state(InstallStates.ssh_port)
    await message.answer(t.install_ask_ssh_port, reply_markup=cancel_only())


@router.message(InstallStates.ssh_port, F.text)
async def step_ssh_port(message: Message, state: FSMContext) -> None:
    port = is_valid_port(message.text)
    if port is None:
        await message.answer("Порт — число от 1 до 65535. Введи ещё раз:")
        return
    await state.update_data(ssh_port=port)
    await state.set_state(InstallStates.ssh_user)
    await message.answer(t.install_ask_ssh_user, reply_markup=cancel_only())


@router.message(InstallStates.ssh_user, F.text)
async def step_ssh_user(message: Message, state: FSMContext) -> None:
    user = message.text.strip()
    if not is_valid_ssh_user(user):
        await message.answer(
            "SSH-юзер: латиница в нижнем регистре, цифры, <code>_-</code>, "
            "до 32 символов. Введи ещё раз:"
        )
        return
    await state.update_data(ssh_user=user)
    await state.set_state(InstallStates.auth_method)
    await message.answer(t.install_ask_auth, reply_markup=install_auth_method())


@router.callback_query(InstallStates.auth_method, F.data == f"{CB_INSTALL}:auth:password")
async def step_auth_password(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(auth_method="password")
    await state.set_state(InstallStates.password)
    await call.message.edit_text(t.install_ask_password, reply_markup=cancel_only())
    await call.answer()


@router.callback_query(InstallStates.auth_method, F.data == f"{CB_INSTALL}:auth:key")
async def step_auth_key(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(auth_method="key")
    await state.set_state(InstallStates.key)
    await call.message.edit_text(t.install_ask_key, reply_markup=cancel_only())
    await call.answer()


@router.message(InstallStates.password, F.text)
async def step_password(message: Message, state: FSMContext) -> None:
    password = message.text
    # Удаляем сообщение с паролем НЕМЕДЛЕННО — оно не должно жить в истории.
    await _safe_delete(message)
    await state.update_data(password=password)
    await state.set_state(InstallStates.wg_port)
    await message.answer(
        t.install_ask_wg_port.format(default_port=settings.default_amnezia_port),
        reply_markup=cancel_only(),
    )


@router.message(InstallStates.key, F.document)
async def step_key_file(message: Message, state: FSMContext) -> None:
    doc: Document = message.document
    if doc.file_size and doc.file_size > _MAX_KEY_BYTES:
        await _safe_delete(message)
        await message.answer("Файл слишком большой для SSH-ключа. Пришли ещё раз:")
        return
    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    if buf is None:
        await _safe_delete(message)
        await message.answer("Не удалось скачать файл. Попробуй ещё раз:")
        return
    key_text = buf.read().decode("utf-8", errors="replace")
    await _safe_delete(message)
    if "PRIVATE KEY" not in key_text:
        await message.answer("Это не похоже на приватный ключ. Пришли ещё раз:")
        return
    await state.update_data(key=key_text)
    await state.set_state(InstallStates.key_passphrase)
    await message.answer(t.install_ask_key_passphrase, reply_markup=cancel_only())


@router.message(InstallStates.key, F.text)
async def step_key_text(message: Message, state: FSMContext) -> None:
    key_text = message.text
    await _safe_delete(message)
    if "PRIVATE KEY" not in key_text:
        await message.answer(
            "Это не похоже на приватный ключ. Пришли ещё раз "
            "(можно файлом <code>.pem</code> или <code>id_ed25519</code>):"
        )
        return
    if len(key_text) > _MAX_KEY_BYTES:
        await message.answer("Ключ слишком большой. Пришли ещё раз:")
        return
    await state.update_data(key=key_text)
    await state.set_state(InstallStates.key_passphrase)
    await message.answer(t.install_ask_key_passphrase, reply_markup=cancel_only())


@router.message(InstallStates.key_passphrase, F.text)
async def step_key_passphrase(message: Message, state: FSMContext) -> None:
    passphrase: str | None = message.text
    await _safe_delete(message)
    if passphrase and passphrase.strip() == "-":
        passphrase = None
    await state.update_data(key_passphrase=passphrase)
    await state.set_state(InstallStates.wg_port)
    await message.answer(
        t.install_ask_wg_port.format(default_port=settings.default_amnezia_port),
        reply_markup=cancel_only(),
    )


@router.message(InstallStates.wg_port, F.text)
async def step_wg_port(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if raw == "-":
        wg_port = settings.default_amnezia_port
    else:
        port = is_valid_port(raw)
        if port is None:
            await message.answer("Порт — число от 1 до 65535 или <code>-</code>. Ещё раз:")
            return
        wg_port = port

    data: dict[str, Any] = await state.update_data(wg_port=wg_port)
    auth_label = "ключ" if data.get("auth_method") == "key" else "пароль"
    await state.set_state(InstallStates.confirm)
    await message.answer(
        t.install_summary.format(
            name=data["name"],
            host=data["host"],
            ssh_user=data["ssh_user"],
            ssh_port=data["ssh_port"],
            auth=auth_label,
            wg_port=wg_port,
        ),
        reply_markup=install_confirm(),
    )


# --- Запуск установки -------------------------------------------------------

@router.callback_query(InstallStates.confirm, F.data == f"{CB_INSTALL}:run")
async def step_run(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    await state.clear()
    await call.message.edit_text(t.install_started)
    await call.answer()

    # Пишем INSTALLING ДО начала, чтобы при падении бота в логах/UI остался след.
    server = await repo.create_server(
        session,
        name=data["name"],
        host=data["host"],
        ssh_port=data["ssh_port"],
        ssh_user=data["ssh_user"],
        ssh_password_enc=encrypt(data.get("password")),
        ssh_key_enc=encrypt(data.get("key")),
        ssh_key_passphrase_enc=encrypt(data.get("key_passphrase")),
        wg_port=data["wg_port"],
        wg_interface=amnezia.WG_INTERFACE,
        wg_subnet="10.8.0.0/24",
        status=ServerStatus.INSTALLING,
        owner_tg_id=call.from_user.id,
    )
    await session.commit()

    chat_id = call.message.chat.id
    progress_msg = await bot.send_message(chat_id, t.install_step.format(step="Подключаюсь по SSH..."))

    async def progress(step: str) -> None:
        try:
            await progress_msg.edit_text(t.install_step.format(step=step))
        except TelegramBadRequest:
            pass

    creds = SSHCredentials(
        host=data["host"],
        port=data["ssh_port"],
        username=data["ssh_user"],
        password=data.get("password"),
        private_key=data.get("key"),
        key_passphrase=data.get("key_passphrase"),
    )

    try:
        async with SSHClient(creds) as ssh:
            result = await amnezia.install_amneziawg(
                ssh,
                host=data["host"],
                wg_port=data["wg_port"],
                progress=progress,
            )
    except SSHError as exc:
        logger.warning("Install failed for server {}: {}", server.id, exc)
        await repo.set_server_status(
            session, server.id, ServerStatus.FAILED, last_error=str(exc)
        )
        await session.commit()
        await bot.send_message(chat_id, t.install_failed.format(error=str(exc)[:1500]))
        return
    except Exception as exc:
        logger.exception("Unexpected install error")
        await repo.set_server_status(
            session, server.id, ServerStatus.FAILED, last_error=repr(exc)
        )
        await session.commit()
        await bot.send_message(chat_id, t.install_failed.format(error=str(exc)[:1500]))
        return

    server.server_public_key = result.server_public_key
    server.server_endpoint = result.endpoint
    server.wg_interface = result.interface
    server.wg_subnet = result.subnet
    server.awg_params_json = result.params.to_json()
    server.status = ServerStatus.READY
    server.last_error = None
    await session.commit()

    user = await repo.get_or_create_user(
        session,
        tg_id=call.from_user.id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
    )
    await bot.send_message(
        chat_id,
        t.install_done.format(name=server.name),
        reply_markup=main_menu(user.is_admin),
    )
