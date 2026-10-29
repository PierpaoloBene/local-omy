# local-omy

Registratore minimo per il ciondolo [Omi](https://github.com/BasedHardware/omi).
Un ingresso: l'audio del ciondolo via BLE. Un'uscita: file audio grezzi sul Mac di casa.
Se il Mac non è raggiungibile i file restano sull'iPhone e partono appena torna raggiungibile.

Sul telefono niente decodifica né trascrizione. Sul Mac la trascrizione è locale (whisper.cpp).

Fork di [kbdevs/omibutfree](https://github.com/kbdevs/omibutfree) (licenza MIT),
ridotto all'osso. Grazie a kbdevs per il lavoro di base sul BLE.

## Struttura

| Percorso | Cosa |
|---|---|
| `iphone/src/` | App Flutter (solo iOS): `lib/`, `ios/`, `pubspec.yaml` |
| `iphone/attic/` | Codice fuori dal build, tenuto per una v2 (sync della scheda SD) |
| `mac/src/` | Mac: ricevitore (FastAPI), CLI `omy` (setup, installazione iPhone, trascrizione), strumenti di analisi |
| `mac/src/app/` | App per Mac (SwiftUI, barra dei menu): interfaccia grafica sopra `omy` |
| `iphone/build/`, `mac/build/` | Artefatti finali (ignorati da git) |

## Protocollo audio BLE del ciondolo

Verificato sul firmware ufficiale: `omi/firmware/omi/src/lib/core/transport.c`
(BasedHardware/omi, commit `1aee0763e4`, 2026-08-05).

| UUID | Cosa |
|---|---|
| `19b10000-e8f2-537e-4f6c-d104768a1214` | servizio Omi |
| `19b10001-…` | stream audio (notify) |
| `19b10002-…` | codec (read): 1 = pcm8, 20 = opus, 21 = opusFS320 |
| `180f` / `2a19` | batteria (read; notify se la characteristic lo supporta) |

Il firmware attuale usa solo il **codec 21**: Opus, 16 kHz mono, frame da 320 campioni
(20 ms, 50 frame/s), al massimo 160 byte per frame.

Ogni notifica audio è `[3 byte di header][payload]`. Da `push_to_gatt()`:

```c
uint32_t id = packet_next_index++;   // static uint16_t, 0 al boot
pusher_temp_data[0] = id & 0xFF;
pusher_temp_data[1] = (id >> 8) & 0xFF;
pusher_temp_data[2] = index;         // frammento dentro lo stesso frame codec
```

- **Byte 0-1: indice pacchetto, u16 little-endian.** Aumenta di 1 a ogni *notifica*, non
  a ogni frame. A 50 notifiche/s ricomincia da 0 ogni ~21,8 minuti. Si azzera solo al
  riavvio del ciondolo, non alla riconnessione.
- **Byte 2: indice di frammento.** Se un frame codec supera `MTU − 3` byte viene spezzato in
  più notifiche numerate 0, 1, 2… iOS negozia l'MTU da solo (di solito tra 135 e 255
  byte), quindi i frame più grandi possono arrivare spezzati. Chi decodifica deve riunire
  i frammenti consecutivi.

### Cosa misura (e cosa no) un buco negli indici

- Quando il ciondolo **non è connesso** l'audio va sulla scheda SD (se presente) e
  l'indice **non avanza**. Una disconnessione appare quindi come un buco di *tempo*, non
  di indice. Quell'audio è sulla SD, recuperabile in una v2.
- Un buco di indice significa che una notifica inviata non è arrivata nel file: persa
  in radio, dal sistema iOS o dall'app.
- Perdite che l'indice **non** vede: ring buffer del firmware pieno (il frame viene
  scartato prima di ricevere un indice), oppure ciondolo connesso ma notifiche non ancora
  sottoscritte (il frame viene scartato senza indice).

## Segmenti sul telefono

L'audio viene scritto in segmenti in `Documents/segments/` dell'app. I segmenti chiusi
**sono** la coda di upload: non c'è un database.

