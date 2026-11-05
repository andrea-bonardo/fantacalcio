"""Ottimizzatore esatto della formazione.

Per ogni modulo ammesso il programma enumera tutte le combinazioni possibili
di portiere, difensori, centrocampisti e attaccanti della rosa. La funzione
obiettivo è:

    punti attesi + modificatore difesa - rischio * rischio_totale

dove i punti attesi dei singoli giocatori sono già pesati per la probabilità
di titolarità. Il modificatore difesa usa il voto atteso (senza bonus) del
portiere e dei tre migliori difensori, come nella tabella "Fantacampionato".
"""

from __future__ import annotations

import math
from collections import Counter
from itertools import combinations
from typing import Iterable, Sequence

from .config import BENCH_QUOTAS, ROLE_ORDER, parse_formation
from .models import (
    LeagueSettings,
    Lineup,
    OptimizationResult,
    PlayerProjection,
)


class LineupError(RuntimeError):
    """Impossibile costruire una formazione valida."""


# ---------------------------------------------------------------------------
# Modificatore difesa
# ---------------------------------------------------------------------------


def modifier_value(average: float, table: Sequence[Sequence[float]]) -> float:
    if average <= 0:
        return 0.0
    # La media è un numero con molti decimali (es. 6,246): con fasce come
    # 6,00-6,24 e 6,25-6,49 cadrebbe nel "buco" tra le due. Una fascia vale quindi
    # fino all'inizio della successiva.
    bands = sorted(
        (float(row[0]), float(row[1]), float(row[2])) for row in table if len(row) >= 3
    )
    for index, (low, high, value) in enumerate(bands):
        next_low = bands[index + 1][0] if index + 1 < len(bands) else None
        if low <= average <= high or (next_low is not None and high < average < next_low):
            return value
    for row in table:
        if len(row) == 2:
            threshold, value = float(row[0]), float(row[1])
            if average >= threshold:
                return value
    return 0.0


def defense_modifier(
    goalkeeper: PlayerProjection | None,
    defenders: Sequence[PlayerProjection],
    settings: LeagueSettings,
) -> tuple[float, float, list[float]]:
    """Restituisce (modificatore, media difesa, voti dei migliori difensori)."""
    best_defenders = sorted(
        (player.expected_raw_vote for player in defenders), reverse=True
    )[:3]
    if len(defenders) < settings.min_difensori_modificatore:
        return 0.0, 0.0, best_defenders
    votes = list(best_defenders)
    if settings.modificatore_portiere and goalkeeper is not None:
        votes.append(goalkeeper.expected_raw_vote)
    if not votes:
        return 0.0, 0.0, best_defenders
    average = sum(votes) / len(votes)
    return modifier_value(average, settings.tabella_modificatore), average, best_defenders


def _defense_modifier_values(
    goalkeeper_vote: float,
    best_defender_votes: Sequence[float],
    defender_count: int,
    settings: LeagueSettings,
) -> tuple[float, float]:
    if defender_count < settings.min_difensori_modificatore:
        return 0.0, 0.0
    votes = list(best_defender_votes)
    if settings.modificatore_portiere:
        votes.append(goalkeeper_vote)
    if not votes:
        return 0.0, 0.0
    average = sum(votes) / len(votes)
    return modifier_value(average, settings.tabella_modificatore), average


# ---------------------------------------------------------------------------
# Ottimizzatore
# ---------------------------------------------------------------------------


