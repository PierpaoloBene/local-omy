# maimaind: piano

Decisioni prese il 29 ottobre 2026. Lo stato di ogni milestone è in fondo.

## Vincoli

- Tutto in locale: niente cloud, niente API esterne.
- Da local-omy si legge solo l'interfaccia documentata in `mac/README.md` ("Output per
  altre app"): `~/OmiArchive/transcripts/AAAA-MM-GG.jsonl` e la CLI `omy --json`. Il repo
  di local-omy non si modifica.
- `person` è la chiave stabile. "Persona N" e "Sconosciuto N" valgono solo nella loro
  sessione. I paragrafi `via.kind == "media"` sono "cose che ho sentito" e non si confondono
  mai con ciò che ho detto o che mi è stato detto.

## Decisioni

| | |
|---|---|
| Nome | maimaind; CLI `brain`; bundle id `com.<team>.maimaind` |
| Modello | Gemma 4 12B QAT (UD-Q4_K_XL) con il drafter MTP (speculative decoding), `llama-server` con Metal su 127.0.0.1:8790 e chiave casuale; ragionamento spento (misurato: 3,5 volte più lento, stesse risposte giuste); cache dei prompt in RAM limitata a 1,5 GB. Profili in `config.py`: `gemma`, `gemma-qat-q4_0`, `gemma-q4km`, `gemma-think`, `qwen` |
| Embedding | Qwen3-Embedding-0.6B Q8_0 (640 MB, 1024 dimensioni), secondo `llama-server --embedding` sulla porta 8791 (M3) |
| Reranker | Qwen3-Reranker-0.6B Q8_0, terzo `llama-server --rerank` sulla porta 8792, caricato solo per le domande (M8) |
| Token | quello di local-omy, letto con `omy --json setup token` |
| Trasporto | API su 127.0.0.1:8788, `tailscale serve --bg --https=8443 http://127.0.0.1:8788`; mai funnel |
| Documenti | Accesso completo al disco; tutta la home con esclusioni (`~/Library`, `~/OmiArchive`, `~/SecondBrain`, cartelle nascoste, `.app` e altri pacchetti, `node_modules`, ambienti virtuali, cartelle con `.git`), più iCloud Drive (`~/Library/Mobile Documents`) e `~/Library/CloudStorage` come radici proprie (M8) |
| Vault | `~/SecondBrain`, solo locale, compatibile con Obsidian; l'indice sta fuori dal vault |
| Chat | nel vault, in `chats/AAAA/MM/<id>.md`, e anche nell'indice |
| Interfaccia | web UI (una pagina statica servita dall'API) prima dell'app SwiftUI |

## Vault `~/SecondBrain/` (M2)

```
days/AAAA-MM-GG.md
sessions/AAAA/MM/GG/<sessione>.md     riassunto, impegni, fatti, "cose che ho sentito", trascrizione con ^p<ms>
people/<person-id>.md                 aliases: [Nome]; blocco "Fatti" dal database, "Chi è" dal modello
topics/<slug>.md
commitments.md                        checkbox `- [ ] … ^c<id>`: la spunta la metti tu ed è la fonte di verità
documents/AAAA/<slug>-<hash8>.md
chats/AAAA/MM/<id>.md
```

- **Citazioni:** `[[<sessione>#^p65610|06:56 Io]]`.
- **Contenuto generato:** sta tra `<!-- brain:begin X h=… -->` e `<!-- brain:end X -->`.
  Fuori dai marcatori il testo è tuo. Se modifichi dentro un blocco l'hash non coincide più,
  il blocco diventa tuo e compare in `brain vault conflicts`.
- **Il vault è la fonte di verità** per riassunti, impegni, schede documento e chat.
  L'indice si ricostruisce da JSONL + vault (`brain reindex`) senza rilanciare il modello.

## Estrazione per sessione (M2)

**Input:**
- paragrafi rinumerati `[P1]…[Pn]`, con ora, speaker e tag `[MEDIA app]`;
- elenco delle persone nominate;
- data e giorno della settimana, per risolvere "domani" e "venerdì".

**Output:** JSON vincolato con `response_format: json_schema`:
`{title, summary[{text,refs}], topics[{name,refs}], commitments[{who,to,what,due,due_text,refs}],
facts[{about,fact,refs}], heard[{source,summary,refs}], quality}`.

**Controlli nel codice:**
- i refs devono esistere;
- `who` deve coincidere con il `person` del paragrafo citato;
- un ref `[MEDIA]` può stare solo in `heard`;
- un `about` che non è una persona nominata diventa null.

**Sessioni oltre circa 12.000 token:** finestre (map), poi un passo di reduce per titolo e
riassunto.

## Scheduler (M4)

- **Ogni minuto, senza modello:** controllo dell'mtime dei `.jsonl`; file nuovi o modificati
  trovati con `mdfind`; scansione completa una volta a settimana.
- **Ogni `t` (default 60 min), giro con il modello:** riassunti, embedding, documenti,
  profili. Parte anche quando local-omy finisce una trascrizione. Gira solo con il gate
  aperto: `omy --json status` → `transcripts.progress.running == false`, pressione di
  memoria normale e memoria disponibile sopra soglia.
- **Domanda mentre local-omy trascrive:**
  1. risposta immediata con la ricerca testuale;
  2. `SIGINT` al pid del file di avanzamento (i suoi `finally` ripuliscono WAV, lock e file
     di avanzamento; le scritture sono atomiche). Se alla fine della trascrizione mancano
     meno di 2 minuti, aspetto;
  3. carico Gemma e rispondo;
  4. dopo `idle_minutes` scarico Gemma, lancio `omy transcribe --pending --render` (riscrive
     i `.jsonl` che la diarizzazione interrotta avrebbe lasciato indietro), poi
     `omy transcribe --pending` se `auto_transcribe` è acceso, e ti mando una notifica.
- **Prima di toccare l'archivio vero:** collaudo su una copia con `OMY_ARCHIVE`.
- **Priorità:** chat > sync > embedding > riassunti > documenti > profili. Una chat
  interrompe il lavoro in corso del modello, che torna in coda.

## Misure (M1, M2 Pro 16 GB)

- **Prefill:** circa 150 token/s (167 col modello QAT), il collo di bottiglia. Per questo i tool restituiscono al
  massimo circa 4.000 token a domanda e il prompt di sistema resta uguale tutto il giorno,
  così va in cache.
- **Generazione:** circa 15 token/s; 26–29 col modello QAT e il drafter MTP (M8).
- **Memoria:** 6,6 GiB di pesi + circa 1 GiB di KV a 32K. Con il Mac in uso la pressione può
  arrivare a "warning".

## Milestone

| | Contenuto | Stato |
|---|---|---|
| M1 | sync JSONL, FTS5 (+ trigram), `brain ask` con tool e citazioni, `brain doctor`, `brain eval` | fatto |
| M2 | riassunti di sessione, vault, `commitments.md`, `list_commitments`, profili, `brain reindex` | fatto |
| M3 | embedding Qwen3-Embedding-0.6B, `search_semantic`, retrieval ibrido (sqlite-vec) | fatto |
| M4 | worker + gate + pausa e ripresa di local-omy + LaunchAgent + `brain setup` | fatto |
| M5 | documenti: MD, TXT, DOCX (stdlib), PDF e OCR (`helpers/extract`: PDFKit + Vision), schede, `find_document` | fatto |
| M6 | API (chat in streaming, sfoglia, impegni, ricerca, download protetto) + pagina web | fatto |
| M7 | app SwiftUI + `brain iphone install/renew` | fatto (compila; non ancora installata: NOTE.md) |
| M8 | precisione ed efficienza: drafter MTP, Gemma QAT, limiti di memoria di llama-server; iCloud Drive e ~25 formati; ricerca ibrida con RRF e reranker, prima ricerca fatta dal codice; schede con ambito e cartella, copie riconosciute; pagina web rifatta; eval a 16 domande | fatto (NOTE.md) |

## Scostamenti dal piano, decisi strada facendo

- **Ragionamento di Gemma 4 spento.** Con il ragionamento acceso le risposte sono 3,5 volte più lente e quelle giuste restano le stesse. A volte il ragionamento consumava tutto lo spazio e lasciava la risposta vuota.
- **Pressione di memoria a modello caricato.** Con Gemma in memoria, su 16 GB, la pressione "warning" è normale: a modello già caricato il gate ferma il lavoro solo con pressione critica. Prima di caricarlo resta il controllo rigoroso.
- **Riassunti senza modello per le sessioni minime.** Le sessioni con meno di 60 caratteri ("Ok, ciao.") non passano dal modello. La soglia iniziale di 150 perdeva promesse brevi come «prenoto io il volo».
- **`brain ask` passa dal daemon quando gira.** Così le domande fatte dal Mac finiscono nella stessa storia delle chat dell'iPhone.
- **Rinnovo dell'app iPhone tra le 04 e le 08** (local-omy rinnova dalle 01): due build insieme non stanno in memoria.
- **Domande senza giro obbligatorio di ricerca (M8).** Prima il modello doveva chiamare un tool al primo giro. Ora la prima ricerca la fa il codice e il modello la riceve già fatta: un giro in meno quando basta (12 domande su 16 dell'eval).
- **Contesto del modello a 32K anche con tre server (M8).** Con la sliding window di Gemma 4 la KV a 32K pesa solo 527 MB: scendere a 16K ne farebbe risparmiare 136. Lo spazio sulla GPU si è trovato riducendo embedder e reranker, che con `-ub 8192` occupavano 2,7 GB ciascuno.
