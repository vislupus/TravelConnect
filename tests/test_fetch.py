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
