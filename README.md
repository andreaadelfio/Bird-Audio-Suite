# Bird Audio Suite

Progetto unificato e separato che fonde:

- `Bird Detector`: analisi batch di file `.wav`
- `Bird sound recognizer`: ascolto live da microfono, denoise ed export delle clip rilevate

## Funzioni

- Analisi batch di file audio con BirdNET
- Ascolto live da microfono con slicing automatico
- Denoise opzionale con `librosa`
- Salvataggio clip `.wav` per specie rilevata
- Catalogo CSV locale in `species_names.txt` con nomi, conteggi storici, media giornaliera e rarita'
- Recupero automatico dei nomi italiano e tedesco via Wikipedia

## Struttura

- `bird_audio_cli.py`: entry point CLI
- `bird_audio_suite/`: moduli condivisi
- `species_names.txt`: catalogo CSV locale con nomi e statistiche storiche di frequenza
- `detections/`: clip esportate

## Installazione

Il progetto usa un ambiente dedicato in `~/.venvs/bird-audio-suite` (fuori da Dropbox), con NumPy 2 e LiteRT (`ai-edge-litert`) al posto di TensorFlow: BirdNET gira con ~200 MB di RAM invece di ~550+ MB.

```bash
make venv     # crea l'ambiente con uv e installa requirements.txt
make test     # esegue i test
```

`bird_audio_suite/detector.py` presenta LiteRT a birdnetlib come `tflite_runtime`; se LiteRT non e' installato, birdnetlib usa TensorFlow come prima.

### Installazione come servizio all'avvio

Il progetto include un `Makefile` per installare e gestire `Bird Audio Suite` come servizio `systemd` utente.

Installazione del servizio:

```bash
make install
```

Avvio e gestione:

```bash
make start
make stop
make restart
make status
make log
make log-follow
```

Per impostazione predefinita il servizio usa:

- backend `sounddevice`
- device index `-1` (selezione automatica, vedi sotto)
- il Python di `~/.venvs/bird-audio-suite`

Puoi personalizzare l'installazione al momento della creazione del servizio:

```bash
make install PYTHON=/percorso/python DEVICE_INDEX=-1 BACKEND=sounddevice
```

### Controllo di salute con notifiche desktop

```bash
make install-health
```

Installa un timer systemd che ogni 5 minuti controlla il servizio live e manda una notifica desktop se, per due controlli di fila: il servizio non e' attivo, non analizza audio da piu' di 5 minuti (es. crash loop), oppure il microfono da' solo silenzio digitale da 10 minuti (muto/scollegato). Una notifica per problema, piu' una quando si risolve. Il live scrive il battito in `$XDG_RUNTIME_DIR/bird-audio-suite/heartbeat.json`. Prova delle notifiche: `python -m bird_audio_suite.health --test-notification`.

Note utili:

- `make install` crea `~/.config/systemd/user/bird-audio-suite.service`
- dopo `make install`, il servizio viene abilitato con `systemctl --user enable`
- `make start` lo avvia subito
- `make log` apre tutto il log del servizio in modalita' scorrevole partendo dalla fine
- `make log-follow` segue il log live in tempo reale
- se vuoi che il servizio utente parta anche senza login grafico, puoi valutare `loginctl enable-linger $USER`

## Uso

### Modalita' interattiva

```bash
python bird_audio_cli.py
```

Se lanci la CLI senza sottocomando, parte una guida interattiva per scegliere tra `batch`, `live` e `denoise`.

### Analisi batch

```bash
python bird_audio_cli.py batch --files audio1.wav audio2.wav
python bird_audio_cli.py batch --directory ./recordings --recursive
python bird_audio_cli.py batch --files audio.wav --denoise --export-clips
```

Formati letti: `.wav`, `.flac`, `.mp3`, `.ogg`, `.m4a` (anche in maiuscolo, es. `.WAV` di AudioMoth).

Data e ora della registrazione:

- se il nome del file contiene un timestamp (`20260601_063000.wav`, `SMA01234_20260601_063000.wav`, `2026-06-01 06-30-00.m4a`) viene usato quello come inizio registrazione;
- altrimenti si usa la data di modifica del file meno la sua durata (la data di modifica e' quella di fine scrittura del registratore: se il file e' stato copiato senza preservare le date, rinominalo con il timestamp).

