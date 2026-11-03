# BirdNET V2.4 per il browser (Registra)

File presi senza modifiche dal pacchetto ufficiale `BirdNET_v2.4_tfjs.zip`, record Zenodo 15050749
(https://zenodo.org/records/15050749), di Stefan Kahl e del BirdNET Team (K. Lisa Yang Center for
Conservation Bioacoustics, Cornell Lab of Ornithology, e Chemnitz University of Technology).

- `model.json` + `group1-shard*of13.bin`: il modello che riconosce ~6500 specie da 3 s di audio a 48 kHz;
- `mdata/`: il modello delle specie possibili in un luogo in una settimana;
- `labels.json`: i nomi ("Scientifico_Inglese"), nello stesso ordine delle uscite;
- `sample.wav`: l'esempio del pacchetto (3 s di organetto minore), per provare Registra senza microfono
  (`?prova-audio=models/birdnet/sample.wav`).

Licenza: Creative Commons Attribuzione - Non commerciale (CC BY-NC 4.0 sul record Zenodo; i modelli di
BirdNET-Analyzer sono distribuiti anche come CC BY-NC-SA 4.0). Solo uso personale e non commerciale.
