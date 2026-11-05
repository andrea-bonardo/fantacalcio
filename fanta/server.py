"""Interfaccia web locale di FantaLineup 26/27 (solo libreria standard).

Avvia un piccolo server su 127.0.0.1, apre il browser e serve la cartella ``fanta/web``
(index.html, app.js, style.css) più ``fantalineup.css``. Il calcolo è lo stesso della finestra
tkinter (``compute_recommendation`` di ``gui.py``): qui lo si espone solo come JSON.

    python -m fanta.server            # oppure: python fantacalcio.py web
"""

from __future__ import annotations

import dataclasses
import json
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import base64
import re
from .dataio import read_json
from .gui import STATE_FILE, parse_json_calendar
from .xlsxio import read_league_calendar
CARICATE_DIR = "caricate"

from .config import ROLE_ORDER
from .dataio import (
    DataError,
    ensure_data,
    load_players,
    load_serie_a,
    load_settings,
    resolve_data_dir,
)
from .gui import (
    DEMO_LABEL,
    arrow,
    comparison_rows,
    compute_recommendation,
    describe_projection,
    expected_goals_of,
    infer_goal_thresholds,
    lineup_spread,
    load_league_calendar,
    match_outcome,
    swap_marks,
    swap_players,
)
from .league import load_league
from .models import Lineup
from .optimizer import LineupError, defense_modifier

WEB_DIR = Path(__file__).resolve().parent / "web"
THEME_DIR = Path(__file__).resolve().parent / "theme"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _signed(value: float) -> str:
    return "0.00" if abs(value) < 0.005 else f"{value:+.2f}"


class ApiError(Exception):
    pass


# ---------------------------------------------------------------------------
# Stato del server (un solo utente, in locale)
# ---------------------------------------------------------------------------


