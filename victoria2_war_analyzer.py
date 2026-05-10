#!/usr/bin/env python3
"""Modern Victoria II War Analyzer.

A single-file, batteries-included analyzer for Victoria II plaintext saves.  It
provides both a modern Tkinter dashboard and a JSON/terminal export mode.
Game artwork is not bundled; instead the app can read flags/backgrounds from a
local Victoria II install or mod directory when you point it at one.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

APP_TITLE = "Victoria II War Analyzer"
COUNTRY_TAG = re.compile(r"^[A-Z0-9]{3}$")
TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|[{}=]|[^\s{}=]+')
DATE_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")


# ----------------------------- save parsing -----------------------------
class SaveParseError(RuntimeError):
    """Raised when a Victoria II save cannot be parsed as plaintext."""


def strip_comments(text: str) -> str:
    """Remove Victoria-style # comments while preserving quoted strings."""
    out: List[str] = []
    in_quote = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = False
        else:
            if ch == '"':
                in_quote = True
                out.append(ch)
            elif ch == "#":
                while i < len(text) and text[i] not in "\r\n":
                    i += 1
                continue
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def atom(token: str) -> Any:
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return bytes(token[1:-1], "utf-8").decode("unicode_escape")
    if token in ("yes", "no"):
        return token == "yes"
    if re.fullmatch(r"-?\d+", token):
        try:
            return int(token)
        except ValueError:
            return token
    if re.fullmatch(r"-?\d+\.\d+", token):
        try:
            return float(token)
        except ValueError:
            return token
    return token


class ClausewitzParser:
    """Small permissive parser for Paradox/Clausewitz key-value saves."""

    def __init__(self, text: str) -> None:
        self.tokens = TOKEN_RE.findall(strip_comments(text))
        self.i = 0

    def peek(self) -> Optional[str]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def pop(self) -> str:
        if self.i >= len(self.tokens):
            raise SaveParseError("Unexpected end of file")
        token = self.tokens[self.i]
        self.i += 1
        return token

    def parse(self) -> Dict[str, Any]:
        root: Dict[str, Any] = {}
        while self.peek() is not None:
            key = self.pop()
            if key in "{}=":
                continue
            if self.peek() == "=":
                self.pop()
                self.add(root, key, self.parse_value())
            else:
                self.add(root, key, atom(key))
        return root

    def parse_value(self) -> Any:
        if self.peek() == "{":
            return self.parse_block()
        return atom(self.pop())

    def parse_block(self) -> Any:
        self.pop()  # {
        pairs: Dict[str, Any] = {}
        values: List[Any] = []
        saw_pair = False
        while self.peek() is not None and self.peek() != "}":
            token = self.pop()
            if self.peek() == "=":
                self.pop()
                self.add(pairs, token, self.parse_value())
                saw_pair = True
            else:
                values.append(atom(token))
        if self.peek() != "}":
            raise SaveParseError("Unclosed brace in save file")
        self.pop()
        if saw_pair and values:
            pairs.setdefault("_values", []).extend(values)
            return pairs
        if saw_pair:
            return pairs
        return values

    @staticmethod
    def add(target: Dict[str, Any], key: str, value: Any) -> None:
        if key in target:
            if not isinstance(target[key], list) or (
                target[key] and isinstance(target[key][0], tuple)
            ):
                target[key] = [target[key]]
            target[key].append(value)
        else:
            target[key] = value


