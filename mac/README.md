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
  batteria del ciondolo, job orario, log.
- **Setup:** la checklist di `omy setup check`. Ogni voce mancante ha il suo pulsante
  "Sistema" (`omy setup fix <voce>`). Ci sono anche:
  - "Copia URL" e "Copia token": il token va negli appunti e non viene mai mostrato;
  - "Apri local-omy al login".
- **iPhone:** scadenza della firma, "Compila e installa", "Rinnova ora", "Reinstalla
  l'ultima build". Ogni azione chiede conferma, perché chiude l'app sul telefono.
- **Trascrizioni:** giorni trascritti e "Trascrivi ora". Durante una trascrizione,
  anche lanciata dal job orario o dal terminale, compare la barra di avanzamento.
- **Persone:** a sinistra le persone riconosciute (tu e quelle a cui hai dato un nome),
  con il menu per rinominarle o eliminarle. A destra le voci di ogni
  sessione del giorno scelto: ▶︎ fa sentire qualche secondo, e scrivendo un nome (o
  scegliendolo da "Chi è") la voce prende quel nome. Vedi "Dare un nome alle voci".

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
3. **Volume normalizzato:** i tratti deboli vengono alzati fino a +20 dB, con il guadagno
   smussato su ~1 s. Servono per le voci lontane dal ciondolo e la TV dall'altra parte della
   stanza.
4. Ogni blocco viene **tagliato nelle pause in finestre da ~30 s**, e su ognuna whisper
   rileva la lingua. Con la lingua rilevata una volta per blocco, whisper traduceva in
   inglese mezz'ora di italiano. La lingua viene salvata in ogni frase. Se in una finestra
   viene rilevata una lingua diversa da italiano e inglese, succede su tratti brevi o
   rumorosi dove whisper inventa anche testo ("Gracias por ver el video."). In quel caso la
   finestra viene ritrascritta in italiano.
5. Il **VAD Silero** di whisper.cpp salta i silenzi, e così riduce le frasi inventate sul
   rumore. Ha una soglia più sensibile del default (`-vt 0.35` invece di 0.5) e 200 ms di
   margine (`-vp 200`): con il default si perdeva la voce debole.
6. **Nessun contesto fra una finestra e l'altra** (`-mc 0`): elimina i loop di ripetizione
   (es. "Grazie a tutti." ripetuto per mezzo minuto).
7. I WAV temporanei stanno in `~/OmiArchive/cache/` e vengono cancellati subito dopo l'uso.

Velocità: circa 15 volte il tempo reale, quindi una giornata da 8 ore si trascrive in circa
mezz'ora. Ogni JSON registra la versione della pipeline (`pipeline`). Quando la pipeline
cambia, le sessioni trascritte con la versione precedente vengono rifatte.

**Output** (derivato: si rigenera con `--force`, il raw non viene mai toccato):

| File | Cosa |
|---|---|
| `~/OmiArchive/transcripts/AAAA-MM-GG/<sessione>.json` | frasi con inizio/fine in UTC, segmenti d'origine, modello |
| `~/OmiArchive/transcripts/AAAA-MM-GG/speakers/<sessione>.json` | turni di parola in UTC e somiglianza di ogni voce alla tua (vedi Chi parla) |
| `~/OmiArchive/transcripts/AAAA-MM-GG.md` | la giornata da leggere: una sezione per ogni sessione con parlato, un paragrafo nuovo a ogni cambio di chi parla, dopo ogni silenzio > 2 min o dopo 5 min di parlato |
| `~/OmiArchive/transcripts/AAAA-MM-GG.jsonl` | gli stessi paragrafi, uno per riga, con ora, chi parla, chiamata o media, luogo: il formato per altre app (vedi Output per altre app) |

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

## Chi parla (diarizzazione)

