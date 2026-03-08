import asyncio
import time
import random
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

TOKEN = "8389508305:AAEznsCDkhc4RlS84l-x2i0ku7orHHE2lfI"

DB_PATH = "event777.db"
SECOND_WINDOW = 30  # 30 sec for second 777
FINAL_WINDOW = 30   # 30 sec for final move

# --- Helpers а-------------------------------------------------

def now() -> int:
    return int(time.time())

def is_comment_thread(msg: Message) -> bool:
    # Comments under channel posts appear in linked discussion group as a forum topic.
    return msg.message_thread_id is not None

def is_forwarded(msg: Message) -> bool:
    # forwarded dice shouldn't count
    return bool(msg.forward_date) or bool(getattr(msg, "is_automatic_forward", False))

def dice_kind(msg: Message) -> str | None:
    if not msg.dice:
        return None
    if msg.dice.emoji == "🎰":
        return "slot"
    if msg.dice.emoji == "🎲":
        return "dice"
    return None

def is_777_slot(msg: Message) -> bool:
    # Telegram slot dice values 1..64. 64 is jackpot 777.
    return bool(msg.dice and msg.dice.emoji == "🎰" and msg.dice.value == 64)

# --- DB ------------------------------------------------------

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS states (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  state TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (chat_id, thread_id, user_id)
);

