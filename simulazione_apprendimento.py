"""Simulazione: l'agente veloce con e senza le nuove regole, su mesi di prezzi veri di SOL-EUR.

Rigioca candela per candela (15 minuti) le regole dell'agente veloce:
  entra se la media 9 > media 21, stop a 2 ATR, take profit a 3 ATR, esce se il trend si gira,
  massimo 4 entrate al giorno, 1 ora di pausa dopo un'uscita, stop alla perdita di 10 EUR al giorno,
  100 EUR a operazione, commissioni 0,6% per lato.
Le notizie storiche non esistono: si assume che il filtro notizie lasci passare (come succede quasi sempre).

Confronta 4 versioni:
  A  regole vecchie
  B  + obiettivo minimo +2,2% (copre commissioni e 1 EUR di guadagno)
  C  + B + freno automatico (5 operazioni a -8 EUR o settimana a -12 EUR -> 48 ore di pausa)
  D  + C + apprendimento settimanale: ogni domenica la revisione cerca filtri sulle operazioni
     GIA' fatte e applica la proposta migliore che passa l'esame (come se tu la approvassi).
     I filtri valgono solo dalla settimana dopo: niente "sbirciate" nel futuro.

Uso:  python simulazione_apprendimento.py          (scarica circa 6 mesi da Coinbase)
      GIORNI=90 python simulazione_apprendimento.py
      python simulazione_apprendimento.py --prova  (dati finti, solo per controllare che giri)
"""
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import apprendimento as ap
import revisione as rv

GIORNI = int(os.environ.get("GIORNI", "240"))
TZ = ZoneInfo("Europe/Rome")
URL = "https://api.coinbase.com/api/v3/brokerage/market/products/SOL-EUR/candles"
SEC = 900                      # le candele scaricate sono da 15 minuti; le altre si ricostruiscono
PUNTATA, FEE = 100.0, 0.006
ATR_N, PERDITA_GIORNO = 14, 10.0
OBIETTIVO_MIN = 2.2
FRENO = dict(ap.FRENO_DEFAULT)
AUTO = dict(rv.AUTO_DEFAULT)
# i tre agenti, con le stesse regole dei config
AGENTI = {
    "veloce":     {"sec": 900,   "mv": 9,  "ml": 21, "sl": 2.0, "tp": 3.0,  "max_g": 4, "pausa": 3600,      "sup": False},
    "intermedio": {"sec": 3600,  "mv": 20, "ml": 50, "sl": 2.0, "tp": 4.0,  "max_g": 2, "pausa": 4 * 3600,  "sup": True},
    "lento":      {"sec": 86400, "mv": 10, "ml": 30, "sl": 3.0, "tp": None, "max_g": 1, "pausa": 12 * 3600, "sup": False},
}


def scarica():
    import requests
    fine = int(time.time()) // SEC * SEC
    t, out = fine - GIORNI * 86400, {}
    while t < fine:
        t2 = min(t + 300 * SEC, fine)
        for k in range(5):
            try:
                r = requests.get(URL, timeout=20, params={"start": t, "end": t2, "granularity": "FIFTEEN_MINUTE"},
                                 headers={"User-Agent": "sol-sentinel-sim"})
                r.raise_for_status()
                for c in r.json().get("candles", []):
                    out[int(c["start"])] = (float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]))
                break
            except Exception as e:
                if k == 4:
                    raise SystemExit("Coinbase non risponde: %s" % e)
                time.sleep(2 * (k + 1))
        t = t2
        print("\r  scaricate %d candele" % len(out), end="", flush=True)
        time.sleep(0.12)
    print()
    return [(k,) + out[k] for k in sorted(out) if k + SEC <= time.time()]


def finte(n):
    random.seed(5)
    p, out, t0 = 150.0, [], int(time.time()) // SEC * SEC - n * SEC
    for i in range(n):
        o = p
        p *= math.exp(random.gauss(0, 0.004))
        out.append((t0 + i * SEC, o, max(o, p) * 1.001, min(o, p) * 0.999, p))
    return out


