# Bird Audio Suite

Ascolta gli uccelli dal microfono del portatile con BirdNET, salva ogni detection su Supabase e la fa rivedere da un sito (anche da telefono, installabile come app), da cui si carica su iNaturalist.

## Come e' fatto

- **Il portatile ascolta.** Il servizio live riconosce le specie con BirdNET e scrive ogni clip in `detections/` (un WAV piu' un piccolo JSON con specie, ora, confidenza e posizione).
- **`detections/` e' solo di passaggio.** Il live scrive solo la clip e basta. Un servizio a parte, `bird-audio-suite-sync` (`sync --watch`), guarda la cartella ogni 3 s: fa MP3 e spettrogramma, manda tutto a Supabase pochi secondi dopo la chiusura della clip e la cancella dal PC. Senza rete le clip aspettano li' e riprova ogni minuto. Essendo un altro processo, a priorita' bassa di CPU e disco, il lavoro pesante (lo spettrogramma di una clip lunga richiede secondi e oltre 100 MB) non rallenta mai l'ascolto.
- **Supabase e' l'unico posto dove vivono i dati**, progetto "progetti" (`supabase/birds_schema.sql`). Ogni informazione sta in un posto solo:
  - Storage, bucket pubblico `bird-audio`: i file, `<media_path>.mp3` e `<media_path>-spectrogram.jpg`;
  - `birds.clips`: una riga per detection, con la sua ora, la sua confidenza, il luogo e il `media_path` dei file (gli URL si ricavano, non si salvano), i secondi in cui BirdNET ha sentito la specie (`segments`) e l'eventuale taglio deciso sul sito (`trim_start_s`/`trim_end_s`, il file resta intero);
  - `birds.observations` (specie x giorno): solo quello che decidi tu (stato, note, specie corretta, iNaturalist);
  - `birds.species`: solo i nomi;
  - viste `daily_summary`, `species_stats`, `hourly_activity`: tutto quello che si calcola (confidenza massima del giorno, prima/ultima volta, conteggi, giorni, rarita').
- **Il sito di revisione** (`docs/`, GitHub Pages) legge e scrive su Supabase.

Accesso a Supabase volutamente aperto alla chiave publishable, come MyFinance: chi ha il link del sito legge (coordinate comprese) e scrive.

## Installazione

Ambiente dedicato in `~/.venvs/bird-audio-suite` (fuori da Dropbox), con NumPy 2 e LiteRT (`ai-edge-litert`) al posto di TensorFlow: BirdNET gira con ~200 MB di RAM invece di ~550+ MB.

```bash
make venv     # crea l'ambiente con uv e installa requirements.txt
make test       # i test, poi tempi e RAM di ogni fase confrontati con il riferimento
make test-fast  # solo i test
```

`bird_audio_suite/detector.py` presenta LiteRT a birdnetlib come `tflite_runtime`; se LiteRT non c'e', birdnetlib usa TensorFlow.

Impostazioni di Supabase, fuori dal repository, in `~/.config/bird-audio-suite/cloud.env`:

```
SUPABASE_URL=https://<progetto>.supabase.co
SUPABASE_KEY=sb_publishable_...
```

### Servizi all'avvio (systemd utente)

```bash
make install          # servizio live: ~/.config/systemd/user/bird-audio-suite.service
make install-health   # ogni 5 minuti: notifica desktop se il live e' fermo, bloccato o sente solo silenzio
make install-sync     # servizio di invio sempre attivo: detections/ -> Supabase (toglie il vecchio timer)
make start | stop | restart | status | log | log-follow | log-sync
```

Il controllo di salute avvisa se, per due controlli di fila, il servizio non e' attivo, non analizza audio da piu' di 5 minuti, il microfono da' solo silenzio digitale da 10 minuti, l'analisi e' indietro di piu' di 60 s, la RAM supera 1.2 GB, il servizio di invio non gira o ci sono clip ferme in `detections/` da piu' di 2 ore; una notifica per problema e una quando si risolve. Prova: `python -m bird_audio_suite.health --test-notification`. Per far partire il servizio anche senza login grafico: `loginctl enable-linger $USER`.

## Uso

### Ascolto live

```bash
python bird_audio_cli.py live
python bird_audio_cli.py live --min-confidence 0.6 --verbose
python bird_audio_cli.py live --backend sounddevice --device-index -1 --device-probe-seconds 1
```

- Per ogni clip vengono salvati i tratti esatti (in secondi dall'inizio della clip, con la confidenza) in cui BirdNET ha sentito la specie, anche quelli piu' deboli (da 0.5) dentro una clip gia' aperta: sul sito diventano bande evidenziate sullo spettrogramma.
- BirdNET analizza ogni finestra da 9.6 s a blocchi di 3 s sovrapposti (uno ogni secondo): dove cade il verso dentro il blocco cambia molto il punteggio (un luì piccolo sentito a 0.95 dava 0.42 a blocchi fissi), quindi con blocchi fissi il live perdeva uccelli ben udibili. Costa circa il triplo (~0.7 s per finestra, 14% del tempo disponibile). Soglia predefinita per aprire una clip: 0.6 (`DEFAULT_LIVE_MIN_CONFIDENCE` in `config.py`, letta all'avvio: dopo averla cambiata `make restart`). Da 0.8 (`LIKELY_CONFIDENCE`) un'identificazione e' **likely**, sotto **unsure**: nella tabella la confidenza e' verde o gialla, sul sito verde o ambra.
- Una clip parte 3 s prima della prima detection e continua finche' la specie viene sentita; si chiude dopo 3 finestre senza detection (pause fino a ~15 s non la spezzano), dura almeno 10 s e al massimo 120 s.
- La tabella del giorno (scorciatoia Ctrl+Alt+L, che segue il log del servizio) unisce Supabase (l'unica verita': le clip eliminate e le osservazioni scartate dal sito spariscono), le clip ancora in `detections/` e le specie con la clip ancora aperta; si aggiorna a ogni detection e ogni minuto. Senza rete mostra solo i dati del PC.
- Sotto la tabella, due righe sulle prestazioni degli ultimi 10 minuti: quanto ci mette BirdNET per finestra e che parte del tempo disponibile usa (una finestra nuova ogni 4.8 s: sopra il 100% il live resta indietro), quanto e' indietro rispetto al microfono, RAM (ora e picco), CPU, costo della sottrazione dell'audio del PC e tempo di invio per clip. Gli stessi numeri vanno nel heartbeat: il controllo di salute avvisa se il live e' indietro di piu' di 60 s o supera 1.2 GB di RAM. Sotto ancora, lo stato del servizio di invio: clip in attesa, ultimo invio, secondi per clip, ultimo errore.
- Livello: il live registra il microfono di sistema dopo il suo volume (impostazioni, ma anche le app di videochiamata lo cambiano da sole: al 45% le clip uscivano 21 dB piu' basse). Lo legge ogni 30 s e lo compensa in digitale, senza perdita perche' il segnale e' in float: le registrazioni restano al livello vero del microfono. Niente normalizzazione (alzerebbe il fondo quanto l'uccello): il volume lo scegli sul sito. BirdNET non dipende dal livello (provato: a +21 dB da' le stesse confidenze).
- Con `--device-index -1` prova ogni ingresso in un processo separato (alcuni plugin ALSA vanno in segfault), scarta il "click" iniziale e sceglie quello con segnale, preferendo 44.1/48 kHz e il server audio (PipeWire/PulseAudio). La scelta resta in `.bird_audio_device.json`.
- Se il PC sta riproducendo audio, il live ne legge il monitor (`pw-record` in modo passivo, che non tiene accese le casse quando il PC tace; `parec` senza PipeWire) e lo sottrae dal microfono con un filtro adattivo lineare; in piu' analizza il monitor e scarta le specie che arrivano dal PC, cosi' un merlo di YouTube non diventa osservazione. Si spegne con `--disable-playback-subtraction`.
- Posizione: dall'IP, ricalcolata ogni 15 minuti (il portatile si sposta); offline resta l'ultima trovata (`.bird_audio_location.json`). `BIRD_AUDIO_LATITUDE`/`BIRD_AUDIO_LONGITUDE` la fissano a mano.
- `Ctrl+C` o `make stop`: le clip ancora aperte vengono salvate in `detections/`; il servizio di invio le manda.

### Analisi batch di registrazioni

```bash
python bird_audio_cli.py batch --directory ./recordings --recursive --export-clips
```

Formati: `.wav`, `.flac`, `.mp3`, `.ogg`, `.m4a`. L'ora di inizio viene dal nome del file (`20260601_063000.wav`, `SMA01234_20260601_063000.wav`, `2026-06-01 06-30-00.m4a`) o, se manca, dalla data di modifica meno la durata. Con `--export-clips` le clip passano da `detections/` a Supabase come quelle del live.

### Sync a mano

```bash
python bird_audio_cli.py sync            # manda quello che aspetta in detections/ (una volta)
python bird_audio_cli.py sync --watch    # come il servizio: resta acceso e manda ogni clip appena c'e'
python bird_audio_cli.py sync --limit 5
```

Per le clip caricate prima che si salvassero i secondi delle detection:

```bash
python bird_audio_cli.py backfill-segments            # riascolta le clip su Supabase con BirdNET e salva i tratti
python bird_audio_cli.py backfill-segments --limit 20
```

Clip lasciate dalla versione precedente (cartelle del giorno con il CSV) vengono convertite e mandate al primo sync.

### Denoise

```bash
python bird_audio_cli.py denoise --input ./recording.wav --noise-ref ./noise.wav --plot
```

## Sito di revisione

`docs/`, pubblicato con GitHub Pages da `main` (cartella `/docs`), installabile come app.

Layout compatto: su computer ogni osservazione e' una riga (a sinistra specie, fatti e azioni, a destra le clip, che riempiono la riga), sul telefono una colonna. Barra in alto sempre visibile; la pagina non si svuota mentre carica e la lista continua da sola scorrendo.

- **Osservazioni**: tutte, dalla piu' recente. Pulsanti di stato sempre visibili (Tutte / Da rivedere con il numero / Confermate / Caricate / Scartate), specie e date accanto (sul telefono sotto "Specie e date"); restano quelli che imposti. Ogni osservazione (una per specie al giorno): nome scientifico, icone Wikipedia e iNaturalist e stato sulla prima riga, nomi italiano e inglese sotto, poi giorno, ore, luogo, `max_obs_conf` e numero di clip. Accanto a `max_obs_conf` il tag **likely** (almeno una clip da 0.8, verde) o **unsure** (0.6-0.8, ambra); `clip_conf` sullo spettrogramma ha lo stesso colore, e i filtri likely/unsure stanno accanto a quelli di stato. Le azioni sono solo quelle sensate per lo stato: a sinistra **↑ iNaturalist** (se confermata) o **Vedi su iNat**; a destra, sotto il pollice, i pulsanti tondi: matita per correggere la specie (si salva quando lasci il campo), ↺ (rimetti da rivedere), X rossa (scarta) e per ultima la spunta verde (conferma).
- **Ogni clip**:
  - sullo spettrogramma: ora, `clip_conf` e durata in alto, tempo di riproduzione in basso, bande gialle dove BirdNET ha sentito la specie (piu' intense = piu' sicure);
  - tocca una banda per ascoltare solo quel tratto, altrove per ascoltare da quel punto; trascina per selezionare un tratto: **▶ Ascolta** o **✂ Taglia** (la parte esclusa resta ombreggiata; l'etichetta ✂ in alto a destra toglie il taglio; il file su Supabase resta intero, su iNaturalist va solo la parte tenuta);
  - una riga di comandi: play/pausa, −2 s, velocita' 1×/½×, ripeti in loop; **Scarta** (la clip va nel Cestino, l'osservazione resta) e il cestino rosso, che elimina subito e per sempre quella clip (audio, spettrogramma e riga su Supabase); da tastiera spazio e frecce;
  - riduzione del rumore (bacchetta, solo per l'ascolto): il rumore e' la clip fuori dalle bande gialle (con 0.3 s di margine; senza bande, il quarto piu' silenzioso), il suo spettro bin per bin viene sottratto da tutta la clip, mai sotto un pavimento. Un tocco = media (fondo circa -6 dB, il canto emerge in tutte le 14 clip di prova, +2.5 dB), due = forte (fondo -18 dB, +7 dB in media ma su qualche clip si mangia un uccello debole), tre = originale; si riprende dallo stesso punto e sullo spettrogramma compare "rumore ridotto: media/forte". Gira in un worker (`docs/assets/denoise-worker.js`, ~2 s per una clip di un minuto). Il file su Supabase, BirdNET e iNaturalist restano sull'originale (BirdNET da' punteggi piu' bassi all'audio ripulito: e' addestrato su registrazioni grezze);
  - volume: cursore in alto (sul telefono dietro l'altoparlante) dal 10% al 3162% (+30 dB), uguale per tutte le clip, anche su quella che sta suonando, ricordato dal browser; tocca la percentuale per tornare al 100%. Le clip registrate fino a ottobre 2026 sono 40-50 dB sotto e vogliono un volume alto.
- **Report**: 7/30/90 giorni o un anno; specie, osservazioni, da rivedere, clip, minuti di audio, specie nuove, attivita' per ora e tabella delle specie (cliccabili: aprono le loro osservazioni del periodo); su computer su due colonne.
- **Cestino**: osservazioni e clip scartate, da riascoltare, ripristinare o eliminare per sempre (con conferma). L'eliminazione cancella righe e file da Supabase e annota le clip in `birds.deleted_clips`, cosi' il PC non le rimanda.
- **iNat**: con il token personale di https://www.inaturalist.org/users/api_token (24 ore, resta nel browser) carica le osservazioni confermate, con la specie corretta se indicata (nome esatto o sinonimo su iNaturalist), fino a 5 clip in MP3 e la posizione precisa/oscurata/privata. Come nota solo `BirdNET confidence 0.957` (la confidenza massima del giorno), piu' le etichette `audio,birdnet`. Supabase tiene traccia di cosa e' online (`inat_observation_id`, `clips.inat_sound_id`, stato "caricata", `uploaded_at`): un caricamento interrotto riprende senza doppioni.
- **Dopo una settimana su iNaturalist** il servizio di invio toglie da Supabase MP3 e spettrogrammi di quell'osservazione (controlla ogni ora; a PC spento recupera alla riaccensione). Le righe delle clip restano, segnate con `media_deleted_at`, perche' report, conteggi e rarita' si calcolano da loro: sul sito al posto del player compare "Audio tolto da Supabase il ...: e' su iNaturalist".

Sviluppo:

- `make dev` / `make dev-stop`: il sito su http://127.0.0.1:8004 con i dati veri (`make stop` resta per il servizio live).
- `make dev_mob`: anteprima mobile senza commit, su http://127.0.0.1:8005/mobile e dal telefono (stessa Wi-Fi) all'indirizzo che stampa; si ricarica da sola a ogni salvataggio.
- `make web-dev`: Supabase finto in locale (Postgres integrato + PostgREST) con una copia in sola lettura dei dati veri, su http://127.0.0.1:8010.
- `make git`: committa tutto e fa push (pubblica il sito).

## Prestazioni

`make test` esegue i test (si ferma se uno fallisce, e mostra i piu' lenti) e poi `dev/benchmark.py`: BirdNET vero su audio sintetico, con le stesse chiamate del live, del sync e del backfill. Per ogni fase stampa tempo e RAM e li confronta con `dev/benchmark_baseline.json` (misurato su questo portatile): segnala quello che e' piu' lento o pesante di oltre il 30%. Ogni esecuzione si aggiunge a `~/.cache/bird-audio-suite/benchmark_history.jsonl`. Dopo un cambiamento voluto (o su un altro PC) `make bench-baseline` aggiorna il riferimento.
