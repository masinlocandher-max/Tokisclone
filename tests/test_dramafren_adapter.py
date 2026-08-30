from dramafren_adapter import (
    DramaRef,
    _episode_numbers_from_html,
    episode_url,
    parse_drama_ref,
)


def test_parse_drama_ref_from_watch_url():
    ref = parse_drama_ref(
        "https://dramabox.dramafren.org/index.php?ep=12&id=42000005228&lang=en&view=watch"
    )
    assert ref.drama_id == "42000005228"
    assert ref.lang == "en"


def test_episode_url():
    ref = DramaRef("42000005228", "en", "x")
    value = episode_url(ref, 64)
    assert "ep=64" in value
    assert "id=42000005228" in value


def test_episode_list_from_total_text():
    html = "<h1>Demo</h1><p>Total: 64 Eps</p><a href='?ep=1&id=42000005228'>Ep 1</a>"
    result = _episode_numbers_from_html(html, "42000005228")
    assert result[0] == 1
    assert result[-1] == 64
    assert len(result) == 64
