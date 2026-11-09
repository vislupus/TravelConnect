"""Четене на страница на пътуване в cedadventures.com.

Датите са в секцията „Дати на програмата“ – за всяка дата има заглавие от вида

    08.04.2027 - 22.04.2027 >> ЯПОНИЯ САКУРА >> 3930 EUR >> ЗАПИШИ СЕ (възможност за комбинация с жена)

плюс ред за местата („ИМА МЕСТА“, „4 СВОБОДНИ МЕСТА“, „1 СВОБОДНО МЯСТО“,
„НЯМА МЕСТА“) и по избор бадж („НОВА ГРУПА“, „ПОТВЪРДЕНА ГРУПА“, „За комбинация *“).

Същите заглавия са и опции във формата за резервация (radio бутони). Тази
секция понякога е по-пълна от мрежата с дати, затова се четат и двете и се
обединяват – мрежата дава местата и баджовете, формата допълва липсващи дати.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from bs4 import BeautifulSoup

GENDER_WORDS = {"мъж": "мъж", "мъже": "мъж", "жена": "жена", "жени": "жена"}

HEADING_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}\.\d{1,2}\.\d{4})\s*[-–—]+\s*(?P<end>\d{1,2}\.\d{1,2}\.\d{4})\s*"
    r">+\s*(?P<name>.*?)\s*>+\s*(?P<price>\d[\d\s.,]*?)\s*(?P<cur>EUR|€|EURO|ЕВРО|BGN|ЛВ\.?|лв\.?)"
    r"(?:\s*>+\s*(?P<tail>.*?))?\s*$",
    re.S | re.I,
)
COMBO_RE = re.compile(r"комбинац\w*(?:\s+(?:с|със)\s+(?P<gender>мъж\w*|жен\w*))?", re.I)
SEATS_RE = re.compile(r"(\d+)\s*СВОБОДН", re.I)


class ParseError(Exception):
    pass


@dataclass
class Departure:
    key: str                     # стабилен ключ: дати + вариант
    start_date: str              # ISO 8601 (2027-04-08)
    end_date: str
    name: str                    # вариантът на програмата („ЯПОНИЯ САКУРА“)
    price: float | None
    currency: str
    action: str                  # ЗАПИШИ СЕ | РЕЗЕРВИ | …
    heading: str                 # пълният текст на заглавието
    seats: int | None = None     # брой свободни места, ако е посочен
    seats_text: str | None = None
    available: bool | None = None  # True = има места, False = няма, None = неизвестно
    combo: str | None = None     # None | мъж | жена | any
    combo_text: str | None = None
    badges: list[str] = field(default_factory=list)
    post_id: str | None = None
    in_grid: bool = False        # True = видяно в мрежата с дати (а не само във формата)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TravelPage:
    title: str | None
    image: str | None
    description: str | None
    departures: list[Departure]


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _iso(d: str) -> str:
    day, month, year = d.split(".")
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _price(raw: str) -> float | None:
    """„3930“, „1 250,50“, „2.990“, „3,950“ -> число. Точка/запетая с точно три
    цифри след тях и без друг разделител е разделител за хиляди."""
    s = raw.replace(" ", "").replace("\u202f", "").replace("\xa0", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")   # 1.250,50
        else:
            s = s.replace(",", "")                     # 1,250.50
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    elif "." in s and re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _currency(raw: str) -> str:
    r = raw.upper().rstrip(".")
    if r in ("€", "EUR", "EURO", "ЕВРО"):
        return "EUR"
    if r in ("BGN", "ЛВ"):
        return "BGN"
    return r


def normalize_name(name: str) -> str:
    return re.sub(r"[^\wа-я]+", " ", name.lower(), flags=re.I).strip()


def make_key(start_iso: str, end_iso: str, name: str) -> str:
    return f"{start_iso}_{end_iso}_{normalize_name(name).replace(' ', '-')}"


def parse_combo(text: str) -> tuple[str | None, str | None]:
    """„(възможност за комбинация с жена)“ -> ('жена', текстът в скобите)."""
    m = COMBO_RE.search(text)
    if not m:
        return None, None
    gender = (m.group("gender") or "").lower()
    combo = "any"
    for prefix, g in GENDER_WORDS.items():
        if gender.startswith(prefix[:3]):
            combo = g
            break
    paren = re.search(r"\(([^()]*комбинац[^()]*)\)", text, re.I)
    return combo, _clean(paren.group(1) if paren else m.group(0))


def parse_heading(text: str) -> Departure | None:
    text = _clean(text)
    m = HEADING_RE.match(text)
    if not m:
        return None
    tail = _clean(m.group("tail") or "")
    combo, combo_text = parse_combo(tail)
    action = _clean(re.sub(r"\(.*?\)", "", tail)).strip(" .,-–") or "—"
    start, end = _iso(m.group("start")), _iso(m.group("end"))
    name = _clean(m.group("name")).strip(" ,;-–")
    return Departure(
        key=make_key(start, end, name),
        start_date=start, end_date=end, name=name,
        price=_price(m.group("price")), currency=_currency(m.group("cur")),
        action=action.upper(), heading=text, combo=combo, combo_text=combo_text,
    )


def parse_seats(text: str) -> tuple[int | None, bool | None]:
    t = _clean(text).upper()
    m = SEATS_RE.search(t)
    if m:
        n = int(m.group(1))
        return n, n > 0
    if "НЯМА" in t and "МЕСТ" in t or "ИЗЧЕРПАН" in t or "ЗАПЪЛНЕН" in t:
        return 0, False
    if "ПОСЛЕДН" in t and "МЯСТО" in t:
        return 1, True
    if "ИМА" in t and "МЕСТ" in t or "ПОСЛЕДН" in t and "МЕСТ" in t:
        return None, True
    return None, None


def _is_seats_text(text: str) -> bool:
    t = text.upper()
    return "МЕСТ" in t or "МЯСТО" in t or "ИЗЧЕРПАН" in t or "ЗАПЪЛНЕН" in t


def _grid_items(soup: BeautifulSoup):
    for item in soup.select(".jet-listing-grid__item"):
        heading = None
        for h in item.select(".elementor-heading-title"):
            dep = parse_heading(h.get_text(" ", strip=True))
            if dep:
                heading = (h, dep)
                break
        if not heading:
            continue
        h, dep = heading
        dep.post_id = item.get("data-post-id") or None
        dep.in_grid = True
        # Малките текстове под заглавието: места, баджове, бележка за комбинация
        for el in item.select(".elementor-heading-title, .jet-listing-dynamic-field__content"):
            if el is h:
                continue
            text = _clean(el.get_text(" ", strip=True))
            if not text or parse_heading(text):
                continue
            if _is_seats_text(text):
                dep.seats_text = text
                dep.seats, dep.available = parse_seats(text)
            elif "комбинац" in text.lower():
                if not dep.combo:
                    dep.combo, dep.combo_text = parse_combo(text)
                    if dep.combo and not dep.combo_text:
                        dep.combo_text = text
            elif text == text.upper() and len(text) <= 40:
                if text not in dep.badges:
                    dep.badges.append(text)
        if dep.available is None:
            # Няма ред за места – съдим по бутона
            if "РЕЗЕРВ" in dep.action:
                dep.available = False
            elif "ЗАПИШИ" in dep.action:
                dep.available = True
        yield dep


def _form_items(soup: BeautifulSoup):
    for inp in soup.select('input[name="_programs_form"]'):
        dep = parse_heading(inp.get("value") or "")
        if dep:
            if dep.available is None:
                dep.available = False if "РЕЗЕРВ" in dep.action else (True if "ЗАПИШИ" in dep.action else None)
            yield dep


def parse_travel(html: str, url: str | None = None) -> TravelPage:
    soup = BeautifulSoup(html, "html.parser")

    title = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = _clean(og["content"])
    elif soup.title and soup.title.string:
        title = _clean(soup.title.string)
    if title:
        title = re.split(r"\s+[-–|]\s+Пътешествия с истории", title)[0].strip()

    image = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        image = og_img["content"].strip()
    desc = None
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        desc = _clean(og_desc["content"])

    deps: dict[str, Departure] = {}
    for dep in _grid_items(soup):
        deps.setdefault(dep.key, dep)
    for dep in _form_items(soup):
        if dep.key not in deps:
            deps[dep.key] = dep

    if not deps:
        if "Дати на програмата" not in html and "_programs_form" not in html:
            raise ParseError("На страницата няма секция „Дати на програмата“ – "
                             "това не прилича на страница на пътуване.")
        # Секцията я има, но без нито една дата – легитимно празно (пътуване без обявени дати)

    ordered = sorted(deps.values(), key=lambda d: (d.start_date, d.end_date, d.name))
    return TravelPage(title=title, image=image, description=desc, departures=ordered)