CREATE TABLE IF NOT EXISTS winners (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  username TEXT,
  prize TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS top (
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  username TEXT,
  points INTEGER NOT NULL,
  PRIMARY KEY (chat_id, user_id)
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def get_state(chat_id: int, thread_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT state, expires_at FROM states WHERE chat_id=? AND thread_id=? AND user_id=?",
            (chat_id, thread_id, user_id),
        )
        row = await cur.fetchone()
        return row  # (state, expires_at) or None

async def set_state(chat_id: int, thread_id: int, user_id: int, state: str, expires_at: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO states(chat_id, thread_id, user_id, state, expires_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id, thread_id, user_id) DO UPDATE SET state=excluded.state, expires_at=excluded.expires_at",
            (chat_id, thread_id, user_id, state, expires_at),
        )
        await db.commit()

async def clear_state(chat_id: int, thread_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM states WHERE chat_id=? AND thread_id=? AND user_id=?",
            (chat_id, thread_id, user_id),
        )
        await db.commit()

async def add_winner(chat_id: int, thread_id: int, user_id: int, username: str | None, prize: str, points: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO winners(ts, chat_id, thread_id, user_id, username, prize) VALUES(?,?,?,?,?,?)",
            (now(), chat_id, thread_id, user_id, username, prize),
        )
        # points to leaderboard
        await db.execute(
            "INSERT INTO top(chat_id, user_id, username, points) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET points=top.points + excluded.points, username=excluded.username",
            (chat_id, user_id, username, points),
        )
        await db.commit()

async def get_top(chat_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, COALESCE(username, ''), points FROM top WHERE chat_id=? ORDER BY points DESC LIMIT ?",
            (chat_id, limit),
        )
        return await cur.fetchall()

# --- Game logic ----------------------------------------------

async def fail_and_reset(msg: Message, reason: str = "❌ Сброс"):
    await clear_state(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id)
    await msg.reply(f"{reason}\nПравило: любые лишние действия = сброс.")

async def handle_idle(msg: Message):
    # Need first jackpot 777 on slot
    if dice_kind(msg) != "slot":
        return await fail_and_reset(msg, "❌ Не то действие. Начинай со слота 🎰")

    if is_777_slot(msg):
        expires = now() + SECOND_WINDOW
        await set_state(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id, "WAIT_SECOND", expires)
        await msg.reply("🔥 7️⃣7️⃣7️⃣ поймал! Теперь у тебя 30 сек, чтобы поймать ВТОРЫЕ 7️⃣7️⃣7️⃣ подряд на 🎰")
    else:
        # nothing happens, user can keep trying
        pass

async def handle_wait_second(msg: Message, expires_at: int):
    if now() > expires_at:
        return await fail_and_reset(msg, "⏳ Время вышло (вторые 777 не успел)")

    if dice_kind(msg) != "slot":
        return await fail_and_reset(msg, "❌ Лишнее действие. Сейчас можно только 🎰")

    if is_777_slot(msg):
        expires = now() + FINAL_WINDOW
        await set_state(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id, "WAIT_FINAL", expires)
        await msg.reply("🚨 ВТОРЫЕ 7️⃣7️⃣7️⃣ подряд! У тебя ОДИН финальный шанс.\nОтправь 🎰 или 🎲 в течение 30 сек.")
    else:
        return await fail_and_reset(msg, "❌ Не 7️⃣7️⃣7️⃣ подряд - серия сгорела")

async def handle_wait_final(msg: Message, expires_at: int):
    if now() > expires_at:
        return await fail_and_reset(msg, "⏳ Время вышло (финальный ход не успел)")

    kind = dice_kind(msg)
    if kind not in ("slot", "dice"):
        return await fail_and_reset(msg, "❌ Лишнее действие. Финал - только 🎰 или 🎲")

    # Final attempt consumes the run
    await clear_state(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id)

    username = msg.from_user.username
    if kind == "dice":
        v = msg.dice.value  # 1..6
        if v == 2:
            await add_winner(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id, username, "🎁 25⭐️", 25)
            await msg.reply("🎉 Выпало 🎲2 - приз 🎁 25⭐️")
        elif v == 4:
            await add_winner(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id, username, "🚀 50⭐️", 50)
            await msg.reply("🎉 Выпало 🎲4 - приз 🚀 50⭐️")
        elif v == 6:
            await add_winner(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id, username, "💎 100⭐️", 100)
            await msg.reply("🎉 Выпало 🎲6 - приз 💎 100⭐️")
        else:
            await msg.reply("❌ Не повезло - сгорело")
    else:
        # slot final
        if is_777_slot(msg):
            await add_winner(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id, username, "🍭 5 NFT", 100)
            await msg.reply("🍭 7️⃣7️⃣7️⃣ на финале! Приз: 5 NFT")
        else:
            await msg.reply("❌ Финальный 🎰 не 7️⃣7️⃣7️⃣ - сгорело")

# --- Bot setup -----------------------------------------------

dp = Dispatcher()

@dp.message(Command("rules"))
async def rules(msg: Message):
    await msg.reply(
        "🎰 7️⃣7️⃣7️⃣EVENT правила:\n"
        "- Игра только в комментариях под постом\n"
        "- Старт: крути 🎰\n"
        "- 7️⃣7️⃣7️⃣ (джекпот) = 30 сек на вторые 7️⃣7️⃣7️⃣ подряд\n"
        "- Поймал вторые 7️⃣7️⃣7️⃣ = 30 сек на финальный ход 🎰 или 🎲 (1 попытка)\n"
        "- Любые лишние действия = сброс\n"
        "- Пересланные 🎰/🎲 не считаются"
    )

@dp.message(Command("top"))
async def top(msg: Message):
    rows = await get_top(msg.chat.id, limit=10)
    if not rows:
        return await msg.reply("Пока пусто. Никто еще не занес в топ 🙂")
    text = ["🔝 Топ игроков:"]
    for i, (uid, uname, pts) in enumerate(rows, start=1):
        name = f"@{uname}" if uname else f"id:{uid}"
    text.append(f"{i}. {name} - {pts}")
    await msg.reply("\n".join(text))

@dp.message(F.dice)
async def dice_handler(msg: Message):
    # Only in comments thread
    if not is_comment_thread(msg):
        return  # silently ignore outside the post-thread

    # Ignore forwarded
    if is_forwarded(msg):
        return await msg.reply("❌ Пересланные 🎰/🎲 не считаются")

    kind = dice_kind(msg)
    if kind is None:
        return  # other dice types ignored

    # Load state
    row = await get_state(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id)
    if row is None:
        return await handle_idle(msg)

    state, expires_at = row
    if state == "WAIT_SECOND":
        return await handle_wait_second(msg, expires_at)
    if state == "WAIT_FINAL":
        return await handle_wait_final(msg, expires_at)

    # unknown state -> reset
    return await fail_and_reset(msg, "❌ Ошибка состояния - сброс")

@dp.message()
async def any_message(msg: Message):
    # If user is in active state in this thread - any non-dice message counts as "лишнее действие"
    if not is_comment_thread(msg):
        return
    row = await get_state(msg.chat.id, msg.message_thread_id or 0, msg.from_user.id)
    if row is None:
        return
    # If they send any other text/sticker/photo etc - reset
    return await fail_and_reset(msg, "❌ Лишнее действие - сброс")

async def main():
    await db_init()
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
