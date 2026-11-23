import json
import sys
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench  # noqa: E402
import dates  # noqa: E402

REF = datetime(2026, 11, 23, 10, 0)  # a Monday
SCHEMA = json.loads((Path(bench.HERE) / "schema.json").read_text())

OUTPUT = {"riassunto": "Parlate di una cena.", "punti_chiave": [{"testo": "Cena sabato", "refs": ["P1"]}],
          "entita": {"persone": [{"nome": "Anna", "refs": ["P2"]}], "luoghi": [], "organizzazioni": [], "progetti": []},
          "impegni_e_todo": [{"chi": "Io", "cosa": "mandare il preventivo", "verso_chi": "Anna",
                              "scadenza_testo": "venerdì prossimo", "refs": ["P2"]}],
          "date_ed_eventi_citati": [{"testo_data": "domani", "evento": "cena", "refs": ["P1"]}], "tag": ["cena"]}


class Fake(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tokenize":
            out = {"tokens": list(range(len(body["content"].split())))}
        else:
            Fake.seen.append(body)
            text = json.dumps(OUTPUT, ensure_ascii=False)
            toks = [{"token": text[i:i + 4], "logprob": -0.1, "bytes": list(text[i:i + 4].encode())}
                    for i in range(0, len(text), 4)]
            out = {"choices": [{"message": {"content": text}, "finish_reason": "stop", "logprobs": {"content": toks}}],
                   "usage": {"prompt_tokens": 1000},
                   "timings": {"prompt_n": 100, "cache_n": 900,
                               "prompt_per_second": 200.0, "predicted_n": 50, "predicted_per_second": 20.0,
                               "draft_n": 40, "draft_n_accepted": 30}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def seg():
    return {"id": "s1", "session": "x", "start": "2026-11-23T10:00:00+01:00", "day": "2026-11-23",
            "weekday": "lunedì", "my_name": "Io", "people": ["Anna"], "n_paragraphs": 3, "tokens": 10,
            "lines": ["[P1] 10:00 Io: domani cena", "[P2] 10:01 Anna: ok", "[P3] 10:02 Io: ciao"]}


def test_dates_fallback():
    f = lambda t: dates.fallback(t, REF)  # noqa: E731
    assert f("domani") == date(2026, 11, 24)
    assert f("dopodomani") == date(2026, 11, 25)
    assert f("ieri") == date(2026, 11, 22)
    assert f("oggi") == date(2026, 11, 23)
    assert f("venerdì prossimo") == date(2026, 11, 27)
    assert f("lunedì") == date(2026, 11, 30)
    assert f("martedì scorso") == date(2026, 11, 17)
    assert f("tra 3 giorni") == date(2026, 11, 26)
    assert f("tra due settimane") == date(2026, 12, 7)
    assert f("settimana prossima") == date(2026, 11, 30)
    assert f("boh") is None
    assert dates.resolve("", REF) is None


def test_validator_and_refs():
    assert bench.validate(SCHEMA, OUTPUT) == []
    bad = json.loads(json.dumps(OUTPUT))
    del bad["tag"]
    bad["punti_chiave"][0]["refs"] = ["Px"]
    assert len(bench.validate(SCHEMA, bad)) >= 2
    assert bench.refs_valid(OUTPUT, 3) and not bench.refs_valid(OUTPUT, 1)


def test_confidence_only_values():
    text = '{"chi":"Io","x":"zzz"}'
    toks = [{"token": c, "logprob": (-2.0 if c == "z" else -0.01), "bytes": [ord(c)]} for c in text]
    c = bench.confidence(text, toks)
    assert c["scope"] == "values" and c["min"] > 0.9 and c["n"] == 2


def test_vmmap_parse():
    out = bench.parse_vmmap("Physical footprint:         1.5G\nPhysical footprint (peak):  2048.0M\n")
    assert out == {"footprint_gb": 1.5, "footprint_peak_gb": 2.0}


def test_run_segment_and_prefix(tmp_path):
    srv, url = fake_server()
    try:
        row = bench.run_segment(url, seg(), SCHEMA, tmp_path / "o.json")
        assert row["json_ok"] and row["schema_ok"] and row["refs_ok"]
        assert row["acceptance"] == 0.75 and row["decode_tps"] == 20.0 and row["conf_scope"] == "values"
        sent = Fake.seen[-1]
        assert sent["logprobs"] is True and sent["chat_template_kwargs"] == {"enable_thinking": False}
        saved = json.loads((tmp_path / "o.json").read_text())
        assert saved["parsed"]["impegni_e_todo"][0]["scadenza_iso"] == "2026-11-27"
        assert saved["parsed"]["date_ed_eventi_citati"][0]["data_iso"] == "2026-11-24"
        pre = bench.prefix_test(url, [seg(), seg()], SCHEMA)
        assert pre["total_prompt_tokens"] == 1000 and pre["prefix_reused"] is True
    finally:
        srv.shutdown()


def test_report():
    rows = [{"label": "A", "kind": "segment", "prompt_tps": 100, "decode_tps": 10, "acceptance": 0.5, "wall_s": 3,
             "json_ok": True, "schema_ok": True, "refs_ok": False, "conf_mean": 0.9, "footprint_peak_gb": 5.0},
            {"label": "A", "kind": "prefix"}]
    out = bench.report(rows)
    assert "| A | 1 | 100.0 | 10.0 | 0.50 | 3.0 | 5.00 | 100 | 100 | 0 | 0.900 |" in out


def test_select_and_segments(tmp_path):
    rows = []
    for s in range(3):
        for i in range(40):
            rows.append({"session": f"s{s}", "start": f"2026-11-0{s + 1}T10:{i:02d}:00+01:00",
                         "speaker": "Io" if i % 2 else "Marco", "person": "me" if i % 2 else "p1", "via": None,
                         "text": "parola " * (30 + s * 20)})
    ses = {}
    for r in rows:
        ses.setdefault(r["session"], []).append(r)
    segs = bench.select_segments(ses, 3)
    assert len(segs) == 3 and all(s["lines"][0].startswith("[P1] ") for s in segs)
    assert all(s["tokens"] <= bench.MAX_TOKENS_SEG for s in segs)