def raggruppa(c, sec):
    gruppi = {}
    for t, o, h, l, cl in c:
        gruppi.setdefault(t // sec * sec, []).append((t, o, h, l, cl))
    return [(k, g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4])
            for k, g in sorted(gruppi.items()) if len(g) >= 0.9 * sec / SEC]


def trend_giornaliero(c15):
    """Per ogni giorno: True se (alla chiusura) la media 10 giorni era sopra la media 30."""
    d = raggruppa(c15, 86400)
    mv, ml, _ = indicatori(d, 10, 30)
    return {d[i][0] + 86400: (mv[i] is not None and ml[i] is not None and mv[i] > ml[i]) for i in range(len(d))}


def indicatori(c, MV, ML):
    chiusure = [x[4] for x in c]
    mv, ml, atr = [None] * len(c), [None] * len(c), [None] * len(c)
    for i in range(len(c)):
        if i >= MV - 1:
            mv[i] = sum(chiusure[i - MV + 1:i + 1]) / MV
        if i >= ML - 1:
            ml[i] = sum(chiusure[i - ML + 1:i + 1]) / ML
    trs = [c[0][2] - c[0][3]] + [max(c[i][2] - c[i][3], abs(c[i][2] - c[i - 1][4]), abs(c[i][3] - c[i - 1][4]))
                                 for i in range(1, len(c))]
    a = None
    for i, tr in enumerate(trs):
        if i == ATR_N - 1:
            a = sum(trs[:ATR_N]) / ATR_N
        elif i >= ATR_N:
            a = (a * (ATR_N - 1) + tr) / ATR_N
        atr[i] = a
    return mv, ml, atr