Dopo la trascrizione, ogni sessione passa da
[pyannote](https://github.com/pyannote/pyannote-audio) (`speaker-diarization-community-1`),
che gira sul Mac con la GPU (MPS) a circa 25 volte il tempo reale.
- Le voci vengono raggruppate per sessione.
- Il gruppo che somiglia alla tua **impronta vocale** (somiglianza coseno ≥ 0,5) si chiama
  `Io`. Il nome si cambia con `my_name = "…"` in `~/.config/local-omy/config.toml`.
- Una voce che somiglia (≥ 0,4) ai campioni di una persona a cui hai dato un nome prende
  quel nome, una sola voce per persona in ogni sessione.
- Le altre voci diventano `Persona 1`, `Persona 2`… nell'ordine in cui compaiono. I
  numeri valgono solo dentro la sessione.
- Nel markdown ogni paragrafo porta nome e orario: `**18:26 · Io** …`. Le frasi che non
  cadono in nessun turno restano senza nome.

Una volta sola:
1. Accetta le condizioni del modello su
   https://huggingface.co/pyannote/speaker-diarization-community-1.
2. Crea un token **Read** su Hugging Face e salvalo, senza incollarlo da nessuna parte:
   `(umask 077; pbpaste | tr -d '\n' > ~/.config/local-omy/hf_token)`. Serve solo a scaricare
   il modello in `~/OmiArchive/models/hf`. Dopo, la diarizzazione gira offline.
3. `uv run omy voice enroll`: ricava l'impronta vocale dagli spezzoni annotati a mano in
   `~/OmiArchive/voices/enroll` e la salva in `~/OmiArchive/voices/me.npy`. Ogni spezzone è
   un `<X>.wav` di 2 minuti con un `<X>.txt`: una battuta per riga con chi parla (`P:` tu,
   `A:`, `B:`… altre persone, `TV:` audio registrato). Le righe con `#` sono ignorate,
   `# default: P` vale per le righe senza etichetta, `[...]` e `(...)` sono note.
   Contano solo gli spezzoni con righe `P:`.

L'impronta vocale è un **dato biometrico**: ha permessi 600 e non lascia mai il Mac, come
l'audio. Se la ricrei, le sessioni vengono ri-diarizzate.

**Chiamate e media.** L'app Mac registra ogni 10 s quali app usano il microfono e gli
altoparlanti (CoreAudio, per processo). Lo fa solo quando ce n'è almeno una attiva, e scrive
in `~/OmiArchive/context/audio-AAAA-MM-GG.jsonl` (mode 600): non registra audio, solo
quale app e se usa ingresso o uscita. La diarizzazione lo usa per le voci che non sono tue:
- una voce che parla per almeno metà del tempo mentre un'app usa il microfono (Teams, Zoom,
  FaceTime, Meet nel browser) è un partecipante alla chiamata: `Persona 2 (Teams)`;
- una voce che parla mentre un'app riproduce soltanto (YouTube nel browser, Musica,
  Spotify) è un media: `Media (Chrome)`.

Le **chiamate sull'iPhone** (telefono, FaceTime, WhatsApp: tutto ciò che usa CallKit) le
osserva l'app iPhone (`CXCallObserver`), che ne registra solo inizio e fine e non chi chiama.
Le copia nel sidecar dei segmenti che toccano (`"calls": [{"start", "end"}]`, opzionale e
validato dal ricevitore), e le voci altrui diventano `Persona 2 (iPhone)`.

Nel markdown, sotto il titolo della sessione, compare `_Chiamata (iPhone) 10:12–10:25_`.
Serve anche quando dall'auricolare non si sente l'altra persona.

Valgono solo dal 29 ottobre 2026. La TV in salotto e quello che succede su un altro
computer (per esempio Teams sul Mac di lavoro) non lasciano traccia. Una persona presente
nella stanza durante una chiamata viene contata come partecipante.

Una voce cantata non viene riconosciuta come tua.

### Dare un nome alle voci

Dalla scheda **Persone** dell'app, o da terminale:

```bash
uv run omy speakers list --day 2026-10-29              # voci di ogni sessione, con cosa hanno detto
uv run omy speakers play <sessione> SPEAKER_01         # qualche secondo in cache/voice-sample.wav
uv run omy speakers rename <sessione> SPEAKER_01 Marco
uv run omy people list                                 # tu e le persone con un nome
uv run omy people rename <id> "Marco Rossi"            # stesso nome di un'altra persona: le unisce
uv run omy people delete <id>
```

