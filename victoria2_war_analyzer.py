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
import mimetypes
import os
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
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


def read_save_document(path: Path) -> Tuple[Dict[str, Any], str]:
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise SaveParseError(
            "This appears to be a binary/compressed save. Re-save Victoria II as plaintext first."
        )
    # Victoria II saves and localisation-era files are commonly ANSI/Latin-1;
    # utf-8-sig keeps modern edited saves readable while latin-1 preserves bytes.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    return ClausewitzParser(text).parse(), text


def read_save(path: Path) -> Dict[str, Any]:
    root, _text = read_save_document(path)
    return root


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
    government: str = ""


@dataclass
class Battle:
    name: str
    date: str = ""
    location: str = ""
    attacker: str = ""
    defender: str = ""
    attacker_leader: str = ""
    defender_leader: str = ""
    attacker_losses: int = 0
    defender_losses: int = 0
    attacker_army: Dict[str, int] = field(default_factory=dict)
    defender_army: Dict[str, int] = field(default_factory=dict)
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
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                pass
        if isinstance(val, list):
            nums = []
            for item in val:
                if isinstance(item, (int, float)):
                    nums.append(float(item))
                elif isinstance(item, str):
                    try:
                        nums.append(float(item))
                    except ValueError:
                        pass
            if nums:
                return float(nums[-1])
    return 0.0



def first_scalar(value: Any) -> Any:
    if isinstance(value, list):
        for item in value:
            scalar = first_scalar(item)
            if scalar not in (None, ""):
                return scalar
        return None
    return value


def country_from_node(tag: str, data: Dict[str, Any]) -> Country:
    military = get_number(data, "military_score", "military", "mil_score")
    industry = get_number(data, "industrial_score", "industry_score", "industrial", "industry")
    total = get_number(data, "score", "total_score", "overall_score")
    # Some Victoria II saves keep the displayed rank scores in a nested score block.
    score_block = data.get("score")
    if isinstance(score_block, dict):
        military = military or get_number(score_block, "military_score", "military")
        industry = industry or get_number(score_block, "industrial_score", "industrial", "industry")
        total = total or get_number(score_block, "score", "total")
    return Country(
        tag=tag,
        name=str(first_scalar(data.get("name")) or tag),
        prestige=get_number(data, "prestige"),
        military_score=military,
        industrial_score=industry,
        total_score=total,
        manpower=get_number(data, "manpower"),
        badboy=get_number(data, "badboy", "infamy"),
        government=str(first_scalar(data.get("government")) or first_scalar(data.get("government_flag")) or ""),
    )


