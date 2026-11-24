from omy import transcribe as T


def _turn(a, b, who):
    return {"start": f"2026-01-01T00:00:{a:02d}.000Z", "end": f"2026-01-01T00:00:{b:02d}.000Z", "speaker": who, "label": who}


def _item(words):
    return {"start": "2026-01-01T00:00:00.000Z", "end": "2026-01-01T00:00:20.000Z", "language": "it",
            "text": " ".join(w for _, w in words), "conf": 0.9, "words": words}


def test_line_with_two_speakers_is_split():
    words = [[i * 1000, f"a{i}"] for i in range(5)] + [[10000 + i * 1000, f"b{i}"] for i in range(5)]
    paras = T._paragraphs([_item(words)], [_turn(0, 9, "Io"), _turn(9, 20, "Anna")])
    assert [(t["speaker"], " ".join(i["text"] for i in its)) for t, its in paras] == [
        ("Io", "a0 a1 a2 a3 a4"), ("Anna", "b0 b1 b2 b3 b4")]
    assert paras[1][1][0]["start"] == "2026-01-01T00:00:10.000Z"


def test_short_flip_is_not_split():
    words = [[i * 1000, f"w{i}"] for i in range(10)]
    paras = T._paragraphs([_item(words)], [_turn(0, 4, "Io"), _turn(4, 6, "Anna"), _turn(6, 20, "Io")])
    assert len(paras) == 1 and paras[0][0]["speaker"] == "Io" and paras[0][1][0]["text"] == _item(words)["text"]


def test_line_without_words_keeps_old_behaviour():
    it = {**_item([]), "text": "ciao a tutti"}
    del it["words"]
    paras = T._paragraphs([it], [_turn(0, 20, "Io")])
    assert paras[0][0]["speaker"] == "Io" and paras[0][1] == [it]


def test_conf_below_threshold_becomes_unclear_without_words():
    it = {**_item([[0, "x"]]), "conf": 0.1}
    out = T._clean([it])
    assert out[0]["text"] == T.UNCLEAR and "words" not in out[0]
