# brainbench

Benchmark dell'"organizer" locale: trascrizione -> nota JSON (schema in `schema.json`, prompt in `prompt.md`).
Parla con un `llama-server` GIÀ avviato (lo script non lo avvia e non carica modelli).
I dati personali stanno in `~/OmiArchive/cache/brainbench/` (fuori dal repo).

```
cd mac/src
python3 -I tools/brainbench/bench.py select [-n 5] [--url http://127.0.0.1:8090]   # segmenti da 2000-4000 token
python3 -I tools/brainbench/bench.py run --label "iq2xs-ista mtp3" --pid <pid llama-server> --repeat-prefix
python3 -I tools/brainbench/bench.py report                                        # tabella markdown
uv run pytest tools/brainbench -q                                                  # test (server finto)
```

- `run` usa `POST /v1/chat/completions` (json_schema, `logprobs`, thinking disattivato) e legge `timings`.
- Confidenza = media/minimo delle probabilità dei token dentro i valori di `chi`, `cosa`, `nome`, `testo_data`.
- Le date non le risolve il modello: `dates.py` aggiunge `scadenza_iso` / `data_iso` (usa `dateparser` se presente).
- Memoria: `vmmap --summary` (footprint e picco) + RSS massimo campionato ogni 0.5 s.
- Risultati: `results.jsonl`, output grezzi in `out/<label>/<segmento>.json`.