def simula(c, P, obiettivo=False, freno=False, impara=False, sup=None):
    mv, ml, atr = indicatori(c, P["mv"], P["ml"])
    sec = P["sec"]
    ops, pos, filtri, appresi, quarantena = [], None, [], [], {}
    giorno, ingressi, pnl_g, ultima_uscita = None, 0, 0.0, 0
    prossima_revisione = None
    for i in range(P["ml"] + ATR_N, len(c)):
        t, o, h, l, cl = c[i]
        fine_candela = t + sec
        ora = datetime.fromtimestamp(fine_candela, timezone.utc).astimezone(TZ)
        if ora.date() != giorno:
            giorno, ingressi, pnl_g = ora.date(), 0, 0.0
        # revisione settimanale (domenica sera): impara solo dalle operazioni gia' chiuse
        if impara:
            if prossima_revisione is None:
                prossima_revisione = ora + timedelta(days=(6 - ora.weekday()) % 7)
                prossima_revisione = prossima_revisione.replace(hour=19, minute=0)
            if ora >= prossima_revisione:
                prossima_revisione += timedelta(days=7)
                media = lambda g: sum(o["netto"] for o in g) / len(g)
                # toglie i filtri che hanno peggiorato la media (dopo 8 operazioni), con 4 settimane di quarantena
                for f in list(filtri):
                    prima = [o for o in ops if o["aperta"] < f["dal"]]
                    dopo = [o for o in ops if o["aperta"] >= f["dal"]]
                    if prima and len(dopo) >= AUTO["verifica_dopo_operazioni"] and media(dopo) < media(prima):
                        filtri.remove(f)
                        quarantena[f["campo"]] = ora + timedelta(days=AUTO["quarantena_giorni"])
                        appresi.append((ora, f, None))
                # applica al massimo 1 filtro forte a settimana
                if len(filtri) < AUTO["max_filtri"] and len(ops) >= AUTO["min_operazioni"]:
                    esaminati = sorted(((f, rv.esame(f, ops)) for f in rv.candidati(ops)), key=lambda x: -x[1].get("guadagno_eur", 0))
                    for f, e in esaminati:
                        forte = (e["passa"] and e["guadagno_eur"] >= AUTO["guadagno_min_eur"]
                                 and e["prima_meta_eur"] >= AUTO["meta_min_eur"] and e["seconda_meta_eur"] >= AUTO["meta_min_eur"])
                        if forte and not any(g["campo"] == f["campo"] for g in filtri) \
                                and quarantena.get(f["campo"], ora) <= ora:
                            f = dict(f, id="F%d" % (1 + sum(1 for x in appresi if x[2] is not None)), dal=ora)
                            filtri.append(f)
                            appresi.append((ora, f, e))
                            break
        if pos:                                      # gestione: stop e obiettivo dentro la candela
            uscita = motivo = None
            if l <= pos["sl"]:
                uscita, motivo = min(pos["sl"], o), "stop loss"
            elif pos["tp"] and h >= pos["tp"]:
                uscita, motivo = max(pos["tp"], o), "take profit"
            elif mv[i] < ml[i]:
                uscita, motivo = cl, "trend finito"
            if uscita:
                netto = pos["q"] * uscita * (1 - FEE) - PUNTATA
                ops.append({"aperta": pos["ora"], "chiusa": ora, "data_apertura": pos["ora"].isoformat(),
                            "netto": netto, "commissioni_eur": PUNTATA * FEE + pos["q"] * uscita * FEE,
                            "motivo_uscita": motivo, "ctx": pos["ctx"],
                            "durata_ore": (ora - pos["ora"]).total_seconds() / 3600})
                pnl_g += netto
                ultima_uscita, pos = fine_candela, None
            continue
        if not (mv[i] > ml[i]) or ingressi >= P["max_g"] or fine_candela - ultima_uscita < P["pausa"] \
                or pnl_g <= -PERDITA_GIORNO:
            continue
        if sup is not None:                          # filtro del trend giornaliero (ultimo giorno chiuso)
            chiuso = fine_candela // 86400 * 86400
            if not sup.get(chiuso, False):
                continue
        a = atr[i]
        ctx = {"ora": ora.hour, "giorno": ora.weekday(), "notizie": None,
               "distanza_media_pct": (cl / ml[i] - 1) * 100, "forza_trend_pct": (mv[i] / ml[i] - 1) * 100,
               "atr_pct": a / cl * 100, "obiettivo_pct": P["tp"] * a / cl * 100 if P["tp"] else None,
               "stop_pct": P["sl"] * a / cl * 100}
        if obiettivo and ctx["obiettivo_pct"] is not None and ctx["obiettivo_pct"] < OBIETTIVO_MIN:
            continue
        if freno and blocco_freno(ops, ora):
            continue
        if impara and any(ap.condizione(f, ctx) for f in filtri):
            continue
        q = PUNTATA * (1 - FEE) / cl
        pos = {"ora": ora, "q": q, "sl": cl - P["sl"] * a, "tp": cl + P["tp"] * a if P["tp"] else None, "ctx": ctx}
        ingressi += 1
    return ops, appresi


def blocco_freno(ops, adesso):
    if not ops:
        return False
    ultima = ops[-1]["chiusa"]
    if adesso - ultima > timedelta(hours=FRENO["pausa_ore"]):
        return False
    n = FRENO["ultime_operazioni"]
    if len(ops) >= n and sum(o["netto"] for o in ops[-n:]) <= -FRENO["perdita_max_eur"]:
        return True
    sett = sum(o["netto"] for o in ops if adesso - o["chiusa"] <= timedelta(days=7))
    return sett <= -FRENO["perdita_7_giorni_eur"]


def riga(nome, ops):
    if not ops:
        return "| %s | 0 | — | — | — | — | — |" % nome
    s = rv.riassunto(ops)
    return "| %s | %d | %.0f%% | **%+.2f €** | %+.2f € | %.2f € | %.2f € |" % (
        nome, s["n"], s["vinte_pct"], s["totale"], s["senza_commissioni"], s["commissioni"], s["calo_massimo"])


