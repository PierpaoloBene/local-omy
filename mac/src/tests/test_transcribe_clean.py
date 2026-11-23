"""The day markdown hides the lines whisper is unsure of."""


def _it(start, text, conf=None):
    it = {"start": f"2026-10-28T18:{start}.000Z", "end": f"2026-10-28T18:{start[:2]}:59.000Z",
          "language": "it", "text": text}
    if conf is not None:
        it["conf"] = conf
    return it


def test_unsure_lines_become_one_unclear():
    from omy.transcribe import CONF_MIN, UNCLEAR, _clean
    out = _clean([
        _it("00:00", "Broccoli, fagiolini e carote.", 0.95),
        _it("01:00", "un'antanella di scena", CONF_MIN - 0.1),
        _it("02:00", "cerco tutto da noi", CONF_MIN - 0.2),
        _it("03:00", "Hai preso i broccoli?", CONF_MIN),
    ])
    assert [it["text"] for it in out] == ["Broccoli, fagiolini e carote.", UNCLEAR, "Hai preso i broccoli?"]
    assert UNCLEAR == "[incomprensibile]"
    assert out[1]["end"] == "2026-10-28T18:02:59.000Z"


def test_lines_without_confidence_are_kept():
    from omy.transcribe import _clean
    assert [it["text"] for it in _clean([_it("00:00", "Ciao a tutti, come va?")])] == ["Ciao a tutti, come va?"]


def test_sound_annotations_and_credits_are_dropped():
    from omy.transcribe import _clean
    out = _clean([
        _it("00:00", "*risate*"), _it("00:10", "[Musica]"), _it("00:20", "- *Risola* (applausi)"),
        _it("00:30", "Sottotitoli creati dalla comunità Amara.org"), _it("00:40", "Che ridere *ride* davvero"),
    ])
    assert [it["text"] for it in out] == ["Che ridere *ride* davvero"]


def test_loops_inside_a_line_are_cut():
    from omy.transcribe import _clean
    out = _clean([
        _it("00:00", "No, " * 50 + "basta!"),
        _it("01:00", "con Claude? Con Claude? Con Claude? Con Claude? L'account di Claude?"),
        _it("02:00", "sì, sì, sì, va bene"),
    ])
    assert out[0]["text"] == "No, No, No, basta!"
    assert out[1]["text"] == "con Claude? Con Claude? Con Claude? L'account di Claude?" or \
        out[1]["text"].count("laude?") == 4
    assert out[2]["text"] == "sì, sì, sì, va bene"


def test_speech_item_keeps_confidence():
    from datetime import datetime, timezone
    from omy.transcribe import _speech_item
    t0 = datetime(2026, 11, 21, 17, 0, tzinfo=timezone.utc)
    item = _speech_item(t0, {"from": 1500, "to": 3000, "language": "it", "text": "ciao", "conf": 0.42})
    assert item == {"start": "2026-11-21T17:00:01.500Z", "end": "2026-11-21T17:00:03.000Z",
                    "language": "it", "text": "ciao", "conf": 0.42}
    assert "conf" not in _speech_item(t0, {"from": 0, "to": 1, "language": "it", "text": "x", "conf": None})


def test_mean_conf_weighted_by_length():
    from omy.transcribe import _mean_conf
    assert _mean_conf([]) == 0.0
    assert abs(_mean_conf([{"conf": 1.0, "text": "aaa"}, {"conf": 0.0, "text": "a"}]) - 0.75) < 1e-9
