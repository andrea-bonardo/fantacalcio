"""Loader del tema grafico: legge fantalineup.css e ne applica i valori a tkinter.

tkinter non ha un motore CSS, quindi qui si legge solo la "parte comune" del file
(da ``:root`` fino al marcatore ``@web-only``): token (``--colore: #abc;``) e regole
piatte per componente. Solo libreria standard.

Se il file manca o è rotto l'app si apre lo stesso con un tema di emergenza
(``EMERGENCY_TOKENS``), e un token mancante dà un valore di riserva, mai un'eccezione.
"""

from __future__ import annotations

import re
from pathlib import Path

THEME_FILE = Path(__file__).resolve().parent / "theme" / "fantalineup.css"
WEB_MARKER = "@web-only"

# Tema di emergenza: colori della vecchia interfaccia chiara, usati solo se il CSS non si carica.
EMERGENCY_TOKENS: dict[str, str] = {
    "--bg": "#f5f5f7", "--bg-elevated": "#ffffff", "--surface": "#ffffff",
    "--surface-2": "#e8e8ed", "--surface-hover": "#dcdce2",
    "--border": "#d2d2d7", "--border-strong": "#b8b8c0",
    "--text": "#1d1d1f", "--text-muted": "#86868b", "--text-faint": "#aeaeb2",
    "--text-on-accent": "#ffffff",
    "--accent": "#007aff", "--accent-hover": "#2b8fff", "--accent-soft": "#cfe4ff", "--accent-2": "#5ac8fa",
    "--good": "#1b7a35", "--good-bg": "#e3f4e6", "--bad": "#c62828", "--bad-bg": "#fbe4e1",
    "--warn": "#b45309", "--warn-bg": "#fff2c4", "--info": "#007aff",
    "--grass-a": "#2f8f4e", "--grass-b": "#2a8247", "--pitch-line": "#e8f5e9", "--pitch-shadow": "#0d2a17",
    "--captain": "#f5c518", "--captain-text": "#3b2d00",
    "--ring-doubt": "#ff9800", "--ring-out": "#e53935", "--ring-selected": "#00e5ff",
    "--ring-swap": "#ffffff", "--ring-default": "#ffffff",
    "--verdict-v": "#2e9e4f", "--verdict-s": "#d2453b", "--verdict-e": "#78909c",
    "--diff-1": "#2e9e4f", "--diff-3": "#e0b020", "--diff-5": "#d2453b",
    "--font-ui": '"Segoe UI", Arial, sans-serif',
    "--font-size-xs": "8px", "--font-size-sm": "9px", "--font-size-md": "10px",
    "--font-size-lg": "12px", "--font-size-xl": "22px",
    "--row-height": "24px",
}


class Theme:
    def __init__(self, tokens: dict[str, str], rules: dict[str, dict[str, str]]):
        self.tokens = {**EMERGENCY_TOKENS, **tokens}  # i token mancanti ripiegano sul tema di emergenza
        self.rules = rules

    def color(self, token: str) -> str:
        return self.tokens.get(f"--{token}", "#ff00ff")  # magenta = token inesistente, ben visibile

    def rule(self, selector: str) -> dict[str, str]:
        return self.rules.get(selector, {})

    def prop(self, selector: str, name: str, default: str = "") -> str:
        return self.rule(selector).get(name) or default

    def size(self, selector: str, default: int = 9) -> int:
        """Dimensione del font di una regola, in punti."""
        return px(self.prop(selector, "font-size"), default)

    def weight(self, selector: str) -> str:
        return "bold" if self.prop(selector, "font-weight") == "bold" else "normal"


def _resolve(value: str, tokens: dict[str, str]) -> str:
    for _ in range(5):  # var() annidati
        value = re.sub(r"var\((--[\w-]+)\)", lambda m: tokens.get(m.group(1), ""), value)
    return value.strip()


def parse_css(text: str) -> Theme:
    text = text.split(WEB_MARKER)[0]  # ignora la parte solo web
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # via i commenti
    tokens: dict[str, str] = {}
    rules: dict[str, dict[str, str]] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        declarations = {
            key.strip(): value.strip() for key, value in re.findall(r"([\w-]+)\s*:\s*([^;]+);", body)
        }
        for sel in selector.split(","):
            sel = sel.strip()
            if sel == ":root":
                tokens.update({k: v for k, v in declarations.items() if k.startswith("--")})
            elif sel:
                rules.setdefault(sel, {}).update(declarations)
    for declarations in rules.values():
        for key, value in declarations.items():
            declarations[key] = _resolve(value, tokens)
    return Theme(tokens, rules)


def load_theme(path: Path = THEME_FILE) -> Theme:
    """Legge il CSS; se manca o non si legge restituisce il tema di emergenza."""
    try:
        return parse_css(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Theme({}, {})


def pick_font(theme: Theme, root=None) -> str:
    """Primo font dello stack --font-ui realmente installato."""
    import tkinter.font as tkfont

    try:
        available = set(tkfont.families(root))
    except Exception:  # noqa: BLE001 - nessuna finestra: si usa il font di sistema
        return "TkDefaultFont"
    for name in theme.color("font-ui").split(","):
        name = name.strip().strip('"')
        if name in available:
            return name
    return "TkDefaultFont"


def px(value: str, default: int = 0) -> int:
    match = re.search(r"-?\d+", value or "")
    return int(match.group()) if match else default


def box(value: str, default: tuple[int, int, int, int] = (0, 0, 0, 0)) -> tuple[int, int, int, int]:
    """``padding: 8px 12px`` → (sinistra, alto, destra, basso) come vuole ttk (accetta 1, 2 o 4 valori)."""
    numbers = [int(n) for n in re.findall(r"-?\d+", value or "")]
    if len(numbers) == 1:
        top = right = bottom = left = numbers[0]
    elif len(numbers) == 2:
        top = bottom = numbers[0]
        left = right = numbers[1]
    elif len(numbers) == 4:
        top, right, bottom, left = numbers
    else:
        return default
    return left, top, right, bottom


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return 128, 128, 128