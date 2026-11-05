"""Modelli dati usati dal motore e dal caricamento dei file JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .config import DEFAULT_FORMATIONS, DEFAULT_MODIFIER_TABLE, ROLE_ORDER


@dataclass
class SerieATeam:
    code: str
    name: str
    slug: str
    position: int
    points: int
    played: int
    wins: int
    draws: int
    losses: int
    gf: int
    ga: int
    goal_diff: int

    @property
    def ppg(self) -> float:
        return self.points / self.played if self.played else 0.0

    @property
    def gfp(self) -> float:
        return self.gf / self.played if self.played else 0.0

    @property
    def gap(self) -> float:
        return self.ga / self.played if self.played else 0.0


@dataclass
class Fixture:
    matchday: int
    home: str
    away: str
    date: str | None = None
    time: str | None = None
    home_goals: int | None = None
    away_goals: int | None = None

    @property
    def played(self) -> bool:
        return self.home_goals is not None and self.away_goals is not None


@dataclass
class SerieAData:
    season: str
    updated: str
    teams: dict[str, SerieATeam]
    fixtures: list[Fixture]
    current_matchday: int
    source_standings: str = ""
    source_fixtures: str = ""

    def team(self, code: str) -> SerieATeam:
        try:
            return self.teams[code]
        except KeyError as exc:
            raise KeyError(f"Squadra di Serie A sconosciuta: {code}") from exc

    def fixture_for(self, team_code: str, matchday: int) -> Fixture | None:
        for fixture in self.fixtures:
            if fixture.matchday != matchday:
                continue
            if fixture.home == team_code or fixture.away == team_code:
                return fixture
        return None

    def opponent(self, team_code: str, matchday: int) -> tuple[str, bool] | None:
        fixture = self.fixture_for(team_code, matchday)
        if fixture is None:
            return None
        if fixture.home == team_code:
            return fixture.away, True
        return fixture.home, False


@dataclass
class Player:
    id: str
    name: str
    team: str
    role: str
    mantra: list[str] = field(default_factory=list)
    price: float = 0.0
    initial_price: float = 0.0
    fvm: float = 0.0
    appearances: int = 0
    media_voto: float | None = None
    fantamedia: float | None = None
    goals: int = 0
    assists: int = 0
    goals_conceded: int = 0
    penalties_scored: int = 0
    penalties_attempted: int = 0
    penalties_saved: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    starter_prob: float = 0.0
    status: str = "ok"
    note: str = ""

    @property
    def role_order(self) -> int:
        return ROLE_ORDER.get(self.role, 9)

    @property
    def available(self) -> bool:
        return self.starter_prob > 0.0 and self.status not in {"infortunato", "squalificato"}

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.team})"


@dataclass
class Matchup:
    opponent_code: str
    opponent_name: str
    home: bool
    difficulty: float
    label: str
    lambda_for: float
    lambda_against: float
    opponent_position: int

    @property
    def venue(self) -> str:
        return "Casa" if self.home else "Trasferta"

    @property
    def stars(self) -> str:
        # 1..5 stelle, arrotondate per difetto/evidenza
        full = max(1, min(5, int(round(self.difficulty))))
        return "★" * full + "☆" * (5 - full)


@dataclass
class PlayerProjection:
    player: Player
    matchup: Matchup
    base_vote: float
    modifier_vote: float
    expected_bonus: float
    expected_malus: float
    expected_points: float
    goal_prob: float
    assist_prob: float
    expected_goals: float
    expected_assists: float
    risk: float
    notes: list[str] = field(default_factory=list)

    @property
    def starter_prob(self) -> float:
        return self.player.starter_prob

    @property
    def expected_raw_vote(self) -> float:
        """Voto atteso (senza bonus/malus) condizionato al fatto che giochi."""
        return self.base_vote + self.modifier_vote

    @property
    def expected_vote(self) -> float:
        """Fantavoto atteso condizionato al fatto che il giocatore giochi."""
        return self.expected_raw_vote + self.expected_bonus + self.expected_malus


@dataclass
class Lineup:
    formation: str
    goalkeeper: PlayerProjection
    defenders: list[PlayerProjection]
    midfielders: list[PlayerProjection]
    forwards: list[PlayerProjection]
    bench: list[PlayerProjection]
    defense_modifier: float
    defense_average: float
    expected_total: float
    avg_starter_prob: float
    risk: float
    objective: float

    @property
    def starters(self) -> list[PlayerProjection]:
        return [self.goalkeeper, *self.defenders, *self.midfielders, *self.forwards]

    @property
    def captain(self) -> PlayerProjection:
        outfield = [p for p in self.starters if p.player.role != "P"]
        return max(outfield or self.starters, key=lambda p: p.expected_points)


@dataclass
class LeagueSettings:
    min_difensori_modificatore: int = 4
    modificatore_portiere: bool = True
    tabella_modificatore: list[list[float]] = field(
        default_factory=lambda: [list(row) for row in DEFAULT_MODIFIER_TABLE]
    )
    voto_sostituto_modificatore: float = 5.5
    bonus_gol: float = 3.0
    bonus_assist: float = 1.0
    bonus_rigore_parato: float = 3.0
    bonus_porta_inviolata: float = 0.0
    malus_ammonizione: float = -1.0
    malus_espulsione: float = -1.0
    malus_autogol: float = -3.0
    malus_rigore_sbagliato: float = -3.0
    malus_gol_subito: float = -1.0
    gol_subito_solo_portiere: bool = True
    peso_gol_subiti_voto: float = 0.50
    moduli: list[str] = field(default_factory=lambda: list(DEFAULT_FORMATIONS))
    max_per_squadra_serie_a: int = 0
    panchina: int = 12
    rischio: float = 0.15
    partite_shrink_voto: int = 4
    partite_shrink_rate: int = 6
    fattore_casa_attacco: float = 1.08
    fattore_trasferta_attacco: float = 0.92
    giornata: int | None = None

    def clone(self) -> "LeagueSettings":
        return LeagueSettings(**{k: (list(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()})


@dataclass
class FantasyTeam:
    name: str
    manager: str = ""
    roster: list[Player] = field(default_factory=list)

    def players_by_role(self, role: str) -> list[Player]:
        return [p for p in self.roster if p.role == role]


@dataclass
class OptimizationResult:
    best: Lineup
    alternatives: list[Lineup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evaluated: int = 0
    approximate: bool = False


@dataclass
class League:
    name: str
    source: str
    teams: list[FantasyTeam]
    settings: LeagueSettings
    serie_a: SerieAData
    matchday: int
    players: dict[str, Player] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def team(self, name: str) -> FantasyTeam:
        normalized = _normalize(name)
        for team in self.teams:
            if _normalize(team.name) == normalized:
                return team
        # fallback: match parziale unico
        candidates = [t for t in self.teams if normalized in _normalize(t.name)]
        if len(candidates) == 1:
            return candidates[0]
        available = ", ".join(t.name for t in self.teams)
        raise KeyError(f"Squadra {name!r} non trovata. Squadre disponibili: {available}")

    def team_names(self) -> list[str]:
        return [t.name for t in self.teams]


def _normalize(value: str) -> str:
    import unicodedata

    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.lower().replace("_", " ").split())


def sort_projections(projections: Iterable[PlayerProjection]) -> list[PlayerProjection]:
    return sorted(
        projections,
        key=lambda p: (p.player.role_order, -p.expected_points, -p.starter_prob, p.player.name),
    )