| File | Significato |
|---|---|
| `<id>.part` | segmento in scrittura |
| `<id>.part.json` | device id del `.part`, serve a chiuderlo dopo un crash |
| `<id>.bin` + `<id>.json` | segmento chiuso, in coda per l'upload |

**Id:** `AAAAMMGGThhmmssZ-xxxxxxxx`, cioè l'istante di inizio in UTC più 32 bit casuali
esadecimali. Si ordina per nome. Il Mac lo valida con `^\d{8}T\d{6}Z-[0-9a-f]{8}$`.

**Rotazione:** dopo 5 minuti (sulla prima notifica con frammento 0, così un frame non
resta spezzato tra due segmenti), alla disconnessione del ciondolo, e alla chiusura
dell'app. iOS non garantisce una callback di chiusura (`detached` arriva solo a volte):
ogni `.part` rimasto viene chiuso al successivo avvio. Il recupero tronca l'eventuale
ultimo record scritto a metà. Un `.part` senza record viene eliminato.

**Chiusura:** fsync del `.part` → scrittura del `.json` (su `.json.tmp`, fsync, rename) →
rename del `.part` in `.bin`. Ogni passo si può ripetere se viene interrotto.

### Formato `.bin` (versione 1)

Tutti gli interi sono **little-endian**.

```
header del file (14 byte)
  off  len
   0    4   magic "OMIS"
   4    1   versione formato = 1
   5    1   codec id (21 = opusFS320; 0 = non letto)
   6    8   inizio, unix ms UTC (u64)
record (ripetuto fino a fine file)
   0    4   offset in ms dall'inizio (u32)
   4    2   lunghezza N (u16)
   6    N   notifica BLE così come è arrivata, header Omi di 3 byte compreso
```

L'offset è il momento di **arrivo sul telefono**, misurato con un orologio monotono. Non
è l'istante di cattura: iOS consegna le notifiche a raffiche, quindi gli offset hanno
qualche decina di ms di jitter. Per l'ordine e i buchi fa fede l'indice pacchetto.

### Sidecar `.json`

```json
{
  "id": "20261027T081530Z-3fa2c91b",
  "device_id": "<UUID CoreBluetooth del ciondolo>",
  "codec": 21,
  "start": "2026-10-27T08:15:30.123Z",
  "end": "2026-10-27T08:20:30.140Z",
  "frame_count": 15001,
  "first_packet_index": 4211,
  "last_packet_index": 19211,
  "sha256": "<sha256 esadecimale del .bin>"
}
```

`device_id` è l'identificatore che iOS assegna al ciondolo. È stabile su questo iPhone,
ma non è il MAC address. `end` è l'inizio più l'offset dell'ultimo record.

Campi opzionali, presenti solo quando ci sono (vedi `mac/README.md`):
- `calls`: le chiamate CallKit che toccano il segmento, `[{"start", "end"?}]`;
- `location`: la posizione migliore, `{"lat", "lon", "acc", "source", "at"}`;
- `battery`: il livello della batteria del ciondolo alla chiusura del segmento, 0–100.
- `firmware`: la versione del firmware del ciondolo (Device Information, `2a26`), letta a ogni
  connessione, per esempio `"3.0.21"`.

## Upload al Mac

Il Mac è raggiungibile via Tailscale, con `tailscale serve` che fa da terminazione HTTPS
(`https://<host>.<tailnet>.ts.net`). Il certificato è valido, quindi non servono eccezioni
ATS. Sull'iPhone la VPN Tailscale deve essere attiva: se non lo è, l'upload fallisce e i
segmenti restano in coda.

```
POST /v1/segments/<id>
Authorization: Bearer <token>
Content-Type: application/octet-stream
X-Segment-Id: <id>
X-Sha256: <sha256 del .bin, esadecimale minuscolo>
X-Segment-Meta: <.json del segmento, base64url>
<corpo: il .bin grezzo>
```

