# Interfacce del second brain (fissate, Fase 1)

Tre moduli scritti in parallelo. Ognuno rispetta queste firme; nessuno importa codice privato degli altri.

## 1. Sidecar JSON della nota — fonte di verità
Percorso: `NOTES/AAAA/MM/GG/<HHMM>-<slug>-<id8>.json` (stesso nome `.md` accanto), dove `id8` = ultimi 8 caratteri
dell'id sessione, `HHMM` ora locale di inizio, `slug` dal titolo (ascii, max 40). Trovare la nota di una sessione:
`organize.note_path(session_id) -> Path | None` (cerca `*-<id8>.json` nella cartella del giorno).

```jsonc
{
  "schema_version": 1,
  "id": "20261121T171433Z-aafa6412",          // = id sessione
  "data": "2026-11-21",                       // giorno locale
  "ora_inizio": "2026-11-21T18:14:33+01:00",  // locale con offset
  "ora_fine":   "2026-11-21T18:59:10+01:00",
  "luogo": {"name": "Casa"} | null,           // il più frequente nei paragrafi
  "file_sorgente": {"sessione": "transcripts/2026-11-21/<id>.json",
                    "giorno": "transcripts/2026-11-21.jsonl", "segmenti": ["<segment id>", ...]},
  "partecipanti": [{"label": "SPEAKER_01", "person": "me" | "<person id>" | null,
                    "nome_al_momento": "Rosalinda", "secondi": 312.4}],
  "paragrafi": {"P1": {"riga": "<id riga JSONL>", "inizio": "<iso locale>", "fine": "<iso>",
                       "label": "SPEAKER_01" | null, "media": false}},
  "titolo": "…", "qualita": "buona" | "parziale" | "rumore",
  "riassunto": "…",
  "parti": [{"da": "P1", "a": "P140", "riassunto": "…"}],   // una sola se la sessione è corta
  "punti_chiave":  [{"testo": "…", "refs": ["P3"], "conf": {"media": 0.93, "minima": 0.41}}],
  "entita": {"persone": [{"nome": "Davide", "person_id": null, "refs": [...], "conf": {...}}],
             "luoghi": [{"nome": …, "refs": …, "conf": …}], "organizzazioni": [...], "progetti": [...]},
  "impegni_e_todo": [{"chi": "Io", "chi_id": "me" | "<id>" | null, "verso_chi": "Rosalinda" | null,
                      "verso_chi_id": … | null, "cosa": "…", "scadenza_testo": "venerdì" | null,
                      "scadenza": "2026-11-27" | null, "stato": "aperto", "refs": [...], "conf": {...},
                      "uid": "<8 hex, stabile>"}],
  "date_ed_eventi_citati": [{"testo_data": "due giorni fa", "data": "2026-10-28" | null,
                             "evento": "…", "refs": [...], "conf": {...}}],
  "ascoltato": [{"riassunto": "…", "app": "YouTube" | null, "refs": [...]}],
  "tag": ["…"],
  "collegamenti": [{"nota": "<session id>", "motivo": "persona:<id>" | "progetto:<nome>" | "entita:<nome>"}],
  "confidenza": {"media": 0.91, "minima": 0.36},
  "da_rivedere": [{"campo": "impegni_e_todo[0]", "motivo": "confidenza 0.31 < 0.40"}],
  "elaborazione": {"modello": "Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp", "prompt_version": 2, "schema_version": 1,
                   "input_hash": "<sha256>", "creato": "<iso>", "durata_s": 312.5, "n_parti": 3}
}
```
Regole: i nomi nei campi testuali (`riassunto`, `testo`, `cosa`…) sono quelli visti dal modello; la sostituzione
con i nomi attuali avviene SOLO in fase di rendering (vault) usando `partecipanti[].nome_al_momento` → nome attuale
della `person`. Il sidecar non viene riscritto per un cambio nome. `collegamenti` li calcola il vault (non il modello)
e li scrive con `vault.update_links()`; l'organizer li lascia `[]` alla prima
elaborazione e, rielaborando, conserva quelli del vecchio sidecar finché `update_links` non li ricalcola.
Sidecar scritti sempre con `json.dumps(..., ensure_ascii=False, indent=2)`, ordine delle chiavi dello schema.

