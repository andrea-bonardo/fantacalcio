#!/usr/bin/env python3
"""Punto di ingresso di FantaLineup 26/27.

Uso rapido:
    python fantacalcio.py            (avvia il server locale e apre il browser)
    python fantacalcio.py web --lega data/lega_demo.json --squadra "I Magnifici" --port 8765
    python fantacalcio.py gui        (vecchia finestra tkinter)
    python fantacalcio.py demo
    python fantacalcio.py consiglia --lega data/lega_demo.json --squadra "I Magnifici"
    python fantacalcio.py update
"""

from __future__ import annotations

import argparse
import sys
import traceback


def _console_is_own() -> bool:
    """True se il programma è stato aperto con doppio clic (finestra propria)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint * 4)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, 4)
        return count <= 2
    except Exception:
        return False


def _wait_before_closing() -> None:
    """Tiene aperta la finestra finché non premi invio."""
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            input("\nPremi invio per chiudere...")
    except (EOFError, KeyboardInterrupt):
        pass


def _run_web(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fantacalcio.py web", description="Interfaccia web locale")
    parser.add_argument("--lega")
    parser.add_argument("--squadra")
    parser.add_argument("--data-dir", dest="data_dir")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="non aprire il browser")
    args = parser.parse_args(argv)

    from fanta.server import launch_web

    return launch_web(args, port=args.port, open_browser=not args.no_browser)


def _dispatch(argv: list[str]) -> tuple[int, bool]:
    """Restituisce (codice di uscita, True se l'interfaccia è stata mostrata con successo)."""
    if not argv or argv[0] == "web" or argv[0].startswith("--"):
        # senza comando (o solo opzioni) si avvia l'interfaccia web
        return _run_web(argv[1:] if argv and argv[0] == "web" else argv), True
    if argv[0] == "gui":
        from fanta.cli import main

        code = main([])
        gui_module = sys.modules.get("fanta.gui")
        return code, bool(getattr(gui_module, "SHOWN", False))

    from fanta.cli import main

    return main(argv), False


if __name__ == "__main__":
    opened_by_double_click = len(sys.argv) == 1 and _console_is_own()
    try:
        code, shown = _dispatch(sys.argv[1:])
    except Exception:
        traceback.print_exc()
        code, shown = 1, False
    if opened_by_double_click and not (code == 0 and shown):
        _wait_before_closing()  # serve per errori e versione testuale
    raise SystemExit(code)