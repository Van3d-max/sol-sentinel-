"""Test del lato SHORT (scommessa sulla discesa) con i futures, contro il solo long sullo spot.

Rigioca le regole dei nostri agenti sui prezzi veri di Coinbase, in due periodi:
  - dal 2 novembre 2026 (il periodo del nostro test, quasi tutto in discesa)
  - l'ultimo anno (salite, discese e periodi piatti: il test onesto)

Per ogni moneta e agente confronta:
  LONG spot     come oggi: solo acquisti, commissioni 0,6% per lato
  SHORT futures entra quando il trend scende, stop sopra, obiettivo sotto, esce se il trend risale
  LONG+SHORT    tutte e due le direzioni sui futures

Futures SENZA leva (1x), commissione prudente 0,10% per lato e costo di finanziamento 0,03% al giorno
(i futures perpetui pagano o incassano un "funding" orario: qui lo contiamo sempre come costo, per prudenza).
Obiettivo minimo: spot +2,2%, futures 1,2% (copre i costi e circa 1 EUR su 100).
Freno: dopo 5 operazioni a -8 EUR o 7 giorni a -12 EUR, 48 ore di pausa.

Uso:  python test_short.py           (scarica i prezzi da Coinbase, 3-5 minuti)
      python test_short.py --prova   (prezzi finti, solo per vedere che gira)
"""
import math
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
URL = "https://api.coinbase.com/api/v3/brokerage/market/products/%s/candles"
MONETE = ["SOL-EUR", "ETH-EUR", "BTC-EUR", "XRP-EUR", "SHIB-EUR"]
INIZIO_TEST = datetime(2026, 11, 2, tzinfo=TZ)
PUNTATA = 100.0
SPOT = {"fee": 0.006, "funding_giorno": 0.0, "obiettivo": 2.2}
FUT = {"fee": 0.001, "funding_giorno": 0.0003, "obiettivo": 1.2}
AGENTI = {  # stesse regole degli agenti veri
    "veloce":     {"gran": "FIFTEEN_MINUTE", "sec": 900,   "giorni": 180, "mv": 9,  "ml": 21, "sl": 2.0, "tp": 3.0,  "max_g": 4, "pausa": 1,  "sup": False},
    "intermedio": {"gran": "ONE_HOUR",       "sec": 3600,  "giorni": 365, "mv": 20, "ml": 50, "sl": 2.0, "tp": 4.0,  "max_g": 2, "pausa": 4,  "sup": True},
    "lento":      {"gran": "ONE_DAY",        "sec": 86400, "giorni": 400, "mv": 10, "ml": 30, "sl": 3.0, "tp": None, "max_g": 1, "pausa": 12, "sup": False},
}
FRENO = {"n": 5, "max": 8.0, "sett": 12.0, "pausa_ore": 48}


# ------------------------------------------------------------------ dati
def scarica(coppia, gran, sec, giorni):
    import requests
    fine = int(time.time()) // sec * sec
    t, out = fine - giorni * 86400, {}
    while t < fine:
        t2 = min(t + 300 * sec, fine)
        for k in range(5):
            try:
                r = requests.get(URL % coppia, timeout=20, params={"start": t, "end": t2, "granularity": gran},
                                 headers={"User-Agent": "sol-sentinel-test"})
                r.raise_for_status()
                for c in r.json().get("candles", []):
                    out[int(c["start"])] = (float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))
                break
            except Exception as e:
                if k == 4:
                    raise SystemExit("Coinbase non risponde (%s): %s" % (coppia, e))
                time.sleep(2 * (k + 1))
        t = t2
        time.sleep(0.1)
    ora = time.time()
    return [(k,) + out[k] for k in sorted(out) if k + sec <= ora]


