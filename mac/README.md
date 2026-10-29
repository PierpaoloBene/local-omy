# local-omy sul Mac

Riceve i segmenti dall'iPhone e li salva in
`~/OmiArchive/raw/audio/AAAA-MM-GG/<id>.bin` + `<id>.json`. La data viene da `start`
nel `.json`, convertito da UTC a Europe/Rome.

Ascolta **solo su 127.0.0.1**: la rete lo raggiunge esclusivamente tramite
`tailscale serve`, che termina l'HTTPS e inoltra le richieste in locale.

## App per Mac

`mac/src/app/` è un'app SwiftUI che vive nella barra dei menu. Non contiene logica propria:
ogni azione lancia `omy --json …` e ne mostra il log.

```bash
mac/src/app/build.sh          # crea mac/build/local-omy.app (firma ad-hoc, senza sandbox)
open mac/build/local-omy.app
```

**Barra dei menu:** l'icona segnala lo stato.

| Icona | Stato |
|---|---|
| onda | tutto ok |
| triangolo | serve un'azione: firma vicina alla scadenza, app da riaprire, setup incompleto |
| ottagono | ricevitore fermo o firma scaduta |

**Schede della finestra:**
- **Stato:** ricevitore, `tailscale serve`, iPhone online sul tailnet, ultimo segmento
  ricevuto, ultimo upload e quanti segmenti restavano in coda sull'iPhone, registrato
  oggi, trascrizione (in corso o quante sessioni mancano), scadenza dell'app iPhone,
  job orario, log.
- **Setup:** la checklist di `omy setup check`. Ogni voce mancante ha il suo pulsante
  "Sistema" (`omy setup fix <voce>`). Ci sono anche:
  - "Copia URL" e "Copia token": il token va negli appunti e non viene mai mostrato;
  - "Apri local-omy al login".
- **iPhone:** scadenza della firma, "Compila e installa", "Rinnova ora", "Reinstalla
  l'ultima build". Ogni azione chiede conferma, perché chiude l'app sul telefono.
- **Trascrizioni:** giorni trascritti e "Trascrivi ora". Durante una trascrizione,
  anche lanciata dal job orario o dal terminale, compare la barra di avanzamento.

La trascrizione gira per conto suo: mentre lavora, Setup, iPhone e l'aggiornamento
dello stato restano utilizzabili. Le azioni di Setup e iPhone, invece, vanno una alla volta.

L'app trova `mac/src` partendo dalla propria posizione (`mac/build/../src`). Se la sposti,
indica il percorso con:
`defaults write com.example.local-omy.mac macSrcPath /percorso/mac/src`.

## Installazione da terminale

Tutti i comandi di questa pagina si lanciano da `mac/src`.

```bash
cd mac/src
uv run omy setup check            # cosa manca
uv run omy setup fix token        # crea ~/.config/local-omy/token (600), mai sovrascritto
uv run omy setup token            # stampa il token, da inserire nell'app iPhone
uv run omy setup fix receiver     # installa/aggiorna il LaunchAgent del ricevitore
uv run omy setup fix periodic     # installa/aggiorna il LaunchAgent del job orario
uv run omy setup fix tailscale    # tailscale serve --bg http://127.0.0.1:8787
uv run omy setup fix models       # modelli di trascrizione
```

I LaunchAgent vengono generati dai template in `launchd/` con i percorsi di questo repo e
un `PATH` che contiene Homebrew e uv. `fix` li reinstalla anche se sposti il repo.

**Ricevitore:**
- Dopo ogni upload accettato scrive `~/OmiArchive/receiver.json`: ora d'arrivo, id del
  segmento e segmenti ancora in coda sull'iPhone (header `X-Queue-Remaining`).
- Log in `~/Library/Logs/local-omy-receiver.log`. Per riavviarlo:
  `launchctl kickstart -k gui/$(id -u)/com.example.local-omy.receiver`.
