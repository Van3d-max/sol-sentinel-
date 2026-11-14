"""Giro unico: tutte le monete e tutti gli agenti in un solo passaggio (ogni 15 minuti).

- Le notizie di ogni moneta si scaricano e si fanno valutare a Claude UNA volta sola,
  poi le usano tutti gli agenti di quella moneta (memoria notizie condivisa: cache_notizie_<moneta>.json).
- I feed in comune (CoinDesk, Cointelegraph, ...) si scaricano una volta per giro, non una per moneta.
- Ogni agente gira solo quando e' il suo turno: veloce ogni 15 minuti, intermedio e lento ogni ora.
- Al primo giro di un agente nuovo ricostruisce il suo passato dal giorno d'inizio (storico.py),
  cosi' grafico e apprendimento partono gia' con i dati.
- Scrive agenti.json per la pagina di controllo.

Uso:  python giro.py               un giro (quello che fa GitHub ogni 15 minuti)
      python giro.py --tutti       fa girare tutti gli agenti anche se non e' il loro turno
"""
import argparse
import copy
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import agente as A
import notizie as nz
import storico

CARTELLA = os.path.dirname(os.path.abspath(__file__))
os.chdir(CARTELLA)
log = A.log
SOL = {"veloce": "config_veloce.json", "intermedio": "config_intermedio.json", "lento": "config_agente.json"}
NOMI_SOL = {"veloce": "veloce", "intermedio": "intermedio", "lento": "lenta"}
ETICHETTE = {"veloce": ("Veloce", "15 MIN"), "intermedio": ("Intermedio", "1 ORA"), "lento": ("Lento", "GIORNALIERO")}
INTERMEDIO_DEFAULT = {   # se config_intermedio.json non c'e' ancora, l'intermedio nasce dal veloce con queste regole
    "trend": {"candela": "ONE_HOUR", "secondi": 3600, "media_veloce": 20, "media_lenta": 50, "stop_atr": 2.0,
              "take_profit_atr": 4.0, "atr_periodo": 14},
    "trend_superiore": {"candela": "ONE_DAY", "secondi": 86400, "media_veloce": 10, "media_lenta": 30},
    "regole_extra": {"max_ingressi_giorno": 2, "pausa_dopo_uscita_ore": 4, "obiettivo_min_pct": 2.2},
    "intervallo_minuti": 60,
}


def carica(f):
    with open(f, encoding="utf-8") as fh:
        return json.load(fh)


def salva(f, d):
    with open(f + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2, ensure_ascii=False)
    os.replace(f + ".tmp", f)


def modelli():
    m = {"veloce": carica(SOL["veloce"]), "lento": carica(SOL["lento"])}
    if os.path.exists(SOL["intermedio"]):
        m["intermedio"] = carica(SOL["intermedio"])
    else:
        c = copy.deepcopy(m["veloce"])
        c["trend"] = dict(INTERMEDIO_DEFAULT["trend"])
        c["trend_superiore"] = dict(INTERMEDIO_DEFAULT["trend_superiore"])
        c["regole"].update(INTERMEDIO_DEFAULT["regole_extra"])
        c["intervallo_minuti"] = INTERMEDIO_DEFAULT["intervallo_minuti"]
        m["intermedio"] = c
    return m


def notizie_moneta(base, mon):
    n = copy.deepcopy(base)
    n["moneta"] = mon["nome"]
    n["parole_chiave"] = mon["parole_chiave"]
    n["query_gdelt"] = mon["google"]
    q = mon["google"].replace(" ", "+")
    n["feed_rss"] = [u for u in n["feed_rss"] if "news.google.com" not in u] + [
        "https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % q]
    return n


def prepara(mon, tipo, mod):
    """Config dell'agente: per SOL usa i file di sempre, per le altre monete lo crea dal modello."""
    cod = mon["codice"]
    f = SOL[tipo] if cod == "sol" else "config_%s_%s.json" % (cod, tipo)
    nuovo = not os.path.exists(f)
    cfg = copy.deepcopy(mod[tipo]) if nuovo else carica(f)
    prima = json.dumps(cfg, sort_keys=True)
    if nuovo:
        nome = NOMI_SOL[tipo] if cod == "sol" else "%s_%s" % (cod, tipo)
        cfg.update(nome=nome, coppia=mon["coppia"], filtri_appresi=[],
                   file_decisioni="decisioni_%s.csv" % (nome if cod != "sol" else tipo),
                   file_operazioni="operazioni_%s.csv" % (nome if cod != "sol" else tipo),
                   file_log="agente_%s.log" % (nome if cod != "sol" else tipo))
        cfg["notizie"] = notizie_moneta(mod[tipo]["notizie"], mon)
    cfg["moneta_codice"], cfg["agente_tipo"] = cod, tipo
    cfg["file_cache"] = "cache_notizie_%s.json" % cod
    if json.dumps(cfg, sort_keys=True) != prima:
        salva(f, cfg)
    return f, cfg, nuovo


def ultimo_controllo(file_dec):
    """Data dell'ultimo controllo (None se mai fatto o se l'ultima riga e' ancora dello storico)."""
    if not os.path.exists(file_dec):
        return None
    with open(file_dec, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 4000))
        righe = fh.read().decode("utf-8", "ignore").strip().splitlines()
    try:
        if "[storico]" in righe[-1]:
            return None
        return datetime.fromisoformat(righe[-1].split(";")[0])
    except (IndexError, ValueError):
        return None


