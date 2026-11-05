"""Motore previsionale: voto base, bonus, malus e titolarità.

Il modello è volutamente trasparente e basato su dati osservabili:

- voto base = media voto della stagione, con shrinkage verso 6,0;
- gol e assist = frequenza per presenza, con shrinkage verso una prior per ruolo;
- gol attesi della squadra = forza offensiva * debolezza difensiva avversaria;
- bonus/malus = valore atteso degli eventi (gol, assist, cartellini, ecc.);
- contributo in formazione = probabilità di titolarità * fantavoto atteso
  + (1 - probabilità) * voto del sostituto.

Non è un modello di Machine Learning: è una stima robusta e spiegabile, adatta
a confrontare giocatori e moduli con i dati di una singola stagione.
"""

from __future__ import annotations

import math
from typing import Iterable

from .matchup import MatchupModel, clamp
from .models import (
    FantasyTeam,
    LeagueSettings,
    Matchup,
    Player,
    PlayerProjection,
)

# Prior per ruolo: frequenza tipica per presenza.
GOAL_PRIOR = {"P": 0.002, "D": 0.040, "C": 0.100, "A": 0.350}
ASSIST_PRIOR = {"P": 0.005, "D": 0.060, "C": 0.120, "A": 0.140}
YELLOW_PRIOR = {"P": 0.030, "D": 0.180, "C": 0.160, "A": 0.120}
RED_PRIOR = {"P": 0.005, "D": 0.020, "C": 0.015, "A": 0.010}
OWN_GOAL_PRIOR = {"P": 0.001, "D": 0.008, "C": 0.004, "A": 0.002}
PENALTY_SAVE_PRIOR = 0.020


def _shrink_rate(count: int, appearances: int, prior: float, k: int) -> float:
    if appearances <= 0:
        return prior
    return (count + prior * k) / (appearances + k)


