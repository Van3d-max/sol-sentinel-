"""Agente trend + notizie per Coinbase (spot, senza leva).

Unisce le idee migliori dei progetti studiati:
  - trend giornaliero (media 10 sopra media 30): l'unica strategia promossa nei nostri test;
  - filtro notizie (stile Signalforge): l'AI da' solo un punteggio, non piazza ordini;
  - dibattito rialzista/ribassista + risk manager (stile TradingAgents): solo prima di
    un acquisto, e puo' soltanto bloccarlo;
  - stop loss piazzato su Coinbase (dal nostro bot), registro di ogni decisione.

Uso:
  python agente.py --check       prova fonti, Claude, prezzi e candele (nessuna operazione)
  python agente.py               simulazione (paper), un controllo ogni ora
  python agente.py --una-volta   un solo controllo e poi esce (per cron / Termux)
  python agente.py --live        soldi veri (chiede conferma scritta)
  python agente.py --sblocca     rimuove un blocco di sicurezza
  python agente.py --config config_veloce.json   modalita' veloce (2-4 operazioni al giorno)
Per fermarlo: Ctrl+C oppure un file STOP nella cartella.
"""
import argparse
import csv
import json
import logging
import os
import signal as sig
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import apprendimento as ap
import notizie as nz
from strategia import sma, atr

CARTELLA = os.path.dirname(os.path.abspath(__file__))
os.chdir(CARTELLA)
API_PUB = "https://api.coinbase.com/api/v3/brokerage/market/products/"
STATI_CHIUSI = ("CANCELLED", "EXPIRED", "FAILED")
log = logging.getLogger("agente")
_fermati = False


def _ctrl_c(*_):
    global _fermati
    _fermati = True
    log.info("Arresto richiesto, chiudo appena possibile...")


def stop_richiesto():
    return _fermati or os.path.exists("STOP")


# ------------------------------------------------------------------ mercato (endpoint pubblici)

class Mercato:
    def __init__(self, coppia):
        self.coppia = coppia
        self._info = None

    def _get(self, percorso="", **p):
        for tentativo in range(4):
            try:
                r = requests.get(API_PUB + self.coppia + percorso, params=p, timeout=15)
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if tentativo == 3:
                    raise
                time.sleep(3)

    def info(self):
        if self._info is None:
            self._info = self._get()
        return self._info

    def prezzo(self):
        return float(self._get()["price"])

    def candele(self, granularita="ONE_DAY", secondi=86400, quante=90):
        """Ultime candele CHIUSE, dalla piu' vecchia alla piu' recente."""
        ora = int(time.time())
        fine = ora // secondi * secondi
        dati = self._get("/candles", start=fine - quante * secondi, end=fine,
                         granularity=granularita, limit=quante).get("candles", [])
        c = sorted(([int(x["start"]), float(x["open"]), float(x["high"]), float(x["low"]),
                     float(x["close"])] for x in dati), key=lambda x: x[0])
        return [{"t": x[0], "open": x[1], "high": x[2], "low": x[3], "close": x[4]}
                for x in c if x[0] + secondi <= ora]


class BrokerSimulato:
    modalita = "paper"

    def __init__(self, mercato, fee):
        self.m, self.fee = mercato, fee

    def compra_mercato(self, coppia, euro):
        p = self.m.prezzo()
        f = euro * self.fee
        q = (euro - f) / p
        return {"id": "paper-" + uuid.uuid4().hex[:8], "quantita": q, "prezzo_medio": p,
                "commissioni": f, "controvalore": q * p}

    def vendi_mercato(self, coppia, q):
        p = self.m.prezzo()
        return {"id": "paper-" + uuid.uuid4().hex[:8], "quantita": q, "prezzo_medio": p,
                "commissioni": q * p * self.fee, "controvalore": q * p}


# ------------------------------------------------------------------ stato e registri