class Session:
    def __init__(self, data_dir: Path, args: Any = None):
        self.data_dir = data_dir
        self.lock = threading.RLock()
        ensure_data(data_dir, auto_update=True)
        self.players, _ = load_players(data_dir=data_dir)
        self.serie_a = load_serie_a(data_dir=data_dir)
        self.base_settings = load_settings(data_dir=data_dir)
        self.leagues: list[tuple[str, str]] = [(DEMO_LABEL, "demo")]
        from .cli import _discover_leagues  # import locale: evita dipendenze circolari

        for name, path in _discover_leagues(data_dir):
            self.leagues.append((f"{name} ({Path(path).name})", str(path)))
        wanted = getattr(args, "lega", None)
        if wanted and wanted not in [address for _, address in self.leagues]:
            self.leagues.append((Path(wanted).name if "://" not in wanted else wanted, wanted))
        self.wanted_league = wanted
        self.wanted_team = getattr(args, "squadra", None)
        self.league_cache: dict[str, dict[str, Any]] = {}
        self.entries: dict[str, dict[str, Any]] = {}

    # -- lega -----------------------------------------------------------

    def league(self, address: str) -> dict[str, Any]:
        if address in self.league_cache:
            return self.league_cache[address]
        try:
            league = load_league(
                address, data_dir=self.data_dir, players=self.players,
                serie_a=self.serie_a, settings=self.base_settings,
            )
        except Exception as exc:  # noqa: BLE001 - l'errore torna al browser
            raise ApiError(str(exc) or exc.__class__.__name__) from exc
        raw, source = load_league_calendar(address, self.data_dir)
        calendar = []
        for match in raw:
            try:
                calendar.append(dataclasses.replace(
                    match, home=league.team(match.home).name, away=league.team(match.away).name))
            except KeyError:
                continue
        if raw and not calendar:
            note = f"Calendario trovato ({source}) ma i nomi delle squadre non coincidono con la lega."
        elif source:
            note = f"Calendario: {source}"
        else:
            note = ""
        info = {
            "league": league, "calendar": calendar, "note": note,
            "thresholds": infer_goal_thresholds(calendar),
        }
        self.league_cache[address] = info
        return info

    def api_init(self) -> dict[str, Any]:
        return {
            "leghe": [{"label": label, "address": address} for label, address in self.leagues],
            "lega": self.wanted_league, "squadra": self.wanted_team,
        }

    def api_lega(self, address: str) -> dict[str, Any]:
        with self.lock:
            info = self.league(address)
            league = info["league"]
            return {
                "name": league.name, "teams": league.team_names(),
                "matchday": league.matchday or league.serie_a.current_matchday,
                "note": info["note"],
            }

    # -- formazione -----------------------------------------------------

    def api_formazione(self, body: dict[str, Any]) -> dict[str, Any]:
        address = body.get("address") or "demo"
        with self.lock:
            info = self.league(address)
            league, calendar = info["league"], info["calendar"]
            try:
                team = league.team(body.get("team") or league.team_names()[0])
            except KeyError as exc:
                raise ApiError("Squadra non trovata.") from exc
            try:
                matchday = int(body.get("matchday") or 0) or None
            except (TypeError, ValueError):
                matchday = None
            try:
                rec = compute_recommendation(league, team, matchday)
            except (LineupError, DataError) as exc:
                raise ApiError(str(exc)) from exc

            me = team.name
            match = next((m for m in calendar if m.day == rec.matchday and me in (m.home, m.away)), None)
            opponent, auto = None, False
            if match:
                opponent, auto = (match.away if me == match.home else match.home), True
            elif body.get("avversario") in league.team_names() and body.get("avversario") != me:
                opponent = body["avversario"]

            entry: dict[str, Any] = {"rec": rec, "info": info, "opp": None, "opp_error": ""}
            if opponent:
                try:
                    entry["opp"] = compute_recommendation(league, league.team(opponent), rec.matchday).lineups[0]
                except Exception as exc:  # noqa: BLE001
                    entry["opp_error"] = str(exc) or exc.__class__.__name__
            rec_id = uuid.uuid4().hex[:10]
            if len(self.entries) > 20:
                self.entries.pop(next(iter(self.entries)))
            self.entries[rec_id] = entry

            lineups = rec.lineups
            data = {
                "recId": rec_id, "team": me, "matchday": rec.matchday,
                "leagueName": league.name,
                "updated": league.serie_a.updated or "",
                "evaluated": rec.evaluated,
                "warnings": rec.warnings,
                "settings": {"moduli": [l.formation for l in lineups]},
                "players": {pid: self._player(rec, lineups[0], pid) for pid in rec.projections},
                "lineups": [self._lineup(l, rec.projections) for l in lineups],
                "compare": [self._compare(entry, l, l, False) for l in lineups],
                "duel": [self._duel(entry, l) for l in lineups],
                "outlooks": [self._outlook(o) for o in rec.outlooks],
                "opponent": {
                    "name": opponent, "auto": auto, "error": entry["opp_error"],
                    "formation": entry["opp"].formation if entry["opp"] else None,
                    "total": entry["opp"].expected_total if entry["opp"] else None,
                    "sd": lineup_spread(entry["opp"], rec.settings) if entry["opp"] else None,
                    "serieA": match.serie_a_day if match else None,
                    "leagueDay": match.league_day if match else None,
                    "played": self._played(match, me) if match and match.score else None,
                },
                "others": [n for n in league.team_names() if n != me],
                "schedule": self._schedule(calendar, me),
                "hasThresholds": bool(info["thresholds"]),
                "calendarNote": info["note"],
            }
            return data

    def api_cambio(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            entry = self.entries.get(body.get("recId", ""))
            if entry is None:
                raise ApiError("Calcolo scaduto: premi Calcola di nuovo.")
            rec = entry["rec"]
            index = int(body.get("index", 0))
            base = rec.lineups[index]
            current = base
            try:
                for swap in body.get("swaps", []):
                    current = swap_players(current, swap["out"], rec.projections[swap["in"]], rec.settings)
            except (ValueError, KeyError) as exc:
                raise ApiError(str(exc)) from exc
            return {
                "lineup": self._lineup(current, rec.projections),
                "marks": swap_marks(base, current),
                "compare": self._compare(entry, base, current, True),
                "duel": self._duel(entry, current),
            }

    # -- serializzazione ------------------------------------------------

    @staticmethod
    def _player(rec: Any, best: Lineup, pid: str) -> dict[str, Any]:
        projection = rec.projections[pid]
        player, matchup = projection.player, projection.matchup
        outlook = rec.outlook_by_team.get(player.team)
        title, lines = describe_projection(projection, captain=False, outlook=outlook)
        if player.status in {"infortunato", "squalificato"}:
            state = "out"
        elif player.status == "in dubbio" or player.starter_prob < 0.50:
            state = "doubt"
        else:
            state = ""
        opp = matchup.opponent_code or "N/D"
        return {
            "id": pid, "name": player.name, "team": player.team, "role": player.role,
            "points": projection.expected_points, "prob": player.starter_prob,
            "opp": opp, "difficulty": matchup.difficulty if matchup.opponent_code else 3.0,
            "verdict": outlook.verdict(player.team) if outlook else "", "state": state,
            "title": title, "lines": lines,
        }

    @staticmethod
    def _lineup(lineup: Lineup, projections: dict[str, Any]) -> dict[str, Any]:
        bench = sorted(lineup.bench, key=lambda p: (ROLE_ORDER.get(p.player.role, 9), -p.expected_points))
        ids = lambda items: [p.player.id for p in items]  # noqa: E731
        used = {p.player.id for p in lineup.starters} | {p.player.id for p in lineup.bench}
        stand = sorted(
            (p for pid, p in projections.items() if pid not in used),
            key=lambda p: (ROLE_ORDER.get(p.player.role, 9), -p.expected_points, p.player.name),
        )
        return {
            "formation": lineup.formation, "total": lineup.expected_total,
            "modifier": lineup.defense_modifier, "avgProb": lineup.avg_starter_prob,
            "risk": lineup.risk, "captain": lineup.captain.player.id,
            "P": [lineup.goalkeeper.player.id], "D": ids(lineup.defenders),
            "C": ids(lineup.midfielders), "A": ids(lineup.forwards),
            "bench": ids(bench), "stand": ids(stand),
        }

    
    @staticmethod
    def _outlook(o: Any) -> dict[str, Any]:
        favorite = o.favorite()
        goals = o.real_score or o.score
        return {
            "home": o.home, "away": o.away, "pHome": o.p_home, "pDraw": o.p_draw, "pAway": o.p_away,
            "favorite": favorite, "score": list(goals), "real": bool(o.real_score),
        }

    @staticmethod
    def _played(match: Any, me: str) -> list[int]:
        return list(match.score if me == match.home else match.score[::-1])

    @staticmethod
    def _schedule(calendar: list[Any], me: str) -> list[dict[str, Any]]:
        rows = []
        for match in sorted((m for m in calendar if me in (m.home, m.away)), key=lambda m: m.league_day):
            at_home = me == match.home
            rows.append({
                "day": match.day, "leagueDay": match.league_day, "serieA": match.serie_a_day,
                "home": at_home, "opponent": match.away if at_home else match.home,
                "result": Session._played(match, me) if match.score else None,
            })
        return rows

    @staticmethod
    def _duel(entry: dict[str, Any], lineup: Lineup) -> dict[str, Any] | None:
        opponent = entry["opp"]
        if opponent is None:
            return None
        rec, thresholds = entry["rec"], entry["info"]["thresholds"]
        win, draw, loss = match_outcome(lineup, opponent, rec.settings, thresholds)
        data = {"win": win, "draw": draw, "loss": loss, "sd": lineup_spread(lineup, rec.settings)}
        if thresholds:
            data["goals"] = expected_goals_of(lineup, rec.settings, thresholds)
            data["oppGoals"] = expected_goals_of(opponent, rec.settings, thresholds)
        return data

    def _compare(self, entry: dict[str, Any], base: Lineup, lineup: Lineup, edited: bool) -> dict[str, Any]:
        rec = entry["rec"]
        settings = rec.settings
        modifier, average, best = defense_modifier(lineup.goalkeeper, lineup.defenders, settings)
        text = f"Modificatore {_signed(modifier)} (media {average:.2f}: portiere {lineup.goalkeeper.expected_raw_vote:.2f}"
        if best:
            text += "; difensori " + ", ".join(f"{vote:.2f}" for vote in best)
        text += ")"
        if len(lineup.defenders) < settings.min_difensori_modificatore:
            text += f" Non applicato: servono almeno {settings.min_difensori_modificatore} difensori a voto."
        rows: list[dict[str, str]] = []
        warn = ""
        if edited:
            for label, before, after, symbol, delta, tag in comparison_rows(base, lineup):
                rows.append({"label": label, "before": before, "after": after,
                             "symbol": symbol, "delta": delta, "tag": tag})
            if entry["opp"] is not None:
                one, two = self._duel(entry, base), self._duel(entry, lineup)
                change = (two["win"] - one["win"]) * 100
                symbol, tag = arrow(change, tolerance=0.5)
                rows.append({"label": "Vittoria nella sfida", "before": f"{one['win']:.0%}",
                             "after": f"{two['win']:.0%}", "symbol": symbol, "delta": f"{change:+.0f}%", "tag": tag})
            limit = settings.max_per_squadra_serie_a
            if limit > 0:
                counts: dict[str, int] = {}
                for p in lineup.starters:
                    counts[p.player.team] = counts.get(p.player.team, 0) + 1
                if any(n > limit for n in counts.values()):
                    warn = f"Attenzione: più di {limit} giocatori della stessa squadra."
        return {"defense": text, "rows": rows, "warn": warn}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_handler(session: Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FantaLineup"

        def log_message(self, *_args: Any) -> None:  # silenzioso
            pass

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            if path == "/fantalineup.css":
                target = THEME_DIR / "fantalineup.css"
            else:
                target = (WEB_DIR / path.lstrip("/")).resolve()
                if WEB_DIR.resolve() not in target.parents:
                    return self._send(HTTPStatus.NOT_FOUND, b"404", "text/plain")
            if not target.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"404", "text/plain")
            self._send(200, target.read_bytes(), MIME.get(target.suffix.lower(), "application/octet-stream"))

        def _api(self, call: Any) -> None:
            try:
                self._json(call())
            except ApiError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001 - non far cadere il server
                import traceback

                traceback.print_exc()
                self._json({"error": f"Errore interno: {exc}"}, 500)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/api/init":
                return self._api(session.api_init)
            if url.path == "/api/lega":
                address = parse_qs(url.query).get("address", ["demo"])[0]
                return self._api(lambda: session.api_lega(address))
            self._static(url.path)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "Richiesta non valida."}, 400)
            path = urlparse(self.path).path
            if path == "/api/formazione":
                return self._api(lambda: session.api_formazione(body))
            if path == "/api/cambio":
                return self._api(lambda: session.api_cambio(body))
            self._json({"error": "Percorso sconosciuto."}, 404)

    return Handler


def launch_web(args: Any = None, port: int = 8765, open_browser: bool = True) -> int:
    data_dir = resolve_data_dir(getattr(args, "data_dir", None))
    session = Session(data_dir, args)
    server = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), make_handler(session))
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        print("Nessuna porta libera tra", port, "e", port + 19)
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"FantaLineup web: {url}  (Ctrl+C per chiudere)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChiuso.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch_web())