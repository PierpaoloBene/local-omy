# Migrazione di local-omy sull'hub di casa

Stato: **implementato e distribuito in parallelo**, cutover da fare (vedi [cutover.md](cutover.md)). Branch `hub-migration`, base `main` (1c369d5).
Il piano è stato scritto in sola lettura sul codice del repo, su `~/OmiArchive` e sull'hub (`/srv/hub/repo`: `CLAUDE.md`,
`docs/architecture.md`, `compose.yaml`, `DESIGN.md`, `bootstrap/`); le sezioni 1-6 sono il piano com'era, **"Decisioni e stato" (sezione 7)
dice cosa è stato deciso e dove l'implementazione si discosta**. Dove non ero sicuro l'ho scritto ("da verificare").

## 0. Sintesi

**Modello "il Mac lavora per l'hub" (pull).** L'hub (Surface, Debian, Docker) diventa la fonte di verità: tiene il
ricevitore, l'archivio, la coda, le API di revisione per l'iPhone, il vault, l'indice testuale. Il Mac esegue un
**compute worker**: chiede lavoro all'hub, scarica l'audio che serve, esegue la pipeline ATTUALE senza modifiche
(whisper-cli, pyannote, llama-server 27B/4B/embedding) e rimanda i risultati. Mac spento = i lavori aspettano nella coda
dell'hub, l'iPhone continua a caricare, niente si perde.

Decisioni chiave (motivate nelle sezioni):

| # | Decisione |
|---|---|
| D1 | L'archivio sull'hub ha **lo stesso layout** di `~/OmiArchive` (`OMY_ARCHIVE=/data` = `/srv/hub/data/omy`). Il codice che lo legge (`sessions.py`, `transcribe.render_day`, `people.py`, `places.py`, `vault.py`, `index.py`, `review.py`) gira sull'hub così com'è. |
| D2 | Il compute worker **è** l'attuale `Worker` (`omy/worker.py`) con la sorgente dei lavori sostituita: `jobs.next_job()` → lease sull'hub. `allowed()`, guardia di preemption, `models.py`, rinnovo iPhone restano sul Mac. |
| D3 | I modelli non vanno mai sull'hub. L'unica parte "calcolo" di ogni job è eseguita sul Mac; commit, naming, rendering e coda sono sull'hub. |
| D4 | `diarize_session` si spezza in due (audio+pyannote sul Mac, `name_clusters`+scrittura sull'hub). È l'unico refactor di pipeline, e non cambia il modello. |
| D5 | Indice: chunk + FTS5 + ricerca sull'hub; i vettori (Qwen3-Embedding-0.6B) li calcola il Mac con un job `embed`. Senza Mac la ricerca ibrida degrada a solo BM25 (comportamento già previsto in `brain/index.py: search`, `EmbedError`). |
| D6 | "Chiedi": recupero sull'hub, generazione col 4B sul Mac. **Deciso (sezione 7): con il Mac spento la domanda è messa in CODA persistente** e la risposta arriva quando il Mac torna (non la risposta degradata prevista qui). |
| D7 | `~/OmiArchive` non è 18 GB di dati: **17 GB sono `models/`** (restano sul Mac). I dati veri sono ~1,2 GB (raw 1,1 GB, 1114 segmenti in 15 giorni). Cresce ~75 MB/giorno (~27 GB/anno): sui ~99 GB liberi dell'hub bastano ~3 anni, con avviso al 75% (regola di `CLAUDE.md` dell'hub). |
| D8 | Obsidian: **deciso**: vault master sull'hub e **copia in sola lettura sul Mac** (`~/OmiBrain`, rsync hub→Mac ogni 10 minuti, le modifiche fatte sul Mac sono sovrascritte). |
| D9 | iPhone: **nessuna reinstallazione**. L'URL si cambia dalle Impostazioni dell'app (`AppModel.saveMac`). Il token si può tenere invariato. |

Trappole trovate (vedi §3.4 e §5) che, non gestite, rovinano la migrazione:

1. **Fuso orario.** `omy_receiver/localtime.py: _system()` usa il fuso del Mac; `config.toml` non ha `timezone`. Sull'hub
   senza `OMY_TZ=Europe/Rome` i giorni cambierebbero confine, le sessioni verrebbero rigruppate e riscritte.
2. **Lingua dell'archivio.** `omy_receiver/l10n.py: _mac()` legge `defaults read -g AppleLanguages` (macOS). Su Linux
   ricadrebbe in inglese: "Persona 1" → "Person 1", "Io" → "Me". Serve `language = "it"` in `config.toml` dell'hub.
3. **Backup SQLite.** `bootstrap/backup/backup.sh` fa lo snapshot `.backup` solo dei file `*.db`; le code di omy sono
   `*.sqlite`. Vanno rinominate o inserite nel `find`.
4. **Pyannote/torch** è dipendenza obbligatoria in `mac/src/pyproject.toml`: non deve entrare nell'immagine dell'hub.
5. `receiver/app.py: main` forza `host="127.0.0.1"`: in Docker serve `network_mode: host` (come `bravia-remote` e `hub-api`),
   non `ports:`.

---

## 1. Inventario

"Dove": **H** = hub, **M** = Mac, **H+M** = entrambi. Per ogni voce: chi legge/scrive oggi e perché la collocazione.

### 1.1 Processi e componenti