class Stato:
    def __init__(self, modalita, nome="agente"):
        self.file = "stato_%s_%s.json" % (nome, modalita)
        self.d = {"pos": None, "giorno": None, "ingressi_oggi": 0, "pnl_oggi": 0.0,
                  "ultima_uscita": 0, "bloccato": None}
        if os.path.exists(self.file):
            with open(self.file, encoding="utf-8") as f:
                self.d.update(json.load(f))

    def salva(self):
        with open(self.file + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=2)
        os.replace(self.file + ".tmp", self.file)


def fp(x):
    """Prezzi leggibili per ogni moneta: 68.35 per SOL, 0.0000098 per SHIB."""
    return "%.6g" % x if abs(x) < 1 else ("%.4f" % x if abs(x) < 1000 else "%.2f" % x)


def scrivi_csv(file, campi, riga):
    nuovo = not os.path.exists(file)
    if not nuovo:                                     # registro vecchio con meno colonne: lo aggiorno
        with open(file, newline="", encoding="utf-8") as f:
            vecchie = list(csv.reader(f, delimiter=";"))
        if vecchie and vecchie[0] != campi:
            intest = vecchie[0]
            righe = [dict(zip(intest, r)) for r in vecchie[1:]]
            with open(file + ".tmp", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=campi, delimiter=";")
                w.writeheader()
                for r in righe:
                    w.writerow({k: r.get(k, "") for k in campi})
            os.replace(file + ".tmp", file)
    with open(file, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campi, delimiter=";")
        if nuovo:
            w.writeheader()
        w.writerow({k: riga.get(k, "") for k in campi})


CAMPI_DEC = ["data", "prezzo", "media_veloce", "media_lenta", "trend", "punteggio_notizie",
             "n_notizie", "nuove", "valutate_claude", "shock", "posizione", "azione", "motivo",
             "notizia_principale", "atr_pct"]
CAMPI_OP = ["data_apertura", "data_chiusura", "modalita", "coppia", "quantita", "prezzo_entrata",
            "prezzo_uscita", "acquisto_eur", "vendita_eur", "commissioni_eur", "risultato_netto_eur",
            "motivo_uscita", "notizie_ingresso", "dibattito", "id_ordini", "durata_ore"] + ap.CAMPI_CONTESTO


# ------------------------------------------------------------------ agente

