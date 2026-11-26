"""Fetcher: редуване на начините за изтегляне, когато сайтът върне друга страница."""
import pytest

from tracker.fetch import BlockedError, Fetcher, UnexpectedPageError
from tracker.parser import is_travel_page

URL = "https://cedadventures.com/travels/yaponia/"
CHALLENGE = "<html><head><title>One moment, please...</title></head><body><script>x()</script>Checking</body></html>"


class Resp:
    def __init__(self, text, status=200, headers=None):
        self.text, self.status_code, self.headers = text, status, headers or {}


class Strategy:
    def __init__(self, name, resp):
        self.name, self.resp, self.calls = name, resp, 0

    def get(self, url, timeout):
        self.calls += 1
        return self.resp


def make(*strategies, accept=is_travel_page):
    f = Fetcher(log=lambda m: None, accept=accept)
    f.strategies = list(strategies)
    return f


def test_challenge_with_http_200_falls_back_to_next_strategy(fixture_html):
    plain = Strategy("requests", Resp(CHALLENGE))
    chrome = Strategy("curl_cffi/chrome", Resp(fixture_html("yaponia")))
    f = make(plain, chrome)
    assert is_travel_page(f.get(URL))
    # успешният начин минава първи и следващата страница не пробва пак requests
    assert f.strategies[0] is chrome
    f.get(URL)
    assert (plain.calls, chrome.calls) == (1, 2)


def test_unexpected_page_everywhere_reports_what_came_back():
    f = make(Strategy("requests", Resp(CHALLENGE)), Strategy("curl_cffi/chrome", Resp(CHALLENGE)))
    with pytest.raises(UnexpectedPageError) as e:
        f.get(URL)
    assert "One moment, please..." in str(e.value) and "Checking" in str(e.value)
    assert "x()" not in str(e.value)
    assert e.value.html == CHALLENGE


def test_unexpected_page_wins_over_block_from_other_strategy():
    f = make(Strategy("requests", Resp(CHALLENGE)), Strategy("curl_cffi/chrome", Resp("no", 403)))
    with pytest.raises(UnexpectedPageError):
        f.get(URL)


def test_blocked_everywhere_is_still_blocked_error():
    f = make(Strategy("requests", Resp("no", 403)), Strategy("curl_cffi/chrome", Resp("no", 403)))
    with pytest.raises(BlockedError):
        f.get(URL)


def test_without_accept_any_200_is_returned():
    assert make(Strategy("requests", Resp(CHALLENGE)), accept=None).get(URL) == CHALLENGE


# ---- браузърът: проверката с JavaScript се минава в самата страница ----

class FakeDoc:
    def __init__(self, text, status=200):
        self._text, self.status = text, status

    def text(self):
        return self._text

    def all_headers(self):
        return {}


class FakePage:
    """goto връща проверката; след няколко питания „скриптът“ е заредил истинската страница."""

    def __init__(self, strategy, real, rounds=2):
        self.strategy, self.real, self.rounds = strategy, real, rounds
        self.gotos = self.polls = 0

    def goto(self, url, wait_until, timeout):
        self.gotos += 1
        doc = FakeDoc(CHALLENGE)
        self.strategy._docs.append(doc)
        return doc

    def content(self):
        self.polls += 1
        if self.polls <= self.rounds:
            return CHALLENGE
        if self.strategy._docs[-1].text() != self.real:
            self.strategy._docs.append(FakeDoc(self.real))
        return "<html><!-- както го вижда браузърът -->" + self.real + "</html>"

    def wait_for_timeout(self, ms):
        pass


def browser_with(real, rounds=2):
    from tracker.fetch import _BrowserStrategy
    b = _BrowserStrategy(is_travel_page)
    b._docs = []
    b._page = FakePage(b, real, rounds)
    return b


def test_browser_waits_for_js_check_and_returns_raw_page(fixture_html):
    real = fixture_html("yaponia")
    b = browser_with(real)
    resp = b.get(URL, 40)
    assert resp.status_code == 200 and resp.text == real
    assert b._page.gotos == 1      # без второ зареждане – то пак би получило проверката


def test_browser_gives_up_when_check_never_passes(fixture_html, monkeypatch):
    b = browser_with(fixture_html("yaponia"), rounds=10**9)
    monkeypatch.setattr(b, "challenge_wait", 0)
    assert b.get(URL, 40).text == CHALLENGE


def test_fetcher_uses_browser_after_plain_requests_get_the_check(fixture_html):
    real = fixture_html("yaponia")
    b = browser_with(real)
    f = make(Strategy("requests", Resp(CHALLENGE)), b)
    assert f.get(URL) == real
    assert f.strategies[0] is b


def test_browser_that_cannot_start_does_not_hide_what_the_site_returned(monkeypatch):
    from tracker.fetch import _BrowserStrategy
    b = _BrowserStrategy(is_travel_page)
    monkeypatch.setattr(b, "_start", lambda: (_ for _ in ()).throw(RuntimeError("Executable doesn't exist\nmore")))
    f = make(Strategy("requests", Resp(CHALLENGE)), b)
    with pytest.raises(UnexpectedPageError) as e:
        f.get(URL)
    assert "SuperJS" not in str(e.value) and "One moment" in str(e.value)
    assert "браузърът не тръгна – Executable doesn't exist" in str(e.value)
