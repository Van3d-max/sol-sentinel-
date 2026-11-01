"""Backtest della strategia sui dati storici di Coinbase.

I dati vengono divisi in due parti:
  - IN-SAMPLE (la prima, 70% di default): si provano piu' combinazioni di parametri
    e si sceglie la migliore;
  - OUT-OF-SAMPLE (il resto): si verifica la scelta su dati MAI usati per sceglierla.
Se va bene solo in-sample, e' quasi certamente un caso (overfitting).

Uso:
  python backtest.py                 scarica ~1 anno di candele orarie (le salva in dati/)
  python backtest.py --giorni 730    piu' storia
  python backtest.py --offline       usa solo i dati gia' scaricati
  python backtest.py --no-griglia    prova solo i parametri di config.json
"""
import argparse
import csv
import itertools
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import strategia as st

CARTELLA = os.path.dirname(os.path.abspath(__file__))
os.chdir(CARTELLA)
DATI = "dati"

GRIGLIA = {
    "rsi_soglia": [30, 35, 40, 45],
    "sl_atr": [1.0, 1.5, 2.0],
    "tp_atr": [2.0, 3.0, 4.0],
}


# ------------------------------------------------------------------ dati

def file_dati(coppia):
    return os.path.join(DATI, f"{coppia}_1h.csv")


def leggi_csv(coppia):
    out = []
    with open(file_dati(coppia), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"t": int(r["t"]), **{k: float(r[k]) for k in
                                             ("open", "high", "low", "close", "volume")}})
    return out


def scarica(coppia, giorni, cfg):
    from broker import DatiMercato, crea_client
    dati = DatiMercato(crea_client(cfg, serve_auth=False))
    ore = giorni * 24
    print(f"Scarico {coppia}: {ore} candele...", flush=True)
    candele = dati.candele(coppia, ore)
    os.makedirs(DATI, exist_ok=True)
    with open(file_dati(coppia), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(candele)
    time.sleep(0.3)
    return candele


# ------------------------------------------------------------------ simulazione

def simula(serie, p, cfg, t_inizio=None, t_fine=None):
    """serie: {coppia: (candele, indicatori)}. Simula solo entrate con t in [t_inizio, t_fine)."""
    tz = ZoneInfo(cfg.get("fuso_orario", "Europe/Rome"))
    fee = cfg["commissione_taker"]
    slip = cfg["sl_offset_limite_pct"] / 100
    puntata = cfg["puntata_eur"]
    fee_ar = 2 * fee

    # indice per tempo
    eventi = {}
    for coppia, (cand, _) in serie.items():
        for i, c in enumerate(cand):
            eventi.setdefault(c["t"], []).append((coppia, i))
    tempi = sorted(eventi)

    aperte, in_attesa, operazioni = {}, {}, []
    giorno, n_oggi, pnl_oggi = None, 0, 0.0

    def chiudi(coppia, prezzo, t, motivo):
        nonlocal pnl_oggi
        pos = aperte.pop(coppia)
        lordo = pos["q"] * prezzo
        netto = lordo * (1 - fee) - puntata
        pnl_oggi += netto
        operazioni.append({"coppia": coppia, "t_in": pos["t"], "t_out": t, "entrata": pos["e"],
                           "uscita": prezzo, "netto": netto, "motivo": motivo})

    for t in tempi:
        g = datetime.fromtimestamp(t, timezone.utc).astimezone(tz).date()
        if g != giorno:
            giorno, n_oggi, pnl_oggi = g, 0, 0.0
        dentro = (t_inizio is None or t >= t_inizio) and (t_fine is None or t < t_fine)

        for coppia, i in eventi[t]:
            cand, ind = serie[coppia]
            c = cand[i]
            # 1) entrata in attesa: si compra all'apertura di questa candela
            if coppia in in_attesa:
                a = in_attesa.pop(coppia)
                ok = (len(aperte) < cfg["max_posizioni_aperte"] and n_oggi < cfg["max_operazioni_giorno"]
                      and pnl_oggi > -abs(cfg["max_perdita_giornaliera_eur"]))
                if ok:
                    e = c["open"]
                    sl, tp = st.livelli(e, a, p)
                    aperte[coppia] = {"q": puntata * (1 - fee) / e, "e": e, "sl": sl, "tp": tp,
                                      "lim": sl * (1 - slip), "t": t}
                    n_oggi += 1
            # 2) uscite (se SL e TP nella stessa candela si assume lo SL: prudente)
            if coppia in aperte:
                pos = aperte[coppia]
                if c["low"] <= pos["sl"]:
                    px = min(pos["sl"], c["open"])
                    if px >= pos["lim"]:
                        chiudi(coppia, px, t, "stop loss")
                    else:
                        # gap sotto il limite: lo stop-limit non si esegue; si esce a fine candela
                        chiudi(coppia, c["close"], t, "stop saltato (gap)")
                elif c["high"] >= pos["tp"]:
                    chiudi(coppia, max(pos["tp"], c["open"]), t, "take profit")
            # 3) segnale sulla chiusura -> entrata alla prossima candela
            if dentro and coppia not in aperte:
                compra, d = st.segnale(ind, i, p, fee_ar)
                if compra:
                    in_attesa[coppia] = d["atr"]

    # chiudi ciò che resta all'ultimo prezzo
    for coppia in list(aperte):
        cand, _ = serie[coppia]
        chiudi(coppia, cand[-1]["close"], cand[-1]["t"], "fine dati")
    return operazioni


def statistiche(op):
    n = len(op)
    if n == 0:
        return {"n": 0, "netto": 0.0, "win": 0.0, "pf": 0.0, "dd": 0.0, "medio": 0.0}
    vinte = [o["netto"] for o in op if o["netto"] > 0]
    perse = [o["netto"] for o in op if o["netto"] <= 0]
    eq = picco = dd = 0.0
    for o in sorted(op, key=lambda x: x["t_out"]):
        eq += o["netto"]
        picco = max(picco, eq)
        dd = max(dd, picco - eq)
    return {"n": n, "netto": sum(o["netto"] for o in op), "win": len(vinte) / n,
            "pf": (sum(vinte) / -sum(perse)) if perse and sum(perse) < 0 else float("inf"),
            "dd": dd, "medio": sum(o["netto"] for o in op) / n}


def riga(nome, s):
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{nome:<34} op={s['n']:>4}  netto={s['netto']:+8.3f} €  medio={s['medio']:+.4f} €  "
            f"vinte={s['win']*100:5.1f}%  PF={pf:>5}  maxDD={s['dd']:.3f} €")