def read_save(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise SaveParseError(
            "This appears to be a binary/compressed save. Re-save Victoria II as plaintext first."
        )
    text = raw.decode("utf-8-sig", errors="replace")
    return ClausewitzParser(text).parse()


# ----------------------------- war model -----------------------------
@dataclass
class Country:
    tag: str
    name: str = ""
    prestige: float = 0.0
    military_score: float = 0.0
    industrial_score: float = 0.0
    total_score: float = 0.0
    manpower: float = 0.0
    badboy: float = 0.0


@dataclass
class Battle:
    name: str
    date: str = ""
    attacker: str = ""
    defender: str = ""
    attacker_losses: int = 0
    defender_losses: int = 0
    winner: str = ""

    @property
    def total_losses(self) -> int:
        return self.attacker_losses + self.defender_losses


@dataclass
class War:
    name: str
    start_date: str = ""
    attackers: List[str] = field(default_factory=list)
    defenders: List[str] = field(default_factory=list)
    battles: List[Battle] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def casualties(self) -> int:
        return sum(b.total_losses for b in self.battles)

    @property
    def duration_days(self) -> Optional[int]:
        start = parse_date(self.start_date)
        if not start:
            return None
        return (date.today() - start).days

    def side_losses(self) -> Tuple[int, int]:
        a = d = 0
        attackers = set(self.attackers)
        defenders = set(self.defenders)
        for battle in self.battles:
            if battle.attacker in attackers or battle.defender in defenders:
                a += battle.attacker_losses
                d += battle.defender_losses
            elif battle.attacker in defenders or battle.defender in attackers:
                d += battle.attacker_losses
                a += battle.defender_losses
            else:
                a += battle.attacker_losses
                d += battle.defender_losses
        return a, d


@dataclass
class Analysis:
    date: str = "Unknown"
    player: str = ""
    wars: List[War] = field(default_factory=list)
    countries: Dict[str, Country] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def listify(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    m = DATE_RE.search(value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def as_tag_list(value: Any) -> List[str]:
    tags: List[str] = []
    if isinstance(value, str) and COUNTRY_TAG.match(value):
        tags.append(value)
    elif isinstance(value, list):
        for item in value:
            tags.extend(as_tag_list(item))
    elif isinstance(value, dict):
        for key in ("country", "tag", "first", "second", "_values"):
            tags.extend(as_tag_list(value.get(key)))
        for key in value.keys():
            if COUNTRY_TAG.match(str(key)):
                tags.append(str(key))
    return sorted(set(tags))


def get_number(data: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        val = data.get(key)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, list):
            nums = [v for v in val if isinstance(v, (int, float))]
            if nums:
                return float(nums[-1])
    return 0.0


def extract_countries(root: Dict[str, Any]) -> Dict[str, Country]:
    countries: Dict[str, Country] = {}
    for key, val in root.items():
        if COUNTRY_TAG.match(str(key)) and isinstance(val, dict):
            countries[key] = Country(
                tag=key,
                name=str(val.get("name", key)),
                prestige=get_number(val, "prestige"),
                military_score=get_number(val, "military_score", "military"),
                industrial_score=get_number(val, "industrial_score", "industrial"),
                total_score=get_number(val, "score", "total_score"),
                manpower=get_number(val, "manpower"),
                badboy=get_number(val, "badboy", "infamy"),
            )
    # Some saves keep countries under a countries={ TAG={...} } object.
    nested = root.get("countries")
    if isinstance(nested, dict):
        for key, val in nested.items():
            if COUNTRY_TAG.match(str(key)) and isinstance(val, dict):
                countries.setdefault(
                    key,
                    Country(
                        tag=key,
                        name=str(val.get("name", key)),
                        prestige=get_number(val, "prestige"),
                        military_score=get_number(val, "military_score", "military"),
                        industrial_score=get_number(val, "industrial_score", "industrial"),
                        total_score=get_number(val, "score", "total_score"),
                        manpower=get_number(val, "manpower"),
                        badboy=get_number(val, "badboy", "infamy"),
                    ),
                )
    return countries


def extract_battle_loss(data: Dict[str, Any], side: str) -> int:
    direct_keys = [f"{side}_losses", f"{side}_casualties", f"losses_{side}"]
    for key in direct_keys:
        n = get_number(data, key)
        if n:
            return int(n)
    side_data = data.get(side)
    if isinstance(side_data, dict):
        return int(get_number(side_data, "losses", "casualties", "dead"))
    return 0


def extract_battles(war_data: Dict[str, Any]) -> List[Battle]:
    battles: List[Battle] = []
    containers: List[Any] = []
    for key in ("battle", "battles", "combat"):
        containers.extend(listify(war_data.get(key)))
    for idx, item in enumerate(containers, 1):
        if not isinstance(item, dict):
            continue
        attackers = as_tag_list(item.get("attacker") or item.get("attackers"))
        defenders = as_tag_list(item.get("defender") or item.get("defenders"))
        battles.append(
            Battle(
                name=str(item.get("name") or item.get("province") or f"Battle {idx}"),
                date=str(item.get("date") or item.get("start_date") or ""),
                attacker=attackers[0] if attackers else str(item.get("attacker", "")),
                defender=defenders[0] if defenders else str(item.get("defender", "")),
                attacker_losses=extract_battle_loss(item, "attacker"),
                defender_losses=extract_battle_loss(item, "defender"),
                winner=str(item.get("winner", "")),
            )
        )
    return battles


def extract_wars(root: Dict[str, Any]) -> Tuple[List[War], List[str]]:
    warnings: List[str] = []
    war_nodes: List[Dict[str, Any]] = []
    for key in ("active_war", "war", "previous_war"):
        for item in listify(root.get(key)):
            if isinstance(item, dict):
                war_nodes.append(item)
    if not war_nodes:
        warnings.append("No war blocks found. The save may be at peace or uses an unsupported format.")
    wars: List[War] = []
    for idx, item in enumerate(war_nodes, 1):
        attackers = as_tag_list(item.get("attacker") or item.get("attackers") or item.get("first"))
        defenders = as_tag_list(item.get("defender") or item.get("defenders") or item.get("second"))
        # War participant blocks can be named attacker={ country=FRA } or attacker=FRA.
        if not attackers:
            attackers = as_tag_list(item.get("original_attacker"))
        if not defenders:
            defenders = as_tag_list(item.get("original_defender"))
        wars.append(
            War(
                name=str(item.get("name") or item.get("casus_belli") or f"War {idx}"),
                start_date=str(item.get("start_date") or item.get("date") or ""),
                attackers=attackers,
                defenders=defenders,
                battles=extract_battles(item),
                raw=item,
            )
        )
    return wars, warnings


def analyze(path: Path) -> Analysis:
    root = read_save(path)
    countries = extract_countries(root)
    wars, warnings = extract_wars(root)
    return Analysis(
        date=str(root.get("date", "Unknown")),
        player=str(root.get("player", "")),
        wars=wars,
        countries=countries,
        warnings=warnings,
    )


# ----------------------------- game art loading -----------------------------
class ArtLoader:
    """Loads local Victoria II artwork without redistributing copyrighted assets."""

    def __init__(self, roots: Sequence[Path] = ()) -> None:
        self.roots = [p for p in roots if p and p.exists()]
        self.cache: Dict[str, Any] = {}

    def find_flag(self, tag: str) -> Optional[Path]:
        names = [f"{tag}.tga", f"{tag}.png", f"{tag}.gif"]
        subdirs = ["gfx/flags", "mod", ""]
        for root in self.roots:
            for sub in subdirs:
                base = root / sub if sub else root
                for name in names:
                    direct = base / name
                    if direct.exists():
                        return direct
                if base.exists():
                    for found in base.glob(f"**/{tag}.tga"):
                        if "flags" in found.parts:
                            return found
        return None

    def photo_for_flag(self, tag: str, size: Tuple[int, int] = (54, 36)) -> Any:
        import tkinter as tk

        cache_key = f"{tag}:{size}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        path = self.find_flag(tag)
        image = None
        if path:
            try:
                if path.suffix.lower() == ".tga":
                    ppm = tga_to_ppm(path, size)
                    image = tk.PhotoImage(data=base64.b64encode(ppm).decode("ascii"), format="PPM")
                else:
                    image = tk.PhotoImage(file=str(path))
                    image = scale_photo(image, size)
            except Exception:
                image = None
        if image is None:
            image = make_flag_placeholder(tag, size)
        self.cache[cache_key] = image
        return image


def scale_photo(photo: Any, size: Tuple[int, int]) -> Any:
    w, h = max(1, photo.width()), max(1, photo.height())
    target_w, target_h = size
    x_sub = max(1, round(w / target_w))
    y_sub = max(1, round(h / target_h))
    scaled = photo.subsample(x_sub, y_sub)
    if scaled.width() > target_w or scaled.height() > target_h:
        return scaled
    x_zoom = max(1, round(target_w / scaled.width()))
    y_zoom = max(1, round(target_h / scaled.height()))
    return scaled.zoom(x_zoom, y_zoom).subsample(
        max(1, round((scaled.width() * x_zoom) / target_w)),
        max(1, round((scaled.height() * y_zoom) / target_h)),
    )


def tga_to_ppm(path: Path, size: Tuple[int, int]) -> bytes:
    """Read common Victoria II 24/32-bit TGA flags and emit a small PPM image."""
    data = path.read_bytes()
    if len(data) < 18:
        raise ValueError("TGA too small")
    id_len = data[0]
    image_type = data[2]
    width = int.from_bytes(data[12:14], "little")
    height = int.from_bytes(data[14:16], "little")
    bpp = data[16]
    top_origin = bool(data[17] & 0x20)
    if bpp not in (24, 32) or image_type not in (2, 10):
        raise ValueError("Unsupported TGA format")
    offset = 18 + id_len
    pixels: List[Tuple[int, int, int]] = []
    count = width * height
    step = bpp // 8
    if image_type == 2:
        for i in range(count):
            b, g, r = data[offset + i * step : offset + i * step + 3]
            pixels.append((r, g, b))
    else:
        while len(pixels) < count:
            header = data[offset]
            offset += 1
            run = (header & 0x7F) + 1
            if header & 0x80:
                b, g, r = data[offset : offset + 3]
                offset += step
                pixels.extend([(r, g, b)] * run)
            else:
                for _ in range(run):
                    b, g, r = data[offset : offset + 3]
                    offset += step
                    pixels.append((r, g, b))
    if not top_origin:
        rows = [pixels[y * width : (y + 1) * width] for y in range(height)]
        pixels = [px for row in reversed(rows) for px in row]
    target_w, target_h = size
    resized = []
    for y in range(target_h):
        src_y = min(height - 1, int(y * height / target_h))
        for x in range(target_w):
            src_x = min(width - 1, int(x * width / target_w))
            resized.append(pixels[src_y * width + src_x])
    header = f"P6\n{target_w} {target_h}\n255\n".encode("ascii")
    body = bytes(channel for pixel in resized for channel in pixel)
    return header + body


def make_flag_placeholder(tag: str, size: Tuple[int, int]) -> Any:
    import tkinter as tk

    w, h = size
    photo = tk.PhotoImage(width=w, height=h)
    palette = ["#243447", "#d4af37", "#8b1e3f", "#f2ead3"]
    seed = sum(ord(c) for c in tag)
    for y in range(h):
        color = palette[(seed + y // max(1, h // 3)) % len(palette)]
        photo.put(color, to=(0, y, w, y + 1))
    return photo


# ----------------------------- GUI -----------------------------
class AnalyzerApp:
    def __init__(self, save_path: Optional[Path] = None, game_dir: Optional[Path] = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1180x760")
        self.root.minsize(960, 620)
        self.art = ArtLoader([p for p in [game_dir, autodetect_game_dir()] if p])
        self.analysis: Optional[Analysis] = None
        self.selected_war: Optional[War] = None
        self.flag_refs: List[Any] = []
        self._style()
        self._build()
        if save_path:
            self.load_save(save_path)

    def _style(self) -> None:
        ttk = self.ttk
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.colors = {
            "bg": "#141821",
            "panel": "#1f2633",
            "card": "#273244",
            "text": "#ecf1f8",
            "muted": "#9aa9bd",
            "gold": "#d7b46a",
            "red": "#c95050",
            "blue": "#5f8dd3",
            "green": "#65b081",
        }
        self.root.configure(bg=self.colors["bg"])
        style.configure("TFrame", background=self.colors["bg"])
        style.configure("Card.TFrame", background=self.colors["panel"], relief="flat")
        style.configure("TLabel", background=self.colors["bg"], foreground=self.colors["text"])
        style.configure("Muted.TLabel", foreground=self.colors["muted"], background=self.colors["bg"])
        style.configure("Hero.TLabel", font=("Segoe UI", 24, "bold"), foreground=self.colors["gold"], background=self.colors["bg"])
        style.configure("Stat.TLabel", font=("Segoe UI", 18, "bold"), background=self.colors["panel"], foreground=self.colors["text"])
        style.configure("Small.TLabel", font=("Segoe UI", 9), background=self.colors["panel"], foreground=self.colors["muted"])
        style.configure("TButton", padding=8)
        style.configure("Treeview", rowheight=28, fieldbackground=self.colors["panel"], background=self.colors["panel"], foreground=self.colors["text"])
        style.map("Treeview", background=[("selected", "#3b4c66")])

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        shell = ttk.Frame(self.root, padding=18)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x")
        ttk.Label(header, text="Victoria II War Analyzer", style="Hero.TLabel").pack(side="left")
        ttk.Button(header, text="Open save", command=self.pick_save).pack(side="right", padx=4)
        ttk.Button(header, text="Set game folder", command=self.pick_game_dir).pack(side="right", padx=4)
        ttk.Label(shell, text="Modern war ledger with local game flags, casualty charts, side balance, and JSON export.", style="Muted.TLabel").pack(anchor="w", pady=(2, 14))

        self.stat_frame = ttk.Frame(shell)
        self.stat_frame.pack(fill="x", pady=(0, 12))
        self.stats: Dict[str, Any] = {}
        for key, label in [("date", "SAVE DATE"), ("wars", "ACTIVE WARS"), ("casualties", "KNOWN LOSSES"), ("countries", "COUNTRIES")]:
            card = ttk.Frame(self.stat_frame, style="Card.TFrame", padding=12)
            card.pack(side="left", fill="x", expand=True, padx=5)
            ttk.Label(card, text="—", style="Stat.TLabel").pack(anchor="w")
            value_label = card.winfo_children()[0]
            ttk.Label(card, text=label, style="Small.TLabel").pack(anchor="w")
            self.stats[key] = value_label

        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Card.TFrame", padding=10)
        left.pack(side="left", fill="both", expand=False, padx=(0, 10))
        columns = ("name", "start", "attackers", "defenders", "losses")
        self.war_tree = ttk.Treeview(left, columns=columns, show="headings", height=18)
        for col, width in [("name", 220), ("start", 90), ("attackers", 115), ("defenders", 115), ("losses", 95)]:
            self.war_tree.heading(col, text=col.title())
            self.war_tree.column(col, width=width, anchor="w")
        self.war_tree.pack(fill="both", expand=True)
        self.war_tree.bind("<<TreeviewSelect>>", self.on_war_selected)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        self.title = ttk.Label(right, text="Open a Victoria II save to begin", style="Hero.TLabel")
        self.title.pack(anchor="w")
        self.subtitle = ttk.Label(right, text="Plaintext .v2 saves are supported.", style="Muted.TLabel")
        self.subtitle.pack(anchor="w", pady=(0, 10))
        self.canvas = tk.Canvas(right, bg=self.colors["panel"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.details = tk.Text(right, height=9, bg=self.colors["panel"], fg=self.colors["text"], insertbackground=self.colors["text"], relief="flat", padx=10, pady=8)
        self.details.pack(fill="x", pady=(10, 0))
        self.details.insert("end", "Tip: Use Set game folder to load Victoria II flags from gfx/flags.\n")
        self.details.configure(state="disabled")

    def pick_save(self) -> None:
        from tkinter import filedialog, messagebox

        path = filedialog.askopenfilename(title="Open Victoria II save", filetypes=[("Victoria II saves", "*.v2 *.eu4 *.txt"), ("All files", "*.*")])
        if path:
            try:
                self.load_save(Path(path))
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc))

    def pick_game_dir(self) -> None:
        from tkinter import filedialog

        path = filedialog.askdirectory(title="Select Victoria II install or mod folder")
        if path:
            self.art = ArtLoader([Path(path)])
            if self.selected_war:
                self.render_war(self.selected_war)

    def load_save(self, path: Path) -> None:
        self.analysis = analyze(path)
        self.root.title(f"{APP_TITLE} — {path.name}")
        self.update_overview()
        self.war_tree.delete(*self.war_tree.get_children())
        for i, war in enumerate(self.analysis.wars):
            self.war_tree.insert("", "end", iid=str(i), values=(war.name, war.start_date, ", ".join(war.attackers), ", ".join(war.defenders), f"{war.casualties:,}"))
        if self.analysis.wars:
            self.war_tree.selection_set("0")
            self.render_war(self.analysis.wars[0])
        else:
            self.title.configure(text="No active wars found")
            self.subtitle.configure(text="Try another plaintext save or inspect the warnings below.")
            self.show_text("\n".join(self.analysis.warnings))

    def update_overview(self) -> None:
        assert self.analysis
        total_losses = sum(w.casualties for w in self.analysis.wars)
        self.stats["date"].configure(text=self.analysis.date)
        self.stats["wars"].configure(text=str(len(self.analysis.wars)))
        self.stats["casualties"].configure(text=f"{total_losses:,}")
        self.stats["countries"].configure(text=str(len(self.analysis.countries)))

    def on_war_selected(self, _event: Any = None) -> None:
        if not self.analysis:
            return
        selected = self.war_tree.selection()
        if selected:
            self.render_war(self.analysis.wars[int(selected[0])])

    def render_war(self, war: War) -> None:
        self.selected_war = war
        self.flag_refs.clear()
        self.title.configure(text=war.name)
        self.subtitle.configure(text=f"Started {war.start_date or 'unknown'} • {len(war.battles)} recorded battles • {war.casualties:,} known casualties")
        self.canvas.delete("all")
        self.root.update_idletasks()
        w = max(600, self.canvas.winfo_width())
        h = max(320, self.canvas.winfo_height())
        self.draw_gradient(w, h)
        self.draw_side(36, 34, "Attackers", war.attackers, self.colors["blue"])
        self.draw_side(w - 330, 34, "Defenders", war.defenders, self.colors["red"])
        a_loss, d_loss = war.side_losses()
        self.draw_balance(w, h, a_loss, d_loss)
        self.draw_battles(w, h, war.battles)
        self.show_text(self.details_for(war))

    def draw_gradient(self, w: int, h: int) -> None:
        for y in range(0, h, 3):
            shade = 31 + int(22 * y / max(1, h))
            self.canvas.create_rectangle(0, y, w, y + 3, fill=f"#{shade:02x}{shade+7:02x}{shade+18:02x}", outline="")
        self.canvas.create_text(w // 2, 28, text="WAR ROOM", fill="#334058", font=("Segoe UI", 34, "bold"))

    def draw_side(self, x: int, y: int, title: str, tags: List[str], color: str) -> None:
        self.canvas.create_text(x, y, text=title.upper(), anchor="nw", fill=color, font=("Segoe UI", 13, "bold"))
        for row, tag in enumerate(tags[:8]):
            yy = y + 32 + row * 44
            img = self.art.photo_for_flag(tag)
            self.flag_refs.append(img)
            self.canvas.create_image(x, yy, image=img, anchor="nw")
            name = tag
            if self.analysis and tag in self.analysis.countries:
                c = self.analysis.countries[tag]
                name = f"{tag}  Mil {c.military_score:.0f}  Prestige {c.prestige:.0f}"
            self.canvas.create_text(x + 66, yy + 18, text=name, anchor="w", fill=self.colors["text"], font=("Segoe UI", 10, "bold"))
        if len(tags) > 8:
            self.canvas.create_text(x, y + 32 + 8 * 44, text=f"+ {len(tags) - 8} more", anchor="nw", fill=self.colors["muted"])

    def draw_balance(self, w: int, h: int, a_loss: int, d_loss: int) -> None:
        x0, y0, bar_w, bar_h = 270, 130, max(220, w - 540), 34
        total = max(1, a_loss + d_loss)
        split = int(bar_w * a_loss / total)
        self.canvas.create_text(w // 2, y0 - 36, text="Casualty balance", fill=self.colors["text"], font=("Segoe UI", 16, "bold"))
        self.canvas.create_rectangle(x0, y0, x0 + bar_w, y0 + bar_h, fill=self.colors["red"], outline="")
        self.canvas.create_rectangle(x0, y0, x0 + split, y0 + bar_h, fill=self.colors["blue"], outline="")
        self.canvas.create_text(x0, y0 + bar_h + 18, text=f"Attackers lost {a_loss:,}", anchor="w", fill=self.colors["muted"])
        self.canvas.create_text(x0 + bar_w, y0 + bar_h + 18, text=f"Defenders lost {d_loss:,}", anchor="e", fill=self.colors["muted"])

    def draw_battles(self, w: int, h: int, battles: List[Battle]) -> None:
        top = 235
        self.canvas.create_text(w // 2, top - 28, text="Largest battles", fill=self.colors["text"], font=("Segoe UI", 15, "bold"))
        ranked = sorted(battles, key=lambda b: b.total_losses, reverse=True)[:7]
        max_loss = max([b.total_losses for b in ranked] or [1])
        for i, battle in enumerate(ranked):
            y = top + i * 34
            width = int((w - 560) * battle.total_losses / max_loss)
            self.canvas.create_rectangle(280, y, 280 + width, y + 18, fill=self.colors["gold"], outline="")
            self.canvas.create_text(272, y + 9, text=str(i + 1), anchor="e", fill=self.colors["muted"])
            self.canvas.create_text(290 + width, y + 9, text=f"{battle.name} — {battle.total_losses:,}", anchor="w", fill=self.colors["text"])
        if not ranked:
            self.canvas.create_text(w // 2, top + 18, text="No battle casualty records found in this save block.", fill=self.colors["muted"])

    def details_for(self, war: War) -> str:
        lines = [
            f"War: {war.name}",
            f"Start date: {war.start_date or 'Unknown'}",
            f"Attackers: {', '.join(war.attackers) or 'Unknown'}",
            f"Defenders: {', '.join(war.defenders) or 'Unknown'}",
            f"Known casualties: {war.casualties:,}",
            "",
            "Top battles:",
        ]
        for battle in sorted(war.battles, key=lambda b: b.total_losses, reverse=True)[:10]:
            lines.append(f"• {battle.name}: {battle.attacker} vs {battle.defender}, {battle.total_losses:,} losses")
        if self.analysis and self.analysis.warnings:
            lines.extend(["", "Warnings:", *[f"• {w}" for w in self.analysis.warnings]])
        return "\n".join(lines)

    def show_text(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text or "No details available.")
        self.details.configure(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


def autodetect_game_dir() -> Optional[Path]:
    candidates = [
        Path.home() / ".steam/steam/steamapps/common/Victoria 2",
        Path.home() / ".local/share/Steam/steamapps/common/Victoria 2",
        Path("/mnt/c/Program Files (x86)/Steam/steamapps/common/Victoria 2"),
    ]
    if os.environ.get("VICTORIA2_DIR"):
        candidates.append(Path(os.environ["VICTORIA2_DIR"]))
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


# ----------------------------- CLI -----------------------------
def to_jsonable(analysis: Analysis) -> Dict[str, Any]:
    return {
        "date": analysis.date,
        "player": analysis.player,
        "warnings": analysis.warnings,
        "wars": [
            {
                "name": w.name,
                "start_date": w.start_date,
                "attackers": w.attackers,
                "defenders": w.defenders,
                "casualties": w.casualties,
                "attacker_losses": w.side_losses()[0],
                "defender_losses": w.side_losses()[1],
                "battles": [battle.__dict__ for battle in w.battles],
            }
            for w in analysis.wars
        ],
        "countries": {tag: country.__dict__ for tag, country in analysis.countries.items()},
    }


def print_summary(analysis: Analysis) -> None:
    print(f"Save date: {analysis.date}")
    if analysis.player:
        print(f"Player: {analysis.player}")
    print(f"Wars: {len(analysis.wars)} | Countries: {len(analysis.countries)}")
    for warning in analysis.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    for war in analysis.wars:
        a_loss, d_loss = war.side_losses()
        print(f"\n{war.name}")
        print(f"  Started: {war.start_date or 'Unknown'}")
        print(f"  Attackers: {', '.join(war.attackers) or 'Unknown'}")
        print(f"  Defenders: {', '.join(war.defenders) or 'Unknown'}")
        print(f"  Casualties: {war.casualties:,} (attackers {a_loss:,}, defenders {d_loss:,})")
        for battle in sorted(war.battles, key=lambda b: b.total_losses, reverse=True)[:5]:
            print(f"    - {battle.name}: {battle.total_losses:,} losses")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("save", nargs="?", type=Path, help="Path to a plaintext Victoria II save")
    parser.add_argument("--game-dir", type=Path, help="Victoria II install/mod folder for flags")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of launching the GUI")
    parser.add_argument("--summary", action="store_true", help="Print a terminal summary instead of launching the GUI")
    args = parser.parse_args(argv)

    if args.json or args.summary:
        if not args.save:
            parser.error("--json/--summary requires a save path")
        analysis = analyze(args.save)
        if args.json:
            print(json.dumps(to_jsonable(analysis), indent=2, sort_keys=True))
        else:
            print_summary(analysis)
        return 0

    app = AnalyzerApp(args.save, args.game_dir)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
