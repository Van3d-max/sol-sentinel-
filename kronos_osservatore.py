"""Kronos in modalita' OSSERVATORE per sol-sentinel.

Solo test: NON legge e NON modifica configurazioni, stato o decisioni degli agenti.
Scrive soltanto kronos_previsioni.csv:
  - a ogni giro fa una previsione per le prossime ORIZZONTE candele orarie di SOL-EUR
    (prezzi pubblici Coinbase, nessuna chiave);
  - quando una vecchia previsione arriva a scadenza, scrive il prezzo reale e se la
    direzione era giusta, cosi' l'HTML puo' mostrare la percentuale di previsioni azzeccate.

Modello: Kronos (github.com/shiyu-coder/Kronos), pesi da Hugging Face (NeoQuasar).
APPRENDIMENTO AUTONOMO (separato dagli agenti: nessun agente legge Kronos):
  - a ogni giro prova piu' impostazioni ("varianti": modello + ore di storia);
  - ogni variante impara il proprio errore medio recente e salva anche una previsione "corretta";
  - il "campione" e' la variante che, sulle previsioni gia' verificate, sbaglia meno di "resta tutto uguale";
  - Kronos diventa "credibile" solo se il campione supera un esame severo (vedi CREDIBILE).
    Solo allora si decidera' se farlo usare agli agenti.

Uso:
  KRONOS_DIR=/percorso/Kronos python kronos_osservatore.py
"""
import argparse
import math
import csv
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

CARTELLA = os.path.dirname(os.path.abspath(__file__))
os.chdir(CARTELLA)
API = "https://api.coinbase.com/api/v3/brokerage/market/products/{}/candles"
FILE = "kronos_previsioni.csv"
CAMPI = ["data", "scadenza", "modello", "scenari", "prezzo", "previsto_medio", "variazione_pct",
         "p10", "p90", "prob_rialzo", "prob_volatilita", "direzione",
         "prezzo_reale", "variazione_reale_pct", "esito",
         "contesto", "corretta_pct", "campione", "credibile"]
VARIANTI = [("small", 72), ("small", 48), ("small", 168), ("mini", 336)]
FINESTRA = 360      # ultime previsioni verificate per variante (circa 2 mesi) su cui si giudica
MIN_CAMPIONE = 20   # sotto questa soglia una variante non puo' diventare campione
# esame di credibilita': previsioni indipendenti (distanti almeno 24 ore), direzione giusta, poca fortuna,
# errore medio sotto "resta uguale", probabilita' di rialzo ben calibrata
CREDIBILE = {"indipendenti": 30, "giuste": 0.58, "p": 0.05, "brier": 0.24}
ORA = 3600


def candele(coppia, quante):
    """Ultime candele orarie CHIUSE (max 350 per richiesta Coinbase)."""
    adesso = int(time.time())
    fine = adesso // ORA * ORA
    for tentativo in range(4):
        try:
            r = requests.get(API.format(coppia), timeout=20, params={
                "start": fine - quante * ORA, "end": fine,
                "granularity": "ONE_HOUR", "limit": quante})
            r.raise_for_status()
            righe = r.json().get("candles", [])
            break
        except requests.RequestException:
            if tentativo == 3:
                raise
            time.sleep(3)
    df = pd.DataFrame(righe)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["start"] = df["start"].astype(int)
    df = df[df["start"] + ORA <= adesso].sort_values("start").reset_index(drop=True)
    df["timestamps"] = pd.to_datetime(df["start"], unit="s")
    return df


def carica_modello(nome):
    sys.path.insert(0, os.environ.get("KRONOS_DIR", os.path.join(CARTELLA, "kronos_lib")))
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    tok = "NeoQuasar/Kronos-Tokenizer-2k" if nome == "mini" else "NeoQuasar/Kronos-Tokenizer-base"
    tokenizer = KronosTokenizer.from_pretrained(tok)
    model = Kronos.from_pretrained(f"NeoQuasar/Kronos-{nome}")
    model.eval()
    ctx = 2048 if nome == "mini" else 512
    return KronosPredictor(model, tokenizer, device="cpu", max_context=ctx)


def prevedi(pred, df, orizzonte, scenari):
    """N scenari indipendenti in un solo batch: ogni copia della serie = un percorso."""
    x = df[["open", "high", "low", "close", "volume"]]
    xt = df["timestamps"]
    yt = pd.Series([xt.iloc[-1] + pd.Timedelta(hours=i + 1) for i in range(orizzonte)])
    out = pred.predict_batch([x] * scenari, [xt] * scenari, [yt] * scenari,
                             pred_len=orizzonte, T=1.0, top_p=0.9, sample_count=1, verbose=False)
    return np.array([o["close"].values for o in out])