| Componente (file) | Oggi legge / scrive | Dove | Perché |
|---|---|---|---|
| Ricevitore `omy_receiver/app.py: upload` (`POST /v1/segments/{id}`) | scrive `raw/audio/GIORNO/<id>.{bin,json}`, `receiver.json`, `tmp/` | **H** | È il punto di ingresso dell'iPhone; con l'archivio sull'hub il Mac può essere spento (requisito). Atomico e idempotente già oggi (hash, `os.replace`, 200 `duplicate`, 409 su hash diverso). |
| `/v1/settings` (`omy_receiver/features.py`) | `settings.json` + `.settings.lock` | **H** (+ copia di sola lettura sul Mac) | Fonte unica. L'app Mac legge `settings.json` direttamente (`app/Sources/LocalOmy/Features.swift: isOn`, usato da `AudioActivity.swift` per `mac_audio`): il compute worker ne tiene una copia in `~/OmiCompute/settings.json` a ogni poll. |
| API di revisione (`/v1/transcripts`, `/search`, `/conversations`, `/people`, `/voices`, `/unlabeled`, `/places`, `/brain/note`) | eseguono `omy --json ...` come sottoprocesso (`app.py: omy()`, timeout 600 s) | **H** | Sono solo lettura/scrittura di file dell'archivio (`review.py`, `people.py`, `places.py`) + lock `cache/.write.lock`. Nessun modello. Stesso codice nell'immagine dell'hub. |
| `/v1/voices/{sessione}/{label}/sample` (`people.play`) | legge i `.bin` della sessione, li decodifica (opuslib/libopus), scrive WAV in `cache/` | **H** | Serve solo `libopus0` (apt). Niente GPU. |
| `/v1/transcribe` ("Trascrivi ora") | `worker.heartbeat_fresh()`, `jobs.wake()`; fallback `fallback_transcribe()` lancia `omy transcribe` | **H** | Sull'hub accoda e sveglia lo scheduler; il fallback locale va **disattivato** (userebbe whisper). Risposta estesa con `mac_online` (campo opzionale). |
| `/v1/brain/status` | `worker.read_status()`, `jobs.summary()`, `models.state()` | **H** | Stesso JSON: `worker` e `models` vengono dallo stato che il compute worker invia a ogni lease/heartbeat (`POST /compute/v1/status`), così l'app e il menu non cambiano forma. |
| `/v1/ask` (`brain/ask.py: answer`) | indice, `models.request_chat()`, llama-server 4B | **H+M** | Vedi §1.3. |
| `sessions.py` (raggruppamento) | solo sidecar `.json` | **H** | Zero audio, già leggero. |
| Coda `jobs.py` (`cache/jobs.sqlite`) | tutti i processi la aprono | **H** (schema esteso con lease, §2) | La coda deve sopravvivere al Mac spento. |
| Tabella `demand` (ultima domanda "Chiedi") + `worker.wake` | `jobs.touch_demand`, `models.chat_active()` | **M** (db locale minuscolo in `~/OmiCompute/cache/jobs.sqlite`) | È un meccanismo locale al Mac per preemptare diarize/organize (`worker.guard_once`). |
| Scheduler/scan (`worker.py: Worker.scan`, `_scan_organize`) | `transcribe.pending_days/plan/_to_diarize`, `organize.needs_organize/input_hash` | **H** | Solo lettura di sidecar e JSON. Estratto in `omy/scan.py` (spostamento puro). `_to_diarize` oggi chiama `diarize.available()` (token HF o `models/hf`): sull'hub diventa "il compute worker dichiara `diarize`" (capabilities, §2.2). |
| Finestra e condizioni (`worker.allowed`, `organize_ok`, `in_window`, `on_ac` = `pmset`) | | **M** | Dipendono dal Mac (batteria, memoria, domande). Il Mac chiede all'hub solo i `kinds` che oggi accetterebbe (`kinds`/`forced_kinds` nel lease). Invariato `organize` solo 01-07 con coda trascrizioni vuota e alimentatore. |
| Rinnovo app iPhone (`worker.renew_iphone` → `iphone.py`) | Xcode, `devicectl`, `~/.config/local-omy`; `_last_segment_start()` legge `raw/audio` | **M** | Serve Xcode (resta sul Mac). `needs_reopen` oggi guarda l'archivio locale: passa a `GET /v1/status` dell'hub (`last_segment_start`). |
| `transcribe` (`transcribe.py: transcribe_session`) | segmenti `.bin` → Opus → WAV → whisper-cli (VAD, finestre 30 s, DTW) → `transcripts/GIORNO/<sessione>.json` | calcolo **M**, commit **H** | Pipeline 5 intoccata. Vedi §2.5. |
| `render_day` (md + jsonl del giorno), `_clean`, `_paragraphs`, `_record` | `transcripts/*.json`, `speakers/`, `places.json`, sidecar | **H** | Python puro. `transcribe_job` oggi chiama `render_day` dopo la trascrizione: nel compute worker si chiama `transcribe_session` direttamente (stessa funzione), il render lo fa l'hub al commit. |
| `diarize` (`diarize.py`) | audio sessione → pyannote (MPS) → turni+embedding; poi `name_clusters` con `voices/me.npy`, `people.json`, `clusters/*.npz`, `context/` | pyannote **M**, naming **H** | D4. Il naming è numpy puro e deve restare sotto `lock.writing()` insieme a rinomine di persone (commento in `diarize_session`: "a rename can't slip in between"): sull'hub è nello stesso processo/lock dei comandi di revisione. |
| `render` job (`vault.rerender_people`, poi `index all`) | vault, people | **H** | Solo file. |
| `organize` (`brain/organize.py: organize_session`) | righe del giorno (`DAY.jsonl`), `speakers/<s>.json`, `voices/people.json`, nota precedente (carry_over) → 27B → sidecar nota | modello **M**, input/commit **H** | Il Mac riceve uno "spazio di lavoro" con questi 4 input e restituisce il sidecar JSON. `needs_organize`/`input_hash` (che include `model_name()`, `PROMPT_VERSION`, `SCHEMA_VERSION`) li calcola l'hub: il nome del modello arriva dalle capabilities del Mac (§2.7). |
| Vault (`brain/vault.py`: `render_note`, `update_links`, `render_entities`, `render_indexes`, `finalize_batch`) | `brain/`, `cache/vault-index.json` | **H** | Solo file; unisce le modifiche manuali (blocchi `omy:begin/end`). |
| Indice (`brain/index.py`, `cache/brain-index.sqlite`: `chunks`, FTS5, `vectors`) | note + righe jsonl | chunk/FTS/ricerca **H**, vettori **M** | D5. 3.912 chunk oggi → 512 float32 = ~8 MB in RAM: banale per l'hub. |
| Embedding (`brain/embed.py`, llama-server `:8092`) | | **M** | È un modello. Sull'hub niente. |
| "Chiedi" (`brain/ask.py`) | vedi §1.3 | **H+M** | |
| Persone/voci (`people.py`, `voices/`) | `me.npy`, `people.json`, `people/*.npz`, `clusters/*.npz` | **H** (dati biometrici a riposo sull'hub, già cifrati da restic) | `rename`/`relabel`/`set_source` sono numpy + file; li usa l'iPhone via API. |
| `omy voice enroll` (`enroll.py`) | `voices/enroll/*.wav+txt` → pyannote (`diarize_wav`) → `me.npy` | **M**; `me.npy` risultante caricato sull'hub (`PUT /compute/v1/profile`) | Serve pyannote. I clip annotati a mano (11 MB, insostituibili) restano sul Mac **e** vanno copiati sull'hub per il backup. |
| Luoghi (`places.py`, `places.json`) | sidecar `location`, `places.json` | **H** | File + calcolo geografico. Mappe/indirizzi sono su iPhone (MapKit): invariato. |
| Batteria (`battery.py`) | sidecar `battery`, `firmware` | **H** | Solo sidecar. Nuovo `GET /v1/battery` per l'app Mac e il widget. |
| Contesto audio (`context.py`; scritto da `app/.../AudioActivity.swift`: `context/audio-GIORNO.jsonl`) | sorgente: il Mac (CoreAudio per processo) | scritto **M**, **copiato sull'hub** | Serve a `diarize` per `Persona 2 (Teams)` / `Media (Chrome)`. Il compute worker carica i file nuovi/cambiati (`PUT /compute/v1/context/GIORNO`, append-only con offset+hash) prima di restituire un diarize. Chiamate iPhone: già nei sidecar. |
| Impostazioni/funzioni (`features.py`) | `settings.json` | **H** | Vedi sopra. `auto_transcribe`, `diarization`, `voice_prints`, `location` agiscono sull'hub (scan, naming, render). |
| `receiver.json` | ultimo upload, `X-Queue-Remaining` | **H** | Alimenta `status` e il widget. |
| `status.py` (`omy status`, `brain_status`) | archivio, `worker.on_ac()` (`pmset`), `tailscale` | **H** (campi Mac vengono dal compute worker) | Su Linux `pmset` non esiste: `on_ac()` ritorna False → il campo va preso dallo stato inviato dal Mac. |
| CLI `omy` (`cli.py`) | | **H** (tutti i sottocomandi di lettura/scrittura) e **M** (`compute-worker`, `iphone`, `setup`, `voice enroll`) | Sul Mac i sottocomandi di dati passano a "modalità remota" (§6, PR 10). |
| App barra dei menu (`mac/src/app`, SwiftUI) | esegue `omy --json ...` (`Omy.swift: run`); legge direttamente `archive` (Stato), `settings.json`, `brain/` ("Apri vault"), scrive `context/` | **M** | Resta, vedi §6. |
| App iPhone nativa (`iphone/app`, installata: `iphone_app = "native"` in `config.toml`) e Flutter (`iphone/src`, legacy) | upload segmenti; API del ricevitore; URL in Impostazioni | **iPhone** | Rinnovo/installazione richiedono Xcode: resta sul Mac. |
| Modelli (`~/OmiArchive/models`: 15 GB `brain/`, 1,5 GB whisper, 868 KB VAD, 32 MB `hf/`) | | **M** | Non entrano nell'hub (8 GB RAM, niente GPU). |
| `cache/` (`ask-log.jsonl`, `worker.json`, `models*.json`, `organize/` checkpoint, `eval/`, `brainbench/`, `backup-pipeline4/`, `group-compare/`) | | `ask-log`, `worker.json` **H**; `organize/`, `models*.json` **M** | Gli altri sono esperimenti: non migrano. |
| `tools/` (`eval_transcribe.py`, `brainbench`) | audio annotato, modelli | **M** | Misura/benchmark. |
| `reference/maimaind/` | codice di riferimento | nessuno | Non esegue. |

### 1.2 Output per altre app (README "Output per altre app")

`transcripts/AAAA-MM-GG.jsonl` oggi è letto da chi? **Non lo so** (domanda 3). Dopo la migrazione il file vive sull'hub.
Proposta, due strade che coesistono:
- **App sull'hub**: mount di sola lettura `/srv/hub/data/omy/transcripts` nel loro container.
- **App sul Mac**: `GET /v1/export/days?since=<mtime>` e `GET /v1/export/GIORNO.jsonl` (stesso token iPhone, `If-Modified-Since`),
  più, se serve, uno specchio in `~/OmiExport/` aggiornato dal compute worker. Il contratto "rileggi i giorni con mtime più
  recente" del README resta valido.

### 1.3 "Chiedi" e ricerca, nel dettaglio

`ask.answer()` ha cinque fasi: (1) `jobs.touch_demand` (sveglia la preemption), (2) date e persone (`dates.question_range`,
`person_groups`: puro Python, `dateparser`), (3) `index.search` (BM25 + coseno con embedding della query), (4) `build_context` e
`build_messages`, (5) streaming dal 4B (`_stream_llama` su `models.request_chat()`).

Sull'hub girano 2-4. Sul Mac girano solo: l'embedding della query (1 chiamata, ~50 token) e la generazione. Due canali sul
compute token, aperti **dal Mac in uscita** (nessuna porta aperta sul Mac):

- `GET /compute/v1/interactive?wait=25`: long-poll. L'hub, quando arriva una domanda, mette nella coda in memoria una richiesta
  `{rid, kind: "embed_query"|"chat", payload}`; il Mac la prende, la esegue sul llama-server locale (con la stessa preemption di
  oggi: `touch_demand` + `request_chat`) e risponde con `POST /compute/v1/interactive/{rid}/chunks` (NDJSON, a pezzi
  durante lo streaming) e `.../end`.
- L'hub inoltra i chunk come SSE all'iPhone (stesso formato `status/citations/delta/done/error` di `AskEvent`: l'app non cambia).
- Latenza attesa: oggi primo testo 2,5-3 s via Tailscale (README); si aggiunge un salto hub↔Mac sulla tailnet (decine di ms) e
  l'attesa del long-poll già in corso (il Mac tiene sempre una richiesta pendente). Da misurare.
- Mac spento/lento: **sostituito dalla coda persistente** (decisione 2, sezione 7): l'hub risponde `status queued:<id>` + `done` e tiene la domanda
  (`omy/asks.py`); l'iPhone la mostra "in attesa del Mac" e la risposta arriva quando il Mac torna acceso.
- Alternativa scartata: l'hub che chiama direttamente un llama-server esposto dal Mac sulla tailnet (come descrive oggi
  `docs/architecture.md` dell'hub, "modelli via API"). Funzionerebbe ma contraddice la scelta "pull" e richiede di esporre
  llama-server/auth sul Mac. Resta come ripiego se il canale interattivo si rivela troppo complesso (PR 9 è isolabile).

### 1.4 Cosa NON cambia

Formato `.bin`/sidecar, protocollo di upload, API `/v1/*` verso l'iPhone (stessi path e JSON), pipeline di trascrizione
(`PIPELINE = 5`, `VAD_ARGS`, `WINDOW_S = 30`, `CHUNK = 30 min`, `CONF_MIN`, `DTW_ARGS`), diarizzazione (`MODEL`, soglie),
organizzatore (prompt, schema, `PROMPT_VERSION = 3`), modelli e argomenti llama-server (`models.py: MODELS`).

---

## 2. Protocollo hub ↔ Mac (compute worker)

Stesso processo uvicorn del ricevitore, router separato `omy_receiver/compute.py`, prefisso `/compute/v1/`.

### 2.1 Autenticazione e rete

- `tailscale serve --https=10000` pubblica `127.0.0.1:8787` solo sul tailnet (stesso schema di oggi sul Mac). Non `funnel`.
- Due token distinti in `/srv/hub/secrets/omy.env`: `OMY_TOKEN` (iPhone, `/v1/*`) e `OMY_COMPUTE_TOKEN` (Mac, `/compute/v1/*`), ≥32
  caratteri (regola di `load_token`). Confronto in tempo costante (`_authorized` esistente). Un token non vale sull'altro prefisso.
  Un terzo, `OMY_WIDGET_TOKEN`, vale solo per `GET /v1/widget` (dashboard).
- Il compute token sta sul Mac in `~/.config/local-omy/compute_token` (600). Mai nei log.
- `tailscale serve` non filtra per path né per nodo: per questo i token. Facoltativo (da valutare): ACL Tailscale che limitano la
  porta 10000 ai soli nodi iPhone e Mac.
- Header `X-Omy-Compute-Protocol: 1` su ogni chiamata; l'hub risponde 426 se non compatibile.

### 2.2 Endpoint

Tutti rispondono JSON salvo gli input binari. Errori: `{"error": "..."}` con 4xx/5xx come il ricevitore attuale.

| Endpoint | Scopo |
|---|---|
| `POST /compute/v1/lease` | Corpo `{worker_id, boot_id, kinds[], forced_kinds[], wait_s ≤ 25, capabilities, state}`. Long-poll: 200 con un job o 204. |
| `GET  /compute/v1/jobs/{id}/inputs/{name}` | Scarica un input. Supporta `Range` e `ETag`=sha256; vale solo con un lease valido. |
| `POST /compute/v1/jobs/{id}/heartbeat` | `{lease_id, progress{...}}` ogni 30 s; estende la lease; risposta `{cancel: bool}`. |
| `PUT  /compute/v1/jobs/{id}/outputs/{name}` | Carica un output (header `X-Sha256`, `X-Lease-Id`); va in staging sotto `tmp/`, idempotente per (nome, hash). |
| `POST /compute/v1/jobs/{id}/complete` | `{lease_id, input_hash, outputs[{name,sha256}], versions, metrics}`. Convalida e commit atomico. |
| `POST /compute/v1/jobs/{id}/fail` | `{lease_id, error, permanent?, retry_after?}`. |
| `POST /compute/v1/jobs/{id}/release` | `{lease_id, reason: "preempted"|"busy"|"shutdown"}`: torna in coda senza consumare tentativi (equivale a `jobs.requeue`). |
| `POST /compute/v1/status` | Stato del worker (stesso JSON di `worker.write_status`: `state`, `job`, `loaded`, `chat_active`, `on_ac`, `in_window`, `heartbeat`): alimenta `brain/status`, la scheda Stato dell'app Mac e il widget. |
| `PUT  /compute/v1/context/{giorno}` | Carica `context/audio-GIORNO.jsonl` (append con `offset` + `sha256` del prefisso; idempotente). |
| `GET  /compute/v1/settings`, `GET /compute/v1/people-names` | Copie di sola lettura per l'app Mac (`mac_audio`, nomi). |
| `PUT  /compute/v1/profile` | Carica `voices/me.npy` dopo `omy voice enroll` sul Mac. |
| `GET  /compute/v1/interactive?wait=25`, `POST .../interactive/{rid}/chunks|end` | Canale "Chiedi" (§1.3). |

`capabilities` = `{protocol: 1, kinds: ["transcribe","diarize","organize","embed"], versions: {pipeline: 5, diarize: <VERSION>,
prompt: 3, schema: 1, whisper_model: "ggml-large-v3-turbo.bin", organizer_model: "<stem GGUF>", embed_model, embed_dim: 512}, git: "<sha>"}`.
Le costanti sono quelle importate dal codice del Mac (`transcribe.PIPELINE`, `organize.PROMPT_VERSION`, ...).

**Controllo di versione.** L'hub confronta `versions` con le *sue* costanti (stesso repo, stesse importazioni). Se differiscono per
un `kind`, non lo assegna (lease 200 senza quel tipo) e segnala nello stato "Mac: versione diversa, aggiorna" (widget).
Così non si mescolano trascrizioni pipeline 4 e 5 dopo un aggiornamento fatto solo da un lato. `complete` ricontrolla
`versions` dell'output (`doc["pipeline"] == PIPELINE`, `speakers.version == VERSION`, `note.elaborazione.prompt_version`).

### 2.3 Tabella `jobs` sull'hub

Stesso schema di `jobs.py` più colonne: `lease_id TEXT, lease_until REAL, worker TEXT, expiries INTEGER NOT NULL DEFAULT 0`;
nuovo stato `leased` (al posto di `running` per i job in mano al Mac; `running` resta per i job interni dell'hub: render, index).
Nuovo kind `embed` (priorità 55) e `commit_*` non sono job: il commit avviene dentro `complete`. File: `/data/db/jobs.db`
(estensione `.db` per il backup, vedi §3.5).

- `lease` = `next_job()` esistente (stessa `BEGIN IMMEDIATE`, stesso ordine `priority, id`, stesso `not_before`) con `kinds`/`forced_kinds`
  del Mac, più `lease_id = uuid4`, `lease_until = now + 120`. `attempts` cresce come oggi.
- Un **reaper** nell'hub (ogni 30 s) riporta in coda i job con `lease_until < now`: stato `queued`, `attempts` come prima del
  claim (come `requeue`), `expiries += 1`. Oltre 5 scadenze → `failed` (= `waiting_manual` per `CLAUDE.md` dell'hub, con il
  pulsante "Riprova" in dashboard: `omy jobs retry <id>`): protegge dal job che fa morire il worker (`reset_running` fa lo stesso
  ragionamento con `MAX_ATTEMPTS`).
- `fail` = `jobs.fail` (backoff `BACKOFF_S * 2^(n-1)`, `MAX_ATTEMPTS = 3`, per transcribe/diarize `JOB_RETRY_S = 300`).
- Il Mac che si sveglia dopo una sospensione e riprende l'heartbeat con lo stesso `lease_id` **se il job non è stato riassegnato**
  riottiene la lease; altrimenti 409 → abbandona il lavoro e cancella la workspace.

### 2.4 Mac che muore a metà

| Caso | Cosa succede |
|---|---|
| Mac spento/in stop durante un job | Niente heartbeat → dopo 120 s il reaper rimette in coda. Al ritorno il Mac riparte da capo (la trascrizione è per sessione; l'organizer riprende dai checkpoint locali `cache/organize/<sessione>`, che restano sul Mac, vedi sotto). |
| Crash/kill del worker | All'avvio il compute worker cancella la workspace e dice `lease` con un nuovo `boot_id`: i suoi vecchi lease vengono rilasciati subito (equivale a `jobs.reset_running`). |
| Upload dei risultati interrotto | Staging in `tmp/`, nessun file visibile finché `complete` non convalida; si ripete `PUT` (idempotente) e `complete` (idempotente per `(job_id, lease_id)`: seconda chiamata → stessa risposta 200). |
| `complete` dopo la scadenza della lease | Accettato se il job è ancora `queued` e non riassegnato e `input_hash` coincide col calcolo corrente (il lavoro fatto non si butta); altrimenti 409 `stale` e il Mac scarta. |
| La sessione è cambiata mentre il Mac lavorava (nuovi segmenti, sessione rigruppata) | `input_hash` (`segments_hash(session)` come in `worker.scan`) non coincide → 409 `stale`; la scansione ne accoda uno nuovo. Per transcribe con `prior`: vale ancora la regola di `transcribe.plan` (accoda solo i segmenti nuovi). |
| Domanda "Chiedi" durante diarize/organize | Come oggi: `guard_once` interrompe il job locale; il compute worker fa `release(reason="preempted")`. |
| Hub non raggiungibile dal Mac | Il job locale in corso **prosegue**; `heartbeat`/`complete` ritentano con backoff 5 s→2 min per un massimo di 30 min; il risultato resta nella workspace. Oltre, scarta (l'hub avrà già rimesso in coda). |
| Mac che dorme durante un job lungo | Da evitare: il compute worker tiene un'asserzione `caffeinate -i` mentre un job gira (oggi `worker.py` non ne usa: **da aggiungere**, è un miglioramento). Per la finestra 01-07, il Mac deve essere sveglio (`pmset repeat wakeorpoweron` o impostazione "Risveglio per accesso alla rete"): oggi è lo stesso problema, non cambia. |

### 2.5 Dati: file grandi e giorni interi

- Un segmento ≈ 1,3 MB (README, `OMY_MAX_BODY` 32 MiB). Un job `transcribe` è **per sessione** (`jobs.key = session.id`), quindi
  `inputs` = i soli segmenti da fare (`transcribe.remaining(session, prior)`: con `prior` solo i nuovi) + tutti i sidecar `.json`
  (pochi KB) + il `prior` (JSON). Sessione continua di 8 ore ≈ 96 segmenti ≈ 125 MB; di giorno-intero sono ~600 MB al massimo
  (da verificare sul massimo reale: nei 15 giorni presenti il raw totale è 1,1 GB).
- Download: parallelismo 4, `Range` per riprendere, verifica `sha256` di ogni file (i sidecar hanno già `sha256` del `.bin`),
  scrittura in `~/OmiCompute/raw/audio/GIORNO/` con lo stesso layout (così `sessions.py`/`find_session` funzionano invariati).
  LAN/tailnet: 125 MB in pochi secondi; whisper impiega ~30 minuti per 8 ore (15× tempo reale, README): il trasferimento è trascurabile.
- Pulizia: dopo `complete` la workspace del job si cancella; una cache LRU opzionale (default 5 GB) tiene gli ultimi segmenti
  per un re-run immediato (`force`). Niente originali "unici" sul Mac.
- Output: il JSON di sessione (`transcripts/GIORNO/<s>.json`, ~100 KB-2 MB), `speakers` raw, sidecar nota: sempre piccoli.
- L'hub non tiene audio decodificato: WAV e finestre restano nella workspace (`cache/`, cancellate come oggi).

### 2.6 Cosa fa ogni tipo di job

| Kind | Input scaricati dal Mac | Cosa esegue il Mac (invariato) | Output | Commit sull'hub (`complete`) |
|---|---|---|---|---|
| `transcribe` | segmenti da fare + sidecar + `prior` | `transcribe.transcribe_session(session, prior)` con `OMY_ARCHIVE=~/OmiCompute` | `transcripts/GIORNO/<s>.json` | Convalida (`pipeline`, `segments` = ids correnti), `os.replace` sotto `lock.writing()`, `render_day`, accoda `diarize` e `index` (come `run_job`/`scan`). |
| `diarize` | tutti i segmenti della sessione + sidecar | `diarize.decode_and_diarize(session)` = decodifica + `diarize_wav` (nuova funzione estratta, stesso codice); carica prima `context/` | `diarize-result.json` (turni, `seconds`) + `embeddings.npz` | Sull'hub `diarize.commit(session, result)` = `_write_doc` attuale (`name_clusters`, `clusters/<s>.npz`, `speakers/<s>.json`) sotto `lock.writing()`, `render_day`, accoda `organize`/`index`. |
| `organize` | `DAY.jsonl` del giorno, `speakers/<s>.json`, `voices/people.json`, nota precedente | `organize.organize_session(session, base_url)` col 27B (`models.ensure("organizer")`) | sidecar nota JSON | Convalida schema + `input_hash`; scrive in `brain/note/...`; `vault.render_note`; `add_batch`; `finalize_batch` come oggi, tutto sull'hub. |
| `embed` | `[{id, text}]` (batch ≤ 64 chunk) | `embed.embed(texts, query=False)` (Qwen3-Embedding) | vettori 512 float32 | Inserisce in `vectors` (la logica di `index._embed_missing`). |
| `render`, `index` (chunk+FTS) | — | — | — | **Girano sull'hub** (sono Python puro). |

La workspace del Mac (`~/OmiCompute`) ha lo stesso layout di un archivio (`raw/`, `transcripts/`, `voices/`, `brain/`, `cache/`) e
`models -> ~/OmiArchive/models` (symlink; oggi lì stanno già i 17 GB). Il compute worker lancia i sottoprocessi con
`OMY_ARCHIVE=~/OmiCompute` e `OMY_TZ=Europe/Rome`: **nessuna riga di whisper/VAD/finestre cambia**.

### 2.7 L'hub può fare lavori leggeri da solo? Il Surface regge?

Sì per tutto ciò che non è un modello: `render`, `render_day`, `index` (chunk, FTS5, ricerca BM25/coseno in numpy), commit di
naming, revisione iPhone, decodifica Opus per i campioni voce, `vault`. Stime (non misurate sull'hub; sul Mac M-series ho misurato
`omy --json transcripts list` ≈ 50-100 ms, l'i5-7300U sarà ~3-5× più lento più l'avvio del container):

| Risorsa | Uso atteso | Note |
|---|---|---|
| RAM | receiver+scheduler ~150-250 MB; ogni sottoprocesso `omy` ~60-120 MB (numpy+dateparser); indice 8 MB | l'hub ha 7,8 GB, ~5,6 GB "available" con bravia+hub-api+kiosk; limite del container 1 GB. |
| CPU | picchi brevi per `omy` (0,3-1 s) e per `render_day` di giorni lunghi; `vault.update_links/render_entities` su un batch notturno | 2 core/4 thread: lanciare i sottoprocessi con `nice 10` e **un solo comando `omy` per volta** (già: `omy_lock` asyncio nel ricevitore). |
| Disco | +75 MB/giorno | avviso al 75%; nessuna copia decodificata. |
| Modelli | **nessuno** | `pyannote-audio`/torch fuori dall'immagine (§3.2). Embedding della query: Mac, ripiego BM25. |

Da tenere d'occhio (da verificare con prova): `omy` come sottoprocesso a ogni richiesta (import di dateparser ecc.) sul Surface;
se pesa, un secondo passo sarà tenere `review`/`people`/`places` in-process nel ricevitore. Non nel perimetro di questa migrazione.

---

## 3. L'hub in Docker

### 3.1 Servizi (`compose.yaml` dell'hub)

Un solo servizio, come da `CLAUDE.md` ("una app = un servizio"), con due processi (supervisione interna minimale):

```yaml
  omy:
    build: { context: ../apps/local-omy/mac/src, dockerfile: Dockerfile.hub }   # git clone in /srv/hub/apps/local-omy
    image: omy-hub:local
    restart: unless-stopped
    network_mode: host                      # il ricevitore ascolta solo 127.0.0.1 (app.py: main)
    user: "1000:1000"
    env_file: /srv/hub/secrets/omy.env      # OMY_TOKEN, OMY_COMPUTE_TOKEN, OMY_WIDGET_TOKEN
    mem_limit: 1g
    environment:
      TZ: Europe/Rome
      OMY_TZ: Europe/Rome                   # OBBLIGATORIO: confine dei giorni (trappola 1)
      OMY_ARCHIVE: /data
      OMY_PORT: "8787"
      HOME: /home/omy                       # config.toml: language="it", timezone, my_name, my_aliases
      OMY_ROLE: hub                         # niente fallback_transcribe, niente pmset
    volumes:
      - /srv/hub/data/omy:/data
      - /srv/hub/data/omy/config:/home/omy/.config/local-omy
      - /srv/hub/logs/omy:/logs
```

Processi nel container: `omy-receiver` (uvicorn, `127.0.0.1:8787`) e `omy hub-worker` (scheduler: scan, reaper, job interni
render/index, commit). Si possono tenere in un unico processo asyncio o in due con `tini`; scelta in PR 4.

### 3.2 Immagine e ambiente Python (uv)

- Base `python:3.13-slim` (l'hub ha 3.13.5; il Mac usa 3.14: `requires-python >= 3.12`, da verificare con la suite).
- `apt-get install -y libopus0 sqlite3` (opuslib; `audio.py: load_opus_decoder`; il percorso `DYLD_FALLBACK_LIBRARY_PATH` è solo macOS).
- `uv sync --frozen --no-dev --no-group compute` da `uv.lock`: **`pyannote-audio` passa in un gruppo opzionale `compute`**
  (PR 1) e `numpy` diventa dipendenza esplicita (oggi arriva da pyannote). Sul Mac: `uv sync --group compute` (invariato per l'utente
  finale perché `setup.sh` lo include).
- Utente 1000 come tutto l'hub; `umask 077` come fa già il ricevitore.
- Tag dell'immagine con lo sha del commit; rollback = rebuild del tag precedente.

### 3.3 Rete

- `127.0.0.1:8787` (libera; in uso: 8080 hub-api, 8765 bravia).
- `bootstrap/serve.sh`: aggiungere `tailscale serve --bg --https=10000 http://127.0.0.1:8787`. HTTPS 443 e 8443 restano di bravia e
  dashboard. URL per l'iPhone: `https://hub.tail7fcf68.ts.net:10000` (valido per `saveMac`: schema https + host).
- Il Mac smette di pubblicare: `tailscale serve reset` (o solo rimozione della 443) a fine cutover.
- Il widget parla col ricevitore su `127.0.0.1:8787` (hub-api è `network_mode: host`).

### 3.4 Configurazione e segreti

- `/srv/hub/secrets/omy.env` (600): `OMY_TOKEN`, `OMY_COMPUTE_TOKEN`, `OMY_WIDGET_TOKEN`. `OMY_TOKEN` può essere **lo stesso valore** già
  nell'iPhone (copiato via `ssh` senza stamparlo), così l'app cambia solo URL.
- `load_token()` accetta `OMY_TOKEN` da ambiente (sconsigliato dal README: finisce nell'ambiente dei processi). In `app.py: omy_env`
  i token vanno rimossi dall'ambiente dei sottoprocessi `omy` (modifica minima, PR 3).
- `/srv/hub/data/omy/config/config.toml`: `language = "it"`, `timezone = "Europe/Rome"`, `my_name`, `my_aliases` (copiati dalla config
  del Mac; **non** `team_id`/`device_udid`/`bundle_id`), eventuale `chat_idle_min`/`organize_window` (usati dal Mac: restano sul Mac).
  Il nome del modello organizzatore (`organizer_model`, entra in `input_hash`) non va configurato sull'hub: arriva dalle capabilities;
  finché il Mac non si è mai collegato, l'hub usa il default di `models.MODELS` (stesso stem del Mac se non c'è override: da verificare
  che `config.toml` del Mac non abbia `organizer_model`: non ce l'ha, le chiavi sono `bundle_id, device_udid, iphone_app, my_aliases, team_id`).

### 3.5 Dati e backup restic

Volume `/srv/hub/data/omy` = archivio. Il backup notturno (`bootstrap/backup/backup.sh`: snapshot `.backup` dei `*.db`, poi
`restic backup /srv/hub/data /srv/hub/secrets /srv/hub/backup`) va adattato:

| Percorso (sotto `/srv/hub/data/omy`) | Backup | Note |
|---|---|---|
| `raw/audio/` (1,1 GB, +75 MB/giorno) | **sì** | Insostituibile (originale solo qui dopo la migrazione). restic deduplica. |
| `transcripts/`, `brain/`, `voices/` (inclusi `enroll/`, `me.npy`, `people/`, `clusters/`), `context/`, `places.json`, `settings.json` | **sì** | Derivati ma costosi/biometrici (`voices`); `transcripts` rifacibili ma con ore di Mac. |
| `db/jobs.db` | **sì**, snapshot SQLite | Rinominare `jobs.sqlite` → `db/jobs.db` (`jobs.py: DB`), così `find -name '*.db'` lo prende. |
| `cache/brain-index.sqlite` | **no** (ricostruibile: `omy index --all`) | Ricostruire costa i vettori sul Mac (3.912 chunk, minuti). In alternativa rinominare in `.db` e includerlo (11 MB). Scelta in PR 11. |
| `cache/` (resto), `tmp/`, `models/`, `receiver.json` | **no** | `models/` non esiste sull'hub. |

Aggiungere `--exclude /srv/hub/data/omy/cache --exclude /srv/hub/data/omy/tmp` e l'inclusione esplicita di `db/`. Il backup finisce in
`rest-server` **sul Mac** → iCloud (`docs/architecture.md` dell'hub): quindi una copia dell'audio torna anche sul Mac (cifrata).
La prova di ripristino mensile (`hub-restore-test.timer`) va estesa con "conta i `.bin` e confronta con `ls`".
Se il Mac è spento il backup non parte (`backup.sh` passo 0) e si ripete ogni ora: già coerente col modello "Mac che aiuta l'hub".

### 3.6 Widget "Omi" per la dashboard

Cartella `widgets/omi/` nel repo dell'hub (partendo da `widgets/_template`), taglia **M** (2x1), `has_expanded: true`, `interval: 30`.
`provider.py` chiama `GET http://127.0.0.1:8787/v1/widget` (token `OMY_WIDGET_TOKEN`, solo lettura, letto lato server come il
token di bravia: nel compose un mount `:ro` di `/srv/hub/secrets/omy.env`, mai nel browser).

`GET /v1/widget` (nuovo, PR 8):
```json
{"today": {"minutes": 312, "segments": 63, "sessions": 9, "last_end": "…Z", "minutes_since_last": 4.2},
 "queue": {"queued": 3, "leased": 1, "failed": 0, "oldest_age_min": 41, "by_kind": {"transcribe": 2, "diarize": 1}},
 "mac": {"online": true, "last_seen_s": 12, "state": "running", "job": {"kind": "transcribe", "key": "…", "since": 1700000000},
         "version_ok": true},
 "pendant": {"battery": 74, "drain_pct_h": 4.1, "hours_left": 18, "firmware": "3.0.21", "last_reading_min": 6},
 "iphone_upload": {"last_upload_at": "…", "queue_remaining": 0},
 "disk": {"omy_gb": 1.3, "free_gb": 98}}
```
UI (`hub/ui/widgets/omi.js`, solo token di `DESIGN.md`): taglia M con anello batteria (`.ring`, colore `--ok/--warn/--bad`: <20% warn,
<10% bad), "Oggi 5 h 12 min" (testo), riga `.svc` "Coda 3 · Mac online" (`--ok`) o "Mac spento, 3 lavori in attesa" (`--warn` solo se la coda
è più vecchia di 6 h: un Mac spento di notte non è un errore). Vista estesa: coda per tipo, lavoro in corso, ultimo upload, versione Mac,
pulsante "Riprova" per i `failed`. Stati vuoti onesti (nessun dato di esempio). Test in `hub/api/tests/` col provider simulato.

### 3.7 Documentazione dell'hub da aggiornare

`docs/architecture.md` oggi descrive l'opposto per local-omy ("worker sul Surface, modelli via API dal Mac", fase 5) e la "Coda verso
il Mac" con `waiting_manual`. Va riscritto: il Mac è un worker in pull; stati `queued → leased → done`, `failed` ≡ `waiting_manual`,
"Riprova" dalla dashboard. Aggiornare anche la tabella "Progetti" e la riga serve (10000).

---

## 4. Migrazione dei dati

### 4.1 Cosa si copia

| Da `~/OmiArchive` | Dimensione | Destinazione `/srv/hub/data/omy` | Note |
|---|---|---|---|
| `raw/` | 1,1 GB, 1114 `.bin` + 1114 `.json`, 15 giorni | `raw/` | cuore. |
| `transcripts/` | 29 MB | `transcripts/` | pipeline 5, quindi nessuna ritrascrizione. |
| `brain/` | 3,7 MB | `brain/` | vault con blocchi `omy:begin/end`. |
| `voices/` | 12 MB (`enroll/` 11 MB, `clusters/` 1,4 MB) | `voices/` (`chmod 700/600`) | biometrico: solo su canale cifrato (tailnet). |
| `context/` | 1,3 MB | `context/` | |
| `places.json`, `settings.json`, `receiver.json` | pochi KB | stessi | 600. |
| `cache/brain-index.sqlite` | 11 MB | `cache/brain-index.sqlite` | copiato con `sqlite3 .backup` (non il file vivo): evita di rifare 3.912 vettori. |
| `cache/jobs.sqlite` | 128 KB | **non copiare** | la scansione dell'hub ricostruisce la coda; `needs_*` evitano rifacimenti. |
| `cache/ask-log.jsonl` | 8 KB | opzionale | |
| `models/` | 17 GB | **resta sul Mac** | 15 GB `brain/` (27B, 4B, embed, reranker), 1,5 GB whisper turbo, 868 KB VAD, 32 MB `hf/` (pyannote). |
| `cache/{eval,brainbench,backup-pipeline4,group-compare,organize,models*.json}` | ~2 MB | resta | esperimenti/stato locale. |
| `~/.config/local-omy/{token,hf_token,config.toml,state.json}` | | resta (token iPhone: copiato in `omy.env` senza mostrarlo) | `hf_token` serve solo al Mac. |

### 4.2 Strumento e ordine

`rsync` sul tailnet (cifrato; LAN `hub.local` più veloce, stessa sicurezza accettabile in casa), mai `--delete` nella prima passata:

1. **Prova a secco e prima copia** (Mac in produzione, hub senza scheduler né `serve`):
   `rsync -aH --partial --info=progress2 --checksum -e ssh ~/OmiArchive/{raw,transcripts,brain,voices,context} hub@hub.local:/srv/hub/data/omy/`
   (+ file singoli). Usare `rsync` 3.x di Homebrew: quello di sistema di macOS (openrsync) non conosce `--info=progress2` (da verificare sulla versione installata). `--checksum` costa 1,2 GB di letture: accettabile e una tantum; `-a` conserva i mode 600/700 (verificare
   proprietario uid 1000 sull'hub: `--chown` se serve).
2. **Indice** : `sqlite3 ~/OmiArchive/cache/brain-index.sqlite ".backup /tmp/…"`, poi `scp`.
3. **Verifica** (§4.4) e **prova generale**: avvio dell'hub con `OMY_HUB_SCHEDULER=off` e **senza** `tailscale serve --https=10000`; `curl`
   locale delle API con `OMY_TOKEN`: `omy --json transcripts list` sull'hub deve dare lo stesso JSON del Mac (diff).
4. **Cutover** (§5): fermo il ricevitore del Mac, **rsync finale** (stesso comando, `--checksum`, pochi secondi) e riverifico.

### 4.3 Segmenti che arrivano durante il trasferimento

Comportamento dell'iPhone verificato nel codice (`iphone/app/Packages/OmyKit/Sources/OmyKit/Uploader.swift`, README "Upload al Mac"):
il segmento si cancella **solo** con risposta 200 con `sha256` uguale (`MacAPI.uploadSegment`); a ogni errore (rete, 4xx/5xx, 502-504)
`drain()` si ferma, il segmento resta in `Documents/segments` (è la coda: "non c'è un database") e riprova con backoff 30 s → 30 min,
anche a ogni chiusura segmento/ritorno in foreground. Quindi:
- prima prova generale: il Mac continua a ricevere normalmente, il delta lo prende il rsync finale;
- durante il cutover, a ricevitore Mac fermo e URL ancora vecchio: gli upload falliscono e **restano sul telefono**;
- dopo il cambio URL: arrivano all'hub in ordine cronologico; l'id è `AAAAMMGGThhmmssZ-xxxxxxxx` (uguale per lo stesso segmento) e
  `duplicate`/409 evitano doppioni se un segmento fosse stato già copiato;
- limite: se l'iPhone resta offline/con URL vecchio più giorni accumula; nessuna perdita (il Flutter legacy ha lo stesso modello:
  `iphone/src/lib/upload/uploader.dart` cancella solo dopo 200 con `sha256` uguale; l'app installata è la nativa).

### 4.4 Verifica

Entrambi i lati, sullo stesso albero:
- conteggi: `find raw/audio -name '*.bin' | wc -l` (oggi 1114), `*.json`, cartelle giorno (15), `transcripts/*.jsonl`, note del vault;
- manifesto hash: `(cd raw && find . -type f -print0 | sort -z | xargs -0 shasum -a 256) | sha256sum` uguale (su Debian `sha256sum`/`shasum`);
  oppure `rsync -ain --checksum` finale che non elenca differenze;
- per ogni `.bin` il `sha256` del sidecar coincide col file (già controllato dal ricevitore all'upload, ripetibile con uno script
  `mac/tools/verify_archive.py` nuovo, PR 12);
- funzionale: `omy --json transcripts day <giorno>` e `omy --json brain status` identici (a meno di campi `worker`) tra Mac e hub;
- permessi 600/700 e proprietario;
- fuso e lingua: `omy --json transcripts list` sull'hub mostra gli stessi giorni e "Persona 1"/"Io" (trappole 1 e 2).

Non si cancella nulla sul Mac: `~/OmiArchive` resta come copia fredda fino al primo ripristino riuscito da restic **e** almeno 2-4 settimane
di funzionamento.

---

## 5. Cutover e rollback

### 5.0 Prerequisiti (tutto fatto prima, senza fermare nulla)

- PR 1-8 e 11 fuse e provate in staging (§6); immagine costruita sull'hub; `omy.env`, `config.toml`, volume creati; backup adattato.
- Il compute worker su Mac installato **ma non caricato** (plist non attivo).
- Finestra consigliata: sera/notte di un giorno senza registrazione in corso e con l'iPhone in casa; durata ~15-20 minuti.
- Controllo che la coda del Mac sia vuota (`omy jobs`: nessun `running`; se serve, `omy organize` non in corso).

### 5.1 Sequenza

1. **Mac**: `launchctl bootout gui/$(id -u)/com.pierpaolobene.local-omy.worker` (il job in corso torna in coda: `worker.stop` →
   `preempted`), poi `…receiver`. Da qui gli upload dell'iPhone falliscono e restano in coda sul telefono.
2. **Mac**: `tailscale serve reset` (o togliere la 443 del ricevitore) così nessuno raggiunge più il vecchio endpoint.
3. **Rsync finale** `Mac → hub` (§4.2 punto 4) + indice con `.backup`; **verifica** (§4.4). Se qualcosa non torna: rollback (§5.2).
4. **Hub**: `docker compose up -d omy`; `bootstrap/serve.sh` (aggiunge `--https=10000`); prova da hub: `curl -sS -H "Authorization: Bearer $OMY_TOKEN"
   https://hub.tail7fcf68.ts.net:10000/v1/transcripts | head` (token letto dal file, non stampato).
5. **iPhone**: Impostazioni → URL = `https://hub.tail7fcf68.ts.net:10000`; token invariato (campo vuoto = tiene quello salvato:
   `AppModel.saveMac`). L'app fa subito `kick` e sincronizza le funzioni. **Nessuna reinstallazione**: la coda in `Documents/segments`
   resta (stesso bundle id, stesso contenitore). Controllo: "Oggi" mostra la coda che si svuota; `GET /v1/transcripts` risponde.
6. **Mac**: carica il compute worker (`omy setup fix compute`: installa il LaunchAgent, nuovo `com.pierpaolobene.local-omy.compute`,
   salva `compute_token`); lo stato su hub passa a "Mac online"; il primo `lease` assegna i job accumulati.
7. **App barra dei menu**: aggiornata (PR 10) e puntata all'hub; verifica scheda Stato, Persone, Luoghi, Second brain.
8. **Osservazione 24-48 h**: segmenti in arrivo, trascrizione di una sessione di prova **con confronto golden** (§6, PR 7), notte con
   organizzazione (01-07, alimentatore), backup notturno, widget.
9. **Dopo ≥2 settimane + ripristino restic provato**: archiviare `~/OmiArchive` (tenere `models/`, spostare dati in `~/OmiArchive.old`),
   rimuovere i vecchi LaunchAgent (`receiver`, `worker`).

### 5.2 Rollback

Entro il punto 5 il rollback è banale perché il Mac non è stato toccato dati:
1. Hub: `docker compose stop omy` e togliere la 10000 (`tailscale serve --https=10000 off`).
2. Mac: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/…receiver.plist` (e `…worker.plist`), `tailscale serve --bg http://127.0.0.1:8787`.
3. iPhone: URL → quello del Mac (`https://macbook-pro-di-peppo.tail7fcf68.ts.net`). I segmenti rimasti in coda partono.

Dopo il punto 5, con dati nuovi già sull'hub:
1. Fermare il compute worker e il ricevitore dell'hub (stop `omy`) **prima** di tutto, per congelare i dati.
2. Copiare **indietro** il delta: `rsync -aH --checksum hub:/srv/hub/data/omy/{raw,transcripts,brain,voices,context,places.json,settings.json} ~/OmiArchive/`
   (senza `--delete`; i file con lo stesso nome hanno la versione più recente dell'hub, per i `.bin` è identica per costruzione;
   `transcripts/*.md|jsonl` del giorno si rigenerano con `omy transcribe --pending --render`).
3. Riavviare ricevitore e worker del Mac (come sopra) e riportare l'URL dell'iPhone.
4. `omy index --all` per riallineare i vettori se l'indice è tornato indietro.
Resta valido perché il formato dell'archivio è identico nei due sensi (D1) e il Mac ha ancora modelli e codice (non si rimuove nulla).

---

## 6. Passi di implementazione (PR piccole, ciascuna con test)

Convenzione: `mac/src/tests` (pytest, `fake_llama.py`, `fake_embed.py`, `conftest.py` esistenti). Ogni PR lascia il Mac funzionante
com'è oggi (modalità locale di default: `OMY_ROLE` assente = comportamento attuale). Rischio: B/M/A.

| PR | Contenuto | Test | Stima | Rischio |
|---|---|---|---|---|
| 1 | `pyproject`: `pyannote-audio` in gruppo `compute`, `numpy` esplicito; `uv.lock` | suite attuale con e senza gruppo; import `omy` senza torch | 0,5 g | B |
| 2 | `jobs.py`: colonne lease, `lease()/heartbeat()/complete_lease()/release()/reap()`, kind `embed`, `db/jobs.db`, stato `leased`; `next_job` invariato | estende `test_jobs.py`, `test_worker_robust.py`: lease/scadenza/riassegnazione/idempotenza, race con `BEGIN IMMEDIATE` | 2 g | M |
| 3 | `omy_receiver/compute.py`: lease, inputs (Range/ETag), outputs, complete/fail/release, status, versioni, token separato, rimozione token dall'`omy_env` | `test_receiver.py` pattern (TestClient): auth, 409/stale, upload idempotente, Range, versione diversa | 3 g | M |
| 4 | `omy/scan.py` (scan estratto da `Worker`, invariato), `omy hub-worker` (scan, reaper, `render`, `index` chunk+FTS, `finalize_batch`), `OMY_ROLE=hub` (niente `fallback_transcribe`, niente `pmset`) | `test_worker.py` esistente verde senza cambi; nuovo test con coda a lease finta | 2-3 g | M |
| 5 | `diarize`: `decode_and_diarize` / `commit` (spezzatura di `diarize_session`) | **golden**: su una sessione reale il `speakers/<s>.json` prodotto dal vecchio e dal nuovo percorso è identico byte a byte; `test_voices.py` verde | 1-2 g | M |
| 6 | Indice: `embed` job (vettori via compute), `index.update` che lascia chunk senza vettore (già previsto) e accoda batch `embed`; ricerca con embedding query remoto + ripiego BM25 | `test_index.py`, `test_embed.py` con `fake_embed.py` | 2 g | M |
| 7 | Compute client Mac: `omy/compute.py` + `omy compute-worker` (loop lease, stage-in con Range+sha, run in `~/OmiCompute`, heartbeat, stage-out, release su preemption, `caffeinate`), riuso di `Worker.allowed/guard/models`; LaunchAgent template + `omy setup fix compute` | contratto contro l'app dell'hub in-process; **golden di qualità**: trascrivere con la nuova via una sessione già trascritta e confrontare `speech` con quella attuale (identica, stessa `PIPELINE`/modello); prova con Mac "spento" a metà (kill) | 4-5 g | A |
| 8 | Nuovi endpoint dell'hub: `GET /v1/widget`, `/v1/battery`, `/v1/status`, `/v1/export/*`, `POST /v1/transcribe` esteso (`mac_online`), `/v1/brain/status` composto | `test_receiver.py`, `test_app_status.py` | 1-2 g | B |
| 9 | Canale interattivo "Chiedi": long-poll, `embed_query` + `chat`, relay SSE, ripiego solo fonti | `test_ask.py` con `fake_llama.py`; prova latenza reale | 3 g | A |
| 10 | Modalità remota della CLI per l'app Mac (config `hub_url`, `compute_token`): `transcripts/people/speakers/places/features/brain/jobs/battery/status` passano a HTTP verso l'hub; upload `context/` (Swift `AudioActivity` invariato, il worker li carica); `iphone: needs_reopen` da `/v1/status`; "Apri vault" → specchio | `test_review.py` + nuovi con server finto; build `mac/src/app/build.sh` | 3 g | M |
| 11 | **Hub repo**: `Dockerfile.hub` (in local-omy), `compose.yaml`, `serve.sh` (10000), backup (`.db`, esclusioni, restore-test), `widgets/omi`, aggiornamento `docs/architecture.md` | `hub/api/run-tests.sh` per il widget; `docker compose config`; avvio a vuoto | 2 g | B |
| 12 | `mac/tools/migrate_to_hub.sh` + `verify_archive.py` + runbook cutover/rollback eseguibile (wizard) | prova generale su copia, dry-run con `rsync -n` | 1 g | B |
| 13 | iPhone: testo "Mac spento" in "Oggi" (campo opzionale `mac_online`), nota nell'URL ("hub"), nessuna logica nuova; aggiornare `iphone/app/PARITY.md` | `swift test` (OmyKit/MacAPITests) | 0,5 g | B |

Ordine: 1 → 2 → 3 → (4, 5, 6 in parallelo) → 7 → 8 → 11 → 12 → **provare tutto in staging** (hub con archivio copiato, Mac compute worker,
iPhone ancora sul Mac) → 10, 9, 13 → cutover. Totale ~25-30 giorni-uomo di agente; il percorso critico è 2-3-7.

### Cosa cambia nell'app iPhone
- **Nulla di obbligatorio.** Stessi path, stesso JSON, stesso token: cambia solo l'URL in Impostazioni (`saveMac`, `SettingsStore.macURL`).
- Nessuna reinstallazione né nuova firma (stesso bundle id `com.pierpaolobene.local-omy`); il rinnovo settimanale resta sul Mac
  (`worker.renew_iphone`, Xcode).
- Opzionale (PR 13): messaggio "Mac spento, in attesa" quando `/v1/transcribe` o lo stato dicono `mac_online:false`; l'errore attuale
  "Mac non raggiungibile: è acceso e su Tailscale?" (`MacAPI.unreachable`) diventa fuorviante perché ora l'interlocutore è l'hub (sempre
  acceso): testo da aggiornare ("Hub non raggiungibile: Tailscale attivo?").
- Timeout di "Chiedi": 90 s di inattività (`MacAPI.ask`), compatibile col canale interattivo.

### Cosa cambia nell'app barra dei menu
- Continua a eseguire `omy --json ...` (`Omy.swift`), ma `omy` sul Mac diventa un client dell'hub per i dati (PR 10). La scheda **Stato**
  (`archive`, ricevitore, coda iPhone) legge dall'hub via `omy status`; **Persone/Luoghi/Conversazioni/Second brain** idem.
- Il pulsante "Apri vault" apre lo specchio locale (o è nascosto se non lo vuoi, domanda 1). "Copia URL/token" mostra URL/token dell'hub.
- `AudioActivity.swift` continua a scrivere `~/OmiCompute/context/` (il path si ottiene da `OMY_ARCHIVE`, già supportato in `Model.swift`):
  il compute worker lo carica sull'hub. `Features.isOn("mac_audio")` legge la copia locale di `settings.json` mantenuta dal worker.
- Le checklist `omy setup check` cambiano: spariscono "ricevitore"/"tailscale serve", compaiono "compute worker", "hub raggiungibile", "versione
  concorde".

---

## 7. Decisioni e stato dell'implementazione

### Decisioni (dell'utente, sulle domande aperte del piano)

1. **Vault**: master sull'hub **e** copia sul Mac per Obsidian, **in sola lettura** hub→Mac. LaunchAgent `vault-mirror` ogni 10 minuti:
   `rsync -a --delete hub@hub.local:/srv/hub/data/omy/brain/ ~/OmiBrain/` (`mac/src/tools/vault_mirror.sh`; rsync c'è sull'hub). **Le modifiche fatte sul Mac
   vengono sovrascritte** (i file della copia sono resi non scrivibili; `.obsidian/` e `.trash/` non si toccano; un vault vuoto sull'hub non svuota la copia).
2. **"Chiedi" con il Mac spento**: la domanda è **in coda** (persistente, tabella `asks` nel database dei lavori), l'iPhone la vede "in attesa del Mac" e
   la risposta arriva quando il Mac torna (il compute worker la prende). Modifica minima all'app iPhone: stage `queued:<id>` nello stream, testo "In attesa del Mac",
   poll di `GET /v1/ask/{id}` ogni 15 s (`MacAPI.askResult`, `AskView.swift`, un test); le app vecchie mostrano il messaggio di cortesia come risposta.
3. **Notte**: il Mac può dormire e **deve svegliarsi di notte per organizzare**. Wake-on-LAN sul Wi-Fi non è affidabile: sveglia programmata sul Mac,
   `sudo pmset repeat wakeorpoweron MTWRFSU 00:55:00` (**da eseguire a mano**, richiede sudo), e il compute worker tra le 01 e le 07 prende i job `organize`
   (solo con l'alimentatore); finito, il Mac torna a dormire da solo. Se alle 07:00 l'hub vede ancora job `organize` in coda da prima delle 07:00,
   il widget Omi li segnala ("Organizzazione notturna non fatta: N in attesa").
4. **`transcripts/*.jsonl`**: nessuno li legge oggi, restano sull'hub (pronti `GET /v1/export/days` e `/v1/export/GIORNO.jsonl`).
5. **Copia fredda `~/OmiArchive`**: da tenere **4 settimane dopo il cutover** (non va cancellata). `voices/enroll/` resta sul Mac e viene copiato anche sull'hub.

### Stato per PR (commit su `hub-migration`)

| PR | Commit | Note e differenze dal piano |
|---|---|---|
| 1 | `e242472` | `pyannote-audio` nel gruppo `compute` (`default-groups = ["dev", "compute"]`: sul Mac `uv sync`/`uv run` non cambiano), `numpy` esplicito, `Dockerfile.hub`. L'immagine non ha torch né pyannote (462 MB). |
| 2 | `8d83fd5` | Lease in `jobs.py`. Il database dell'hub è `db/jobs.sqlite` (non `.db`): il backup è stato esteso ai `*.sqlite` (come chiesto), non il file rinominato. `organize` non si assegna finché ci sono `transcribe`/`diarize` in coda o in lease (la regola di `organize_ok` ora vive nell'hub). |
| 3, 5 | `5545f27` | `omy_receiver/compute.py` + `omy/hubjobs.py` (input/commit di ogni tipo). Gli input sono percorsi relativi all'archivio (stesso layout nel workspace del Mac): `transcribe` con `prior` manda i sidecar di tutti i segmenti ma l'audio solo dei nuovi (gli altri sono file vuoti: non vengono mai decodificati). Una sessione cresciuta mentre il Mac lavorava si committa come prior (prefisso), una rigruppata è `stale`. `diarize_session` = `decode_and_diarize` (Mac) + `commit` (hub): test "golden", i byte scritti sono identici. |
| 4, 6 | `f5739ff` | `omy/hubworker.py`: **sottoclasse di `Worker`** (non è stato estratto `scan.py`: `test_worker.py` resta verde senza cambi). Job `embed` (il Mac calcola i vettori, l'hub li inserisce) e `search(qvec=…)`. |
| 7 | `321b563` | `omy/compute.py`. Il worker gira sul Mac con `OMY_ARCHIVE=~/OmiCompute`; rifiuta di svuotare una cartella che non sia un workspace (marker). `omy compute-worker [--once --dry-run --kind K]`; LaunchAgent `compute` installato **disabilitato** (`launchctl disable`). |
| 8, 9 | `15b45cd` | `GET /v1/widget`, `/v1/status`, `/v1/battery`, `/v1/export/*`, `/v1/transcribe` con `mac_online`; **"Chiedi" non usa il relay SSE interattivo del piano ma la coda persistente** (`GET /compute/v1/ask/next`, `…/retrieve`, `…/events`): il Mac embedda la query, l'hub cerca, il Mac genera e rimanda a pezzi; se l'app è ancora connessa lo stream viene inoltrato in diretta. |
| 13 | `4974894` | App iPhone: "in attesa del Mac" + poll; testo "Server non raggiungibile (Mac o hub)"; `PARITY.md`. L'app compila (xcodebuild simulatore) e `swift test` passa (75). |
| 10 | `65b982d` | `hub_url` in `config.toml` → `omy` è un client dell'hub (stesso JSON): transcripts, speakers, people, places, features, battery, brain, jobs, status, organize, index, transcribe `--pending`. `omy voice enroll` carica `me.npy` sull'hub. Resta locale: iphone, setup. L'app Swift non è cambiata. |
| 11, 12 | `80e1dbd` + repo dell'hub | Hub: `compose.yaml`, `serve.sh` (:10000), backup (`*.sqlite`, esclusioni, restore-test che conta i `.bin`), widget Omi, `bootstrap/omy/setup.sh`, `architecture.md`, README (commit nel repo dell'hub). Qui: `tools/verify_archive.py`, `tools/vault_mirror.sh`, `docs/cutover.md`, `scripts/cutover.sh`. |

### Distribuzione in parallelo (fatta, senza cutover)

Hub: servizio `omy` su (scheduler spento), `serve` :10000, snapshot di `~/OmiArchive` in `/srv/hub/data/omy` (1114 segmenti, hash identici),
`omy transcripts list/day`, `people`, `places`, `brain` identici a quelli del Mac. Prova di upload su :10000: segmento nuovo `stored`, ripetuto `duplicate`,
stesso id con contenuto diverso 409, token sbagliato 401. Compute worker: **dry-run** e **un job vero** (trascrizione whisper di un segmento di prova con id finto, poi diarizzazione
pyannote dello stesso) contro l'hub; i dati di prova sono stati rimossi. Il worker originale del Mac non è stato toccato.

### Non fatto / da sapere

- **Il cutover** (`scripts/cutover.sh`), la sveglia notturna (`pmset`, sudo) e il rinnovo dell'app iPhone con la nuova versione (`omy iphone install`).
- La sveglia del Mac da `pmset` e il comportamento con il coperchio chiuso non sono stati verificati.
- L'organizzatore (27B) non è stato provato attraverso l'hub (richiede il modello e la notte): è coperto dai test del contratto (input, hash, commit, vault), non da una nota vera.
- Rinnovo dell'app iPhone (`worker.renew_iphone`): resta nel compute worker (Xcode), `needs_reopen`, nello stato che mostra l'app, si calcola con i segmenti dell'hub (`GET /v1/status`, `last_segment_start`).
- Le impostazioni delle funzioni (`settings.json`) sono sull'hub; il compute worker le copia in `~/OmiArchive/settings.json` (dove l'app del Mac le legge).
- L'app a barra dei menu non è stata modificata né provata con il Mac in modalità hub (solo l'output di `omy` è verificato identico).
- Da valutare (piano §2.1): ACL Tailscale che limitano la porta 10000 ai soli nodi iPhone e Mac.
