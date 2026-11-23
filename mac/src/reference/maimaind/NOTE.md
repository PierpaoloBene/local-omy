# maimaind: note per te

Questo file elenca cosa ho fatto e cosa ho verificato davvero. Poi i passi da fare a mano e le cose da rivedere. Stato al 30 ottobre 2026, dopo la seconda sessione (precisione, efficienza, file, ricerca, interfaccia). Comandi e struttura sono nel [README](README.md), le decisioni di progetto in [PLAN.md](PLAN.md).

## 1. Stato in breve

### 1.1 Cosa è cambiato in questa sessione

| | Prima | Adesso |
|---|---|---|
| **Modello** | Gemma 4 12B Q4_K_M, 14 token/s in generazione | Gemma 4 12B **QAT** (UD-Q4_K_XL) con il **drafter MTP**: 26–29 token/s, stesso testo. Un riassunto di sessione passa da 88 a 45 s |
| **Memoria** | cache dei prompt di llama-server al default di 8 GB: con il modello sulla GPU spingeva il Mac in swap | cache a 1,5 GB (misurato: il processo cresce di 1,5 GiB invece di 4,4); embedder ridotto da 2,7 a 1,4 GB di GPU |
| **File letti** | home senza `~/Library`, 4 formati: **7 documenti** | anche **iCloud Drive** e `~/Library/CloudStorage`, **~25 formati**: **184 documenti** sul tuo Mac (148 solo in iCloud Drive/Documenti) |
| **Ricerca** | il modello sceglieva tra ricerca per parole e per significato, un giro di tool obbligatorio | una sola ricerca **ibrida** (BM25 + vettori fusi per posizione) riordinata da un **reranker**; la prima la fa il codice prima che il modello parta, e 12 domande su 16 hanno risposta al primo giro |
| **Documenti** | scheda dal nome del file e dall'inizio | scheda dal **percorso** (le tue cartelle Salute, Auto, Anagrafica…) con **ambito**; pezzi di testo con titolo e cartella davanti; **copie** riconosciute senza modello; `get_person` elenca i documenti di quella persona |
| **Interfaccia** | pagina funzionale, passi mostrati come nomi di tool | pagina rifatta: stato dell'indice sempre visibile, domande suggerite, passi in italiano, fonti come schede, **anteprima** dei PDF e delle immagini nella pagina, documenti filtrabili per ambito, indirizzi salvabili (`#impegni`, `#documenti`) |
| **Eval** | 9 domande, 1 documento | 16 domande, 7 documenti in 4 formati, domande che si trovano solo per significato: **16/16**, mediana **6,7 s**, 0 citazioni inventate |

I test automatici sono 125 (`uv run pytest`), tutti verdi. Non ho fatto commit: le modifiche sono nel working tree, da rivedere.

### 1.2 Cosa ho verificato e come

| | Verificato davvero | Solo con test o compilazione | Non verificato |
|---|---|---|---|
| Drafter MTP, QAT, parametri di llama-server | benchmark sui tuoi dati (un riassunto vero e due domande vere per ogni configurazione) e memoria della GPU letta dai log di llama-server | | |
| Eval a 16 domande | su tre modelli, con embedder e reranker veri | | |
| Reranker | Qwen3 contro bge-m3 su 7 domande italiane con 30 paragrafi veri a caso come distrattori | | |
| Scansione di iCloud Drive e dei formati nuovi | conteggio sui tuoi file (184); il daemon vero li sta leggendo (§1.3) | lettura di xlsx, pptx, odt, epub, html, eml, csv | `.doc` e `.rtf` (textutil) e iWork solo sul codice: sul tuo Mac non ci sono file Pages/Numbers/Keynote |
| Pagina web | aperta in Chrome (headless) sul daemon vero: benvenuto, chat con fonti, impegni, documenti per ambito; desktop e 520 px, chiaro e scuro | | anteprima di un PDF dentro la pagina: il clic su «Apri» non l'ho provato (Chrome headless, senza clic) |
| App iPhone | | compila; i passi ora sono in italiano come sulla pagina | non reinstallata: la vecchia versione funziona con l'API nuova |

