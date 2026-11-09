from tracker.parser import ParseError, make_key, parse_combo, parse_heading, parse_seats, parse_travel


def test_japan_page(fixture_html):
    page = parse_travel(fixture_html("yaponia"))
    assert page.title == "Класическа Япония"
    assert page.image and page.image.startswith("https://cedadventures.com/wp-content/uploads/")
    assert len(page.departures) == 6

    by_key = {d.key: d for d in page.departures}
    sakura = by_key[make_key("2027-04-08", "2027-04-22", "ЯПОНИЯ САКУРА")]
    assert sakura.price == 3930 and sakura.currency == "EUR"
    assert sakura.action == "ЗАПИШИ СЕ"
    assert sakura.seats == 1 and sakura.available is True
    assert sakura.seats_text == "1 СВОБОДНО МЯСТО"
    assert sakura.combo == "жена"
    assert sakura.combo_text == "възможност за комбинация с жена"
    assert sakura.post_id == "266" and sakura.in_grid

    new_group = by_key[make_key("2027-03-21", "2027-04-04", "ЯПОНИЯ")]
    assert new_group.seats is None and new_group.available is True
    assert new_group.badges == ["НОВА ГРУПА"]
    assert new_group.combo is None

    full = by_key[make_key("2026-11-03", "2026-11-18", "ЯПОНИЯ МОМИДЖИ")]
    assert full.action == "РЕЗЕРВИ" and full.available is False and full.seats == 0
    assert full.price == 3795

    four = by_key[make_key("2027-04-07", "2027-04-21", "ЯПОНИЯ САКУРА")]
    assert four.seats == 4

    # сортирани по дата на тръгване
    assert [d.start_date for d in page.departures] == sorted(d.start_date for d in page.departures)


def test_namibia_variants_and_badges(fixture_html):
    page = parse_travel(fixture_html("yuar-namibiya"))
    assert len(page.departures) == 8
    assert all(d.badges == ["ПОТВЪРДЕНА ГРУПА"] for d in page.departures)
    assert all(d.available is True for d in page.departures)
    names = {d.name for d in page.departures}
    # запетаята в края на „…С ОПЦИЯ КЕЙПТАУН,“ е почистена
    assert "ЮАР И НАМИБИЯ С ОПЦИЯ КЕЙПТАУН" in names
    combos = [d for d in page.departures if d.combo]
    assert len(combos) == 1 and combos[0].combo == "жена" and combos[0].price == 4300


def test_new_zealand_unspecified_gender(fixture_html):
    page = parse_travel(fixture_html("nova-zelandiya-samoa"))
    assert len(page.departures) == 2
    assert all(d.combo == "any" for d in page.departures)
    assert all(d.combo_text == "възможност за комбинация" for d in page.departures)
    assert {d.price for d in page.departures} == {4790, 6040}
    # „За комбинация *“ не е бадж
    assert all(not d.badges for d in page.departures)


def test_guatemala(fixture_html):
    page = parse_travel(fixture_html("gvatemala-beliz-honduras"))
    assert len(page.departures) == 3
    avail = [d for d in page.departures if d.available]
    assert len(avail) == 1 and avail[0].seats == 1 and avail[0].combo == "жена"


def test_parse_heading_variants():
    d = parse_heading("08.04.2027 - 22.04.2027 >> ЯПОНИЯ САКУРА >> 3930 EUR >> ЗАПИШИ СЕ (възможност за комбинация с мъж)")
    assert d.combo == "мъж" and d.action == "ЗАПИШИ СЕ" and d.start_date == "2027-04-08"
    d = parse_heading("1.5.2027 – 9.5.2027 >> ГРУЗИЯ >> 1 250,50 € >> РЕЗЕРВИ")
    assert d.start_date == "2027-05-01" and d.end_date == "2027-05-09"
    assert d.price == 1250.5 and d.currency == "EUR" and d.action == "РЕЗЕРВИ"
    d = parse_heading("10.10.2027 - 20.10.2027 >> ПЕРУ >> 2.990 EUR")
    assert d.price == 2990 and d.action == "—"
    assert parse_heading("Програма") is None
    assert parse_heading("ИМА МЕСТА") is None


def test_parse_combo():
    assert parse_combo("ЗАПИШИ СЕ (възможност за комбинация с жена)") == ("жена", "възможност за комбинация с жена")
    assert parse_combo("ЗАПИШИ СЕ (възможност за комбинация с мъж)") == ("мъж", "възможност за комбинация с мъж")
    assert parse_combo("ЗАПИШИ СЕ (Възможност за комбинация със жена)")[0] == "жена"
    assert parse_combo("ЗАПИШИ СЕ (възможност за комбинация)") == ("any", "възможност за комбинация")
    assert parse_combo("ЗАПИШИ СЕ") == (None, None)


def test_parse_seats():
    assert parse_seats("ИМА МЕСТА") == (None, True)
    assert parse_seats("НЯМА МЕСТА") == (0, False)
    assert parse_seats("4 СВОБОДНИ МЕСТА") == (4, True)
    assert parse_seats("1 СВОБОДНО МЯСТО") == (1, True)
    assert parse_seats("ПОСЛЕДНО МЯСТО") == (1, True)
    assert parse_seats("ПОСЛЕДНИ МЕСТА") == (None, True)
    assert parse_seats("НОВА ГРУПА") == (None, None)


def test_form_only_departure_is_picked_up(fixture_html):
    """Дата, която е само във формата (мрежата е ограничена), също се чете."""
    html = fixture_html("yaponia").replace(
        '<input type="radio" name="_programs_form" value="21.03.2027 - 04.04.2027',
        '<input type="radio" name="_programs_form" value="01.06.2027 - 15.06.2027 &gt;&gt; ЯПОНИЯ ЛЯТО &gt;&gt; 3500 EUR &gt;&gt; ЗАПИШИ СЕ">'
        '<input type="radio" name="_programs_form" value="21.03.2027 - 04.04.2027', 1)
    page = parse_travel(html)
    extra = [d for d in page.departures if d.name == "ЯПОНИЯ ЛЯТО"]
    assert len(extra) == 1
    assert extra[0].price == 3500 and extra[0].in_grid is False and extra[0].available is True


def test_not_a_travel_page():
    try:
        parse_travel("<html><body><h1>404</h1></body></html>")
    except ParseError:
        pass
    else:
        raise AssertionError("очакваше се ParseError")


def test_travel_page_without_dates_is_ok():
    page = parse_travel("<html><body><h2>Дати на програмата</h2><p>No data was found</p></body></html>")
    assert page.departures == []