- `KeepAlive` lo rilancia se si chiude. Se la configurazione è sbagliata (token assente,
  o permessi diversi da 600) esce con errore nel log e launchd ritenta ogni ~10 s.

### Esposizione sul tailnet

`omy setup fix tailscale` esegue `tailscale serve --bg http://127.0.0.1:8787`. L'URL da mettere nell'app è `https://<nome-mac>.<tailnet>.ts.net`, per esempio
`https://mio-mac.tail1234.ts.net`. **Non usare `tailscale funnel`**:
renderebbe il servizio pubblico su Internet. `serve` lo rende visibile solo ai
dispositivi del tuo tailnet.

## App iPhone: installazione e rinnovo (al posto di AltStore)

`omy` compila l'app, la firma con il Personal Team gratuito del tuo Apple ID (quello
collegato a Xcode, in Impostazioni → Account) e la installa con `devicectl`, via cavo o
Wi-Fi. Con un account gratuito il profilo dura **7 giorni**.

```bash
uv run omy iphone install          # telefono sbloccato e vicino: build, firma, installa
uv run omy iphone install --no-build   # reinstalla l'ultima build (se è fallito solo l'install)
uv run omy iphone status           # scadenza, giorni rimasti, serve riaprire l'app?
uv run omy iphone renew            # reinstalla solo se mancano < 3 giorni, e di notte (01-08)
uv run omy iphone renew --force    # reinstalla subito
```

Aggiungi `--json` subito dopo `omy` per avere il risultato in JSON. I log vanno su stderr.

**Prima installazione:**
- Sul telefono deve essere attiva la Modalità sviluppatore (Impostazioni → Privacy e
  sicurezza).
- `install` registra l'iPhone nel team, quindi il telefono deve essere sbloccato.
- Al primo avvio dell'app iOS chiede di autorizzare lo sviluppatore (Impostazioni →
  Generali → VPN e gestione dispositivi).
- Se `codesign` chiede l'accesso al portachiavi, rispondi "Consenti sempre": il job
  periodico non può rispondere al posto tuo.
- Team id e UDID vengono rilevati la prima volta e salvati in
  `~/.config/local-omy/config.toml` (600). Il team **non** viene mai scritto nel progetto
  Xcode.

L'app installata così ha il bundle id `com.example.local-omy` e **non** è la stessa
dell'app installata da AltStore, che aggiunge un suffisso al bundle id:
1. lascia che la vecchia app finisca di caricare i segmenti in coda;
2. nella nuova app reinserisci URL e token;
3. cancella la vecchia app.

**Rinnovo automatico:** il LaunchAgent `periodic` lancia `omy periodic` ogni ora.
- Il rinnovo parte solo quando la firma scade entro 3 giorni e solo tra l'01 e le 08,
  perché reinstallare chiude l'app sull'iPhone e iOS non la riapre da sola. Se manca meno
  di un giorno, prova a qualsiasi ora.
- La build è generica e non richiede il telefono. L'installazione invece sì: deve essere
  sulla stessa rete e potrebbe dover essere sbloccato. Se non riesce, riprova l'ora
  dopo.
- Dopo ogni reinstallazione arriva una notifica sul Mac: riapri l'app.
- `status` riporta `needs_reopen: true` finché sul Mac non arriva un segmento iniziato dopo
  l'installazione.

Installalo **dopo** il primo `omy iphone install`, con `omy setup fix periodic` o dalla
scheda Setup. Log in `~/Library/Logs/local-omy-periodic.log`.

## Trascrizione locale

