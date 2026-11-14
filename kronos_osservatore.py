"""Kronos in modalita' OSSERVATORE per sol-sentinel.

Solo test: NON legge e NON modifica configurazioni, stato o decisioni degli agenti.
Scrive soltanto kronos_previsioni.csv:
  - a ogni giro fa una previsione per le prossime ORIZZONTE candele orarie di SOL-EUR
    (prezzi pubblici Coinbase, nessuna chiave);
  - quando una vecchia previsione arriva a scadenza, scrive il prezzo reale e se la
    direzione era giusta, cosi' l'HTML puo' mostrare la percentuale di previsioni azzeccate.

Modello: Kronos (github.com/shiyu-coder/Kronos), pesi da Hugging Face (NeoQuasar).
Uso:
  KRONOS_DIR=/percorso/Kronos python kronos_osservatore.py
"""
import argparse
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
         "prezzo_reale", "variazione_reale_pct", "esito"]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coppia", default="SOL-EUR")
    ap.add_argument("--modello", default="small", choices=["mini", "small", "base"])
    ap.add_argument("--orizzonte", type=int, default=24, help="candele orarie da prevedere")
    ap.add_argument("--scenari", type=int, default=20)
    ap.add_argument("--contesto", type=int, default=336, help="candele di storia (max 350)")
    ap.add_argument("--solo-verifica", action="store_true")
    a = ap.parse_args()

    df = candele(a.coppia, 350)                       # sempre 350 ore: servono per verificare le vecchie previsioni
    storia = df.tail(min(a.contesto, 350)).reset_index(drop=True)   # al modello solo le ultime 'contesto' ore
    righe = leggi()
    n = verifica_scadute(righe, df)
    print(f"previsioni verificate ora: {n}")

    if not a.solo_verifica:
        if len(storia) < 0.9 * min(a.contesto, 350):
            raise SystemExit("candele insufficienti: %d" % len(storia))
        pred = carica_modello(a.modello)
        t0 = time.time()
        percorsi = prevedi(pred, storia, a.orizzonte, a.scenari)
        ultimo = float(storia["close"].iloc[-1])
        s = statistiche(percorsi, ultimo, df["close"].values, a.orizzonte)
        roma = ZoneInfo("Europe/Rome")
        base = datetime.fromtimestamp(int(df["start"].iloc[-1]) + ORA, timezone.utc)
        righe.append({
            "data": base.astimezone(roma).isoformat(timespec="seconds"),
            "scadenza": datetime.fromtimestamp(base.timestamp() + a.orizzonte * ORA, timezone.utc)
                        .astimezone(roma).isoformat(timespec="seconds"),
            "modello": f"Kronos-{a.modello}", "scenari": a.scenari, "prezzo": "%.4f" % ultimo,
            "previsto_medio": "%.4f" % s["previsto_medio"], "variazione_pct": "%+.2f" % s["variazione_pct"],
            "p10": "%.4f" % s["p10"], "p90": "%.4f" % s["p90"],
            "prob_rialzo": "%.2f" % s["prob_rialzo"], "prob_volatilita": "%.2f" % s["prob_volatilita"],
            "direzione": s["direzione"], "prezzo_reale": "", "variazione_reale_pct": "", "esito": "",
        })
        print(f"{a.coppia} {ultimo:.2f} -> media {s['previsto_medio']:.2f} ({s['variazione_pct']:+.2f}%) "
              f"| rialzo {s['prob_rialzo'] * 100:.0f}% | {time.time() - t0:.0f}s")
    scrivi(righe)


if __name__ == "__main__":
    main()
