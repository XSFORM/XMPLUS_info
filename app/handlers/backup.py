from __future__ import annotations

import logging, os, zipfile, shutil
from pathlib import Path
from datetime import datetime

from aiogram import Router, Bot, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
)
from aiogram.fsm.context import FSMContext

from app.db import SessionLocal, engine
from app.config import settings
from app.states import BackupStates
from app.keyboards import main_menu_kb, confirm_kb
from aiogram.filters import Command
from app.utils import now_tz

log = logging.getLogger(__name__)

router = Router()

BACKUP_DIR = Path("./data/backups")

# ====== Бэкап базы данных (только админ) ======

BACKUP_DIR = Path("./data/backups")


def backup_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Создать бэкап", callback_data="backup:create")],
        [InlineKeyboardButton(text="📥 Восстановить из бэкапа", callback_data="backup:restore")],
        [InlineKeyboardButton(text="📋 Список бэкапов", callback_data="backup:list")],
    ])


@router.message(Command("backup"))
@router.message(F.text.in_(["/backup", "💾 Бэкап"]))
async def backup_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("💾 Бэкап базы данных\n\nВыберите действие:", reply_markup=backup_menu_kb())


@router.callback_query(F.data == "backup:home")
async def backup_home_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("💾 Бэкап базы данных\n\nВыберите действие:", reply_markup=backup_menu_kb())


def _find_db_path() -> "Path | None":
    """Найти файл БД: пробуем несколько вариантов пути."""
    candidates = [
        Path("/app/data/data.db"),       # абсолютный путь в Docker
        Path("./data/data.db"),          # относительный (WORKDIR /app)
        Path("data/data.db"),            # без ./
    ]
    # Также пробуем извлечь путь из DATABASE_URL
    url = os.environ.get("DATABASE_URL", "")
    if ":///" in url:
        raw = url.split("///", 1)[1].split("?")[0]
        candidates.insert(0, Path(raw))
    for p in candidates:
        if p.exists():
            return p
    return None


@router.callback_query(F.data == "backup:create")
async def backup_create(cb: CallbackQuery) -> None:
    await cb.answer("Создаю бэкап…")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts = now_tz().strftime("%Y%m%d_%H%M%S")
    zip_name = f"xmplus_backup_{ts}.zip"
    zip_path = BACKUP_DIR / zip_name

    db_path = _find_db_path()
    tz_path = Path("/app/.tz_override")

    if not db_path:
        await cb.message.answer(
            "❌ Файл базы данных не найден!\n"
            f"DATABASE_URL: {os.environ.get('DATABASE_URL', '(не задан)')}\n"
            f"CWD: {Path.cwd()}",
            reply_markup=backup_menu_kb(),
        )
        return

    try:
        # Сохраняем переменные окружения (.env)
        env_keys = (
            "BOT_TOKEN", "OWNER_CHAT_ID", "BOT_MODE", "DEALER_NAME",
            "TIMEZONE", "CHECK_INTERVAL_MINUTES", "PRE_NOTIFY_HOURS",
            "NOTIFY_EVERY_MINUTES", "MAX_NOTIFICATIONS", "DATABASE_URL",
        )
        env_lines = [f"{k}={os.environ[k]}" for k in env_keys if k in os.environ]
        env_content = "\n".join(env_lines) + "\n"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, "data/data.db")
            zf.writestr(".env", env_content)
            if tz_path.exists():
                zf.write(tz_path, ".tz_override")

        # Проверяем что data.db действительно попало в архив
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
        if "data/data.db" not in names:
            await cb.message.answer(
                f"❌ Ошибка: data/data.db не в архиве.\nСодержимое: {names}",
                reply_markup=backup_menu_kb(),
            )
            return

        size_kb = zip_path.stat().st_size / 1024
        with open(zip_path, "rb") as f:
            doc = BufferedInputFile(f.read(), filename=zip_name)
        await cb.message.answer_document(
            doc,
            caption=(
                f"📦 Бэкап создан: {zip_name}\n"
                f"Размер: {size_kb:.1f} KB\n"
                f"Содержимое: {', '.join(names)}"
            ),
        )
        await cb.message.answer(
            "Бэкап сохранён на сервере и отправлен вам.",
            reply_markup=backup_menu_kb(),
        )
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка создания бэкапа: {e}", reply_markup=backup_menu_kb())


