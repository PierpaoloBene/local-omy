# Inventario dell'app Flutter (iphone/src): cosa la versione Swift deve fare uguale

Fonte di verità in caso di dubbio: il codice Dart in `iphone/src/lib` e `iphone/src/ios/Runner/AppDelegate.swift`.

## 1. BLE (lib/services/ble_service.dart)
- Servizio Omi `19b10000-e8f2-537e-4f6c-d104768a1214`; audio notify `19b10001-…`; codec read `19b10002-…` (1=pcm8, 20=opus, 21=opusFS320; sconosciuto → 0).
- Batteria `180f/2a19` (read, poi notify se supportato); firmware `180a/2a26` (UTF-8, NUL rimossi).
- Scan 15 s (attende fino a 10 s l'adattatore acceso), filtro nome contiene "omi" (case-insensitive), ordinati per RSSI; l'utente sceglie in Impostazioni.
- Dispositivo ricordato (`device_id`); all'avvio connect con riconnessione automatica (pending connect iOS), ripristino stato.
  Errore di setup → disconnect e riconnetti; caratteristica audio mancante → dimentica il dispositivo.
- I frame audio si salvano COSÌ COME ARRIVANO (header di 3 byte incluso). Nessuna decodifica sul telefono.
- Stato: notFound (nessun dispositivo); connected (streaming, audio < 10 s fa); idle (streaming, silenzio); disconnected (perso < 10 min fa); altrimenti notFound.
- Background: solo `bluetooth-central`.

## 2. Registrazione e segmenti
- Cartella `Documents/segments` (NON Application Support). Accanto: `Documents/calls.json`, `locations.json`, `features.json`.
- File: `{id}.part`, `.part.json` (device_id), `.bin`, `.json`, `.json.tmp`. Id = UTC `yyyyMMdd'T'HHmmss'Z'` + "-" + 8 hex casuali.
- Rotazione: 5 min, solo su un frame con byte[2] == 0; timer 15 s chiude forzatamente a età ≥ 5 min 10 s; chiusura a ogni stato BLE diverso da streaming, alla terminazione, al dispose.
- Header 14 byte: "OMIS", versione u8 = 1, codec u8, start unix ms u64 LE. Record: offset ms u32 LE (orologio monotono), lunghezza u16 LE, frame.
- Chiusura: flush; tronca la coda rovinata; sidecar in `.json.tmp` → rename `.json`; `.part` → `.bin`; elimina `.part.json`. `.part` vuoto/invalido → eliminato.
- All'avvio ogni `.part` orfano viene finalizzato con recovering=true (niente batteria/firmware).
- Sidecar (indent 2): id, device_id, codec, start, end (ISO UTC), frame_count, first_packet_index, last_packet_index, sha256 (hex del .bin);
  opzionali calls [{start, end?}] (max 20; chiamata aperta = 4 h), location {lat, lon, acc (int), source "visit"|"move", at}, battery (0-100),
  firmware (regex `^[0-9A-Za-z._+-]{1,32}$`). packetIndex/fragmentIndex dai byte dell'header del frame (vedi segment_format.dart:46-50).
- Coda = coppie .bin+.json chiuse, ordinate per nome (più vecchie prima). Eliminate dopo upload verificato.
- bestLocation: la visita che copre il centro del segmento, altrimenti un move entro 2 h. Calls/locations più vecchi di 3 giorni potati.

## 3. Upload
- POST `{macUrl}/v1/segments/{id}`; header Authorization Bearer, Content-Type application/octet-stream, X-Segment-Id, X-Sha256 (hex minuscolo),
  X-Segment-Meta = base64url dei byte del .json, X-Queue-Remaining = segmenti rimasti dopo questo. Corpo = .bin. Timeout 60 s.
- Successo = HTTP 200 e JSON con sha256 uguale al nostro → elimina. Altrimenti tieni.
- Primo errore ferma il giro; backoff 30 s raddoppiato (esponente max 6) fino a 30 min; reset al successo; timer 30 s riprova dopo il backoff.
- Trigger: segmento chiuso, app in primo piano, timer 30 s, "Invia ora", pull-to-refresh, salvataggio impostazioni. Solo in primo piano (+ BLE background).

## 4. Impostazioni
- UserDefaults Flutter: `flutter.device_id`, `flutter.mac_url`, `flutter.features` (JSON {key: {on, at}}).
- Keychain Flutter: service `flutter_secure_storage_service`, account `upload_token`, AfterFirstUnlockThisDeviceOnly.
- Features: location, calls, battery (iphone); mac_audio, diarization, voice_prints, auto_transcribe (mac, via PUT /v1/settings).
  Mai cambiato = on. Last-writer-wins su "at". Sync ogni 5 min, al resume e alla modifica. `Documents/features.json` contiene solo le chiavi iphone.
- Lingua: prima tra it/en nelle preferenze, default inglese; tr(it, en).

## 5. Schermate
- Schede: Oggi, Conversazioni, Persone, Luoghi, Chiedi. Tap sulla scheda attiva → torna alla radice / in cima. Mostrare la scheda la ricarica.
- **Oggi** (home_page.dart): card ciondolo (stato, batteria, firmware, visto l'ultima volta); iPhone (Registrazione On/Off, In coda, Ultimo invio, Invia ora);
  Mac (sessioni in attesa, progresso, "Trascrivi ora" → POST /v1/transcribe, ricarica dopo 2 s, poll ogni 5 s mentre lavora); OGGI IN BREVE
  (conversations(day, limit 1000) → viewer); ultimo errore; ingranaggio → Impostazioni.
- **Impostazioni**: Ciondolo (attuale, Cerca, risultati con RSSI, Dimentica); MAC (URL solo https, token oscurato, Salva); interruttori features con riga
  di problema del permesso di posizione (Apri → Impostazioni di sistema).
- **Conversazioni**: ricerca (debounce 400 ms, min 2 caratteri), filtro persona (Tutti, io, persone), filtri tipo, avviso in attesa, risultati →
  viewer con focus e evidenziazione. Timeline per giorno con intestazioni fisse e indice dei giorni sul bordo destro (trascinabile, haptics),
  paginazione a giorni interi (pageSize 1000). Riga: fino a 3 avatar, "ora · app o luogo", durata, icona tipo, partecipanti o "Solo tu", estratto «…».
- **Viewer conversazione** (chat.dart): pagine scorrevoli (destra = precedente), intestazione data/ora + app o luogo, contatore n/tot; bolle (io a destra
  col colore d'accento, altri a sinistra con avatar al primo della serie); intestazioni di cambio luogo; focus = bordo giallo 2,5 pt centrato a 0,3;
  evidenziazione = sfondo giallo case-insensitive; pulsanti Prima/Dopo. Giorni caricati con day(day), cache in memoria.
- **Persone**: segmentato Persone / Da etichettare·N. Persone: filtri tipo, card "io", righe persone, conversazioni recenti (8, Mostra tutte).
  Da etichettare: unlabeled(hours 24); per sessione "Segna tutte…" (Sconosciuti, Video, Riunione online, Di persona, Automatico); per voce
  play/stop del campione, nome/tipo/stato, citazione, Etichetta/Cambia → sheet (tipo, nome solo con voice_prints e non video, suggerimenti
  io + note max 20, riga "Sconosciuto").
- **Persona**: titolo = nome; Modifica (non per "io") → sheet (nome, tipo, Elimina). Conversione in video (conferma, kind media), unione su nome
  già esistente (conferma, setPersonKind poi rename), eliminazione (conferma). Card con luoghi; timeline delle sue conversazioni.
- **Luoghi**: "Con un nome" (mappa piccola, indirizzo, raggio, durata, ultima volta) → azioni Rinomina, Cambia raggio (50,100,150,300,500,1000 m),
  Elimina. "Dove hai registrato, senza un nome": mappa, indirizzo ("Indirizzo non trovato"), durata, giorni, dettagli, campione, Nomina → sheet
  (nome, raggio 50-500, anteprima). Geocodifica inversa in italiano, una richiesta alla volta.
- **Chiedi**: suggerimento iniziale; bolle utente; risposta in streaming con [n] in apice toccabili; stato (sveglio il modello, cerco, rispondo);
  "non trovato" come avviso; errore con Riprova; chip delle citazioni (tocco → conversazione su row_id usando session_day, poi day, poi ±1 giorno;
  pressione lunga → nota); "X s · primo testo Y s"; Nuova chat; storico ultimi 8 messaggi esclusi non-trovato ed errori.

## 6. API (mac_api.dart, models.dart) — campi JSON esatti
- GET /v1/transcripts → {days[{day, minutes, sessions, paragraphs, from, to, speakers[{name, person, minutes}], places[], unnamed_places}],
  pending{sessions, open_sessions, minutes, progress{running, sessions_total, sessions_done, minutes_total, minutes_done}}, today}
- GET /v1/transcripts/{day} → {summary, records[{id, day, session, start, end, text, speaker, person, language, via{kind, app}, place{name, lat, lon, acc}}]}
- POST /v1/search {q, person?, type?} → {results[Paragraph], more}
- GET /v1/conversations?person&type&day&before&limit → {conversations[{day, session, focus, start, end, snippet, place, app, minutes, types[],
  participants[{name, person, type, minutes}]}], more, next}
- GET /v1/unlabeled?hours → {sessions[{session, start, day, voices[{label, name, sample, person, kind, app, source, pinned, unknown, seconds}]}]}
- GET /v1/brain/note/{session} → nota (sidecar; 404 = nessuna). POST /v1/ask (SSE, vedi mac/src/omy/brain/INTERFACES.md §9).
- POST /v1/transcribe. GET /v1/people → {me{name, enrolled, minutes, sessions, last_seen}, people[{id, name, samples, sessions, minutes, last_seen}],
  index{id: {types, places, conversations}}}; PUT /v1/people/{id} {name}; DELETE /v1/people/{id}; POST /v1/people/{id}/kind {source}.
- GET /v1/voices/{day}; PUT /v1/voices/{session}/{label} {name}; POST /v1/voices/{session}/kind {source, labels};
  POST /v1/voices/{session}/unknown {labels, unknown: true}; GET /v1/voices/{session}/{label}/sample → WAV.
- GET /v1/places → {places[{name, lat, lon, radius, minutes, last_seen}], unnamed[{lat, lon, minutes, days, last_seen, from, to, sample, nearest{name, meters}}]};
  POST /v1/places {name, lat, lon, radius}; PUT /v1/places/{name} {name}; DELETE /v1/places/{name}.
- PUT /v1/settings {features: {key: {on, at}}} → features unite.
- Hub (docs/hub-migration.md): stessi path e JSON; l'URL in Impostazioni è quello dell'hub (`https://hub.<tailnet>.ts.net:10000`).
  Chiedi con il Mac spento: POST /v1/ask emette `status` con `stage = "queued:<id>"` e `done` (l'app mostra "In attesa del Mac" e,
  ogni 15 s, GET /v1/ask/{id} → {state: pending|leased|done|failed|expired|cancelled, answer, citations, not_found, error}).
  Le app vecchie vedono il `delta` di cortesia come risposta. `GET /v1/asks` elenca le ultime domande (3 giorni).
- Errori: 401 token; 502-504 ricevitore fermo; 404 Mac da aggiornare; altri → campo "error" tradotto; JSON non valido → "risposta non valida".

## 7. Nativo
- AppDelegate.swift attuale: CallLog (CXCallObserver → calls.json, rispetta la feature calls), LocationLog (visite e cambi significativi →
  locations.json; chiede "sempre" se non determinato o solo-in-uso; 100 m; requestLocation una volta). Riprodurre identici.
- Audio campioni: AVAudioSession .playback, play(WAV) → durata, stop.
- Icona: `iphone/src/ios/Runner/Assets.xcassets/AppIcon.appiconset`. Testi dei permessi: Info.plist e it.lproj/InfoPlist.strings.
