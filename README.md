# Bird Audio Suite

Progetto unificato e separato che fonde:

- `Bird Detector`: analisi batch di file `.wav`
- `Bird sound recognizer`: ascolto live da microfono, denoise ed export delle clip rilevate

## Funzioni

- Analisi batch di file audio con BirdNET
- Ascolto live da microfono con slicing automatico
- Denoise opzionale con `librosa`
- Salvataggio clip `.wav` per specie rilevata
- Cache locale dei nomi specie in `species_names.txt`
- Recupero automatico dei nomi italiano e tedesco via Wikipedia

## Struttura

- `bird_audio_cli.py`: entry point CLI
- `bird_audio_suite/`: moduli condivisi
- `species_names.txt`: archivio locale nomi specie
- `detections/`: clip esportate

## Installazione

```bash
pip install -r requirements.txt
```

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
- device index `17`
- il primo `python3` trovato nel `PATH`

Puoi personalizzare l'installazione al momento della creazione del servizio:

```bash
make install PYTHON=/home/andrea-adelfio/anaconda3/bin/python3 DEVICE_INDEX=17 BACKEND=sounddevice
```

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

### Ascolto live

```bash
python bird_audio_cli.py live
python bird_audio_cli.py live --slice-interval 240 --min-confidence 0.15
python bird_audio_cli.py live --noise-ref ./noise.wav
python bird_audio_cli.py live --backend sounddevice --device-index 17 --disable-denoise --verbose
python bird_audio_cli.py live --clip-span full_slice
```

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
- Le clip esportate finiscono in `detections/YYYYMMDD/`.
- In `live`, la registrazione si interrompe con `Ctrl+C`.
- Su questa macchina il default live e' `sounddevice` con device `17` (`Gruppo microfoni (Senary Audio capture)`).
- In `live`, il default export e' `from_detection`: dalla prima detection fino alla fine della slice.