@router.callback_query(F.data == "backup:restore")
async def backup_restore_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await state.set_state(BackupStates.waiting_restore_file)
    await cb.message.answer(
        "📥 Восстановление из бэкапа\n\n"
        "Отправьте ZIP-архив бэкапа.\n"
        "⚠️ Текущая база данных будет заменена!\n\n"
        "Отмена — /cancel",
    )


@router.message(BackupStates.waiting_restore_file, F.document)
async def backup_restore_got_file(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await message.answer(
            "Отправьте ZIP-архив (.zip). Попробуйте ещё раз или /cancel.",
        )
        return

    await message.answer("⏳ Загружаю архив…")

    try:
        file_obj = await bot.get_file(doc.file_id)
        bio = await bot.download_file(file_obj.file_path)

        tmp_zip = Path("./data/_restore_tmp.zip")
        tmp_zip.write_bytes(bio.read())

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            names = zf.namelist()

        if "data/data.db" not in names:
            tmp_zip.unlink(missing_ok=True)
            await state.clear()
            await message.answer(
                "❌ Архив не содержит data/data.db — это не бэкап XMPLUS.",
                reply_markup=backup_menu_kb(),
            )
            return

        contents = ", ".join(names)
        await state.update_data(restore_zip=str(tmp_zip))
        await state.set_state(BackupStates.waiting_restore_confirm)
        await message.answer(
            f"Архив: {doc.file_name}\n"
            f"Содержимое: {contents}\n\n"
            "⚠️ Текущая база данных будет заменена!\n"
            "Подтвердите восстановление:",
            reply_markup=confirm_kb("cfb", show_edit=False),
        )
    except zipfile.BadZipFile:
        Path("./data/_restore_tmp.zip").unlink(missing_ok=True)
        await state.clear()
        await message.answer("❌ Файл повреждён или не является ZIP.", reply_markup=backup_menu_kb())
    except Exception as e:
        Path("./data/_restore_tmp.zip").unlink(missing_ok=True)
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=backup_menu_kb())


@router.message(BackupStates.waiting_restore_file)
async def backup_restore_not_file(message: Message) -> None:
    await message.answer(
        "Отправьте файл (ZIP-архив). Или /cancel для отмены.",
    )


@router.callback_query(F.data == "cfb:cancel")
async def backup_restore_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    tmp_zip_str = data.get("restore_zip", "")
    if tmp_zip_str:
        Path(tmp_zip_str).unlink(missing_ok=True)
    await state.clear()
    await cb.message.answer("Восстановление отменено.")