def finte(coppia, gran, sec, giorni):
    random.seed(hash((coppia, sec)) % 999)
    n = giorni * 86400 // sec
    p, out, t0 = 100.0, [], int(time.time()) // sec * sec - n * sec
    for i in range(n):
        o = p
        p *= math.exp(random.gauss(0, 0.004 * math.sqrt(sec / 900)))
        out.append((t0 + i * sec, o, max(o, p) * 1.002, min(o, p) * 0.998, p))
    return out


def medie(c, n):
    out, s = [None] * len(c), 0.0
    for i in range(len(c)):
        s += c[i][4]
        if i >= n:
            s -= c[i - n][4]
        if i >= n - 1:
            out[i] = s / n
    return out


def atr(c, n=14):
    out, a = [None] * len(c), None
    for i in range(len(c)):
        tr = c[i][2] - c[i][3] if i == 0 else max(c[i][2] - c[i][3], abs(c[i][2] - c[i - 1][4]), abs(c[i][3] - c[i - 1][4]))
        if i < n:
            a = (a or 0) + tr / n
            if i == n - 1:
                out[i] = a
        else:
            a = (a * (n - 1) + tr) / n
            out[i] = a
    return out


def trend_giornaliero(d):
    mv, ml = medie(d, 10), medie(d, 30)
    return [(d[i][0] + 86400, None if mv[i] is None or ml[i] is None else mv[i] > ml[i]) for i in range(len(d))]


# ------------------------------------------------------------------ simulazione
def frenato(ops, adesso):
    if not ops or adesso - ops[-1]["chiusa"] > timedelta(hours=FRENO["pausa_ore"]):
        return False
    if len(ops) >= FRENO["n"] and sum(o["netto"] for o in ops[-FRENO["n"]:]) <= -FRENO["max"]:
        return True
    return sum(o["netto"] for o in ops if adesso - o["chiusa"] <= timedelta(days=7)) <= -FRENO["sett"]


def simula(c, P, costi, direzioni, sup, dal):
    mv, ml, a_ = medie(c, P["mv"]), medie(c, P["ml"]), atr(c)
    ops, pos, giorno, ingressi, ultima = [], None, None, 0, 0
    j = 0
    for i, (t, o, h, l, cl) in enumerate(c):
        fine = t + P["sec"]
        while sup and j + 1 < len(sup) and sup[j + 1][0] <= fine:
            j += 1
        if mv[i] is None or ml[i] is None or a_[i] is None:
            continue
        adesso = datetime.fromtimestamp(fine, timezone.utc).astimezone(TZ)
        if adesso < dal:
            continue
        if adesso.date() != giorno:
            giorno, ingressi = adesso.date(), 0
        su = mv[i] > ml[i]
        if pos:
            d = pos["dir"]
            uscita = motivo = None
            if (d > 0 and l <= pos["sl"]) or (d < 0 and h >= pos["sl"]):
                uscita, motivo = (min(pos["sl"], o) if d > 0 else max(pos["sl"], o)), "stop"
            elif pos["tp"] and ((d > 0 and h >= pos["tp"]) or (d < 0 and l <= pos["tp"])):
                uscita, motivo = pos["tp"], "obiettivo"
            elif (d > 0 and not su) or (d < 0 and su):
                uscita, motivo = cl, "trend girato"
            if uscita:
                giorni = (fine - pos["t"]) / 86400
                lordo = PUNTATA * d * (uscita / pos["entrata"] - 1)
                costo = PUNTATA * costi["fee"] * (1 + uscita / pos["entrata"]) + PUNTATA * costi["funding_giorno"] * giorni
                ops.append({"chiusa": adesso, "netto": lordo - costo, "dir": d, "motivo": motivo})
                ultima, pos = fine, None
            continue
        if ingressi >= P["max_g"] or fine - ultima < P["pausa"] * 3600 or frenato(ops, adesso):
            continue
        g = sup[j][1] if sup else None
        for d in direzioni:
            if (d > 0 and not su) or (d < 0 and su):
                continue
            if P["sup"] and g is not None and ((d > 0 and not g) or (d < 0 and g)):
                continue
            a = a_[i]
            if P["tp"] and P["tp"] * a / cl * 100 < costi["obiettivo"]:
                continue
            pos = {"dir": d, "t": fine, "entrata": cl, "sl": cl - d * P["sl"] * a,
                   "tp": cl + d * P["tp"] * a if P["tp"] else None}
            ingressi += 1
            break
    return ops


