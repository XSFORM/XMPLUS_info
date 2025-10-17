from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BotCommand, BotCommandScopeDefault
from aiogram import Bot

router = Router()

# Команды, которые появятся в кнопке-меню Telegram
BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="add", description="Добавить запись (заглушка)"),
    BotCommand(command="list", description="Список записей (заглушка)"),
    BotCommand(command="remove", description="Удалить запись (заглушка)"),
    BotCommand(command="import", description="Импорт CSV (заглушка)"),
    BotCommand(command="export", description="Экспорт CSV (заглушка)"),
    BotCommand(command="next", description="Ближайшие истечения (заглушка)"),
    BotCommand(command="status", description="Статус бота"),
    BotCommand(command="settings", description="Настройки (заглушка)"),
]


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(commands=BOT_COMMANDS, scope=BotCommandScopeDefault())


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "✅ XMPLUS запущен.\n"
        "Открой меню команд (кнопка с точками) или напиши /help."
    )


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    text = "Доступные команды:\n" + "\n".join([f"/{c.command} — {c.description}" for c in BOT_COMMANDS])
    await message.answer(text)


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    await message.answer("Бот работает ✅")


# Заглушки под дальнейшую реализацию доменной логики
@router.message(Command("add"))
async def on_add(message: Message) -> None:
    await message.answer("Команда /add: логика добавления будет реализована позже.")


@router.message(Command("list"))
async def on_list(message: Message) -> None:
    await message.answer("Команда /list: здесь будет список записей.")


@router.message(Command("remove"))
async def on_remove(message: Message) -> None:
    await message.answer("Команда /remove: удаление записи будет добавлено позже.")


@router.message(Command("import"))
async def on_import(message: Message) -> None:
    await message.answer("Команда /import: импорт CSV будет добавлен позже.")


@router.message(Command("export"))
async def on_export(message: Message) -> None:
    await message.answer("Команда /export: экспорт CSV будет добавлен позже.")


@router.message(Command("next"))
async def on_next(message: Message) -> None:
    await message.answer("Команда /next: ближайшие истечения будут показаны позже.")


@router.message(Command("settings"))
async def on_settings(message: Message) -> None:
    await message.answer("Команда /settings: настройки будут добавлены позже.")