Gira tutta sul Mac con [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (`brew install
whisper-cpp`): modello `large-v3-turbo`, sulla GPU con Metal. Niente esce dal Mac.

```bash
uv run omy setup models               # una volta: ~1,6 GB in ~/OmiArchive/models, sha256 verificato
uv run omy transcribe --pending       # sessioni chiuse non ancora trascritte (ultimi 7 giorni)
uv run omy transcribe --day 2026-10-27 [--force]
```

**Sessioni:** il telefono spezza l'audio in segmenti da 5 minuti, ma è solo trasporto.
- Segmenti consecutivi dello stesso ciondolo, separati da meno di 2 s, sono una
  **sessione**: un tratto continuo di registrazione. Il raggruppamento usa solo i
  sidecar `.json`.
- Una sessione appartiene al giorno (ora di Roma) in cui inizia.
- È **chiusa** quando non arrivano nuovi segmenti da 10 minuti. Solo le sessioni chiuse
  vengono trascritte.
- I segmenti possono arrivare con ore di ritardo, per esempio se il Mac era in stop e
  l'iPhone li ha tenuti in coda. Se una sessione già trascritta si allunga, vengono
  trascritti solo i segmenti nuovi e accodati al suo JSON. Se invece cambia in altro modo,
  la sessione viene ritrascritta da capo.

**Come trascrive:**
1. L'audio della sessione viene decodificato in blocchi da ~30 minuti, tagliati ai
   confini dei segmenti: così whisper non deve caricare ore di audio. Raramente una
   parola si spezza tra due blocchi.
2. I pacchetti persi diventano PLC di Opus, quindi la linea del tempo resta esatta: ogni
   frame vale 20 ms.
3. Il **VAD Silero** di whisper.cpp salta i silenzi. Questo riduce anche le frasi inventate
   da Whisper sul rumore.
4. **Nessun contesto fra una finestra e l'altra** (`-mc 0`): elimina i loop di ripetizione
   (es. "Grazie a tutti." ripetuto per mezzo minuto).
5. **Lingua rilevata in automatico** per ogni blocco, sui primi 30 s di parlato, e salvata in
   ogni frase. Forzare l'italiano faceva tradurre male il parlato inglese. Limite: un blocco
   che passa da una lingua all'altra viene trascritto tutto nella prima.
6. I WAV temporanei stanno in `~/OmiArchive/cache/` e vengono cancellati subito dopo l'uso.

**Output** (derivato: si rigenera con `--force`, il raw non viene mai toccato):

| File | Cosa |
|---|---|
| `~/OmiArchive/transcripts/AAAA-MM-GG/<sessione>.json` | frasi con inizio/fine in UTC, segmenti d'origine, modello |
| `~/OmiArchive/transcripts/AAAA-MM-GG.md` | la giornata da leggere: una sezione per ogni sessione con parlato, un paragrafo nuovo dopo ogni silenzio > 2 min o dopo 5 min di parlato |

Il markdown viene ripulito, mentre i JSON restano grezzi:
- una frase ripetuta più volte di fila (un loop di whisper) compare una volta sola;
- le frasi tipiche che whisper inventa sul rumore ("Thank you.", "Grazie per la visione."…)
  vengono tolte se nei 30 s prima e dopo non c'è altro parlato;
- le sessioni senza parlato non compaiono.

`uv run omy transcribe --pending --render` rigenera il markdown dai JSON senza ritrascrivere.

Il job `periodic` trascrive ogni ora le sessioni appena chiuse. Un lock impedisce che due
trascrizioni girino insieme.

Mentre lavora, `~/OmiArchive/cache/transcribe-progress.json` registra pid, sessioni fatte
e totali e minuti di audio fatti e totali. `omy status` e l'app lo leggono, e alla fine il
file viene cancellato. Il markdown di un giorno si riscrive appena ne finisce l'ultima sessione.

## Configurazione (variabili d'ambiente)

| Variabile | Default | |
|---|---|---|
| `OMY_TOKEN_FILE` | `~/.config/local-omy/token` | deve essere tuo, file regolare (niente symlink), mode 600 |
| `OMY_TOKEN` | — | alternativa al file (sconsigliata: finisce nell'ambiente del processo) |
| `OMY_ARCHIVE` | `~/OmiArchive` | |
| `OMY_PORT` | `8787` | l'host è sempre 127.0.0.1 e non si può cambiare |
| `OMY_MAX_BODY` | 32 MiB | un segmento da 5 minuti pesa ~1,3 MB |

## Sicurezza

Controlli eseguiti, in quest'ordine, prima di leggere il corpo:
- **Autenticazione:** `Authorization: Bearer`. Il token viene confrontato in tempo costante
  (`hmac.compare_digest` sugli sha256 dei due valori, così nemmeno la lunghezza trapela).
  Il token deve avere almeno 32 caratteri, altrimenti il server non parte.
- **Segment id:** `[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}` con `fullmatch` e classi ASCII esplicite
  (niente `\d`, che accetterebbe cifre Unicode). Deve coincidere con `X-Segment-Id`.
  Nessun altro input dell'utente entra nei percorsi dei file: la cartella del giorno è
  calcolata dalla data.
- **Metadati:** `X-Segment-Meta` al massimo 4 KB, JSON con esattamente i campi attesi e
  i tipi giusti. `id` e `sha256` devono coincidere con la richiesta, e `start` deve
  coincidere con l'istante scritto nell'id.
- **Dimensione del corpo:** `Content-Length` viene controllato subito, e i byte vengono
  contati anche durante lo streaming (risposta 413).

Poi:
- **Scrittura atomica:** il corpo va in un file temporaneo in `~/OmiArchive/tmp`
  (stesso filesystem) mentre se ne calcola lo sha256, poi fsync. Se l'hash non coincide
  con `X-Sha256` → 400. Si controlla anche il magic `OMIS` e la versione. Infine il
  `.json` e il `.bin` vengono spostati con `os.replace`, e viene fatto fsync della cartella.
  I file temporanei vengono sempre rimossi.
- **Idempotenza:** stesso id con stesso hash → 200 `duplicate`. Stesso id con hash diverso
  → 409, e il file esistente non viene toccato. La verifica e lo spostamento avvengono
  sotto un lock.
- **Altro:**
  - cartelle create con permessi 700 e `umask 077`;
  - `/docs`, `/redoc` e `/openapi.json` disabilitati;
  - niente header `Server`;
  - `proxy_headers` disattivati.

## Analisi: `tools/inspect_segment.py`

Serve libopus (`brew install opus`).

```bash
# Un segmento: report dei pacchetti mancanti + WAV 16 kHz mono accanto al .bin
uv run tools/inspect_segment.py ~/OmiArchive/raw/audio/2026-10-27/20261027T081530Z-3fa2c91b.bin
uv run tools/inspect_segment.py SEG.bin --wav /tmp/out.wav
uv run tools/inspect_segment.py SEG.bin --no-wav

# Report di un'intera giornata
uv run tools/inspect_segment.py --day 2026-10-27
```

Come legge i dati:
- Toglie l'header di 3 byte e riunisce i frammenti di uno stesso frame (frammento
  0, 1, … con indici consecutivi).
- Decodifica l'Opus con libopus. Al posto dei pacchetti mancanti e dei frame rotti inserisce
  il **packet loss concealment** di Opus: la durata del WAV resta allineata al tempo reale.
  `--no-fill` lo disattiva.
- **Mancanti:** salto di indice modulo 65536, meno uno.
- **Discontinuità:** il salto è più grande di quanto il tempo trascorso possa spiegare
  (riavvio del ciondolo, indice ripartito da zero) oppure l'indice è ripetuto. Le
  discontinuità non vengono contate come perdita.
- Il report giornaliero controlla la continuità dell'indice anche **tra segmenti
  consecutivi** (rotazione, distacco < 2 s). Un distacco più lungo, o un cambio di
  ciondolo, viene riportato come **tempo non registrato**: è audio finito sulla SD del
  ciondolo o mai registrato, non un pacchetto perso (vedi il README principale).
