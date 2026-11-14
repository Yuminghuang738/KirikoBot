"""前端静态回归：独立好感度页已并入画像、超级大脑标题不带 (DSH)。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "KirikoBot"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_affection_page_is_gone_from_nav_and_router():
    html = _read("templates/dashboard.html")
    js = _read("static/js/app.js")
    assert 'data-page="affection"' not in html, "导航里不能再有好感度入口"
    assert "affectionHTML" not in js
    assert "bindAffection" not in js
    assert "loadPage('affection')" not in js


def test_old_affection_bookmark_redirects_to_profiles():
    js = _read("static/js/app.js")
    assert "location.hash==='#affection'" in js
    assert "loadPage('profiles')" in js


def test_dsh_title_has_no_suffix():
    html = _read("templates/dashboard.html")
    js = _read("static/js/app.js")
    assert "超级大脑 (DSH)" not in html
    assert "超级大脑 (DSH)" not in js
    assert "超级大脑" in js
