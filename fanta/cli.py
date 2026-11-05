"""Interfaccia a riga di comando di FantaLineup 26/27."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import parse_formation
from .dataio import (
    BASE_DIR,
    LEAGUE_FILE,
    DataError,
    ensure_data,
    load_players,
    load_serie_a,
    load_settings,
    read_json,
    resolve_data_dir,
)
from .demo import DEMO_TEAMS, ensure_demo_league
from .engine import ProjectionEngine
from .xlsxio import is_calendar_workbook
from .league import (
    FantacalcioURLError,
    LeagueError,
    PlayerNotFoundError,
    build_manual_league,
    create_league_file,
    describe_league,
    load_league,
)
from .matchup import MatchupModel
from .models import League, LeagueSettings
from .optimizer import LineupError, LineupOptimizer
from .report import format_lineup, result_to_dict

COMMANDS = {"gui", "consiglia", "update", "demo", "elenco-squadre", "importa"}


# ---------------------------------------------------------------------------
# Input interattivo
# ---------------------------------------------------------------------------


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _discover_leagues(data_dir: Path) -> list[tuple[str, str]]:
    """Trova i file di lega salvati (escluso la demo).

    Cerca i file JSON ``lega*.json`` / ``league*.json`` e i file Excel ``*.xlsx``
    nella cartella dati e nella cartella del programma.
    Restituisce coppie (nome della lega, percorso del file).
    """
    found: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for directory in (data_dir, BASE_DIR):
        if not directory.is_dir():
            continue
        candidates = [
            *directory.glob("lega*.json"),
            *directory.glob("league*.json"),
            *directory.glob("*.xlsx"),
        ]
        for path in sorted(candidates):
            resolved = path.resolve()
            if resolved in seen or path.name == LEAGUE_FILE or path.name.startswith("~$"):
                continue
            if path.suffix.lower() == ".xlsx" and is_calendar_workbook(path):
                continue  # è un calendario, non una lega con le rose
            seen.add(resolved)
            name = path.stem
            if path.suffix.lower() == ".json":
                try:
                    raw = read_json(path)
                    if isinstance(raw, dict):
                        name = str(raw.get("nome") or raw.get("name") or raw.get("lega") or name)
                except Exception:
                    pass
            found.append((name, str(path)))
    return found


def _prompt_league_address(data_dir: Path) -> str:
    options: list[tuple[str, str | None]] = [("Lega demo (8 squadre di esempio)", "demo")]
    for name, path in _discover_leagues(data_dir):
        options.append((f"{name} ({Path(path).name})", path))
    options.append(("Altra lega (file JSON, Excel o URL pubblico)", None))

    print("\nQuale lega del fantacalcio vuoi usare?")
    for index, (label, _) in enumerate(options, start=1):
        print(f"  {index}. {label}")

    while True:
        value = input(f"Scegli la lega (1-{len(options)}, invio per la demo): ").strip()
        if not value:
            return "demo"
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(options):
                address = options[index - 1][1]
                if address is not None:
                    return address
                return input("Inserisci il file JSON/Excel o l'URL della lega: ").strip() or "demo"
            print(f"Scelta non valida: inserisci un numero da 1 a {len(options)}.")
            continue
        # compatibilità: si può ancora scrivere direttamente un file, un URL o 'demo'
        return value


def _prompt_team(league: League) -> Any:
    print("\nSquadre disponibili:")
    for index, team in enumerate(league.teams, start=1):
        print(f"  {index}. {team.name} ({len(team.roster)} giocatori)")
    while True:
        value = input("Scegli la squadra (numero o nome, invio per la prima): ").strip()
        if not value:
            return league.teams[0]
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(league.teams):
                return league.teams[index - 1]
        try:
            return league.team(value)
        except KeyError as exc:
            print(exc)


def _parse_modules(value: str) -> list[str]:
    modules = [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
    if not modules:
        raise ValueError("Nessun modulo specificato.")
    for module in modules:
        parse_formation(module)
    return modules


# ---------------------------------------------------------------------------
# Comandi
# ---------------------------------------------------------------------------


def run_update(args: argparse.Namespace) -> int:
    from .updater import update_data

    data_dir = resolve_data_dir(args.data_dir)
    summary = update_data(data_dir, verbose=True)
    print("\nAggiornamento completato:")
    print(f"  giocatori: {summary['giocatori']}")
    print(f"  partite: {summary['partite']}")
    print(f"  squadre: {summary['squadre']}")
    print(f"  giornata corrente: {summary['giornata']}")
    print(f"  giornata titolarità: {summary['giornata_titolarita']}")
    print(f"  dati scritti in: {data_dir}")
    return 0


def _load_base(data_dir: Path) -> tuple[dict[str, Any], Any, Any]:
    players, _ = load_players(data_dir=data_dir)
    serie_a = load_serie_a(data_dir=data_dir)
    settings = load_settings(data_dir=data_dir)
    return players, serie_a, settings


def _resolve_league(
    args: argparse.Namespace,
    data_dir: Path,
    players: dict[str, Any],
    serie_a: Any,
    settings: LeagueSettings,
) -> League:
    if args.rosa:
        return build_manual_league(
            args.squadra or "La mia squadra",
            args.rosa,
            league_name="Lega manuale",
            data_dir=data_dir,
            players=players,
            serie_a=serie_a,
            settings=settings,
            matchday=args.giornata,
        )

    address = args.lega
    interactive = _is_interactive() and not args.no_interattivo
    if not address:
        address = _prompt_league_address(data_dir) if interactive else "demo"

    while True:
        try:
            return load_league(
                address,
                data_dir=data_dir,
                players=players,
                serie_a=serie_a,
                settings=settings,
            )
        except FantacalcioURLError as exc:
            if not interactive:
                raise
            print(f"\n{exc}", file=sys.stderr)
            address = input(
                "Inserisci un file JSON, un URL JSON pubblico o 'demo' "
                "(invio per la demo): "
            ).strip() or "demo"
        except PlayerNotFoundError:
            raise
        except LeagueError as exc:
            if not interactive:
                raise
            print(f"\nErrore nella lega: {exc}", file=sys.stderr)
            address = input("Riprova con un altro indirizzo (invio per la demo): ").strip() or "demo"


def _choose_team(args: argparse.Namespace, league: League) -> Any:
    if args.squadra:
        return league.team(args.squadra)
    interactive = _is_interactive() and not args.no_interattivo
    if interactive:
        return _prompt_team(league)
    if len(league.teams) == 1:
        return league.teams[0]
    names = ", ".join(team.name for team in league.teams)
    raise LeagueError(
        "Specifica la squadra con --squadra. Squadre disponibili: " + names
    )


def run_consiglia(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    if getattr(args, "aggiorna", False):
        from .updater import update_data

        update_data(data_dir, verbose=True)
    ensure_data(data_dir, auto_update=True)
    players, serie_a, base_settings = _load_base(data_dir)

    league = _resolve_league(args, data_dir, players, serie_a, base_settings)
    for warning in league.warnings:
        print(f"Avviso: {warning}", file=sys.stderr)
    team = _choose_team(args, league)

    settings = league.settings.clone()
    if args.modulo:
        try:
            settings.moduli = _parse_modules(args.modulo)
        except ValueError as exc:
            raise LeagueError(f"Modulo non valido: {exc}") from exc
    if args.rischio is not None:
        settings.rischio = max(0.0, args.rischio)

    matchday = args.giornata or league.matchday or serie_a.current_matchday

    matchup_model = MatchupModel(league.serie_a, settings)
    engine = ProjectionEngine(settings, matchup_model)
    projections = engine.project_team(team, matchday)
    if not projections:
        raise LineupError(f"La squadra '{team.name}' non ha giocatori nella rosa.")

    warnings: list[str] = list(league.warnings)
    missing_fixture = [
        projection.player.name
        for projection in projections
        if projection.matchup.label == "Da definire"
    ]
    if missing_fixture:
        warnings.append(
            f"Avversario non trovato per la giornata {matchday}: "
            + ", ".join(missing_fixture[:5])
            + ("..." if len(missing_fixture) > 5 else "")
        )

    optimizer = LineupOptimizer(
        projections,
        settings,
        max_alternatives=max(0, args.top),
    )
    result = optimizer.optimize()
    warnings.extend(result.warnings)

    if args.json:
        payload = result_to_dict(result, league, team.name, matchday)
        payload["avvisi"] = warnings
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            format_lineup(
                result.best,
                league,
                team.name,
                matchday,
                settings,
                alternatives=result.alternatives,
                warnings=warnings,
                evaluated=result.evaluated,
            )
        )
    return 0


def run_gui(args: argparse.Namespace) -> int:
    """Apre la finestra grafica; se non è disponibile ripiega sulla versione testuale."""
    try:
        from .gui import GuiUnavailable, launch
    except ImportError:
        print(
            "Interfaccia grafica non disponibile (manca tkinter): uso la versione testuale.",
            file=sys.stderr,
        )
        return main(_text_fallback_argv(args))
    try:
        return launch(args)
    except GuiUnavailable as exc:
        print(
            f"Impossibile aprire la finestra ({exc}): uso la versione testuale.",
            file=sys.stderr,
        )
        return main(_text_fallback_argv(args))


def _text_fallback_argv(args: argparse.Namespace) -> list[str]:
    argv = ["consiglia"]
    for flag, value in (
        ("--lega", getattr(args, "lega", None)),
        ("--squadra", getattr(args, "squadra", None)),
        ("--giornata", getattr(args, "giornata", None)),
        ("--data-dir", getattr(args, "data_dir", None)),
    ):
        if value:
            argv += [flag, str(value)]
    return argv


def run_demo(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    path = ensure_demo_league(data_dir, force=args.rigenera)
    namespace = argparse.Namespace(
        lega=str(path),
        squadra=args.squadra or DEMO_TEAMS[0]["nome"],
        giornata=args.giornata,
        modulo=args.modulo,
        rischio=args.rischio,
        top=args.top,
        json=args.json,
        rosa=None,
        data_dir=args.data_dir,
        aggiorna=False,
        no_interattivo=True,
        verbose=args.verbose,
    )
    return run_consiglia(namespace)


def run_elenco_squadre(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    ensure_data(data_dir, auto_update=True)
    players, serie_a, settings = _load_base(data_dir)
    address = args.lega or "demo"
    league = load_league(
        address,
        data_dir=data_dir,
        players=players,
        serie_a=serie_a,
        settings=settings,
    )
    print(describe_league(league))
    if league.warnings:
        print("\nAvvisi:")
        for warning in league.warnings:
            print(f"  - {warning}")
    return 0


def run_importa(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    ensure_data(data_dir, auto_update=True)
    players, serie_a, settings = _load_base(data_dir)
    if not args.rosa:
        raise LeagueError("Specifica la rosa con --rosa (file di testo o elenco separato da virgole).")
    output = Path(args.output).expanduser() if args.output else data_dir / "lega_importata.json"
    path = create_league_file(
        args.squadra,
        args.rosa,
        output,
        league_name=args.nome_lega,
        data_dir=data_dir,
        players=players,
        serie_a=serie_a,
        settings=settings,
        matchday=args.giornata,
    )
    print(f"Lega scritta in: {path}")
    print("\nPer usarla:")
    print(
        f'  python fantacalcio.py consiglia --lega "{path}" '
        f'--squadra "{args.squadra}"'
    )
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantacalcio.py",
        description=(
            "FantaLineup 26/27: consiglia la formazione ottimale usando titolarità, "
            "bonus/malus, modificatore difesa e difficoltà degli avversari."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Esempi:\n"
            "  python fantacalcio.py demo\n"
            "  python fantacalcio.py consiglia --lega data/lega_demo.json --squadra \"I Magnifici\"\n"
            "  python fantacalcio.py consiglia --squadra \"La mia squadra\" --rosa rosa.txt\n"
            "  python fantacalcio.py update\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # gui ---------------------------------------------------------------
    gui = subparsers.add_parser(
        "gui",
        help="apre la finestra grafica (predefinito senza argomenti)",
        description="Apre la finestra grafica con la formazione consigliata.",
    )
    gui.add_argument("--lega", "-l", help="file JSON/Excel della lega, URL JSON o 'demo'")
    gui.add_argument("--squadra", "-s", help="squadra da selezionare all'avvio")
    gui.add_argument("--giornata", "-g", type=int, help="giornata da considerare")
    gui.add_argument("--data-dir", help="cartella dei dati (default: ./data)")
    gui.set_defaults(func=run_gui)

    # consiglia ---------------------------------------------------------
    consiglia = subparsers.add_parser(
        "consiglia",
        help="calcola la formazione consigliata",
        description="Calcola la formazione consigliata per una squadra della lega.",
    )
    consiglia.add_argument(
        "--lega",
        "-l",
        help="file JSON della lega, URL JSON pubblico oppure 'demo' (default: demo)",
    )
    consiglia.add_argument("--squadra", "-s", help="nome della squadra da ottimizzare")
    consiglia.add_argument("--giornata", "-g", type=int, help="giornata da considerare")
    consiglia.add_argument(
        "--modulo",
        "-m",
        help="moduli da usare, separati da virgola (es. 4-3-3,3-5-2)",
    )
    consiglia.add_argument(
        "--rischio",
        type=float,
        help="avversione al rischio (0 = neutrale; default 0.15)",
    )
    consiglia.add_argument(
        "--top",
        type=int,
        default=3,
        help="numero di moduli alternativi da mostrare (default 3)",
    )
    consiglia.add_argument("--json", action="store_true", help="stampa l'output in JSON")
    consiglia.add_argument(
        "--rosa",
        help="rosa manuale: file di testo (un giocatore per riga) o elenco separato da virgole",
    )
    consiglia.add_argument("--data-dir", help="cartella dei dati (default: ./data)")
    consiglia.add_argument(
        "--aggiorna",
        action="store_true",
        help="scarica i dati aggiornati prima di calcolare la formazione",
    )
    consiglia.add_argument(
        "--no-interattivo",
        action="store_true",
        help="non chiedere input; usa i valori di default",
    )
    consiglia.add_argument("--verbose", action="store_true", help="output più dettagliato")
    consiglia.set_defaults(func=run_consiglia)

    # update ------------------------------------------------------------
    update = subparsers.add_parser(
        "update",
        help="aggiorna i dati da Fantacalcio.it e Wikipedia",
        description="Scarica quotazioni, statistiche, probabili formazioni, classifica e calendario.",
    )
    update.add_argument("--data-dir", help="cartella dei dati (default: ./data)")
    update.set_defaults(func=run_update)

    # demo --------------------------------------------------------------
    demo = subparsers.add_parser(
        "demo",
        help="crea (se serve) e usa la lega dimostrativa",
        description="Genera una lega demo con 8 squadre e calcola la formazione.",
    )
    demo.add_argument("--squadra", "-s", help="squadra della lega demo")
    demo.add_argument("--giornata", "-g", type=int, help="giornata da considerare")
    demo.add_argument("--modulo", "-m", help="moduli da usare, separati da virgola")
    demo.add_argument("--rischio", type=float, help="avversione al rischio")
    demo.add_argument("--top", type=int, default=3, help="numero di alternative")
    demo.add_argument("--json", action="store_true", help="stampa l'output in JSON")
    demo.add_argument("--data-dir", help="cartella dei dati (default: ./data)")
    demo.add_argument(
        "--rigenera",
        action="store_true",
        help="rigenera la lega demo anche se esiste già",
    )
    demo.add_argument("--verbose", action="store_true", help="output più dettagliato")
    demo.set_defaults(func=run_demo)

    # elenco-squadre ----------------------------------------------------
    elenco = subparsers.add_parser(
        "elenco-squadre",
        help="elenca le squadre di una lega",
        description="Mostra le squadre e il numero di giocatori della rosa.",
    )
    elenco.add_argument("--lega", "-l", help="file JSON, URL JSON pubblico o 'demo'")
    elenco.add_argument("--data-dir", help="cartella dei dati (default: ./data)")
    elenco.set_defaults(func=run_elenco_squadre)

    # importa -----------------------------------------------------------
    importa = subparsers.add_parser(
        "importa",
        help="crea un file di lega da una rosa manuale",
        description=(
            "Risolve i nomi della rosa e scrive un file JSON canonico, "
            "pronto per il comando consiglia."
        ),
    )
    importa.add_argument("--squadra", "-s", required=True, help="nome della squadra")
    importa.add_argument(
        "--rosa",
        required=True,
        help="file di testo (un giocatore per riga) o elenco separato da virgole",
    )
    importa.add_argument("--output", "-o", help="file JSON di output")
    importa.add_argument("--nome-lega", default="Lega importata", help="nome della lega")
    importa.add_argument("--giornata", "-g", type=int, help="giornata da impostare")
    importa.add_argument("--data-dir", help="cartella dei dati (default: ./data)")
    importa.set_defaults(func=run_importa)

    return parser


def _configure_output() -> None:
    """Evita UnicodeEncodeError su console Windows con code page non UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["gui"]
    elif argv[0] not in COMMANDS and argv[0] not in {"-h", "--help"}:
        argv = ["consiglia", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nOperazione annullata.", file=sys.stderr)
        return 130
    except PlayerNotFoundError as exc:
        print(f"\nErrore nella rosa: {exc}", file=sys.stderr)
        return 1
    except (DataError, LeagueError, LineupError) as exc:
        print(f"\nErrore: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"\nErrore: {exc}", file=sys.stderr)
        return 1