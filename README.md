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

## Note

- Il progetto non modifica i due progetti originali.
- Le clip esportate finiscono in `detections/YYYYMMDD/`.
- In `live`, la registrazione si interrompe con `Ctrl+C`.
- Su questa macchina il default live e' `sounddevice` con device `17` (`Gruppo microfoni (Senary Audio capture)`).
- In `live`, il default export e' `from_detection`: dalla prima detection fino alla fine della slice.