def riassumi(ops):
    if not ops:
        return "0 | — | —"
    tot = sum(o["netto"] for o in ops)
    v = sum(1 for o in ops if o["netto"] > 0)
    return "%d | %.0f%% | **%+.2f €**" % (len(ops), 100 * v / len(ops), tot)


def main():
    prova = "--prova" in sys.argv
    get = finte if prova else scarica
    un_anno = datetime.now(TZ) - timedelta(days=365)
    L = ["# Test short con i futures%s" % (" — PREZZI FINTI" if prova else ""), "",
         "Futures senza leva: 0,10% per lato + 0,03% al giorno di finanziamento. Spot: 0,6% per lato. 100 € a operazione.",
         "Agente veloce: ultimi 180 giorni (candele da 15 minuti). Intermedio e lento: ultimo anno.", ""]
    totali = {}
    for coppia in MONETE:
        print("Scarico %s..." % coppia, flush=True)
        d = get(coppia, "ONE_DAY", 86400, 450)
        sup = trend_giornaliero(d)
        bh_nov = next((x[4] for x in d if datetime.fromtimestamp(x[0], TZ) >= INIZIO_TEST), d[-1][4])
        bh_anno = next((x[4] for x in d if datetime.fromtimestamp(x[0], TZ) >= un_anno), d[0][4])
        L += ["## %s" % coppia, "",
              "Compra e tieni: dal 2 novembre **%+.1f%%**, nell'ultimo anno **%+.1f%%**." % (
                  (d[-1][4] / bh_nov - 1) * 100, (d[-1][4] / bh_anno - 1) * 100), "",
              "| Agente | Periodo | Long spot: op | vinte | € | Short futures: op | vinte | € | Long+short futures: op | vinte | € |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for nome, P in AGENTI.items():
            c = d if P["sec"] == 86400 else get(coppia, P["gran"], P["sec"], P["giorni"] + 3)
            for periodo, dal in (("dal 2 nov", INIZIO_TEST), ("tutto", un_anno if P["giorni"] >= 365 else
                                                               datetime.now(TZ) - timedelta(days=P["giorni"]))):
                ris = {}
                for tipo, costi, dirs in (("long", SPOT, (1,)), ("short", FUT, (-1,)), ("entrambi", FUT, (1, -1))):
                    ops = simula(c, P, costi, dirs, sup, dal)
                    ris[tipo] = ops
                    if periodo == "tutto":
                        totali.setdefault(tipo, 0.0)
                        totali[tipo] += sum(o["netto"] for o in ops)
                L.append("| %s | %s | %s | %s | %s |" % (nome, "dal 2 novembre" if periodo == "dal 2 nov" else
                                                        ("ultimi 180 giorni" if P["giorni"] < 365 else "ultimo anno"),
                                                        riassumi(ris["long"]), riassumi(ris["short"]), riassumi(ris["entrambi"])))
        L.append("")
    L += ["## Totale su tutte le monete e gli agenti (periodo lungo)", "",
          "| LONG spot | SHORT futures | LONG+SHORT futures |", "|---|---|---|",
          "| **%+.2f €** | **%+.2f €** | **%+.2f €** |" % (totali.get("long", 0), totali.get("short", 0), totali.get("entrambi", 0)), ""]
    testo = "\n".join(L) + "\n"
    with open("test_short.md", "w", encoding="utf-8") as f:
        f.write(testo)
    print(testo)


if __name__ == "__main__":
    main()
