"""Bot di trading spot per Coinbase Advanced Trade — strategia "trend + ritracciamento".

Uso:
  python bot.py --check     verifica chiave, coppie, candele e saldi (nessuna operazione)
  python bot.py             modalita' paper (prezzi veri, operazioni simulate)
  python bot.py --live      soldi veri (chiede conferma scritta)
  python bot.py --sblocca   rimuove un blocco di sicurezza dopo che hai controllato

Per fermarlo: Ctrl+C, oppure crea un file chiamato STOP nella cartella del bot.
"""
import argparse
import csv
import json
import logging
import os
import signal as sig
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import strategia as st
from broker import (BrokerCoinbase, BrokerPaper, DatiMercato, ErroreBroker,
                    crea_client, SECONDI_CANDELA)

CARTELLA = os.path.dirname(os.path.abspath(__file__))
os.chdir(CARTELLA)

STATI_CHIUSI = ("CANCELLED", "EXPIRED", "FAILED")
log = logging.getLogger("bot")
_fermati = False


def _ctrl_c(*_):
    global _fermati
    _fermati = True
    log.info("Arresto richiesto (Ctrl+C), chiudo appena possibile...")


def stop_richiesto():
    return _fermati or os.path.exists("STOP")


def carica_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def imposta_log(file_log):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(file_log, encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)


# ------------------------------------------------------------------ stato

class Stato:
    def __init__(self, modalita):
        self.file = f"stato_{modalita}.json"
        self.d = {"posizioni": {}, "giorno": None, "operazioni_oggi": 0,
                  "pnl_oggi": 0.0, "bloccato": None, "ultima_scansione": 0}
        if os.path.exists(self.file):
            with open(self.file, encoding="utf-8") as f:
                self.d.update(json.load(f))

    def salva(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=2)
        os.replace(tmp, self.file)

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v


# ------------------------------------------------------------------ CSV

CAMPI_CSV = ["data_apertura", "data_chiusura", "modalita", "coppia", "quantita",
             "prezzo_entrata", "prezzo_uscita", "acquisto_eur", "vendita_eur",
             "commissioni_eur", "risultato_netto_eur", "motivo_uscita",
             "stop_loss", "take_profit", "id_ordine_acquisto", "id_ordini_vendita"]


def registra_operazione(file_csv, riga):
    nuovo = not os.path.exists(file_csv)
    with open(file_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPI_CSV, delimiter=";")
        if nuovo:
            w.writeheader()
        w.writerow({k: riga.get(k, "") for k in CAMPI_CSV})


# ------------------------------------------------------------------ bot

