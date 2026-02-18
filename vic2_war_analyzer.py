#!/usr/bin/env python3
"""
Vic2 War Analyzer
=================
A standalone desktop application that parses Victoria 2 save files and displays
war information (wars, battles, wargoals) in a modern Python/Tkinter GUI.

Notes:
- Victoria 2 save files are Paradox-style key/value documents with nested braces.
- Some saves may be gzipped; this app auto-detects gzip via magic bytes.
- Country names are resolved from localisation CSV files if a game directory is set.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk
except Exception:  # Pillow is optional; app still works without flags.
    Image = None
    ImageTk = None


APP_NAME = "Vic2 War Analyzer"
CONFIG_PATH = Path.home() / ".vic2_war_analyzer.json"


# ---------------------------- Data Models ---------------------------- #


@dataclass
class Battle:
    date: str = ""
    location: str = ""
    attacker: str = ""
    defender: str = ""
    attacker_leader: str = ""
    defender_leader: str = ""
    attacker_losses: int = 0
    defender_losses: int = 0
    winner: str = ""


@dataclass
class Wargoal:
    added_by: str = ""
    target: str = ""
    war_goal_type: str = ""
    status: str = "unknown"


@dataclass
class War:
    key: str = ""
    name: str = "Unnamed War"
    start_date: str = ""
    end_date: str = ""
    attackers: List[str] = field(default_factory=list)
    defenders: List[str] = field(default_factory=list)
    attacker_losses: int = 0
    defender_losses: int = 0
    outcome: str = "unknown"
    participants: List[str] = field(default_factory=list)
    battles: List[Battle] = field(default_factory=list)
    wargoals: List[Wargoal] = field(default_factory=list)

    @property
    def total_losses(self) -> int:
        return self.attacker_losses + self.defender_losses


# ---------------------------- Parser ---------------------------- #


_TOKEN_RE = re.compile(
    r'\s*(?:'
    r'(?P<brace>[{}=])|'
    r'(?P<quoted>"(?:\\.|[^"])*")|'
    r'(?P<atom>[^\s{}="]+)'
    r')',
    re.DOTALL,
)


def _decode_text(path: Path) -> str:
    """Load text from plain or gzipped save file."""
    with path.open("rb") as f:
        head = f.read(2)
        f.seek(0)
        raw = gzip.decompress(f.read()) if head == b"\x1f\x8b" else f.read()

    # Victoria 2 saves are usually UTF-8 or cp1252. Try UTF-8 first.
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


class ParadoxParser:
    """A lightweight parser for Paradox-style nested key/value data."""

    def __init__(self, text: str):
        self.tokens = self._tokenize(text)
        self.index = 0

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        tokens: List[str] = []
        for m in _TOKEN_RE.finditer(text):
            if m.group("brace"):
                tokens.append(m.group("brace"))
            elif m.group("quoted"):
                tokens.append(m.group("quoted"))
            elif m.group("atom"):
                tokens.append(m.group("atom"))
        return tokens

    def _peek(self) -> Optional[str]:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _next(self) -> Optional[str]:
        t = self._peek()
        if t is not None:
            self.index += 1
        return t

    @staticmethod
    def _atom_value(tok: str) -> Any:
        if tok is None:
            return None
        if tok.startswith('"') and tok.endswith('"'):
            return tok[1:-1].replace('\\"', '"')
        if re.fullmatch(r"-?\d+", tok):
            try:
                return int(tok)
            except ValueError:
                return tok
        if re.fullmatch(r"-?\d+\.\d+", tok):
            try:
                return float(tok)
            except ValueError:
                return tok
        return tok

    def parse(self) -> Dict[str, Any]:
        root: Dict[str, Any] = {}
        while self._peek() is not None:
            key = self._next()
            if key in ("{", "}", "="):
                continue
            if self._peek() == "=":
                self._next()  # consume '='
                value = self._parse_value()
                self._assign(root, str(key), value)
        return root

    def _parse_value(self) -> Any:
        tok = self._next()
        if tok == "{":
            return self._parse_block()
        return self._atom_value(tok)

    def _parse_block(self) -> Any:
        # Blocks can be list-like (anonymous values) or dict-like (key = value)
        items: List[Any] = []
        obj: Dict[str, Any] = {}
        is_dict = False

        while True:
            tok = self._peek()
            if tok is None:
                break
            if tok == "}":
                self._next()  # consume
                break

            key_or_val = self._next()
            if self._peek() == "=":
                is_dict = True
                self._next()
                value = self._parse_value()
                self._assign(obj, str(key_or_val), value)
            else:
                items.append(self._atom_value(str(key_or_val)))

        if is_dict and items:
            # Mixed blocks are rare. Keep anonymous values in a special key.
            obj["__items__"] = items
            return obj
        return obj if is_dict else items

    @staticmethod
    def _assign(obj: Dict[str, Any], key: str, value: Any) -> None:
        if key in obj:
            if not isinstance(obj[key], list):
                obj[key] = [obj[key]]
            obj[key].append(value)
        else:
            obj[key] = value


# ---------------------------- Extraction ---------------------------- #


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _extract_country_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, (str, int, float))]
    if isinstance(value, dict):
        out: List[str] = []
        for k in ("__items__", "members", "countries", "participants"):
            out.extend([str(x) for x in _as_list(value.get(k))])
        if not out:
            for v in value.values():
                if isinstance(v, (str, int, float)):
                    out.append(str(v))
        return out
    if value is not None:
        return [str(value)]
    return []


def _safe_int(v: Any) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return 0
    return 0


def _normalize_date(value: Any) -> str:
    """Normalize save date values into a readable `YYYY.M.D` string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [str(x) for x in value if x is not None]
        return ".".join(parts)
    if isinstance(value, dict):
        # Some structures store dates as year/month/day blocks.
        y = value.get("year")
        m = value.get("month")
        d = value.get("day")
        if y is not None and m is not None and d is not None:
            return f"{y}.{m}.{d}"
        # Other blocks keep anonymous values in __items__.
        if "__items__" in value:
            return _normalize_date(value.get("__items__"))
    return str(value)


