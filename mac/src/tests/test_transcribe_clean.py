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