class ProjectionEngine:
    def __init__(self, settings: LeagueSettings, matchup_model: MatchupModel):
        self.settings = settings
        self.matchup_model = matchup_model

    # ------------------------------------------------------------------
    # Componenti della proiezione
    # ------------------------------------------------------------------

    def _base_vote(self, player: Player) -> float:
        k = max(1, self.settings.partite_shrink_voto)
        if player.media_voto is not None and player.appearances > 0:
            return (player.media_voto * player.appearances + 6.0 * k) / (player.appearances + k)
        return 6.0

    def _matchup_adjustment(self, player: Player, matchup: Matchup) -> float:
        if player.role in {"P", "D"}:
            # Portiere e difensori: il voto scende quando la squadra subisce gol.
            # Si confrontano i gol subiti attesi in questa partita con quelli che la
            # squadra subisce di solito (già inclusi nella media voto del giocatore).
            # Il fattore campo è già dentro i gol attesi.
            if matchup.label == "Da definire":
                return 0.0
            baseline = self.matchup_model.baseline_conceded(player.team)
            shift = -self.settings.peso_gol_subiti_voto * (matchup.lambda_against - baseline)
            return clamp(shift, -0.80, 0.50)
        # Centrocampisti e attaccanti: avversario facile alza il voto atteso, difficile lo abbassa.
        adjustment = 0.12 * (3.0 - matchup.difficulty)
        adjustment += 0.04 if matchup.home else -0.04
        return adjustment

    def _attack_scale(self, matchup: Matchup) -> float:
        average = self.matchup_model.league_average
        if average <= 0:
            return 1.0
        return clamp(matchup.lambda_for / average, 0.55, 1.75)

    def _penalty_save_rate(self, player: Player) -> float:
        k = max(1, self.settings.partite_shrink_rate)
        return _shrink_rate(player.penalties_saved, player.appearances, PENALTY_SAVE_PRIOR, k)

    # ------------------------------------------------------------------
    # Proiezione
    # ------------------------------------------------------------------

    def project(self, player: Player, matchday: int) -> PlayerProjection:
        matchup = self.matchup_model.matchup(player.team, matchday)
        role = player.role if player.role in GOAL_PRIOR else "C"
        k = max(1, self.settings.partite_shrink_rate)

        base_vote = self._base_vote(player)
        modifier_vote = self._matchup_adjustment(player, matchup)
        attack_scale = self._attack_scale(matchup)

        goal_rate = _shrink_rate(player.goals, player.appearances, GOAL_PRIOR[role], k)
        assist_rate = _shrink_rate(player.assists, player.appearances, ASSIST_PRIOR[role], k)
        yellow_rate = _shrink_rate(
            player.yellow_cards, player.appearances, YELLOW_PRIOR[role], k
        )
        red_rate = _shrink_rate(player.red_cards, player.appearances, RED_PRIOR[role], k)
        own_goal_rate = _shrink_rate(0, player.appearances, OWN_GOAL_PRIOR[role], k)

        expected_goals = goal_rate * attack_scale
        expected_assists = assist_rate * attack_scale
        goal_prob = 1.0 - math.exp(-expected_goals)
        assist_prob = 1.0 - math.exp(-expected_assists)

        expected_bonus = (
            expected_goals * self.settings.bonus_gol
            + expected_assists * self.settings.bonus_assist
        )

        if role == "P":
            expected_bonus += self._penalty_save_rate(player) * self.settings.bonus_rigore_parato

        if self.settings.bonus_porta_inviolata > 0 and role in {"P", "D"}:
            clean_sheet_prob = math.exp(-matchup.lambda_against)
            expected_bonus += clean_sheet_prob * self.settings.bonus_porta_inviolata

        expected_malus = 0.0
        expected_malus += yellow_rate * self.settings.malus_ammonizione
        expected_malus += red_rate * self.settings.malus_espulsione
        expected_malus += own_goal_rate * self.settings.malus_autogol

        if player.penalties_attempted > 0:
            attempts = max(1, player.appearances)
            attempt_rate = player.penalties_attempted / attempts
            misses = max(0, player.penalties_attempted - player.penalties_scored)
            miss_rate = misses / player.penalties_attempted
            expected_malus += (
                attempt_rate * miss_rate * self.settings.malus_rigore_sbagliato
            )

        if role == "P" or (role == "D" and not self.settings.gol_subito_solo_portiere):
            expected_malus += matchup.lambda_against * self.settings.malus_gol_subito

        expected_vote_if_plays = base_vote + modifier_vote + expected_bonus + expected_malus

        starter_prob = clamp(player.starter_prob, 0.0, 1.0)
        fallback = self.settings.voto_sostituto_modificatore
        expected_points = starter_prob * expected_vote_if_plays + (1.0 - starter_prob) * fallback

        # Rischio: incertezza sulla titolarità + variabilità del rendimento.
        difference = max(0.0, expected_vote_if_plays - fallback)
        risk = (1.0 - starter_prob) * 4.0
        risk += starter_prob * (1.0 - starter_prob) * difference * difference * 0.10

        notes: list[str] = []
        if player.status == "infortunato":
            notes.append("Infortunato")
        elif player.status == "squalificato":
            notes.append("Squalificato")
        elif player.status == "in dubbio":
            notes.append("In dubbio")
        elif player.status == "diffidato":
            notes.append("Diffidato")
        if starter_prob < 0.50:
            notes.append("Titolarità bassa")
        if player.appearances == 0:
            notes.append("Poche presenze: stima prudente")
        if player.media_voto is None:
            notes.append("Media voto non disponibile")
        if matchup.label == "Da definire":
            notes.append("Avversario da definire")

        return PlayerProjection(
            player=player,
            matchup=matchup,
            base_vote=base_vote,
            modifier_vote=modifier_vote,
            expected_bonus=expected_bonus,
            expected_malus=expected_malus,
            expected_points=expected_points,
            goal_prob=goal_prob,
            assist_prob=assist_prob,
            expected_goals=expected_goals,
            expected_assists=expected_assists,
            risk=risk,
            notes=notes,
        )

    def project_all(
        self,
        players: Iterable[Player],
        matchday: int,
    ) -> dict[str, PlayerProjection]:
        return {player.id: self.project(player, matchday) for player in players}

    def project_team(self, team: FantasyTeam, matchday: int) -> list[PlayerProjection]:
        return [self.project(player, matchday) for player in team.roster]