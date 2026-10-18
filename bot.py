"""
Telegram-бот: считает, сколько раз каждый админ забанил или замутил участников чата.

Как это работает
-----------------
Когда админ банит или мутит кого-то через интерфейс Telegram (или через другого бота),
Telegram шлёт update типа `chat_member` с полями:
    - chat            — чат, где произошло событие
    - from_user       — КТО совершил действие (в нашем случае — админ)
    - old_chat_member — статус участника ДО изменения
    - new_chat_member — статус участника ПОСЛЕ изменения

Если new_chat_member.status == "kicked"      -> это бан
Если new_chat_member.status == "restricted"
    и can_send_messages == False             -> это мут

Мы сохраняем счётчик (admin_id, chat_id) -> {bans, mutes} в SQLite.

Команды
-------
/b @username   — показать статистику конкретного админа (бан/мут упоминанием)
/b (в ответ на сообщение админа) — то же самое, но через reply
/b             — без аргументов, без reply -> показать топ всех админов в этом чате
/bstats        — алиас для /b без аргументов (топ всех админов)

Требования
----------
pip install python-telegram-bot==21.*

ВАЖНО:
1. Бот должен быть добавлен в чат как АДМИНИСТРАТОР (иначе не получит chat_member апдейты
   о действиях других админов).
2. Нужно явно подписаться на allowed_updates=["chat_member", "message"] при запуске поллинга
   (это уже сделано в коде ниже).
3. Telegram даёт from_user только если действие совершено через официальный клиент/API с
   правами админа. Если банит сам бот от имени пользователя через kick-команду другого бота,
   from_user будет этим ботом, а не живым человеком — это особенность Telegram, а не баг кода.
"""

import logging
import sqlite3
from pathlib import Path

from telegram import Update, ChatMember
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "PUT_YOUR_BOT_TOKEN_HERE"  # <-- вставь токен от @BotFather
DB_PATH = Path(__file__).parent / "mod_stats.db"


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_actions (
            chat_id     INTEGER NOT NULL,
            admin_id    INTEGER NOT NULL,
            admin_name  TEXT NOT NULL,
            bans        INTEGER NOT NULL DEFAULT 0,
            mutes       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, admin_id)
        )
        """
    )
    con.commit()
    con.close()


def bump_counter(chat_id: int, admin_id: int, admin_name: str, field: str) -> None:
    assert field in ("bans", "mutes")
    con = sqlite3.connect(DB_PATH)
    con.execute(
        f"""
        INSERT INTO admin_actions (chat_id, admin_id, admin_name, {field})
        VALUES (?, ?, ?, 1)
        ON CONFLICT(chat_id, admin_id) DO UPDATE SET
            {field} = {field} + 1,
            admin_name = excluded.admin_name
        """,
        (chat_id, admin_id, admin_name),
    )
    con.commit()
    con.close()


def get_stats_for_admin(chat_id: int, admin_id: int):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT admin_name, bans, mutes FROM admin_actions WHERE chat_id=? AND admin_id=?",
        (chat_id, admin_id),
    ).fetchone()
    con.close()
    return row  # (admin_name, bans, mutes) или None


def get_stats_all(chat_id: int):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT admin_name, bans, mutes
        FROM admin_actions
        WHERE chat_id=?
        ORDER BY (bans + mutes) DESC
        """,
        (chat_id,),
    ).fetchall()
    con.close()
    return rows


# ---------------------------------------------------------------------------
# Обработчик изменений статуса участников (бан / мут)
# ---------------------------------------------------------------------------

async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    if cmu is None:
        return

    actor = cmu.from_user  # кто совершил действие
    if actor is None or actor.is_bot and actor.id == context.bot.id:
        # если действие совершил сам этот бот — не считаем
        pass

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    chat_id = cmu.chat.id
    admin_name = f"@{actor.username}" if actor.username else actor.full_name

    # Бан: стал kicked и раньше не был kicked
    if new_status == ChatMember.BANNED and old_status != ChatMember.BANNED:
        bump_counter(chat_id, actor.id, admin_name, "bans")
        logger.info("Ban: %s забанил кого-то в чате %s", admin_name, chat_id)
        return

    # Мут: стал restricted с запретом на отправку сообщений
    if new_status == ChatMember.RESTRICTED:
        new_member = cmu.new_chat_member
        can_send = getattr(new_member, "can_send_messages", True)
        was_restricted_already = old_status == ChatMember.RESTRICTED
        if can_send is False and not was_restricted_already:
            bump_counter(chat_id, actor.id, admin_name, "mutes")
            logger.info("Mute: %s замутил кого-то в чате %s", admin_name, chat_id)
            return


# ---------------------------------------------------------------------------
# Команда /b
# ---------------------------------------------------------------------------

async def cmd_b(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    message = update.effective_message

    target_id = None
    target_name = None

    # Вариант 1: /b @username
    if context.args:
        arg = context.args[0]
        if arg.startswith("@"):
            username = arg[1:]
            # Пытаемся найти среди тех, у кого уже есть записи в БД (быстрый путь)
            con = sqlite3.connect(DB_PATH)
            row = con.execute(
                "SELECT admin_id, admin_name FROM admin_actions WHERE chat_id=? AND admin_name=?",
                (chat_id, arg),
            ).fetchone()
            con.close()
            if row:
                target_id, target_name = row
            else:
                await message.reply_text(
                    f"У {arg} пока нет зафиксированных банов/мутов в этом чате."
                )
                return
        else:
            await message.reply_text("Используй формат: /b @username")
            return

    # Вариант 2: /b ответом на сообщение админа
    elif message.reply_to_message:
        user = message.reply_to_message.from_user
        target_id = user.id
        target_name = f"@{user.username}" if user.username else user.full_name

    # Вариант 3: без аргументов — топ всех админов
    if target_id is None and not context.args and not message.reply_to_message:
        rows = get_stats_all(chat_id)
        if not rows:
            await message.reply_text("Пока нет статистики банов/мутов в этом чате.")
            return
        lines = ["📊 Статистика модерации в чате:\n"]
        for name, bans, mutes in rows:
            lines.append(f"{name} — банов: {bans}, мутов: {mutes}, всего: {bans + mutes}")
        await message.reply_text("\n".join(lines))
        return

    # Показываем статистику по конкретному админу
    row = get_stats_for_admin(chat_id, target_id)
    if row is None:
        name = target_name or "этот пользователь"
        await message.reply_text(f"У {name} пока нет зафиксированных банов/мутов в этом чате.")
        return

    admin_name, bans, mutes = row
    await message.reply_text(
        f"📊 {admin_name}\n"
        f"Банов: {bans}\n"
        f"Мутов: {mutes}\n"
        f"Всего действий: {bans + mutes}"
    )


async def cmd_bstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = []
    update.effective_message.reply_to_message = None
    await cmd_b(update, context)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler("b", cmd_b))
    app.add_handler(CommandHandler("bstats", cmd_bstats))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=["chat_member", "message"])


if __name__ == "__main__":
    main()
