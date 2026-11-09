"""Зареждане и проверка на travels.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

SUPPORTED_HOSTS = ("cedadventures.com", "www.cedadventures.com")
GENDERS = {"мъж": "мъж", "мъже": "мъж", "мъжка": "мъж", "m": "мъж", "male": "мъж", "man": "мъж",
           "жена": "жена", "жени": "жена", "женска": "жена", "f": "жена", "female": "жена", "woman": "жена",
           "any": "any", "всеки": "any", "всички": "any", "*": "any"}


class ConfigError(Exception):
    pass


@dataclass
class TravelConfig:
    slug: str
    url: str
    name: str | None = None
    active: bool = True


@dataclass
class Settings:
    combination: str = "мъж"            # кого търсиш за споделена стая: мъж | жена | any
    notify_any_combination: bool = False  # известие и за комбинации с другия пол
    notify_new_dates: bool = True
    notify_price_changes: bool = True
    notify_last_seats: int = 2          # известие при <= N свободни места (0 = изключено)
    notify_available_again: bool = True
    notify_on_errors: bool = True
    repeat_alerts_daily: bool = False
    request_delay: float = 3.0


@dataclass
class Config:
    settings: Settings
    travels: list[TravelConfig] = field(default_factory=list)

    @property
    def active_travels(self) -> list[TravelConfig]:
        return [t for t in self.travels if t.active]


def slug_from_url(url: str) -> str | None:
    """`https://cedadventures.com/travels/yaponia/` -> `yaponia`."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in SUPPORTED_HOSTS:
        return None
    m = re.match(r"^/travels/([a-z0-9][a-z0-9\-_%]*)/?$", parsed.path, re.I)
    return m.group(1).lower() if m else None


def _bool(raw: dict, key: str, default: bool) -> bool:
    return bool(raw.get(key, default))


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Файлът {path} не съществува.")

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    s = raw.get("settings") or {}
    combo = str(s.get("combination", "мъж")).strip().lower()
    if combo not in GENDERS:
        raise ConfigError(f"settings.combination трябва да е „мъж“, „жена“ или „any“, а е „{combo}“.")
    try:
        last_seats = int(s.get("notify_last_seats", 2))
    except (TypeError, ValueError):
        raise ConfigError("settings.notify_last_seats трябва да е цяло число.") from None

    settings = Settings(
        combination=GENDERS[combo],
        notify_any_combination=_bool(s, "notify_any_combination", False),
        notify_new_dates=_bool(s, "notify_new_dates", True),
        notify_price_changes=_bool(s, "notify_price_changes", True),
        notify_last_seats=max(0, last_seats),
        notify_available_again=_bool(s, "notify_available_again", True),
        notify_on_errors=_bool(s, "notify_on_errors", True),
        repeat_alerts_daily=_bool(s, "repeat_alerts_daily", False),
        request_delay=float(s.get("request_delay", 3)),
    )

    items = raw.get("travels")
    if not isinstance(items, list) or not items:
        raise ConfigError("В travels.yaml няма списък 'travels'.")

    errors: list[str] = []
    travels: list[TravelConfig] = []
    seen: dict[str, int] = {}
    for i, item in enumerate(items, start=1):
        where = f"пътуване #{i}"
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            errors.append(f"{where}: очаква се линк или обект с url.")
            continue
        url = str(item.get("url") or "").strip()
        slug = slug_from_url(url) if url else None
        if not slug:
            errors.append(f"{where}: линкът трябва да е от вида "
                          f"https://cedadventures.com/travels/<име>/ ({url or '—'}).")
            continue
        if slug in seen:
            errors.append(f"{where}: {slug} вече е описано в пътуване #{seen[slug]}.")
            continue
        seen[slug] = i
        travels.append(TravelConfig(
            slug=slug,
            url=url if url.endswith("/") else url + "/",
            name=(str(item["name"]).strip() if item.get("name") else None),
            active=bool(item.get("active", True)),
        ))

    if errors:
        raise ConfigError("Грешки в travels.yaml:\n  - " + "\n  - ".join(errors))
    return Config(settings=settings, travels=travels)
