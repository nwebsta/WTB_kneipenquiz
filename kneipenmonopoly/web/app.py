"""Admin-Webseite (FastAPI + Jinja2 + HTMX) und Prozess-Lebenszyklus."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..config import Settings
from ..db import DB
from ..gamedata import load_game_data
from ..service import (EVENT_CARD_KINDS, PHASE_LABEL, SETUP_PARTS, Game, fmt_duration, normalize_game_data,
                       parse_iso)
from ..simulation import SPEEDS, Simulator
from ..texts import PLACEHOLDER_INFO

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
QUICK_REASONS = [
    "Bar nicht erkennbar",
    "Nicht das ganze Team drauf",
    "Nicht alle haben ein Getränk",
    "Foto unscharf / zu dunkel",
    "Altes oder fremdes Foto",
]


# Nach "Spiel zurücksetzen" beginnen die IDs wieder bei 1. Deshalb tragen die Bild-URLs die
# Telegram-file_unique_id (?v=...), und der Browser muss vor jeder Nutzung nachfragen (ETag -> 304).
PHOTO_CACHE = {"Cache-Control": "private, no-cache"}
MAX_IMPORT_BYTES = 5 * 1024 * 1024


class NotLoggedIn(Exception):
    pass


def _hm_span(td) -> str:
    """timedelta -> „2:13“ (Stunden:Minuten), negativ als 0:00."""
    minutes = max(0, int(td.total_seconds() // 60))
    return f"{minutes // 60}:{minutes % 60:02d}"


def _image_type(path: str) -> str:
    return "image/png" if path.lower().endswith(".png") else "image/jpeg"


def parse_offset(raw: str) -> int | None:
    """'0:45' -> 45, '150' -> 150."""
    raw = raw.strip()
    try:
        if ":" in raw:
            h, m = raw.split(":", 1)
            return int(h) * 60 + int(m)
        return int(raw)
    except ValueError:
        return None


def _age(seconds: int | None) -> str:
    """Alter einer Live-Position lesbar: „gerade eben“, „vor 3 Min“."""
    if seconds is None:
        return ""
    if seconds < 60:
        return "gerade eben"
    if seconds < 3600:
        return f"vor {seconds // 60} Min"
    return f"vor {seconds // 3600} Std {seconds % 3600 // 60} Min"


def _plain(text: str | None) -> str:
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def create_app(settings: Settings, game: Game | None = None, run_bot: bool = True) -> FastAPI:
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["plain"] = _plain
    templates.env.filters["age"] = _age

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        g = game
        if g is None:
            data = load_game_data(settings.active_game_data_path)
            g = Game(settings, data, DB(settings.db_path))
        app.state.game = g
        app.state.sim = Simulator(g)
        tg_app = None
        if run_bot and settings.bot_token:
            from ..bot import build_application, start_bot
            tg_app = build_application(g)
            await start_bot(tg_app)
        elif run_bot:
            log.warning("Kein BOT_TOKEN gesetzt: nur die Webseite läuft.")

        async def sweeper():
            while True:
                try:
                    await g.sweep()
                except Exception:
                    log.exception("Fehler im Sweeper")
                await asyncio.sleep(20)

        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            await app.state.sim.stop()
            task.cancel()
            if tg_app is not None:
                from ..bot import stop_bot
                await stop_bot(tg_app)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    secret = settings.session_secret or secrets.token_hex(32)
    # Bei HTTPS wird das Login-Cookie nur verschlüsselt übertragen.
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="strict", max_age=14 * 24 * 3600,
                       session_cookie="km_session", https_only=settings.https)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ---------------------------------------------------------------- Helfer
    def G(request: Request) -> Game:
        return request.app.state.game

    def admin(request: Request) -> str:
        name = request.session.get("admin")
        if not name:
            raise NotLoggedIn()
        return name

    @app.exception_handler(NotLoggedIn)
    async def _not_logged_in(request: Request, exc: NotLoggedIn):
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    def flash(request: Request, msg: str, ok: bool = True) -> None:
        request.session["flash"] = {"msg": msg, "ok": ok}

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        g = G(request)
        ctx.setdefault("flash", request.session.pop("flash", None))
        ctx.update(request=request, game=g, admin_name=request.session.get("admin"),
                   phase=g.phase, phase_label=PHASE_LABEL[g.phase],
                   pending=g.pending_count() + g.open_calls_count(),
                   test_mode=g.test_mode(), texts_error=g.t.error, sim=request.app.state.sim,
                   sim_speeds=SPEEDS)
        return templates.TemplateResponse(request, name, ctx)

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    # ---------------------------------------------------------------- Login
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login(request: Request, name: str = Form(...), password: str = Form(...)):
        if settings.admin_password and secrets.compare_digest(password.encode(), settings.admin_password.encode()) \
                and name.strip():
            request.session["admin"] = name.strip()[:30]
            return back("/")
        await asyncio.sleep(1.5)
        return templates.TemplateResponse(request, "login.html", {"error": "Name oder Passwort falsch."},
                                          status_code=401)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return back("/login")

    # ---------------------------------------------------------------- Dashboard
    def event_card_rows(g: Game) -> list[dict]:
        rows = []
        for c in g.event_cards():
            due = g.card_due(c)
            if c["kind"] == "next_checkin":
                n = g.db.one("SELECT COUNT(DISTINCT team_id) AS n FROM checkins WHERE event_card_id=? "
                             "AND status IN ('open','submitted','done')", (c["id"],))["n"]
                status = f"bei {n} Team(s) angezeigt" if g.card_is_due(c) else "wartet"
            else:
                status = "ausgelöst" if g.card_fired(c) else "wartet"
            rows.append({"c": c, "offset": f"{c['offset_min'] // 60}:{c['offset_min'] % 60:02d}",
                         "due": g.hm(due) if due else None, "status": status})
        return rows

    def clock_ctx(g: Game) -> dict:
        dur, left, elapsed = g.duration_min(), g.game_remaining(), g.game_elapsed()
        running = g.phase in ("running", "paused")
        return {
            "duration": fmt_duration(dur) if dur else None,
            "duration_hm": f"{dur // 60}:{dur % 60:02d}" if dur else "",
            "remaining": _hm_span(left) if left is not None and running else None,
            "elapsed": _hm_span(elapsed) if elapsed is not None and running else None,
            "end_time": g.hm(g.end_time()) if left is not None and running else None,
        }

    def dashboard_ctx(g: Game) -> dict:
        return {"standings": g.standings(), "recent": g.recent_entries(20), "clock": clock_ctx(g),
                "hidden": g.scores_hidden(), "multiplier": g.state().multiplier, "cards": event_card_rows(g)}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        admin(request)
        return render(request, "dashboard.html", **dashboard_ctx(G(request)))

    @app.get("/partials/dashboard", response_class=HTMLResponse)
    async def dashboard_partial(request: Request):
        admin(request)
        return render(request, "_dashboard.html", **dashboard_ctx(G(request)))

    # ---------------------------------------------------------------- Prüfen
    def queue_ctx(g: Game) -> dict:
        names = {t["id"]: t for t in g.teams()}
        items = []
        now = g.clock()
        st = g.state()

        def describe(entries):
            return [(g.team_name(e.team), e.delta, g.describe_entry(e)) for e in entries]

        for s in g.pending_submissions():
            sp = g.spot(s["spot_id"])
            older = g.blocked_by(s)
            owner = st.owners.get(s["spot_id"])
            items.append({
                "type": "photo", "sort": s["submitted_at"],
                "s": s, "spot": sp, "team": names.get(s["team_id"]),
                "age_min": int((now - parse_iso(s["submitted_at"])).total_seconds() // 60),
                "time": g.hm(s["submitted_at"]), "checkin_time": g.hm(s["checkin_at"]),
                "blocked_by": g.team_name(older["team_id"]) if older else None,
                "owner": g.team_name(owner) if owner else None,
                "preview": describe(g.preview(s)),
                "live_now": g.live_snapshot(s["team_id"], sp),
            })
        for c in g.open_calls():
            ci = g.db.one("SELECT * FROM checkins WHERE id=?", (c["checkin_id"],)) if c["checkin_id"] else None
            owner = st.owners.get(c["spot_id"]) if c["spot_id"] else None
            items.append({
                "type": "call", "sort": c["requested_at"],
                "c": c, "team": names.get(c["team_id"]),
                "spot": g.spot(c["spot_id"]) if c["spot_id"] else None,
                "checkin_time": g.hm(ci["created_at"]) if ci else None,
                "owner": g.team_name(owner) if owner else None,
                "age_min": int((now - parse_iso(c["requested_at"])).total_seconds() // 60),
                "time": g.hm(c["requested_at"]),
                "preview_ok": describe(g.call_preview(c, True)),
                "preview_fail": describe(g.call_preview(c, False)),
            })
        items.sort(key=lambda it: it["sort"])
        return {"items": items, "reasons": QUICK_REASONS, "multiplier": st.multiplier}

    @app.get("/pruefen", response_class=HTMLResponse)
    async def queue_page(request: Request):
        admin(request)
        return render(request, "pruefen.html", **queue_ctx(G(request)))

    @app.get("/partials/queue", response_class=HTMLResponse)
    async def queue_partial(request: Request):
        admin(request)
        return render(request, "_queue.html", **queue_ctx(G(request)))

    @app.post("/pruefen/{sub_id}/ok", response_class=HTMLResponse)
    async def approve(request: Request, sub_id: int, task_done: str | None = Form(None)):
        who = admin(request)
        res = await G(request).approve(sub_id, who, bool(task_done))
        return render(request, "_queue.html", flash={"msg": res.message, "ok": res.ok}, **queue_ctx(G(request)))

    @app.post("/pruefen/{sub_id}/nein", response_class=HTMLResponse)
    async def reject(request: Request, sub_id: int, reason: str = Form("")):
        who = admin(request)
        res = await G(request).reject(sub_id, who, reason)
        return render(request, "_queue.html", flash={"msg": res.message, "ok": res.ok}, **queue_ctx(G(request)))

    @app.post("/pruefen/anruf/{call_id}/{decision}", response_class=HTMLResponse)
    async def decide_call(request: Request, call_id: int, decision: str):
        who = admin(request)
        g = G(request)
        if decision == "abbrechen":
            res = await g.cancel_call(call_id, who)
        elif decision in ("ok", "nein"):
            res = await g.decide_call(call_id, who, decision == "ok")
        else:
            return PlainTextResponse("Unbekannt", status_code=400)
        return render(request, "_queue.html", flash={"msg": res.message, "ok": res.ok}, **queue_ctx(g))

    @app.get("/foto/{sub_id}")
    async def photo(request: Request, sub_id: int):
        admin(request)
        s = G(request).db.one("SELECT photo_path FROM submissions WHERE id=?", (sub_id,))
        if not s or not s["photo_path"] or not Path(s["photo_path"]).exists():
            return PlainTextResponse("Foto nicht vorhanden", status_code=404)
        return FileResponse(s["photo_path"], media_type=_image_type(s["photo_path"]), headers=PHOTO_CACHE)

    @app.get("/chatfoto/{msg_id}")
    async def chat_photo(request: Request, msg_id: int):
        admin(request)
        m = G(request).db.one("SELECT photo_path FROM messages WHERE id=?", (msg_id,))
        if not m or not m["photo_path"] or not Path(m["photo_path"]).exists():
            return PlainTextResponse("Foto nicht vorhanden", status_code=404)
        return FileResponse(m["photo_path"], media_type=_image_type(m["photo_path"]), headers=PHOTO_CACHE)

    # ---------------------------------------------------------------- Karte
    @app.get("/karte", response_class=HTMLResponse)
    async def map_page(request: Request):
        admin(request)
        return render(request, "karte.html")

    @app.get("/api/karte")
    async def map_api(request: Request):
        admin(request)
        return JSONResponse(G(request).map_data())

    # ---------------------------------------------------------------- Chats
    def chats_ctx(g: Game, team: str | None) -> dict:
        teams = {t["id"]: t for t in g.teams()}
        unlinked = team == "frei"
        team_id = int(team) if team and team.isdigit() else None
        rows = list(reversed(g.messages(team_id, limit=300, unlinked=unlinked)))
        msgs = [{"m": m, "team": teams.get(m["team_id"]), "time": g.hm(m["ts"])} for m in rows]
        n_unlinked = g.db.one("SELECT COUNT(DISTINCT chat_id) AS n FROM messages WHERE team_id IS NULL")["n"]
        return {"msgs": msgs, "teams": list(teams.values()), "team_filter": team_id,
                "filter_key": team or "", "n_unlinked": n_unlinked}

    @app.get("/chats", response_class=HTMLResponse)
    async def chats(request: Request, team: str | None = None):
        admin(request)
        return render(request, "chats.html", **chats_ctx(G(request), team))

    @app.get("/partials/chats", response_class=HTMLResponse)
    async def chats_partial(request: Request, team: str | None = None):
        admin(request)
        return render(request, "_chats.html", **chats_ctx(G(request), team))

    @app.post("/chats/senden")
    async def chats_send(request: Request, target: str = Form(...), text: str = Form(...)):
        who = admin(request)
        g = G(request)
        if text.strip():
            await g.admin_message(who, text.strip(), None if target == "alle" else int(target))
            flash(request, "Nachricht gesendet.")
        return back("/chats" + ("" if target == "alle" else f"?team={target}"))

    # ---------------------------------------------------------------- Verwaltung
    @app.get("/verwaltung", response_class=HTMLResponse)
    async def manage(request: Request):
        admin(request)
        g = G(request)
        st = g.state()
        spots = []
        tasks = g.tasks()
        for sp in g.all_spots(st):
            spots.append({"sp": sp, "owner": st.owners.get(sp.id), "task": tasks.get(sp.id, ""),
                          "replaced": st.replaced.get(sp.id)})
        used_reserve = set(st.replaced.values())
        return render(request, "verwaltung.html", teams=g.teams(), spots=spots,
                      event_cards=event_card_rows(g), card_kinds=EVENT_CARD_KINDS, action_cards=g.action_cards(),
                      reserve=list(g.data.reserve.values()), used_reserve=used_reserve,
                      random_tasks=g.random_tasks(), hidden=g.scores_hidden(), clock=clock_ctx(g),
                      setup_parts=SETUP_PARTS, game_data_source=g.game_data_source(), map_status=g.map_status(),
                      game_data_imported=g.s.imported_game_data_path.exists(),
                      points=st.points)

    async def _do(request: Request, coro_or_result) -> RedirectResponse:
        res = await coro_or_result if asyncio.iscoroutine(coro_or_result) else coro_or_result
        flash(request, res.message, res.ok)
        return back("/verwaltung")

    @app.post("/verwaltung/team/neu")
    async def team_new(request: Request, name: str = Form(...), color: str = Form("#888888"), code: str = Form("")):
        admin(request)
        return await _do(request, G(request).create_team(name, color, code))

    @app.post("/verwaltung/team/{team_id}/speichern")
    async def team_save(request: Request, team_id: int, name: str = Form(...), color: str = Form(...),
                        code: str = Form(...)):
        admin(request)
        return await _do(request, G(request).update_team(team_id, name, color, code))

    @app.post("/verwaltung/team/{team_id}/loeschen")
    async def team_delete(request: Request, team_id: int):
        admin(request)
        return await _do(request, G(request).delete_team(team_id))

    @app.post("/verwaltung/team/{team_id}/trennen")
    async def team_unlink(request: Request, team_id: int):
        admin(request)
        return await _do(request, G(request).unlink_team(team_id))

    @app.post("/verwaltung/spiel/{action}")
    async def game_action(request: Request, action: str):
        who = admin(request)
        g = G(request)
        fn = {"start": g.start_game, "pause": g.pause_game, "weiter": g.resume_game, "ende": g.end_game}.get(action)
        if action == "endstand":
            return await _do(request, g.announce_final())
        if fn is None:
            return back("/verwaltung")
        return await _do(request, fn(who))

    @app.post("/verwaltung/spieldauer")
    async def set_duration(request: Request, dauer: str = Form("")):
        admin(request)
        return await _do(request, G(request).set_duration(dauer))

    @app.post("/verwaltung/verdeckt")
    async def hidden(request: Request, hidden: str | None = Form(None)):
        admin(request)
        G(request).set_scores_hidden(bool(hidden))
        flash(request, "Punktestand ist jetzt " + ("verdeckt." if hidden else "sichtbar."))
        return back("/verwaltung")

    @app.post("/verwaltung/feld/{spot_id}/aufgabe")
    async def set_task(request: Request, spot_id: str, text: str = Form("")):
        admin(request)
        G(request).set_task(spot_id, text)
        flash(request, "Aufgabe gespeichert.")
        return back("/verwaltung#felder")

    @app.post("/verwaltung/feld/{spot_id}/deaktivieren")
    async def deactivate(request: Request, spot_id: str):
        who = admin(request)
        return await _do(request, G(request).deactivate_spot(spot_id, who))

    @app.post("/verwaltung/feld/{spot_id}/aktivieren")
    async def reactivate(request: Request, spot_id: str):
        who = admin(request)
        return await _do(request, G(request).reactivate_spot(spot_id, who))

    @app.post("/verwaltung/feld/{spot_id}/ersetzen")
    async def replace(request: Request, spot_id: str, reserve: str = Form("")):
        who = admin(request)
        return await _do(request, G(request).replace_spot(spot_id, reserve or None, who))

    @app.post("/verwaltung/zufallsaufgabe/neu")
    async def rnd_new(request: Request, text: str = Form("")):
        admin(request)
        G(request).add_random_task(text)
        flash(request, "Zufallsaufgabe hinzugefügt.")
        return back("/verwaltung#aufgaben")

    @app.post("/verwaltung/zufallsaufgabe/{task_id}/loeschen")
    async def rnd_del(request: Request, task_id: int):
        admin(request)
        G(request).delete_random_task(task_id)
        flash(request, "Zufallsaufgabe gelöscht.")
        return back("/verwaltung#aufgaben")

    @app.post("/verwaltung/besitzer")
    async def set_owner(request: Request, spot: str = Form(...), team: str = Form(""), comment: str = Form("")):
        who = admin(request)
        return await _do(request, G(request).set_owner(spot, int(team) if team else None, who, comment.strip()))

    @app.post("/verwaltung/punkte")
    async def adjust(request: Request, team: int = Form(...), delta: int = Form(...), comment: str = Form("")):
        who = admin(request)
        return await _do(request, G(request).adjust_points(team, delta, who, comment.strip()))

    @app.post("/verwaltung/nachricht")
    async def manage_msg(request: Request, target: str = Form(...), text: str = Form(...)):
        who = admin(request)
        if text.strip():
            await G(request).admin_message(who, text.strip(), None if target == "alle" else int(target))
            flash(request, "Nachricht gesendet.")
        return back("/verwaltung")

    @app.post("/verwaltung/ereigniskarte/neu")
    async def event_card_new(request: Request, offset: str = Form(...), kind: str = Form(...),
                             text: str = Form(...)):
        admin(request)
        minutes = parse_offset(offset)
        if minutes is None:
            flash(request, "Zeit bitte als H:MM (z.B. 0:45) oder in Minuten angeben.", False)
            return back("/verwaltung#ereignisse")
        res = G(request).save_event_card(None, minutes, kind, text)
        flash(request, res.message, res.ok)
        return back("/verwaltung#ereignisse")

    @app.post("/verwaltung/ereigniskarte/{card_id}/speichern")
    async def event_card_save(request: Request, card_id: int, offset: str = Form(...), kind: str = Form(...),
                              text: str = Form(...)):
        admin(request)
        minutes = parse_offset(offset)
        if minutes is None:
            flash(request, "Zeit bitte als H:MM (z.B. 0:45) oder in Minuten angeben.", False)
            return back("/verwaltung#ereignisse")
        res = G(request).save_event_card(card_id, minutes, kind, text)
        flash(request, res.message, res.ok)
        return back("/verwaltung#ereignisse")

    @app.post("/verwaltung/ereigniskarte/{card_id}/loeschen")
    async def event_card_delete(request: Request, card_id: int):
        admin(request)
        G(request).delete_event_card(card_id)
        flash(request, "Ereigniskarte gelöscht.")
        return back("/verwaltung#ereignisse")

    @app.post("/verwaltung/aktionskarte/neu")
    async def action_card_new(request: Request, text: str = Form("")):
        admin(request)
        G(request).add_action_card(text)
        flash(request, "Aktionskarte hinzugefügt.")
        return back("/verwaltung#aktionen")

    @app.post("/verwaltung/aktionskarte/{card_id}/loeschen")
    async def action_card_delete(request: Request, card_id: int):
        admin(request)
        G(request).delete_action_card(card_id)
        flash(request, "Aktionskarte gelöscht.")
        return back("/verwaltung#aktionen")

    @app.post("/verwaltung/simulation/start")
    async def sim_start(request: Request):
        admin(request)
        form = await request.form()
        team_ids = [int(t) for t in form.getlist("team")]
        msg = await request.app.state.sim.start(team_ids or None, send_to_groups=bool(form.get("telegram")),
                                                auto_approve=bool(form.get("auto")),
                                                speed=str(form.get("speed") or "") or None)
        flash(request, msg, request.app.state.sim.running)
        return back("/")

    @app.post("/verwaltung/simulation/tempo")
    async def sim_speed(request: Request, speed: str = Form(...)):
        admin(request)
        flash(request, request.app.state.sim.set_speed(speed))
        return back("/verwaltung#simulation")

    @app.post("/verwaltung/simulation/stop")
    async def sim_stop(request: Request):
        admin(request)
        flash(request, await request.app.state.sim.stop())
        return back("/verwaltung#simulation")

    @app.post("/verwaltung/testmodus")
    async def toggle_test_mode(request: Request, on: str | None = Form(None)):
        admin(request)
        G(request).set_test_mode(bool(on))
        flash(request, "Testmodus ist jetzt " + ("AN: Teams können mit /checkin NUMMER einchecken." if on
                                                  else "aus."))
        return back("/verwaltung#test")

    @app.post("/verwaltung/reset")
    async def reset(request: Request, confirm: str = Form("")):
        admin(request)
        if confirm.strip().upper() != "RESET":
            flash(request, "Zum Bestätigen bitte RESET eintippen.", False)
            return back("/verwaltung#test")
        await request.app.state.sim.stop()
        return await _do(request, G(request).reset_game())

    @app.post("/verwaltung/datenschutz")
    async def purge(request: Request, confirm: str = Form("")):
        admin(request)
        if confirm.strip().upper() != "LÖSCHEN":
            flash(request, "Zum Bestätigen bitte LÖSCHEN eintippen.", False)
            return back("/verwaltung")
        return await _do(request, G(request).purge_private_data())

    # ---------------------------------------------------------------- Protokoll
    @app.get("/protokoll", response_class=HTMLResponse)
    async def protocol(request: Request):
        admin(request)
        return render(request, "protokoll.html", rows=G(request).event_rows())

    @app.post("/protokoll/{event_id}/zuruecknehmen")
    async def revoke(request: Request, event_id: int, comment: str = Form("")):
        who = admin(request)
        res = await G(request).revoke(event_id, who, comment.strip())
        flash(request, res.message, res.ok)
        return back("/protokoll")

    # ---------------------------------------------------------------- Export
    def _stamp() -> str:
        return datetime.now().strftime("%Y%m%d-%H%M")

    @app.get("/export/stand.csv")
    async def export_standings(request: Request):
        admin(request)
        return Response("﻿" + G(request).export_standings_csv(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="endstand-{_stamp()}.csv"'})

    @app.get("/export/protokoll.csv")
    async def export_events(request: Request):
        admin(request)
        return Response("﻿" + G(request).export_events_csv(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="protokoll-{_stamp()}.csv"'})

    @app.get("/export/spiel.json")
    async def export_json(request: Request):
        admin(request)
        return Response(G(request).export_json(), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="spiel-{_stamp()}.json"'})

    # ---------------------------------------------------------------- Texte bearbeiten
    @app.get("/texte", response_class=HTMLResponse)
    async def texts_page(request: Request):
        admin(request)
        t = G(request).t
        entries = t.entries()
        sections = []
        for e in entries:
            if not sections or sections[-1] != e["section"]:
                sections.append(e["section"])
        return render(request, "texte.html", entries=entries, sections=sections,
                      placeholder_info={k: {"info": v[0], "beispiel": v[1]} for k, v in PLACEHOLDER_INFO.items()},
                      texts_file=str(t.user_path) if t.user_path else "")

    @app.post("/texte/api/speichern")
    async def texts_save(request: Request):
        who = admin(request)
        body = await request.json()
        t = G(request).t
        key = str(body.get("key", ""))
        if body.get("reset"):
            err = t.reset(key)
        else:
            err = t.save(key, str(body.get("value", "")))
        if err:
            return JSONResponse({"ok": False, "message": err}, status_code=422)
        log.info("Text %s geändert von %s", key, who)
        entry = next((e for e in t.entries() if e["key"] == key), None)
        return JSONResponse({"ok": True, "message": "Gespeichert, der Bot nutzt den Text ab sofort.", "entry": entry})

    @app.get("/texte/datei")
    async def texts_download(request: Request):
        admin(request)
        t = G(request).t
        if not t.user_path or not t.user_path.exists():
            return PlainTextResponse("Keine Textdatei vorhanden", status_code=404)
        return FileResponse(t.user_path, media_type="application/toml",
                            headers={"Content-Disposition": f'attachment; filename="texte-{_stamp()}.toml"'})

    # ---------------------------------------------------------------- Karten-Editor
    @app.get("/editor", response_class=HTMLResponse)
    async def editor_page(request: Request):
        admin(request)
        return render(request, "editor.html")

    @app.get("/editor/api/daten")
    async def editor_data(request: Request):
        admin(request)
        g = G(request)
        return JSONResponse({"data": normalize_game_data(g.game_data_raw()), "phase": g.phase,
                             "struktur": g.phase == "setup", "quelle": g.game_data_source()})

    @app.post("/editor/api/speichern")
    async def editor_save(request: Request):
        who = admin(request)
        g = G(request)
        try:
            raw = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "message": "Keine gültigen Daten empfangen."}, status_code=400)
        res, notes = g.editor_save(raw, who)
        body = {"ok": res.ok, "message": res.message, "notes": notes}
        if res.ok:
            body["data"] = normalize_game_data(g.game_data_raw())
            body["quelle"] = g.game_data_source()
        return JSONResponse(body, status_code=200 if res.ok else 422)

    # ---------------------------------------------------------------- Spielkarte (Bild)
    @app.get("/karte/bild.jpg")
    async def map_image(request: Request, download: int = 0):
        admin(request)
        try:
            path = await G(request).ensure_map_image()
        except Exception as e:
            log.exception("Kartenbild fehlgeschlagen")
            return PlainTextResponse(f"Kartenbild konnte nicht erzeugt werden: {e}", status_code=500)
        headers = {"Cache-Control": "private, no-cache"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="spielkarte-{_stamp()}.jpg"'
        return FileResponse(path, media_type="image/jpeg", headers=headers)

    @app.post("/verwaltung/karte/rendern")
    async def map_render(request: Request):
        admin(request)
        g = G(request)
        try:
            await g.ensure_map_image(force=True)
        except Exception as e:
            log.exception("Kartenbild fehlgeschlagen")
            flash(request, f"Kartenbild konnte nicht erzeugt werden: {e}", False)
            return back("/verwaltung#spielkarte")
        st = g.map_status()
        msg = f"Kartenbild neu erzeugt ({st['size']} Pixel)."
        if g.s.map_tiles and not st["tiles"]:
            msg += " Ohne Kartenhintergrund: OpenStreetMap war nicht erreichbar."
        flash(request, msg, True)
        return back("/verwaltung#spielkarte")

    # ---------------------------------------------------------------- Setup übertragen
    @app.get("/export/setup.json")
    async def export_setup(request: Request):
        admin(request)
        body = json.dumps(G(request).export_setup(), ensure_ascii=False, indent=2)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="kneipenmonopoly-setup-{_stamp()}.json"'})

    @app.get("/export/spieldaten.json")
    async def export_game_data(request: Request):
        admin(request)
        body = json.dumps(G(request).game_data_raw(), ensure_ascii=False, indent=2)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="spieldaten-{_stamp()}.json"'})

    @app.post("/verwaltung/import")
    async def import_setup(request: Request):
        admin(request)
        form = await request.form()
        upload = form.get("datei")
        if upload is None or not hasattr(upload, "read"):
            flash(request, "Bitte eine Datei auswählen.", False)
            return back("/verwaltung#import")
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            flash(request, "Die Datei ist zu groß (max. 5 MB).", False)
            return back("/verwaltung#import")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            flash(request, f"Keine gültige JSON-Datei: {e}", False)
            return back("/verwaltung#import")
        parts = {p for p in form.getlist("teil") if p in SETUP_PARTS}
        return await _do(request, G(request).import_setup(payload, parts))

    @app.post("/verwaltung/spieldaten/zuruecksetzen")
    async def reset_game_data(request: Request):
        admin(request)
        return await _do(request, G(request).reset_game_data())

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    return app
