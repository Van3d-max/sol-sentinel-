"""Test di Kronos sullo storico di SOL-EUR: quale impostazione prevede davvero meglio?

Per ogni impostazione (modello + quante ore di storia gli diamo) rifà una previsione a 24 ore
ogni GIORNO degli ultimi ~60 giorni, usando SOLO i dati disponibili in quel momento, e la
confronta con il prezzo vero 24 ore dopo.

Misure:
  - direzione azzeccata (% di volte che "sale/scende" era giusto)
  - errore medio della variazione prevista, confrontato con chi dice sempre "resta uguale"
  - sbilanciamento medio (se prevede sempre rialzi o ribassi)
  - "ancoraggio": quanto la previsione segue solo la media della finestra (1,00 = la copia)
  - prova di trading: compra 100 EUR solo se prevede almeno +2,3% (sopra le commissioni)
    con probabilita' di rialzo >= 70%, rivende dopo 24 ore, commissioni 0,6% per lato.

Scrive kronos_backtest.md. Non tocca gli agenti.
Uso: KRONOS_DIR=/percorso/Kronos python kronos_backtest.py
"""
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from kronos_osservatore import carica_modello, prevedi, statistiche

API = "https://api.coinbase.com/api/v3/brokerage/market/products/{}/candles"
ORA = 3600
COPPIA = os.environ.get("COPPIA", "SOL-EUR")
GIORNI_TEST = int(os.environ.get("GIORNI_TEST", "60"))
SCENARI = int(os.environ.get("SCENARI", "10"))
ORIZZONTE = 24
COMM = 0.006
SOGLIA = 2.3          # % minima prevista per comprare (pari + ~1 EUR su 100)
PROB_MIN = 0.70
# (modello, ore di storia)
VARIANTI = [("small", 336), ("small", 168), ("small", 72), ("mini", 336)]
USCITA = "kronos_backtest.md"


def storico(coppia, giorni):
    fine = int(time.time()) // ORA * ORA
    t, righe = fine - giorni * 86400, {}
    while t < fine:
        t2 = min(t + 300 * ORA, fine)
        for tentativo in range(5):
            try:
                r = requests.get(API.format(coppia), timeout=20,
                                 params={"start": t, "end": t2, "granularity": "ONE_HOUR"})
                r.raise_for_status()
                for c in r.json().get("candles", []):
                    righe[int(c["start"])] = c
                break
            except requests.RequestException:
                if tentativo == 4:
                    raise
                time.sleep(3 * (tentativo + 1))
        t = t2
        time.sleep(0.2)
    df = pd.DataFrame(sorted(righe.values(), key=lambda c: int(c["start"])))
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["start"] = df["start"].astype(int)
    df = df[df["start"] + ORA <= time.time()].reset_index(drop=True)
    df["timestamps"] = pd.to_datetime(df["start"], unit="s")
    return df


def main():
    max_storia = max(v[1] for v in VARIANTI)
    df = storico(COPPIA, GIORNI_TEST + max_storia // 24 + 3)
    print(f"{len(df)} candele orarie di {COPPIA}")
    # punti di partenza: uno al giorno, alla stessa ora, lasciando 24 ore per verificare
    ultimo_possibile = len(df) - 1 - ORIZZONTE
    origini = list(range(ultimo_possibile, max_storia, -24))[:GIORNI_TEST][::-1]
    print(f"{len(origini)} previsioni per impostazione")

    modelli, risultati = {}, {}
    for nome, storia in VARIANTI:
        if nome not in modelli:
            modelli[nome] = carica_modello(nome)
        pred = modelli[nome]
        righe, t0 = [], time.time()
        for k, i in enumerate(origini):
            finestra = df.iloc[i - storia + 1:i + 1].reset_index(drop=True)
            ultimo = float(finestra["close"].iloc[-1])
            percorsi = prevedi(pred, finestra, ORIZZONTE, SCENARI)
            s = statistiche(percorsi, ultimo, finestra["close"].values, ORIZZONTE)
            reale = float(df["close"].iloc[i + ORIZZONTE])
            righe.append({
                "prevista": s["variazione_pct"], "reale": (reale / ultimo - 1) * 100,
                "prob": s["prob_rialzo"], "verso_media": (finestra["close"].mean() / ultimo - 1) * 100,
            })
            if k % 10 == 0:
                print(f"  {nome}/{storia}h: {k + 1}/{len(origini)} ({time.time() - t0:.0f}s)", flush=True)
        risultati[(nome, storia)] = pd.DataFrame(righe)
    scrivi(risultati, df)


def misura(r):
    giuste = ((r["prevista"] > 0) == (r["reale"] > 0)).mean() * 100
    err = (r["prevista"] - r["reale"]).abs().mean()
    err_zero = r["reale"].abs().mean()
    anc = np.corrcoef(r["prevista"], r["verso_media"])[0, 1] if r["prevista"].std() > 0 else float("nan")
    corr = np.corrcoef(r["prevista"], r["reale"])[0, 1] if r["prevista"].std() > 0 else float("nan")
    seg = r[(r["prevista"] >= SOGLIA) & (r["prob"] >= PROB_MIN)]
    netti = [100 * (1 - COMM) * (1 + x / 100) * (1 - COMM) - 100 for x in seg["reale"]]
    sempre = [100 * (1 - COMM) * (1 + x / 100) * (1 - COMM) - 100 for x in r["reale"]]
    return giuste, err, err_zero, r["prevista"].mean(), r["reale"].mean(), anc, corr, len(netti), sum(netti), sum(sempre)


def scrivi(risultati, df):
    ora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Kronos sullo storico di {COPPIA}", "",
         f"Generato {ora}. Una previsione a 24 ore al giorno per circa {GIORNI_TEST} giorni, {SCENARI} scenari ciascuna, "
         "usando solo i dati disponibili in quel momento.", "",
         "| Modello | Storia | Direzione giusta | Errore medio | Errore di \"resta uguale\" | Media prevista | Media reale | "
         "Ancoraggio alla media | Correlazione col reale | Operazioni (≥ +2,3%, prob ≥ 70%) | € segnali | € comprando sempre |",
         "|" + "---|" * 12]
    for (nome, storia), r in risultati.items():
        g, e, ez, mp, mr, anc, corr, n, eur, sempre = misura(r)
        L.append(f"| Kronos-{nome} | {storia} h | {g:.0f}% | {e:.2f}% | {ez:.2f}% | {mp:+.2f}% | {mr:+.2f}% | "
                 f"{anc:.2f} | {corr:+.2f} | {n} | {eur:+.2f} | {sempre:+.2f} |")
    L += ["", "Come leggerla:",
          "- **Errore medio** più basso di quello di \"resta uguale\" = il modello aggiunge informazione.",
          "- **Media prevista** molto diversa da quella reale = sbilanciamento sistematico.",
          "- **Ancoraggio** vicino a 1 = la previsione copia solo il ritorno alla media della finestra.",
          "- **Correlazione col reale** sopra +0,2 con molte previsioni = segnale interessante; intorno a 0 = rumore.",
          "- **€ segnali**: risultato con commissioni comprando solo sui segnali forti."]
    testo = "\n".join(L) + "\n"
    with open(USCITA, "w", encoding="utf-8") as f:
        f.write(testo)
    print(testo)


if __name__ == "__main__":
    main()
