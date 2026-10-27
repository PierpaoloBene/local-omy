# Ricevitore sul Mac

Riceve i segmenti dall'iPhone e li salva in
`~/OmiArchive/raw/audio/AAAA-MM-GG/<id>.bin` + `<id>.json`. La data viene da `start`
nel `.json`, convertito da UTC a Europe/Rome.

Ascolta **solo su 127.0.0.1**: la rete lo raggiunge esclusivamente tramite
`tailscale serve`, che termina l'HTTPS e inoltra le richieste in locale.

## Installazione

```bash
cd mac
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
