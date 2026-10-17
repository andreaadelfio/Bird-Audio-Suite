# Bird Audio Suite

Ascolta gli uccelli dal microfono del portatile con BirdNET, salva ogni detection su Supabase e la fa rivedere da un sito (anche da telefono, installabile come app), da cui si carica su iNaturalist.

## Come e' fatto

- **Il portatile ascolta.** Il servizio live riconosce le specie con BirdNET e scrive ogni clip in `detections/` (un WAV piu' un piccolo JSON con specie, ora, confidenza e posizione).
- **`detections/` e' solo di passaggio.** Il live scrive solo la clip e basta. Un servizio a parte, `bird-audio-suite-sync` (`sync --watch`), guarda la cartella ogni 3 s: fa MP3 e spettrogramma, manda tutto a Supabase pochi secondi dopo la chiusura della clip e la cancella dal PC. Senza rete le clip aspettano li' e riprova ogni minuto. Essendo un altro processo, a priorita' bassa di CPU e disco, il lavoro pesante (lo spettrogramma di una clip lunga richiede secondi e oltre 100 MB) non rallenta mai l'ascolto.
- **Supabase e' l'unico posto dove vivono i dati**, progetto "progetti" (`supabase/birds_schema.sql`). Ogni informazione sta in un posto solo:
  - Storage, bucket pubblico `bird-audio`: i file, `<media_path>.mp3` e `<media_path>-spectrogram.jpg`;
  - `birds.clips`: una riga per detection (la sua `confidence` e' BirdNET sulla clip salvata, vedi sotto; il punteggio del live resta in `live_confidence`), con la sua ora, la sua confidenza, il luogo e il `media_path` dei file (gli URL si ricavano, non si salvano), i secondi in cui BirdNET ha sentito la specie (`segments`) e l'eventuale taglio deciso sul sito (`trim_start_s`/`trim_end_s`, il file resta intero);
  - `birds.observations`: una per visita, cioe' le clip di una specie distanti al massimo 10 minuti l'una dall'altra (lo stesso uccello ancora li'; `OBSERVATION_GAP_MINUTES` in `config.py`); una pausa piu' lunga apre una nuova osservazione. Solo quello che decidi tu (stato, specie corretta, iNaturalist);
  - `birds.species`: i nomi e i codici per eBird e iNaturalist (`ebird_code`, `inat_taxon_id`; `python bird_audio_cli.py refresh-species` riempie quelli mancanti);
  - viste `daily_summary`, `species_stats`, `hourly_activity`: tutto quello che si calcola (confidenza massima del giorno, prima/ultima volta, conteggi, giorni, rarita').
- **Un solo metro per la revisione.** Il servizio di invio analizza ogni clip salvata con BirdNET (tutta la clip, blocchi ogni 0.5 s, `bird_audio_suite/analysis.py`): da quell'unica analisi vengono `clip_conf`, likely/unsure, le bande (la migliore e' proprio `clip_conf`) e le tre specie candidate. Il punteggio dato dal live mentre ascoltava viene da altri blocchi del flusso (e, fino a ottobre 2026, da una catena diversa): resta in `live_confidence` e sul sito si vede passando sopra `clip_conf`. Per le clip gia' su Supabase: `python bird_audio_cli.py backfill-segments --all`.
- **Perch come doppio controllo.** Sulla stessa clip salvata il servizio di invio fa girare anche Perch 2.0 di Google DeepMind (`bird_audio_suite/perch.py`): un modello piu' grande, addestrato su ~14.800 specie (uccelli, e anche rane, cavallette, mammiferi) da Xeno-canto e iNaturalist. Le sue specie (fino a tre, da 0.1 in su) vanno in `clips.perch` e sul sito compaiono accanto a quelle di BirdNET, solo da leggere. Nel test di ottobre 2026 su 288 clip era d'accordo con BirdNET nel 77% dei casi (92% dove BirdNET e' sicuro) e ha indovinato tutte e 7 le clip con risposta nota, tra cui due "balie nere" che erano picchi rossi maggiori. Non sa dove ascolta, quindi le sue specie sono limitate a quelle osservate su iNaturalist entro 50 km dalla clip (qualita' research: uccelli, mammiferi, anfibi, ortotteri, cicale), chieste una volta per posto e rinnovate ogni tre mesi in `~/.cache/bird-audio-suite/perch`. Il modello (ONNX, 413 MB, licenza Apache 2.0, convertito dalla versione di Google su Kaggle da justinchuby e tphakala su Hugging Face) si scarica al primo uso da una revisione fissa e si controlla con lo SHA-256; gira con ONNX Runtime un blocco di 5 s alla volta (~3 s ogni 30 s di audio, ~0.9 GB di RAM) e il servizio lo libera dopo ogni giro. Senza onnxruntime, offline al primo download o senza la colonna su Supabase, la clip parte lo stesso senza parere; il servizio lo aggiunge ogni ora a 20 clip che non ce l'hanno, o tutte insieme `python bird_audio_cli.py backfill-perch`.
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
- BirdNET analizza ogni finestra da 9.6 s a blocchi di 3 s sovrapposti (uno ogni secondo): dove cade il verso dentro il blocco cambia molto il punteggio (un luì piccolo sentito a 0.95 dava 0.42 a blocchi fissi), quindi con blocchi fissi il live perdeva uccelli ben udibili. Costa circa il triplo (~0.7 s per finestra, 14% del tempo disponibile). Soglia per aprire una clip: `DEFAULT_LIVE_MIN_CONFIDENCE` in `config.py`. Da `LIKELY_CONFIDENCE` un'identificazione e' **likely**, sotto **unsure**: nella tabella la confidenza e' verde o gialla, sul sito verde o ambra.
- **`config.py` si cambia senza riavviare**: il live e il servizio di invio guardano ogni 3 s la data di modifica del file (una sola `stat()`, il file si rilegge solo quando cambia) e prendono i valori nuovi dalla finestra successiva: soglia del live, soglia per l'audio del PC, `LIKELY_CONFIDENCE` (colori della tabella, e il servizio di invio la scrive in `birds.shared_settings`, da cui la legge il sito all'apertura) e `OBSERVATION_GAP_MINUTES` (dalla clip successiva). Nel log compare `config.py: NOME vecchio -> nuovo`; i valori che danno forma all'audio (frequenza, dispositivi, finestre...) valgono dal riavvio successivo, e il log lo dice. Un file salvato a meta' o con un errore non viene preso: restano i valori in uso. Un valore dato a riga di comando resta quello.
- Una clip parte 3 s prima della prima detection e continua finche' la specie viene sentita; si chiude dopo 3 finestre senza detection (pause fino a ~15 s non la spezzano), dura almeno 10 s e al massimo 120 s.
- La tabella del giorno (scorciatoia Ctrl+Alt+L, che segue il log del servizio) unisce Supabase (l'unica verita': le clip eliminate e le osservazioni scartate dal sito spariscono), le clip ancora in `detections/` e le specie con la clip ancora aperta; si aggiorna a ogni detection e ogni minuto. Senza rete mostra solo i dati del PC.
- Sotto la tabella, due righe sulle prestazioni degli ultimi 10 minuti: quanto ci mette BirdNET per finestra e che parte del tempo disponibile usa (una finestra nuova ogni 4.8 s: sopra il 100% il live resta indietro), quanto e' indietro rispetto al microfono, RAM (ora e picco), CPU, costo della sottrazione dell'audio del PC e tempo di invio per clip. Quando il PC suona, una riga dice se il microfono lo sente (correlazione con l'audio del PC) e di quanto cala il microfono dopo la sottrazione (la misura comprende anche la tua voce e gli uccelli, che restano). Gli stessi numeri vanno nel heartbeat: il controllo di salute avvisa se il live e' indietro di piu' di 60 s o supera 1.2 GB di RAM. Sotto ancora, lo stato del servizio di invio: clip in attesa, ultimo invio, secondi per clip, ultimo errore.
- Livello: il live registra il microfono di sistema dopo il suo volume (impostazioni, ma anche le app di videochiamata lo cambiano da sole: al 45% le clip uscivano 21 dB piu' basse). Lo legge ogni 30 s e lo compensa in digitale, senza perdita perche' il segnale e' in float: le registrazioni restano al livello vero del microfono. Niente normalizzazione (alzerebbe il fondo quanto l'uccello): il volume lo scegli sul sito. BirdNET non dipende dal livello (provato: a +21 dB da' le stesse confidenze).
- Con `--device-index -1` prova ogni ingresso in un processo separato (alcuni plugin ALSA vanno in segfault), scarta il "click" iniziale e sceglie quello con segnale, preferendo 44.1/48 kHz e il server audio (PipeWire/PulseAudio). La scelta resta in `.bird_audio_device.json`.
- Se il PC sta riproducendo audio, il live ne legge il monitor (`pw-record` in modo passivo, che non tiene accese le casse quando il PC tace; `parec` senza PipeWire) e lo sottrae dal microfono con un filtro adattivo lineare; in piu' analizza il monitor e scarta le specie che arrivano dal PC, cosi' un merlo di YouTube non diventa osservazione. Si spegne con `--disable-playback-subtraction`. La sottrazione lavora sul microfono come lo da' la scheda audio, la compensazione del volume di sistema viene dopo; quando il PC riparte dopo una pausa misura il ritardo dopo 2 s di riproduzione (solo su quella, non sul silenzio prima) e riprova ogni 2 s finche' il microfono non lo sente. Fino al 16 ottobre 2026 il PC usciva solo 5-8 dB piu' basso: col microfono compensato x6.8 il PC arrivava al filtro piu' forte del suo riferimento, e la protezione contro la divergenza (un frame due volte piu' forte del microfono) scattava nelle pause dopo ogni suono e azzerava il filtro; ora la protezione guarda il livello del microfono nell'ultimo secondo e resetta solo dopo 8 frame di fila (in simulazione 20-30 dB in meno). Quello che le casse del portatile distorcono resta: una voce dal PC puo' ancora far sentire a BirdNET un uccello che non c'e'.
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
python bird_audio_cli.py backfill-perch               # il parere di Perch per le clip che non ce l'hanno (--all: tutte)
```

Clip lasciate dalla versione precedente (cartelle del giorno con il CSV) vengono convertite e mandate al primo sync.

### Denoise

```bash
python bird_audio_cli.py denoise --input ./recording.wav --noise-ref ./noise.wav --plot
```

## Sito di revisione

`docs/`, pubblicato con GitHub Pages da `main` (cartella `/docs`), installabile come app.

Layout compatto: su computer ogni osservazione e' una riga (a sinistra specie, fatti e azioni, a destra le clip, che riempiono la riga), sul telefono una colonna. Barra in alto sempre visibile; la pagina non si svuota mentre carica e la lista continua da sola scorrendo.

- **Osservazioni**: tutte, dalla piu' recente. Pulsanti di stato sempre visibili (Tutte / Da rivedere con il numero / Confermate / Caricate / Scartate), specie e date accanto (sul telefono sotto "Specie e date"); restano quelli che imposti. Ogni osservazione (una visita: clip della stessa specie a non piu' di 10 minuti): nome scientifico, icone Wikipedia, iNaturalist (pagina esatta della specie: su Android apre l'app), eBird (foto e suoni della Macaulay Library, gli stessi di Merlin) e ♪ (un esempio da iNaturalist, il piu' votato; tocca ancora per il successivo) e stato sulla prima riga, nomi italiano e inglese sotto, poi giorno, ore, luogo, `max_obs_conf` e numero di clip. Accanto a `max_obs_conf` il tag **likely** (almeno una clip da 0.8, verde) o **unsure** (0.6-0.8, ambra); `clip_conf` sullo spettrogramma ha lo stesso colore, e i filtri likely/unsure stanno accanto a quelli di stato. Le azioni sono solo quelle sensate per lo stato: a sinistra **↑ iNaturalist** (se confermata) o **Vedi su iNat**; a destra, sotto il pollice, i pulsanti tondi: matita per correggere la specie (si salva quando lasci il campo), ↺ (rimetti da rivedere), X rossa (scarta) e per ultima la spunta verde (conferma).
- **Seleziona** (accanto ai filtri): un cerchio su ogni osservazione e ogni clip della lista e una barra in basso, a portata di pollice, con quante ne hai scelte, **Tutte** (le osservazioni della pagina) e le azioni in blocco: rimetti da rivedere, scarta, conferma, carica su iNaturalist (le confermate), elimina per sempre (chiede conferma). Valgono le regole di quelle singole: una clip scelta da sola si scarta (va nel Cestino) o si rimette nell'osservazione, una clip su iNaturalist perde solo i file, una clip di un'osservazione scelta intera va con lei. Le azioni non applicabili alla selezione sono spente; Esc, **Fine** o un'altra lista chiudono la selezione. Sul telefono la barra va su due righe.
- **Ogni clip**:
  - sullo spettrogramma: ora, `clip_conf` e durata in alto, tempo di riproduzione in basso, bande in ogni punto in cui BirdNET sente la specie della clip da 0.6 in su (sotto non c'e' niente di affidabile): ambra se unsure (0.6-0.8), verdi se likely (>= 0.8), cosi' una clip a 0.65 mostra se altrove arriva sopra 0.8 e una a 0.8 se ricompare piu' debole; piu' intense quanto piu' e' sicuro; per ricalcolarle su tutte le clip dopo un cambio di soglia `python bird_audio_cli.py backfill-segments --all`;
  - tocca una banda per ascoltare solo quel tratto, altrove per ascoltare da quel punto; trascina per selezionare un tratto: **▶ Ascolta** o **✂ Taglia** (la parte esclusa resta ombreggiata; l'etichetta ✂ in alto a destra toglie il taglio; il file su Supabase resta intero, su iNaturalist va solo la parte tenuta);
  - sotto, le tre specie piu' probabili per BirdNET in quella clip (calcolate dal servizio di invio su tutta la clip; un tocco apre la correzione con quella specie) e, accanto, quelle di Perch: un secondo parere da leggere, nessuna clip messa in evidenza se i due non sono d'accordo;
  - una riga di comandi: play/pausa, −2 s, velocita' 1×/½×, ripeti in loop; la catena spezzata stacca la clip in un'osservazione a se', da rivedere (non e' lo stesso uccello o la stessa specie; non c'e' se la clip e' gia' l'unica della sua osservazione); la catena intera la collega a un'altra osservazione: in alto compare una barra, si sceglie **anche qui** (resta anche dov'era, con gli stessi file) o **sposta**, e si tocca **Collega qui** sull'osservazione giusta (l'unica clip di un'osservazione non si sposta: viene collegata e resta anche li', perche' la sua specie puo' esserci comunque; se non c'e', la si scarta li'). Nessuna nuova analisi: la clip fa parte di quell'osservazione e si carica con lei su iNaturalist. Se la specie e' diversa, i numeri di BirdNET (che erano per l'altra specie) non valgono piu': la clip mostra "collegata a mano" al posto di clip_conf, senza bande, non conta nel max_obs_conf e su iNaturalist va caricata per prima; restano le specie probabili per BirdNET. I file condivisi si cancellano solo quando nessuna clip li usa piu' (anche nella pulizia dopo iNaturalist); **Scarta** (la clip va nel Cestino, l'osservazione resta) e il cestino rosso, che elimina subito e per sempre quella clip (audio, spettrogramma e riga su Supabase); da tastiera spazio e frecce;
  - riduzione del rumore (bacchetta, solo per l'ascolto): il rumore e' la clip fuori dalle bande (con 0.3 s di margine; senza bande, il quarto piu' silenzioso), il suo spettro bin per bin viene sottratto da tutta la clip, mai sotto un pavimento. Un tocco = media (fondo circa -6 dB, il canto emerge in tutte le 14 clip di prova, +2.5 dB), due = forte (fondo -18 dB, +7 dB in media ma su qualche clip si mangia un uccello debole), tre = originale; si riprende dallo stesso punto e sullo spettrogramma compare "rumore ridotto: media/forte". Gira in un worker (`docs/assets/denoise-worker.js`, ~2 s per una clip di un minuto). Il file su Supabase, BirdNET e iNaturalist restano sull'originale (BirdNET da' punteggi piu' bassi all'audio ripulito: e' addestrato su registrazioni grezze);
  - volume: cursore in alto (sul telefono dietro l'altoparlante) dal 10% al 3162% (+30 dB), uguale per tutte le clip, anche su quella che sta suonando, ricordato dal browser; tocca la percentuale per tornare al 100%. Le clip registrate fino a ottobre 2026 sono 40-50 dB sotto e vogliono un volume alto.
- **Report**: 7/30/90 giorni o un anno; ogni cosa una volta sola, contata in osservazioni (le visite che rivedi). In alto, chiusa, la tendina **Su iNaturalist**: le caricate del periodo (specie, data e ora, link all'osservazione su iNaturalist, "BirdNET: ..." se aveva sentito altro) e **Specie per mese**, su tutte le caricate da sempre: quante identificazioni al giorno ha ogni specie in ogni mese dell'anno (le sue osservazioni del mese diviso i giorni del mese con caricate; sotto ogni mese quei giorni, "·" dove non ce ne sono; piu' scura, piu' frequente). Poi specie, osservazioni e minuti di audio del periodo, le osservazioni per ora e la tabella delle specie (osservazioni, giorni, confidenza massima di BirdNET, ultima volta; si ordina toccando l'intestazione di una colonna, di nuovo per invertire, e la scelta resta) con **NEW** accanto a quelle sentite per la prima volta in assoluto nel periodo (non compare quando il periodo arriva al primo giorno di ascolto, perche' lo sarebbero tutte). I nomi delle specie aprono le loro osservazioni del periodo.
- **Statistiche** (viste `species_stats`, `daily_summary`, `hourly_activity`, report, tabella del live): contano la specie che hai deciso, cioe' la correzione (matita) se c'e', altrimenti quella di BirdNET. Per le osservazioni caricate decide iNaturalist: ogni ora il servizio di invio legge il taxon che iNaturalist mostra (identificazioni della comunita' e tue) e lo scrive come correzione, per esempio una "gru" che la comunita' ha riconosciuto come volpe, o un tordo che hai lasciato solo al genere *Turdus*. Una sottospecie conta come la sua specie, un genere resta genere; la stessa specie con un altro nome (*Corvus*/*Coloeus monedula*) e' riconosciuta dal taxon di iNaturalist e non cambia nulla. Sulla scheda la riga diventa "Su iNaturalist", senza matita; nel report, accanto alle caricate, "BirdNET: ..." quando aveva sentito altro (`daily_summary.birdnet_scientific_name` tiene l'originale). La confidenza di BirdNET conta solo dove riguarda quella specie: un'osservazione corretta non ne ha (nel report "—", e su iNaturalist niente nota). Una specie corretta che non c'era ancora viene aggiunta da un trigger del database; entro un'ora il servizio di invio le da' i nomi (BirdNET e Wikipedia) e i link eBird/iNaturalist. Il filtro per specie trova sia la specie di BirdNET sia la correzione.
- **Cestino**: osservazioni e clip scartate, da riascoltare, ripristinare o eliminare per sempre (con conferma). L'eliminazione cancella righe e file da Supabase e annota le clip in `birds.deleted_clips`, cosi' il PC non le rimanda. Regola dello storico: una riga sparisce solo con quello che hai giudicato sbagliato; quello che hai scartato non conta nei report gia' da subito, anche prima di svuotare il Cestino. Quello che e' su iNaturalist resta nello storico: il cestino tondo di una sua clip toglie solo audio e spettrogramma (come dopo il caricamento), la riga resta e continua a contare; il database stesso rifiuta di cancellare osservazioni caricate e le loro clip non scartate (trigger `keep_observation_history` e `keep_clip_history`). Se su iNaturalist si scopre che era sbagliata, prima la si scarta, poi il Cestino la puo' eliminare.
- **iNat**: con il token personale di https://www.inaturalist.org/users/api_token (24 ore, resta nel browser) carica le osservazioni confermate, con la specie corretta se indicata (nome esatto o sinonimo su iNaturalist), fino a 5 clip in MP3 e la posizione precisa/oscurata/privata. Come nota solo `BirdNET confidence 0.957` (la confidenza massima dell'osservazione; niente se la specie e' corretta), piu' le etichette `audio,birdnet`. Supabase tiene traccia di cosa e' online (`inat_observation_id`, `clips.inat_sound_id`, stato "caricata", `uploaded_at`): un caricamento interrotto riprende senza doppioni.
- **Appena caricata su iNaturalist** il sito toglie da Supabase MP3 e spettrogrammi di quell'osservazione (anche delle clip oltre le 5 inviate: l'osservazione e' li'; le clip scartate tengono i loro file per il Cestino). Se non ci riesce (connessione caduta a meta'), lo fa il servizio di invio al controllo orario (a PC spento recupera alla riaccensione). Le righe delle clip restano (sul sito la scheda mostra solo i dati dell'osservazione, senza riquadri delle clip), segnate con `media_deleted_at`, con le osservazioni: nessuna riga viene mai cancellata per far spazio, perche' report storici, conteggi e rarita' si calcolano da loro (una clip e' qualche centinaio di byte, i file sono il 99% dello spazio): sul sito al posto del player compare "Audio tolto da Supabase il ...: e' su iNaturalist".

Sviluppo:

- `make dev` / `make dev-stop`: il sito su http://127.0.0.1:8004 con i dati veri (`make stop` resta per il servizio live).
- `make dev_mob`: anteprima mobile senza commit, su http://127.0.0.1:8005/mobile e dal telefono (stessa Wi-Fi) all'indirizzo che stampa; si ricarica da sola a ogni salvataggio.
- `make web-dev`: Supabase finto in locale (Postgres integrato + PostgREST) con una copia in sola lettura dei dati veri, su http://127.0.0.1:8010.
- `make git`: committa tutto e fa push (pubblica il sito).

## Prestazioni

`make test` esegue i test (si ferma se uno fallisce, e mostra i piu' lenti) e poi `dev/benchmark.py`: BirdNET vero su audio sintetico, con le stesse chiamate del live, del sync e del backfill. Per ogni fase stampa tempo e RAM e li confronta con `dev/benchmark_baseline.json` (misurato su questo portatile): segnala quello che e' piu' lento o pesante di oltre il 30%. Ogni esecuzione si aggiunge a `~/.cache/bird-audio-suite/benchmark_history.jsonl`. Dopo un cambiamento voluto (o su un altro PC) `make bench-baseline` aggiorna il riferimento.
