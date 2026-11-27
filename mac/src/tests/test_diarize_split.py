"""diarize_session is split in two (docs/hub-migration.md, D4): the Mac decodes the audio and runs pyannote, the
hub names the voices. What the hub writes must be byte for byte what the one-machine path wrote."""

import json

import numpy as np

from helpers import write_segments


def test_hub_path_is_identical_to_the_local_one(archive, monkeypatch):
    from omy import diarize, transcribe
    ids = write_segments(archive)
    session = transcribe.find_session(ids[0])
    rng = np.random.default_rng(7)
    emb = {}
    for k in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02"):
        v = rng.normal(size=256).astype(np.float32)
        emb[k] = v / np.linalg.norm(v)
    result = {"turns": [(0.5, 12.25, "SPEAKER_01"), (13.0, 40.125, "SPEAKER_00"), (41.0, 60.0, "SPEAKER_02"),
                        (61.0, 70.5, "SPEAKER_01")],
              "embeddings": emb, "seconds": {"SPEAKER_00": 27.125, "SPEAKER_01": 21.75, "SPEAKER_02": 19.0}}
    diarize.save_profile(emb["SPEAKER_00"] + 0.1 * emb["SPEAKER_02"])       # you are close to SPEAKER_00
    path = diarize.speakers_path(session)

    monkeypatch.setattr(diarize, "decode_and_diarize", lambda s: result)
    local = diarize.diarize_session(session)
    expected_file = path.read_bytes()
    path.unlink()
    shutil_clusters = (diarize.CLUSTERS / f"{ids[0]}.npz")
    expected_npz = shutil_clusters.read_bytes()
    shutil_clusters.unlink()

    wire = json.loads(json.dumps(diarize.result_to_json(result)))           # what travels from the Mac
    hub = diarize.commit(session, diarize.result_from_json(wire))
    assert hub == local
    assert path.read_bytes() == expected_file
    assert np.load(shutil_clusters)["SPEAKER_00"].tolist() == np.load(__import__("io").BytesIO(expected_npz))["SPEAKER_00"].tolist()
    assert local["clusters"]["SPEAKER_00"]["name"] == diarize.my_name()
