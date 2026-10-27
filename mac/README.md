# Ricevitore sul Mac

Riceve i segmenti dall'iPhone e li salva in
`~/OmiArchive/raw/audio/AAAA-MM-GG/<id>.bin` + `<id>.json`. La data viene da `start`
nel `.json`, convertito da UTC a Europe/Rome.

Ascolta **solo su 127.0.0.1**: la rete lo raggiunge esclusivamente tramite
`tailscale serve`, che termina l'HTTPS e inoltra le richieste in locale.

## Installazione

Tutti i comandi di questa pagina si lanciano da `mac/src`.

```bash
cd mac/src
uv sync

# Token: 32 byte casuali, file leggibile solo da te
mkdir -p ~/.config/local-omy && chmod 700 ~/.config/local-omy
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/local-omy/token
chmod 600 ~/.config/local-omy/token
cat ~/.config/local-omy/token            # da copiare nelle impostazioni dell'app
```

### Avvio al login (LaunchAgent)

```bash
PLIST=~/Library/LaunchAgents/com.example.local-omy.receiver.plist
sed -e "s|__UV__|$(command -v uv)|" -e "s|__MAC_DIR__|$PWD|g" -e "s|__HOME__|$HOME|g" \
    launchd/com.example.local-omy.receiver.plist > "$PLIST"
launchctl bootstrap gui/$(id -u) "$PLIST"
tail -f ~/Library/Logs/local-omy-receiver.log
```

- Per riavviarlo dopo un aggiornamento: `launchctl kickstart -k gui/$(id -u)/com.example.local-omy.receiver`
- Per toglierlo: `launchctl bootout gui/$(id -u)/com.example.local-omy.receiver`
- Se sposti il repo: `bootout`, poi rigenera il plist con il `sed` qui sopra e rifai `bootstrap`.

`KeepAlive` lo rilancia se si chiude. Se la configurazione è sbagliata (token assente, o
permessi diversi da 600) esce con errore nel log e launchd ritenta ogni ~10 s.

### Esposizione sul tailnet

```bash
tailscale serve --bg http://127.0.0.1:8787
tailscale serve status
```

L'URL da mettere nell'app è `https://<nome-mac>.<tailnet>.ts.net`, per esempio
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

Installalo **dopo** il primo `omy iphone install`:

```bash
PLIST=~/Library/LaunchAgents/com.example.local-omy.periodic.plist
sed -e "s|__UV__|$(command -v uv)|" -e "s|__MAC_DIR__|$PWD|g" -e "s|__HOME__|$HOME|g" \
    -e "s|__PATH__|$PATH|" launchd/com.example.local-omy.periodic.plist > "$PLIST"
launchctl bootstrap gui/$(id -u) "$PLIST"
tail -f ~/Library/Logs/local-omy-periodic.log
```

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