@router.callback_query(F.data == "cfb:ok", BackupStates.waiting_restore_confirm)
async def backup_restore_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    tmp_zip_str = data.get("restore_zip", "")
    tmp_zip = Path(tmp_zip_str) if tmp_zip_str else None

    if not tmp_zip or not tmp_zip.exists():
        await state.clear()
        await cb.message.answer(
            "❌ Временный файл не найден. Начните заново.",
            reply_markup=backup_menu_kb(),
        )
        return

    try:
        db_path = Path("./data/data.db")

        # Бэкап текущей базы перед заменой
        if db_path.exists():
            safe_ts = now_tz().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(db_path, Path(f"./data/data.db.pre_restore_{safe_ts}"))

        # Закрываем соединения с БД
        await engine.dispose()

        # Распаковка
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            if "data/data.db" in zf.namelist():
                zf.extract("data/data.db", ".")
            if ".tz_override" in zf.namelist():
                tz_data = zf.read(".tz_override")
                Path("/app/.tz_override").write_bytes(tz_data)

        tmp_zip.unlink(missing_ok=True)
        await state.clear()
        await cb.message.answer(
            "✅ База данных восстановлена!\n"
            "Старая база сохранена как резерв.\n\n"
            "⚠️ Перезапустите бота для полного применения:\n"
            "<code>cd /opt/xmplus && docker compose restart</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        if tmp_zip:
            tmp_zip.unlink(missing_ok=True)
        await state.clear()
        await cb.message.answer(f"❌ Ошибка восстановления: {e}")


# --- Список бэкапов ---

@router.callback_query(F.data == "backup:list")
async def backup_list_show(cb: CallbackQuery) -> None:
    await cb.answer()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(BACKUP_DIR.glob("xmplus_backup_*.zip"), reverse=True)
    if not files:
        await cb.message.answer(
            "📋 Список бэкапов пуст.\nСоздайте первый бэкап.",
            reply_markup=backup_menu_kb(),
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for f in files:
        size_kb = f.stat().st_size / 1024
        ts = f.stem.replace("xmplus_backup_", "")
        label = f"{ts} ({size_kb:.0f} KB)"
        rows.append([
            InlineKeyboardButton(text=f"📦 {label}", callback_data=f"backup:dl:{ts}"),
            InlineKeyboardButton(text="🗑", callback_data=f"backup:rm:{ts}"),
        ])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="backup:home")])

    await cb.message.answer(
        f"📋 Бэкапов на сервере: {len(files)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("backup:dl:"))
async def backup_download(cb: CallbackQuery) -> None:
    await cb.answer()
    ts = cb.data.split(":", 2)[-1]
    zip_name = f"xmplus_backup_{ts}.zip"
    zip_path = BACKUP_DIR / zip_name

    if not zip_path.exists():
        await cb.message.answer("Файл не найден.", reply_markup=backup_menu_kb())
        return

    try:
        with open(zip_path, "rb") as f:
            doc = BufferedInputFile(f.read(), filename=zip_name)
        size_kb = zip_path.stat().st_size / 1024
        await cb.message.answer_document(doc, caption=f"📦 {zip_name} ({size_kb:.1f} KB)")
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}", reply_markup=backup_menu_kb())


@router.callback_query(F.data.startswith("backup:rm:"))
async def backup_delete_ask(cb: CallbackQuery) -> None:
    await cb.answer()
    ts = cb.data.split(":", 2)[-1]
    zip_name = f"xmplus_backup_{ts}.zip"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удалить", callback_data=f"backup:rmok:{ts}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="backup:list"),
    ]])
    await cb.message.answer(f"Удалить бэкап {zip_name}?", reply_markup=kb)


@router.callback_query(F.data.startswith("backup:rmok:"))
async def backup_delete_exec(cb: CallbackQuery) -> None:
    await cb.answer()
    ts = cb.data.split(":", 2)[-1]
    zip_name = f"xmplus_backup_{ts}.zip"
    zip_path = BACKUP_DIR / zip_name

    if zip_path.exists():
        zip_path.unlink()
        await cb.message.answer(f"🗑 {zip_name} удалён.")
    else:
        await cb.message.answer("Файл уже удалён.")

    # Обновляем список
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("xmplus_backup_*.zip"), reverse=True)
    if files:
        rows: list[list[InlineKeyboardButton]] = []
        for f in files:
            size_kb = f.stat().st_size / 1024
            ts2 = f.stem.replace("xmplus_backup_", "")
            label = f"{ts2} ({size_kb:.0f} KB)"
            rows.append([
                InlineKeyboardButton(text=f"📦 {label}", callback_data=f"backup:dl:{ts2}"),
                InlineKeyboardButton(text="🗑", callback_data=f"backup:rm:{ts2}"),
            ])
        rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="backup:home")])
        await cb.message.answer(
            f"📋 Бэкапов на сервере: {len(files)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    else:
        await cb.message.answer("📋 Список бэкапов пуст.", reply_markup=backup_menu_kb())


