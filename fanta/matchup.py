"""Difficoltà degli incontri e stima dei gol attesi.

La difficoltà è un punteggio da 1 (avversario molto abbordabile) a 5
(avversario molto difficile). Combina:

- posizione in classifica dell'avversario;
- punti per partita dell'avversario;
- gol attesi contro la nostra squadra (``lambda_against``).

I gol attesi della nostra squadra (``lambda_for``) servono al motore per
scalare le probabilità di gol e assist dei singoli giocatori.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CODE_TO_NAME
from .models import Fixture, LeagueSettings, Matchup, SerieAData, SerieATeam

FALLBACK_LEAGUE_AVERAGE = 1.35


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def difficulty_label(difficulty: float) -> str:
    if difficulty <= 1.75:
        return "Molto facile"
    if difficulty <= 2.50:
        return "Facile"
    if difficulty <= 3.25:
        return "Media"
    if difficulty <= 4.00:
        return "Difficile"
    return "Molto difficile"


@dataclass
class TeamStrength:
    attack: float
    defense: float


class MatchupModel:
    def __init__(self, serie_a: SerieAData, settings: LeagueSettings):
        self.serie_a = serie_a
        self.settings = settings
        self.teams = serie_a.teams
        self.league_average = self._league_average()
        self.home_factor, self.away_factor = self._venue_factors()
        self.strength = self._team_strengths()

    # ------------------------------------------------------------------
    # Stime di base
    # ------------------------------------------------------------------

    def _league_average(self) -> float:
        total_played = sum(team.played for team in self.teams.values())
        total_goals = sum(team.gf for team in self.teams.values())
        if total_played <= 0:
            return FALLBACK_LEAGUE_AVERAGE
        value = total_goals / total_played
        if not (0.5 <= value <= 4.0):
            return FALLBACK_LEAGUE_AVERAGE
        return value

    def _venue_factors(self) -> tuple[float, float]:
        played = [fixture for fixture in self.serie_a.fixtures if fixture.played]
        if len(played) >= 20:
            home_goals = sum(f.home_goals or 0 for f in played)
            away_goals = sum(f.away_goals or 0 for f in played)
            home_average = home_goals / len(played)
            away_average = away_goals / len(played)
            home_factor = home_average / self.league_average
            away_factor = away_average / self.league_average
            if 0.70 <= home_factor <= 1.30 and 0.70 <= away_factor <= 1.30:
                return home_factor, away_factor
        return self.settings.fattore_casa_attacco, self.settings.fattore_trasferta_attacco

    def _team_strengths(self) -> dict[str, TeamStrength]:
        shrink = max(1, self.settings.partite_shrink_rate)
        strengths: dict[str, TeamStrength] = {}
        for code, team in self.teams.items():
            attack_raw = team.gfp / self.league_average if self.league_average else 1.0
            defense_raw = team.gap / self.league_average if self.league_average else 1.0
            attack = (attack_raw * team.played + 1.0 * shrink) / (team.played + shrink)
            defense = (defense_raw * team.played + 1.0 * shrink) / (team.played + shrink)
            strengths[code] = TeamStrength(
                attack=clamp(attack, 0.45, 2.20),
                defense=clamp(defense, 0.45, 2.20),
            )
        return strengths

    def _strength(self, code: str) -> TeamStrength:
        return self.strength.get(code, TeamStrength(1.0, 1.0))

    # ------------------------------------------------------------------
    # Gol attesi
    # ------------------------------------------------------------------

    def expected_goals(self, team_code: str, opponent_code: str, home: bool) -> tuple[float, float]:
        """Restituisce (gol attesi nostri, gol attesi avversari)."""
        ours = self._strength(team_code)
        theirs = self._strength(opponent_code)
        venue_for = self.home_factor if home else self.away_factor
        venue_against = self.away_factor if home else self.home_factor
        lambda_for = self.league_average * ours.attack * theirs.defense * venue_for
        lambda_against = self.league_average * theirs.attack * ours.defense * venue_against
        return clamp(lambda_for, 0.20, 4.00), clamp(lambda_against, 0.20, 4.00)

    def baseline_conceded(self, team_code: str) -> float:
        """Gol subiti attesi da una squadra contro un avversario medio, in campo neutro."""
        return self.league_average * self._strength(team_code).defense

    # ------------------------------------------------------------------
    # Matchup
    # ------------------------------------------------------------------

    def matchup(self, team_code: str, matchday: int) -> Matchup:
        fixture: Fixture | None = self.serie_a.fixture_for(team_code, matchday)
        if fixture is None:
            return Matchup(
                opponent_code="",
                opponent_name="N/D",
                home=False,
                difficulty=3.0,
                label="Da definire",
                lambda_for=self.league_average,
                lambda_against=self.league_average,
                opponent_position=0,
            )

        home = fixture.home == team_code
        opponent_code = fixture.away if home else fixture.home
        lambda_for, lambda_against = self.expected_goals(team_code, opponent_code, home)
        opponent = self.teams.get(opponent_code)

        if opponent is None:
            position_score = 0.5
            ppg_score = 0.5
        else:
            teams_count = max(1, len(self.teams) - 1)
            position = opponent.position if opponent.position > 0 else 10
            position_score = 1.0 - (position - 1) / teams_count
            ppg_score = clamp(opponent.ppg / 2.40, 0.0, 1.0)

        defense_score = clamp(lambda_against / (2.0 * self.league_average), 0.0, 1.0)
        raw = 0.45 * position_score + 0.25 * ppg_score + 0.30 * defense_score
        difficulty = clamp(1.0 + 4.0 * raw, 1.0, 5.0)

        return Matchup(
            opponent_code=opponent_code,
            opponent_name=CODE_TO_NAME.get(opponent_code, opponent_code),
            home=home,
            difficulty=difficulty,
            label=difficulty_label(difficulty),
            lambda_for=lambda_for,
            lambda_against=lambda_against,
            opponent_position=opponent.position if opponent else 0,
        )

    def matchup_for_team(self, team: SerieATeam, matchday: int) -> Matchup:
        return self.matchup(team.code, matchday)


def matchup_short(matchup: Matchup) -> str:
    venue = "C" if matchup.home else "T"
    return f"{matchup.opponent_code or 'N/D'} ({venue})"