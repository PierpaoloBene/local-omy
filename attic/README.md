# attic/

Codice fuori dal build (non è sotto `lib/` ed è escluso da `analysis_options.yaml`).
È tenuto come riferimento per una v2.

## sdcard_sync/

Sync dei file audio registrati sulla scheda SD del ciondolo mentre è disconnesso,
così com'era in kbdevs/omibutfree (il commit "sd card sync kinda works").

- `sdcard_sync_service.dart`: protocollo di lettura della SD (servizio
  `30295780-4301-eabd-2904-2849adfeae43`, characteristic dati `…81` e controllo `…82`).
- `sdcard_sync_page.dart`: UI.
- `ble_service_original.dart`: il `ble_service.dart` originale, con i metodi
  `getStorageList`, `writeToStorage` e `startStorageStream`.
- `opus_decoder_service.dart`: decoder Opus usato dalla sync (pacchetti `opus_dart` e `opus_flutter`).

Non compila così com'è: dipende dal vecchio `AppProvider` e da `BleAudioCodec`, che non
esistono più. Per riattivarlo vanno portati i metodi storage nel nuovo `BleService`, e i
file letti dalla SD vanno scritti come segmenti nel formato di `lib/`.
