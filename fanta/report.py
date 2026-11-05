"""Report leggibili per il terminale e output JSON."""

from __future__ import annotations

import sys
from typing import Any, Iterable, Sequence

from .models import (
    League,
    LeagueSettings,
    Lineup,
    OptimizationResult,
    PlayerProjection,
)
from .optimizer import defense_modifier


# ---------------------------------------------------------------------------
# Tabelle
# ---------------------------------------------------------------------------


def _align(value: str, width: int, mode: str) -> str:
    if mode == "r":
        return value.rjust(width)
    if mode == "c":
        return value.center(width)
    return value.ljust(width)


def format_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    aligns: Sequence[str] | None = None,
) -> str:
    rows = [[str(cell) for cell in row] for row in rows]
    headers = [str(header) for header in headers]
    aligns = list(aligns or ["l"] * len(headers))
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    separator = "-+-".join("-" * width for width in widths)
    lines = [
        " | ".join(_align(header, widths[i], aligns[i]) for i, header in enumerate(headers)),
        separator,
    ]
    for row in rows:
        lines.append(
            " | ".join(
                _align(cell, widths[i], aligns[i] if i < len(aligns) else "l")
                for i, cell in enumerate(row)
            )
        )
    return "\n".join(lines)


def _signed(value: float) -> str:
    if abs(value) < 0.005:
        return "0.00"
    return f"{value:+.2f}"


def _safe_stars(projection: PlayerProjection) -> str:
    stars = projection.matchup.stars
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        stars.encode(encoding)
        return stars
    except UnicodeEncodeError:
        full = max(1, min(5, int(round(projection.matchup.difficulty))))
        return "*" * full + "." * (5 - full)


def _projection_row(projection: PlayerProjection) -> list[str]:
    matchup = projection.matchup
    return [
        projection.player.role,
        projection.player.name,
        projection.player.team,
        matchup.opponent_code or "N/D",
        "C" if matchup.home else "T",
        _safe_stars(projection),
        f"{projection.player.starter_prob:.0%}",
        f"{projection.expected_raw_vote:.2f}",
        _signed(projection.expected_bonus),
        _signed(projection.expected_malus),
        f"{projection.expected_points:.2f}",
    ]


# ---------------------------------------------------------------------------
# Report testuale
# ---------------------------------------------------------------------------


