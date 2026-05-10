#!/usr/bin/env python3
"""
Victoria II Modern War Analyzer
================================

A single-file, batteries-included analyzer for Victoria II save games. It can
be used as a modern Tk desktop application or as a command-line report tool.

The analyzer intentionally does not bundle Victoria II artwork. Instead, it
looks for a local Victoria II installation and, when possible, uses compatible
PNG/GIF/PPM artwork from that installation as backgrounds. Most Victoria II
assets are DDS/TGA, which the Python standard library cannot decode, so the UI
falls back to a parchment-and-steel theme inspired by the game when direct
loading is not possible.

Usage:
    python victoria2_war_analyzer_modern.py              # launch GUI
    python victoria2_war_analyzer_modern.py save.v2      # text report
    python victoria2_war_analyzer_modern.py save.v2 --json

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

APP_NAME = "Victoria II Modern War Analyzer"
APP_VERSION = "1.0.0"
TAG_RE = re.compile(r"^[A-Z][A-Z0-9]{2}$")
CASUALTY_WORDS = (
    "loss",
    "losses",
    "casualt",
    "dead",
    "killed",
    "wounded",
    "attrition",
    "manpower",
    "strength_loss",
)
BATTLE_WORDS = ("battle", "combat", "siege", "occupation", "naval")


class ParseError(RuntimeError):
    """Raised when a save file cannot be parsed."""


@dataclass
class Node:
    """A Clausewitz-style object with duplicate keys and list items preserved."""

    pairs: list[tuple[Optional[str], Any]] = field(default_factory=list)

    def add(self, key: Optional[str], value: Any) -> None:
        self.pairs.append((key, value))

    def values(self, key: str) -> list[Any]:
        return [value for k, value in self.pairs if k == key]

    def first(self, key: str, default: Any = None) -> Any:
        for k, value in self.pairs:
            if k == key:
                return value
        return default

    def list_items(self) -> list[Any]:
        return [value for key, value in self.pairs if key is None]

    def walk(self, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Optional[str], Any]]:
        for key, value in self.pairs:
            next_path = path + ((key or "[]"),)
            yield path, key, value
            if isinstance(value, Node):
                yield from value.walk(next_path)


class ClausewitzParser:
    """Small forgiving parser for Paradox/Clausewitz text saves."""

    def __init__(self, text: str, progress: Optional[Callable[[float, str], None]] = None) -> None:
        self.text = text
        self.length = len(text)
        self.i = 0
        self.progress = progress
        self.last_progress = 0.0

    def parse(self) -> Node:
        root = self._parse_object(until_brace=False)
        self._skip_ws_and_comments()
        if self.i < self.length:
            raise ParseError(f"Unexpected data at byte {self.i:,}")
        return root

    def _parse_object(self, until_brace: bool) -> Node:
        node = Node()
        while True:
            self._skip_ws_and_comments()
            if self.i >= self.length:
                if until_brace:
                    raise ParseError("Unexpected end of file inside '{...}' block")
                return node
            if self.text[self.i] == "}":
                if until_brace:
                    self.i += 1
                    return node
                raise ParseError(f"Unexpected closing brace at byte {self.i:,}")

            token = self._read_token()
            if token == "{":
                node.add(None, self._parse_object(until_brace=True))
                continue

            self._skip_ws_and_comments()
            if self.i < self.length and self.text[self.i] == "=":
                self.i += 1
                value = self._read_value()
                node.add(str(token), value)
            else:
                node.add(None, token)

            if self.progress and self.length:
                now = time.monotonic()
                if now - self.last_progress > 0.20:
                    self.last_progress = now
                    self.progress(min(self.i / self.length, 0.99), "Parsing save file")

    def _read_value(self) -> Any:
        self._skip_ws_and_comments()
        if self.i >= self.length:
            return ""
        if self.text[self.i] == "{":
            self.i += 1
            return self._parse_object(until_brace=True)
        return self._read_token()

    def _skip_ws_and_comments(self) -> None:
        while self.i < self.length:
            ch = self.text[self.i]
            if ch.isspace():
                self.i += 1
                continue
            if ch == "#":
                while self.i < self.length and self.text[self.i] not in "\r\n":
                    self.i += 1
                continue
            break

    def _read_token(self) -> Any:
        self._skip_ws_and_comments()
        if self.i >= self.length:
            return ""
        ch = self.text[self.i]
        if ch in "{}=":
            self.i += 1
            return ch
        if ch == '"':
            return self._read_quoted()
        start = self.i
        while self.i < self.length:
            ch = self.text[self.i]
            if ch.isspace() or ch in "{}=#":
                break
            self.i += 1
        raw = self.text[start : self.i]
        return self._coerce_atom(raw)

    def _read_quoted(self) -> str:
        self.i += 1
        out: list[str] = []
        while self.i < self.length:
            ch = self.text[self.i]
            self.i += 1
            if ch == '"':
                return "".join(out)
            if ch == "\\" and self.i < self.length:
                out.append(self.text[self.i])
                self.i += 1
            else:
                out.append(ch)
        raise ParseError("Unterminated quoted string")

    @staticmethod
    def _coerce_atom(raw: str) -> Any:
        if raw in ("yes", "no"):
            return raw == "yes"
        if re.fullmatch(r"[-+]?\d+", raw):
            try:
                return int(raw)
            except ValueError:
                return raw
        if re.fullmatch(r"[-+]?\d+\.\d+", raw):
            try:
                return float(raw)
            except ValueError:
                return raw
        return raw


@dataclass
class WarSide:
    name: str
    tags: list[str] = field(default_factory=list)
    casualties: float = 0.0
    battles: int = 0


@dataclass
class WarReport:
    name: str
    start_date: str = "Unknown"
    end_date: str = "Active / unknown"
    attackers: WarSide = field(default_factory=lambda: WarSide("Attackers"))
    defenders: WarSide = field(default_factory=lambda: WarSide("Defenders"))
    warscore: Optional[float] = None
    goals: list[str] = field(default_factory=list)
    battles: list[dict[str, Any]] = field(default_factory=list)
    raw_keys: Counter = field(default_factory=Counter)

    @property
    def total_casualties(self) -> float:
        return self.attackers.casualties + self.defenders.casualties

    @property
    def participant_count(self) -> int:
        return len(set(self.attackers.tags + self.defenders.tags))


@dataclass
class SaveReport:
    path: str
    date: str = "Unknown"
    player: str = "Unknown"
    wars: list[WarReport] = field(default_factory=list)
    countries: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def active_wars(self) -> list[WarReport]:
        return [war for war in self.wars if war.end_date == "Active / unknown"]


def read_save_text(path: Path, progress: Optional[Callable[[float, str], None]] = None) -> str:
    if progress:
        progress(0.02, "Reading save file")
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def analyze_save(path: Path, progress: Optional[Callable[[float, str], None]] = None) -> SaveReport:
    text = read_save_text(path, progress)
    parser = ClausewitzParser(text, progress=progress)
    root = parser.parse()
    if progress:
        progress(0.995, "Building war report")
    return build_report(path, root)


def atom_to_string(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return ""
    return str(value)


def extract_tags(value: Any) -> list[str]:
    tags: list[str] = []

    def visit(obj: Any, parent_key: str = "") -> None:
        if isinstance(obj, Node):
            for key, val in obj.pairs:
                if key and TAG_RE.match(key):
                    tags.append(key)
                if key in ("country", "tag", "actor", "receiver", "attacker", "defender") and isinstance(val, str) and TAG_RE.match(val):
                    tags.append(val)
                visit(val, key or parent_key)
        elif isinstance(obj, str) and TAG_RE.match(obj):
            tags.append(obj)

    visit(value)
    return sorted(dict.fromkeys(tags))


def find_first_date(value: Any) -> Optional[str]:
    candidates = ("start_date", "start", "date", "begin", "begin_date")
    if isinstance(value, Node):
        for key in candidates:
            date = value.first(key)
            if is_date(date):
                return str(date)
        for _, child in value.pairs:
            if isinstance(child, Node):
                found = find_first_date(child)
                if found:
                    return found
    return None


def is_date(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", value))


def numeric_sum_by_words(node: Any, words: Iterable[str]) -> float:
    total = 0.0
    word_tuple = tuple(words)
    if not isinstance(node, Node):
        return total
    for key, value in node.pairs:
        key_l = (key or "").lower()
        if isinstance(value, (int, float)) and any(word in key_l for word in word_tuple):
            total += float(value)
        elif isinstance(value, Node):
            total += numeric_sum_by_words(value, word_tuple)
    return total


def count_nodes_by_words(node: Any, words: Iterable[str]) -> int:
    count = 0
    word_tuple = tuple(words)
    if not isinstance(node, Node):
        return 0
    for key, value in node.pairs:
        key_l = (key or "").lower()
        if isinstance(value, Node) and any(word in key_l for word in word_tuple):
            count += 1
        if isinstance(value, Node):
            count += count_nodes_by_words(value, word_tuple)
    return count


def short_node_label(node: Any, limit: int = 80) -> str:
    if not isinstance(node, Node):
        return atom_to_string(node)[:limit]
    parts = []
    for key, value in node.pairs[:6]:
        if key:
            parts.append(f"{key}={atom_to_string(value) if not isinstance(value, Node) else '{...}'}")
        else:
            parts.append(atom_to_string(value))
    label = ", ".join(parts)
    return label[:limit] + ("..." if len(label) > limit else "")


def node_has_war_shape(key: Optional[str], value: Any) -> bool:
    if not isinstance(value, Node):
        return False
    key_l = (key or "").lower()
    child_keys = {k for k, _ in value.pairs if k}
    has_sides = {"attacker", "defender"} <= child_keys or {"attackers", "defenders"} <= child_keys
    if "war" in key_l and "goal" not in key_l and "score" not in key_l:
        return has_sides or "history" in child_keys or "name" in child_keys
    return bool(has_sides and ("name" in child_keys or "history" in child_keys))


def iter_war_nodes(root: Node) -> Iterator[tuple[str, Node]]:
    for path, key, value in root.walk(()) :
        if node_has_war_shape(key, value):
            path_label = "/".join(path + ((key or "war"),))
            yield path_label, value



def extract_side_tags(value: Any) -> list[str]:
    """Extract country tags from a side block without counting opposing battle fields."""
    tags: list[str] = []
    if isinstance(value, Node):
        for key, child in value.pairs:
            key_l = (key or "").lower()
            if key and TAG_RE.match(key):
                tags.append(key)
            if key_l in ("country", "tag", "participant", "ally") and isinstance(child, str) and TAG_RE.match(child):
                tags.append(child)
            if key is None and isinstance(child, str) and TAG_RE.match(child):
                tags.append(child)
        if tags:
            return sorted(dict.fromkeys(tags))
        for key, child in value.pairs:
            key_l = (key or "").lower()
            if any(word in key_l for word in BATTLE_WORDS):
                continue
            if key_l in ("attacker", "defender"):
                continue
            tags.extend(extract_side_tags(child))
    elif isinstance(value, str) and TAG_RE.match(value):
        tags.append(value)
    return sorted(dict.fromkeys(tags))

def side_from_node(name: str, war_node: Node, keys: tuple[str, ...]) -> WarSide:
    tags: list[str] = []
    casualties = 0.0
    battles = 0
    for key in keys:
        for value in war_node.values(key):
            tags.extend(extract_side_tags(value))
            casualties += numeric_sum_by_words(value, CASUALTY_WORDS)
            battles += count_nodes_by_words(value, BATTLE_WORDS)
    return WarSide(name=name, tags=sorted(dict.fromkeys(tags)), casualties=casualties, battles=battles)


def extract_war_battles(war_node: Node) -> list[dict[str, Any]]:
    battles: list[dict[str, Any]] = []
    for path, key, value in war_node.walk(()) :
        key_l = (key or "").lower()
        if isinstance(value, Node) and any(word in key_l for word in BATTLE_WORDS):
            battles.append(
                {
                    "type": key or "battle",
                    "date": find_first_date(value) or "Unknown",
                    "label": short_node_label(value, 120),
                    "casualties": numeric_sum_by_words(value, CASUALTY_WORDS),
                    "tags": extract_tags(value),
                    "path": "/".join(path + ((key or "battle"),)),
                }
            )
    battles.sort(key=lambda item: item.get("date", ""))
    return battles[:200]


def extract_goals(war_node: Node) -> list[str]:
    goals: list[str] = []
    for path, key, value in war_node.walk(()) :
        if key and "goal" in key.lower():
            label = short_node_label(value, 120)
            if label:
                goals.append(label)
    return list(dict.fromkeys(goals))[:30]


def extract_warscore(war_node: Node) -> Optional[float]:
    for key in ("war_score", "warscore", "score"):
        value = war_node.first(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def extract_country_names(root: Node) -> dict[str, str]:
    countries: dict[str, str] = {}
    for key, value in root.pairs:
        if key and TAG_RE.match(key) and isinstance(value, Node):
            name = value.first("name") or value.first("adjective") or key
            countries[key] = atom_to_string(name)
    countries_container = root.first("country") or root.first("countries")
    if isinstance(countries_container, Node):
        for key, value in countries_container.pairs:
            if key and TAG_RE.match(key) and isinstance(value, Node):
                name = value.first("name") or value.first("adjective") or key
                countries[key] = atom_to_string(name)
    return countries


def build_report(path: Path, root: Node) -> SaveReport:
    report = SaveReport(path=str(path), date=atom_to_string(root.first("date", "Unknown")), player=atom_to_string(root.first("player", "Unknown")))
    report.countries = extract_country_names(root)
    seen: set[int] = set()
    for path_label, war_node in iter_war_nodes(root):
        if id(war_node) in seen:
            continue
        seen.add(id(war_node))
        raw_name = war_node.first("name") or war_node.first("war_name") or war_node.first("id") or path_label
        name = atom_to_string(raw_name)
        start = find_first_date(war_node) or "Unknown"
        end = "Active / unknown"
        for end_key in ("end_date", "end", "finish_date"):
            end_value = war_node.first(end_key)
            if is_date(end_value):
                end = str(end_value)
                break
        attackers = side_from_node("Attackers", war_node, ("attacker", "attackers", "original_attacker", "attackers_history"))
        defenders = side_from_node("Defenders", war_node, ("defender", "defenders", "original_defender", "defenders_history"))
        if not attackers.tags:
            attackers.tags = extract_tags(war_node.first("attacker"))
        if not defenders.tags:
            defenders.tags = extract_tags(war_node.first("defender"))
        battles = extract_war_battles(war_node)
        if not attackers.casualties and not defenders.casualties:
            total = numeric_sum_by_words(war_node, CASUALTY_WORDS)
            attackers.casualties = total / 2 if total else 0
            defenders.casualties = total / 2 if total else 0
        war = WarReport(
            name=name,
            start_date=start,
            end_date=end,
            attackers=attackers,
            defenders=defenders,
            warscore=extract_warscore(war_node),
            goals=extract_goals(war_node),
            battles=battles,
            raw_keys=Counter(k or "[]" for k, _ in war_node.pairs),
        )
        report.wars.append(war)
    if not report.wars:
        report.warnings.append("No war blocks were found. The save may be compressed, binary, or from a mod with an unusual structure.")
    report.wars.sort(key=lambda w: (w.end_date != "Active / unknown", w.start_date, w.name))
    return report


def format_int(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def display_tags(tags: list[str], countries: dict[str, str]) -> str:
    if not tags:
        return "Unknown"
    return ", ".join(f"{tag} ({countries.get(tag, tag)})" if countries.get(tag, tag) != tag else tag for tag in tags)


def report_to_text(report: SaveReport) -> str:
    lines = [APP_NAME, "=" * len(APP_NAME), f"Save: {report.path}", f"Date: {report.date}", f"Player: {report.player}", f"Wars found: {len(report.wars)}", ""]
    for warning in report.warnings:
        lines.append(f"Warning: {warning}")
    for index, war in enumerate(report.wars, 1):
        lines.extend(
            [
                f"{index}. {war.name}",
                f"   Dates: {war.start_date} -> {war.end_date}",
                f"   Warscore: {war.warscore if war.warscore is not None else 'Unknown'}",
                f"   Attackers: {display_tags(war.attackers.tags, report.countries)}",
                f"   Defenders: {display_tags(war.defenders.tags, report.countries)}",
                f"   Estimated casualties: {format_int(war.total_casualties)}",
                f"   Battles found: {len(war.battles)}",
            ]
        )
        if war.goals:
            lines.append("   Goals: " + "; ".join(war.goals[:5]))
        lines.append("")
    return "\n".join(lines)


def report_to_json(report: SaveReport) -> str:
    def war_dict(war: WarReport) -> dict[str, Any]:
        return {
            "name": war.name,
            "start_date": war.start_date,
            "end_date": war.end_date,
            "warscore": war.warscore,
            "attackers": {"tags": war.attackers.tags, "casualties": war.attackers.casualties, "battles": war.attackers.battles},
            "defenders": {"tags": war.defenders.tags, "casualties": war.defenders.casualties, "battles": war.defenders.battles},
            "goals": war.goals,
            "battles": war.battles,
        }

    return json.dumps(
        {
            "app": APP_NAME,
            "version": APP_VERSION,
            "path": report.path,
            "date": report.date,
            "player": report.player,
            "warnings": report.warnings,
            "countries": report.countries,
            "wars": [war_dict(war) for war in report.wars],
        },
        indent=2,
        ensure_ascii=False,
    )


def possible_vic2_dirs() -> list[Path]:
    roots = []
    env = os.environ.get("VICTORIA2_PATH") or os.environ.get("VIC2_PATH")
    if env:
        roots.append(Path(env))
    home = Path.home()
    roots.extend(
        [
            home / ".steam/steam/steamapps/common/Victoria 2",
            home / ".local/share/Steam/steamapps/common/Victoria 2",
            Path("/mnt/c/Program Files (x86)/Steam/steamapps/common/Victoria 2"),
            Path("C:/Program Files (x86)/Steam/steamapps/common/Victoria 2"),
            Path("C:/GOG Games/Victoria 2"),
        ]
    )
    return [p for p in roots if p.exists()]


def find_compatible_game_art() -> Optional[Path]:
    names = ("*.png", "*.gif", "*.ppm", "*.pgm")
    for root in possible_vic2_dirs():
        for sub in ("gfx", "interface", "map"):
            folder = root / sub
            if not folder.exists():
                continue
            for pattern in names:
                for candidate in folder.rglob(pattern):
                    if candidate.stat().st_size < 8_000_000:
                        return candidate
    return None


class AnalyzerGUI:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1180x760")
        self.root.minsize(980, 620)
        self.report: Optional[SaveReport] = None
        self.selected_war: Optional[WarReport] = None
        self.game_art: Optional[Any] = None
        self.status = tk.StringVar(value="Open a Victoria II .v2 save to begin.")
        self.progress = tk.DoubleVar(value=0)
        self._configure_style()
        self._build_ui()

    def _configure_style(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background="#18202a")
        style.configure("Panel.TFrame", background="#efe3c6", relief="ridge", borderwidth=1)
        style.configure("TLabel", background="#18202a", foreground="#f4e8ce")
        style.configure("Panel.TLabel", background="#efe3c6", foreground="#1f252c")
        style.configure("Title.TLabel", background="#18202a", foreground="#f7d56b", font=("Georgia", 20, "bold"))
        style.configure("TButton", padding=7)
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10), background="#fff7e6", fieldbackground="#fff7e6")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)

        header = tk.Canvas(shell, height=92, bg="#111820", highlightthickness=0)
        header.pack(fill="x")
        self._draw_header(header)
        self._try_place_game_art(header)
        header.create_text(26, 26, anchor="nw", text="Victoria II", fill="#d7b55d", font=("Georgia", 16, "bold"))
        header.create_text(26, 48, anchor="nw", text="Modern War Analyzer", fill="#fff1cc", font=("Georgia", 25, "bold"))
        header.create_text(570, 52, anchor="w", text="Wars • goals • participants • casualties • battle timeline", fill="#d8e0ea", font=("Segoe UI", 11))

        toolbar = ttk.Frame(shell)
        toolbar.pack(fill="x", padx=14, pady=(10, 6))
        ttk.Button(toolbar, text="Open save…", command=self.open_save).pack(side="left")
        ttk.Button(toolbar, text="Export report…", command=self.export_report).pack(side="left", padx=8)
        ttk.Button(toolbar, text="Game art status", command=self.show_art_status).pack(side="left")
        ttk.Label(toolbar, textvariable=self.status).pack(side="left", padx=16)
        ttk.Progressbar(toolbar, variable=self.progress, maximum=1.0, length=190).pack(side="right", padx=8)

        paned = ttk.PanedWindow(shell, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        left = ttk.Frame(paned, style="Panel.TFrame")
        right = ttk.Frame(paned, style="Panel.TFrame")
        paned.add(left, weight=2)
        paned.add(right, weight=3)

        columns = ("dates", "participants", "casualties", "score")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings")
        self.tree.heading("#0", text="War")
        self.tree.heading("dates", text="Dates")
        self.tree.heading("participants", text="Nations")
        self.tree.heading("casualties", text="Casualties")
        self.tree.heading("score", text="Score")
        self.tree.column("#0", width=260, minwidth=160)
        self.tree.column("dates", width=170, anchor="center")
        self.tree.column("participants", width=80, anchor="center")
        self.tree.column("casualties", width=110, anchor="e")
        self.tree.column("score", width=70, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        self.detail_title = ttk.Label(right, text="No war selected", style="Panel.TLabel", font=("Georgia", 16, "bold"))
        self.detail_title.pack(fill="x", padx=12, pady=(12, 4))
        self.canvas = tk.Canvas(right, height=230, bg="#efe3c6", highlightthickness=0)
        self.canvas.pack(fill="x", padx=12, pady=8)
        self.details = tk.Text(right, wrap="word", bg="#fff7e6", fg="#18202a", relief="flat", font=("Segoe UI", 10), padx=12, pady=12)
        self.details.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.details.insert("end", "Open a save file to see a war dashboard.\n\nTip: set VICTORIA2_PATH to your Victoria II install folder if you want the app to try compatible local game artwork.")
        self.details.configure(state="disabled")

    def _draw_header(self, canvas: Any) -> None:
        width = 1400
        for x in range(0, width, 6):
            shade = int(20 + 24 * (x / width))
            canvas.create_rectangle(x, 0, x + 6, 92, outline="", fill=f"#{shade:02x}{shade+8:02x}{shade+16:02x}")
        for x in range(0, width, 90):
            canvas.create_oval(x - 30, 18, x + 30, 78, outline="#33485d", width=1)
            canvas.create_line(x, 25, x, 73, fill="#263747")
            canvas.create_line(x - 24, 49, x + 24, 49, fill="#263747")

    def _try_place_game_art(self, canvas: Any) -> None:
        art = find_compatible_game_art()
        if not art:
            return
        try:
            self.game_art = self.tk.PhotoImage(file=str(art))
        except Exception:
            self.game_art = None
            return
        width = max(1, self.game_art.width())
        height = max(1, self.game_art.height())
        scale = max(1, math.ceil(max(width / 360, height / 90)))
        if scale > 1:
            self.game_art = self.game_art.subsample(scale, scale)
        canvas.create_image(1160, 46, image=self.game_art, anchor="center")
        canvas.create_rectangle(975, 0, 1400, 92, outline="", fill="#111820", stipple="gray50")

    def open_save(self) -> None:
        from tkinter import filedialog, messagebox

        filename = filedialog.askopenfilename(title="Open Victoria II save", filetypes=[("Victoria II saves", "*.v2 *.eu3"), ("All files", "*")])
        if not filename:
            return
        path = Path(filename)
        self.status.set(f"Loading {path.name}…")
        self.progress.set(0)
        self.tree.delete(*self.tree.get_children())
        self._set_details("Parsing save. Large late-game saves can take a little while…")

        def worker() -> None:
            try:
                report = analyze_save(path, progress=lambda value, msg: self.root.after(0, self._progress, value, msg))
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"Could not analyze save:\n\n{exc}"))
                self.root.after(0, self._progress, 0, "Failed")
                return
            self.root.after(0, self._load_report, report)

        threading.Thread(target=worker, daemon=True).start()

    def _progress(self, value: float, message: str) -> None:
        self.progress.set(value)
        self.status.set(message)

    def _load_report(self, report: SaveReport) -> None:
        self.report = report
        self.selected_war = None
        self.tree.delete(*self.tree.get_children())
        for idx, war in enumerate(report.wars):
            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                text=war.name,
                values=(f"{war.start_date} → {war.end_date}", war.participant_count, format_int(war.total_casualties), war.warscore if war.warscore is not None else "—"),
            )
        self.progress.set(1.0)
        self.status.set(f"Loaded {Path(report.path).name}: {len(report.wars)} wars found")
        self._set_details(report_to_text(report))
        self._draw_overview_chart()

    def on_tree_select(self, _event: Any = None) -> None:
        if not self.report:
            return
        selection = self.tree.selection()
        if not selection:
            return
        war = self.report.wars[int(selection[0])]
        self.selected_war = war
        self.detail_title.configure(text=war.name)
        self._set_details(self._war_details(war))
        self._draw_war_chart(war)

    def _war_details(self, war: WarReport) -> str:
        assert self.report is not None
        lines = [war.name, "-" * len(war.name), f"Dates: {war.start_date} -> {war.end_date}", f"Warscore: {war.warscore if war.warscore is not None else 'Unknown'}", ""]
        lines.append("Attackers")
        lines.append(f"  {display_tags(war.attackers.tags, self.report.countries)}")
        lines.append(f"  Estimated losses: {format_int(war.attackers.casualties)}")
        lines.append("")
        lines.append("Defenders")
        lines.append(f"  {display_tags(war.defenders.tags, self.report.countries)}")
        lines.append(f"  Estimated losses: {format_int(war.defenders.casualties)}")
        lines.append("")
        if war.goals:
            lines.append("War goals")
            lines.extend(f"  • {goal}" for goal in war.goals[:15])
            lines.append("")
        if war.battles:
            lines.append("Detected battles / siege events")
            for battle in war.battles[:60]:
                tags = ", ".join(battle.get("tags") or [])
                lines.append(f"  • {battle['date']} — {battle['type']} — {format_int(battle.get('casualties', 0))} losses" + (f" — {tags}" if tags else ""))
        else:
            lines.append("No battle history nodes were detected for this war. Victoria II save detail varies by patch and mod.")
        return "\n".join(lines)

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        self.details.configure(state="disabled")

    def _draw_overview_chart(self) -> None:
        self.canvas.delete("all")
        if not self.report or not self.report.wars:
            self.canvas.create_text(20, 20, anchor="nw", text="No wars found", fill="#1f252c", font=("Segoe UI", 13, "bold"))
            return
        self.canvas.create_text(16, 14, anchor="nw", text="War casualty overview", fill="#1f252c", font=("Georgia", 14, "bold"))
        values = [max(w.total_casualties, 1) for w in self.report.wars[:10]]
        max_value = max(values)
        y = 52
        for war, value in zip(self.report.wars[:10], values):
            width = int(620 * value / max_value)
            self.canvas.create_rectangle(180, y, 180 + width, y + 16, fill="#7c1f23", outline="#4b1114")
            self.canvas.create_text(16, y + 8, anchor="w", text=war.name[:24], fill="#1f252c", font=("Segoe UI", 9))
            self.canvas.create_text(810, y + 8, anchor="e", text=format_int(war.total_casualties), fill="#1f252c", font=("Segoe UI", 9, "bold"))
            y += 24

    def _draw_war_chart(self, war: WarReport) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(16, 14, anchor="nw", text="Loss estimate by side", fill="#1f252c", font=("Georgia", 14, "bold"))
        total = max(war.total_casualties, 1)
        left = war.attackers.casualties / total
        self.canvas.create_rectangle(70, 72, 770, 132, fill="#315f9f", outline="#1a375f")
        self.canvas.create_rectangle(70 + int(700 * left), 72, 770, 132, fill="#8d2628", outline="#5c1518")
        self.canvas.create_text(78, 102, anchor="w", text=f"Attackers: {format_int(war.attackers.casualties)}", fill="white", font=("Segoe UI", 11, "bold"))
        self.canvas.create_text(762, 102, anchor="e", text=f"Defenders: {format_int(war.defenders.casualties)}", fill="white", font=("Segoe UI", 11, "bold"))
        if war.warscore is not None:
            score = max(-100, min(100, war.warscore))
            x0, x1, y = 180, 660, 182
            self.canvas.create_line(x0, y, x1, y, fill="#1f252c", width=3)
            self.canvas.create_line((x0 + x1) / 2, y - 10, (x0 + x1) / 2, y + 10, fill="#1f252c", width=2)
            sx = x0 + (score + 100) / 200 * (x1 - x0)
            self.canvas.create_oval(sx - 9, y - 9, sx + 9, y + 9, fill="#d7b55d", outline="#5d4b1f", width=2)
            self.canvas.create_text(16, y, anchor="w", text=f"Warscore {war.warscore:g}", fill="#1f252c", font=("Segoe UI", 11, "bold"))

    def export_report(self) -> None:
        from tkinter import filedialog, messagebox

        if not self.report:
            messagebox.showinfo(APP_NAME, "Open a save first.")
            return
        filename = filedialog.asksaveasfilename(title="Export report", defaultextension=".txt", filetypes=[("Text", "*.txt"), ("JSON", "*.json")])
        if not filename:
            return
        path = Path(filename)
        content = report_to_json(self.report) if path.suffix.lower() == ".json" else report_to_text(self.report)
        path.write_text(content, encoding="utf-8")
        self.status.set(f"Exported {path.name}")

    def show_art_status(self) -> None:
        from tkinter import messagebox

        art = find_compatible_game_art()
        if art:
            messagebox.showinfo(APP_NAME, f"Found compatible local game artwork:\n{art}")
        else:
            dirs = "\n".join(str(p) for p in possible_vic2_dirs()) or "No install folder detected."
            messagebox.showinfo(
                APP_NAME,
                "No directly loadable PNG/GIF/PPM Victoria II artwork was found.\n\n"
                "Most game art is DDS/TGA, which this single-file standard-library tool cannot decode. "
                "The analyzer is using its built-in Victoria II-inspired theme instead.\n\n"
                f"Install folders checked:\n{dirs}",
            )

    def run(self) -> None:
        self.root.mainloop()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("save", nargs="?", help="Path to a Victoria II save file. If omitted, launches the GUI.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text report when a save path is supplied.")
    args = parser.parse_args(argv)
    if not args.save:
        try:
            AnalyzerGUI().run()
        except ImportError as exc:
            print(f"Tkinter is not available: {exc}", file=sys.stderr)
            return 2
        return 0
    report = analyze_save(Path(args.save))
    print(report_to_json(report) if args.json else report_to_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