def extract_tagged_country(value: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not isinstance(value, dict):
        return None, None
    tag_value = first_scalar(value.get("tag") or value.get("country"))
    if isinstance(tag_value, str) and COUNTRY_TAG.match(tag_value):
        return tag_value, value
    for key, child in value.items():
        if COUNTRY_TAG.match(str(key)) and isinstance(child, dict):
            return str(key), child
    return None, None

def extract_countries(root: Dict[str, Any]) -> Dict[str, Country]:
    countries: Dict[str, Country] = {}
    for key, val in root.items():
        if COUNTRY_TAG.match(str(key)) and isinstance(val, dict):
            countries[key] = country_from_node(key, val)
    # Some saves keep countries under a countries={ TAG={...} } object.
    nested = root.get("countries")
    if isinstance(nested, dict):
        for key, val in nested.items():
            if COUNTRY_TAG.match(str(key)) and isinstance(val, dict):
                countries.setdefault(key, country_from_node(key, val))
    for item in listify(root.get("country")):
        tag, node = extract_tagged_country(item)
        if tag and node:
            countries[tag] = country_from_node(tag, node)
    return countries


def extract_battle_loss(data: Dict[str, Any], side: str) -> int:
    direct_keys = [f"{side}_losses", f"{side}_casualties", f"losses_{side}"]
    for key in direct_keys:
        n = get_number(data, key)
        if n:
            return int(n)
    side_data = data.get(side)
    side_nodes = side_data if isinstance(side_data, list) else [side_data]
    for node in side_nodes:
        if isinstance(node, dict):
            loss = get_number(node, "losses", "casualties", "dead")
            if loss:
                return int(loss)
    losses = data.get("losses")
    if isinstance(losses, list):
        numeric_losses = [int(x) for x in losses if isinstance(x, (int, float))]
        if len(numeric_losses) >= 2:
            return numeric_losses[0 if side == "attacker" else 1]
    elif isinstance(losses, dict):
        loss = get_number(losses, side, f"{side}_losses")
        if loss:
            return int(loss)
    return 0




def side_details(data: Dict[str, Any], side: str) -> Tuple[str, str, Dict[str, int]]:
    side_data = data.get(side)
    nodes = side_data if isinstance(side_data, list) else [side_data]
    for node in nodes:
        if isinstance(node, dict):
            tags = as_tag_list(node.get("country") or node.get("tag") or node)
            leader = str(first_scalar(node.get("leader")) or "")
            army: Dict[str, int] = {}
            for key, value in node.items():
                if key in {"country", "tag", "leader", "losses", "casualties", "dead"}:
                    continue
                if isinstance(value, (int, float)):
                    army[key] = int(value)
            return (tags[0] if tags else "", leader, army)
    return "", "", {}


def date_from_line(line: str) -> str:
    match = DATE_RE.search(line)
    return match.group(0) if match else ""

def extract_battles(war_data: Dict[str, Any]) -> List[Battle]:
    battles: List[Battle] = []
    containers: List[Any] = []
    for key in ("battle", "battles", "combat"):
        containers.extend(listify(war_data.get(key)))
    for idx, item in enumerate(containers, 1):
        if not isinstance(item, dict):
            continue
        attacker, attacker_leader, attacker_army = side_details(item, "attacker")
        defender, defender_leader, defender_army = side_details(item, "defender")
        attackers = as_tag_list(item.get("attacker") or item.get("attackers"))
        defenders = as_tag_list(item.get("defender") or item.get("defenders"))
        battles.append(
            Battle(
                name=str(item.get("name") or item.get("province") or item.get("location") or f"Battle {idx}"),
                date=str(item.get("date") or item.get("start_date") or ""),
                location=str(item.get("location") or item.get("province") or ""),
                attacker=attacker or (attackers[0] if attackers else str(item.get("attacker", ""))),
                defender=defender or (defenders[0] if defenders else str(item.get("defender", ""))),
                attacker_leader=attacker_leader,
                defender_leader=defender_leader,
                attacker_losses=extract_battle_loss(item, "attacker"),
                defender_losses=extract_battle_loss(item, "defender"),
                attacker_army=attacker_army,
                defender_army=defender_army,
                winner=str(item.get("winner") or item.get("result") or ""),
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



def brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def value_after_equals(line: str) -> str:
    if "=" not in line:
        return ""
    return line.split("=", 1)[1].strip().strip('"').strip("'")


def tag_from_line_value(line: str) -> str:
    value = value_after_equals(line)
    if COUNTRY_TAG.match(value):
        return value
    match = re.search(r'"?([A-Z0-9]{3})"?', value)
    return match.group(1) if match else ""


def parse_int_value(line: str) -> int:
    try:
        return int(float(value_after_equals(line)))
    except ValueError:
        return 0


def is_unit_line(line: str) -> bool:
    if "=" not in line or "{" in line or "}" in line:
        return False
    key = line.split("=", 1)[0].strip()
    return key not in {"country", "tag", "leader", "losses", "casualties", "dead", "name", "date", "result", "location"}


def add_unique(target: List[str], tag: str) -> None:
    if tag and tag != "---" and tag not in target:
        target.append(tag)


def finalize_war_dates(war: War) -> War:
    if not war.start_date:
        dated = sorted(
            (b.date for b in war.battles if parse_date(b.date)),
            key=lambda d: parse_date(d) or date.max,
        )
        if dated:
            war.start_date = dated[0]
    return war


def extract_wars_from_lines(text: str) -> List[War]:
    """Fallback scanner for the exact active_war/previous_war layout Victoria II writes.

    The full parser is useful for arbitrary data, but real battle losses are stored as
    repeated `losses=` keys inside attacker/defender sub-blocks.  This scanner keeps
    that order intact so losses do not collapse to zero on saves with old-style battle
    blocks.
    """
    wars: List[War] = []
    current: Optional[War] = None
    battle: Optional[Battle] = None
    in_war = False
    in_battle = False
    side: Optional[str] = None
    war_depth = 0
    battle_depth = 0
    side_depth = 0
    pending_war = False
    pending_battle = False
    pending_side: Optional[str] = None
    pending_participant_side: Optional[str] = None
    participant_side: Optional[str] = None
    participant_depth = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        is_war_key = line.startswith("active_war=") or line.startswith("previous_war=") or line.startswith("war=")
        starts_war = (is_war_key and "{" in line) or (pending_war and line.startswith("{"))
        if is_war_key and "{" not in line:
            pending_war = True
            continue
        if starts_war and not in_war:
            current = War(name=f"War {len(wars) + 1}")
            in_war = True
            pending_war = False
            war_depth = brace_delta(line)
            continue
        if pending_war and line and not line.startswith("{"):
            pending_war = False
        if not in_war or current is None:
            continue

        delta = brace_delta(line)
        if in_battle and battle is not None:
            if line.startswith("name="):
                battle.name = value_after_equals(line)
            elif line.startswith("date="):
                battle.date = value_after_equals(line)
            elif line.startswith("location="):
                battle.location = value_after_equals(line)
                if not battle.name:
                    battle.name = battle.location
            elif line.startswith("result="):
                battle.winner = "attacker" if value_after_equals(line) == "yes" else "defender"
            elif line.startswith("attacker=") or line.startswith("defender=") or (pending_side and line.startswith("{")):
                if pending_side and line.startswith("{"):
                    side = pending_side
                    pending_side = None
                    side_depth = delta
                    direct_tag = ""
                else:
                    side = "attacker" if line.startswith("attacker=") else "defender"
                    if "{" not in line:
                        pending_side = side
                        side_depth = 0
                        battle_depth += delta
                        war_depth += delta
                        continue
                    side_depth = delta
                    direct_tag = tag_from_line_value(line)
                if direct_tag and side == "attacker":
                    battle.attacker = direct_tag
                elif direct_tag and side == "defender":
                    battle.defender = direct_tag
            elif side and line.startswith("country="):
                tag = tag_from_line_value(line)
                if tag and side == "attacker":
                    battle.attacker = tag
                elif tag and side == "defender":
                    battle.defender = tag
            elif side and line.startswith("leader="):
                if side == "attacker":
                    battle.attacker_leader = value_after_equals(line)
                else:
                    battle.defender_leader = value_after_equals(line)
            elif side and line.startswith("losses="):
                losses = parse_int_value(line)
                if side == "attacker":
                    battle.attacker_losses = losses
                else:
                    battle.defender_losses = losses
            elif side and is_unit_line(line):
                unit = line.split("=", 1)[0].strip()
                count = parse_int_value(line)
                if count:
                    if side == "attacker":
                        battle.attacker_army[unit] = count
                    else:
                        battle.defender_army[unit] = count
            if side:
                side_depth += delta if not (line.startswith("attacker=") or line.startswith("defender=")) else 0
                if side_depth <= 0 and "}" in line:
                    side = None
                    side_depth = 0
            battle_depth += delta
            if battle_depth <= 0:
                if not battle.name:
                    battle.name = f"Battle {len(current.battles) + 1}"
                current.battles.append(battle)
                battle = None
                in_battle = False
                side = None
            war_depth += delta
            continue

        starts_battle = (line.startswith("battle=") and "{" in line) or (pending_battle and line.startswith("{"))
        if line.startswith("battle=") and "{" not in line:
            pending_battle = True
            continue
        if starts_battle:
            pending_battle = False
            battle = Battle(name=f"Battle {len(current.battles) + 1}")
            in_battle = True
            battle_depth = delta
            war_depth += delta
            if battle_depth <= 0:
                current.battles.append(battle)
                battle = None
                in_battle = False
            continue
        if pending_participant_side and line.startswith("{"):
            participant_side = pending_participant_side
            pending_participant_side = None
            participant_depth = delta
            war_depth += delta
            continue
        if participant_side:
            if line.startswith("country=") or line.startswith("tag="):
                if participant_side == "attacker":
                    add_unique(current.attackers, tag_from_line_value(line))
                else:
                    add_unique(current.defenders, tag_from_line_value(line))
            participant_depth += delta
            war_depth += delta
            if participant_depth <= 0 and "}" in line:
                participant_side = None
                participant_depth = 0
            continue

        if "date=" in line and not current.start_date:
            current.start_date = date_from_line(line) or value_after_equals(line)
        if line.startswith("name="):
            current.name = value_after_equals(line)
        elif line.startswith("start_date=") or line.startswith("date="):
            if not current.start_date:
                current.start_date = date_from_line(line) or value_after_equals(line)
        elif line.startswith("attacker=") or line.startswith("add_attacker=") or line.startswith("original_attacker="):
            tag = tag_from_line_value(line)
            add_unique(current.attackers, tag)
            if not tag and "{" not in line:
                pending_participant_side = "attacker"
            elif not tag and "{" in line:
                participant_side = "attacker"
                participant_depth = delta
        elif line.startswith("defender=") or line.startswith("add_defender=") or line.startswith("original_defender="):
            tag = tag_from_line_value(line)
            add_unique(current.defenders, tag)
            if not tag and "{" not in line:
                pending_participant_side = "defender"
            elif not tag and "{" in line:
                participant_side = "defender"
                participant_depth = delta

        war_depth += delta
        if war_depth <= 0:
            wars.append(finalize_war_dates(current))
            current = None
            in_war = False
            war_depth = 0
    if current is not None:
        wars.append(finalize_war_dates(current))
    return wars


def merge_line_war_data(wars: List[War], line_wars: List[War]) -> List[War]:
    if not line_wars:
        return wars
    if not wars:
        return line_wars
    for i, line_war in enumerate(line_wars):
        if i >= len(wars):
            wars.append(line_war)
            continue
        parsed = wars[i]
        if line_war.name and (not parsed.name or parsed.name.startswith("War ")):
            parsed.name = line_war.name
        if line_war.casualties and (parsed.casualties == 0 or len(line_war.battles) >= len(parsed.battles)):
            parsed.battles = line_war.battles
        for tag in line_war.attackers:
            add_unique(parsed.attackers, tag)
        for tag in line_war.defenders:
            add_unique(parsed.defenders, tag)
        if not parsed.start_date:
            parsed.start_date = line_war.start_date
        finalize_war_dates(parsed)
    return wars


def analyze(path: Path) -> Analysis:
    root, text = read_save_document(path)
    countries = extract_countries(root)
    wars, warnings = extract_wars(root)
    line_wars = extract_wars_from_lines(text)
    wars = merge_line_war_data(wars, line_wars)
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

    def find_flag(self, tag: str, government: str = "") -> Optional[Path]:
        suffixes = (".tga", ".png", ".gif")
        stems: List[str] = []
        if government:
            stems.extend([f"{tag}_{government}", f"{tag}_{government.lower()}"])
        stems.append(tag)
        subdirs = ["gfx/flags", "mod", ""]
        for root in self.roots:
            for sub in subdirs:
                base = root / sub if sub else root
                if not base.exists():
                    continue
                for stem in stems:
                    for suffix in suffixes:
                        direct = base / f"{stem}{suffix}"
                        if direct.exists():
                            return direct
                # Victoria II's real flag files are usually TAG_government.tga,
                # not TAG.tga. If the save did not expose a government, use a
                # deterministic country-specific flag rather than a placeholder.
                matches = sorted(
                    [p for p in base.glob(f"**/{tag}_*.tga") if "flags" in p.parts],
                    key=lambda p: (0 if government and government.lower() in p.stem.lower() else 1, p.name),
                )
                if matches:
                    return matches[0]
        return None

    def photo_for_flag(self, tag: str, government: str = "", size: Tuple[int, int] = (54, 36)) -> Any:
        import tkinter as tk

        cache_key = f"{tag}:{government}:{size}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        path = self.find_flag(tag, government)
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
            c = self.analysis.countries.get(tag) if self.analysis else None
            img = self.art.photo_for_flag(tag, c.government if c else "")
            self.flag_refs.append(img)
            self.canvas.create_image(x, yy, image=img, anchor="nw")
            name = tag
            if c:
                mil = f"Mil {c.military_score:.0f}" if c.military_score else "Mil ?"
                name = f"{tag}  {mil}  Prestige {c.prestige:.0f}"
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



# ----------------------------- browser UI -----------------------------
def ppm_to_bmp(ppm: bytes) -> bytes:
    header, rest = ppm.split(b"\n", 3)[0:3], ppm.split(b"\n", 3)[3]
    width, height = map(int, header[1].split())
    row_pad = (4 - (width * 3) % 4) % 4
    pixel_rows = []
    for y in range(height - 1, -1, -1):
        start = y * width * 3
        row = bytearray()
        for x in range(width):
            r, g, b = rest[start + x * 3 : start + x * 3 + 3]
            row.extend([b, g, r])
        row.extend(b"\x00" * row_pad)
        pixel_rows.append(bytes(row))
    pixel_data = b"".join(pixel_rows)
    file_size = 54 + len(pixel_data)
    return (
        b"BM"
        + file_size.to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + (54).to_bytes(4, "little")
        + (40).to_bytes(4, "little")
        + width.to_bytes(4, "little")
        + height.to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (24).to_bytes(2, "little")
        + (0).to_bytes(4, "little")
        + len(pixel_data).to_bytes(4, "little")
        + (2835).to_bytes(4, "little")
        + (2835).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + pixel_data
    )


def flag_data_uri(art: ArtLoader, tag: str, government: str = "") -> str:
    path = art.find_flag(tag, government)
    if not path:
        return ""
    try:
        if path.suffix.lower() == ".tga":
            bmp = ppm_to_bmp(tga_to_ppm(path, (72, 48)))
            return "data:image/bmp;base64," + base64.b64encode(bmp).decode("ascii")
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        return ""


def web_payload(analysis: Analysis, art: ArtLoader) -> Dict[str, Any]:
    payload = to_jsonable(analysis)
    payload["flags"] = {
        tag: flag_data_uri(art, tag, country.government)
        for tag, country in analysis.countries.items()
    }
    return payload


def render_web_app(analysis: Analysis, art: ArtLoader) -> str:
    data = json.dumps(web_payload(analysis, art)).replace("</", "<\\/")
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Victoria II War Analyzer</title>
<style>
:root{--bg:#0b1020;--panel:#121a2c;--panel2:#19243a;--text:#edf3ff;--muted:#91a0b8;--gold:#e1b866;--red:#ef6b73;--blue:#71a7ff;--green:#70d99b;--line:#29364f}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#23375f 0,#0b1020 42%,#070a12 100%);color:var(--text);font:14px/1.45 Inter,Segoe UI,system-ui,sans-serif}.app{display:grid;grid-template-columns:320px 1fr;min-height:100vh}.side{border-right:1px solid var(--line);background:rgba(9,13,24,.82);backdrop-filter:blur(12px);padding:24px;position:sticky;top:0;height:100vh;overflow:auto}.brand{font-size:28px;font-weight:900;letter-spacing:-.04em;color:var(--gold);margin-bottom:6px}.sub{color:var(--muted);margin-bottom:22px}.statgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:18px 0}.stat{background:linear-gradient(145deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:18px;padding:14px}.stat b{display:block;font-size:22px}.stat span{font-size:11px;color:var(--muted);letter-spacing:.08em}.warbtn{width:100%;text-align:left;background:rgba(255,255,255,.04);color:var(--text);border:1px solid var(--line);border-radius:14px;margin:8px 0;padding:12px;cursor:pointer}.warbtn.active{border-color:var(--gold);box-shadow:0 0 0 1px rgba(225,184,102,.35) inset}.main{padding:28px 34px 50px;overflow:auto}.hero{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:18px}.hero h1{font-size:34px;line-height:1.05;margin:0 0 8px}.pill{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--line);background:rgba(255,255,255,.05);padding:8px 11px;border-radius:999px;color:var(--muted)}.cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0}.card{background:rgba(18,26,44,.86);border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:0 18px 60px rgba(0,0,0,.28)}.sideTitle{font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}.nation{display:flex;align-items:center;gap:12px;padding:8px;border-radius:12px}.flag{width:54px;height:36px;border-radius:6px;object-fit:cover;background:linear-gradient(135deg,#31486f,#d5b56a);border:1px solid rgba(255,255,255,.2)}.tag{font-weight:800}.muted{color:var(--muted)}.balance{height:20px;background:var(--red);border-radius:999px;overflow:hidden;border:1px solid var(--line)}.balance span{display:block;height:100%;background:var(--blue)}.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:18px 0}.toolbar input,.toolbar select{background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:12px;padding:10px 12px}table{width:100%;border-collapse:separate;border-spacing:0 8px}th{text-align:left;color:var(--muted);font-size:12px;letter-spacing:.07em;text-transform:uppercase;padding:0 12px;cursor:pointer}td{background:rgba(18,26,44,.88);border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:12px}td:first-child{border-left:1px solid var(--line);border-radius:14px 0 0 14px}td:last-child{border-right:1px solid var(--line);border-radius:0 14px 14px 0}.row{cursor:pointer}.row:hover td{background:#1d2a44}.detail{position:fixed;right:24px;bottom:24px;width:min(520px,calc(100vw - 48px));max-height:72vh;overflow:auto;background:#10192b;border:1px solid var(--gold);border-radius:24px;padding:20px;box-shadow:0 30px 100px rgba(0,0,0,.55);display:none}.detail.open{display:block}.close{float:right;background:transparent;color:var(--muted);border:0;font-size:22px;cursor:pointer}.army{display:grid;grid-template-columns:1fr 1fr;gap:12px}.mini{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:14px;padding:12px}@media(max-width:850px){.app{grid-template-columns:1fr}.side{position:static;height:auto}.cards{grid-template-columns:1fr}.hero{display:block}}
</style>
</head><body><div class="app"><aside class="side"><div class="brand">Victoria II<br>War Analyzer</div><div class="sub">Browser war room with sortable battles and commander details.</div><div class="statgrid"><div class="stat"><b id="saveDate">—</b><span>SAVE DATE</span></div><div class="stat"><b id="warCount">—</b><span>WARS</span></div><div class="stat"><b id="lossCount">—</b><span>LOSSES</span></div><div class="stat"><b id="countryCount">—</b><span>COUNTRIES</span></div></div><div id="wars"></div></aside><main class="main"><section class="hero"><div><h1 id="warName">Select a war</h1><div class="muted" id="warMeta"></div></div><div class="pill" id="lossPill">0 losses</div></section><section class="cards"><div class="card"><div class="sideTitle" style="color:var(--blue)">Attackers</div><div id="attackers"></div></div><div class="card"><div class="sideTitle" style="color:var(--red)">Defenders</div><div id="defenders"></div></div></section><section class="card"><div class="sideTitle">Casualty Balance</div><div class="balance"><span id="balanceBar"></span></div><div class="muted" id="balanceText" style="margin-top:8px"></div></section><section class="card" style="margin-top:16px"><div class="toolbar"><strong>Battle Ledger</strong><input id="filter" placeholder="Filter battles, commanders, tags…"><select id="sort"><option value="losses-desc">Losses ↓</option><option value="losses-asc">Losses ↑</option><option value="date-asc">Date ↑</option><option value="date-desc">Date ↓</option><option value="name-asc">Name A-Z</option></select></div><table><thead><tr><th data-sort="name-asc">Battle</th><th data-sort="date-asc">Date</th><th>Commanders</th><th data-sort="losses-desc">Losses</th><th>Winner</th></tr></thead><tbody id="battleRows"></tbody></table></section></main></div><aside id="detail" class="detail"></aside><script id="payload" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('payload').textContent);let current=0;let sort='losses-desc';const fmt=n=>(n||0).toLocaleString();const country=t=>data.countries[t]||{tag:t,name:t,military_score:0,prestige:0,government:''};function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function nation(tag){const c=country(tag);const src=data.flags[tag]||'';const flag=src?`<img class="flag" src="${src}">`:`<div class="flag"></div>`;return `<div class="nation">${flag}<div><div class="tag">${esc(tag)} ${esc(c.name&&c.name!==tag?'· '+c.name:'')}</div><div class="muted">Mil ${fmt(c.military_score)} · Prestige ${fmt(c.prestige)}${c.government?' · '+esc(c.government):''}</div></div></div>`}function renderWars(){document.getElementById('saveDate').textContent=data.date||'Unknown';document.getElementById('warCount').textContent=data.wars.length;document.getElementById('countryCount').textContent=Object.keys(data.countries).length;document.getElementById('lossCount').textContent=fmt(data.wars.reduce((a,w)=>a+w.casualties,0));document.getElementById('wars').innerHTML=data.wars.map((w,i)=>`<button class="warbtn ${i===current?'active':''}" onclick="current=${i};render()"><b>${esc(w.name)}</b><br><span class="muted">${esc(w.start_date||'Unknown start')} · ${fmt(w.casualties)} losses</span></button>`).join('')}function sortedBattles(w){const q=document.getElementById('filter').value.toLowerCase();let rows=[...w.battles].filter(b=>JSON.stringify(b).toLowerCase().includes(q));const [k,dir]=sort.split('-');rows.sort((a,b)=>{let av=k==='losses'?a.total_losses:(a[k]||''),bv=k==='losses'?b.total_losses:(b[k]||'');if(k==='date'){av=av||'9999.99.99';bv=bv||'9999.99.99'}return (av>bv?1:av<bv?-1:0)*(dir==='desc'?-1:1)});return rows}function showBattle(i){const b=sortedBattles(data.wars[current])[i];const d=document.getElementById('detail');const army=o=>Object.entries(o||{}).map(([k,v])=>`<div>${esc(k)}: <b>${fmt(v)}</b></div>`).join('')||'<span class="muted">No army composition in save</span>';d.innerHTML=`<button class="close" onclick="detail.classList.remove('open')">×</button><h2>${esc(b.name)}</h2><p class="muted">${esc(b.date||'Unknown date')} ${b.location?'· Province '+esc(b.location):''}</p><div class="army"><div class="mini"><b style="color:var(--blue)">${esc(b.attacker||'Unknown attacker')}</b><br>Commander: ${esc(b.attacker_leader||'Unknown')}<br>Losses: ${fmt(b.attacker_losses)}<hr>${army(b.attacker_army)}</div><div class="mini"><b style="color:var(--red)">${esc(b.defender||'Unknown defender')}</b><br>Commander: ${esc(b.defender_leader||'Unknown')}<br>Losses: ${fmt(b.defender_losses)}<hr>${army(b.defender_army)}</div></div><p><b>Winner:</b> ${esc(b.winner||'Unknown')}</p>`;d.classList.add('open')}function renderBattles(w){const rows=sortedBattles(w);document.getElementById('battleRows').innerHTML=rows.map((b,i)=>`<tr class="row" onclick="showBattle(${i})"><td><b>${esc(b.name)}</b><br><span class="muted">${esc(b.attacker||'Unknown')} vs ${esc(b.defender||'Unknown')}</span></td><td>${esc(b.date||'Unknown')}</td><td>${esc(b.attacker_leader||'Unknown')}<br><span class="muted">vs ${esc(b.defender_leader||'Unknown')}</span></td><td><b>${fmt(b.total_losses)}</b><br><span class="muted">${fmt(b.attacker_losses)} / ${fmt(b.defender_losses)}</span></td><td>${esc(b.winner||'Unknown')}</td></tr>`).join('')||'<tr><td colspan="5">No battle records found.</td></tr>'}function render(){renderWars();const w=data.wars[current]||{attackers:[],defenders:[],battles:[],casualties:0};document.getElementById('warName').textContent=w.name||'Unknown war';document.getElementById('warMeta').textContent=`Started ${w.start_date||'Unknown'} · ${w.battles.length} battles`;document.getElementById('lossPill').textContent=fmt(w.casualties)+' known losses';document.getElementById('attackers').innerHTML=w.attackers.map(nation).join('')||'<span class="muted">No attackers found</span>';document.getElementById('defenders').innerHTML=w.defenders.map(nation).join('')||'<span class="muted">No defenders found</span>';const al=w.attacker_losses||0,dl=w.defender_losses||0,total=Math.max(1,al+dl);document.getElementById('balanceBar').style.width=(al/total*100)+'%';document.getElementById('balanceText').textContent=`Attackers lost ${fmt(al)} · Defenders lost ${fmt(dl)}`;renderBattles(w)}document.getElementById('filter').addEventListener('input',()=>renderBattles(data.wars[current]));document.getElementById('sort').addEventListener('change',e=>{sort=e.target.value;renderBattles(data.wars[current])});document.querySelectorAll('th[data-sort]').forEach(th=>th.onclick=()=>{sort=th.dataset.sort;document.getElementById('sort').value=sort;renderBattles(data.wars[current])});render();
</script></body></html>""".replace("__DATA__", data)


def run_web_app(save_path: Path, game_dir: Optional[Path], port: int, open_browser: bool = True) -> None:
    analysis = analyze(save_path)
    art = ArtLoader([p for p in [game_dir, autodetect_game_dir()] if p])
    page = render_web_app(analysis, art).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"{APP_TITLE} running at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


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
                "battles": [dict(battle.__dict__, total_losses=battle.total_losses) for battle in w.battles],
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
    parser.add_argument("--web", action="store_true", help="Launch the modern browser UI")
    parser.add_argument("--port", type=int, default=8765, help="Port for --web (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open a browser for --web")
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

    if args.web:
        if not args.save:
            parser.error("--web requires a save path")
        run_web_app(args.save, args.game_dir, args.port, not args.no_browser)
        return 0

    app = AnalyzerApp(args.save, args.game_dir)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