def format_lineup(
    lineup: Lineup,
    league: League,
    team_name: str,
    matchday: int,
    settings: LeagueSettings,
    *,
    alternatives: Sequence[Lineup] = (),
    warnings: Sequence[str] = (),
    evaluated: int | None = None,
) -> str:
    separator = "=" * 78
    lines: list[str] = [
        separator,
        " FantaLineup 26/27 - formazione consigliata",
        f" Lega: {league.name}",
        f" Squadra: {team_name}",
        f" Giornata: {matchday}",
    ]
    if league.serie_a.updated:
        lines.append(f" Dati aggiornati: {league.serie_a.updated}")
    lines.append(separator)
    lines.append("")

    lines.append(f"Modulo consigliato: {lineup.formation}")
    lines.append(
        f"Punteggio atteso: {lineup.expected_total:.2f} "
        f"(modificatore difesa {_signed(lineup.defense_modifier)})"
    )
    lines.append(
        f"Obiettivo: {lineup.objective:.2f} | Rischio: {lineup.risk:.2f} | "
        f"Titolarità media: {lineup.avg_starter_prob:.0%}"
    )
    if evaluated is not None:
        lines.append(f"Combinazioni valutate: {evaluated:,}".replace(",", "."))
    lines.append("")

    headers = ["Ruolo", "Giocatore", "Squadra", "Avversario", "C/T", "Diff.", "Tit.", "Voto", "Bonus", "Malus", "Punti"]
    aligns = ["l", "l", "l", "l", "c", "c", "r", "r", "r", "r", "r"]
    rows = [_projection_row(projection) for projection in lineup.starters]
    lines.append(format_table(headers, rows, aligns))
    lines.append("")

    modifier, average, best_defenders = defense_modifier(
        lineup.goalkeeper, lineup.defenders, settings
    )
    lines.append("Modificatore difesa")
    lines.append(
        f"  Media: {average:.2f} -> {_signed(modifier)} "
        f"(portiere {lineup.goalkeeper.expected_raw_vote:.2f}"
    )
    if best_defenders:
        votes = ", ".join(f"{vote:.2f}" for vote in best_defenders)
        lines[-1] += f"; migliori difensori: {votes})"
    else:
        lines[-1] += ")"
    if len(lineup.defenders) < settings.min_difensori_modificatore:
        lines.append(
            f"  Modificatore non applicato: servono almeno "
            f"{settings.min_difensori_modificatore} difensori a voto."
        )
    else:
        lines.append(
            "  Regola: portiere + 3 migliori difensori; minimo "
            f"{settings.min_difensori_modificatore} difensori a voto."
        )
    lines.append("")

    lines.append("Panchina consigliata")
    bench_by_role: dict[str, list[PlayerProjection]] = {role: [] for role in "PDCA"}
    for projection in lineup.bench:
        bench_by_role.setdefault(projection.player.role, []).append(projection)
    for role, label in (("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti")):
        players = bench_by_role.get(role, [])
        if not players:
            continue
        names = ", ".join(
            f"{p.player.name} ({p.player.team}, {p.expected_points:.2f})" for p in players
        )
        lines.append(f"  {label}: {names}")
    if not lineup.bench:
        lines.append("  Nessun giocatore disponibile in panchina.")
    lines.append("")

    captain = lineup.captain
    lines.append(
        f"Capitano consigliato (se previsto): {captain.player.name} "
        f"({captain.player.team}, {captain.expected_points:.2f} punti attesi)"
    )
    lines.append("")

    notes: list[str] = []
    for projection in lineup.starters:
        if projection.notes:
            notes.append(f"  - {projection.player.name}: {', '.join(projection.notes)}")
    if notes:
        lines.append("Giocatori da valutare")
        lines.extend(notes)
        lines.append("")

    if warnings:
        lines.append("Avvisi")
        for warning in warnings:
            lines.append(f"  - {warning}")
        lines.append("")

    if alternatives:
        lines.append("Alternative di modulo")
        for alternative in alternatives:
            lines.append(
                f"  {alternative.formation}: {alternative.expected_total:.2f} punti "
                f"(modificatore {_signed(alternative.defense_modifier)}, "
                f"titolarità media {alternative.avg_starter_prob:.0%})"
            )
        lines.append("")

    lines.append("Legenda")
    lines.append(
        "  Diff. = difficoltà avversario (1-5 stelle); Tit. = probabilità di titolarità; "
        "Voto = voto atteso senza bonus;"
    )
    lines.append(
        "  Bonus/Malus = valore atteso degli eventi; Punti = contributo atteso pesato per titolarità."
    )
    lines.append(
        "  Il punteggio atteso include il modificatore difesa e il voto del sostituto per i giocatori "
        "che non dovessero scendere in campo."
    )
    if league.source:
        lines.append(f"  Fonte lega: {league.source}")
    lines.append(separator)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def projection_to_dict(projection: PlayerProjection) -> dict[str, Any]:
    matchup = projection.matchup
    return {
        "id": projection.player.id,
        "ruolo": projection.player.role,
        "nome": projection.player.name,
        "squadra": projection.player.team,
        "avversario": matchup.opponent_code or "N/D",
        "casa": matchup.home,
        "difficolta": round(matchup.difficulty, 2),
        "difficolta_label": matchup.label,
        "titolarita": round(projection.player.starter_prob, 3),
        "voto_base": round(projection.base_vote, 2),
        "correzione_matchup": round(projection.modifier_vote, 2),
        "bonus_atteso": round(projection.expected_bonus, 2),
        "malus_atteso": round(projection.expected_malus, 2),
        "punti_attesi": round(projection.expected_points, 2),
        "prob_gol": round(projection.goal_prob, 3),
        "prob_assist": round(projection.assist_prob, 3),
        "gol_attesi": round(projection.expected_goals, 3),
        "assist_attesi": round(projection.expected_assists, 3),
        "rischio": round(projection.risk, 2),
        "note": projection.notes,
    }


def lineup_to_dict(lineup: Lineup) -> dict[str, Any]:
    return {
        "modulo": lineup.formation,
        "punteggio_atteso": round(lineup.expected_total, 2),
        "modificatore_difesa": round(lineup.defense_modifier, 2),
        "media_difesa": round(lineup.defense_average, 2),
        "obiettivo": round(lineup.objective, 2),
        "rischio": round(lineup.risk, 2),
        "titolarita_media": round(lineup.avg_starter_prob, 3),
        "titolari": [projection_to_dict(projection) for projection in lineup.starters],
        "panchina": [projection_to_dict(projection) for projection in lineup.bench],
        "capitano": lineup.captain.player.name,
    }


def result_to_dict(
    result: OptimizationResult,
    league: League,
    team_name: str,
    matchday: int,
) -> dict[str, Any]:
    return {
        "lega": league.name,
        "squadra": team_name,
        "giornata": matchday,
        "stagione": league.serie_a.season,
        "aggiornato": league.serie_a.updated,
        "formazione": lineup_to_dict(result.best),
        "alternative": [lineup_to_dict(lineup) for lineup in result.alternatives],
        "avvisi": result.warnings,
        "combinazioni_valutate": result.evaluated,
        "approssimato": result.approximate,
    }