def statistiche(percorsi, ultimo, storico_close, orizzonte):
    finali = percorsi[:, -1]
    rend = np.diff(np.log(np.column_stack([np.full(len(percorsi), ultimo), percorsi])), axis=1)
    vol_storica = np.diff(np.log(storico_close[-orizzonte * 4:])).std()
    prob_su = float((finali > ultimo).mean())
    return {
        "previsto_medio": float(finali.mean()),
        "variazione_pct": float((finali.mean() / ultimo - 1) * 100),
        "p10": float(np.percentile(finali, 10)),
        "p90": float(np.percentile(finali, 90)),
        "prob_rialzo": prob_su,
        "prob_volatilita": float((rend.std(axis=1) > vol_storica).mean()),
        "direzione": "su" if prob_su > 0.5 else "giu" if prob_su < 0.5 else "neutra",
    }


def leggi():
    if not os.path.exists(FILE):
        return []
    with open(FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def scrivi(righe):
    tmp = FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPI, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(righe)
    os.replace(tmp, FILE)


def verifica_scadute(righe, df):
    """Chiude le previsioni scadute col close della candela delle ore 'scadenza' (UTC)."""
    chiusure = {int(t.timestamp()): c for t, c in zip(df["timestamps"], df["close"])}
    fatte = 0
    for r in righe:
        if r.get("esito"):
            continue
        scad = int(datetime.fromisoformat(r["scadenza"]).timestamp())
        reale = chiusure.get(scad - ORA)  # candela che CHIUDE alla scadenza
        if reale is None:
            continue
        p0 = float(r["prezzo"])
        r["prezzo_reale"] = "%.4f" % reale
        r["variazione_reale_pct"] = "%+.2f" % ((reale / p0 - 1) * 100)
        mosso = "su" if reale > p0 else "giu" if reale < p0 else "piatto"
        r["esito"] = "neutra" if r["direzione"] == "neutra" or mosso == "piatto" else \
            ("giusta" if r["direzione"] == mosso else "sbagliata")
        fatte += 1
    return fatte


def chiave(r):
    """Nome della variante di una riga ('small/72h'); le righe vecchie non hanno 'contesto'."""
    if not r.get("contesto"):
        r["contesto"] = "336" if r["data"] < "2026-11-14T17" else "72"
    return "%s/%sh" % (r["modello"].replace("Kronos-", ""), r["contesto"])


def p_fortuna(k, n):
    """Probabilita' di indovinare almeno k direzioni su n tirando una moneta."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0


def valuta(righe):
    """Pagella di ogni variante (grezza e corretta) sulle previsioni gia' verificate."""
    gruppi = {}
    for r in righe:
        if r.get("esito") and r.get("variazione_reale_pct"):
            gruppi.setdefault(chiave(r), []).append(r)
    out = {}
    for k, g in gruppi.items():
        g = g[-FINESTRA:]
        for corretta in (False, True):
            if corretta and not all(r.get("corretta_pct") for r in g):
                continue
            prev = [float(r["corretta_pct" if corretta else "variazione_pct"]) for r in g]
            reale = [float(r["variazione_reale_pct"]) for r in g]
            if corretta:
                su = [p > 0 for p in prev]
            else:
                su = [r["direzione"] == "su" if r["direzione"] != "neutra" else None for r in g]
            esiti = [(s == (x > 0)) for s, x in zip(su, reale) if s is not None and x != 0]
            # previsioni indipendenti: una ogni 24 ore almeno (quelle vicine guardano lo stesso movimento)
            ind, ultima = [], None
            for r, s, x in zip(g, su, reale):
                t = datetime.fromisoformat(r["data"])
                if s is not None and x != 0 and (ultima is None or (t - ultima).total_seconds() >= 86400):
                    ind.append(s == (x > 0))
                    ultima = t
            out[k + (" corretta" if corretta else "")] = {
                "n": len(g), "giuste": sum(esiti) / len(esiti) if esiti else 0.0,
                "indipendenti": len(ind), "p": p_fortuna(sum(ind), len(ind)),
                "errore": sum(abs(p - x) for p, x in zip(prev, reale)) / len(g),
                "errore_fermo": sum(abs(x) for x in reale) / len(g),
                "brier": sum((float(r["prob_rialzo"]) - (x > 0)) ** 2 for r, x in zip(g, reale)) / len(g),
            }
    return out


def campione_e_credibilita(pagelle):
    cand = {k: v for k, v in pagelle.items() if v["n"] >= MIN_CAMPIONE}
    if not cand:
        return None, False
    k = min(cand, key=lambda x: (cand[x]["errore"] / max(cand[x]["errore_fermo"], 1e-9), -cand[x]["giuste"]))
    v, c = cand[k], CREDIBILE
    ok = (v["indipendenti"] >= c["indipendenti"] and v["giuste"] >= c["giuste"] and v["p"] < c["p"]
          and v["errore"] < v["errore_fermo"] and v["brier"] < c["brier"])
    return k, ok


def sbilanciamento(righe, k):
    """Errore medio delle ultime 30 previsioni verificate della variante (es. +3 = prevede sempre 3% troppo alto)."""
    g = [r for r in righe if r.get("esito") and r.get("variazione_reale_pct") and chiave(r) == k][-30:]
    if len(g) < 10:
        return 0.0
    return sum(float(r["variazione_pct"]) - float(r["variazione_reale_pct"]) for r in g) / len(g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coppia", default="SOL-EUR")
    ap.add_argument("--modello", default="small", choices=["mini", "small", "base"],
                    help="ignorato: ora si provano tutte le VARIANTI")
    ap.add_argument("--orizzonte", type=int, default=24, help="candele orarie da prevedere")
    ap.add_argument("--scenari", type=int, default=20)
    ap.add_argument("--contesto", type=int, default=72, help="ignorato: ora si provano tutte le VARIANTI")
    ap.add_argument("--solo-verifica", action="store_true")
    a = ap.parse_args()

    df = candele(a.coppia, 350)                       # sempre 350 ore: servono per verificare le vecchie previsioni
    righe = leggi()
    n = verifica_scadute(righe, df)
    print(f"previsioni verificate ora: {n}")

    pagelle = valuta(righe)
    campione, credibile = campione_e_credibilita(pagelle)
    print("Campione: %s | credibile: %s" % (campione or "nessuno (servono piu' verifiche)", "SI" if credibile else "no"))
    for k, v in sorted(pagelle.items()):
        print("  %-20s %3d verificate | giuste %3.0f%% | indipendenti %2d (fortuna p=%.2f) | errore %.2f%% vs fermo %.2f%%"
              % (k, v["n"], v["giuste"] * 100, v["indipendenti"], v["p"], v["errore"], v["errore_fermo"]))

    if not a.solo_verifica:
        roma = ZoneInfo("Europe/Rome")
        base = datetime.fromtimestamp(int(df["start"].iloc[-1]) + ORA, timezone.utc)
        modelli = {}
        for nome, ore in VARIANTI:
            storia = df.tail(min(ore, 350)).reset_index(drop=True)
            k = "%s/%dh" % (nome, ore)
            if len(storia) < 0.9 * min(ore, 350):
                print("%s: candele insufficienti (%d)" % (k, len(storia)))
                continue
            try:
                if nome not in modelli:
                    modelli[nome] = carica_modello(nome)
                t0 = time.time()
                percorsi = prevedi(modelli[nome], storia, a.orizzonte, a.scenari)
            except Exception as e:                       # una variante rotta non ferma le altre
                print("%s: errore %s" % (k, e))
                continue
            ultimo = float(storia["close"].iloc[-1])
            s = statistiche(percorsi, ultimo, df["close"].values, a.orizzonte)
            bias = sbilanciamento(righe, k)
            corr = s["variazione_pct"] - bias
            e_camp = campione in (k, k + " corretta")
            righe.append({
                "data": base.astimezone(roma).isoformat(timespec="seconds"),
                "scadenza": datetime.fromtimestamp(base.timestamp() + a.orizzonte * ORA, timezone.utc)
                            .astimezone(roma).isoformat(timespec="seconds"),
                "modello": f"Kronos-{nome}", "scenari": a.scenari, "prezzo": "%.4f" % ultimo,
                "previsto_medio": "%.4f" % s["previsto_medio"], "variazione_pct": "%+.2f" % s["variazione_pct"],
                "p10": "%.4f" % s["p10"], "p90": "%.4f" % s["p90"],
                "prob_rialzo": "%.2f" % s["prob_rialzo"], "prob_volatilita": "%.2f" % s["prob_volatilita"],
                "direzione": s["direzione"], "prezzo_reale": "", "variazione_reale_pct": "", "esito": "",
                "contesto": ore, "corretta_pct": "%+.2f" % corr,
                "campione": ("corretta" if campione.endswith("corretta") else "si") if e_camp else "",
                "credibile": "si" if e_camp and credibile else "",
            })
            print(f"{k}: {ultimo:.2f} -> {s['previsto_medio']:.2f} ({s['variazione_pct']:+.2f}%, "
                  f"corretta {corr:+.2f}%) | rialzo {s['prob_rialzo'] * 100:.0f}% | {time.time() - t0:.0f}s")
    scrivi(righe)


if __name__ == "__main__":
    main()
