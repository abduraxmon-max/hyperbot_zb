import html
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# Render Dashboard -> Environment:
# BOT_TOKEN       = token from @BotFather
# WEBHOOK_SECRET = a private string containing only A-Z, a-z, 0-9, _ or -
# RENDER_EXTERNAL_URL is set automatically by Render for a Web Service.
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook")
PORT = int(os.getenv("PORT", "10000"))

TZ = ZoneInfo("Asia/Tashkent")
DB_FILE = os.getenv("DB_FILE", "admin_bot.db")
MAX_PLUS_PER_DAY = 3
HYPER_REPLIES = ["Ha eshtaman", "Shu yerdaman", "Qanday yordam kerak?"]

router = Router()
db = sqlite3.connect(DB_FILE)
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    chat_id INTEGER, user_id INTEGER, name TEXT, last_seen TEXT,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS msgs(
    chat_id INTEGER, user_id INTEGER, day TEXT, cnt INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id, day));
CREATE TABLE IF NOT EXISTS stars(
    chat_id INTEGER, user_id INTEGER, stars INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS plus_log(
    chat_id INTEGER, giver_id INTEGER, day TEXT, cnt INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, giver_id, day));
"""
)
db.commit()


def now() -> datetime:
    return datetime.now(TZ)


def mention(user_id: int, name: str) -> str:
    safe_name = html.escape(name)
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


async def is_admin(msg: Message) -> bool:
    if not msg.from_user:
        return False
    member = await msg.bot.get_chat_member(msg.chat.id, msg.from_user.id)
    return member.status in ("administrator", "creator")


async def target_user(msg: Message):
    """Reply qilingan xabar egasini qaytaradi (admin bo'lmasa)."""
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply("Buyruqni foydalanuvchi xabariga javob (reply) qilib yozing.")
        return None

    user = msg.reply_to_message.from_user
    member = await msg.bot.get_chat_member(msg.chat.id, user.id)
    if member.status in ("administrator", "creator"):
        await msg.reply("Adminga bu buyruqni ishlatib bo'lmaydi.")
        return None
    return user


# ---------- Har bir xabarni hisoblash (middleware) ----------
@router.message.outer_middleware()
async def track_activity(handler, event: Message, data):
    if (
        event.chat.type in ("group", "supergroup")
        and event.from_user
        and not event.from_user.is_bot
    ):
        t = now()
        day = t.strftime("%Y-%m-%d")
        user = event.from_user
        db.execute(
            "INSERT INTO users(chat_id,user_id,name,last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET "
            "name=excluded.name, last_seen=excluded.last_seen",
            (event.chat.id, user.id, user.full_name, t.isoformat()),
        )
        db.execute(
            "INSERT INTO msgs(chat_id,user_id,day,cnt) VALUES(?,?,?,1) "
            "ON CONFLICT(chat_id,user_id,day) DO UPDATE SET cnt=cnt+1",
            (event.chat.id, user.id, day),
        )
        db.commit()
    return await handler(event, data)


# ---------- Mut / Unmut ----------
@router.message(Command("mut", "mute"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mute(msg: Message, command: CommandObject):
    if not await is_admin(msg):
        return await msg.reply("Bu buyruq faqat adminlar uchun.")

    user = await target_user(msg)
    if not user:
        return

    # Misol: /mut 30m, /mut 2h, /mut 1d (standart: 1 soat)
    delta = timedelta(hours=1)
    if command.args:
        match = re.fullmatch(r"(\d+)\s*([mhd])", command.args.strip().lower())
        if not match:
            return await msg.reply("Format: /mut 30m yoki /mut 2h yoki /mut 1d")
        amount, unit = int(match.group(1)), match.group(2)
        if amount <= 0:
            return await msg.reply("Muddat 0 dan katta bo'lishi kerak.")
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]

    until = now() + delta
    await msg.bot.restrict_chat_member(
        msg.chat.id,
        user.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until,
    )
    await msg.reply(
        f"🔇 {mention(user.id, user.full_name)} "
        f"{until.strftime('%d.%m.%Y %H:%M')} gacha mut qilindi."
    )


@router.message(Command("unmut", "unmute"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unmute(msg: Message):
    if not await is_admin(msg):
        return await msg.reply("Bu buyruq faqat adminlar uchun.")

    user = await target_user(msg)
    if not user:
        return

    await msg.bot.restrict_chat_member(
        msg.chat.id,
        user.id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        ),
    )
    await msg.reply(f"🔊 {mention(user.id, user.full_name)} muti olib tashlandi.")


# ---------- Ban / Unban ----------
@router.message(Command("ban"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_ban(msg: Message):
    if not await is_admin(msg):
        return await msg.reply("Bu buyruq faqat adminlar uchun.")

    user = await target_user(msg)
    if not user:
        return

    await msg.bot.ban_chat_member(msg.chat.id, user.id)
    await msg.reply(f"⛔ {mention(user.id, user.full_name)} ban qilindi.")


@router.message(Command("unban"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unban(msg: Message):
    if not await is_admin(msg):
        return await msg.reply("Bu buyruq faqat adminlar uchun.")

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return await msg.reply("Buyruqni foydalanuvchi xabariga javob qilib yozing.")

    user = msg.reply_to_message.from_user
    await msg.bot.unban_chat_member(msg.chat.id, user.id, only_if_banned=True)
    await msg.reply(f"✅ {mention(user.id, user.full_name)} bandan chiqarildi.")


# ---------- Statistika ----------
def top_text(chat_id: int, like: str, title: str) -> str:
    rows = db.execute(
        "SELECT u.user_id, u.name, SUM(m.cnt) c FROM msgs m "
        "JOIN users u ON u.chat_id=m.chat_id AND u.user_id=m.user_id "
        "WHERE m.chat_id=? AND m.day LIKE ? "
        "GROUP BY m.user_id ORDER BY c DESC LIMIT 15",
        (chat_id, like),
    ).fetchall()

    if not rows:
        return "Hali ma'lumot yo'q."

    lines = [f"📊 <b>{title}</b>\n"]
    for index, (user_id, name, count) in enumerate(rows, 1):
        lines.append(f"{index}. {mention(user_id, name)} — {count} ta xabar")
    return "\n".join(lines)


@router.message(Command("kun", "top_kun"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_day(msg: Message):
    day = now().strftime("%Y-%m-%d")
    await msg.reply(top_text(msg.chat.id, day, "Bugungi faollik"))


@router.message(Command("oy", "top_oy"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_month(msg: Message):
    month = now().strftime("%Y-%m") + "-%"
    await msg.reply(top_text(msg.chat.id, month, "Shu oydagi faollik"))


# ---------- AFK ----------
@router.message(Command("afk"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_afk(msg: Message):
    limit = now() - timedelta(days=1)
    rows = db.execute(
        "SELECT user_id, name, last_seen FROM users WHERE chat_id=?",
        (msg.chat.id,),
    ).fetchall()

    afk = []
    for user_id, name, last_seen in rows:
        last_dt = datetime.fromisoformat(last_seen)
        if last_dt < limit:
            afk.append((user_id, name, now() - last_dt))

    if not afk:
        return await msg.reply("Hozircha 1 kundan ortiq AFK odam yo'q 👍")

    afk.sort(key=lambda item: item[2], reverse=True)
    lines = ["😴 <b>1 kundan ortiq yozmaganlar:</b>\n"]
    for user_id, name, duration in afk[:30]:
        lines.append(
            f"• {mention(user_id, name)} — "
            f"{duration.days} kun {duration.seconds // 3600} soat"
        )
    await msg.reply("\n".join(lines))


# ---------- Yulduzchalar ----------
@router.message(Command("yulduz", "stars"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_stars(msg: Message):
    rows = db.execute(
        "SELECT s.user_id, u.name, s.stars FROM stars s "
        "JOIN users u ON u.chat_id=s.chat_id AND u.user_id=s.user_id "
        "WHERE s.chat_id=? ORDER BY s.stars DESC LIMIT 15",
        (msg.chat.id,),
    ).fetchall()

    if not rows:
        return await msg.reply("Hali hech kimda yulduzcha yo'q.")

    lines = ["⭐ <b>Yulduzchalar reytingi:</b>\n"]
    for index, (user_id, name, stars) in enumerate(rows, 1):
        lines.append(f"{index}. {mention(user_id, name)} — {stars} ⭐")
    await msg.reply("\n".join(lines))


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.strip() == "+",
    F.reply_to_message,
)
async def plus_handler(msg: Message):
    target = msg.reply_to_message.from_user
    giver = msg.from_user

    if not target or target.is_bot or not giver or target.id == giver.id:
        return

    day = now().strftime("%Y-%m-%d")
    row = db.execute(
        "SELECT cnt FROM plus_log WHERE chat_id=? AND giver_id=? AND day=?",
        (msg.chat.id, giver.id, day),
    ).fetchone()
    used = row[0] if row else 0

    if used >= MAX_PLUS_PER_DAY:
        return await msg.reply(f"Bugungi {MAX_PLUS_PER_DAY} ta '+' limitingiz tugadi.")

    db.execute(
        "INSERT INTO plus_log(chat_id,giver_id,day,cnt) VALUES(?,?,?,1) "
        "ON CONFLICT(chat_id,giver_id,day) DO UPDATE SET cnt=cnt+1",
        (msg.chat.id, giver.id, day),
    )
    db.execute(
        "INSERT INTO stars(chat_id,user_id,stars) VALUES(?,?,1) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET stars=stars+1",
        (msg.chat.id, target.id),
    )
    db.execute(
        "INSERT INTO users(chat_id,user_id,name,last_seen) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id,user_id) DO NOTHING",
        (msg.chat.id, target.id, target.full_name, now().isoformat()),
    )
    db.commit()

    total = db.execute(
        "SELECT stars FROM stars WHERE chat_id=? AND user_id=?",
        (msg.chat.id, target.id),
    ).fetchone()[0]

    await msg.reply(
        f"⭐ {mention(target.id, target.full_name)} 1 ta yulduzcha oldi! Jami: {total}\n"
        f"(Sizning limitingiz: {used + 1}/{MAX_PLUS_PER_DAY})"
    )


# ---------- Hyper va soat ----------
@router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def text_handler(msg: Message):
    if not msg.text:
        return

    text = re.sub(r"[^\w\s]", "", msg.text.lower()).strip()
    if text == "hyper":
        await msg.reply(random.choice(HYPER_REPLIES))
    elif text in ("soat nechi", "soat nechchi", "soat necha"):
        await msg.reply(f"🕒 Hozir soat {now().strftime('%H:%M')}")


# ---------- Render Webhook server ----------
async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    if not TOKEN:
        raise RuntimeError("Render Environment'da BOT_TOKEN o'rnatilmagan.")
    if not BASE_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL topilmadi. Render Web Service ishlatayotganingizni tekshiring.")
    if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
        raise RuntimeError(
            "WEBHOOK_SECRET kerak: 1-256 ta belgi, faqat A-Z, a-z, 0-9, _ yoki - ishlating."
        )
    if not WEBHOOK_PATH.startswith("/"):
        raise RuntimeError("WEBHOOK_PATH '/' bilan boshlanishi kerak.")

    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    async def on_startup(bot: Bot):
        webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
        )
        logging.info("Webhook o'rnatildi: %s", webhook_url)

    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(create_app(), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