def migra_cache_sol():
    """La memoria notizie di SOL diventa una sola: unisce le due vecchie (lento + veloce)."""
    nuovo = "cache_notizie_sol.json"
    if os.path.exists(nuovo):
        return
    unita = {}
    for vecchio in ("cache_notizie.json", "cache_notizie_veloce.json", "cache_notizie_intermedio.json"):
        if os.path.exists(vecchio):
            try:
                unita.update(carica(vecchio))
            except ValueError:
                pass
    if unita:
        salva(nuovo, unita)


def rss_in_comune():
    """Ogni feed si scarica una volta per giro, anche se lo usano piu' monete."""
    originale, memoria = nz.leggi_rss, {}

    def leggi(url, timeout=10):
        if url not in memoria:
            try:
                memoria[url] = (originale(url, timeout), None)
            except Exception as e:
                memoria[url] = (None, e)
        voci, err = memoria[url]
        if err:
            raise err
        return [dict(v) for v in voci]
    nz.leggi_rss = leggi


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tutti", action="store_true")
    a = p.parse_args()
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%d/%m %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if os.path.exists("STOP"):
        sys.exit("C'e' un file STOP: giro annullato.")
    conf = carica("monete.json")
    tz = ZoneInfo("Europe/Rome")
    mod = modelli()
    migra_cache_sol()
    rss_in_comune()
    elenco = []
    for mon in [m for m in conf["monete"] if m.get("attiva")]:
        agenti = [prepara(mon, tipo, mod) for tipo in conf["agenti"]]
        # passato degli agenti nuovi (SOL veloce e lento hanno gia' la loro storia vera)
        for f, cfg, nuovo in agenti:
            if not os.path.exists(cfg["file_decisioni"]) and conf.get("storico_dal"):
                try:
                    storico.ricostruisci(cfg, conf["storico_dal"], tz, log.info)
                except Exception as e:
                    log.error("storico %s non riuscito: %s", cfg["nome"], e)
        adesso = datetime.now(tz)
        dovuti = []
        for f, cfg, _ in agenti:
            elenco.append({"id": cfg["nome"], "moneta": mon["codice"], "coppia": cfg["coppia"],
                           "tipo": cfg["agente_tipo"], "nome": "%s %s" % (ETICHETTE[cfg["agente_tipo"]][0], mon["codice"].upper()),
                           "tag": ETICHETTE[cfg["agente_tipo"]][1], "dec": cfg["file_decisioni"],
                           "op": cfg["file_operazioni"], "ogni": cfg.get("intervallo_minuti", 60), "config": f})
            ult = ultimo_controllo(cfg["file_decisioni"])
            if a.tutti or ult is None or (adesso - ult).total_seconds() >= (cfg.get("intervallo_minuti", 60) - 3) * 60:
                dovuti.append(cfg)
        if not dovuti:
            continue
        # notizie: una raccolta e una valutazione per moneta, valide per tutti i suoi agenti
        base = copy.deepcopy(dovuti[0])
        base["notizie"]["finestra_ore"] = max(c["notizie"]["finestra_ore"] for c in dovuti)
        try:
            voci, fonti = nz.raccogli(base, log.info)
            log.info("[%s] Fonti: %s", mon["codice"].upper(), ", ".join("%s=%s" % (k, v) for k, v in fonti.items()))
            voci, nuove, da_claude = nz.valuta(voci, base, nz.Cache(base["file_cache"]), log.info)
            notizie = (voci, nuove, da_claude)
        except Exception as e:
            log.error("[%s] notizie non disponibili: %s", mon["codice"].upper(), e)
            notizie = ([], 0, 0)
        mercato = A.Mercato(mon["coppia"])
        for cfg in dovuti:
            fh = logging.FileHandler(cfg["file_log"], encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
            try:
                stato = A.Stato("paper", cfg["nome"])
                if stato.d["bloccato"]:
                    log.warning("[%s] bloccato: %s", cfg["nome"], stato.d["bloccato"])
                    continue
                log.info("=== %s (%s) ===", cfg["nome"], cfg["coppia"])
                A.Agente(cfg, A.BrokerSimulato(mercato, cfg["commissione_taker"]), mercato, stato).controllo(notizie)
            except Exception as e:
                log.exception("[%s] errore nel controllo: %s", cfg["nome"], e)
            finally:
                log.removeHandler(fh)
                fh.close()
    salva("agenti.json", elenco)


if __name__ == "__main__":
    main()
