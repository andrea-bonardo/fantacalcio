"""Interfaccia grafica (tkinter) di FantaLineup 26/27.

La finestra mostra la formazione consigliata disegnata sul campo, l'elenco dei
moduli con il punteggio atteso (cliccando un modulo il campo si ridisegna), i
dettagli del giocatore selezionato, il modificatore difesa, la panchina e il
calendario della giornata con l'esito atteso di ogni partita.

Si possono provare dei cambi (titolare ⇄ panchina, stesso ruolo): le frecce
mostrano come variano punti attesi, media difesa e titolarità rispetto alla
formazione consigliata.

Il calcolo vero e proprio è in ``compute_recommendation`` (nessuna dipendenza da
tkinter); il resto del file si occupa solo di disegnare i risultati.
"""

from __future__ import annotations

import dataclasses
import json
import math
import queue
import sys
import threading
import traceback
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .config import ROLE_LABEL, ROLE_ORDER
from .dataio import (
    BASE_DIR,
    DataError,
    ensure_data,
    load_players,
    load_serie_a,
    load_settings,
    read_json,
    resolve_data_dir,
)
from .engine import ProjectionEngine
from .league import load_league
from .matchup import MatchupModel
from .models import Fixture, League, LeagueSettings, Lineup, PlayerProjection
from .optimizer import LineupError, LineupOptimizer, defense_modifier
from .theme import Theme, box, hex_to_rgb, load_theme, pick_font, px
from .xlsxio import XLSX_SUFFIXES, LeagueMatch, is_calendar_workbook, read_league_calendar

SHOWN = False  # True dopo che la finestra è stata aperta e chiusa

STATE_FILE = "ultima_scelta.json"
DEMO_LABEL = "Lega demo (8 squadre di esempio)"

MAX_ROW_HEIGHT = 24  # le tabelle stanno in una colonna stretta: --row-height del CSS viene limitato


class GuiUnavailable(RuntimeError):
    """La finestra non può essere aperta (nessuno schermo disponibile)."""


# ---------------------------------------------------------------------------
# Calcolo (senza tkinter)
# ---------------------------------------------------------------------------


@dataclass
class MatchOutlook:
    """Esito atteso di una partita, stimato dai gol attesi delle due squadre (Poisson)."""

    home: str
    away: str
    lam_home: float
    lam_away: float
    p_home: float
    p_draw: float
    p_away: float
    score: tuple[int, int]  # risultato più probabile
    real_score: tuple[int, int] | None = None  # se la partita è già stata giocata

    def for_team(self, code: str) -> tuple[float, float, float]:
        """(vittoria, pareggio, sconfitta) dal punto di vista della squadra."""
        if code == self.home:
            return self.p_home, self.p_draw, self.p_away
        return self.p_away, self.p_draw, self.p_home

    def verdict(self, code: str) -> str:
        """'V' vittoria attesa, 'S' sconfitta attesa, '=' partita equilibrata."""
        win, _, loss = self.for_team(code)
        if win - loss >= 0.15:
            return "V"
        if loss - win >= 0.15:
            return "S"
        return "="

    def favorite(self) -> str | None:
        if self.p_home - self.p_away >= 0.15:
            return self.home
        if self.p_away - self.p_home >= 0.15:
            return self.away
        return None


def _poisson(mean: float, size: int = 11) -> list[float]:
    probs = [math.exp(-mean)]
    for goals in range(1, size):
        probs.append(probs[-1] * mean / goals)
    return probs


def match_outlook(model: MatchupModel, fixture: Fixture) -> MatchOutlook:
    lam_home, lam_away = model.expected_goals(fixture.home, fixture.away, True)
    home, away = _poisson(lam_home), _poisson(lam_away)
    p_home = p_draw = p_away = 0.0
    best, best_p = (0, 0), -1.0
    for h, ph in enumerate(home):
        for a, pa in enumerate(away):
            p = ph * pa
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
            if p > best_p:
                best, best_p = (h, a), p
    total = p_home + p_draw + p_away
    real = (fixture.home_goals, fixture.away_goals) if fixture.played else None
    return MatchOutlook(
        home=fixture.home, away=fixture.away, lam_home=lam_home, lam_away=lam_away,
        p_home=p_home / total, p_draw=p_draw / total, p_away=p_away / total,
        score=best, real_score=real,  # type: ignore[arg-type]
    )


@dataclass
class Recommendation:
    team_name: str
    matchday: int
    settings: LeagueSettings
    lineups: list[Lineup]  # la migliore per prima, poi le alternative
    projections: dict[str, PlayerProjection]
    warnings: list[str] = field(default_factory=list)
    evaluated: int = 0
    outlooks: list[MatchOutlook] = field(default_factory=list)
    outlook_by_team: dict[str, MatchOutlook] = field(default_factory=dict)


def compute_recommendation(league: League, team: Any, matchday: int | None = None) -> Recommendation:
    settings = league.settings.clone()
    serie_a = league.serie_a
    matchday = matchday or league.matchday or serie_a.current_matchday

    model = MatchupModel(serie_a, settings)
    engine = ProjectionEngine(settings, model)
    projections = engine.project_team(team, matchday)
    if not projections:
        raise LineupError(f"La squadra '{team.name}' non ha giocatori nella rosa.")

    warnings = list(league.warnings)
    missing = [p.player.name for p in projections if p.matchup.label == "Da definire"]
    if missing:
        warnings.append(
            f"Avversario non trovato per la giornata {matchday}: "
            + ", ".join(missing[:5])
            + ("..." if len(missing) > 5 else "")
        )

    optimizer = LineupOptimizer(
        projections, settings, max_alternatives=max(1, len(settings.moduli))
    )
    result = optimizer.optimize()
    warnings.extend(result.warnings)

    alternatives = sorted(result.alternatives, key=lambda lineup: -lineup.objective)
    fixtures = sorted(
        (f for f in serie_a.fixtures if f.matchday == matchday),
        key=lambda f: (f.date or "", f.time or ""),
    )
    outlooks = [match_outlook(model, fixture) for fixture in fixtures]
    by_team = {}
    for outlook in outlooks:
        by_team[outlook.home] = outlook
        by_team[outlook.away] = outlook
    return Recommendation(
        team_name=team.name,
        matchday=matchday,
        settings=settings,
        lineups=[result.best, *alternatives],
        projections={p.player.id: p for p in projections},
        warnings=warnings,
        evaluated=result.evaluated,
        outlooks=outlooks,
        outlook_by_team=by_team,
    )


# ---------------------------------------------------------------------------
# Cambi rispetto alla formazione consigliata
# ---------------------------------------------------------------------------


def swap_players(
    lineup: Lineup,
    out_id: str,
    incoming: PlayerProjection,
    settings: LeagueSettings,
) -> Lineup:
    """Scambia un titolare con un giocatore della panchina dello stesso ruolo."""
    outgoing = next((p for p in lineup.starters if p.player.id == out_id), None)
    if outgoing is None:
        raise ValueError("Il giocatore da sostituire non è tra i titolari.")
    if outgoing.player.role != incoming.player.role:
        raise ValueError("Il cambio deve essere tra giocatori dello stesso ruolo.")

    def replace(items: list[PlayerProjection]) -> list[PlayerProjection]:
        return [incoming if p.player.id == out_id else p for p in items]

    goalkeeper = incoming if lineup.goalkeeper.player.id == out_id else lineup.goalkeeper
    defenders = replace(lineup.defenders)
    midfielders = replace(lineup.midfielders)
    forwards = replace(lineup.forwards)
    starters = [goalkeeper, *defenders, *midfielders, *forwards]
    modifier, average, _ = defense_modifier(goalkeeper, defenders, settings)
    total = sum(p.expected_points for p in starters) + modifier
    risk = sum(p.risk for p in starters)
    bench = [outgoing if p.player.id == incoming.player.id else p for p in lineup.bench]
    bench.sort(key=lambda p: (p.player.role_order, -p.expected_points, p.player.name))
    return Lineup(
        formation=lineup.formation,
        goalkeeper=goalkeeper,
        defenders=defenders,
        midfielders=midfielders,
        forwards=forwards,
        bench=bench,
        defense_modifier=modifier,
        defense_average=average,
        expected_total=total,
        avg_starter_prob=sum(p.player.starter_prob for p in starters) / len(starters),
        risk=risk,
        objective=total - settings.rischio * risk,
    )


def swap_marks(base: Lineup, current: Lineup) -> dict[str, float]:
    """Per ogni titolare cambiato, di quanto cambiano i punti rispetto a chi è uscito."""
    base_ids = {p.player.id for p in base.starters}
    current_ids = {p.player.id for p in current.starters}
    removed = [p for p in base.starters if p.player.id not in current_ids]
    added = [p for p in current.starters if p.player.id not in base_ids]
    marks: dict[str, float] = {}
    for role in "PDCA":
        out = sorted((p for p in removed if p.player.role == role), key=lambda p: -p.expected_points)
        new = sorted((p for p in added if p.player.role == role), key=lambda p: -p.expected_points)
        for outgoing, incoming in zip(out, new):
            marks[incoming.player.id] = incoming.expected_points - outgoing.expected_points
    return marks


def arrow(delta: float, higher_is_better: bool = True, tolerance: float = 0.005) -> tuple[str, str]:
    """(freccia, tag): ▲/▼ e verde/rosso a seconda che il cambiamento sia favorevole."""
    if abs(delta) < tolerance:
        return "=", "flat"
    up = delta > 0
    return ("▲" if up else "▼"), ("good" if up == higher_is_better else "bad")


def comparison_rows(base: Lineup, current: Lineup) -> list[tuple[str, str, str, str, str, str]]:
    """Righe (etichetta, prima, dopo, freccia, variazione, tag) del confronto con la consigliata."""
    rows = []

    def add(label: str, before: str, after: str, delta: float, text: str, better_up: bool, tol: float) -> None:
        symbol, tag = arrow(delta, better_up, tol)
        rows.append((label, before, after, symbol, text.format(delta), tag))

    add("Punti attesi", f"{base.expected_total:.2f}", f"{current.expected_total:.2f}",
        current.expected_total - base.expected_total, "{:+.2f}", True, 0.005)
    add("Media difesa", f"{base.defense_average:.2f}", f"{current.defense_average:.2f}",
        current.defense_average - base.defense_average, "{:+.2f}", True, 0.005)
    add("Modificatore", _signed(base.defense_modifier), _signed(current.defense_modifier),
        current.defense_modifier - base.defense_modifier, "{:+.2f}", True, 0.005)
    add("Titolarità media", f"{base.avg_starter_prob:.0%}", f"{current.avg_starter_prob:.0%}",
        (current.avg_starter_prob - base.avg_starter_prob) * 100, "{:+.0f}%", True, 0.5)
    add("Rischio", f"{base.risk:.2f}", f"{current.risk:.2f}",
        current.risk - base.risk, "{:+.2f}", False, 0.005)
    return rows