`uid` (impegni): generato una volta (`sha1(sessione|primo ref|cosa)[:8]`) e usato dal vault per l'id `^c<uid>`
(senza `uid`: formula vecchia, hash di sessione+`cosa`). Rielaborando, un impegno che coincide con uno vecchio
(stesso `chi_id`, o stesso `chi`, e `cosa` simile >= 0.8) eredita `stato` e `uid`. Il vault applica anche i
"destick": `- [ ]` in `impegni.md`/nota -> `stato` "aperto" (vince il file modificato più di recente).

`elaborazione.input_hash` NON contiene i nomi dei parlanti: per paragrafo (id riga, inizio, label cluster o
person id, media, testo) + prompt/schema version + nome modello (`models.model_path("organizer").stem`),
calcolato una volta all'inizio. Un rename di persona non rielabora. `organize.rehash(session_id) -> bool`
migra un sidecar vecchio al nuovo schema di hash (si fida del chiamante che il testo sia lo stesso; controlla
solo la struttura dei paragrafi).

## 2. Modelli (modulo `omy.models`, agente A)
```python
models.ensure(role: Literal["chat", "organizer"]) -> str   # base URL del llama-server pronto (carica/scarica)
models.release(role) -> None
models.state() -> dict                                     # {"loaded": role|None, "pid":…, "port":…}
class Preempted(Exception)                                 # sollevata da chi sta usando "organizer" se arriva una domanda
models.check_preempt() -> None                             # alza Preempted se c'è una richiesta chat pendente
```
Config modelli in `omy.models.MODELS` (path GGUF, flag llama-server; organizer: `--spec-type draft-mtp
--spec-draft-n-max 4 -c 16384 -fa on -ngl all --no-mmproj --jinja --cache-ram 1024`, porta 8090; chat porta 8091).

## 3. Client LLM (modulo `omy.brain.llm`, agente B)
```python
def chat(base_url: str, system: str, user: str, schema: dict, max_tokens: int = 1500) -> LLMResult
@dataclass class LLMResult: text: str; data: dict | None; tokens: list[dict]  # {"token","logprob","bytes"}
                            timings: dict; finish_reason: str
```
POST `/v1/chat/completions`, `response_format` json_schema, `logprobs: true, top_logprobs: 1`, temperature 0.1,
`chat_template_kwargs: {"enable_thinking": false}`, `cache_prompt: true`. Timeout 600 s. Chiamare
`models.check_preempt()` NON è compito del client: lo fa l'organizer tra una parte e l'altra; per interrompere a metà
il worker chiude il server (la richiesta fallisce con errore di connessione → `Preempted`).

## 4. Organizer (modulo `omy.brain.organize`, agente B)
```python
def needs_organize(session_id: str) -> bool        # nota assente o input_hash cambiato
def organize_session(session_id: str, base_url: str) -> Path   # scrive il sidecar, ritorna il path; checkpoint per parte in WORK/<id>/
def note_path(session_id: str) -> Path | None
def input_hash(session_id: str) -> str              # sha256(testo+label dei paragrafi, PROMPT_VERSION, SCHEMA_VERSION, modello)
```
Legge `ARCHIVE/transcripts/<giorno>.jsonl` (righe di quella sessione) e `transcripts/<giorno>/speakers/<id>.json`.

## 5. Vault (modulo `omy.brain.vault`, agente C)
```python
def render_note(session_id: str) -> Path            # Markdown della nota dal sidecar, nomi attuali
def render_entities(session_ids: list[str]) -> list[Path]   # pagine persone/luoghi/organizzazioni/progetti toccate
def render_indexes() -> None                        # giorni/, impegni.md, da-rivedere.md
def update_links(session_ids: list[str]) -> None    # collegamenti tra note (persona, progetto, entità)
def rerender_people(person_ids: list[str] | None) -> list[str]   # dopo rename/merge/delete: note + pagine coinvolte
```
Blocchi generati tra `<!-- omy:begin NAME h=<hash> -->` e `<!-- omy:end NAME -->`; se l'utente ha modificato un blocco
(hash diverso), non sovrascriverlo e segnalarlo in `cache/vault-conflicts.json`. Spunte `- [x]` in `impegni.md` sono
la verità per `stato` (letto e applicato al rendering, mai scritto nel sidecar dal vault salvo il campo `stato`).