### 1.3 Verifica sui tuoi dati

Domande vere fatte con `brain ask` attraverso il daemon, come le fa l'app, con 53 documenti già letti:

| Domanda | Risposta | Tempo |
|---|---|---|


**Due problemi trovati sul campo e corretti:**
- **Daemon riavviato male:** se il daemon viene ucciso mentre lavora (un crash, `launchctl kickstart -k`), i suoi llama-server restavano accesi, la memoria restava occupata e il nuovo daemon aspettava per sempre ("memoria sotto pressione"). Ora all'avvio spegne i server rimasti (verificato con `kickstart -k` a metà di un riassunto); allo stop normale interrompe la chiamata al modello in corso e spegne i server (verificato con `launchctl bootout`: nessun processo rimasto; lo stop dura circa 50 s, quasi tutti di llama-server che libera la GPU).
- **Testi troppo lunghi per l'embedder:** un pezzo di testo con molti simboli superava il contesto ridotto dell'embedder e bloccava il giro. Ora quel testo viene accorciato.

## 2. Passi manuali

Daemon, `tailscale serve` e app iPhone risultano già installati. Restano:

### 2.1 Accesso completo al disco

`uv run brain setup check` dice ancora `NO full-disk-access`. Ti serve per Documenti, Scrivania e Download senza dialoghi di macOS; iCloud Drive si legge anche senza.

1. Impostazioni di Sistema → Privacy e sicurezza → Accesso completo al disco → `+`.
2. Aggiungi `~/.local/bin/uv` (⌘⇧G per scrivere il percorso).
3. Se nel log `~/Library/Logs/maimaind.log` compare "Operation not permitted", aggiungi anche il Python vero: `~/.local/share/uv/python/cpython-3.13.15-macos-aarch64-none/bin/python3.13`. Questo percorso cambia quando uv aggiorna Python.

### 2.2 I documenti di iCloud non scaricati

Dei 184 documenti, 112 sono "solo in iCloud" (non scaricati sul Mac). Leggerli li scarica. Il daemon ne scarica al massimo 40 ogni scansione (una ogni 15 minuti), così una scansione non resta mai appesa alla rete: tutti entro un'ora circa. Per cambiare il limite, in `~/.config/maimaind/config.toml` (permessi 600):

```toml
doc_icloud_downloads = 40     # 0: mai scaricare, indicizza solo quelli già sul Mac
```

Dopo la lettura macOS può liberare di nuovo lo spazio: l'indice resta, e "Me lo mandi?" lo riscarica.

### 2.3 Modelli in più sul disco

In `~/Library/Application Support/maimaind/models/` restano due modelli che servono solo per confronto: `gemma-4-12b-it-Q4_K_M.gguf` (7,1 GB, il vecchio) e `gemma-4-12b-it-qat-q4_0.gguf` (7,0 GB, il QAT di Google). Se non ti servono, cancellali con il loro drafter `mtp-gemma-4-12b-it-Q8_0.gguf`; i profili `gemma-q4km` e `gemma-qat-q4_0` li riscaricano su richiesta.

### 2.4 Dai un nome alle voci in local-omy

Oggi non c'è nessuna persona con un nome. Senza nomi non nascono le pagine persona, e "Ho parlato con Rosa Linda?" funziona solo se il nome compare nel testo. I documenti invece i nomi li hanno (Anagrafica/Rosa Linda/…): `get_person` li trova appena la persona ha un nome.

### 2.5 Con il daemon attivo

Non lanciare a mano `brain eval`, `brain doctor`, `brain summarize` o `brain docs process`: avviano i loro server e con quelli del daemon la memoria della GPU non basta. L'ho visto succedere: llama-server risponde "Compute error" (`kIOGPUCommandBufferCallbackErrorOutOfMemory`). Ferma prima il daemon con `launchctl bootout gui/$(id -u)/maimaind.serve` e rimettilo con `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/maimaind.serve.plist`.

## 3. Da rivedere a mano

### 3.1 La pausa di local-omy

Non è cambiata, ed è ancora collaudata solo con dei finti. Prima di fidarti: lancia `uv run omy transcribe --pending` con sessioni da fare, fai una domanda, cerca nel log "pausing local-omy" e dopo 10 minuti "local-omy resumed", controlla che in `~/OmiArchive/cache/` non restino `.wav`.

