# Cutover: local-omy dal Mac all'hub

Checklist operativa. Il piano e le scelte stanno in [hub-migration.md](hub-migration.md); questa è la sequenza da eseguire.
Quasi tutto è automatizzato da **`scripts/cutover.sh`**: ogni passo dice cosa farà e si ferma a chiedere conferma
(`--dry-run` mostra i comandi senza cambiare niente, `--from N` riparte da un passo, `--rollback` torna indietro).
Qui sotto, per ogni passo, i comandi esatti a mano.

Durata ~20 minuti più l'attesa dello smoke test (la sessione si chiude 10 minuti dopo l'ultimo segmento).
Quando: iPhone in casa, nessuna registrazione in corso, Mac sveglio e in carica.

## Stato prima del cutover (già fatto, in parallelo)

- Hub: servizio `omy` in `compose.yaml` (immagine `omy-hub:local`, scheduler **spento**: `OMY_HUB_SCHEDULER=off`), dati in
  `/srv/hub/data/omy` (uno **snapshot di prova** di `~/OmiArchive`, da rifare al passo 2), segreti in `/srv/hub/secrets/omy.env`,
  `tailscale serve --https=10000 → 127.0.0.1:8787`, widget Omi sulla dashboard, backup con i `*.sqlite`.
- Mac: `~/.config/local-omy/compute_token` (600), `~/OmiCompute` (workspace) e i LaunchAgent `compute` e `vault-mirror`
  **installati ma disabilitati e non caricati**. Il receiver e il worker originali girano ancora e l'iPhone punta ancora al Mac.
- Il Mac e l'hub hanno lo stesso commit (`git -C /srv/hub/apps/local-omy log -1` = `git log -1` qui): è la prima cosa che controlla il passo 0,
  e comunque l'hub non assegna al Mac i lavori di una pipeline con versione diversa (il widget lo segnala).

## Passi

### 0. Controlli (non cambia niente)

```bash
scripts/cutover.sh --only 0
```
Controlla: ssh all'hub, token, stesso commit, container `omy` su, `serve` su :10000, API dell'hub che risponde, scheduler ancora spento,
spazio libero sull'hub, agent del Mac, nessun `compute` già caricato.

### 1. Ferma il receiver e il worker del Mac

```bash
launchctl bootout gui/$(id -u)/com.pierpaolobene.local-omy.worker      # il job in corso torna in coda (SIGTERM)
launchctl bootout gui/$(id -u)/com.pierpaolobene.local-omy.receiver
launchctl disable gui/$(id -u)/com.pierpaolobene.local-omy.worker      # non ripartono al prossimo login
launchctl disable gui/$(id -u)/com.pierpaolobene.local-omy.receiver
tailscale serve status                                                  # guarda cosa pubblica il Mac
tailscale serve --https=443 off                                         # chiude https://macbook-pro-di-peppo…ts.net
```
Da qui gli upload dell'iPhone falliscono e **restano sul telefono** (`Documents/segments`): nessuna perdita, riprovano con backoff.

### 2. Copia finale Mac → hub

```bash
ssh hub@hub.local 'cd /srv/hub/repo && docker compose stop omy'                  # niente scrive mentre si copia
cd ~/OmiArchive
rsync -ain --checksum --delete -e ssh raw transcripts brain voices context places.json settings.json receiver.json \
      hub@hub.local:/srv/hub/data/omy/ | grep -E '^(>f|\*deleting)'               # anteprima
rsync -a --checksum --delete --stats -e ssh raw transcripts brain voices context places.json settings.json receiver.json \
      hub@hub.local:/srv/hub/data/omy/
# l'indice di ricerca: copia consistente, non il file vivo
sqlite3 ~/OmiArchive/cache/brain-index.sqlite ".backup /tmp/brain-index.sqlite"
ssh hub@hub.local 'rm -f /srv/hub/data/omy/cache/brain-index.sqlite-wal /srv/hub/data/omy/cache/brain-index.sqlite-shm /srv/hub/data/omy/db/jobs.sqlite*'
scp /tmp/brain-index.sqlite hub@hub.local:/srv/hub/data/omy/cache/ && ssh hub@hub.local 'chmod 600 /srv/hub/data/omy/cache/brain-index.sqlite'
```
`models/` e le cache ricostruibili non si copiano. `--delete` rende l'hub una copia esatta (toglie i resti della prova).
Il rsync di sistema del Mac è `openrsync`: va bene così (niente `--info=progress2`, usare `--stats`).

### 3. Verifica (conteggi, hash, permessi)

