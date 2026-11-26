"""HTTP заявки към сайта.

Сайтът е WordPress и засега отговаря на обикновени заявки, но за всеки случай
се пробват няколко начина по ред (както в другите тракери): първият успешен
се запомня и се ползва за останалите страници.

  1. обикновен requests с браузърски User-Agent
  2. curl_cffi, имитиращ Chrome изцяло (TLS + HTTP/2 + заглавки)
  3. curl_cffi, имитиращ Safari
  4. истински браузър (Chromium през Playwright)

Някои защити връщат страницата „провери дали си робот“ с HTTP 200. Затова
Fetcher може да получи функция accept(html) – ако отговорът не мине през нея,
се пробва следващият начин, а ако никой не успее, грешката казва какво точно
е върнал сайтът.

Сайтът има такава защита („SuperJS check“ на хостинга): първата заявка се
пренасочва към страница със скрипт, който слага бисквитка и връща обратно.
Само браузър може да го изпълни, затова четвъртият начин отваря страниците
в Chromium без прозорец. Той тръгва чак когато другите не успеят.
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlsplit

import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # локално без curl_cffi – остава само requests
    cffi_requests = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # без playwright – остават горните начини
    sync_playwright = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """Сайтът отказа достъп – има смисъл да се пробва друг начин."""


class UnexpectedPageError(FetchError):
    """HTTP 200, но не е страницата, която очакваме (проверка за робот, поддръжка…)."""

    def __init__(self, message: str, html: str):
        super().__init__(message)
        self.html = html


def describe_page(html: str) -> str:
    """Кратко описание на непозната страница – за съобщението за грешка."""
    parts = [f"{len(html)} знака"]
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m and m.group(1).strip():
        parts.append(f"заглавие „{' '.join(m.group(1).split())[:80]}“")
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>|<[^>]+>", " ", html)
    text = " ".join(text.split())
    if text:
        parts.append(f"текст „{text[:120]}“")
    return ", ".join(parts)


def describe_block(resp) -> str:
    h = {k.lower(): v for k, v in resp.headers.items()}
    parts = [f"HTTP {resp.status_code}"]
    if h.get("server"):
        parts.append(f"server={h['server']}")
    for key in ("cf-mitigated", "x-iinfo", "x-datadome"):
        if key in h:
            parts.append(f"{key}={h[key][:40]}")
    m = re.search(r"<title[^>]*>(.*?)</title>", resp.text or "", re.S | re.I)
    if m:
        parts.append(f"title='{' '.join(m.group(1).split())[:60]}'")
    return ", ".join(parts)


class _RequestsStrategy:
    name = "requests"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
        })

    def get(self, url, timeout):
        return self.session.get(url, timeout=timeout)


class _CffiStrategy:
    def __init__(self, browser: str):
        self.name = f"curl_cffi/{browser}"
        self.session = cffi_requests.Session(impersonate=browser)
        self.session.headers.update({"Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8"})

    def get(self, url, timeout):
        return self.session.get(url, timeout=timeout, allow_redirects=True)


class _BrowserResponse:
    def __init__(self, status_code: int, text: str, headers: dict):
        self.status_code, self.text, self.headers = status_code, text, headers


class _BrowserStrategy:
    """Chromium без прозорец. Пуска се при първата заявка и се затваря с close()."""

    name = "браузър (Chromium)"
    challenge_wait = 30  # секунди за скрипта на проверката

    def __init__(self, accept=None, launch_options: dict | None = None):
        self.accept = accept
        self.launch_options = dict(launch_options or {})
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        if proxy and "proxy" not in self.launch_options:
            self.launch_options["proxy"] = {"server": proxy}
        self._pw = self._browser = self._page = None

    def _start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(**self.launch_options)
        context = self._browser.new_context(locale="bg-BG")
        context.route("**/*", self._route)
        self._page = context.new_page()
        self._docs = []
        self._page.on("response", self._on_response)

    def _route(self, route):
        """Трябват само страницата и скриптовете на самия сайт – без снимки,
        шрифтове, стилове и чужди броячи (по-бързо и по-леко за сайта)."""
        req = route.request
        own = urlsplit(req.url).hostname == urlsplit(req.frame.url or req.url).hostname
        main_doc = req.resource_type == "document" and req.frame == self._page.main_frame
        if main_doc or (own and req.resource_type in ("script", "xhr", "fetch")):
            route.continue_()
        else:
            route.abort()

    def _on_response(self, resp):
        if resp.request.resource_type == "document" and resp.frame == self._page.main_frame:
            self._docs.append(resp)

    @staticmethod
    def _read(resp):
        try:
            return resp.status, resp.text(), resp.all_headers()
        except Exception:  # noqa: BLE001 – страницата вече е сменена от скрипта на проверката
            return resp.status, "", {}

    def _passed(self) -> bool:
        try:
            return bool(self.accept(self._page.content()))
        except Exception:  # noqa: BLE001 – в момента тече навигация
            return False

    def get(self, url, timeout):
        if self._page is None:
            try:
                self._start()
            except Exception as e:  # noqa: BLE001 – най-често липсва „playwright install chromium“
                self.close()
                raise BlockedError(f"браузърът не тръгна – {str(e).strip().splitlines()[0][:150]}") from e
        self._docs.clear()
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        status, text, headers = self._read(resp)
        if status == 200 and self.accept is not None and not self.accept(text):
            # Вероятно проверката: изчакваме скрипта ѝ да върне браузъра на
            # истинската страница. Не я зареждаме втори път – проверката се
            # иска при всяко зареждане.
            deadline = time.monotonic() + self.challenge_wait
            while time.monotonic() < deadline and not self._passed():
                self._page.wait_for_timeout(500)
            if self._passed():
                # суровият HTML от сървъра, а ако не е достъпен – както го вижда браузърът
                _, raw, raw_headers = self._read(self._docs[-1]) if self._docs else (0, "", {})
                if self.accept(raw):
                    text, headers = raw, raw_headers
                else:
                    text = self._page.content()
        return _BrowserResponse(status, text, headers)

    def close(self):
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # noqa: BLE001
                    pass
        self._pw = self._browser = self._page = None


class Fetcher:
    def __init__(self, timeout: float = 40, log=print, accept=None):
        self.timeout = timeout
        self.log = log
        self.accept = accept
        self.strategies = [_RequestsStrategy()]
        if cffi_requests is not None:
            self.strategies += [_CffiStrategy("chrome"), _CffiStrategy("safari")]
        if sync_playwright is not None:
            self.strategies.append(_BrowserStrategy(accept))
        self.last_used: str | None = None

    def close(self) -> None:
        for strategy in self.strategies:
            if hasattr(strategy, "close"):
                strategy.close()

    def _once(self, strategy, url: str) -> str:
        last = "неизвестна грешка"
        for attempt in range(3):
            try:
                resp = strategy.get(url, self.timeout)
            except FetchError:
                raise
            except Exception as e:  # noqa: BLE001 – различни библиотеки, различни изключения
                last = f"Мрежова грешка: {e}"
                time.sleep(3 * (attempt + 1))
                continue
            code = resp.status_code
            if code == 200:
                return resp.text
            if code == 404:
                raise FetchError("Страницата не съществува (404) – линкът вероятно е сменен.")
            if code in (401, 403):
                raise BlockedError(describe_block(resp))
            if code in (429, 503) or code >= 500:
                last = describe_block(resp)
                time.sleep(5 * (attempt + 1))
                continue
            raise FetchError(f"Сайтът върна HTTP {code}.")
        if "HTTP 429" in last or "HTTP 503" in last:
            raise BlockedError(last)
        raise FetchError(last)

    def get(self, url: str) -> str:
        blocked = []
        unexpected: str | None = None
        for strategy in list(self.strategies):
            try:
                html = self._once(strategy, url)
            except BlockedError as e:
                blocked.append(f"{strategy.name}: {e}")
                continue
            if self.accept is not None and not self.accept(html):
                if unexpected is None:
                    unexpected = html
                continue
            if strategy is not self.strategies[0]:
                self.strategies.remove(strategy)
                self.strategies.insert(0, strategy)
            if self.last_used != strategy.name:
                self.log(f"Изтегляне чрез {strategy.name}")
                self.last_used = strategy.name
            return html
        if unexpected is not None:
            raise UnexpectedPageError(
                "Сайтът върна друга страница вместо пътуването – вероятно проверка „не си робот“, "
                f"поддръжка или сменен линк ({describe_page(unexpected)})"
                + "".join(f"; {b}" for b in blocked), unexpected)
        reasons = {b.split(": ", 1)[1] for b in blocked}
        detail = reasons.pop() if len(reasons) == 1 else " | ".join(blocked)
        if "cloudflare" in detail.lower() and "challenge" in detail.lower():
            raise BlockedError(f"Cloudflare поиска проверка „не си робот“ ({detail})")
        raise BlockedError(f"Сайтът блокира достъпа ({detail})")
