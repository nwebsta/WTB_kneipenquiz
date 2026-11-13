"""Telegram-Bot (python-telegram-bot v21, Long-Polling)."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import ChatMigrated, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .service import Game

log = logging.getLogger(__name__)

CHOICE_MAX_AGE_S = 300
LIVE_FOREVER_S = 0x7FFFFFFF  # Telegram: Live-Standort „bis ich ihn beende“


def keyboard(buttons: list[tuple[str, str]] | None):
    """Inline-Buttons, je Button eine Zeile."""
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons])


class TelegramNotifier:
    def __init__(self, app: Application, game: Game):
        self.app = app
        self.game = game

    async def _migrated(self, chat_id: int, send):
        """Führt `send(chat_id)` aus. Wurde die Gruppe zur Supergruppe, neue Chat-ID merken und erneut senden."""
        try:
            return await send(chat_id)
        except ChatMigrated as e:
            self.game.migrate_chat(chat_id, e.new_chat_id)
            return await send(e.new_chat_id)

    async def send_text(self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        await self._migrated(chat_id, lambda cid: self.app.bot.send_message(
            cid, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=keyboard(buttons)))

    async def send_map(self, chat_id: int, caption: str) -> None:
        """Spielkarte als Bild, aus den aktuellen Spieldaten erzeugt. Fällt bei Problemen auf die PDF zurück."""
        try:
            path = await self.game.ensure_map_image()
            sig = self.game.db.get_kv("map_image_hash")
            cached = self.game.db.get_kv("map_photo_id") if self.game.db.get_kv("map_photo_hash") == sig else None
            if cached:
                try:
                    await self._migrated(chat_id, lambda cid: self.app.bot.send_photo(cid, cached, caption=caption))
                    return
                except TelegramError:
                    log.warning("Gecachtes Kartenbild ungültig, lade neu hoch")

            async def upload_photo(cid):
                with path.open("rb") as fh:
                    return await self.app.bot.send_photo(cid, fh, caption=caption)

            msg = await self._migrated(chat_id, upload_photo)
            if msg.photo:
                self.game.db.set_kv("map_photo_id", msg.photo[-1].file_id)
                self.game.db.set_kv("map_photo_hash", sig)
            return
        except Exception:
            log.exception("Kartenbild konnte nicht gesendet werden, nehme die PDF")
        await self._send_pdf(chat_id, caption)

    async def _send_pdf(self, chat_id: int, caption: str) -> None:
        cached = self.game.db.get_kv("map_file_id")
        if cached:
            try:
                await self._migrated(chat_id, lambda cid: self.app.bot.send_document(cid, cached, caption=caption))
                return
            except TelegramError:
                log.warning("Gecachte Karten-file_id ungültig, lade neu hoch")
        path = Path(self.game.s.map_pdf_path)
        if not path.exists():
            await self.send_text(chat_id, self.game.t("allgemein.karte_fehlt"))
            return

        async def upload(cid):
            with path.open("rb") as fh:
                return await self.app.bot.send_document(cid, fh, filename=path.name, caption=caption)

        msg = await self._migrated(chat_id, upload)
        if msg.document:
            self.game.db.set_kv("map_file_id", msg.document.file_id)


def _sender(update: Update) -> str:
    u = update.effective_user
    if u is None:
        return "?"
    return u.full_name + (f" (@{u.username})" if u.username else "")


def build_application(game: Game) -> Application:
    app = Application.builder().token(game.s.bot_token).build()
    notifier = TelegramNotifier(app, game)
    game.notifier = notifier
    T = game.t

    async def reply(update: Update, text: str, markup=None) -> None:
        msg = update.effective_message
        if msg is None:
            return
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
        chat = update.effective_chat
        team = game.team_by_chat(chat.id)
        game.log_message(team_id=team["id"] if team else None, chat_id=chat.id,
                         direction="out", kind="text", text=text, sender="Bot",
                         chat_title=chat.title or chat.full_name or str(chat.id))

    async def team_or_hint(update: Update):
        team = game.team_by_chat(update.effective_chat.id)
        if team is None:
            await reply(update, T("allgemein.nicht_verknuepft"))
        return team

    # --- Protokoll aller eingehenden Nachrichten (läuft vor allen anderen) ----
    async def log_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        chat = update.effective_chat
        if msg is None or chat is None:
            return
        # Auch Chats ohne Team mitschreiben (z.B. Gruppe vor dem /join), damit die Spielleitung sie sieht.
        team = game.team_by_chat(chat.id)
        common = dict(team_id=team["id"] if team else None, chat_id=chat.id, direction="in",
                      sender=_sender(update), tg_message_id=msg.message_id,
                      chat_title=chat.title or chat.full_name or str(chat.id))
        if msg.photo:
            ph = msg.photo[-1]
            path = await download_photo(context, ph.file_id, ph.file_unique_id)
            game.log_message(kind="photo", text=msg.caption, photo_path=path, file_unique_id=ph.file_unique_id,
                             **common)
        elif msg.location:
            game.log_message(kind="location", lat=msg.location.latitude, lon=msg.location.longitude,
                             text="Live-Standort" if msg.location.live_period else "Standort", **common)
        elif msg.text:
            game.log_message(kind="text", text=msg.text, **common)
        else:
            game.log_message(kind="other", text=msg.caption or "[andere Nachricht]", **common)

    async def download_photo(context: ContextTypes.DEFAULT_TYPE, file_id: str, unique_id: str) -> str | None:
        known = game.known_photo(unique_id)
        if known:
            return known
        dest = game.photo_dest(unique_id)
        try:
            f = await context.bot.get_file(file_id)
            await f.download_to_drive(custom_path=dest)
            return str(dest)
        except TelegramError:
            log.exception("Foto-Download fehlgeschlagen")
            return None

    # --- Befehle --------------------------------------------------------------
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, game.help_text())

    async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        code = " ".join(context.args or [])
        result, send_map = game.join(chat.id, chat.title or chat.full_name or str(chat.id), code)
        await reply(update, result.message)
        if result.ok and send_map:
            team = game.team_by_chat(chat.id)
            await game.send_welcome_pack(team["id"])

    async def cmd_stand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        team = await team_or_hint(update)
        if team:
            await reply(update, game.stand_text(team["id"]))

    async def cmd_felder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await team_or_hint(update):
            await reply(update, game.felder_text())

    async def cmd_karte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        team = await team_or_hint(update)
        if team:
            await game.send_map_to(team["id"])

    async def cmd_regeln(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, game.rules_text())

    async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Nur im Testmodus: Check-in per Feldnummer, z.B. /checkin 15 oder /checkin N."""
        team = await team_or_hint(update)
        if team:
            res = game.test_checkin(team["id"], " ".join(context.args or []))
            await reply(update, res.message, keyboard(res.buttons))

    async def cmd_aktion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Versteckter Ersatz für den Button „Aktionskarte ziehen“."""
        team = await team_or_hint(update)
        if team:
            await reply(update, (await game.request_action(team["id"])).message)

    async def on_action_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        team = game.team_by_chat(update.effective_chat.id)
        if team is None:
            await q.answer(T("allgemein.nicht_zugeordnet_kurz"))
            return
        await q.answer()
        game.log_message(team_id=team["id"], chat_id=update.effective_chat.id, direction="in", kind="text",
                         text="[Button: Aktionskarte ziehen]", sender=_sender(update))
        await reply(update, (await game.request_action(team["id"])).message)

    async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Gruppe wurde zur Supergruppe: Telegram vergibt eine neue Chat-ID."""
        msg = update.message
        if msg.migrate_to_chat_id:
            game.migrate_chat(msg.chat_id, msg.migrate_to_chat_id)
        elif msg.migrate_from_chat_id:
            game.migrate_chat(msg.migrate_from_chat_id, msg.chat_id)

    # --- Standort -------------------------------------------------------------
    async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        team = await team_or_hint(update)
        if team is None:
            return
        msg = update.message
        loc = msg.location
        if loc.live_period:
            # Live-Standort: nur mitlesen (letzte Position für die Spielleitung), KEIN Check-in.
            until = msg.date + timedelta(seconds=loc.live_period) if loc.live_period < LIVE_FOREVER_S else None
            game.update_live(team["id"], loc.latitude, loc.longitude, loc.horizontal_accuracy, until)
            await reply(update, T("checkin.live_gestartet"))
            return
        res = game.handle_location(team["id"], loc.latitude, loc.longitude)
        if res.choices:
            now = int(time.time())
            buttons = [[InlineKeyboardButton(
                T("checkin.auswahl_button", bar=sp.name, meter=round(d)),
                callback_data=f"ci|{sp.id}|{loc.latitude:.5f}|{loc.longitude:.5f}|{now}")]
                for sp, d in res.choices[:6]]
            await reply(update, T("checkin.mehrere_felder"), InlineKeyboardMarkup(buttons))
        elif res.text:
            await reply(update, res.text, keyboard(res.buttons))

    async def on_live_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Telegram schickt Live-Standort-Updates als bearbeitete Nachricht. Still mitlesen, nichts antworten."""
        msg = update.edited_message
        if msg is None or msg.location is None or update.effective_chat is None:
            return
        team = game.team_by_chat(update.effective_chat.id)
        if team is not None:
            game.update_live(team["id"], msg.location.latitude, msg.location.longitude,
                             msg.location.horizontal_accuracy)

    async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        team = game.team_by_chat(update.effective_chat.id)
        if team is None:
            await q.answer(T("allgemein.nicht_zugeordnet_kurz"))
            return
        try:
            _, spot_id, lat, lon, ts = q.data.split("|")
            lat, lon, ts = float(lat), float(lon), int(ts)
        except ValueError:
            await q.answer(T("allgemein.auswahl_ungueltig"))
            return
        if time.time() - ts > CHOICE_MAX_AGE_S:
            await q.answer(T("checkin.auswahl_zu_alt"))
            await q.edit_message_reply_markup(None)
            return
        await q.answer()
        await q.edit_message_reply_markup(None)
        result = game.checkin(team["id"], spot_id, lat, lon)
        game.log_message(team_id=team["id"], chat_id=update.effective_chat.id, direction="in", kind="text",
                         text=f"[Auswahl: {game.spot(spot_id).name}]", sender=_sender(update))
        await reply(update, result.message, keyboard(result.buttons))

    async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Freie Bar mit anderem Team drin: Kurzen & gehen."""
        q = update.callback_query
        team = game.team_by_chat(update.effective_chat.id)
        if team is None:
            await q.answer(T("allgemein.nicht_zugeordnet_kurz"))
            return
        try:
            _, checkin_id, mode = q.data.split("|")
            checkin_id = int(checkin_id)
        except ValueError:
            await q.answer(T("allgemein.auswahl_ungueltig"))
            return
        await q.answer()
        result = await game.choose_mode(team["id"], checkin_id, mode)
        if result.ok:
            await q.edit_message_reply_markup(None)
        game.log_message(team_id=team["id"], chat_id=update.effective_chat.id, direction="in", kind="text",
                         text="[Auswahl: Kurzen & gehen]", sender=_sender(update))
        await reply(update, result.message)

    # --- Fotos ----------------------------------------------------------------
    async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        team = game.team_by_chat(update.effective_chat.id)
        if team is None:
            return
        ph = update.message.photo[-1]
        path = await download_photo(context, ph.file_id, ph.file_unique_id)
        result = game.submit_photo(team["id"], path, ph.file_id, ph.file_unique_id)
        if result is not None:
            await reply(update, result.message)

    async def on_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if game.team_by_chat(update.effective_chat.id) and game.open_checkin(
                game.team_by_chat(update.effective_chat.id)["id"]):
            await reply(update, T("allgemein.foto_als_datei"))

    async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        for m in update.message.new_chat_members:
            if m.id == context.bot.id:
                await reply(update, T("allgemein.bot_begruessung"))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Fehler im Bot", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(T("allgemein.fehler"), parse_mode=ParseMode.HTML)
            except TelegramError:
                pass

    # Reihenfolge: Gruppe -1 protokolliert, Gruppe 0 verarbeitet.
    app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, log_incoming), group=-1)
    app.add_handler(CommandHandler(["start", "hilfe", "help"], cmd_start))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("stand", cmd_stand))
    app.add_handler(CommandHandler("felder", cmd_felder))
    app.add_handler(CommandHandler("karte", cmd_karte))
    app.add_handler(CommandHandler("regeln", cmd_regeln))
    app.add_handler(CommandHandler(["checkin", "c"], cmd_checkin))
    app.add_handler(CommandHandler("aktion", cmd_aktion))
    app.add_handler(MessageHandler(filters.LOCATION & filters.UpdateType.MESSAGE, on_location))
    app.add_handler(MessageHandler(filters.LOCATION & filters.UpdateType.EDITED_MESSAGE, on_live_update))
    app.add_handler(MessageHandler(filters.PHOTO & filters.UpdateType.MESSAGE, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document_image))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^ci\|"))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode\|"))
    app.add_handler(CallbackQueryHandler(on_action_button, pattern=r"^aktion$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate), group=1)
    app.add_error_handler(on_error)
    return app


async def start_bot(app: Application) -> None:
    await app.initialize()
    await app.start()
    # edited_message: darüber kommen die Live-Standort-Updates (nur letzte Position wird gespeichert).
    await app.updater.start_polling(allowed_updates=["message", "edited_message", "callback_query",
                                                     "my_chat_member"])
    log.info("Telegram-Bot läuft als @%s", app.bot.username)


async def stop_bot(app: Application) -> None:
    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
    except Exception:
        log.exception("Fehler beim Stoppen des Bots")