class LineupOptimizer:
    def __init__(
        self,
        projections: Iterable[PlayerProjection],
        settings: LeagueSettings,
        *,
        max_alternatives: int = 3,
        max_combinations: int = 4_000_000,
    ):
        self.projections = list(projections)
        self.settings = settings
        self.max_alternatives = max(0, max_alternatives)
        self.max_combinations = max(100_000, max_combinations)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _pools(self) -> dict[str, list[PlayerProjection]]:
        available = [p for p in self.projections if p.player.available]
        pools: dict[str, list[PlayerProjection]] = {}
        for role in "PDCA":
            pools[role] = sorted(
                (p for p in available if p.player.role == role),
                key=lambda p: (-p.expected_points, -p.player.starter_prob, p.player.name),
            )
        return pools

    def _estimate_combinations(
        self,
        pools: dict[str, list[PlayerProjection]],
        formations: Sequence[str],
    ) -> int:
        total = 0
        for formation in formations:
            try:
                d, c, a = parse_formation(formation)
            except ValueError:
                continue
            if (
                len(pools["P"]) < 1
                or len(pools["D"]) < d
                or len(pools["C"]) < c
                or len(pools["A"]) < a
            ):
                continue
            total += (
                len(pools["P"])
                * math.comb(len(pools["D"]), d)
                * math.comb(len(pools["C"]), c)
                * math.comb(len(pools["A"]), a)
            )
        return total

    def _prune_pools(
        self,
        pools: dict[str, list[PlayerProjection]],
        formations: Sequence[str],
    ) -> tuple[dict[str, list[PlayerProjection]], bool]:
        total = self._estimate_combinations(pools, formations)
        if total <= self.max_combinations:
            return pools, False

        # Requisiti minimi per ruolo tra tutti i moduli ammessi.
        required = {"P": 1, "D": 3, "C": 3, "A": 1}
        for formation in formations:
            try:
                d, c, a = parse_formation(formation)
            except ValueError:
                continue
            required["D"] = max(required["D"], d)
            required["C"] = max(required["C"], c)
            required["A"] = max(required["A"], a)

        best_pools = pools
        best_total = total
        max_size = min(14, max(len(pool) for pool in pools.values()))
        for size in range(max_size, min(required.values()) - 1, -1):
            candidate = {
                role: pool[: max(size, required[role])]
                for role, pool in pools.items()
            }
            candidate_total = self._estimate_combinations(candidate, formations)
            if candidate_total < best_total:
                best_pools = candidate
                best_total = candidate_total
            if candidate_total <= self.max_combinations:
                return candidate, True
        return best_pools, True

    def _pack(
        self,
        combo: Sequence[PlayerProjection],
        *,
        keep_votes: bool = False,
    ) -> tuple[float, float, list[float], tuple[str, ...], Sequence[PlayerProjection]]:
        points = 0.0
        risk = 0.0
        votes: list[float] = []
        teams: list[str] = []
        for projection in combo:
            points += projection.expected_points
            risk += projection.risk
            if keep_votes:
                votes.append(projection.expected_raw_vote)
            teams.append(projection.player.team)
        if keep_votes:
            votes.sort(reverse=True)
            votes = votes[:3]
        return points, risk, votes, tuple(teams), combo

    def _bench(self, starters: Sequence[PlayerProjection]) -> list[PlayerProjection]:
        """Panchina a quote fisse (2 P, 3 D, 3 C, 3 A): prima i disponibili, poi i migliori per punti."""
        used = {p.player.id for p in starters}
        by_role: dict[str, list[PlayerProjection]] = {role: [] for role in "PDCA"}
        for projection in sorted(
            (p for p in self.projections if p.player.id not in used),
            key=lambda p: (not p.player.available, -p.expected_points, p.player.name),
        ):
            by_role.setdefault(projection.player.role, []).append(projection)
        bench: list[PlayerProjection] = []
        for role, quota in BENCH_QUOTAS.items():
            bench.extend(by_role.get(role, [])[:quota])
        bench.sort(key=lambda p: (p.player.role_order, -p.expected_points, p.player.name))
        return bench


    

    def _build_lineup(
        self,
        formation: str,
        goalkeeper: PlayerProjection,
        defenders: Sequence[PlayerProjection],
        midfielders: Sequence[PlayerProjection],
        forwards: Sequence[PlayerProjection],
        defense_mod: float,
        defense_average: float,
        expected_total: float,
        risk: float,
    ) -> Lineup:
        starters = [goalkeeper, *defenders, *midfielders, *forwards]
        average_probability = sum(p.player.starter_prob for p in starters) / len(starters)
        return Lineup(
            formation=formation,
            goalkeeper=goalkeeper,
            defenders=list(defenders),
            midfielders=list(midfielders),
            forwards=list(forwards),
            bench=self._bench(starters),
            defense_modifier=defense_mod,
            defense_average=defense_average,
            expected_total=expected_total,
            avg_starter_prob=average_probability,
            risk=risk,
            objective=expected_total - self.settings.rischio * risk,
        )

    def _team_constraint_ok(
        self,
        goalkeeper_team: str,
        defender_teams: Sequence[str],
        midfielder_teams: Sequence[str],
        forward_teams: Sequence[str],
    ) -> bool:
        limit = self.settings.max_per_squadra_serie_a
        if limit <= 0:
            return True
        counts = Counter((goalkeeper_team, *defender_teams, *midfielder_teams, *forward_teams))
        return all(value <= limit for value in counts.values())

    # ------------------------------------------------------------------
    # Ottimizzazione
    # ------------------------------------------------------------------

    def optimize(self) -> OptimizationResult:
        pools = self._pools()
        missing = [role for role in "PDCA" if not pools.get(role)]
        if missing:
            labels = {"P": "portieri", "D": "difensori", "C": "centrocampisti", "A": "attaccanti"}
            raise LineupError(
                "Rosa incompleta: mancano " + ", ".join(labels[role] for role in missing) + "."
            )

        formations = [f for f in self.settings.moduli if f]
        if not formations:
            raise LineupError("Nessun modulo ammesso nelle impostazioni.")

        pools, approximate = self._prune_pools(pools, formations)
        warnings: list[str] = []
        if approximate:
            sizes = ", ".join(f"{role}={len(pools[role])}" for role in "PDCA")
            warnings.append(
                "Rosa molto numerosa: per contenere i tempi l'ottimizzazione usa i migliori "
                f"giocatori per ruolo ({sizes})."
            )

        best: Lineup | None = None
        best_by_formation: dict[str, Lineup] = {}
        evaluated = 0

        roster_counts = Counter(p.player.role for p in self.projections)
        for formation in formations:
            try:
                d_count, c_count, a_count = parse_formation(formation)
            except ValueError as exc:
                warnings.append(f"Modulo ignorato ({formation}): {exc}")
                continue

            needed = {
                "P": 1 + BENCH_QUOTAS["P"], "D": d_count + BENCH_QUOTAS["D"],
                "C": c_count + BENCH_QUOTAS["C"], "A": a_count + BENCH_QUOTAS["A"],
            }
            missing = [f"{role} {needed[role]}" for role in "PDCA" if roster_counts[role] < needed[role]]
            if missing:
                warnings.append(
                    f"Modulo {formation} non valido: la rosa non copre campo e panchina "
                    f"(servono almeno {', '.join(missing)})."
                )
                continue
            
            if (
                len(pools["P"]) < 1
                or len(pools["D"]) < d_count
                or len(pools["C"]) < c_count
                or len(pools["A"]) < a_count
            ):
                warnings.append(f"Modulo {formation} non disponibile con questa rosa.")
                continue

            defender_combos = list(combinations(pools["D"], d_count))
            midfielder_combos = list(combinations(pools["C"], c_count))
            forward_combos = list(combinations(pools["A"], a_count))

            defender_packs = [self._pack(combo, keep_votes=True) for combo in defender_combos]
            midfielder_packs = [self._pack(combo) for combo in midfielder_combos]
            forward_packs = [self._pack(combo) for combo in forward_combos]

            formation_best: Lineup | None = None

            for goalkeeper in pools["P"]:
                gk_points = goalkeeper.expected_points
                gk_risk = goalkeeper.risk
                gk_vote = goalkeeper.expected_raw_vote
                gk_team = goalkeeper.player.team

                for d_points, d_risk, d_votes, d_teams, defenders in defender_packs:
                    defense_mod, defense_average = _defense_modifier_values(
                        gk_vote, d_votes, d_count, self.settings
                    )
                    base_points = gk_points + d_points + defense_mod
                    base_risk = gk_risk + d_risk

                    for c_points, c_risk, _, c_teams, midfielders in midfielder_packs:
                        partial_points = base_points + c_points
                        partial_risk = base_risk + c_risk

                        for a_points, a_risk, _, a_teams, forwards in forward_packs:
                            if not self._team_constraint_ok(
                                gk_team, d_teams, c_teams, a_teams
                            ):
                                continue
                            evaluated += 1
                            total_points = partial_points + a_points
                            total_risk = partial_risk + a_risk
                            objective = total_points - self.settings.rischio * total_risk
                            if formation_best is not None and objective <= formation_best.objective:
                                continue
                            formation_best = self._build_lineup(
                                formation,
                                goalkeeper,
                                defenders,
                                midfielders,
                                forwards,
                                defense_mod,
                                defense_average,
                                total_points,
                                total_risk,
                            )

            if formation_best is None:
                warnings.append(f"Nessuna formazione valida per il modulo {formation}.")
                continue
            best_by_formation[formation] = formation_best
            if best is None or formation_best.objective > best.objective:
                best = formation_best

        if best is None:
            raise LineupError(
                "Non è stato possibile costruire una formazione valida. "
                "Controlla la rosa e i moduli ammessi."
            )

        alternatives = sorted(
            (lineup for lineup in best_by_formation.values() if lineup is not best),
            key=lambda lineup: lineup.objective,
            reverse=True,
        )[: self.max_alternatives]

        low_probability = [
            projection
            for projection in best.starters
            if projection.player.starter_prob < 0.50
        ]
        if low_probability:
            names = ", ".join(
                f"{p.player.name} ({p.player.starter_prob:.0%})" for p in low_probability
            )
            warnings.append(
                "Attenzione: alcuni titolari hanno titolarità inferiore al 50%: " + names + "."
            )

        if best.defense_modifier <= 0:
            if len(best.defenders) < self.settings.min_difensori_modificatore:
                warnings.append(
                    f"Il modulo {best.formation} non permette il modificatore difesa "
                    f"(servono almeno {self.settings.min_difensori_modificatore} difensori)."
                )
            else:
                warnings.append(
                    "Il modificatore difesa stimato è 0: la media dei voti non raggiunge "
                    "la prima soglia della tabella."
                )

        return OptimizationResult(
            best=best,
            alternatives=alternatives,
            warnings=warnings,
            evaluated=evaluated,
            approximate=approximate,
        )


def format_roster_by_role(projections: Iterable[PlayerProjection]) -> dict[str, list[PlayerProjection]]:
    grouped: dict[str, list[PlayerProjection]] = {role: [] for role in "PDCA"}
    for projection in projections:
        grouped.setdefault(projection.player.role, []).append(projection)
    for role in grouped:
        grouped[role].sort(key=lambda p: (-p.expected_points, p.player.name))
    return grouped


__all__ = [
    "LineupError",
    "LineupOptimizer",
    "defense_modifier",
    "format_roster_by_role",
    "modifier_value",
]