- **Rinominare** una voce copia la sua impronta tra i campioni della persona e fissa quel
  nome a quella voce. Poi tutte le sessioni già diarizzate vengono rinominate dalle
  impronte salvate, senza rifare pyannote, e i loro markdown riscritti. Le sessioni nuove
  la riconoscono da sole. Più voci della stessa persona nomini, meglio la riconosce.
- Dare a una voce il tuo nome (`Io`) la fissa a te, ma non cambia la tua impronta.
- **Eliminare** una persona cancella il nome e i suoi campioni (che sono copie). Le impronte
  delle voci restano in `voices/clusters`: nelle trascrizioni tornano `Persona N` e puoi
  dar loro di nuovo un nome, anche lo stesso.

Dove stanno, tutto con permessi 600 e niente fuori dal Mac:
- `voices/clusters/<sessione>.npz`: l'impronta di ogni voce di ogni sessione, anche senza
  nome. Serve a nominarla dopo.
- `voices/people.json` e `voices/people/<id>.npz`: nomi e campioni.

Sono impronte vocali di **altre persone**, cioè dati biometrici raccolti senza il loro
consenso: tienine conto prima di condividere l'archivio. Per non salvarne di nuove, spegni
"Impronte vocali" nelle Funzioni.

## Dove (luoghi)

L'app iPhone usa le **visite** di iOS (arrivo e partenza da un luogo, precise a qualche
decina di metri) e i **cambi di posizione significativi** (circa 500 m): funzionano in
background e consumano poco, perché non tengono acceso il GPS. Li scrive in
`Documents/locations.json` sul telefono (ultimi 3 giorni) e mette nel sidecar di ogni
segmento la posizione migliore:
`"location": {"lat", "lon", "acc", "source": "visit"|"move", "at"}`.
All'avvio, e quando cambia il permesso, l'app chiede anche una posizione singola e aggiornata
(`requestLocation`). Senza, un telefono fermo resterebbe senza posizione finché non arriva
una visita, cosa che con iOS può richiedere ore.
- vale la visita in corso a metà del segmento;
- altrimenti l'ultimo cambio significativo, se non ha più di 2 ore e non è precedente
  all'uscita da una visita.

Serve "Sempre" con **Posizione esatta** attiva. Con "Mentre usi l'app", iOS non dà la
posizione in background, quindi le registrazioni fatte con il telefono in tasca restano
senza luogo. Senza Posizione esatta, la posizione è approssimata a qualche chilometro.
- iOS concede prima solo "Mentre usi l'app". All'apertura successiva l'app chiede di passare
  a "Sempre", ma iOS mostra questa richiesta una volta sola.
- Se la posizione non va bene, nelle impostazioni dell'app, sotto Posizione, compare un
  avviso con il pulsante per aprire Impostazioni.

Le coordinate non vengono mai trasformate in indirizzi da un servizio esterno. Il nome lo dà
il Mac, con la tua lista in `~/OmiArchive/places.json` (mode 600):

```bash
uv run omy places list                          # luoghi con nome, e dove hai registrato senza un nome
uv run omy places add Casa 45.4642 9.1900       # --radius 150 (metri) di default
uv run omy places rename Casa "Casa mia"
uv run omy places remove Casa
```

Nell'app per Mac, il tab **Luoghi** fa lo stesso. A sinistra ci sono i luoghi con nome, che
puoi rinominare, eliminare o di cui puoi cambiare il raggio. A destra ci sono i punti dove hai
registrato senza un nome, con un campo per darglielo. Per riconoscere un punto senza una
mappa, ogni riga mostra:
- quanto ci hai registrato e in quanti giorni;
- la fascia oraria;
- la distanza dal luogo con nome più vicino;
- la frase più lunga detta lì l'ultimo giorno.