class Agente:
    def __init__(self, cfg, broker, mercato, stato):
        self.cfg, self.b, self.m, self.s = cfg, broker, mercato, stato
        self.tz = ZoneInfo(cfg["fuso_orario"])
        self.cache = nz.Cache(cfg["file_cache"])

    def ora(self):
        return datetime.now(self.tz).isoformat(timespec="seconds")

    def blocca(self, motivo):
        self.s.d["bloccato"] = motivo
        self.s.salva()
        log.critical("AGENTE FERMATO: %s", motivo)
        log.critical("Controlla su Coinbase, poi riavvia con --sblocca.")

    def nuovo_giorno(self):
        oggi = datetime.now(self.tz).date().isoformat()
        if self.s.d["giorno"] != oggi:
            self.s.d.update(giorno=oggi, ingressi_oggi=0, pnl_oggi=0.0)
            self.s.salva()

    # ---------------------------------------------------- un controllo completo
    def controllo(self, notizie=None):
        """notizie: (voci gia' valutate, nuove, da_claude) quando le legge il giro comune per tutti gli agenti."""
        cfg, r = self.cfg, self.cfg["regole"]
        self.nuovo_giorno()
        t0 = time.time()
        if notizie is None:
            voci, fonti = nz.raccogli(cfg, log.info)
            log.info("Fonti: %s", ", ".join("%s=%s" % (k, v) for k, v in fonti.items()))
            voci, nuove, da_claude = nz.valuta(voci, cfg, self.cache, log.info)
        else:
            tutte, nuove, da_claude = notizie
            limite = time.time() - cfg["notizie"]["finestra_ore"] * 3600
            voci = [v for v in tutte if v["ts"] >= limite]
        agg = nz.aggrega(voci, cfg)
        tr = cfg["trend"]
        cand = self.m.candele(tr.get("candela", "ONE_DAY"), tr.get("secondi", 86400), 120)
        chiusure = [c["close"] for c in cand]
        mv, ml = sma(chiusure, tr["media_veloce"])[-1], sma(chiusure, tr["media_lenta"])[-1]
        a = atr(cand, tr["atr_periodo"])[-1]
        prezzo = self.m.prezzo()
        if None in (mv, ml, a):
            raise RuntimeError("candele insufficienti per le medie")
        trend = mv > ml
        sup = None
        if cfg.get("trend_superiore"):                # es. agente intermedio: entra solo col trend giornaliero a favore
            ts = cfg["trend_superiore"]
            cs = [c["close"] for c in self.m.candele(ts.get("candela", "ONE_DAY"), ts.get("secondi", 86400), 120)]
            mvs, mls = sma(cs, ts["media_veloce"])[-1], sma(cs, ts["media_lenta"])[-1]
            sup = bool(mvs and mls and mvs > mls)
            log.info("Trend superiore (%s): %s", ts.get("candela", "ONE_DAY"), "SU" if sup else "GIU")
        log.info("Prezzo %s | medie %s/%s trend %s | notizie %+.2f su %d (%d nuove, %d da Claude) "
                 "| %.1fs", fp(prezzo), fp(mv), fp(ml), "SU" if trend else "GIU", agg["punteggio"], agg["n"],
                 nuove, da_claude, time.time() - t0)
        for v in agg["principali"]:
            log.info("   (%+.1f r%.1f %s) %s", v["s"], v["r"], v["m"], v["titolo"][:110])

        azione, motivo = "attendi", ""
        pos = self.s.d["pos"]
        ctx = ap.contesto(cfg, prezzo, mv, ml, a, agg, datetime.now(self.tz))
        if pos:
            azione, motivo = self.gestisci(pos, prezzo, trend, agg)
        else:
            ok, motivo = self.puo_entrare(trend, agg, ctx, sup)
            if ok:
                azione, motivo = self.prova_ingresso(prezzo, mv, ml, a, agg, ctx)
        principale = agg["principali"][0]["titolo"][:150] if agg["principali"] else ""
        scrivi_csv(cfg["file_decisioni"], CAMPI_DEC, {
            "data": self.ora(), "prezzo": fp(prezzo), "media_veloce": fp(mv),
            "media_lenta": fp(ml), "trend": "su" if trend else "giu",
            "punteggio_notizie": "%+.3f" % agg["punteggio"], "n_notizie": agg["n"], "nuove": nuove,
            "valutate_claude": da_claude, "shock": len(agg["shock"]),
            "posizione": "si" if self.s.d["pos"] else "no", "azione": azione, "motivo": motivo,
            "notizia_principale": principale, "atr_pct": ctx["atr_pct"]})
        log.info("-> %s%s", azione.upper(), (": " + motivo) if motivo else "")

    def puo_entrare(self, trend, agg, ctx=None, sup=None):
        r = self.cfg["regole"]
        candela = {"FIFTEEN_MINUTE": "15 minuti", "ONE_HOUR": "orario", "ONE_DAY": "giornaliero"}.get(
            self.cfg["trend"].get("candela", "ONE_DAY"), self.cfg["trend"].get("candela", ""))
        if not trend:
            return False, "trend %s non rialzista" % candela
        if sup is False:
            return False, "trend giornaliero non rialzista (filtro superiore)"
        if agg["shock"]:
            return False, "notizia shock: " + agg["shock"][0]["titolo"][:80]
        if agg["punteggio"] < r["soglia_ingresso"]:
            return False, "notizie sotto la soglia (%+.2f)" % agg["punteggio"]
        if self.s.d["ingressi_oggi"] >= r["max_ingressi_giorno"]:
            return False, "gia' entrato oggi"
        if time.time() - self.s.d["ultima_uscita"] < r["pausa_dopo_uscita_ore"] * 3600:
            return False, "pausa dopo l'ultima uscita"
        if self.s.d["pnl_oggi"] <= -abs(r["max_perdita_giornaliera_eur"]):
            return False, "perdita massima di oggi raggiunta"
        motivo = ap.freno(self.cfg, datetime.now(self.tz))
        if motivo:
            return False, motivo
        if ctx:
            motivo = ap.obiettivo_troppo_piccolo(self.cfg, ctx) or ap.filtro_appreso(self.cfg, ctx)
            if motivo:
                return False, motivo
        return True, ""

    def prova_ingresso(self, prezzo, mv, ml, a, agg, ctx=None):
        contesto = {"moneta": self.cfg["notizie"]["moneta"],
                    "mercato": {"prezzo": prezzo, "media10": round(mv, 4), "media30": round(ml, 4),
                                "atr_giornaliero": round(a, 4), "punteggio_notizie": agg["punteggio"]},
                    "titoli": agg["principali"]}
        d = nz.dibattito(contesto, self.cfg, log.info)
        if d.get("rialzista"):
            log.info("   rialzista: %s", d["rialzista"])
            log.info("   ribassista: %s", d.get("ribassista", ""))
        if d["decisione"] != "approva":
            return "bloccato dal dibattito", d.get("motivo", "")
        return self.apri(prezzo, a, agg, d.get("motivo", ""), ctx)

    def apri(self, prezzo, a, agg, motivo_dib, ctx=None):
        cfg = self.cfg
        euro = float(cfg["puntata_eur"])
        minimo = float(self.m.info().get("quote_min_size") or 0)
        if euro < minimo:
            return "attendi", "puntata sotto il minimo di Coinbase (%s)" % minimo
        try:
            acq = self.b.compra_mercato(cfg["coppia"], euro)
        except Exception as e:
            return "errore", "acquisto fallito: %s" % e
        sl = acq["prezzo_medio"] - cfg["trend"]["stop_atr"] * a
        pos = {"apertura": self.ora(), "quantita": acq["quantita"], "entrata": acq["prezzo_medio"],
               "costo": acq["controvalore"] + acq["commissioni"], "comm": acq["commissioni"],
               "sl": sl, "sl_limite": sl * (1 - cfg["sl_offset_limite_pct"] / 100), "id_stop": None,
               "tp": (acq["prezzo_medio"] + cfg["trend"]["take_profit_atr"] * a
                      if cfg["trend"].get("take_profit_atr") else None),
               "ordini": [acq["id"]], "notizie": agg["punteggio"], "dibattito": motivo_dib,
               "ctx": ctx or {}}
        self.s.d["pos"] = pos
        self.s.d["ingressi_oggi"] += 1
        self.s.salva()
        if self.b.modalita == "live":
            try:
                pos["id_stop"] = self.b.piazza_stop(cfg["coppia"], pos["quantita"], pos["sl"], pos["sl_limite"])
                self.s.salva()
            except Exception as e:
                log.error("Stop loss NON piazzato (%s): rivendo subito", e)
                try:
                    self.chiudi([self.b.vendi_mercato(cfg["coppia"], pos["quantita"])], "stop non piazzabile")
                except Exception as e2:
                    self.blocca("stop non piazzabile e vendita fallita (%s): vendi a mano" % e2)
                    return "errore", "posizione senza protezione"
                self.blocca("stop loss non piazzabile (%s)" % e)
                return "errore", "stop non piazzabile, posizione chiusa"
        return "compra", "trend su + notizie %+.2f; %s" % (agg["punteggio"], motivo_dib)

    def gestisci(self, pos, prezzo, trend, agg):
        coppia = self.cfg["coppia"]
        if self.b.modalita == "live":
            so = self.b.stato_ordine(pos["id_stop"])
            if so["stato"] == "FILLED":
                self.chiudi([so], "stop loss")
                return "venduto", "stop loss eseguito su Coinbase"
            if so["stato"] in STATI_CHIUSI:
                self.blocca("lo stop loss risulta %s senza che l'agente l'abbia annullato" % so["stato"])
                return "errore", "stop annullato dall'esterno"
            if prezzo < pos["sl_limite"]:
                return self.esci("stop saltato (gap), venduto a mercato")
        elif prezzo <= pos["sl"]:
            self.chiudi([self.b.vendi_mercato(coppia, pos["quantita"])], "stop loss")
            return "venduto", "stop loss (simulato)"
        if pos.get("tp") and prezzo >= pos["tp"]:
            return self.esci("take profit")
        if not trend:
            return self.esci("trend finito")
        if agg["shock"]:
            return self.esci("notizia shock: " + agg["shock"][0]["titolo"][:80])
        if agg["punteggio"] <= self.cfg["regole"]["soglia_uscita"]:
            return self.esci("notizie molto negative (%+.2f)" % agg["punteggio"])
        return "tieni", "trend su, notizie %+.2f, stop %s" % (agg["punteggio"], fp(pos["sl"]))

    def esci(self, motivo):
        pos, coppia = self.s.d["pos"], self.cfg["coppia"]
        esec = []
        if self.b.modalita == "live" and pos.get("id_stop"):
            self.b.annulla(pos["id_stop"])
            time.sleep(1)
            so = self.b.stato_ordine(pos["id_stop"])
            if so["stato"] == "FILLED":
                self.chiudi([so], "stop loss")
                return "venduto", "stop loss eseguito"
            if so["stato"] not in STATI_CHIUSI:
                return "attendi", "stop ancora attivo, riprovo al prossimo controllo"
            if so["quantita"] > 0:
                esec.append(so)
        resto = pos["quantita"] - sum(e["quantita"] for e in esec)
        try:
            esec.append(self.b.vendi_mercato(coppia, resto))
        except Exception as e:
            if self.b.modalita == "live":
                try:
                    pos["id_stop"] = self.b.piazza_stop(coppia, resto, pos["sl"], pos["sl_limite"])
                    pos["quantita"] = resto
                    self.s.salva()
                except Exception as e2:
                    self.blocca("vendita fallita (%s) e stop non ripiazzabile (%s): vendi a mano" % (e, e2))
            return "errore", "vendita fallita: %s" % e
        self.chiudi(esec, motivo)
        return "venduto", motivo

    def chiudi(self, esec, motivo):
        pos = self.s.d["pos"]
        lordo = sum(e["controvalore"] for e in esec)
        comm = sum(e["commissioni"] for e in esec)
        q = sum(e["quantita"] for e in esec)
        netto = lordo - comm - pos["costo"]
        try:
            durata = (datetime.now(self.tz) - datetime.fromisoformat(pos["apertura"])).total_seconds() / 3600
        except (KeyError, ValueError):
            durata = None
        self.s.d.update(pos=None, ultima_uscita=time.time(), pnl_oggi=self.s.d["pnl_oggi"] + netto)
        self.s.salva()
        riga = {
            "data_apertura": pos["apertura"], "data_chiusura": self.ora(), "modalita": self.b.modalita,
            "coppia": self.cfg["coppia"], "quantita": "%.8f" % q, "prezzo_entrata": fp(pos["entrata"]),
            "prezzo_uscita": fp(lordo / q if q else 0), "acquisto_eur": "%.4f" % pos["costo"],
            "vendita_eur": "%.4f" % (lordo - comm), "commissioni_eur": "%.4f" % (pos["comm"] + comm),
            "risultato_netto_eur": "%.4f" % netto, "motivo_uscita": motivo,
            "notizie_ingresso": "%+.3f" % pos["notizie"], "dibattito": pos["dibattito"],
            "id_ordini": ",".join(pos["ordini"] + [e["id"] for e in esec]),
            "durata_ore": "" if durata is None else "%.2f" % durata}
        riga.update(ap.contesto_csv(pos.get("ctx")))
        scrivi_csv(self.cfg["file_operazioni"], CAMPI_OP, riga)
        log.info("POSIZIONE CHIUSA (%s): %+.2f EUR", motivo, netto)

    def gira(self, una_volta=False):
        log.info("Agente '%s' avviato in modalita' %s su %s", self.cfg.get("nome", "agente"),
                 self.b.modalita.upper(), self.cfg["coppia"])
        while not stop_richiesto():
            try:
                self.controllo()
            except Exception as e:
                log.exception("Errore nel controllo: %s", e)
            if una_volta or self.s.d["bloccato"]:
                break
            for _ in range(int(self.cfg["intervallo_minuti"] * 60)):
                if stop_richiesto():
                    break
                time.sleep(1)
        log.info("Agente fermato.")