class Bot:
    def __init__(self, cfg, broker, stato):
        self.cfg = cfg
        self.b = broker
        self.s = stato
        self.p = cfg["strategia"]
        self.tz = ZoneInfo(cfg.get("fuso_orario", "Europe/Rome"))
        self.fee_ar = 2 * cfg["commissione_taker"]

    def ora_locale(self):
        return datetime.now(self.tz)

    def blocca(self, motivo):
        self.s["bloccato"] = motivo
        self.s.salva()
        log.critical("BOT FERMATO: %s", motivo)
        log.critical("Controlla la situazione su Coinbase, poi riavvia con --sblocca.")

    def aggiorna_giorno(self):
        oggi = self.ora_locale().date().isoformat()
        if self.s["giorno"] != oggi:
            if self.s["giorno"] is not None:
                log.info("Nuovo giorno: contatori azzerati (ieri %d operazioni, %.2f €)",
                         self.s["operazioni_oggi"], self.s["pnl_oggi"])
            self.s["giorno"] = oggi
            self.s["operazioni_oggi"] = 0
            self.s["pnl_oggi"] = 0.0
            self.s.salva()

    def puo_aprire(self):
        c = self.cfg
        if len(self.s["posizioni"]) >= c["max_posizioni_aperte"]:
            return False, "posizione gia' aperta"
        if self.s["operazioni_oggi"] >= c["max_operazioni_giorno"]:
            return False, "raggiunto il massimo di operazioni di oggi"
        if self.s["pnl_oggi"] <= -abs(c["max_perdita_giornaliera_eur"]):
            return False, "raggiunta la perdita massima di oggi"
        return True, ""

    # ---------------------------------------------------------- entrate
    def scansiona(self):
        ok, motivo = self.puo_aprire()
        if not ok:
            log.info("Scansione saltata: %s", motivo)
            return True
        completa = True
        n = st.candele_minime(self.p) + 20
        for coppia in self.cfg["coppie"]:
            if coppia in self.s["posizioni"]:
                continue
            try:
                candele = self.b.candele(coppia, n)
            except Exception as e:
                log.warning("%s: candele non disponibili (%s)", coppia, e)
                completa = False
                continue
            if len(candele) < st.candele_minime(self.p):
                log.warning("%s: solo %d candele, ne servono %d", coppia, len(candele),
                            st.candele_minime(self.p))
                continue
            ind = st.calcola_indicatori(candele, self.p)
            compra, d = st.segnale(ind, len(candele) - 1, self.p, self.fee_ar)
            if "rsi" in d:
                log.info("%s close=%.2f sma200=%.2f sma20=%.2f sma50=%.2f rsi=%.1f->%.1f atr=%.2f | %s",
                         coppia, d["close"], d["sma200"], d["sma20"], d["sma50"],
                         d["rsi_prec"], d["rsi"], d["atr"], d["motivo"])
            if compra:
                self.apri(coppia, d["atr"])
                ok, motivo = self.puo_aprire()
                if not ok or self.s["bloccato"]:
                    return True
        return completa

    def apri(self, coppia, atr_val):
        euro = float(self.cfg["puntata_eur"])
        info = self.b.info_coppia(coppia)
        if euro < float(info.get("quote_min_size") or 0):
            log.warning("%s: puntata %.2f € sotto il minimo di Coinbase (%s)",
                        coppia, euro, info.get("quote_min_size"))
            return
        if self.b.modalita == "live":
            eur = self.b.saldi().get("EUR", 0.0)
            if eur < euro:
                log.warning("Saldo EUR insufficiente (%.2f €) per puntare %.2f €", eur, euro)
                return

        log.info("ACQUISTO %s per %.2f €", coppia, euro)
        try:
            acq = self.b.compra_mercato(coppia, euro)
        except ErroreBroker as e:
            log.error("Acquisto fallito: %s", e)
            return
        self.s["operazioni_oggi"] += 1
        entrata = acq["prezzo_medio"]
        quantita = acq["quantita"]
        sl, tp = st.livelli(entrata, atr_val, self.p)
        lim = sl * (1 - self.cfg["sl_offset_limite_pct"] / 100)
        pos = {"coppia": coppia, "quantita": quantita, "entrata": entrata,
               "costo_eur": acq["controvalore"] + acq["commissioni"],
               "commissioni_acquisto": acq["commissioni"], "sl": sl, "sl_limite": lim,
               "tp": tp, "id_acquisto": acq["id"], "id_stop": None,
               "apertura": self.ora_locale().isoformat(timespec="seconds")}
        log.info("Comprati %s %s a %.4f (comm. %.4f €) | SL %.4f  TP %.4f",
                 quantita, coppia, entrata, acq["commissioni"], sl, tp)

        # salva SUBITO la posizione: se il bot cade qui, al riavvio la ritrova
        self.s["posizioni"][coppia] = pos
        self.s.salva()

        try:
            prezzo_ora = self.b.prezzo(coppia)
            if prezzo_ora <= sl:
                raise ErroreBroker(f"prezzo gia' sotto lo stop ({prezzo_ora:.4f} <= {sl:.4f})")
            pos["id_stop"] = self.b.piazza_stop(coppia, quantita, sl, lim)
            self.s.salva()
            log.info("Stop loss piazzato su %s: id %s",
                     "Coinbase" if self.b.modalita == "live" else "simulatore", pos["id_stop"])
        except Exception as e:
            log.error("Stop loss NON piazzato (%s): rivendo subito", e)
            try:
                v = self.b.vendi_mercato(coppia, quantita)
                self.chiudi(coppia, [v], "stop loss non piazzabile")
            except Exception as e2:
                self.blocca(f"impossibile piazzare lo stop E rivendere {coppia}: {e2}. "
                            "Hai crypto senza protezione: vendi a mano su Coinbase.")
                return
            self.blocca(f"stop loss non piazzabile su {coppia} ({e}); posizione rivenduta.")

    # ---------------------------------------------------------- uscite
    def gestisci(self, coppia):
        pos = self.s["posizioni"][coppia]
        if not pos.get("id_stop"):
            self.blocca(f"posizione {coppia} senza stop loss registrato (bot caduto durante l'apertura?)")
            return
        so = self.b.stato_ordine(pos["id_stop"])
        if so["stato"] == "FILLED":
            log.info("STOP LOSS eseguito su %s a %.4f", coppia, so["prezzo_medio"])
            self.chiudi(coppia, [so], "stop loss")
            return
        if so["stato"] in STATI_CHIUSI:
            if so["quantita"] > 0:
                log.warning("Stop su %s chiuso con esecuzione parziale", coppia)
            self.blocca(f"lo stop loss di {coppia} risulta {so['stato']} senza che il bot "
                        "l'abbia annullato (annullato a mano?). La posizione e' senza protezione.")
            return

        prezzo = self.b.prezzo(coppia)
        if prezzo >= pos["tp"]:
            log.info("TAKE PROFIT raggiunto su %s: %.4f >= %.4f", coppia, prezzo, pos["tp"])
            self.vendi_annullando_stop(coppia, "take profit")
        elif prezzo < pos["sl_limite"]:
            # Lo stop-limit e' scattato ma il prezzo e' sceso sotto il limite (gap):
            # l'ordine resterebbe ineseguito. Si esce a mercato.
            log.warning("Prezzo %.4f sotto il limite dello stop (%.4f) su %s: esco a mercato",
                        prezzo, pos["sl_limite"], coppia)
            self.vendi_annullando_stop(coppia, "stop saltato (gap), venduto a mercato")

    def vendi_annullando_stop(self, coppia, motivo):
        pos = self.s["posizioni"][coppia]
        if not self.b.annulla(pos["id_stop"]):
            log.warning("Annullamento stop non confermato, ricontrollo...")
        time.sleep(1)
        so = self.b.stato_ordine(pos["id_stop"])
        if so["stato"] == "FILLED":
            self.chiudi(coppia, [so], "stop loss")
            return
        if so["stato"] not in STATI_CHIUSI:
            log.warning("Stop ancora attivo (%s): riprovo al prossimo giro", so["stato"])
            return
        esecuzioni = [so] if so["quantita"] > 0 else []
        resto = pos["quantita"] - so["quantita"]
        try:
            esecuzioni.append(self.b.vendi_mercato(coppia, resto))
        except Exception as e:
            # ripristina la protezione
            try:
                pos["id_stop"] = self.b.piazza_stop(coppia, resto, pos["sl"], pos["sl_limite"])
                pos["quantita"] = resto
                self.s.salva()
                log.error("Vendita (%s) fallita (%s): stop ripiazzato, riprovo", motivo, e)
            except Exception as e2:
                self.blocca(f"vendita fallita ({e}) e stop non ripiazzabile ({e2}) "
                            f"su {coppia}: vendi a mano.")
            return
        self.chiudi(coppia, esecuzioni, motivo)

    def chiudi(self, coppia, esecuzioni, motivo):
        pos = self.s["posizioni"].pop(coppia)
        vendita = sum(e["controvalore"] for e in esecuzioni)
        comm_v = sum(e["commissioni"] for e in esecuzioni)
        qv = sum(e["quantita"] for e in esecuzioni)
        p_uscita = vendita / qv if qv else 0.0
        netto = vendita - comm_v - pos["costo_eur"]
        self.s["pnl_oggi"] += netto
        self.s.salva()
        registra_operazione(self.cfg["file_operazioni"], {
            "data_apertura": pos["apertura"],
            "data_chiusura": self.ora_locale().isoformat(timespec="seconds"),
            "modalita": self.b.modalita, "coppia": coppia,
            "quantita": f"{qv:.8f}", "prezzo_entrata": f"{pos['entrata']:.4f}",
            "prezzo_uscita": f"{p_uscita:.4f}", "acquisto_eur": f"{pos['costo_eur']:.4f}",
            "vendita_eur": f"{vendita - comm_v:.4f}",
            "commissioni_eur": f"{pos['commissioni_acquisto'] + comm_v:.4f}",
            "risultato_netto_eur": f"{netto:.4f}", "motivo_uscita": motivo,
            "stop_loss": f"{pos['sl']:.4f}", "take_profit": f"{pos['tp']:.4f}",
            "id_ordine_acquisto": pos["id_acquisto"],
            "id_ordini_vendita": ",".join(e["id"] for e in esecuzioni)})
        log.info("CHIUSA %s (%s): %+.4f € | oggi %+.4f € in %d operazioni",
                 coppia, motivo, netto, self.s["pnl_oggi"], self.s["operazioni_oggi"])
        if self.s["pnl_oggi"] <= -abs(self.cfg["max_perdita_giornaliera_eur"]):
            log.warning("Perdita massima giornaliera raggiunta: niente nuove operazioni fino a domani")

    # ---------------------------------------------------------- ciclo
    def ciclo(self):
        intervallo = self.cfg["intervallo_controllo_sec"]
        log.info("Avvio in modalita' %s su %s", self.b.modalita.upper(), ", ".join(self.cfg["coppie"]))
        if self.s["posizioni"]:
            log.info("Riprendo posizioni aperte: %s", ", ".join(self.s["posizioni"]))
        while not stop_richiesto():
            try:
                self.aggiorna_giorno()
                for coppia in list(self.s["posizioni"]):
                    self.gestisci(coppia)
                    if self.s["bloccato"]:
                        return
                # nuova candela oraria chiusa? (si aspettano 15s per lasciarla consolidare)
                ora = int(time.time())
                ultima_chiusa = (ora - 15) // SECONDI_CANDELA * SECONDI_CANDELA
                if ultima_chiusa > self.s["ultima_scansione"]:
                    if self.scansiona():
                        self.s["ultima_scansione"] = ultima_chiusa
                        self.s.salva()
                    else:
                        log.info("Scansione incompleta: riprovo al prossimo controllo")
                    if self.s["bloccato"]:
                        return
            except ErroreBroker as e:
                log.error("Errore Coinbase: %s", e)
            except Exception as e:
                log.exception("Errore inatteso: %s", e)
            for _ in range(intervallo):
                if stop_richiesto():
                    break
                time.sleep(1)
        log.info("Bot fermato.")
        if self.s["posizioni"]:
            if self.b.modalita == "live":
                log.info("Posizioni ancora aperte: %s. Lo stop loss resta attivo su Coinbase; "
                         "il take profit no finche' il bot e' spento.", ", ".join(self.s["posizioni"]))
            else:
                log.info("Posizioni simulate ancora aperte: %s (riprendono al riavvio).",
                         ", ".join(self.s["posizioni"]))