Di proposito non c'è una mappa: le tile di Apple Maps rivelerebbero la zona ad Apple. Un
luogo ha un solo centro, quindi non si può dare a un punto il nome di un luogo che esiste
già. Se un punto è "ancora casa", conviene allargare il raggio di Casa.

`places list` riporta i punti senza nome dove hai registrato di più, con le coordinate da
usare in `places add`. Aggiungere o togliere un luogo riscrive i markdown. Una posizione è
un luogo se cade entro il suo raggio, più fino a 100 m dell'imprecisione della posizione
stessa. Nel markdown, sotto il titolo della sessione:
`_📍 Casa_`, `_📍 Casa → 45.4700, 9.1950 (±20 m)_` se ti sei spostato.

## Batteria del ciondolo

L'app iPhone mette nel sidecar il livello della batteria del ciondolo quando chiude un
segmento (`"battery": 0–100`). `uv run omy battery` (e la riga "Batteria ciondolo" nella
scheda Stato) ne ricava:
- il consumo in %/ora, sul tempo reale tra una lettura e l'altra, registrando o no;
- quanto dura una carica e quanto ne resta.

Una risalita di almeno 3 punti è una ricarica, e chiude il tratto. Letture a più di 12 ore
di distanza non vengono unite. La stima diventa sensata dopo qualche giorno di uso normale.

## Come è stata scelta la pipeline

Le scelte della trascrizione vengono da una misura su 7 spezzoni da 2 minuti trascritti a
mano, non da impressioni. Le prove sono concluse e il materiale è stato rimosso; restano
solo gli spezzoni che servono all'impronta vocale.

Risultati del 29 ottobre 2026 (1.000 parole circa):

| | trascrizione a blocchi da 30 min | pipeline attuale |
|---|---|---|
| Parole delle persone ritrovate | 49% | 78% |
| WER sul tuo italiano finito in inglese (B) | 56% | 16% |
| Parole inventate nel silenzio | 2 | 0 |

`large-v3` (non turbo) non ha migliorato le misure ed è 3 volte più lento. La
conversazione in famiglia, con più voci lontane e rumore, resta il caso difficile:
lì si ritrova circa metà delle parole.

Diarizzazione: il 95% delle tue parole viene attribuito a te, e il 92% di quelle
attribuite a te è davvero tuo. La misura è ottimista, perché gli spezzoni sono gli stessi
da cui viene ricavata l'impronta vocale.

## Funzioni da accendere e spegnere

Nella scheda **Setup** dell'app Mac (sezione Funzioni) e nelle **Impostazioni** dell'app
iPhone ci sono gli stessi interruttori, sincronizzati. Da terminale:
`uv run omy features list`, `uv run omy features set location off`.

| Funzione | Dove agisce | Spenta |
|---|---|---|
| `location` | iPhone | niente posizione nei segmenti; il Mac non mostra più i luoghi nel markdown |
| `calls` | iPhone | le chiamate CallKit non vengono più registrate |
| `battery` | iPhone | niente livello della batteria nei segmenti |
| `mac_audio` | Mac | l'app Mac non registra più quali app usano microfono e altoparlanti |
| `diarization` | Mac | le sessioni nuove non passano da pyannote: niente "chi parla" |
| `voice_prints` | Mac | le voci delle sessioni nuove non vengono salvate e nessuno, tranne te, viene riconosciuto; non si possono dare nomi |
| `auto_transcribe` | Mac | il job orario non trascrive (resta "Trascrivi ora") |

Spegnere non cancella niente di quello che è già stato raccolto.

**Sincronizzazione.** I valori stanno in `~/OmiArchive/settings.json` (mode 600). Ogni
valore porta con sé l'ora dell'ultima modifica, e vince la più recente. L'iPhone si
sincronizza con `PUT /v1/settings`: manda i suoi valori e riceve quelli uniti. Lo fa
all'avvio, quando torna in primo piano, dopo ogni modifica e ogni 5 minuti, quindi una
modifica fatta sul Mac arriva all'iPhone entro 5 minuti. Sul telefono i valori sono nelle
preferenze dell'app, e quelli che servono al codice nativo (posizione, chiamate) anche in
`Documents/features.json`.