# ------------------------------------------------------------------ --check

def verifica(cfg):
    print("== Fonti di notizie ==")
    t0 = time.time()
    voci, fonti = nz.raccogli(cfg, print)
    for nome, esito in fonti.items():
        print("  %-28s %s" % (nome[:28], esito if isinstance(esito, str) else "%d titoli" % esito))
    print("  -> %d titoli su %s nelle ultime %d ore (%.1fs)" % (
        len(voci), cfg["notizie"]["moneta"], cfg["notizie"]["finestra_ore"], time.time() - t0))
    for v in voci[:5]:
        print("     - %s" % v["titolo"][:100])

    print("\n== Valutazione ==")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  Chiave Anthropic trovata: valuto con %s" % cfg["notizie"]["modello"])
    else:
        print("  Nessuna ANTHROPIC_API_KEY: uso il dizionario di parole chiave")
    cache = nz.Cache(cfg["file_cache"])
    voci, nuove, claude = nz.valuta(voci, cfg, cache, print)
    agg = nz.aggrega(voci, cfg)
    print("  %d nuove valutate (%d da Claude). Punteggio complessivo: %+.2f, shock: %d" % (
        nuove, claude, agg["punteggio"], len(agg["shock"])))
    for v in agg["principali"]:
        print("     (%+.1f) %s" % (v["s"], v["titolo"][:95]))

    print("\n== Mercato ==")
    m = Mercato(cfg["coppia"])
    tr = cfg["trend"]
    cand = m.candele(tr.get("candela", "ONE_DAY"), tr.get("secondi", 86400), 120)
    ch = [c["close"] for c in cand]
    mv = sma(ch, cfg["trend"]["media_veloce"])[-1]
    ml = sma(ch, cfg["trend"]["media_lenta"])[-1]
    print("  %s prezzo %.4f | %d candele %s | medie %.4f / %.4f -> trend %s" % (
        cfg["coppia"], m.prezzo(), len(cand), tr.get("candela", "ONE_DAY"), mv, ml,
        "SU" if mv > ml else "GIU"))


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--una-volta", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sblocca", action="store_true")
    ap.add_argument("--config", default="config_agente.json",
                    help="config_agente.json (lenta) oppure config_veloce.json (2-4 al giorno)")
    a = ap.parse_args()
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)

    if a.check:
        verifica(cfg)
        return

    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%d/%m %H:%M:%S")
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(cfg["file_log"], encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)
    sig.signal(sig.SIGINT, _ctrl_c)
    if os.path.exists("STOP"):
        sys.exit("C'e' un file STOP nella cartella: cancellalo per avviare l'agente.")

    stato = Stato("live" if a.live else "paper", cfg.get("nome", "agente"))
    if stato.d["bloccato"]:
        if not a.sblocca:
            sys.exit("Agente bloccato: %s\nControlla e poi avvia con --sblocca." % stato.d["bloccato"])
        stato.d["bloccato"] = None
        stato.salva()

    mercato = Mercato(cfg["coppia"])
    if a.live:
        from broker import BrokerCoinbase, crea_client
        print("\n*** MODALITA' LIVE: SOLDI VERI su Coinbase, %s, %.0f EUR a operazione ***" % (
            cfg["coppia"], cfg["puntata_eur"]))
        if input("Scrivi CONFERMO per partire: ").strip() != "CONFERMO":
            sys.exit("Annullato.")
        broker = BrokerCoinbase(crea_client(cfg, serve_auth=True), cfg)
    else:
        broker = BrokerSimulato(mercato, cfg["commissione_taker"])
    Agente(cfg, broker, mercato, stato).gira(una_volta=a.una_volta)


if __name__ == "__main__":
    main()
