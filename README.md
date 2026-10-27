# local-omy

Registratore minimo per il ciondolo [Omi](https://github.com/BasedHardware/omi).
Un ingresso: l'audio del ciondolo via BLE. Un'uscita: file audio grezzi sul Mac di casa.
Se il Mac non è raggiungibile i file restano sull'iPhone e partono appena torna raggiungibile.

Niente trascrizione, niente LLM, niente decodifica sul telefono.

Fork di [kbdevs/omibutfree](https://github.com/kbdevs/omibutfree) (licenza MIT),
ridotto all'osso. Grazie a kbdevs per il lavoro di base sul BLE.

## Struttura

| Percorso | Cosa |
|---|---|
| `lib/` | App Flutter (solo iOS) |
| `mac/` | Ricevitore sul Mac (Python, FastAPI) e strumenti di analisi |
| `attic/` | Codice fuori dal build, tenuto per una v2 (sync della scheda SD) |

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

Il test `test/segment_test.dart` controlla il layout byte per byte e confronta lo
scrittore Dart con `mac/tests/fixtures/sample.bin`, letto anche dai test Python.

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

Requisiti: Flutter ≥ 3.47, Xcode, CocoaPods, AltServer.

```bash
flutter pub get
flutter build ipa --no-codesign   # oppure: flutter build ios --no-codesign
```

Il bundle id è `com.example.local-omy`. Nel progetto non c'è un team di sviluppo
impostato: firma AltServer. Non ci sono entitlement (quelli iCloud dell'originale non
funzionano con un account gratuito).

**Non tenere il repo in iCloud Drive.** I metadati estesi che iCloud aggiunge ai file
("resource fork, Finder information, or similar detritus not allowed") fanno fallire la
firma di `Flutter.framework`. L'originale aveva uno script `fix_build.sh` che lanciava
`sudo xattr -rc` sull'SDK di Flutter per aggirarlo: è stato rimosso.

## Licenza

MIT, come l'originale.