# ---------------------------------------------------------------------------
# Sfida contro l'avversario della lega
# ---------------------------------------------------------------------------

PERFORMANCE_SD = 1.7  # variabilità tipica del fantavoto di un singolo giocatore


def lineup_spread(lineup: Lineup, settings: LeagueSettings) -> float:
    """Deviazione standard stimata del punteggio di una formazione."""
    fallback = settings.voto_sostituto_modificatore
    variance = 0.0
    for projection in lineup.starters:
        prob = projection.player.starter_prob
        gap = projection.expected_vote - fallback
        variance += PERFORMANCE_SD**2 + prob * (1.0 - prob) * gap * gap
    return math.sqrt(variance)


def _normal_cdf(x: float, mean: float, sd: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (sd * math.sqrt(2.0))))


def goal_distribution(mean: float, sd: float, thresholds: list[float]) -> list[float]:
    """Probabilità di fare 0, 1, 2... gol dato il punteggio atteso e le soglie punti→gol."""
    cumulative = [0.0] + [_normal_cdf(t, mean, sd) for t in thresholds] + [1.0]
    return [cumulative[i + 1] - cumulative[i] for i in range(len(thresholds) + 1)]


def match_outcome(
    mine: Lineup,
    theirs: Lineup,
    settings: LeagueSettings,
    thresholds: list[float] | None = None,
) -> tuple[float, float, float]:
    """(vittoria, pareggio, sconfitta) di una sfida di lega.

    Con le soglie punti→gol della lega si confrontano i gol (il pareggio esiste);
    senza soglie si confrontano i punti totali.
    """
    sd_mine, sd_theirs = lineup_spread(mine, settings), lineup_spread(theirs, settings)
    if not thresholds:
        spread = math.hypot(sd_mine, sd_theirs)
        win = _normal_cdf(mine.expected_total - theirs.expected_total, 0.0, spread)
        return win, 0.0, 1.0 - win
    dist_mine = goal_distribution(mine.expected_total, sd_mine, thresholds)
    dist_theirs = goal_distribution(theirs.expected_total, sd_theirs, thresholds)
    win = draw = loss = 0.0
    for goals_mine, p_mine in enumerate(dist_mine):
        for goals_theirs, p_theirs in enumerate(dist_theirs):
            p = p_mine * p_theirs
            if goals_mine > goals_theirs:
                win += p
            elif goals_mine == goals_theirs:
                draw += p
            else:
                loss += p
    return win, draw, loss


def expected_goals_of(lineup: Lineup, settings: LeagueSettings, thresholds: list[float]) -> float:
    dist = goal_distribution(lineup.expected_total, lineup_spread(lineup, settings), thresholds)
    return sum(goals * p for goals, p in enumerate(dist))


def infer_goal_thresholds(matches: list[LeagueMatch]) -> list[float] | None:
    """Ricava dalle partite già giocate come la lega trasforma i punti in gol.

    Cerca soglie equidistanti (es. 66, 71, 76...) coerenti con tutti i risultati;
    restituisce None se i dati sono pochi o non tornano.
    """
    samples: list[tuple[float, int]] = []
    for match in matches:
        if match.score and match.home_points is not None and match.away_points is not None:
            samples.append((match.home_points, match.score[0]))
            samples.append((match.away_points, match.score[1]))
    if len(samples) < 10:
        return None
    for first in [x / 2 for x in range(120, 145)]:  # 60 ... 72
        for step in [x / 2 for x in range(6, 17)]:  # 3 ... 8
            thresholds = [first + step * k for k in range(12)]
            if all(sum(1 for t in thresholds if points >= t) == goals for points, goals in samples):
                return thresholds
    return None


def parse_json_calendar(raw: Any) -> list[LeagueMatch]:
    """Calendario dal JSON della lega: chiave 'calendario' con giornata, casa, trasferta."""
    data = raw.get("calendario", raw.get("calendar")) if isinstance(raw, dict) else None
    result: list[LeagueMatch] = []
    if isinstance(data, dict):
        for key, matches in data.items():
            for match in matches or []:
                if isinstance(match, (list, tuple)) and len(match) >= 2:
                    result.append(LeagueMatch(int(key), str(match[0]), str(match[1])))
    elif isinstance(data, list):
        for item in data:
            try:
                if isinstance(item, dict):
                    day = item.get("giornata", item.get("matchday"))
                    home = item.get("casa", item.get("home"))
                    away = item.get("trasferta", item.get("away"))
                    result.append(LeagueMatch(int(day), str(home), str(away),
                                              serie_a_day=item.get("giornata_serie_a")))
                elif isinstance(item, (list, tuple)) and len(item) >= 3:
                    result.append(LeagueMatch(int(item[0]), str(item[1]), str(item[2])))
            except (TypeError, ValueError):
                continue
    return result


def load_league_calendar(address: str, data_dir: Path) -> tuple[list[LeagueMatch], str]:
    """Cerca il calendario della lega: nel file della lega, poi nei file Excel di calendario
    presenti nella cartella dati o in quella del programma. Restituisce (partite, nome file)."""
    candidates: list[Path] = []
    if address and address != "demo" and "://" not in address:
        candidates.append(Path(address).expanduser())
    for directory in (data_dir, BASE_DIR):
        if directory.is_dir():
            candidates.extend(sorted(directory.glob("*.xlsx")))
            candidates.extend(sorted(directory.glob("calendario*.json")))
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved in seen or not path.is_file() or path.name.startswith("~$"):
                continue
            seen.add(resolved)
            if path.suffix.lower() in XLSX_SUFFIXES:
                matches = read_league_calendar(path)
            elif path.suffix.lower() == ".json":
                matches = parse_json_calendar(read_json(path))
            else:
                continue
        except Exception:  # noqa: BLE001 - il calendario è facoltativo
            continue
        if matches:
            return matches, path.name
    return [], ""


def _signed(value: float) -> str:
    return "0.00" if abs(value) < 0.005 else f"{value:+.2f}"


def describe_projection(
    projection: PlayerProjection,
    *,
    captain: bool = False,
    outlook: MatchOutlook | None = None,
) -> tuple[str, list[str]]:
    """Titolo e righe di dettaglio per il pannello del giocatore."""
    player = projection.player
    matchup = projection.matchup
    title = f"{player.name} ({player.team})" + ("  ·  Capitano consigliato" if captain else "")
    lines = [ROLE_LABEL.get(player.role, player.role)]
    if matchup.opponent_code:
        lines.append(
            f"Avversario: {matchup.opponent_name} ({'casa' if matchup.home else 'trasferta'})"
            f" - {matchup.label} {matchup.stars} ({matchup.difficulty:.1f}/5)"
        )
    else:
        lines.append("Avversario: da definire")
    if outlook is not None:
        win, draw, loss = outlook.for_team(player.team)
        verdict = {"V": "squadra favorita", "S": "squadra sfavorita", "=": "partita equilibrata"}[
            outlook.verdict(player.team)
        ]
        lines.append(f"Esito atteso: {verdict} (vittoria {win:.0%}, pari {draw:.0%}, sconfitta {loss:.0%})")
    lines.append(f"Titolarità {player.starter_prob:.0%}  ·  voto atteso {projection.expected_raw_vote:.2f}")
    lines.append(
        f"Bonus {_signed(projection.expected_bonus)}  ·  malus {_signed(projection.expected_malus)}"
        f"  →  punti attesi {projection.expected_points:.2f}"
    )
    if player.role != "P":
        lines.append(f"Probabilità di gol {projection.goal_prob:.0%}  ·  di assist {projection.assist_prob:.0%}")
    if projection.notes:
        lines.append("Note: " + ", ".join(projection.notes))
    return title, lines


# ---------------------------------------------------------------------------
# Colori
# ---------------------------------------------------------------------------

_STOPS = [(1.0, (46, 158, 79)), (3.0, (224, 176, 32)), (5.0, (210, 69, 59))]


def set_difficulty_scale(theme: Theme) -> None:
    """Costruisce la scala di difficoltà (verde → giallo → rosso) dai token --diff-1/3/5."""
    global _STOPS
    _STOPS = [
        (1.0, hex_to_rgb(theme.color("diff-1"))),
        (3.0, hex_to_rgb(theme.color("diff-3"))),
        (5.0, hex_to_rgb(theme.color("diff-5"))),
    ]


def _difficulty_rgb(difficulty: float) -> tuple[int, int, int]:
    difficulty = max(1.0, min(5.0, difficulty))
    for (low, color_low), (high, color_high) in zip(_STOPS, _STOPS[1:]):
        if difficulty <= high:
            t = (difficulty - low) / (high - low)
            return tuple(int(color_low[i] + (color_high[i] - color_low[i]) * t) for i in range(3))  # type: ignore[return-value]
    return _STOPS[-1][1]


def difficulty_color(difficulty: float) -> str:
    return "#%02x%02x%02x" % _difficulty_rgb(difficulty)


def text_color_on(difficulty: float) -> str:
    red, green, blue = _difficulty_rgb(difficulty)
    return "#1b1b1b" if 0.299 * red + 0.587 * green + 0.114 * blue > 150 else "#ffffff"


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


# ---------------------------------------------------------------------------
# Campo da calcio
# ---------------------------------------------------------------------------


class PitchCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc, theme: Theme, on_select: Callable[[str], None]):
        self.theme = theme
        self.font = pick_font(theme, master)
        t = theme
        self.bg = t.prop(".pitch", "background", t.color("bg"))
        self.grass_a = t.prop(".pitch-grass-a", "background", t.color("grass-a"))
        self.grass_b = t.prop(".pitch-grass-b", "background", t.color("grass-b"))
        self.line = t.prop(".pitch-line", "border-color", t.color("pitch-line"))
        self.line_width = px(t.prop(".pitch-line", "border-width"), 2)
        self.shadow = t.prop(".pitch-shadow", "background", t.color("pitch-shadow"))
        self.muted = t.prop(".pitch-legend", "color", t.color("text-muted"))
        self.verdict_colors = {
            "V": t.prop(".badge-verdict-v", "background", t.color("verdict-v")),
            "S": t.prop(".badge-verdict-s", "background", t.color("verdict-s")),
            "=": t.prop(".badge-verdict-e", "background", t.color("verdict-e")),
        }
        self.captain_color = t.prop(".badge-captain", "background", t.color("captain"))
        self.captain_text = t.prop(".badge-captain", "color", t.color("captain-text"))
        self.role_color = t.prop(".badge-role", "background", "#0b2b1d")
        self.role_text = t.prop(".badge-role", "color", "#ffffff")
        self.ring_default = t.prop(".player-ring", "border-color", t.color("ring-default"))
        super().__init__(master, background=self.bg, highlightthickness=0)
        self.on_select = on_select
        self.lineup: Lineup | None = None
        self.captain_id: str | None = None
        self.selected_id: str | None = None
        self.swap_out_id: str | None = None
        self.verdicts: dict[str, str] = {}
        self.marks: dict[str, float] = {}
        self.bind("<Configure>", lambda _event: self.redraw())

    def show(
        self,
        lineup: Lineup | None,
        captain_id: str | None,
        selected_id: str | None = None,
        swap_out_id: str | None = None,
        verdicts: dict[str, str] | None = None,
        marks: dict[str, float] | None = None,
    ) -> None:
        self.lineup = lineup
        self.captain_id = captain_id
        self.selected_id = selected_id
        self.swap_out_id = swap_out_id
        self.verdicts = verdicts or {}
        self.marks = marks or {}
        self.redraw()

    def select(self, player_id: str | None, swap_out_id: str | None = None) -> None:
        self.selected_id = player_id
        self.swap_out_id = swap_out_id
        self.redraw()

    # -- stile ------------------------------------------------------------

    def _ring(self, state: str) -> tuple[str, int]:
        """(colore, spessore) del bordo del gettone, dalle regole ``.player-ring.is-<stato>``."""
        selector = ".player-ring" if state == "default" else f".player-ring.is-{state}"
        fallback = {"default": 2, "doubt": 4, "out": 4, "selected": 3, "swap": 2}[state]
        color = self.theme.prop(selector, "border-color", self.theme.color(f"ring-{state}"))
        return color, px(self.theme.prop(selector, "border-width"), fallback)

    # -- disegno --------------------------------------------------------

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 300)
        height = max(self.winfo_height(), 300)
        pad = 12
        x0, y0, x1, y1 = pad, pad, width - pad, height - pad - 46
        self._draw_field(x0, y0, x1, y1)
        self._draw_legend(x0, y1 + 16)
        if self.lineup is None:
            self.create_text(
                width / 2, (y0 + y1) / 2, text="Scegli una lega e una squadra",
                fill=self.theme.prop(".pitch-empty", "color", self.line),
                font=(self.font, self.theme.size(".pitch-empty", 14)),
            )
            return

        lineup = self.lineup
        rows = [
            (lineup.forwards, 0.13),
            (lineup.midfielders, 0.38),
            (lineup.defenders, 0.63),
            ([lineup.goalkeeper], 0.87),
        ]
        widest = max(len(row) for row, _ in rows)
        radius = int(max(16, min(30, (x1 - x0) / (widest + 1) / 2.4, (y1 - y0) / 4 / 2.9)))
        for players, fraction in rows:
            count = len(players)
            for index, projection in enumerate(players):
                cx = x0 + (x1 - x0) * (index + 1) / (count + 1)
                cy = y0 + (y1 - y0) * fraction
                slot = (x1 - x0) / (count + 1)
                self._draw_player(projection, cx, cy, radius, slot)

    def _draw_field(self, x0: float, y0: float, x1: float, y1: float) -> None:
        fw, fh = x1 - x0, y1 - y0
        self.create_rectangle(x0, y0, x1, y1, fill=self.grass_a, outline="")
        stripe = fh / 8
        for i in range(1, 8, 2):
            self.create_rectangle(x0, y0 + i * stripe, x1, y0 + (i + 1) * stripe, fill=self.grass_b, outline="")
        self.create_rectangle(x0, y0, x1, y1, outline=self.line, width=self.line_width)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.create_line(x0, cy, x1, cy, fill=self.line, width=self.line_width)
        circle = min(fw, fh) * 0.12
        self.create_oval(cx - circle, cy - circle, cx + circle, cy + circle, outline=self.line, width=self.line_width)
        for top in (True, False):
            box_w, box_h = fw * 0.50, fh * 0.15
            goal_w, goal_h = fw * 0.24, fh * 0.055
            if top:
                self.create_rectangle(cx - box_w / 2, y0, cx + box_w / 2, y0 + box_h, outline=self.line, width=self.line_width)
                self.create_rectangle(cx - goal_w / 2, y0, cx + goal_w / 2, y0 + goal_h, outline=self.line, width=self.line_width)
            else:
                self.create_rectangle(cx - box_w / 2, y1 - box_h, cx + box_w / 2, y1, outline=self.line, width=self.line_width)
                self.create_rectangle(cx - goal_w / 2, y1 - goal_h, cx + goal_w / 2, y1, outline=self.line, width=self.line_width)

    def _draw_legend(self, x: float, y: float) -> None:
        font = (self.font, self.theme.size(".pitch-legend", 8))
        muted = self.muted
        # riga 1: difficoltà dell'avversario ed esito atteso della partita
        x1 = x
        self.create_text(x1, y, text="Avversario:  facile", anchor="w", fill=muted, font=font)
        x1 += 112
        for difficulty in (1, 2, 3, 4, 5):
            self.create_oval(x1, y - 6, x1 + 12, y + 6, fill=difficulty_color(difficulty), outline=self.ring_default)
            x1 += 15
        self.create_text(x1 + 2, y, text="difficile", anchor="w", fill=muted, font=font)
        x1 += 84
        for letter, color, label, width in (
            ("V", self.verdict_colors["V"], "vittoria attesa", 116),
            ("S", self.verdict_colors["S"], "sconfitta attesa", 122),
            ("=", self.verdict_colors["="], "equilibrata", 80),
        ):
            self.create_oval(x1, y - 7, x1 + 14, y + 7, fill=color, outline=self.ring_default)
            self.create_text(x1 + 7, y, text=letter, fill=self.role_text, font=(self.font, 8, "bold"))
            self.create_text(x1 + 18, y, text=label, anchor="w", fill=muted, font=font)
            x1 += width
        # riga 2: segnali sul giocatore
        y2 = y + 18
        x2 = x
        self.create_oval(x2, y2 - 6, x2 + 12, y2 + 6, fill=self.captain_color, outline="")
        self.create_text(x2 + 16, y2, text="capitano", anchor="w", fill=muted, font=font)
        x2 += 78
        self.create_oval(x2, y2 - 6, x2 + 12, y2 + 6, outline=self._ring("doubt")[0], width=3)
        self.create_text(x2 + 16, y2, text="titolarità incerta", anchor="w", fill=muted, font=font)
        x2 += 124
        self.create_oval(x2, y2 - 6, x2 + 12, y2 + 6, outline=self._ring("out")[0], width=3)
        self.create_text(x2 + 16, y2, text="indisponibile", anchor="w", fill=muted, font=font)
        x2 += 100
        self.create_oval(x2, y2 - 6, x2 + 12, y2 + 6, outline=self._ring("swap")[0], width=2, dash=(3, 2))
        self.create_text(x2 + 16, y2, text="titolare da sostituire", anchor="w", fill=muted, font=font)

    def _draw_player(self, projection: PlayerProjection, cx: float, cy: float, r: int, slot: float) -> None:
        player = projection.player
        matchup = projection.matchup
        tag = "player:" + str(id(projection))
        tags = (tag,)

        if player.id == self.selected_id:
            color, width = self._ring("selected")
            self.create_oval(
                cx - r - 7, cy - r - 7, cx + r + 7, cy + r + 7,
                outline=color, width=width, tags=tags,
            )
        elif player.id == self.swap_out_id:
            color, width = self._ring("swap")
            self.create_oval(
                cx - r - 7, cy - r - 7, cx + r + 7, cy + r + 7,
                outline=color, width=width, dash=(4, 3), tags=tags,
            )

        ring, ring_width = self._ring("default")
        if player.status in {"infortunato", "squalificato"}:
            ring, ring_width = self._ring("out")
        elif player.status == "in dubbio" or player.starter_prob < 0.50:
            ring, ring_width = self._ring("doubt")

        fill = difficulty_color(matchup.difficulty)
        self.create_oval(cx - r + 2, cy - r + 3, cx + r + 2, cy + r + 3, fill=self.shadow, outline="", tags=tags)
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=ring, width=ring_width, tags=tags)
        self.create_text(
            cx, cy, text=f"{projection.expected_points:.1f}",
            fill=text_color_on(matchup.difficulty),
            font=(self.font, max(9, int(r * 0.52)), "bold"), tags=tags,
        )

        badge = max(7, int(r * 0.36))
        self.create_oval(cx - r - 2, cy - r - 2, cx - r - 2 + 2 * badge, cy - r - 2 + 2 * badge,
                         fill=self.role_color, outline=self.ring_default, tags=tags)
        self.create_text(cx - r - 2 + badge, cy - r - 2 + badge, text=player.role, fill=self.role_text,
                         font=(self.font, max(7, badge - 1), "bold"), tags=tags)
        if player.id == self.captain_id:
            self.create_oval(cx + r - 2 * badge + 2, cy - r - 2, cx + r + 2, cy - r - 2 + 2 * badge,
                             fill=self.captain_color, outline=self.captain_text, tags=tags)
            self.create_text(cx + r - badge + 2, cy - r - 2 + badge, text="C", fill=self.captain_text,
                             font=(self.font, max(7, badge - 1), "bold"), tags=tags)

        verdict = self.verdicts.get(player.team)
        if verdict:
            vb = max(7, int(r * 0.36))
            vx, vy = cx + r * 0.72, cy + r * 0.72
            self.create_oval(vx - vb, vy - vb, vx + vb, vy + vb, fill=self.verdict_colors[verdict],
                             outline=self.ring_default, tags=tags)
            self.create_text(vx, vy, text=verdict, fill=self.theme.prop(".badge-verdict-v", "color", "#ffffff"),
                             font=(self.font, max(7, vb - 1), "bold"), tags=tags)

        delta = self.marks.get(player.id)
        if delta is not None:
            symbol, tag_color = arrow(delta)
            chip = self.theme.prop(".delta-chip", "background", "#ffffff")
            color = {
                "good": self.theme.prop(".delta-chip.is-good", "color", "#14733f"),
                "bad": self.theme.prop(".delta-chip.is-bad", "color", "#c62828"),
                "flat": self.theme.prop(".delta-chip", "color", self.muted),
            }[tag_color]
            label = f"{symbol} {delta:+.2f}"
            half = 6 * len(label) / 2 + 5
            top = cy - r - 14
            self.create_rectangle(cx - half, top - 8, cx + half, top + 8, fill=chip, outline=color, tags=tags)
            self.create_text(cx, top, text=label, fill=color, font=(self.font, 8, "bold"), tags=tags)

        size = 9 if r < 24 else 10
        limit = max(8, int(slot / (size * 0.78)))
        name = _short(player.name, limit)
        opponent = f"vs {matchup.opponent_code or 'N/D'}" + (
            f" ({'C' if matchup.home else 'T'})" if matchup.opponent_code else ""
        )
        name_color = self.theme.prop(".player-name", "color", "#ffffff")
        opponent_color = self.theme.prop(".player-opponent", "color", "#d5ecd9")
        for text, dy, color, weight in ((name, r + 12, name_color, "bold"), (opponent, r + 26, opponent_color, "normal")):
            self.create_text(cx + 1, cy + dy + 1, text=text, fill=self.shadow, font=(self.font, size, weight), tags=tags)
            self.create_text(cx, cy + dy, text=text, fill=color, font=(self.font, size, weight), tags=tags)

        self.tag_bind(tag, "<Button-1>", lambda _event, pid=player.id: self.on_select(pid))
        self.tag_bind(tag, "<Enter>", lambda _event: self.config(cursor="hand2"))
        self.tag_bind(tag, "<Leave>", lambda _event: self.config(cursor=""))