## Output per altre app

Un'altra app (per esempio un second brain) dovrebbe leggere solo
`~/OmiArchive/transcripts/AAAA-MM-GG.jsonl`: il resto dell'archivio è interno e può
cambiare. Il file contiene un paragrafo per riga, gli stessi paragrafi del `.md` del giorno:
- è già ripulito dai loop e dalle frasi inventate da whisper;
- un paragrafo nuovo comincia a ogni cambio di chi parla, dopo 2 min di silenzio o dopo
  5 min di parlato.

Ogni riga è completa: non serve incrociarla con altri file.

```json
{"v": 1, "id": "20261029T055509Z-4a7fcd28/65610", "session": "20261029T055509Z-4a7fcd28",
 "start": "2026-10-29T06:56:15+01:00", "end": "2026-10-29T06:58:26+01:00",
 "speaker": "Marco", "person": "3f2c…", "via": {"kind": "call", "app": "Teams"},
 "place": {"name": "Casa", "lat": 45.46421, "lon": 9.19003, "acc": 20},
 "language": "it", "text": "…"}
```

| Campo | |
|---|---|
| `v` | versione del formato. Cresce solo se un campo cambia significato o sparisce; un campo nuovo non la cambia |
| `id` | `<sessione>/<ms dall'inizio della sessione>` |
| `session` | la registrazione continua di cui fa parte. Paragrafi della stessa sessione sono la stessa conversazione o lo stesso contesto |
| `start`, `end` | ora locale (Europe/Rome) con offset, al secondo |
| `speaker` | nome da mostrare: il tuo (`my_name`, default "Io"), il nome dato alla voce, "Persona N", "Media". `null` se la sessione non è diarizzata |
| `person` | `"me"` per te, l'id di una persona con nome (stabile anche se la rinomini), `null` per le voci senza nome. "Persona N" vale solo dentro la sua sessione |
| `via` | `{"kind": "call"\|"media", "app"}` se la voce veniva da una chiamata (Teams, Zoom, iPhone…) o da un media sul Mac (YouTube nel browser, Spotify…), altrimenti `null`. Una TV nella stanza non lascia traccia |
| `place` | posizione del telefono a inizio paragrafo, con `name` se cade in un luogo di `places.json` (altrimenti `null`); `null` se non c'è posizione o la funzione Posizione è spenta |
| `language` | `it` o `en`, la lingua prevalente del paragrafo |
| `text` | il testo |

**Aggiornamenti.** Il file di un giorno viene riscritto per intero, in modo atomico, ogni
volta che il giorno cambia:
- una sessione viene trascritta o si allunga;
- dai un nome a una voce o ne cancelli uno;
- aggiungi o togli un luogo.

Il file è sempre la verità completa su quel giorno. Il modo più semplice di sincronizzarsi è
rileggere i giorni il cui file ha un mtime più recente dell'ultima lettura, e sostituire
tutti i paragrafi di quel giorno. L'`id` resta lo stesso finché non vengono rifatte la
trascrizione o la separazione delle voci, quindi può servire anche per gli aggiornamenti
puntuali. Un giorno con solo
silenzio ha il file vuoto.

I file hanno mode 600 e restano sul Mac, come il resto dell'archivio.

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
  i tipi giusti. Gli unici campi opzionali sono `calls`, `location` (coordinate nei limiti,
  `source` tra `visit` e `move`, nessun altro campo) e `battery` (intero 0–100). `id` e `sha256` devono coincidere con la richiesta, e `start` deve
  coincidere con l'istante scritto nell'id.
- **Impostazioni:** `GET` e `PUT /v1/settings`, con lo stesso token. Il corpo del `PUT` è al
  massimo 4 KB. Accetta solo `{"features": {chiave: {"on": bool, "at": ISO UTC}}}`, ignora
  le chiavi sconosciute e non scrive mai altro che `settings.json`.
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