Ogni specie viene datata `inizio registrazione + offset della detection` e salvata nella cartella del giorno in cui e' stata sentita.

### Ascolto live

```bash
python bird_audio_cli.py live
python bird_audio_cli.py live --slice-interval 240 --min-confidence 0.15
python bird_audio_cli.py live --noise-ref ./noise.wav
python bird_audio_cli.py live --backend sounddevice --device-index 17 --disable-denoise --verbose
python bird_audio_cli.py live --backend sounddevice --device-index -1 --device-probe-seconds 1
python bird_audio_cli.py live --clip-span full_slice
```

### Riparazione delle detection salvate nel giorno sbagliato

Le versioni precedenti del live calcolavano la cartella `detections/YYYYMMDD` solo all'avvio: una sessione lunga piu' giorni salvava tutto nella cartella del primo giorno e il CSV univa giorni diversi in un'unica osservazione per specie. Il comando seguente sposta ogni clip nel giorno indicato dal suo nome e ricostruisce i CSV con una riga per specie per giorno:

```bash
python bird_audio_cli.py repair-detections            # mostra solo il piano
python bird_audio_cli.py repair-detections --apply    # applica
```

Con `--apply` i CSV originali e la lista degli spostamenti (`moves.json`) vengono salvati in `detections/_backup_repair_<timestamp>/`.

### Sito di revisione e cloud

Le osservazioni si rivedono su un sito statico (`docs/`, pubblicato con GitHub Pages) che legge da Supabase, progetto "progetti": dati nello schema `birds`, audio (MP3) e spettrogrammi nel bucket pubblico `bird-audio` di Supabase Storage:

- **Da rivedere**: per ogni osservazione (una per specie al giorno) le clip con spettrogramma (tocca per ascoltare da quel punto) e player. **Conferma** / **Scarta** valgono per l'intera osservazione ("questa specie c'era quel giorno?"); **Scarta clip** serve solo per le singole clip sbagliate dentro un'osservazione che tieni. Si puo' correggere la specie e aggiungere note.
- **Cestino**: osservazioni scartate e clip scartate, da riascoltare e ripristinare o eliminare definitivamente (con conferma). L'eliminazione cancella righe, MP3 e spettrogrammi da Supabase e annota le clip in `birds.deleted_clips`, cosi' `sync` non le ricarica; i WAV sul PC restano.
- **Osservazioni**: tutte, filtrabili per stato, specie e date.
- **Report**: ultimi 7/30/90 giorni: specie, clip, minuti di audio, specie nuove, attività per ora.

Pezzi:

- `supabase/birds_schema.sql`: tabelle `species`, `observations`, `clips` e viste del report, nel progetto Supabase "progetti". Accesso volutamente aperto alla chiave publishable, come MyFinance: chi ha il link del sito legge (coordinate comprese) e scrive.
- `bird_audio_cli.py sync`: carica le clip in MP3 (8 volte piu' leggere del WAV, che resta sul PC per iNaturalist) e gli spettrogrammi (JPEG, rumore di fondo sottratto banda per banda) nel bucket e scrive le righe nello schema `birds`; rimanda solo cio' che e' cambiato, non tocca mai le revisioni e se e' offline recupera al giro dopo. Credenziali in `~/.config/bird-audio-suite/cloud.env` (vedi `bird_audio_suite/cloud.py`), mai nel repository.
- `make web-dev`: prova tutto in locale, senza Supabase: Postgres integrato + PostgREST (lo stesso strato REST di Supabase) con lo schema, sync delle detection e sito su http://127.0.0.1:8010.

Messa in linea: schema eseguito nel SQL Editor di "progetti" ed esposto in Data API, credenziali in `~/.config/bird-audio-suite/cloud.env`, `sync`, `make install-sync` (sync ogni 15 minuti), GitHub Pages da `main` cartella `/docs`.

### Denoise standalone

```bash
python bird_audio_cli.py denoise --input ./recording.wav
python bird_audio_cli.py denoise --input ./recording.wav --noise-ref ./noise.wav --plot
```

### Import automatico su iNaturalist

E' possibile automatizzare l'import da script usando l'API ufficiale di iNaturalist:

- l'API v2 espone `POST /observations` per creare osservazioni
- l'API v2 espone `POST /observation_sounds` per allegare file audio
- per le richieste `POST` serve autenticazione con JWT

Riferimenti ufficiali:

- https://api.inaturalist.org/v2/docs/
- https://www.inaturalist.org/pages/developers
- https://www.inaturalist.org/pages/api%2Breference
- https://help.inaturalist.org/en/support/solutions/articles/151000169939-how-do-i-add-sounds-

Flusso pratico:

1. ottieni un JWT iNaturalist autenticandoti con il tuo account
2. esportalo nell'ambiente
3. lancia l'import sul CSV orario generato da Bird Audio Suite

Esempio:

```bash
export INATURALIST_JWT="incolla-qui-il-jwt"
python bird_audio_cli.py inat-import --csv detections/20260522/18/inaturalist_import_20260522_18.csv --dry-run
python bird_audio_cli.py inat-import --csv detections/20260522/18/inaturalist_import_20260522_18.csv
```

Comportamento dell'importatore:

- crea una osservazione per ogni riga del CSV
- allega tutti i file audio elencati in `audio_files=...`
- salva un file di stato accanto al CSV con estensione `.state.json`
- se rilanci lo script, aggiorna l'osservazione esistente invece di duplicarla e carica solo i nuovi audio mancanti

Nota:

- il JWT va ottenuto fuori dallo script; il progetto usa il token presente in `INATURALIST_JWT`
- l'integrazione e' stata implementata contro gli endpoint ufficiali documentati, ma non e' stata testata live da qui perche' manca un token reale e l'accesso di rete esecutivo non e' disponibile in questa sessione

## Note

- Il progetto non modifica i due progetti originali.
- Le clip esportate finiscono in `detections/YYYYMMDD/`, dove `YYYYMMDD` e' il giorno in cui la specie e' stata sentita (anche se il live gira da piu' giorni). Il CSV del giorno contiene una riga per specie: piu' detection della stessa specie nello stesso giorno vengono unite, giorni diversi mai.
- In `live`, la registrazione si interrompe con `Ctrl+C` o con `SIGTERM` (`make stop`): in entrambi i casi le clip ancora aperte vengono salvate prima di uscire.
- In `live`, il default e' `sounddevice` con `--device-index -1` (selezione automatica).
- Con `--device-index -1` il programma ascolta ogni ingresso sounddevice (di default per un secondo), mostra RMS e picco e seleziona quello con piu' segnale, preferendo a parita' di segnale quelli a 44.1/48 kHz (BirdNET lavora a 48 kHz: a 16 kHz si perde tutto sopra gli 8 kHz). Ogni ingresso viene provato in un processo separato con le stesse impostazioni del live, scartando il primo mezzo secondo: alcuni plugin ALSA (`speex`, `upmix`, `vdownmix`) vanno in segfault o emettono solo un "click" iniziale. Usa `--device-probe-seconds` per cambiare la durata del test.
- Il device selezionato automaticamente (con il sample rate verificato) viene salvato in `.bird_audio_device.json`; agli avvii successivi viene provato solo quello. Se non e' disponibile, il segnale e' insufficiente o lo stream non si apre, la cache viene scartata e vengono provati gli altri ingressi.
- In `live`, il default export e' `from_detection`: la clip parte 3 s prima della prima detection e continua finche' la specie viene sentita; si chiude dopo 3 finestre consecutive senza detection (pause fino a ~15 s non la spezzano), dura almeno 10 s e al massimo 120 s (i cori lunghi vengono spezzati in piu' clip della stessa osservazione).
- Posizione: in `live` viene ricalcolata ogni 15 minuti (il portatile si sposta). Se la ricerca via IP fallisce si usa l'ultima posizione trovata (`.bird_audio_location.json`), non quella di default nel config. `BIRD_AUDIO_LATITUDE`/`BIRD_AUDIO_LONGITUDE` la fissano a mano.
- `species_names.txt` ora e' un CSV storico: tiene i nomi della specie, primo/ultimo giorno di ascolto, giorni osservati, conteggio totale, media giornaliera e una rarita' inversa basata sulla frequenza media. Le osservazioni del giorno vengono invece lette dai CSV `detections/YYYYMMDD/*/inaturalist_import_*.csv`.
