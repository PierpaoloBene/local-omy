# Riferimento: maimaind (MVP second brain con Gemma, eliminato il 2026-11-23)

Codice copiato com'era dall'MVP per riusarlo nella pipeline unificata di local-omy.
**Non è importato né eseguito**: è materiale di riferimento per le fasi 1 e 2 e va rimosso quando
l'integrazione sarà completa.

| File | Cosa riusare |
|---|---|
| `brain/ingest.py` | lettura di `transcripts/AAAA-MM-GG.jsonl`, sync idempotente per hash di riga, canali (me/said/heard) e pesi |
| `brain/summarize.py`, `prompts/session_extract.md` | schema JSON vincolato per sessione (refs `P<n>`), controlli post-generazione |
| `brain/tools.py`, `brain/db.py`, `brain/embed.py` | FTS5 (unicode + trigram) + sqlite-vec, fusione RRF k=60, rerank top-30 |
| `prompts/ask_system.md`, `brain/ask.py` | prompt RAG con citazioni e risposta "non ho trovato" |
| `brain/vault.py` | vault Obsidian con blocchi generati `<!-- brain:begin X h=… -->` e rilevamento conflitti |
| `brain/dates.py` | intervalli di date relative in italiano |
| `brain/gate.py`, `brain/worker.py` | quando lavorare (inattività, batteria) e pausa di local-omy |
| `brain/docs.py`, `helpers/extract-main.swift` | indicizzazione documenti (PDF/OCR/Office): **fuori ambito**, tenuto solo per riferimento |
| `tests/`, `eval/questions.toml`, `brain/fixtures.py` | test con LLM finto e domande di valutazione su dati finti |
| `NOTE.md`, `PLAN.md` | numeri misurati e problemi incontrati (MTP drafter, `--cache-ram`, OOM GPU con due modelli) |
