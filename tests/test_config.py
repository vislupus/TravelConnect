import pytest

from tracker.config import ConfigError, load_config, slug_from_url


def write(tmp_path, text):
    p = tmp_path / "travels.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_slug_from_url():
    assert slug_from_url("https://cedadventures.com/travels/yaponia/") == "yaponia"
    assert slug_from_url("https://www.cedadventures.com/travels/Yuar-Namibiya") == "yuar-namibiya"
    assert slug_from_url("https://cedadventures.com/blog/") is None
    assert slug_from_url("https://example.com/travels/x/") is None
    assert slug_from_url("not a url") is None


def test_load_minimal(tmp_path):
    cfg = load_config(write(tmp_path, """
travels:
  - https://cedadventures.com/travels/yaponia/
  - url: https://cedadventures.com/travels/yuar-namibiya
    name: Намибия
    active: false
"""))
    assert cfg.settings.combination == "мъж"
    assert cfg.settings.notify_last_seats == 2
    assert [t.slug for t in cfg.travels] == ["yaponia", "yuar-namibiya"]
    assert cfg.travels[1].url.endswith("/") and cfg.travels[1].name == "Намибия"
    assert [t.slug for t in cfg.active_travels] == ["yaponia"]


def test_settings(tmp_path):
    cfg = load_config(write(tmp_path, """
settings:
  combination: жена
  notify_last_seats: 3
  notify_price_changes: false
  repeat_alerts_daily: true
travels: [https://cedadventures.com/travels/yaponia/]
"""))
    s = cfg.settings
    assert s.combination == "жена" and s.notify_last_seats == 3
    assert s.notify_price_changes is False and s.repeat_alerts_daily is True


def test_errors(tmp_path):
    with pytest.raises(ConfigError, match="combination"):
        load_config(write(tmp_path, "settings: {combination: котка}\ntravels: [https://cedadventures.com/travels/a/]"))
    with pytest.raises(ConfigError, match="няма списък"):
        load_config(write(tmp_path, "settings: {}"))
    with pytest.raises(ConfigError, match="линкът трябва"):
        load_config(write(tmp_path, "travels: [https://example.com/x/]"))
    with pytest.raises(ConfigError, match="вече е описано"):
        load_config(write(tmp_path, "travels: [https://cedadventures.com/travels/a/, https://cedadventures.com/travels/a]"))
    with pytest.raises(ConfigError, match="не съществува"):
        load_config(tmp_path / "missing.yaml")