## 6. Coda (modulo `omy.jobs` + `omy.worker`, agente A)
`jobs.enqueue(kind, key, priority=None)`, kinds: `transcribe`, `diarize`, `render`, `organize`, `index`.
Priorità: transcribe 10, diarize 20, render 30, organize 40, index 50. Il worker esegue:
`organize` → `models.ensure("organizer")` + `organize_session` (+ `render_note`, `update_links`, `render_entities`
alla fine del batch) solo se nessun transcribe/diarize è in coda o in corso, finestra 01:00–07:00 e alimentazione AC
(salvo `omy organize --since D [--force]`). `render` chiama `vault.rerender_people`.

## 7. Domande durante il lavoro pesante (prelazione)
- Il receiver (Fase 2, endpoint chat) chiama `models.request_chat()`: scrive/aggiorna la riga `demand(role='chat',
  last_at)` in `cache/jobs.sqlite`, poi attende `models.state()["loaded"] == "chat"` (poll 0,2 s, timeout 60 s).
  Ogni domanda aggiorna `last_at`.
- Il worker ha un thread di guardia (ogni 0,5 s): se c'è una domanda recente e il lavoro in corso è `organize` o
  `diarize`, lo interrompe SUBITO (organize: ferma il llama-server dell'organizer → `Preempted`; diarize: termina il
  sottoprocesso), rimette il lavoro in coda (`attempts` invariato), carica il modello chat.
- Il lavoro pesante riprende quando `now - last_at > CHAT_IDLE` (default 5 min, `config.toml: chat_idle_min`):
  scarica il chat, ricarica il modello del lavoro.
- Per questo `diarize` e `transcribe` girano come sottoprocessi (`python -m omy.cli job transcribe|diarize <id>`), uno
  per sessione; whisper non viene interrotto (pochi GB, convive con il 4B), pyannote sì.
- Di giorno, a riposo, il worker tiene caricato il modello chat; lo scarica durante `diarize` e lo ricarica dopo.

# Fase 2: indice e scheda "Chiedi"

## 8. Indice (modulo `omy.brain.index`, agente D)
SQLite `ARCHIVE/cache/brain-index.sqlite` (WAL). Un **chunk** è l'unità di ricerca:
```
chunks(id INTEGER PK, kind TEXT  -- 'nota' (riassunto, punto chiave, impegno, data, entità di una nota) | 'testo' (finestra di paragrafi della trascrizione)
       session TEXT, day TEXT, start TEXT  -- iso locale del primo paragrafo citato
       t REAL       -- secondi dall'inizio sessione
       row_id TEXT  -- id riga JSONL del primo paragrafo ("<session>/<n>"), per aprire la conversazione lì
       persons TEXT -- JSON: person id coinvolti ("me" incluso)
       title TEXT   -- titolo della nota (o "HH:MM, <partecipanti>" se non c'è nota)
       field TEXT   -- 'riassunto'|'punto'|'impegno'|'data'|'entita'|'testo'
       text TEXT, source_hash TEXT)
chunks_fts USING fts5(text, title, content='chunks', tokenize='unicode61 remove_diacritics 2')
vectors(chunk_id PK, vec BLOB)   -- float16, normalizzati, EMBED_DIM=512 (troncamento MRL di Qwen3-Embedding-0.6B)
meta(key PK, value)              -- 'version' incrementata a ogni update (il receiver ricarica la matrice)
```
- `index.update(sessions: list[str] | None = None) -> dict`: incrementale per `source_hash` (sidecar o righe JSONL);
  i chunk `testo` esistono anche per sessioni non ancora organizzate (finestre di ~600 caratteri, a confine di paragrafo).
