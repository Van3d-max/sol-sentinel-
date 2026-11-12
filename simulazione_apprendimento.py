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

GIORNI = int(os.environ.get("GIORNI", "180"))
TZ = ZoneInfo("Europe/Rome")
URL = "https://api.coinbase.com/api/v3/brokerage/market/products/SOL-EUR/candles"
SEC = 900
PUNTATA, FEE = 100.0, 0.006
MV, ML, ATR_N, SL_ATR, TP_ATR = 9, 21, 14, 2.0, 3.0
MAX_GIORNO, PAUSA, PERDITA_GIORNO = 4, 3600, 10.0
OBIETTIVO_MIN = 2.2
FRENO = dict(ap.FRENO_DEFAULT)
AUTO = dict(rv.AUTO_DEFAULT)


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


def indicatori(c):
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


def simula(c, mv, ml, atr, obiettivo=False, freno=False, impara=False):
    ops, pos, filtri, appresi, quarantena = [], None, [], [], {}
    giorno, ingressi, pnl_g, ultima_uscita = None, 0, 0.0, 0
    prossima_revisione = None
    for i in range(ML + ATR_N, len(c)):
        t, o, h, l, cl = c[i]
        ora = datetime.fromtimestamp(t + SEC, timezone.utc).astimezone(TZ)
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
            elif h >= pos["tp"]:
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
                ultima_uscita, pos = t + SEC, None
            continue
        if not (mv[i] > ml[i]) or ingressi >= MAX_GIORNO or t + SEC - ultima_uscita < PAUSA or pnl_g <= -PERDITA_GIORNO:
            continue
        a = atr[i]
        ctx = {"ora": ora.hour, "giorno": ora.weekday(), "notizie": None,
               "distanza_media_pct": (cl / ml[i] - 1) * 100, "forza_trend_pct": (mv[i] / ml[i] - 1) * 100,
               "atr_pct": a / cl * 100, "obiettivo_pct": TP_ATR * a / cl * 100, "stop_pct": SL_ATR * a / cl * 100}
        if obiettivo and ctx["obiettivo_pct"] < OBIETTIVO_MIN:
            continue
        if freno and blocco_freno(ops, ora):
            continue
        if impara and any(ap.condizione(f, ctx) for f in filtri):
            continue
        q = PUNTATA * (1 - FEE) / cl
        pos = {"ora": ora, "q": q, "sl": cl - SL_ATR * a, "tp": cl + TP_ATR * a, "ctx": ctx}
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
    c = finte(GIORNI * 96) if prova else scarica()
    mv, ml, atr = indicatori(c)
    inizio = datetime.fromtimestamp(c[0][0], TZ)
    fine = datetime.fromtimestamp(c[-1][0], TZ)
    bh = c[-1][4] / c[ML + ATR_N][4] - 1
    versioni = [("A · regole vecchie", {}), ("B · + obiettivo minimo 2,2%", {"obiettivo": True}),
                ("C · + freno automatico", {"obiettivo": True, "freno": True}),
                ("D · + apprendimento settimanale", {"obiettivo": True, "freno": True, "impara": True})]
    L = ["# Simulazione agente veloce SOL-EUR%s" % (" — DATI FINTI" if prova else ""), "",
         "Dal %s al %s (%d candele da 15 minuti). Compra e tieni nello stesso periodo: **%+.1f%%** (su 100 € = %+.2f €)."
         % (inizio.strftime("%d/%m/%Y"), fine.strftime("%d/%m/%Y"), len(c), bh * 100, bh * 100), "",
         "| Versione | Operazioni | Vinte | Risultato | Senza commissioni | Commissioni | Calo massimo |",
         "|---|---|---|---|---|---|---|"]
    risultati = {}
    for nome, opz in versioni:
        ops, appresi = simula(c, mv, ml, atr, **opz)
        risultati[nome] = (ops, appresi)
        L.append(riga(nome, ops))
    L += ["", "**Mese per mese (risultato in €):**", "", "| Mese | " + " | ".join(n.split(" ·")[0] for n, _ in versioni) + " |",
          "|---|" + "---|" * len(versioni)]
    mesi = sorted({o["chiusa"].strftime("%Y-%m") for ops, _ in risultati.values() for o in ops})
    for m in mesi:
        L.append("| %s | " % m + " | ".join("%+.2f" % sum(o["netto"] for o in risultati[n][0] if o["chiusa"].strftime("%Y-%m") == m)
                                            for n, _ in versioni) + " |")
    appresi = risultati[versioni[-1][0]][1]
    L += ["", "**Filtri imparati dalla versione D:**", ""]
    L += [("- %s: ✅ applicato %s, %s (nei dati fino a quel giorno: %+.2f €)" % (d.strftime("%d/%m/%Y"), f["id"], ap.descrivi(f), e["guadagno_eur"]))
          if e else ("- %s: ❌ tolto %s (aveva peggiorato la media)" % (d.strftime("%d/%m/%Y"), f["id"]))
          for d, f, e in appresi] or ["- nessun filtro ha superato l'esame"]
    testo = "\n".join(L) + "\n"
    with open("simulazione_apprendimento.md", "w", encoding="utf-8") as f:
        f.write(testo)
    print(testo)


if __name__ == "__main__":
    main()
