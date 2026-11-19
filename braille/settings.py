"""Настройки исполнителя шагов: config/braille.json и переменные окружения BRAILLE_*.

Всё, что захочется крутить, не трогая логику (окна, таймауты, хосты, маски секретов), лежит
в config/braille.json; в коде остаются протокол языка шагов и допуски корректности. Разбор
шагов и самопроверка читают настройки без браузера и сети.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "braille.json"

#: переменные окружения, что переопределяют конфиг
ENV_HOSTS = "BRAILLE_ALLOWED_HOSTS"
ENV_API_FAIL_GRACE = "BRAILLE_API_FAIL_GRACE_MS"
ENV_OCCLUSION_GRACE = "BRAILLE_OCCLUSION_GRACE_MS"
ENV_FILES_DIR = "BRAILLE_FILES_DIR"


@dataclass(frozen=True)
class Settings:
    viewports: dict[str, dict[str, int]]
    allowed_hosts: tuple[str, ...]
    files_dir: Path
    timing_ms: dict[str, int]
    timing_s: dict[str, float]
    session: dict[str, int]
    masks: dict
    limits: dict[str, int]

    def ms(self, name: str) -> int:
        return self.timing_ms[name]

    def seconds(self, name: str) -> float:
        return self.timing_s[name]

    def mask_names(self) -> re.Pattern:
        return re.compile(self.masks["field_names"], re.I)

    def tuned(self, ms: dict[str, int] | None = None, s: dict[str, float] | None = None,
              ) -> Settings:
        """Копия с другими сроками: тесты ускоряют ожидание, не трогая файл настроек."""
        return replace(self, timing_ms={**self.timing_ms, **(ms or {})},
                       timing_s={**self.timing_s, **(s or {})})


def load(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    extra = [host for host in env.get(ENV_HOSTS, "").split(",") if host]
    timing = dict(raw["timing_ms"])
    for name, key in (("api_fail_grace", ENV_API_FAIL_GRACE),
                      ("occlusion_grace", ENV_OCCLUSION_GRACE)):
        if env.get(key):
            timing[name] = int(env[key])
    files = Path(env.get(ENV_FILES_DIR) or raw["files_dir"])
    return Settings(
        viewports=raw["viewports"],
        allowed_hosts=(*raw["allowed_hosts"], *extra),
        files_dir=files if files.is_absolute() else ROOT / files,
        timing_ms=timing, timing_s=raw["timing_s"], session=raw["session"],
        masks=raw["masks"], limits=raw["limits"],
    )