def _sum_numeric_fields(node: Any, include_pattern: str) -> int:
    """Recursively sum numeric fields whose keys match include_pattern."""
    total = 0
    pat = re.compile(include_pattern, re.IGNORECASE)

    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                total += _sum_numeric_fields(v, include_pattern)
                continue
            if pat.search(str(k)):
                total += _safe_int(v)
    elif isinstance(node, list):
        for item in node:
            total += _sum_numeric_fields(item, include_pattern)
    return total


def _first_non_empty(node: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        val = node.get(k)
        if val not in (None, "", [], {}):
            return val
    return None


def _format_war_name(node: Dict[str, Any], attackers: List[str], defenders: List[str], index: int) -> str:
    name = _first_non_empty(node, "name", "war_name", "localized_name", "cb_name")
    if isinstance(name, str) and name.strip() and not re.fullmatch(r"war\s*\d+", name.strip(), re.IGNORECASE):
        return name

    # Build a descriptive fallback name from sides and first wargoal if available.
    wg_type = ""
    for src in _as_list(_first_non_empty(node, "wargoals", "casus_belli")):
        if isinstance(src, dict):
            if "type" in src:
                wg_type = str(src.get("type") or "")
                break
            for sv in src.values():
                if isinstance(sv, dict) and sv.get("type"):
                    wg_type = str(sv.get("type") or "")
                    break
        if wg_type:
            break

    left = attackers[0] if attackers else "Unknown"
    right = defenders[0] if defenders else "Unknown"
    if wg_type:
        return f"{left} vs {right} ({wg_type})"
    return f"{left} vs {right}"


def _find_war_candidates(node: Any, parent_key: str = "") -> List[Tuple[str, Dict[str, Any]]]:
    """Recursively collect objects that look like wars."""
    wars: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(node, dict):
        keys = set(node.keys())
        attackers_present = "attackers" in keys or "original_attacker" in keys
        defenders_present = "defenders" in keys or "original_defender" in keys
        war_context = (
            "war" in parent_key.lower()
            or "wargoals" in keys
            or "casus_belli" in keys
            or "battle" in keys
            or "is_great_war" in keys
            or "war_exhaustion" in keys
        )
        looks_like_war = bool(attackers_present and defenders_present and war_context)
        if looks_like_war:
            wars.append((parent_key, node))

        for k, v in node.items():
            if isinstance(v, dict):
                wars.extend(_find_war_candidates(v, k))
            elif isinstance(v, list):
                for item in v:
                    wars.extend(_find_war_candidates(item, k))
    elif isinstance(node, list):
        for item in node:
            wars.extend(_find_war_candidates(item, parent_key))
    return wars


def _parse_battle(node: Dict[str, Any]) -> Battle:
    attacker_losses = _safe_int(
        _first_non_empty(node, "attacker_losses", "losses_attacker", "attackers_losses", "attacker_casualties")
    )
    defender_losses = _safe_int(
        _first_non_empty(node, "defender_losses", "losses_defender", "defenders_losses", "defender_casualties")
    )
    if attacker_losses == 0:
        attacker_losses = _sum_numeric_fields(node.get("attacker", {}), r"(loss|casualt|dead|killed)")
    if defender_losses == 0:
        defender_losses = _sum_numeric_fields(node.get("defender", {}), r"(loss|casualt|dead|killed)")

    return Battle(
        date=_normalize_date(_first_non_empty(node, "date", "battle_date", "start_date")),
        location=str(node.get("location", node.get("province", ""))),
        attacker=str(node.get("attacker", node.get("attacking_country", ""))),
        defender=str(node.get("defender", node.get("defending_country", ""))),
        attacker_leader=str(node.get("attacker_leader", node.get("attacking_leader", ""))),
        defender_leader=str(node.get("defender_leader", node.get("defending_leader", ""))),
        attacker_losses=attacker_losses,
        defender_losses=defender_losses,
        winner=str(node.get("winner", "")),
    )


def _parse_wargoal(node: Dict[str, Any]) -> Wargoal:
    achieved = node.get("fulfilled", node.get("achieved", None))
    if isinstance(achieved, str):
        achieved_status = achieved
    elif achieved is None:
        achieved_status = "unknown"
    else:
        achieved_status = "achieved" if bool(achieved) else "failed"

    return Wargoal(
        added_by=str(node.get("added_by", node.get("actor", ""))),
        target=str(node.get("target", node.get("receiver", ""))),
        war_goal_type=str(node.get("type", node.get("wargoal", ""))),
        status=achieved_status,
    )


def extract_wars(root: Dict[str, Any]) -> List[War]:
    candidates = _find_war_candidates(root)
    unique_ids = set()
    wars: List[War] = []

    for i, (parent_key, node) in enumerate(candidates, start=1):
        if not isinstance(node, dict):
            continue
        uid = id(node)
        if uid in unique_ids:
            continue
        unique_ids.add(uid)

        attackers = _extract_country_list(_first_non_empty(node, "attackers", "original_attacker", "attacker"))
        defenders = _extract_country_list(_first_non_empty(node, "defenders", "original_defender", "defender"))
        if not attackers or not defenders:
            continue

        battles: List[Battle] = []
        for b in _as_list(_first_non_empty(node, "battles", "combat", "engagements")):
            if isinstance(b, dict):
                # battles can be dict of battle_id -> battle_data
                sub_values = list(b.values()) if any(isinstance(v, dict) for v in b.values()) else [b]
                for item in sub_values:
                    if isinstance(item, dict):
                        battles.append(_parse_battle(item))

        wargoals: List[Wargoal] = []
        for w in _as_list(_first_non_empty(node, "wargoals", "casus_belli", "goals")):
            if isinstance(w, dict):
                sub_values = list(w.values()) if any(isinstance(v, dict) for v in w.values()) else [w]
                for item in sub_values:
                    if isinstance(item, dict):
                        wargoals.append(_parse_wargoal(item))

        attacker_losses = _safe_int(
            _first_non_empty(node, "attacker_losses", "losses_attacker", "attackers_losses", "total_attacker_losses")
        )
        defender_losses = _safe_int(
            _first_non_empty(node, "defender_losses", "losses_defender", "defenders_losses", "total_defender_losses")
        )

        # If not directly present, estimate losses from battles.
        if attacker_losses == 0 and battles:
            attacker_losses = sum(b.attacker_losses for b in battles)
        if defender_losses == 0 and battles:
            defender_losses = sum(b.defender_losses for b in battles)
        if attacker_losses == 0:
            attacker_losses = _sum_numeric_fields(node, r"attacker.*(loss|casualt|dead|killed)|losses_attacker")
        if defender_losses == 0:
            defender_losses = _sum_numeric_fields(node, r"defender.*(loss|casualt|dead|killed)|losses_defender")

        participants = sorted(set(attackers + defenders + _extract_country_list(node.get("participants"))))

        war = War(
            key=f"{parent_key or 'war'}_{i}",
            name=_format_war_name(node, attackers, defenders, i),
            start_date=_normalize_date(_first_non_empty(node, "start_date", "date", "war_start_date", "begin_date")),
            end_date=_normalize_date(_first_non_empty(node, "end_date", "peace_date", "war_end_date", "last_action_date")),
            attackers=attackers,
            defenders=defenders,
            attacker_losses=attacker_losses,
            defender_losses=defender_losses,
            outcome=str(node.get("outcome", node.get("result", "unknown"))),
            participants=participants,
            battles=battles,
            wargoals=wargoals,
        )

        if war.start_date in ("", "0"):
            for battle in war.battles:
                if battle.date:
                    war.start_date = battle.date
                    break

        wars.append(war)

    return wars


# ---------------------------- Localization & Flags ---------------------------- #


class CountryResolver:
    def __init__(self) -> None:
        self.tag_to_name: Dict[str, str] = {}

    def load_from_game_dir(self, game_dir: Path) -> None:
        self.tag_to_name.clear()
        loc_dir = game_dir / "localisation"
        if not loc_dir.exists():
            return

        for csv_file in loc_dir.glob("*.csv"):
            try:
                with csv_file.open("r", encoding="cp1252", errors="ignore") as f:
                    for line in f:
                        # Typical format: TAG;English;French;German;Spanish;...
                        if line.startswith("#") or ";" not in line:
                            continue
                        parts = line.rstrip("\n").split(";")
                        if len(parts) >= 2:
                            key = parts[0].strip()
                            val = parts[1].strip()
                            if re.fullmatch(r"[A-Z0-9_]{3,}", key) and val:
                                self.tag_to_name[key] = val
            except OSError:
                continue

    def resolve(self, tag: str) -> str:
        tag = str(tag)
        return self.tag_to_name.get(tag, tag)


class FlagCache:
    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self.game_dir: Optional[Path] = None

    def set_game_dir(self, game_dir: Optional[Path]) -> None:
        self.game_dir = game_dir
        self._cache.clear()

    def get_flag(self, tag: str, size: Tuple[int, int] = (24, 16)) -> Optional[Any]:
        if Image is None or ImageTk is None or self.game_dir is None:
            return None
        key = f"{tag}_{size[0]}x{size[1]}"
        if key in self._cache:
            return self._cache[key]

        flags_dir = self.game_dir / "gfx" / "flags"
        for ext in (".png", ".tga", ".bmp", ".jpg", ".jpeg"):
            p = flags_dir / f"{tag}{ext}"
            if p.exists():
                try:
                    img = Image.open(p).convert("RGBA")
                    img = img.resize(size, Image.LANCZOS)
                    tkimg = ImageTk.PhotoImage(img)
                    self._cache[key] = tkimg
                    return tkimg
                except Exception:
                    return None
        return None


# ---------------------------- GUI ---------------------------- #


class Vic2WarAnalyzerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1400x820")

        self.config_data = self._load_config()
        self.save_path: Optional[Path] = None
        self.game_dir: Optional[Path] = None
        self.wars: List[War] = []
        self.filtered_wars: List[War] = []

        self.country_resolver = CountryResolver()
        self.flag_cache = FlagCache()

        self._build_ui()
        self._restore_config_values()

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=8)

        ttk.Button(top, text="Open Save (.v2)", command=self.on_open_save).pack(side=tk.LEFT)
        ttk.Button(top, text="Set Game Directory", command=self.on_set_game_dir).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Export CSV", command=self.on_export_csv).pack(side=tk.LEFT)

        self.path_var = tk.StringVar(value="No save loaded")
        ttk.Label(top, textvariable=self.path_var).pack(side=tk.LEFT, padx=10)

        search_frame = ttk.Frame(self)
        search_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Label(search_frame, text="Filter wars:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.apply_filters())
        ttk.Entry(search_frame, textvariable=self.search_var, width=40).pack(side=tk.LEFT, padx=6)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.tab_all_wars = ttk.Frame(self.notebook)
        self.tab_war_details = ttk.Frame(self.notebook)
        self.tab_battles = ttk.Frame(self.notebook)
        self.tab_wargoals = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_all_wars, text="All Wars")
        self.notebook.add(self.tab_war_details, text="War Details")
        self.notebook.add(self.tab_battles, text="Battles")
        self.notebook.add(self.tab_wargoals, text="Wargoals")

        self._build_all_wars_tab()
        self._build_war_details_tab()
        self._build_battles_tab()
        self._build_wargoals_tab()

    def _build_all_wars_tab(self) -> None:
        columns = (
            "name",
            "start",
            "end",
            "attackers",
            "defenders",
            "losses",
            "outcome",
            "battles",
        )
        self.wars_tree = ttk.Treeview(self.tab_all_wars, columns=columns, show="headings", height=25)
        for c, w in {
            "name": 250,
            "start": 100,
            "end": 100,
            "attackers": 240,
            "defenders": 240,
            "losses": 100,
            "outcome": 120,
            "battles": 80,
        }.items():
            self.wars_tree.heading(c, text=c.title())
            self.wars_tree.column(c, width=w, anchor=tk.W)

        self.wars_tree.pack(fill=tk.BOTH, expand=True)
        self.wars_tree.bind("<<TreeviewSelect>>", self.on_war_selected)

    def _build_war_details_tab(self) -> None:
        self.details_text = tk.Text(self.tab_war_details, wrap=tk.WORD, state=tk.DISABLED)
        self.details_text.pack(fill=tk.BOTH, expand=True)

    def _build_battles_tab(self) -> None:
        wrap = ttk.Frame(self.tab_battles)
        wrap.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(wrap)
        top.pack(fill=tk.X, pady=4)
        ttk.Label(top, text="Filter battles:").pack(side=tk.LEFT)
        self.battle_filter_var = tk.StringVar()
        self.battle_filter_var.trace_add("write", lambda *_: self.refresh_battles_tab())
        ttk.Entry(top, textvariable=self.battle_filter_var, width=40).pack(side=tk.LEFT, padx=6)

        columns = (
            "date",
            "location",
            "attacker",
            "defender",
            "attacker_losses",
            "defender_losses",
            "winner",
        )
        self.battles_tree = ttk.Treeview(wrap, columns=columns, show="headings", height=25)
        for c, w in {
            "date": 100,
            "location": 180,
            "attacker": 180,
            "defender": 180,
            "attacker_losses": 120,
            "defender_losses": 120,
            "winner": 120,
        }.items():
            self.battles_tree.heading(c, text=c.replace("_", " ").title())
            self.battles_tree.column(c, width=w, anchor=tk.W)

        self.battles_tree.pack(fill=tk.BOTH, expand=True)

    def _build_wargoals_tab(self) -> None:
        columns = ("added_by", "target", "type", "status")
        self.wargoals_tree = ttk.Treeview(self.tab_wargoals, columns=columns, show="headings", height=25)
        for c, w in {"added_by": 220, "target": 220, "type": 280, "status": 140}.items():
            self.wargoals_tree.heading(c, text=c.replace("_", " ").title())
            self.wargoals_tree.column(c, width=w, anchor=tk.W)
        self.wargoals_tree.pack(fill=tk.BOTH, expand=True)

    def _restore_config_values(self) -> None:
        save = self.config_data.get("last_save")
        if save:
            self.path_var.set(f"Last save: {save}")

        game_dir = self.config_data.get("game_dir")
        if game_dir:
            p = Path(game_dir)
            if p.exists():
                self.game_dir = p
                self.country_resolver.load_from_game_dir(p)
                self.flag_cache.set_game_dir(p)

    def _load_config(self) -> Dict[str, Any]:
        if CONFIG_PATH.exists():
            try:
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self.config_data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def on_open_save(self) -> None:
        initial_dir = self.config_data.get("last_save_dir") or str(Path.home())
        file_path = filedialog.askopenfilename(
            title="Select Victoria 2 Save",
            initialdir=initial_dir,
            filetypes=[("Victoria 2 Save", "*.v2"), ("All Files", "*.*")],
        )
        if not file_path:
            return

        try:
            self.load_save(Path(file_path))
            self.config_data["last_save"] = file_path
            self.config_data["last_save_dir"] = str(Path(file_path).parent)
            self._save_config()
        except Exception as exc:
            traceback.print_exc()
            messagebox.showerror(
                APP_NAME,
                f"Failed to load save file.\n\n{exc}\n\n"
                f"Tip: Ensure the save is unencrypted text (not binary) and readable.",
            )

    def on_set_game_dir(self) -> None:
        initial = self.config_data.get("game_dir") or str(Path.home())
        d = filedialog.askdirectory(title="Select Victoria 2 Install Directory", initialdir=initial)
        if not d:
            return
        p = Path(d)
        self.game_dir = p
        self.country_resolver.load_from_game_dir(p)
        self.flag_cache.set_game_dir(p)
        self.config_data["game_dir"] = str(p)
        self._save_config()
        messagebox.showinfo(APP_NAME, "Game directory set. Country names/flags will update where available.")
        self.refresh_all_views()

    def on_export_csv(self) -> None:
        if not self.filtered_wars:
            messagebox.showwarning(APP_NAME, "No wars available to export.")
            return

        dest = filedialog.asksaveasfilename(
            title="Export War Data to CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not dest:
            return

        try:
            with open(dest, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "Name",
                        "Start Date",
                        "End Date",
                        "Attackers",
                        "Defenders",
                        "Attacker Losses",
                        "Defender Losses",
                        "Total Losses",
                        "Outcome",
                        "Battle Count",
                        "Wargoal Count",
                    ]
                )
                for war in self.filtered_wars:
                    writer.writerow(
                        [
                            war.name,
                            war.start_date,
                            war.end_date,
                            ", ".join(self._resolve_many(war.attackers)),
                            ", ".join(self._resolve_many(war.defenders)),
                            war.attacker_losses,
                            war.defender_losses,
                            war.total_losses,
                            war.outcome,
                            len(war.battles),
                            len(war.wargoals),
                        ]
                    )
            messagebox.showinfo(APP_NAME, f"Export complete:\n{dest}")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Failed to export CSV:\n{exc}")

    def load_save(self, path: Path) -> None:
        text = _decode_text(path)
        parser = ParadoxParser(text)
        root = parser.parse()
        wars = extract_wars(root)

        self.save_path = path
        self.wars = wars
        self.path_var.set(f"Loaded: {path}")
        self.apply_filters()

    def apply_filters(self) -> None:
        q = self.search_var.get().strip().lower()
        if not q:
            self.filtered_wars = list(self.wars)
        else:
            out: List[War] = []
            for w in self.wars:
                hay = " | ".join(
                    [
                        w.name,
                        w.start_date,
                        w.end_date,
                        " ".join(w.attackers),
                        " ".join(w.defenders),
                        w.outcome,
                    ]
                ).lower()
                if q in hay:
                    out.append(w)
            self.filtered_wars = out

        self.refresh_all_wars_tab()
        self.clear_secondary_tabs()

    def refresh_all_views(self) -> None:
        self.refresh_all_wars_tab()
        self.refresh_details_tab(None)
        self.refresh_battles_tab()
        self.refresh_wargoals_tab(None)

    def clear_secondary_tabs(self) -> None:
        self.refresh_details_tab(None)
        self.refresh_battles_tab()
        self.refresh_wargoals_tab(None)

    def refresh_all_wars_tab(self) -> None:
        self.wars_tree.delete(*self.wars_tree.get_children())
        for idx, war in enumerate(self.filtered_wars):
            attackers = ", ".join(self._resolve_many(war.attackers))
            defenders = ", ".join(self._resolve_many(war.defenders))
            self.wars_tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    war.name,
                    war.start_date,
                    war.end_date,
                    attackers,
                    defenders,
                    war.total_losses,
                    war.outcome,
                    len(war.battles),
                ),
            )

    def get_selected_war(self) -> Optional[War]:
        sel = self.wars_tree.selection()
        if not sel:
            return None
        try:
            idx = int(sel[0])
            return self.filtered_wars[idx]
        except Exception:
            return None

    def on_war_selected(self, _event: Any = None) -> None:
        war = self.get_selected_war()
        self.refresh_details_tab(war)
        self.refresh_battles_tab(war)
        self.refresh_wargoals_tab(war)

    def _resolve_many(self, tags: Iterable[str]) -> List[str]:
        return [self.country_resolver.resolve(t) for t in tags]

    def refresh_details_tab(self, war: Optional[War]) -> None:
        self.details_text.configure(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        if war is None:
            self.details_text.insert(tk.END, "Select a war in 'All Wars' to see details.")
        else:
            attackers = ", ".join(self._resolve_many(war.attackers)) or "-"
            defenders = ", ".join(self._resolve_many(war.defenders)) or "-"
            participants = ", ".join(self._resolve_many(war.participants)) or "-"

            summary = (
                f"Name: {war.name}\n"
                f"Start Date: {war.start_date or '-'}\n"
                f"End Date: {war.end_date or '-'}\n"
                f"Outcome: {war.outcome}\n\n"
                f"Attackers: {attackers}\n"
                f"Defenders: {defenders}\n"
                f"Participants: {participants}\n\n"
                f"Attacker Losses: {war.attacker_losses}\n"
                f"Defender Losses: {war.defender_losses}\n"
                f"Total Losses: {war.total_losses}\n"
                f"Battles: {len(war.battles)}\n"
                f"Wargoals: {len(war.wargoals)}\n"
            )
            self.details_text.insert(tk.END, summary)
        self.details_text.configure(state=tk.DISABLED)

    def refresh_battles_tab(self, war: Optional[War] = None) -> None:
        if war is None:
            war = self.get_selected_war()

        self.battles_tree.delete(*self.battles_tree.get_children())
        if war is None:
            return

        q = self.battle_filter_var.get().strip().lower()
        for i, b in enumerate(war.battles):
            attacker = self.country_resolver.resolve(b.attacker)
            defender = self.country_resolver.resolve(b.defender)
            winner = self.country_resolver.resolve(b.winner)
            row = [b.date, b.location, attacker, defender, b.attacker_losses, b.defender_losses, winner]
            if q and q not in " | ".join(map(str, row)).lower():
                continue
            self.battles_tree.insert("", tk.END, iid=str(i), values=row)

    def refresh_wargoals_tab(self, war: Optional[War]) -> None:
        self.wargoals_tree.delete(*self.wargoals_tree.get_children())
        if war is None:
            return
        for i, wg in enumerate(war.wargoals):
            self.wargoals_tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(
                    self.country_resolver.resolve(wg.added_by),
                    self.country_resolver.resolve(wg.target),
                    wg.war_goal_type,
                    wg.status,
                ),
            )


def main() -> int:
    app = Vic2WarAnalyzerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
