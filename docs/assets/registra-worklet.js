// Registra: gira nel thread audio del browser e passa alla pagina i campioni del microfono (mono),
// a blocchi di 4096 (~85 ms a 48 kHz) invece che ogni 128: meno messaggi, nessun campione perso.
class Raccoglitore extends AudioWorkletProcessor {
  constructor() {
    super();
    this.blocco = new Float32Array(4096);
    this.pieno = 0;
  }

  process(inputs) {
    const canale = inputs[0] && inputs[0][0];
    if (canale) {
      let letto = 0;
      while (letto < canale.length) {
        const quanti = Math.min(canale.length - letto, this.blocco.length - this.pieno);
        this.blocco.set(canale.subarray(letto, letto + quanti), this.pieno);
        this.pieno += quanti;
        letto += quanti;
        if (this.pieno === this.blocco.length) {
          this.port.postMessage(this.blocco, [this.blocco.buffer]);
          this.blocco = new Float32Array(4096);
          this.pieno = 0;
        }
      }
    }
    return true; // resta acceso finché la pagina non lo stacca
  }
}

registerProcessor("raccoglitore", Raccoglitore);