# ---------------------------------------------------------------------------
# Finestra principale
# ---------------------------------------------------------------------------


class FantaApp:
    def __init__(
        self,
        root: tk.Tk,
        data_dir: Path,
        args: Any = None,
        preloaded: tuple[dict[str, Any], Any, LeagueSettings] | None = None,
    ):
        self.root = root
        self.data_dir = data_dir
        self.args = args
        self.players: dict[str, Any] = {}
        self.serie_a: Any = None
        self.base_settings: LeagueSettings | None = None
        self.league: League | None = None
        self.rec: Recommendation | None = None
        self.current_address: str | None = None
        self.selected_id: str | None = None
        self._index: int | None = None
        self.edited: dict[int, Lineup] = {}
        self.swap_out_id: str | None = None
        self.calendar: list[LeagueMatch] = []  # partite della lega, con i nomi delle squadre della lega
        self.calendar_note = ""
        self.goal_thresholds: list[float] | None = None
        self.opponent_name: str | None = None
        self.opponent_auto = False
        self.opp_cache: dict[tuple[str, str, int], Recommendation] = {}
        self.opp_errors: dict[tuple[str, str, int], str] = {}
        self._leagues: list[tuple[str, str]] = []
        self._queue: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self._state = self._load_state()

        self.theme = load_theme()  # se il CSS manca o è rotto: tema di emergenza
        set_difficulty_scale(self.theme)
        self.font = pick_font(self.theme, root)
        self._build_ui()
        self._poll()
        if preloaded is not None:
            self._on_base_loaded(preloaded)
        else:
            self._run_async(self._load_base_data, self._on_base_loaded, "Carico i dati della Serie A...")

    # -- interfaccia -----------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title("FantaLineup 26/27")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1280, sw - 40)}x{min(900, sh - 120)}+20+20")
        root.minsize(min(1060, sw - 40), min(640, sh - 120))
        self._setup_modern_style()
        t = self.theme

        top = ttk.Frame(root, style="Toolbar.TFrame", padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Lega", style="Field.TLabel").pack(side="left")
        self.league_box = ttk.Combobox(top, state="readonly", width=40)
        self.league_box.pack(side="left", padx=(6, 4))
        self.league_box.bind("<<ComboboxSelected>>", lambda _e: self._on_league_selected())
        self.browse_button = ttk.Button(top, text="Sfoglia...", command=self._browse)
        self.browse_button.pack(side="left", padx=(0, 16))
        ttk.Label(top, text="Squadra", style="Field.TLabel").pack(side="left")
        self.team_box = ttk.Combobox(top, state="readonly", width=28)
        self.team_box.pack(side="left", padx=(6, 16))
        self.team_box.bind("<<ComboboxSelected>>", lambda _e: self._on_team_selected())
        ttk.Label(top, text="Giornata", style="Field.TLabel").pack(side="left")
        self.matchday_var = tk.StringVar()
        self.matchday_box = ttk.Spinbox(top, from_=1, to=38, width=4, textvariable=self.matchday_var)
        self.matchday_box.pack(side="left", padx=(6, 12))
        self.matchday_box.bind("<Return>", lambda _e: self._calculate())
        self.calc_button = ttk.Button(top, text="Calcola", style="Accent.TButton", command=self._calculate)
        self.calc_button.pack(side="left")

        self.status = ttk.Label(root, style="Status.TLabel", anchor="w")
        self.status.pack(side="bottom", fill="x")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        right = ttk.Frame(body, width=400)
        right.pack(side="right", fill="y", padx=(0, 10), pady=(0, 6))
        right.pack_propagate(False)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.summary = ttk.Label(left, style="Summary.TLabel", padding=(10, 2))
        self.summary.pack(anchor="w")
        self.duel_banner = ttk.Label(left, style="Banner.TLabel", padding=(10, 0, 10, 4))
        self.duel_banner.pack(anchor="w")
        self.pitch = PitchCanvas(left, self.theme, on_select=self._on_pitch_click)
        self.pitch.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 6))

        frame = ttk.LabelFrame(right, text="Moduli", padding=4)
        frame.pack(fill="x")
        self.form_tree = ttk.Treeview(
            frame, columns=("punti", "mod", "tit"), height=8, selectmode="browse"
        )
        self.form_tree.heading("#0", text="Modulo")
        self.form_tree.heading("punti", text="Punti")
        self.form_tree.heading("mod", text="Modif.")
        self.form_tree.heading("tit", text="Titol.")
        self.form_tree.column("#0", width=120, anchor="w")
        for column in ("punti", "mod", "tit"):
            self.form_tree.column(column, width=70, anchor="e")
        self.form_tree.pack(fill="x")
        self.form_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_form_select())

        text_kw = dict(
            background=t.prop(".text-box", "background", t.color("surface")),
            foreground=t.prop(".text-box", "color", t.color("text")),
            selectbackground=t.prop(".text-box-select", "background", t.color("accent-soft")),
            selectforeground=t.color("text"),
            insertbackground=t.color("text"),
            highlightthickness=0,
        )
        small = t.size(".text-box", 9)

        frame = ttk.LabelFrame(right, text="Giocatore", padding=4)
        frame.pack(fill="x", pady=(6, 0))
        self.detail = tk.Text(
            frame, height=9, wrap="word", relief="flat", borderwidth=0,
            font=(self.font, small), cursor="arrow", **text_kw,
        )
        self.detail.tag_configure(
            "title", font=(self.font, t.size(".detail-title", 10), "bold"), spacing3=3,
            foreground=t.prop(".detail-title", "color", t.color("text")),
        )
        self.detail.pack(fill="x")
        self.detail.configure(state="disabled")

        frame = ttk.LabelFrame(right, text="Difesa e confronto con la consigliata", padding=4)
        frame.pack(fill="x", pady=(6, 0))
        self.compare = tk.Text(
            frame, height=9, wrap="word", relief="flat", borderwidth=0,
            font=(self.font, small), cursor="arrow", tabs=(118, 214), **text_kw,
        )
        self._configure_rich_tags(self.compare, spacing_hint=4)
        self.compare.pack(fill="x")
        self.compare.configure(state="disabled")

        self.notebook = notebook = ttk.Notebook(right)
        notebook.pack(fill="both", expand=True, pady=(6, 0))

        # -- scheda Panchina (con i cambi)
        bench_tab = ttk.Frame(notebook, style="Page.TFrame", padding=4)
        notebook.add(bench_tab, text="Panchina")
        buttons = ttk.Frame(bench_tab, style="Page.TFrame")
        buttons.pack(side="bottom", fill="x", pady=(4, 0))
        self.swap_button = ttk.Button(
            buttons, text="Scambia", style="Swap.TButton", command=self._swap, state="disabled"
        )
        self.swap_button.pack(side="left", fill="x", expand=True)
        self.reset_button = ttk.Button(
            buttons, text="Ripristina consigliata", style="Ghost.TButton", command=self._reset_edits,
            state="disabled",
        )
        self.reset_button.pack(side="left", padx=(6, 0))
        tree_frame = ttk.Frame(bench_tab, style="Page.TFrame")
        tree_frame.pack(side="top", fill="both", expand=True)
        self.bench_tree = ttk.Treeview(
            tree_frame, columns=("ruolo", "avv", "punti", "dpunti", "dtit"), height=6, selectmode="browse"
        )
        for column, text, width, anchor in (
            ("#0", "Giocatore", 94, "w"), ("ruolo", "R", 24, "center"), ("avv", "Avv.", 66, "center"),
            ("punti", "Punti", 44, "e"), ("dpunti", "Δ punti", 62, "e"), ("dtit", "Δ tit.", 58, "e"),
        ):
            self.bench_tree.heading(column, text=text)
            self.bench_tree.column(column, width=width, anchor=anchor)
        self.bench_tree.tag_configure(
            "up", background=t.prop(".row-up", "background", t.color("good-bg")),
            foreground=t.prop(".row-up", "color", t.color("good")),
        )
        self.bench_tree.tag_configure(
            "down", background=t.prop(".row-down", "background", t.color("bad-bg")),
            foreground=t.prop(".row-down", "color", t.color("bad")),
        )
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.bench_tree.yview)
        self.bench_tree.configure(yscrollcommand=scroll.set)
        self.bench_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.bench_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_bench_select())
        self.bench_tree.bind("<Double-1>", lambda _e: self._swap())

        # -- scheda Calendario
        calendar_tab = ttk.Frame(notebook, style="Page.TFrame", padding=4)
        notebook.add(calendar_tab, text="Serie A")
        note = ttk.Label(
            calendar_tab, style="Note.TLabel", wraplength=370, justify="left",
            text="Stima dai gol della stagione, solo indicativa. Righe gialle: partite dei tuoi titolari.",
        )
        note.pack(side="bottom", fill="x", pady=(4, 0))
        cal_frame = ttk.Frame(calendar_tab, style="Page.TFrame")
        cal_frame.pack(side="top", fill="both", expand=True)
        self.cal_tree = ttk.Treeview(
            cal_frame, columns=("p1", "px", "p2", "fav", "ris", "tuoi"), height=6, selectmode="browse"
        )
        for column, text, width, anchor in (
            ("#0", "Partita", 92, "w"), ("p1", "1", 32, "e"), ("px", "X", 32, "e"), ("p2", "2", 32, "e"),
            ("fav", "Favorita", 84, "w"), ("ris", "Ris.", 46, "center"), ("tuoi", "Tuoi", 34, "center"),
        ):
            self.cal_tree.heading(column, text=text)
            self.cal_tree.column(column, width=width, anchor=anchor)
        self.cal_tree.tag_configure(
            "mine", background=t.prop(".row-mine", "background", t.color("warn-bg")),
            foreground=t.prop(".row-mine", "color", t.color("warn")),
        )
        cal_scroll = ttk.Scrollbar(cal_frame, orient="vertical", command=self.cal_tree.yview)
        self.cal_tree.configure(yscrollcommand=cal_scroll.set)
        self.cal_tree.pack(side="left", fill="both", expand=True)
        cal_scroll.pack(side="right", fill="y")
        self.cal_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_calendar_select())

        # -- scheda Sfida (avversario della lega)
        self.duel_tab = duel_tab = ttk.Frame(notebook, style="Page.TFrame", padding=4)
        notebook.add(duel_tab, text="Sfida")
        row = ttk.Frame(duel_tab, style="Page.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Avversario", style="Field.TLabel").pack(side="left")
        self.opp_box = ttk.Combobox(row, state="readonly", width=30)
        self.opp_box.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.opp_box.bind("<<ComboboxSelected>>", lambda _e: self._on_opponent_selected())
        self.duel_text = tk.Text(
            duel_tab, height=9, wrap="word", relief="flat", borderwidth=0,
            font=(self.font, small), cursor="arrow", tabs=(92,), **text_kw,
        )
        self._configure_rich_tags(self.duel_text)
        self.duel_text.pack(fill="both", expand=True, pady=(4, 0))
        self.duel_text.configure(state="disabled")

        # -- scheda Calendario della lega
        league_tab = ttk.Frame(notebook, style="Page.TFrame", padding=4)
        notebook.add(league_tab, text="Calendario lega")
        self.league_cal_note = ttk.Label(
            league_tab, style="Note.TLabel", wraplength=370, justify="left",
            text="Doppio clic su una giornata per calcolare la formazione e la sfida di quel turno.",
        )
        self.league_cal_note.pack(side="bottom", fill="x", pady=(4, 0))
        sched_frame = ttk.Frame(league_tab, style="Page.TFrame")
        sched_frame.pack(side="top", fill="both", expand=True)
        self.sched_tree = ttk.Treeview(sched_frame, columns=("campo", "avv", "ris"), height=6, selectmode="browse")
        for column, text, width, anchor in (
            ("#0", "Lega (Serie A)", 96, "w"), ("campo", "Campo", 52, "center"),
            ("avv", "Avversario", 140, "w"), ("ris", "Esito", 56, "center"),
        ):
            self.sched_tree.heading(column, text=text)
            self.sched_tree.column(column, width=width, anchor=anchor)
        self.sched_tree.tag_configure(
            "now", background=t.prop(".row-mine", "background", t.color("warn-bg")),
            foreground=t.prop(".row-mine", "color", t.color("warn")),
        )
        sched_scroll = ttk.Scrollbar(sched_frame, orient="vertical", command=self.sched_tree.yview)
        self.sched_tree.configure(yscrollcommand=sched_scroll.set)
        self.sched_tree.pack(side="left", fill="both", expand=True)
        sched_scroll.pack(side="right", fill="y")
        self.sched_tree.bind("<Double-1>", lambda _e: self._on_schedule_open())

        self.warning_label = ttk.Label(right, style="Warning.TLabel", wraplength=380, justify="left")
        self.warning_label.pack(fill="x", pady=(4, 0))

    # -- tema ------------------------------------------------------------

    def _configure_rich_tags(self, widget: tk.Text, spacing_hint: int = 0) -> None:
        """Tag dei pannelli di testo (head/good/bad/flat/hint) dalle regole ``.compare-*``."""
        t, font = self.theme, self.font
        size = t.size(".compare-head", 9)
        widget.tag_configure("head", font=(font, size, "bold"), spacing1=4,
                             foreground=t.prop(".compare-head", "color", t.color("text")))
        widget.tag_configure("good", font=(font, size, "bold"),
                             foreground=t.prop(".compare-good", "color", t.color("good")))
        widget.tag_configure("bad", font=(font, size, "bold"),
                             foreground=t.prop(".compare-bad", "color", t.color("bad")))
        widget.tag_configure("flat", foreground=t.prop(".compare-flat", "color", t.color("text-muted")))
        widget.tag_configure("hint", foreground=t.prop(".compare-hint", "color", t.color("text-muted")),
                             spacing1=spacing_hint)

    def _setup_modern_style(self) -> None:
        """Configura ttk.Style leggendo i valori dal tema (fantalineup.css)."""
        t, font = self.theme, self.font
        root = self.root
        c = t.color

        def sz(selector: str, default: int) -> int:
            return t.size(selector, default)

        def bg_of(selector: str, token: str) -> str:
            return t.prop(selector, "background", c(token))

        def fg_of(selector: str, token: str) -> str:
            return t.prop(selector, "color", c(token))

        app_bg = bg_of(".app", "bg")
        surface = bg_of(".card", "surface")
        root.configure(background=app_bg)
        # elenco a discesa dei combobox (è un widget tk, non ttk)
        root.option_add("*TCombobox*Listbox.background", bg_of(".combobox-list", "surface"))
        root.option_add("*TCombobox*Listbox.foreground", fg_of(".combobox-list", "text"))
        root.option_add("*TCombobox*Listbox.selectBackground", c("accent-soft"))
        root.option_add("*TCombobox*Listbox.selectForeground", c("accent"))
        root.option_add("*TCombobox*Listbox.font", (font, sz(".app", 10)))

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=app_bg, foreground=fg_of(".app", "text"), font=(font, sz(".app", 10)),
                        bordercolor=c("border"), lightcolor=c("border"), darkcolor=c("border"),
                        troughcolor=c("bg-elevated"), focuscolor=c("accent"))
        style.map(".", foreground=[("disabled", c("text-faint"))])

        # contenitori
        style.configure("TFrame", background=app_bg)
        style.configure("Toolbar.TFrame", background=bg_of(".toolbar", "surface"))
        style.configure("Page.TFrame", background=bg_of(".tab-page", "surface"))
        style.configure(
            "TLabelframe", background=surface, bordercolor=t.prop(".card", "border-color", c("border")),
            lightcolor=surface, darkcolor=surface, relief="solid", borderwidth=px(t.prop(".card", "border-width"), 1),
        )
        style.configure(
            "TLabelframe.Label", background=surface, foreground=fg_of(".section-label", "text-muted"),
            font=(font, sz(".section-label", 8) + 1, t.weight(".section-label")),
        )

        # etichette
        style.configure("TLabel", background=app_bg, foreground=fg_of(".app", "text"))
        style.configure("Field.TLabel", background=bg_of(".field-label", "surface"),
                        foreground=fg_of(".field-label", "text-muted"),
                        font=(font, sz(".field-label", 8) + 1, t.weight(".field-label")))
        style.configure("Summary.TLabel", background=bg_of(".summary", "bg"), foreground=fg_of(".summary", "text"),
                        font=(font, sz(".summary", 12), t.weight(".summary")))
        style.configure("Banner.TLabel", background=app_bg, foreground=fg_of(".subtitle", "text-muted"),
                        font=(font, sz(".app", 10)))
        style.configure("Status.TLabel", background=bg_of(".status", "bg"), foreground=fg_of(".status", "text-muted"),
                        font=(font, sz(".status", 9)), padding=box(t.prop(".status", "padding"), (10, 4, 10, 4)))
        style.configure("Warning.TLabel", background=bg_of(".warning", "warn-bg"),
                        foreground=fg_of(".warning", "warn"), font=(font, sz(".warning", 9)))
        style.configure("Note.TLabel", background=bg_of(".tab-page", "surface"),
                        foreground=c("text-muted"), font=(font, sz(".text-box", 9)))

        # pulsanti: .button, .button-primary, .button-swap, .button-ghost (+ :hover / :disabled)
        for style_name, selector in (
            ("TButton", ".button"), ("Accent.TButton", ".button-primary"),
            ("Swap.TButton", ".button-swap"), ("Ghost.TButton", ".button-ghost"),
        ):
            style.configure(
                style_name,
                background=t.prop(selector, "background", c("surface-2")),
                foreground=t.prop(selector, "color", c("text")),
                font=(font, sz(selector, 9), t.weight(selector)),
                padding=box(t.prop(selector, "padding"), (12, 8, 12, 8)),
                borderwidth=0, relief="flat", focuscolor=t.prop(selector, "background", c("surface-2")),
            )
            normal_bg = t.prop(selector, "background", c("surface-2"))
            style.map(
                style_name,
                background=[
                    ("disabled", t.prop(f"{selector}:disabled", "background", c("bg-elevated"))),
                    ("active", t.prop(f"{selector}:hover", "background", normal_bg)),
                ],
                foreground=[
                    ("disabled", t.prop(f"{selector}:disabled", "color", c("text-faint"))),
                    ("active", t.prop(f"{selector}:hover", "color", t.prop(selector, "color", c("text")))),
                ],
            )

        # campi
        for style_name, selector in (("TCombobox", ".combobox"), ("TSpinbox", ".spinbox")):
            field_bg = bg_of(selector, "bg-elevated")
            style.configure(
                style_name, fieldbackground=field_bg, background=c("surface-2"), foreground=fg_of(selector, "text"),
                bordercolor=t.prop(selector, "border-color", c("border-strong")),
                lightcolor=field_bg, darkcolor=field_bg, arrowcolor=c("text-muted"),
                insertcolor=c("text"), padding=box(t.prop(selector, "padding"), (8, 6, 8, 6)),
            )
            style.map(
                style_name,
                fieldbackground=[("disabled", t.prop(f"{selector}:disabled", "background", c("bg"))),
                                 ("readonly", field_bg)],
                foreground=[("disabled", t.prop(f"{selector}:disabled", "color", c("text-faint")))],
                bordercolor=[("focus", t.prop(f"{selector}:focus", "border-color", c("accent")))],
                selectbackground=[("readonly", field_bg)], selectforeground=[("readonly", fg_of(selector, "text"))],
            )

        # tabelle
        row_height = min(px(t.prop(".tree", "height"), MAX_ROW_HEIGHT), MAX_ROW_HEIGHT)
        tree_bg = bg_of(".tree", "surface")
        style.configure(
            "Treeview", background=tree_bg, fieldbackground=tree_bg, foreground=fg_of(".tree", "text"),
            bordercolor=t.prop(".tree", "border-color", tree_bg), lightcolor=tree_bg, darkcolor=tree_bg,
            font=(font, sz(".tree", 9)), rowheight=row_height, borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", t.prop(".tree-row:selected", "background", c("accent-soft")))],
            foreground=[("selected", t.prop(".tree-row:selected", "color", c("accent")))],
        )
        style.configure(
            "Treeview.Heading", background=bg_of(".tree-heading", "surface"),
            foreground=fg_of(".tree-heading", "text-muted"), relief="flat", borderwidth=0,
            font=(font, sz(".tree-heading", 8) + 1, t.weight(".tree-heading")),
            padding=box(t.prop(".tree-heading", "padding"), (7, 8, 7, 8)),
        )
        style.map("Treeview.Heading", background=[("active", t.prop(".tree-heading:hover", "background", c("surface-2")))])

        # barre di scorrimento
        style.configure(
            "Vertical.TScrollbar", background=bg_of(".scrollbar", "surface-2"),
            troughcolor=t.prop(".scrollbar", "border-color", c("surface")), bordercolor=c("surface"),
            arrowcolor=fg_of(".scrollbar", "text-muted"), lightcolor=c("surface-2"), darkcolor=c("surface-2"),
            relief="flat", borderwidth=0,
        )
        style.map("Vertical.TScrollbar", background=[("active", c("surface-hover"))])

        # schede laterali (.tab, .tab.is-active, .tab-page)
        style.configure("TNotebook", background=bg_of(".tabs", "bg"), bordercolor=c("border"),
                        lightcolor=bg_of(".tabs", "bg"), darkcolor=bg_of(".tabs", "bg"), tabmargins=(0, 0, 0, 0))
        tab_bg = bg_of(".tab", "bg")
        style.configure(
            "TNotebook.Tab", background=tab_bg, foreground=fg_of(".tab", "text-muted"),
            font=(font, sz(".tab", 9), t.weight(".tab")), padding=box(t.prop(".tab", "padding"), (8, 10, 8, 10)),
            borderwidth=0, lightcolor=tab_bg, darkcolor=tab_bg, bordercolor=tab_bg,
        )
        active_line = bg_of(".tab-line.is-active", "accent")
        style.map(
            "TNotebook.Tab",
            background=[("selected", bg_of(".tab-page", "surface"))],
            foreground=[("selected", t.prop(".tab.is-active", "color", c("accent"))),
                        ("active", t.prop(".tab:hover", "color", c("text")))],
            lightcolor=[("selected", active_line)],  # filo colorato sopra la scheda attiva
            bordercolor=[("selected", active_line)],
        )

    # -- esecuzione in background --------------------------------------

    def _run_async(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None],
        status: str,
        on_error: Callable[[Any], None] | None = None,
    ) -> None:
        self._set_busy(True, status)

        def runner() -> None:
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - l'errore viene mostrato nella finestra
                self._queue.put((on_error or self._on_error, exc))
            else:
                self._queue.put((done, result))

        threading.Thread(target=runner, daemon=True).start()

    def _poll(self) -> None:
        while True:
            try:
                callback, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            self._set_busy(False, "")
            try:
                callback(payload)
            except Exception as exc:  # noqa: BLE001
                self._on_error(exc)
        self.root.after(60, self._poll)

    def _set_busy(self, busy: bool, status: str) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.calc_button, self.browse_button, self.matchday_box):
            widget.configure(state=state)
        for box in (self.league_box, self.team_box):
            box.configure(state="disabled" if busy else "readonly")
        self.root.configure(cursor="watch" if busy else "")
        if busy:
            self.status.configure(text=status)

    def _on_error(self, exc: Any) -> None:
        if not isinstance(exc, (DataError, LineupError, ValueError, OSError, KeyError)):
            traceback.print_exception(type(exc), exc, exc.__traceback__)  # errore inatteso: resta nel terminale
        message = str(exc) or exc.__class__.__name__
        self.status.configure(text="Errore")
        messagebox.showerror("FantaLineup", message, parent=self.root)

    # -- caricamento dati --------------------------------------------------

    def _load_base_data(self) -> tuple[dict[str, Any], Any, LeagueSettings]:
        ensure_data(self.data_dir, auto_update=True)
        players, _ = load_players(data_dir=self.data_dir)
        return players, load_serie_a(data_dir=self.data_dir), load_settings(data_dir=self.data_dir)

    def _on_base_loaded(self, base: tuple[dict[str, Any], Any, LeagueSettings]) -> None:
        self.players, self.serie_a, self.base_settings = base
        self._leagues = [(DEMO_LABEL, "demo")]
        from .cli import _discover_leagues  # import locale: evita dipendenze circolari

        for name, path in _discover_leagues(self.data_dir):
            self._leagues.append((f"{name} ({Path(path).name})", path))

        wanted = getattr(self.args, "lega", None) or self._state.get("lega") or "demo"
        if wanted not in [address for _, address in self._leagues]:
            if wanted != "demo" and (wanted.startswith(("http://", "https://")) or Path(wanted).exists()):
                self._leagues.append((Path(wanted).name if "://" not in wanted else wanted, wanted))
            else:
                wanted = "demo"
        self._refresh_league_box(wanted)
        self._load_league(wanted)

    def _refresh_league_box(self, selected: str | None = None) -> None:
        self.league_box.configure(values=[label for label, _ in self._leagues])
        for index, (_, address) in enumerate(self._leagues):
            if address == selected:
                self.league_box.current(index)
                break

    def _on_league_selected(self) -> None:
        index = self.league_box.current()
        if 0 <= index < len(self._leagues):
            self._load_league(self._leagues[index][1])

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Scegli il file della lega",
            initialdir=str(self.data_dir),
            filetypes=[("Leghe (Excel o JSON)", "*.xlsx *.xlsm *.json"), ("Tutti i file", "*.*")],
        )
        if not path:
            return
        if path not in [address for _, address in self._leagues]:
            self._leagues.append((Path(path).name, path))
        self._refresh_league_box(path)
        self._load_league(path)

    def _load_league(self, address: str) -> None:
        def work() -> League:
            return load_league(
                address,
                data_dir=self.data_dir,
                players=self.players,
                serie_a=self.serie_a,
                settings=self.base_settings,
            )

        self._run_async(
            work,
            lambda league: self._on_league_loaded(league, address),
            "Carico la lega...",
            on_error=self._on_league_error,
        )

    def _on_league_error(self, exc: Any) -> None:
        self._on_error(exc)
        # ripristina la scelta precedente nel menu
        self._refresh_league_box(self.current_address)
        if self.current_address is None:
            self.league_box.set("")

    def _on_league_loaded(self, league: League, address: str) -> None:
        self.league = league
        self.current_address = address
        names = league.team_names()
        self.team_box.configure(values=names)

        chosen = names[0]
        for candidate in (getattr(self.args, "squadra", None), self._state.get("squadra")):
            if candidate:
                try:
                    chosen = league.team(candidate).name
                    break
                except KeyError:
                    continue
        self.args = None  # --lega e --squadra valgono solo per il primo avvio
        self.team_box.set(chosen)
        self.opp_cache.clear()
        self.opp_errors.clear()
        self.opponent_name = None
        raw_calendar, source = load_league_calendar(address, self.data_dir)
        self.calendar = self._resolve_calendar(league, raw_calendar)
        self.goal_thresholds = infer_goal_thresholds(self.calendar)
        if raw_calendar and not self.calendar:
            self.calendar_note = f"Calendario trovato ({source}) ma i nomi delle squadre non coincidono con la lega."
        elif source:
            self.calendar_note = f"Calendario: {source}"
        else:
            self.calendar_note = ""
        self.matchday_var.set(str(league.matchday or league.serie_a.current_matchday))
        self._calculate()

    @staticmethod
    def _resolve_calendar(league: League, raw: list[LeagueMatch]) -> list[LeagueMatch]:
        """Tiene solo le partite fra squadre che esistono nella lega (con i loro nomi)."""
        result: list[LeagueMatch] = []
        for match in raw:
            try:
                result.append(dataclasses.replace(
                    match, home=league.team(match.home).name, away=league.team(match.away).name
                ))
            except KeyError:
                continue
        return result

    def _on_team_selected(self) -> None:
        self.opponent_name = None
        self._calculate()

    # -- calcolo -----------------------------------------------------------

    def _calculate(self) -> None:
        if self.league is None or not self.team_box.get():
            return
        league = self.league
        try:
            team = league.team(self.team_box.get())
        except KeyError as exc:
            self._on_error(exc)
            return
        try:
            matchday = int(self.matchday_var.get())
        except ValueError:
            matchday = league.matchday or league.serie_a.current_matchday
            self.matchday_var.set(str(matchday))
        self._run_async(
            lambda: compute_recommendation(league, team, matchday),
            self._on_calculated,
            f"Calcolo la formazione migliore per {team.name}...",
        )

    def _on_calculated(self, rec: Recommendation) -> None:
        self.rec = rec
        self._index = None
        self.edited = {}
        self.swap_out_id = None
        for item in self.form_tree.get_children():
            self.form_tree.delete(item)
        for index, lineup in enumerate(rec.lineups):
            self.form_tree.insert("", "end", iid=str(index), text="", values=("", "", ""))
            self._update_form_row(index, lineup, edited=False)
        self.form_tree.selection_set("0")
        self.selected_id = None
        self._index = 0
        self._pick_opponent()
        self._render()
        self._ensure_opponent()

        self.warning_label.configure(text="\n".join(rec.warnings))
        league = self.league
        updated = f" · dati aggiornati {league.serie_a.updated}" if league and league.serie_a.updated else ""
        self.status.configure(
            text=f"{league.name if league else ''} · {rec.team_name} · giornata {rec.matchday} · "
            f"{rec.evaluated:,} combinazioni valutate".replace(",", ".") + updated
        )
        self._save_state()

    # -- visualizzazione ---------------------------------------------------

    def _current(self) -> Lineup:
        assert self.rec is not None and self._index is not None
        return self.edited.get(self._index, self.rec.lineups[self._index])

    def _update_form_row(self, index: int, lineup: Lineup, edited: bool) -> None:
        mark = "★ " if index == 0 else "    "
        self.form_tree.item(
            str(index),
            text=mark + lineup.formation + (" ✎" if edited else ""),
            values=(f"{lineup.expected_total:.2f}", _signed(lineup.defense_modifier),
                    f"{lineup.avg_starter_prob:.0%}"),
        )

    def _on_form_select(self) -> None:
        selection = self.form_tree.selection()
        if selection and self.rec is not None:
            index = int(selection[0])
            if index != self._index:
                self._index = index
                self._render()

    def _render(self) -> None:
        """Ridisegna tutto per il modulo scelto (con gli eventuali cambi)."""
        rec = self.rec
        if rec is None or self._index is None:
            return
        index = self._index
        base = rec.lineups[index]
        lineup = self._current()
        edited = index in self.edited
        settings = rec.settings

        label = " (modificato)" if edited else (" (consigliato)" if index == 0 else "")
        self.summary.configure(
            text=f"Modulo {lineup.formation}{label}  ·  {lineup.expected_total:.2f} punti attesi"
            f"  ·  modificatore {_signed(lineup.defense_modifier)}"
            f"  ·  titolarità {lineup.avg_starter_prob:.0%}"
        )
        self._update_form_row(index, lineup, edited)
        duel = self._duel_probabilities(base, lineup)
        self._write_compare(base, lineup, edited, duel)
        self._write_duel(lineup, duel)
        self._fill_bench(lineup)
        self._fill_calendar(lineup)

        starter_ids = {p.player.id for p in lineup.starters}
        valid = starter_ids | {p.player.id for p in lineup.bench}
        if self.selected_id not in valid:
            self.selected_id = lineup.captain.player.id
        if self.swap_out_id not in starter_ids:
            self.swap_out_id = None
        self.reset_button.configure(state="normal" if edited else "disabled")
        self._after_selection()

    def _write_compare(
        self,
        base: Lineup,
        lineup: Lineup,
        edited: bool,
        duel: Any = None,
    ) -> None:
        rec = self.rec
        assert rec is not None
        settings = rec.settings
        modifier, average, best_defenders = defense_modifier(lineup.goalkeeper, lineup.defenders, settings)
        text = f"Modificatore {_signed(modifier)} (media {average:.2f}: portiere {lineup.goalkeeper.expected_raw_vote:.2f}"
        if best_defenders:
            text += "; difensori " + ", ".join(f"{vote:.2f}" for vote in best_defenders)
        text += ")"
        if len(lineup.defenders) < settings.min_difensori_modificatore:
            text += f"\nNon applicato: servono almeno {settings.min_difensori_modificatore} difensori a voto."

        box = self.compare
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", text + "\n")
        if edited:
            box.insert("end", "Confronto con la formazione consigliata\n", "head")
            for name, before, after, symbol, delta, tag in comparison_rows(base, lineup):
                box.insert("end", f"{name}\t{before} → {after}\t")
                box.insert("end", f"{symbol} {delta}\n", tag)
            if duel is not None and self.opponent_name:
                change = (duel[1][0] - duel[0][0]) * 100
                symbol, tag = arrow(change, tolerance=0.5)
                box.insert("end", f"Vittoria vs {_short(self.opponent_name, 14)}\t{duel[0][0]:.0%} → {duel[1][0]:.0%}\t")
                box.insert("end", f"{symbol} {change:+.0f}%\n", tag)
            limit = settings.max_per_squadra_serie_a
            if limit > 0:
                counts: dict[str, int] = {}
                for p in lineup.starters:
                    counts[p.player.team] = counts.get(p.player.team, 0) + 1
                if any(n > limit for n in counts.values()):
                    box.insert("end", f"Attenzione: più di {limit} giocatori della stessa squadra.\n", "bad")
        else:
            box.insert(
                "end",
                "Per provare un cambio: clicca un titolare sul campo, poi un giocatore della panchina "
                "(stesso ruolo) e premi «Scambia». Le frecce mostrano come cambiano punti, media difesa e titolarità.",
                "hint",
            )
        box.configure(state="disabled")

    def _fill_bench(self, lineup: Lineup) -> None:
        for item in self.bench_tree.get_children():
            self.bench_tree.delete(item)
        bench = sorted(lineup.bench, key=lambda p: (ROLE_ORDER.get(p.player.role, 9), -p.expected_points))
        for projection in bench:
            matchup = projection.matchup
            opponent = (matchup.opponent_code or "N/D") + (
                f" ({'C' if matchup.home else 'T'})" if matchup.opponent_code else ""
            )
            self.bench_tree.insert(
                "", "end", iid=projection.player.id, text=projection.player.name,
                values=(projection.player.role, opponent, f"{projection.expected_points:.2f}", "", ""),
            )

    def _fill_calendar(self, lineup: Lineup) -> None:
        rec = self.rec
        assert rec is not None
        counts: dict[str, int] = {}
        for projection in lineup.starters:
            counts[projection.player.team] = counts.get(projection.player.team, 0) + 1
        for item in self.cal_tree.get_children():
            self.cal_tree.delete(item)
        for index, outlook in enumerate(rec.outlooks):
            favorite = outlook.favorite()
            if favorite is None:
                favorite_text = "Equilibrata"
            else:
                favorite_text = f"{favorite} {max(outlook.p_home, outlook.p_away):.0%}"
            home_goals, away_goals = outlook.real_score or outlook.score
            score = f"{home_goals}-{away_goals}" + (" ✓" if outlook.real_score else "")
            mine = counts.get(outlook.home, 0) + counts.get(outlook.away, 0)
            self.cal_tree.insert(
                "", "end", iid=str(index), text=f"{outlook.home} - {outlook.away}",
                values=(f"{outlook.p_home:.0%}", f"{outlook.p_draw:.0%}", f"{outlook.p_away:.0%}",
                        favorite_text, score, str(mine) if mine else ""),
                tags=("mine",) if mine else (),
            )

    def _on_calendar_select(self) -> None:
        rec = self.rec
        selection = self.cal_tree.selection()
        if rec is None or self._index is None or not selection:
            return
        outlook = rec.outlooks[int(selection[0])]
        involved = [
            p for p in self._current().starters if p.player.team in (outlook.home, outlook.away)
        ]
        if involved:
            best = max(involved, key=lambda p: p.expected_points)
            self._select_player(best.player.id)

    # -- sfida contro l'avversario della lega ------------------------------

    def _my_match(self, matchday: int) -> LeagueMatch | None:
        me = self.team_box.get()
        for match in self.calendar:
            if match.day == matchday and me in (match.home, match.away):
                return match
        return None

    def _pick_opponent(self) -> None:
        """Sceglie l'avversario: quello del calendario, altrimenti quello scelto a mano."""
        rec, league = self.rec, self.league
        if rec is None or league is None:
            return
        me = self.team_box.get()
        match = self._my_match(rec.matchday)
        if match:
            self.opponent_name = match.away if me == match.home else match.home
            self.opponent_auto = True
        else:
            self.opponent_auto = False
            if self.opponent_name == me or self.opponent_name not in league.team_names():
                self.opponent_name = None
        others = [name for name in league.team_names() if name != me]
        self.opp_box.configure(values=["(nessuno)", *others])
        self.opp_box.set(self.opponent_name or "(nessuno)")
        self._fill_schedule(me, rec.matchday)

    def _fill_schedule(self, me: str, matchday: int) -> None:
        for item in self.sched_tree.get_children():
            self.sched_tree.delete(item)
        for match in sorted((m for m in self.calendar if me in (m.home, m.away)), key=lambda m: m.league_day):
            at_home = me == match.home
            opponent = match.away if at_home else match.home
            result = ""
            if match.score:
                mine, theirs = match.score if at_home else match.score[::-1]
                result = f"{mine}-{theirs}" + (" V" if mine > theirs else " S" if mine < theirs else " P")
            label = f"{match.league_day}" + (f" (SA {match.serie_a_day})" if match.serie_a_day else "")
            self.sched_tree.insert(
                "", "end", iid=str(match.day), text=label,
                values=("Casa" if at_home else "Trasf.", opponent, result),
                tags=("now",) if match.day == matchday else (),
            )
        current = str(matchday)
        if self.sched_tree.exists(current):
            self.sched_tree.see(current)

    def _on_schedule_open(self) -> None:
        selection = self.sched_tree.selection()
        if selection:
            self.matchday_var.set(selection[0])
            self.notebook.select(self.duel_tab)
            self._calculate()

    def _on_opponent_selected(self) -> None:
        value = self.opp_box.get()
        self.opponent_name = None if value in ("", "(nessuno)") else value
        self.opponent_auto = False
        self._ensure_opponent()
        self._render()

    def _opponent_key(self) -> tuple[str, str, int] | None:
        if self.rec is None or not self.opponent_name:
            return None
        return (self.current_address or "", self.opponent_name, self.rec.matchday)

    def _ensure_opponent(self) -> None:
        """Calcola (una volta sola) la miglior formazione dell'avversario."""
        key, league = self._opponent_key(), self.league
        if key is None or league is None or key in self.opp_cache or key in self.opp_errors:
            return
        matchday = key[2]
        try:
            team = league.team(key[1])
        except KeyError:
            return

        def done(result: Recommendation) -> None:
            self.opp_cache[key] = result
            self._render()

        def failed(exc: Any) -> None:
            self.opp_errors[key] = str(exc) or exc.__class__.__name__
            self._render()

        self._run_async(
            lambda: compute_recommendation(league, team, matchday), done,
            f"Calcolo la formazione di {team.name}...", on_error=failed,
        )

    def _opponent_lineup(self) -> Lineup | None:
        key = self._opponent_key()
        result = self.opp_cache.get(key) if key else None
        return result.lineups[0] if result else None

    def _duel_probabilities(
        self, base: Lineup, lineup: Lineup
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        """((V, P, S) con la consigliata, (V, P, S) con la formazione attuale)."""
        opponent, rec = self._opponent_lineup(), self.rec
        if opponent is None or rec is None:
            return None
        thresholds = self.goal_thresholds
        return (
            match_outcome(base, opponent, rec.settings, thresholds),
            match_outcome(lineup, opponent, rec.settings, thresholds),
        )

    def _write_duel(self, lineup: Lineup, duel: Any) -> None:
        rec = self.rec
        assert rec is not None
        box = self.duel_text
        box.configure(state="normal")
        box.delete("1.0", "end")
        opponent_lineup = self._opponent_lineup()
        key = self._opponent_key()
        banner, color = "", self.theme.color("text-muted")

        if not self.opponent_name:
            hint = "Nessun avversario selezionato."
            if not self.calendar:
                hint += " " + (self.calendar_note or "Non ho trovato il calendario della lega") + \
                    ": scegli l'avversario dal menu qui sopra."
            elif self._my_match(rec.matchday) is None:
                hint += f" Il calendario non ha partite per la giornata {rec.matchday}: scegli l'avversario a mano."
            box.insert("end", hint, "hint")
            banner = "Sfida: scegli l'avversario nella scheda «Sfida» per vedere la probabilità di vittoria."
        elif key in self.opp_errors:
            box.insert("end", f"Non riesco a schierare {self.opponent_name}: {self.opp_errors[key]}", "bad")
            banner = f"Sfida contro {self.opponent_name}: formazione avversaria non disponibile."
        elif opponent_lineup is None or duel is None:
            box.insert("end", f"Calcolo la formazione di {self.opponent_name}...", "hint")
            banner = f"Sfida contro {self.opponent_name}: calcolo in corso..."
        else:
            win, draw, loss = duel[1]
            settings = rec.settings
            mine_sd = lineup_spread(lineup, settings)
            their_sd = lineup_spread(opponent_lineup, settings)
            difference = lineup.expected_total - opponent_lineup.expected_total
            symbol, tag = arrow(difference, tolerance=0.05)
            if win - loss >= 0.15:
                verdict, tag_v, color = "Sei favorito", "good", self.theme.color("good")
            elif loss - win >= 0.15:
                verdict, tag_v, color = "Sei sfavorito", "bad", self.theme.color("bad")
            else:
                verdict, tag_v, color = "Sfida equilibrata", "flat", self.theme.color("text-muted")
            match = self._my_match(rec.matchday)
            origin = f"lega {match.league_day}, dal calendario" if match and self.opponent_auto else "avversario scelto da te"
            box.insert("end", f"{rec.team_name} vs {self.opponent_name}\n", "head")
            box.insert("end", f"Serie A {rec.matchday} · {origin}\n", "hint")
            box.insert("end", "Punti attesi\t")
            box.insert("end", f"{lineup.expected_total:.1f} (± {mine_sd:.1f}) vs {opponent_lineup.expected_total:.1f} "
                              f"(± {their_sd:.1f})  ")
            box.insert("end", f"{symbol} {difference:+.1f}\n", tag)
            box.insert("end", "Moduli\t" f"{lineup.formation} vs {opponent_lineup.formation} (al meglio)\n")
            box.insert("end", "Vittoria\t")
            box.insert("end", f"{win:.0%}", tag_v)
            if self.goal_thresholds:
                box.insert("end", f" · pari {draw:.0%} · sconfitta {loss:.0%}\n")
                goals_mine = expected_goals_of(lineup, settings, self.goal_thresholds)
                goals_theirs = expected_goals_of(opponent_lineup, settings, self.goal_thresholds)
                box.insert("end", f"Gol attesi\t{goals_mine:.1f} - {goals_theirs:.1f}\n")
            else:
                box.insert("end", f" · sconfitta {loss:.0%} (sui punti totali)\n")
            box.insert("end", f"{verdict}. ", tag_v)
            box.insert("end", "Stima indicativa.", "hint")
            if match and match.score:
                at_home = self.team_box.get() == match.home
                mine, theirs = match.score if at_home else match.score[::-1]
                box.insert("end", f"\nPartita già giocata: {mine}-{theirs}.", "head")
            banner = (
                f"Sfida contro {self.opponent_name}: {lineup.expected_total:.1f} a "
                f"{opponent_lineup.expected_total:.1f}  ·  vittoria {win:.0%}"
                + (f"  ·  pareggio {draw:.0%}" if self.goal_thresholds else "")
                + f"  ·  sconfitta {loss:.0%}  ·  {verdict.lower()}"
            )
        box.configure(state="disabled")
        self.duel_banner.configure(text=banner, foreground=color)

    # -- selezione e cambi -----------------------------------------------

    def _on_pitch_click(self, player_id: str) -> None:
        """Click su un titolare: è anche il giocatore che potrai sostituire."""
        self.swap_out_id = player_id
        if player_id == self.selected_id:
            self._after_selection()
        else:
            self._select_player(player_id)

    def _select_player(self, player_id: str) -> None:
        rec = self.rec
        if rec is None or player_id == self.selected_id or player_id not in rec.projections:
            return
        self.selected_id = player_id
        self._after_selection()

    def _after_selection(self) -> None:
        rec = self.rec
        if rec is None or self._index is None or self.selected_id is None:
            return
        base = rec.lineups[self._index]
        lineup = self._current()
        edited = self._index in self.edited
        projection = rec.projections[self.selected_id]
        verdicts = {code: outlook.verdict(code) for code, outlook in rec.outlook_by_team.items()}
        marks = swap_marks(base, lineup) if edited else {}
        self.pitch.show(
            lineup, lineup.captain.player.id, self.selected_id, self.swap_out_id, verdicts, marks
        )

        title, lines = describe_projection(
            projection,
            captain=self.selected_id == lineup.captain.player.id,
            outlook=rec.outlook_by_team.get(projection.player.team),
        )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", title + "\n", "title")
        self.detail.insert("end", "\n".join(lines))
        self.detail.configure(state="disabled")

        self._update_bench_deltas()
        if self.bench_tree.exists(self.selected_id):
            if self.bench_tree.selection() != (self.selected_id,):
                self.bench_tree.selection_set(self.selected_id)
            self.bench_tree.see(self.selected_id)
        elif self.bench_tree.selection():
            self.bench_tree.selection_remove(self.bench_tree.selection())
        self._update_swap_button()

    def _update_bench_deltas(self) -> None:
        rec = self.rec
        assert rec is not None
        outgoing = rec.projections.get(self.swap_out_id) if self.swap_out_id else None
        for item in self.bench_tree.get_children():
            projection = rec.projections[item]
            values = list(self.bench_tree.item(item, "values"))[:3]
            tags: tuple[str, ...] = ()
            if outgoing is not None and projection.player.role == outgoing.player.role:
                delta = projection.expected_points - outgoing.expected_points
                symbol, _ = arrow(delta)
                tit = (projection.player.starter_prob - outgoing.player.starter_prob) * 100
                tit_symbol, _ = arrow(tit, tolerance=0.5)
                values += [f"{symbol} {delta:+.2f}", f"{tit_symbol} {tit:+.0f}%"]
                tags = ("up",) if delta >= 0.005 else ("down",) if delta <= -0.005 else ()
            else:
                values += ["", ""]
            self.bench_tree.item(item, values=values, tags=tags)

    def _update_swap_button(self) -> None:
        rec = self.rec
        assert rec is not None
        incoming = rec.projections.get(self.selected_id) if self.selected_id else None
        outgoing = rec.projections.get(self.swap_out_id) if self.swap_out_id else None
        ready = (
            incoming is not None
            and outgoing is not None
            and self.bench_tree.exists(incoming.player.id)
            and incoming.player.role == outgoing.player.role
        )
        if ready:
            text = f"Scambia: {_short(outgoing.player.name, 12)} ⇄ {_short(incoming.player.name, 12)}"
        else:
            text = "Scambia"
        self.swap_button.configure(text=text, state="normal" if ready else "disabled")

    def _on_bench_select(self) -> None:
        selection = self.bench_tree.selection()
        if selection:
            self._select_player(selection[0])

    def _swap(self) -> None:
        rec = self.rec
        if rec is None or self._index is None:
            return
        out_id, in_id = self.swap_out_id, self.selected_id
        lineup = self._current()
        if not out_id or not in_id or in_id not in {p.player.id for p in lineup.bench}:
            self.status.configure(text="Clicca un titolare sul campo e poi un giocatore della panchina.")
            return
        try:
            new = swap_players(lineup, out_id, rec.projections[in_id], rec.settings)
        except ValueError as exc:
            self.status.configure(text=str(exc))
            return
        base = rec.lineups[self._index]
        if {p.player.id for p in new.starters} == {p.player.id for p in base.starters}:
            self.edited.pop(self._index, None)  # sei tornato alla formazione consigliata
        else:
            self.edited[self._index] = new
        self.selected_id = in_id
        self.swap_out_id = in_id
        self._render()

    def _reset_edits(self) -> None:
        if self._index is not None and self._index in self.edited:
            del self.edited[self._index]
            self.swap_out_id = None
            self._render()

    # -- ultima scelta -----------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads((self.data_dir / STATE_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        if not self.current_address:
            return
        state = {"lega": self.current_address, "squadra": self.team_box.get()}
        try:
            (self.data_dir / STATE_FILE).write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Avvio
# ---------------------------------------------------------------------------


def launch(args: Any = None) -> int:
    global SHOWN
    data_dir = resolve_data_dir(getattr(args, "data_dir", None))
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise GuiUnavailable(str(exc)) from exc
    FantaApp(root, data_dir, args)
    SHOWN = True
    root.mainloop()
    return 0