### 3.2 Le schede dei documenti

Guarda la pagina Documenti, o `~/SecondBrain/documents/`, appena il daemon ha finito.

- **Il titolo e l'ambito** vengono dal testo e dalla cartella. Se uno è sbagliato, correggilo nella scheda del vault: il blocco diventa tuo, ma la ricerca usa ancora la scheda del modello. Un comando per correggerla non c'è ancora.
- **Il tipo di documento:** nell'eval una lista della spesa veniva classificata come documento d'identità, e sui tuoi file quasi tutto finiva in «altro». La causa: lo schema JSON vincola cosa il modello scrive ma non gli mostra le scelte. Ora l'elenco dei tipi e degli ambiti è nel prompt, e tipo e ambito si scelgono dopo titolo e sintesi. Su 6 documenti veri di prova: 6 tipi giusti su 6 (prima 0 su 6). Le schede fatte prima sono state rifatte.
- **Le immagini** ora si leggono con l'OCR. Una con meno di 80 caratteri di testo (una foto, un meme) non diventa un documento. Gli screenshot con testo sì: nella pagina hanno il tipo «screenshot».
- **Le copie** (lo stesso testo in due file, per esempio il contratto in Download, compresso, e in iCloud) prendono la scheda del primo senza chiamare il modello e non compaiono due volte nelle ricerche.
- **Cosa resta fuori:** le cartelle di codice (con un `.git`), i pacchetti (app, librerie Foto, pass del Wallet, libri di Books salvati come cartella), i file oltre 50 MB.

### 3.3 La pertinenza

Il reranker dà a ogni risultato una pertinenza tra 0 e 1. Sotto 0,05 il risultato non arriva al modello, e il prompt gli dice che sotto 0,3 di solito non c'entra. Se vedi risposte "Non ho trovato…" a domande che dovrebbero trovare qualcosa, la soglia `MIN_RELEVANCE` in `brain/tools.py` è il primo posto da guardare.

### 3.4 I riassunti, il vault, la privacy

