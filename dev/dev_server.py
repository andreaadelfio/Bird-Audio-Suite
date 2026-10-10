"""Server di sviluppo per l'anteprima mobile del sito di revisione (make dev_mob), come in MyFinance.

Come `python3 -m http.server`, ma:
  - senza cache: ricaricando si vedono subito le modifiche a HTML, CSS e JS;
  - /mobile: il sito dentro un "telefono" (Motorola edge 50 neo) che si ricarica da solo
    quando salvi un file del sito;
  - con --rete il sito si apre anche dal telefono vero, sulla stessa rete Wi-Fi, e anche
    li' si ricarica da solo a ogni salvataggio (lo script si aggiunge solo qui, non nei file).
Dal telefono l'indirizzo e' http (non https): la parte "app installabile" (service worker)
resta spenta, il resto funziona come sul sito pubblicato, con i dati veri di Supabase.

Uso:
    python3 dev/dev_server.py [porta] [--rete]       (Ctrl+C per fermarlo)

Serve solo i file del sito (docs/: index.html, assets/, manifest): non detections/ né .git, che
dalla rete di casa non devono essere raggiungibili.
"""
import socket
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CARTELLA = Path(__file__).resolve().parent.parent / "docs"
FILE_DEL_SITO = ("/index.html", "/manifest.webmanifest", "/service-worker.js", "/assets/")

# Motorola edge 50 neo in Chrome: circa 412 x 915 px "CSS" (stima; aprendo /mobile sul
# telefono la pagina mostra le misure vere, che si impostano con ?w=...&h=...)
PAGINA_MOBILE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bird Audio Suite mobile</title>
<style>
  body { margin: 0; padding: 16px; display: flex; flex-direction: column; align-items: center; gap: 12px;
         background: #2b2f2d; color: #eee; font: 14px system-ui, sans-serif; }
  .barra { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: center; }
  button { font: inherit; padding: 4px 12px; border-radius: 6px; border: 1px solid #888;
           background: #444; color: #eee; cursor: pointer; }
  .telefono { border: 10px solid #111; border-radius: 32px; overflow: hidden; background: #111;
              box-shadow: 0 10px 40px #0008; }
  iframe { display: block; border: 0; background: #fff; }
  .nota { max-width: 560px; margin: 0; text-align: center; color: #aaa; font-size: 12px; }
</style>
</head>
<body>
<div class="barra">
  <strong>Motorola edge 50 neo</strong> <span id="misura"></span>
  <button id="ricarica" type="button">Ricarica</button>
  <button id="ruota" type="button">Ruota</button>
</div>
<div class="telefono"><iframe id="sito" src="/index.html" title="Bird Audio Suite"></iframe></div>
<p class="nota">Si ricarica da solo quando salvi un file del sito.
  Dal telefono (stessa rete Wi-Fi): <strong>__INDIRIZZO_TELEFONO__</strong><br>
  Schermo di questo dispositivo: <span id="schermo"></span> px. Aperta sul telefono, questa riga
  dà le sue misure vere: si usano con /mobile?w=...&amp;h=...</p>
<script>
  const parametri = new URLSearchParams(location.search);
  let larghezza = Number(parametri.get("w")) || 412;
  let altezza = Number(parametri.get("h")) || 915;
  const sito = document.getElementById("sito");

  function dimensiona() {
    sito.style.width = `${larghezza}px`;
    sito.style.height = `${altezza}px`;
    document.getElementById("misura").textContent = `${larghezza} × ${altezza}`;
  }

  // Come sul telefono, la barra di scorrimento non toglie spazio alla pagina
  sito.addEventListener("load", () => {
    sito.contentDocument.head.insertAdjacentHTML("beforeend", "<style>html { scrollbar-width: none; }</style>");
  });
  document.getElementById("ricarica").addEventListener("click", () => sito.contentWindow.location.reload());
  document.getElementById("ruota").addEventListener("click", () => {
    [larghezza, altezza] = [altezza, larghezza];
    dimensiona();
  });
  document.getElementById("schermo").textContent = `${innerWidth} × ${innerHeight}`;
  dimensiona();

  // Ricarica automatica: ogni secondo chiede al server quando è cambiato l'ultimo file
  let ultima = null;
  setInterval(async () => {
    try {
      const adesso = await (await fetch("/__modifiche")).text();
      if (ultima !== null && adesso !== ultima) sito.contentWindow.location.reload();
      ultima = adesso;
    } catch {
      // server fermo: riprova al giro dopo
    }
  }, 1000);
</script>
</body>
</html>
"""


# aggiunto a index.html solo da questo server: il telefono vero si ricarica da solo
RICARICA_AUTOMATICA = """<script>
  (() => {
    let ultima = null;
    setInterval(async () => {
      try {
        const adesso = await (await fetch("/__modifiche", { cache: "no-store" })).text();
        if (ultima !== null && adesso !== ultima) location.reload();
        ultima = adesso;
      } catch {
        // server fermo: riprova al giro dopo
      }
    }, 1000);
  })();
</script>
"""


def ultima_modifica():
    """Data dell'ultimo file del sito salvato (per la ricarica automatica)."""
    file = [f for f in CARTELLA.rglob("*") if f.is_file()]
    return str(max(f.stat().st_mtime for f in file))


def ip_locale():
    """Indirizzo del computer nella rete di casa (None se non c'è rete)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))  # non invia nulla: sceglie solo la scheda di rete
            return s.getsockname()[0]
        except OSError:
            return None


class Gestore(SimpleHTTPRequestHandler):
    indirizzo_telefono = "attiva --rete"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CARTELLA), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        percorso = self.path.split("?")[0]
        if percorso == "/mobile":
            return self.rispondi(PAGINA_MOBILE.replace("__INDIRIZZO_TELEFONO__", self.indirizzo_telefono), "text/html")
        if percorso == "/__modifiche":
            return self.rispondi(ultima_modifica(), "text/plain")
        if percorso in ("/", "/index.html"):
            pagina = (CARTELLA / "index.html").read_text(encoding="utf-8")
            return self.rispondi(pagina.replace("</body>", RICARICA_AUTOMATICA + "</body>"), "text/html")
        if not percorso.startswith(FILE_DEL_SITO):
            return self.send_error(404)
        super().do_GET()

    def rispondi(self, testo, tipo):
        dati = testo.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{tipo}; charset=utf-8")
        self.send_header("Content-Length", str(len(dati)))
        self.end_headers()
        self.wfile.write(dati)

    def log_message(self, *args):
        pass  # niente log a ogni richiesta


def main():
    porta = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8005)
    rete = "--rete" in sys.argv
    ip = ip_locale() if rete else None
    if ip:
        Gestore.indirizzo_telefono = f"http://{ip}:{porta}/"
    server = ThreadingHTTPServer(("0.0.0.0" if rete else "127.0.0.1", porta), Gestore)

    anteprima = f"http://127.0.0.1:{porta}/mobile"
    print(f"Anteprima mobile: {anteprima}")
    if ip:
        print(f"Dal telefono (stessa rete Wi-Fi): http://{ip}:{porta}/")
    print("Ctrl+C per fermare.")
    webbrowser.open(anteprima)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer fermato.")


if __name__ == "__main__":
    main()