- `index.search(query: str, *, day_from: str|None, day_to: str|None, persons: list[str]|None, k: int = 8) -> list[Hit]`:
  ibrida BM25 (FTS5) + coseno (numpy sulla matrice in memoria), fusione RRF k=60, filtri su giorno e persone,
  preferenza per chunk `nota` a parità. `Hit` = dict con tutti i campi del chunk + `score`.
- Embedding: `omy.brain.embed.embed(texts: list[str], query: bool) -> np.ndarray` (llama-server `--embedding
  --pooling last`, porta 8092, modello `ARCHIVE/models/brain/Qwen3-Embedding-0.6B-Q8_0.gguf`; per le query il prefisso
  `Instruct: Dato una domanda, trova i passaggi dell'archivio personale che rispondono\nQuery: `).
- `models.ensure("embed")`: ruolo **compagno**, può stare insieme a "chat" (≈0,7 GB) ma mai con "organizer" né durante
  diarize. Il worker carica embed per il job `index` (dopo il batch notturno e dopo ogni transcribe/diarize per i
  chunk `testo` nuovi), e lo tiene di giorno insieme al chat.

## 9. Domande (modulo `omy.brain.ask` + endpoint, agente E)
`POST /v1/ask` (auth Bearer come le altre). Richiesta:
```json
{"question": "Cosa ho promesso a Marco la settimana scorsa?",
 "history": [{"role": "user", "content": "…"}, {"role": "assistant", "content": "…"}],   // ultimi ≤ 4 turni
 "now": "2026-11-23T14:30:00+01:00"}                                                    // opzionale, ora del telefono
```
Risposta `text/event-stream`, un evento JSON per riga `data: {...}`:
```
{"type":"status","stage":"waking"|"searching"|"answering"}      // waking = il worker sta caricando il modello chat
{"type":"citations","items":[Citation, ...]}                     // prima del testo
{"type":"delta","text":"…"}                                      // testo in streaming; cita come [1], [2]
{"type":"done","not_found":false,"timings":{"wait_model_ms":0,"retrieval_ms":120,"ttft_ms":1800,"total_ms":3400}}
{"type":"error","message":"…"}
```
`Citation = {"n": 1, "kind": "nota"|"testo", "session": "<id>", "day": "2026-11-21", "time": "18:22",
"t": 512.0, "row_id": "<session>/<n>", "title": "…", "snippet": "≤ 200 caratteri"}`.
Pipeline: date relative della domanda risolte in codice (`omy.brain.dates`: oggi, ieri, settimana scorsa, martedì,
a ottobre, il 3 novembre…) → filtro giorni; nomi di persone noti (people.json, my_aliases, fuzzy) → filtro/boost
persone; `index.search` (k=8, contesto ≤ ~1.200 token) → se nessun hit sopra soglia: risposta "Non ho trovato nulla
nell'archivio su questo." senza modello; altrimenti `models.request_chat()` + llama-server streaming con prompt
`prompts/ask.md` (risposta breve, italiano, solo dalle fonti, cita [n], se le fonti non bastano lo dice).
Ogni domanda aggiorna la domanda chat (prelazione, §7). Log di ogni domanda con i timings in
`ARCHIVE/cache/ask-log.jsonl` (senza testo della risposta).

`GET /v1/brain/note/{session}`: il sidecar JSON della nota (404 se non c'è) per la vista nota nell'app.

## 10. App iPhone, scheda "Chiedi" (agente F)
Quinta scheda in `app_shell.dart` (icona `CupertinoIcons.chat_bubble_2`), stesso stile (`common.dart`). Chat con
storico della sessione (in memoria), risposte in streaming (SSE via `http` client `send()` + stream di linee),
stato "sto svegliando il modello…/cerco…". Citazioni [n] come chip toccabili sotto la risposta: tocco → apre
`ConversationViewer` sul paragrafo `row_id` della sessione (via `conversations(day:)`), pressione lunga → vista
nota (`GET /v1/brain/note/{session}`: titolo, riassunto, punti chiave, impegni). Sotto ogni risposta, piccolo, il
tempo totale misurato sul telefono (dall'invio al `done`) e il primo token. Nessun nuovo target né nuova app.