Come nella sessione precedente:
- **Riassunti:** le citazioni puntano ai paragrafi giusti? Gli impegni in `commitments.md` sono veri impegni? I prompt sono in `prompts/`; se li cambi, alza `PROMPT_VERSION` in `brain/summarize.py`.
- **Vault:** fuori dai blocchi `<!-- brain:begin … -->` il testo è tuo; un blocco che modifichi diventa tuo (`uv run brain vault conflicts`).
- **Privacy:** il vault contiene le trascrizioni intere e ora le schede dei tuoi documenti (anche d'identità e sanitari): resta solo locale, con permessi 700/600. Non attivare Obsidian Sync né iCloud su `~/SecondBrain`. I file restano dove sono: brain non li copia.

## 4. Numeri misurati (M2 Pro con GPU a 16 core, 16 GB)

### 4.1 Modello e llama-server

Un riassunto vero (una sessione di 3.331 token, circa 800 token di JSON in uscita) e due domande vere, per configurazione, con il daemon fermo:

| Configurazione | Riassunto | Lettura del prompt | Generazione | Token proposti accettati |
|---|---|---|---|---|
| Q4_K_M, senza drafter (prima) | 88,5 s | 149 token/s | 14,3 token/s | — |
| Q4_K_M + MTP, 2 token alla volta | 66,0 s | 149 | 19,0 | 82% |
| Q4_K_M + MTP, 3 token | 57,7 s | 149 | 22,4 | 79% |
| Q4_K_M + MTP, 4 token | 63,4 s | 149 | 23,1 | 71% |
| Q4_K_M + MTP 3, `-ub 1024` | 63,0 s | 149 | 22,1 | 78% |
| **QAT UD-Q4_K_XL + MTP 3 (scelto)** | **45,5 s** | **167** | **25,4** | 67% |
| QAT Q4_0 di Google + MTP 3 | 44,7 s | 167 | 26,4 | 74% |

- **Il drafter MTP** è un modellino da 0,5 GB che propone i prossimi 3 token; Gemma li controlla tutti in un passaggio. Il testo è identico a quello senza drafter. Rende di più su testi che ripetono l'input (riassunti, citazioni).
- **`-ub 1024`** non aiuta la lettura del prompt su questo Mac: resta 512.
- **Il ragionamento** resta spento (sessione precedente: 3,5 volte più lento, stesse risposte giuste).

**Memoria della GPU** (macOS ne concede 12.124 MiB su 16 GB), dai log di llama-server:

| Server | Prima | Adesso |
|---|---|---|
| Gemma 12B QAT + drafter, contesto 32K | — | 7.350 MiB (pesi 6.637, KV 527, calcolo 185) |
| Embedder | 2.724 MiB (`-c 8192 -ub 8192`) | 1.428 MiB (`-c 2048 -ub 1024`) |
| Reranker | — | 1.656 MiB (`-c 4096 -np 4 -ub 1024`), solo durante le domande |

- **Il contesto resta a 32K:** con la sliding window di Gemma 4 la KV a 32K pesa 527 MB, a 16K 391.
- **La cache dei prompt in RAM** (`--cache-ram`): 8 prompt diversi da 8.000 token fanno crescere il processo di 4,4 GiB con il default (8 GB), di 1,5 GiB con 1,5 GB. Questa RAM si aggiunge ai 7 GB della GPU: era la causa della pressione "warning" e dei 166.000 pageout che ho trovato.

### 4.2 Precisione (`brain eval`, 16 domande sui dati finti, con embedder e reranker)

| | QAT UD-Q4_K_XL (scelto) | QAT Q4_0 | Q4_K_M |
|---|---|---|---|
| risposte corrette | 16/16 | 16/16 | 16/16 |
| chiamate ai tool valide | 22/22 | 22/22 | 20/20 |
| citazioni inventate | 0 | 0 | 0 |
| tempo mediano per domanda | **6,7 s** | 7,2 s | 7,4 s |
| generazione | **29,6 token/s** | 28,3 | 22,2 |
| 7 schede di documenti | 41 s | 43 s | 52 s |

- **Il confronto con prima:** la sessione precedente faceva 9/9 con mediana 10 s, su meno domande e più facili.
- **I limiti dei dati finti:** con 16/16 per tutti, non distinguono più i modelli; serve `eval/questions.local.toml` con domande sui tuoi dati, da lanciare con `brain eval --set real` e il daemon fermo (§2.5).
- **La prima domanda dopo il caricamento** impiega circa 20 s: il prompt di sistema con le descrizioni dei tool (circa 1.900 token) va letto una volta, poi resta in cache.

### 4.3 Reranker

7 domande italiane, ognuna con le risposte giuste, un distrattore difficile (per esempio il podcast che dice anche «ristrutturazione») e 30 paragrafi veri a caso:

| | Qwen3-Reranker-0.6B (scelto) | bge-reranker-v2-m3 |
|---|---|---|
| risposta giusta al primo posto | 7 su 7 | 6 su 7 |
| separazione | netta: giusti ~1,0, sbagliati < 0,03 | punteggi sovrapposti |
| tempo per 37 candidati | 3–4 s con la GPU occupata dai riassunti | ~2 s |

### 4.4 Altri tempi

- **domanda:** di solito 5–10 s; la prima dopo il caricamento ~20 s;
- **riassunto di una sessione vera:** 30–60 s (prima 30 s – 2 min);
- **scheda di un documento:** ~6 s; zero per le copie;
- **scansione dei documenti** (home e iCloud, 184 file): meno di un secondo senza novità;
- **OCR:** circa 0,5 s a pagina.

## 5. Possibili passi successivi (non fatti)

- correggere dall'app la scheda di un documento o nascondere un fatto sbagliato;
- un eval sui tuoi dati veri (`eval/questions.local.toml`);
- i pass del Wallet (biglietti, carte d'imbarco) e le note di Apple Notes come fonti: chiedono permessi in più;
- un launcher `maimaind.app`, per l'Accesso completo al disco stabile e un'icona nella barra dei menu;
- notifiche locali sull'iPhone quando una risposta in coda è pro