```bash
python3 mac/src/tools/verify_archive.py ~/OmiArchive > /tmp/mac.json
ssh hub@hub.local 'python3 - /srv/hub/data/omy' < mac/src/tools/verify_archive.py > /tmp/hub.json
diff <(python3 -c "import json;d=json.load(open('/tmp/mac.json'));d.pop('root');print(json.dumps(d,indent=1))") \
     <(python3 -c "import json;d=json.load(open('/tmp/hub.json'));d.pop('root');print(json.dumps(d,indent=1))") && echo IDENTICI
rsync -ain --checksum --delete -e ssh ~/OmiArchive/{raw,transcripts,brain,voices,context} hub@hub.local:/srv/hub/data/omy/   # nessuna riga >f
ssh hub@hub.local 'find /srv/hub/data/omy -type f ! -perm 600 -not -path "*/cache/*" | wc -l; find /srv/hub/data/omy ! -user hub | wc -l'   # 0 e 0
```
Devono coincidere: segmenti `.bin` e `.json` (oggi 1114 + 1114, 15 giorni), `transcripts/*.jsonl`, sessioni, `speakers`, note (60), file di `voices`,
`manifest_sha256` (l'hash di tutti i file di `raw/`), e nessun segmento con hash diverso dal suo sidecar. Se qualcosa non torna: **fermati**, il Mac
non è stato toccato nei dati (rollback = passo "Rollback").

### 4. L'hub prende il comando

```bash
ssh hub@hub.local "sed -i 's/^OMY_HUB_SCHEDULER=.*/OMY_HUB_SCHEDULER=on/' /srv/hub/secrets/omy.env"
ssh hub@hub.local 'cd /srv/hub/repo && docker compose up -d omy && docker compose logs --tail 5 omy'    # "hub-worker started"
TOKEN=$(cat ~/.config/local-omy/token)        # (lo script usa curl --config: il token non compare mai nei processi)
curl -sS -H "Authorization: Bearer $TOKEN" https://hub.tail7fcf68.ts.net:10000/v1/transcripts | python3 -m json.tool | head -20
cd mac/src && uv run omy --json transcripts list | python3 -c "import json,sys;print(json.load(sys.stdin)['days'][0])"   # stessi giorni dell'hub
```
Fuso orario e lingua: i giorni devono essere gli stessi e le voci "Persona 1"/"Io" (`OMY_TZ=Europe/Rome`, `language = "it"` sono già nel compose e in `config.toml` dell'hub).
Alla prima scansione l'hub mette in coda quello che manca (oggi: 3 sessioni che il Mac non riusciva a trascrivere, `read_audio_data … miniaudio`, e una nota);
riproveranno 3 volte e poi restano `failed` (pulsante "Riprova" nel widget): è lo stesso comportamento di prima, non un errore della migrazione.

### 5. Il Mac lavora per l'hub

```bash
cd mac/src
uv run omy setup hub https://hub.tail7fcf68.ts.net:10000     # omy e l'app del Mac leggono/scrivono sull'hub (hub_url in config.toml)
uv run omy setup fix compute-on                              # abilita e carica il LaunchAgent compute
uv run omy setup fix vault-mirror-on                         # copia del vault in ~/OmiBrain ogni 10 minuti
tail -f ~/Library/Logs/local-omy-compute.log                 # "compute worker started"
curl -sS -H "Authorization: Bearer $(cat ~/.config/local-omy/token)" https://hub.tail7fcf68.ts.net:10000/v1/widget | python3 -m json.tool | head -30
```
Nel widget: `mac.online: true`, `mac.version_ok: true`, `mac.kinds` con `transcribe, diarize, organize, embed, ask`.
La **app a barra dei menu** va riaperta (`omy` ora parla con l'hub): scheda Stato, Persone, Luoghi, Second brain.
L'app continua a scrivere il contesto audio in `~/OmiArchive/context`: il compute worker lo carica sull'hub prima di ogni diarizzazione e tiene
`~/OmiArchive/settings.json` allineato alle funzioni dell'hub (es. `mac_audio` spento dall'iPhone).

### 6. iPhone

Impostazioni → URL del Mac → `https://hub.tail7fcf68.ts.net:10000`. Il token resta (campo vuoto = tiene quello salvato). Nessuna reinstallazione.
I segmenti rimasti sul telefono salgono da soli, in ordine; `duplicate` evita doppioni. Controllo: "Oggi" mostra la coda che si svuota e il widget Omi
sulla dashboard mostra l'ultimo upload.

### 7. Smoke test: un segmento vero arriva e il Mac lo trascrive

1. Parla al ciondolo qualche frase. Il segmento arriva sull'hub: `ssh hub@hub.local 'ls -t /srv/hub/data/omy/raw/audio/*/*.bin | head -1'`.
2. Dopo 10 minuti dall'ultimo segmento la sessione è chiusa; "Trascrivi ora" nell'app (o `curl -X POST …/v1/transcribe`) sveglia lo scheduler.
3. Il widget Omi mostra "Trascrizione … in corso"; il Mac scarica l'audio, whisper lo trascrive, l'hub committa:
   `ssh hub@hub.local 'ls -t /srv/hub/data/omy/transcripts/*/*.json | head -3'` e nell'app la giornata ha il testo nuovo (poi le voci e, di notte, la nota).
4. Prova "Chiedi": con il Mac acceso risponde come prima; con il Mac spento (`omy setup fix compute-off`, poi `compute-on`) la domanda resta "in attesa del Mac" e la risposta arriva quando si riaccende.

`scripts/cutover.sh --from 6` fa le attese e i controlli dei passi 6 e 7.

### 8. A mano, dopo

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 00:55:00     # il Mac si risveglia ogni notte alle 00:55 per organizzare (01-07)
pmset -g sched                                       # controllo;  per togliere: sudo pmset repeat cancel
```
Di notte il Mac deve stare **in carica** (l'organizzatore parte solo con l'alimentatore) e col **coperchio aperto** o con un monitor collegato:
con il coperchio chiuso e senza monitor macOS si riaddormenta e le note aspettano la notte dopo. Finito il lavoro il Mac torna a dormire da solo.
Se alle 07:00 ci sono ancora note da organizzare il widget Omi lo scrive ("Organizzazione notturna non fatta: N in attesa").

## Cosa cambia per te

- **iPhone**: solo l'URL. "Chiedi" a Mac spento mostra "In attesa del Mac" (serve l'app aggiornata: `uv run omy iphone install`; le app vecchie mostrano il messaggio di cortesia come risposta).
- **App del Mac**: stesse schede, dati dall'hub. "Apri vault" apre `~/OmiBrain`.
- **Obsidian**: apri `~/OmiBrain` (copia in **sola lettura** dell'hub, aggiornata ogni 10 minuti; le modifiche fatte lì **vengono sovrascritte**; `.obsidian` si salva).
  Le spunte in `impegni.md` si fanno sull'hub (file nel vault dell'hub), non nella copia.
- **`transcripts/*.jsonl`**: restano sull'hub (nessuno li legge oggi); `GET /v1/export/days` e `/v1/export/GIORNO.jsonl` sono pronti per altre app.
- **Backup**: l'audio e i `voices/` sono nel backup notturno dell'hub (03:30); `cache/` e `tmp/` no (si ricostruiscono). Prova di ripristino il 1° del mese (conta anche i `.bin`).
- **`~/OmiArchive` sul Mac**: copia fredda, **da tenere 4 settimane** dal cutover (non cancellarla, non riavviare i vecchi agent). Dopo, e solo dopo un ripristino restic riuscito:
  archiviarla (`mv ~/OmiArchive ~/OmiArchive.old`, tenendo `models/` e `voices/enroll/`) e togliere i vecchi plist (`com.pierpaolobene.local-omy.receiver` e `.worker`).

## Rollback

**Prima che arrivino dati nuovi sull'hub** (passi 1-5, o 6 senza upload ancora): i dati del Mac non sono stati toccati.

```bash
scripts/cutover.sh --rollback
# a mano:
ssh hub@hub.local 'cd /srv/hub/repo && docker compose stop omy'
cat ~/.config/home-hub/hub-password.txt | ssh hub@hub.local 'sudo -S -p "" tailscale serve --https=10000 off'
cd mac/src && uv run omy setup fix compute-off && uv run omy setup fix vault-mirror-off && uv run omy setup hub --off
for a in receiver worker; do launchctl enable gui/$(id -u)/com.pierpaolobene.local-omy.$a
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pierpaolobene.local-omy.$a.plist; done
tailscale serve --bg http://127.0.0.1:8787                    # https://macbook-pro-di-peppo.tail7fcf68.ts.net torna
```
iPhone: URL → `https://macbook-pro-di-peppo.tail7fcf68.ts.net`. I segmenti in coda sul telefono partono.

**Dopo che l'hub ha ricevuto dati nuovi** (dal passo 6 in poi): prima si congela l'hub, poi si copia indietro il delta, poi si riparte.

```bash
scripts/cutover.sh --rollback --with-new-data
# a mano, dopo lo stop dell'hub (sopra):
rsync -a --checksum -e ssh hub@hub.local:/srv/hub/data/omy/{raw,transcripts,brain,voices,context,places.json,settings.json} ~/OmiArchive/   # senza --delete
cd mac/src && uv run omy index --all                            # riallinea i vettori
```
Il formato dell'archivio è identico nei due sensi, il Mac ha ancora codice e modelli (non si rimuove nulla): il worker originale riscansiona e rifà ciò che manca.

## Rischi e cose da sapere

- **Mac in stop**: i lavori aspettano sull'hub (la trascrizione riparte quando il Mac si sveglia, o a "Trascrivi ora" con il Mac acceso). L'audio è al sicuro sull'hub, che fa anche il backup.
- **Versioni**: se si aggiorna il codice solo da un lato (pipeline, prompt, schema) l'hub non assegna più quel tipo di lavoro e il widget dice "versione diversa: aggiorna il Mac". Aggiornare = `git pull` su entrambi, `docker compose up -d --build omy` sull'hub, `launchctl kickstart -k gui/$(id -u)/com.pierpaolobene.local-omy.compute` sul Mac.
- **Wake del Mac** non verificato: `pmset repeat` dipende dal Mac (coperchio, alimentatore); da controllare la prima notte (`pmset -g log | grep -i wake`).
- **Ricerca senza Mac**: solo testo (BM25). Con il Mac acceso torna ibrida; i vettori dei chunk nuovi li calcola il Mac (job `embed`).
- Le 3 sessioni che whisper non riesce a leggere (`read_audio_data … miniaudio`) erano già `failed` sul Mac.
