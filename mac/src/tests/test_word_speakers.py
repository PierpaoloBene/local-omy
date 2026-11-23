"""Speakers are assigned word by word, not per whisper line."""

from datetime import datetime, timedelta, timezone

T0 = "2026-10-28T18:00:00.000Z"


def _tok(text, a, b, p=0.9):
    return {"text": text, "offsets": {"from": a, "to": b}, "p": p}


def _item(words, text=None):
    return {"start": T0, "end": "2026-10-28T18:00:09.000Z", "language": "it",
            "text": text or " ".join(w[0] for w in words), "conf": 0.9, "words": words}


def _turn(name, a, b):
    return {"speaker": name, "label": name, "start": f"2026-10-28T18:00:{a:02d}.000Z",
            "end": f"2026-10-28T18:00:{b:02d}.000Z"}


def _w(text, a, b):
    return [text, a, b, 0.9]


def test_words_from_tokens():
    from omy.transcribe import _words
    toks = [_tok("[_BEG_]", 0, 0, 1.0), _tok(" Cia", 100, 200, 0.8), _tok("o,", 200, 300, 0.6),
            _tok(" come", 400, 600, 0.9), _tok("[_TT_50]", 600, 600, 1.0)]
    assert _words(toks) == [["Ciao,", 100, 300, 0.7], ["come", 400, 600, 0.9]]


def test_split_two_speakers_in_one_line():
    from omy.transcribe import _paragraphs
    it = _item([_w("Ciao", 0, 500), _w("come", 600, 1000), _w("va", 1100, 1500),
                _w("Bene", 2500, 3000), _w("grazie", 3100, 3600)])
    paras = _paragraphs([it], [_turn("Io", 0, 2), _turn("Marta", 2, 5)])
    assert [(t["speaker"], [i["text"] for i in items]) for t, items in paras] == \
        [("Io", ["Ciao come va"]), ("Marta", ["Bene grazie"])]
    assert paras[1][1][0]["start"] == "2026-10-28T18:00:02.500Z"
    assert paras[1][1][0]["conf"] == 0.9


def test_one_word_blip_is_smoothed():
    from omy.transcribe import _paragraphs
    it = _item([_w("Ho", 0, 400), _w("detto", 500, 900), _w("no", 1000, 1300),
                _w("a", 1400, 1700), _w("lui", 1800, 2200)])
    turns = [{"speaker": "Io", "label": "A", "start": "2026-10-28T18:00:00.000Z", "end": "2026-10-28T18:00:01.000Z"},
             {"speaker": "Marta", "label": "B", "start": "2026-10-28T18:00:01.000Z", "end": "2026-10-28T18:00:01.400Z"},
             {"speaker": "Io", "label": "A", "start": "2026-10-28T18:00:01.400Z", "end": "2026-10-28T18:00:05.000Z"}]
    paras = _paragraphs([it], turns)
    assert [t["speaker"] for t, _ in paras] == ["Io"]
    assert paras[0][1][0]["text"] == "Ho detto no a lui"


def test_nearest_turn_fallback():
    from omy.transcribe import _word_turn
    turns = [_turn("Io", 0, 2)]
    t = lambda s: datetime(2026, 10, 28, 18, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=s)
    assert _word_turn(turns, t(2.5), t(2.8))["speaker"] == "Io"
    assert _word_turn(turns, t(3.5), t(3.8)) is None


def test_items_without_words_keep_whole_line():
    from omy.transcribe import _paragraphs
    it = {"start": T0, "end": "2026-10-28T18:00:09.000Z", "language": "it", "text": "Ciao a tutti"}
    paras = _paragraphs([it], [_turn("Io", 0, 2), _turn("Marta", 2, 9)])
    assert [(t["speaker"], items) for t, items in paras] == [("Marta", [it])]


def test_item_rewritten_by_clean_falls_back():
    from omy.transcribe import _clean, _paragraphs
    it = _item([_w("no,", 0, 300), _w("no,", 400, 700), _w("no,", 800, 1100), _w("no,", 1200, 1500),
                _w("no,", 1600, 1900), _w("ok", 2500, 3000)])
    out = _clean([it])
    assert out[0]["text"] != it["text"] and "words" not in out[0]
    paras = _paragraphs(out, [_turn("Io", 0, 2), _turn("Marta", 2, 9)])
    assert len(paras) == 1