def riepilogo_settimane(op, t_min, t_max, cfg):
    """Come sarebbe andata ogni settimana con il bot acceso H24 (parametri di config.json)."""
    tz = ZoneInfo(cfg.get("fuso_orario", "Europe/Rome"))
    chiave = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(tz).isocalendar()[:2]
    # tutte le settimane complete del periodo, anche quelle senza operazioni
    settimane = {}
    t = t_min + 7 * 86400  # salta la prima (indicatori ancora in avvio)
    while t < t_max - 7 * 86400:
        settimane[chiave(t)] = [0.0, 0]
        t += 86400
    for o in op:
        k = chiave(o["t_out"])
        if k in settimane:
            settimane[k][0] += o["netto"]
            settimane[k][1] += 1
    if not settimane:
        return
    valori = sorted(v[0] for v in settimane.values())
    n = len(valori)
    pos = sum(1 for v in valori if v > 0)
    zero = sum(1 for v in settimane.values() if v[1] == 0)
    print(f"\n== Una settimana H24 con i parametri di config.json ({n} settimane reali simulate) ==")
    print(f"Operazioni a settimana (media): {sum(v[1] for v in settimane.values()) / n:.1f}"
          f"   | settimane senza operazioni: {zero}")
    print(f"Risultato medio:  {sum(valori) / n:+.3f} €     mediano: {valori[n // 2]:+.3f} €")
    print(f"Settimana migliore: {valori[-1]:+.3f} €   peggiore: {valori[0]:+.3f} €")
    print(f"80% delle settimane tra {valori[int(n * 0.1)]:+.3f} € e {valori[int(n * 0.9) - 1]:+.3f} €")
    print(f"Settimane in guadagno: {pos}/{n} ({pos / n * 100:.0f}%)")
    print("Ultime 8 settimane:")
    for (anno, sett), (netto, k) in sorted(settimane.items())[-8:]:
        print(f"  {anno}-S{sett:02d}: {k:>2} operazioni  {netto:+.3f} €")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--giorni", type=int, default=365)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--quota-in-sample", type=float, default=0.7)
    ap.add_argument("--no-griglia", action="store_true")
    ap.add_argument("--csv-operazioni", default="backtest_operazioni.csv")
    a = ap.parse_args()

    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    base = dict(cfg["strategia"])

    candele = {}
    for coppia in cfg["coppie"]:
        if a.offline or (os.path.exists(file_dati(coppia)) and
                         time.time() - os.path.getmtime(file_dati(coppia)) < 6 * 3600):
            candele[coppia] = leggi_csv(coppia)
        else:
            candele[coppia] = scarica(coppia, a.giorni, cfg)
        print(f"{coppia}: {len(candele[coppia])} candele")

    t_min = min(c[0]["t"] for c in candele.values())
    t_max = max(c[-1]["t"] for c in candele.values())
    t_split = t_min + int((t_max - t_min) * a.quota_in_sample)
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%d/%m/%Y")
    print(f"\nIn-sample:     {fmt(t_min)} -> {fmt(t_split)}")
    print(f"Out-of-sample: {fmt(t_split)} -> {fmt(t_max)}")
    print(f"Commissioni: {cfg['commissione_taker']*100:.2f}% per lato | puntata {cfg['puntata_eur']} €\n")

    def prepara(p):
        return {k: (v, st.calcola_indicatori(v, p)) for k, v in candele.items()}

    # B&H di riferimento sul periodo out-of-sample
    def buy_and_hold(t0, t1):
        rend = []
        for c in candele.values():
            sel = [x for x in c if t0 <= x["t"] < t1]
            if len(sel) > 1:
                rend.append(sel[-1]["close"] / sel[0]["open"] - 1)
        return sum(rend) / len(rend) if rend else 0.0

    print("== Parametri di config.json ==")
    s_cfg = prepara(base)
    st_in = statistiche(simula(s_cfg, base, cfg, None, t_split))
    op_out_cfg = simula(s_cfg, base, cfg, t_split, None)
    st_out = statistiche(op_out_cfg)
    print(riga("in-sample", st_in))
    print(riga("out-of-sample", st_out))
    riepilogo_settimane(simula(s_cfg, base, cfg, None, None), t_min, t_max, cfg)

    scelti, op_scelti = base, op_out_cfg
    if not a.no_griglia:
        combinazioni = [dict(zip(GRIGLIA, v)) for v in itertools.product(*GRIGLIA.values())]
        print(f"\n== Ricerca parametri su IN-SAMPLE ({len(combinazioni)} combinazioni) ==")
        cache, risultati = {}, []
        for comb in combinazioni:
            p = {**base, **comb}
            # gli indicatori dipendono solo dai periodi, non da soglie/moltiplicatori
            if "ind" not in cache:
                cache["ind"] = prepara(p)
            s = statistiche(simula(cache["ind"], p, cfg, None, t_split))
            risultati.append((comb, s))
        validi = [r for r in risultati if r[1]["n"] >= 15] or risultati
        validi.sort(key=lambda r: r[1]["netto"], reverse=True)
        for comb, s in validi[:5]:
            print(riga(f"rsi<{comb['rsi_soglia']} sl{comb['sl_atr']} tp{comb['tp_atr']}", s))
        migliore = validi[0][0]
        scelti = {**base, **migliore}
        op_scelti = simula(cache["ind"], scelti, cfg, t_split, None)
        print("\n== Verifica OUT-OF-SAMPLE dei parametri migliori ==")
        print(riga(f"rsi<{migliore['rsi_soglia']} sl{migliore['sl_atr']} tp{migliore['tp_atr']}",
                   statistiche(op_scelti)))

    bh = buy_and_hold(t_split, t_max + 1)
    print(f"\nRiferimento: comprare e tenere (media delle coppie) out-of-sample = {bh*100:+.1f}%")
    print(f"             con {cfg['puntata_eur']} € sarebbero {bh*cfg['puntata_eur']:+.3f} €")

    with open(a.csv_operazioni, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["coppia", "entrata_utc", "uscita_utc", "prezzo_entrata", "prezzo_uscita",
                    "netto_eur", "motivo"])
        for o in op_scelti:
            w.writerow([o["coppia"], datetime.fromtimestamp(o["t_in"], timezone.utc).isoformat(),
                        datetime.fromtimestamp(o["t_out"], timezone.utc).isoformat(),
                        f"{o['entrata']:.4f}", f"{o['uscita']:.4f}", f"{o['netto']:.4f}", o["motivo"]])
    print(f"\nOperazioni out-of-sample salvate in {a.csv_operazioni}")
    print("Nota: il passato non garantisce il futuro. Se l'out-of-sample e' negativo, non andare live.")


if __name__ == "__main__":
    main()