def main():
    prova = "--prova" in sys.argv
    print("Scarico %d giorni di candele da 15 minuti di SOL-EUR..." % GIORNI)
    c15 = finte(GIORNI * 96) if prova else scarica()
    sup = trend_giornaliero(c15)
    inizio = datetime.fromtimestamp(c15[0][0], TZ)
    fine = datetime.fromtimestamp(c15[-1][0], TZ)
    avvio = 60 * 86400                               # i primi 60 giorni servono a calcolare le medie del lento
    i0 = next(i for i, x in enumerate(c15) if x[0] >= c15[0][0] + avvio)
    bh = c15[-1][4] / c15[i0][4] - 1
    L = ["# Simulazione dei tre agenti su SOL-EUR%s" % (" — DATI FINTI" if prova else ""), "",
         "Dal %s al %s. Compra e tieni dal %s: **%+.1f%%** (su 100 € = %+.2f €). 100 € a operazione, commissioni 0,6%% per lato. "
         "Le notizie storiche non esistono: si assume che il filtro notizie lasci passare."
         % (inizio.strftime("%d/%m/%Y"), fine.strftime("%d/%m/%Y"), datetime.fromtimestamp(c15[i0][0], TZ).strftime("%d/%m/%Y"),
            bh * 100, bh * 100), ""]
    versioni = [("A · regole vecchie", {}), ("B · + obiettivo minimo 2,2%", {"obiettivo": True}),
                ("C · + freno automatico", {"obiettivo": True, "freno": True}),
                ("D · + apprendimento automatico", {"obiettivo": True, "freno": True, "impara": True})]
    finali = {}
    for nome_ag, P in AGENTI.items():
        c = raggruppa(c15, P["sec"]) if P["sec"] != SEC else c15
        c = [x for x in c if x[0] >= c15[0][0] + avvio - (P["ml"] + ATR_N + 2) * P["sec"]]
        L += ["## Agente %s (candele da %s)" % (nome_ag, {900: "15 minuti", 3600: "1 ora", 86400: "1 giorno"}[P["sec"]]), "",
              "| Versione | Operazioni | Vinte | Risultato | Senza commissioni | Commissioni | Calo massimo |",
              "|---|---|---|---|---|---|---|"]
        vv = list(versioni)
        if P["sup"]:
            vv = [("A0 · senza filtro giornaliero", {"_nosup": True})] + vv
        for nome_v, opz in vv:
            opz = dict(opz)
            usa_sup = sup if P["sup"] and not opz.pop("_nosup", False) else None
            ops, appresi = simula(c, P, sup=usa_sup, **opz)
            L.append(riga(nome_v, ops))
            if nome_v.startswith("D"):
                finali[nome_ag] = ops
                if appresi:
                    L += [""] + [("- %s: ✅ imparato %s, %s" % (d.strftime("%d/%m/%Y"), f["id"], ap.descrivi(f))) if e
                                 else ("- %s: ❌ tolto %s" % (d.strftime("%d/%m/%Y"), f["id"])) for d, f, e in appresi]
        L.append("")
    L += ["## Versione D (tutte le regole nuove) mese per mese, in €", "", "| Mese | " + " | ".join(finali) + " | Insieme |",
          "|---|" + "---|" * (len(finali) + 1)]
    mesi = sorted({o["chiusa"].strftime("%Y-%m") for ops in finali.values() for o in ops})
    for m in mesi:
        val = [sum(o["netto"] for o in ops if o["chiusa"].strftime("%Y-%m") == m) for ops in finali.values()]
        L.append("| %s | " % m + " | ".join("%+.2f" % v for v in val) + " | **%+.2f** |" % sum(val))
    tot = [sum(o["netto"] for o in ops) for ops in finali.values()]
    L.append("| **Totale** | " + " | ".join("**%+.2f**" % v for v in tot) + " | **%+.2f** |" % sum(tot))
    testo = "\n".join(L) + "\n"
    with open("simulazione_apprendimento.md", "w", encoding="utf-8") as f:
        f.write(testo)
    print(testo)


if __name__ == "__main__":
    main()
