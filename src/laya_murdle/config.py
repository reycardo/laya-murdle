"""Tunables. Defaults here, overridden by config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_FILENAME = "config.toml"


@dataclass
class FetchConfig:
    url: str = "https://murdle.com"
    timeout: float = 20.0
    render_timeout: float = 60.0
    fingerprint_wait_ms: int = 1000


@dataclass
class LayaConfig:
    # A clue with a lexical cue is read as negative unless Laya is this sure otherwise.
    negated_override: float = 0.90
    # A clue without one is read as positive unless Laya is this sure otherwise.
    affirmed_floor: float = 0.10


@dataclass
class SolverConfig:
    max_drops: int = 2


@dataclass
class GridConfig:
    name_width: int = 9
    yes: str = "✓"
    no: str = "·"
    unknown: str = " "


@dataclass
class GifConfig:
    seconds: float = 4.0
    final_hold: float = 2.5
    font: str = "/System/Library/Fonts/Menlo.ttc"
    font_size: int = 15
    wrap: int = 78
    margin: int = 20
    line_spacing: float = 1.9
    background: str = "#fdfdf7"
    title_color: str = "#b02020"
    grid_color: str = "#101010"


@dataclass
class OutputConfig:
    directory: str = "output"


@dataclass
class Config:
    fetch: FetchConfig = field(default_factory=FetchConfig)
    laya: LayaConfig = field(default_factory=LayaConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    grid: GridConfig = field(default_factory=GridConfig)
    gif: GifConfig = field(default_factory=GifConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    @property
    def output_dir(self) -> Path:
        return Path(self.output.directory)


def _candidates(path: Path | None) -> list[Path]:
    if path:
        return [path]
    return [
        Path.cwd() / CONFIG_FILENAME,
        Path(__file__).resolve().parents[2] / CONFIG_FILENAME,
    ]


def load_config(path: Path | None = None) -> Config:
    config = Config()
    for candidate in _candidates(path):
        if not candidate.is_file():
            continue
        data = tomllib.loads(candidate.read_text(encoding="utf-8"))
        for section in fields(config):
            target = getattr(config, section.name)
            known = {entry.name for entry in fields(target)}
            for key, value in (data.get(section.name) or {}).items():
                if key in known:
                    setattr(target, key, value)
        break
    return config