| Risposta | Significato | Il telefono… |
|---|---|---|
| `200 {"id", "sha256", "status": "stored"\|"duplicate"}` | salvato (o già presente identico) | cancella il segmento **solo** se `sha256` coincide |
| `400` | richiesta non valida (id, header, hash del corpo) | lo tiene, riprova con backoff |
| `401` | token errato | lo tiene, riprova con backoff |
| `409` | stesso id già presente con hash diverso | lo tiene: serve un intervento manuale |
| `413` | corpo troppo grande | lo tiene |

**Quando prova:**
- alla chiusura di ogni segmento;
- al ritorno in foreground;
- con un timer da 30 s, ma solo dopo che è scaduto il backoff (30 s, 1, 2, 4… fino a
  30 min, azzerato al primo successo).

Un errore ferma il giro: il segmento successivo aspetta il prossimo tentativo, così l'ordine
resta cronologico. Il token sta nel Keychain con accessibilità
`first_unlock_this_device`: è leggibile anche a telefono bloccato, purché sia stato
sbloccato una volta dopo l'accensione.

**In background su iOS:** non c'è nessun task di background dedicato (BGTaskScheduler).
Gli upload partono solo mentre l'app è sveglia:
- in pratica sempre, finché il ciondolo è connesso e manda audio (il segmento si chiude
  ogni 5 minuti e fa partire l'upload);
- a ciondolo disconnesso e app in background, iOS sospende l'app dopo pochi secondi, e
  timer e tentativi si fermano finché un evento BLE (riconnessione) non la risveglia o
  finché non la riapri.

## iOS: cosa succede davvero in background

`Info.plist` dichiara solo `UIBackgroundModes = bluetooth-central`.

- **App in background, connessa:** iOS consegna le notifiche BLE e l'app gira mentre le
  riceve. L'audio continua a essere registrato.
- **Riconnessione:** `connect(autoConnect: true)` di flutter_blue_plus crea su iOS una
  richiesta di connessione in attesa che non scade mai. Il plugin la ripete dopo ogni
  disconnessione e quando il Bluetooth si riaccende. Funziona anche in background: quando
  il ciondolo torna nel raggio, iOS riconnette e sveglia l'app.
- **App chiusa dal sistema** (memoria): con `FlutterBluePlus.setOptions(restoreState: true)`
  iOS rilancia l'app in background al primo evento BLE (state restoration di CoreBluetooth).
- **App chiusa da te** (swipe nel multitasking): iOS **non** la rilancia più finché non la
  riapri a mano. Non c'è rimedio.
- **Da verificare sul telefono:** Flutter 3.47 ha migrato il progetto al ciclo di vita
  UIScene. In un rilancio in background non c'è nessuna scena. Bisogna verificare che il
  motore Dart parta lo stesso (prova: chiudi l'app con molta memoria occupata, allontana e
  riavvicina il ciondolo, controlla che compaiano nuovi segmenti).

## Build e installazione

Requisiti: Flutter ≥ 3.47, Xcode (con il tuo Apple ID in Impostazioni → Account), CocoaPods.

Per installare e rinnovare l'app sull'iPhone usa `omy iphone install` (vedi
[mac/README.md](mac/README.md)). Solo per compilare:

```bash
cd iphone/src
flutter pub get
flutter build ipa --no-codesign   # oppure: flutter build ios --no-codesign
```

Il bundle id è `com.example.local-omy`. Nel progetto non c'è un team di sviluppo
impostato: `omy` lo passa a `xcodebuild` da riga di comando. Non ci sono entitlement (quelli iCloud dell'originale non
funzionano con un account gratuito).

**Non tenere il repo in iCloud Drive.** I metadati estesi che iCloud aggiunge ai file
("resource fork, Finder information, or similar detritus not allowed") fanno fallire la
firma di `Flutter.framework`. L'originale aveva uno script `fix_build.sh` che lanciava
`sudo xattr -rc` sull'SDK di Flutter per aggirarlo: è stato rimosso.

## Licenza

MIT, come l'originale.