# ------------------------------------------------------------------ --check

def controllo(cfg):
    ok = True
    print("== Verifica configurazione ==")
    try:
        client = crea_client(cfg, serve_auth=False)
    except ErroreBroker as e:
        print("ERRORE:", e)
        return False
    dati = DatiMercato(client)
    for coppia in cfg["coppie"]:
        try:
            info = dati.info_coppia(coppia)
            stato = info.get("status")
            bloccata = info.get("trading_disabled") or info.get("is_disabled")
            qmin = float(info.get("quote_min_size") or 0)
            print(f"[{'OK' if stato == 'online' and not bloccata else '!!'}] {coppia}: stato={stato} "
                  f"prezzo={info.get('price')} min_ordine={qmin} € incr_qta={info.get('base_increment')}")
            if cfg["puntata_eur"] < qmin:
                print(f"     !! la puntata {cfg['puntata_eur']} € e' sotto il minimo {qmin} €")
                ok = False
            c = dati.candele(coppia, st.candele_minime(cfg["strategia"]) + 20)
            ultima = datetime.fromtimestamp(c[-1]["t"], timezone.utc).astimezone(
                ZoneInfo(cfg["fuso_orario"])) if c else None
            print(f"     candele orarie chiuse: {len(c)} (ultima {ultima:%d/%m %H:%M})")
            if len(c) < st.candele_minime(cfg["strategia"]):
                print("     !! candele insufficienti per la SMA lunga")
                ok = False
        except Exception as e:
            print(f"[!!] {coppia}: {e}")
            ok = False

    print("\n== Chiave API e saldi ==")
    try:
        client_auth = crea_client(cfg, serve_auth=True)
        saldi = BrokerCoinbase(client_auth, cfg).saldi()
        print("[OK] Chiave valida")
        valute = {"EUR"} | {c.split("-")[0] for c in cfg["coppie"]}
        for v in sorted(valute):
            print(f"     {v}: {saldi.get(v, 0.0):.8f}".rstrip("0").rstrip("."))
        if saldi.get("EUR", 0.0) < cfg["puntata_eur"]:
            print(f"     !! saldo EUR sotto la puntata ({cfg['puntata_eur']} €): in live non comprera'")
    except Exception as e:
        print(f"[!!] Chiave/saldi: {e}")
        print("     (per la modalita' paper la chiave non serve)")
        ok = False

    fee = cfg["commissione_taker"]
    print(f"\nCommissioni impostate: {fee*100:.2f}% a operazione, ~{2*fee*100:.2f}% andata e ritorno "
          f"(~{2*fee*cfg['puntata_eur']:.3f} € su {cfg['puntata_eur']} €).")
    print("\nTutto OK." if ok else "\nCi sono problemi da sistemare (vedi sopra).")
    return ok


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="verifica senza operare")
    ap.add_argument("--live", action="store_true", help="soldi veri")
    ap.add_argument("--sblocca", action="store_true", help="rimuove il blocco di sicurezza")
    a = ap.parse_args()

    cfg = carica_config()
    if a.check:
        sys.exit(0 if controllo(cfg) else 1)

    imposta_log(cfg.get("file_log", "bot.log"))
    sig.signal(sig.SIGINT, _ctrl_c)
    if hasattr(sig, "SIGTERM"):
        sig.signal(sig.SIGTERM, _ctrl_c)
    if os.path.exists("STOP"):
        print("C'e' un file STOP nella cartella: cancellalo per avviare il bot.")
        sys.exit(1)

    modalita = "live" if a.live else "paper"
    stato = Stato(modalita)
    if stato["bloccato"]:
        if a.sblocca:
            log.info("Blocco rimosso: %s", stato["bloccato"])
            stato["bloccato"] = None
            stato.salva()
        else:
            print(f"Il bot e' bloccato: {stato['bloccato']}\n"
                  "Controlla su Coinbase e poi avvia con --sblocca.")
            sys.exit(1)

    try:
        if a.live:
            print("\n*** MODALITA' LIVE: operazioni con SOLDI VERI su Coinbase ***")
            print(f"Puntata {cfg['puntata_eur']} € | max {cfg['max_posizioni_aperte']} posizione/i | "
                  f"max {cfg['max_operazioni_giorno']} operazioni/giorno | stop giornaliero "
                  f"-{cfg['max_perdita_giornaliera_eur']} €")
            if input("Scrivi CONFERMO per partire: ").strip() != "CONFERMO":
                print("Annullato.")
                sys.exit(1)
            broker = BrokerCoinbase(crea_client(cfg, serve_auth=True), cfg)
        else:
            broker = BrokerPaper(DatiMercato(crea_client(cfg, serve_auth=False)), cfg)
    except ErroreBroker as e:
        print("ERRORE:", e)
        sys.exit(1)

    Bot(cfg, broker, stato).ciclo()


if __name__ == "__main__":
    main()
