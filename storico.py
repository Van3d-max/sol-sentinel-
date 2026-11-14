"""Ricostruisce il passato di un agente nuovo, dal giorno d'inizio del progetto.

Rigioca le stesse regole dell'agente sulle candele vere di Coinbase:
trend, filtro giornaliero (intermedio), obiettivo minimo, freno, stop loss, take profit, uscita a fine trend.
Le notizie storiche non esistono: il passato si gioca con notizie neutre (punteggio 0).
Scrive i registri decisioni/operazioni (motivo che inizia con [storico], modalita' "storico")
e lo stato, cosi' l'agente riparte da dove lo storico e' arrivato e l'apprendimento ha subito dei dati.
"""
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import agente as A
import apprendimento as ap
from strategia import sma, atr

API = "https://api.coinbase.com/api/v3/brokerage/market/products/%s/candles"


def candele(coppia, gran, sec, dal_ts):
    """Candele chiuse dal timestamp indicato a oggi, a pagine da 300."""
    fine = int(time.time()) // sec * sec
    out, t = {}, int(dal_ts) // sec * sec
    while t < fine:
        t2 = min(t + 300 * sec, fine)
        for k in range(5):
            try:
                r = requests.get(API % coppia, timeout=20, params={"start": t, "end": t2, "granularity": gran})
                r.raise_for_status()
                for c in r.json().get("candles", []):
                    out[int(c["start"])] = c
                break
            except requests.RequestException:
                if k == 4:
                    raise
                time.sleep(2 * (k + 1))
        t = t2
        time.sleep(0.12)
    ora = time.time()
    return [{"t": k, "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
             "close": float(c["close"])} for k, c in sorted(out.items()) if k + sec <= ora]


def ricostruisci(cfg, dal, tz, log=print, scarica=None):
    scarica = scarica or candele
    tr, r = cfg["trend"], cfg["regole"]
    sec, gran = tr.get("secondi", 86400), tr.get("candela", "ONE_DAY")
    dal_ts = datetime.fromisoformat(dal).replace(tzinfo=tz).timestamp()
    margine = (tr["media_lenta"] + tr["atr_periodo"] + 5) * sec
    c = scarica(cfg["coppia"], gran, sec, dal_ts - margine)
    if len(c) < tr["media_lenta"] + tr["atr_periodo"]:
        log("storico %s: candele insufficienti (%d)" % (cfg["nome"], len(c)))
        return 0
    ch = [x["close"] for x in c]
    mv_l, ml_l, a_l = sma(ch, tr["media_veloce"]), sma(ch, tr["media_lenta"]), atr(c, tr["atr_periodo"])
    sup_l = None
    if cfg.get("trend_superiore"):
        ts = cfg["trend_superiore"]
        d = scarica(cfg["coppia"], ts.get("candela", "ONE_DAY"), ts.get("secondi", 86400),
                    dal_ts - (ts["media_lenta"] + 5) * ts.get("secondi", 86400))
        dch = [x["close"] for x in d]
        dmv, dml = sma(dch, ts["media_veloce"]), sma(dch, ts["media_lenta"])
        sup_l = [(x["t"] + ts.get("secondi", 86400), bool(dmv[i] and dml[i] and dmv[i] > dml[i])) for i, x in enumerate(d)]
    fee, puntata = cfg["commissione_taker"], float(cfg["puntata_eur"])
    pos, giorno, ingressi, pnl_g, ultima_uscita, n_op = None, None, 0, 0.0, 0.0, 0
    nf = cfg["file_decisioni"]
    for i, x in enumerate(c):
        fine = x["t"] + sec
        if fine < dal_ts or None in (mv_l[i], ml_l[i], a_l[i]):
            continue
        adesso = datetime.fromtimestamp(fine, timezone.utc).astimezone(tz)
        if adesso.date().isoformat() != giorno:
            giorno, ingressi, pnl_g = adesso.date().isoformat(), 0, 0.0
        prezzo, mv, ml, a = x["close"], mv_l[i], ml_l[i], a_l[i]
        trend = mv > ml
        ctx = ap.contesto(cfg, prezzo, mv, ml, a, {"punteggio": 0.0}, adesso)
        azione, motivo = "attendi", ""
        if pos:
            uscita = None
            if x["low"] <= pos["sl"]:
                uscita, motivo = min(pos["sl"], x["open"]), "stop loss"
            elif pos.get("tp") and x["high"] >= pos["tp"]:
                uscita, motivo = max(pos["tp"], x["open"]), "take profit"
            elif not trend:
                uscita, motivo = prezzo, "trend finito"
            if uscita:
                lordo = pos["quantita"] * uscita
                comm = lordo * fee
                netto = lordo - comm - pos["costo"]
                A.scrivi_csv(cfg["file_operazioni"], A.CAMPI_OP, dict({
                    "data_apertura": pos["apertura"], "data_chiusura": adesso.isoformat(timespec="seconds"),
                    "modalita": "storico", "coppia": cfg["coppia"], "quantita": "%.8f" % pos["quantita"],
                    "prezzo_entrata": A.fp(pos["entrata"]), "prezzo_uscita": A.fp(uscita),
                    "acquisto_eur": "%.4f" % pos["costo"], "vendita_eur": "%.4f" % (lordo - comm),
                    "commissioni_eur": "%.4f" % (pos["comm"] + comm), "risultato_netto_eur": "%.4f" % netto,
                    "motivo_uscita": motivo, "notizie_ingresso": "+0.000", "dibattito": "storico",
                    "id_ordini": ",".join(pos["ordini"]),
                    "durata_ore": "%.2f" % ((fine - datetime.fromisoformat(pos["apertura"]).timestamp()) / 3600)},
                    **ap.contesto_csv(pos["ctx"])))
                pnl_g += netto
                ultima_uscita, pos, n_op = fine, None, n_op + 1
                azione = "venduto"
            else:
                azione, motivo = "tieni", "stop %s" % A.fp(pos["sl"])
        else:
            if not trend:
                motivo = "trend non rialzista"
            elif sup_l is not None and not next((v for t_, v in reversed(sup_l) if t_ <= fine), False):
                motivo = "trend giornaliero non rialzista (filtro superiore)"
            elif ingressi >= r["max_ingressi_giorno"]:
                motivo = "gia' entrato oggi"
            elif fine - ultima_uscita < r["pausa_dopo_uscita_ore"] * 3600:
                motivo = "pausa dopo l'ultima uscita"
            elif pnl_g <= -abs(r["max_perdita_giornaliera_eur"]):
                motivo = "perdita massima di oggi raggiunta"
            else:
                motivo = ap.freno(cfg, adesso) or ap.obiettivo_troppo_piccolo(cfg, ctx) or ap.filtro_appreso(cfg, ctx) or ""
                if not motivo:
                    q = puntata * (1 - fee) / prezzo
                    sl = prezzo - tr["stop_atr"] * a
                    pos = {"apertura": adesso.isoformat(timespec="seconds"), "quantita": q, "entrata": prezzo,
                           "costo": puntata, "comm": puntata * fee, "sl": sl,
                           "sl_limite": sl * (1 - cfg.get("sl_offset_limite_pct", 1.0) / 100), "id_stop": None,
                           "tp": prezzo + tr["take_profit_atr"] * a if tr.get("take_profit_atr") else None,
                           "ordini": ["storico-" + uuid.uuid4().hex[:8]], "notizie": 0.0, "dibattito": "storico",
                           "ctx": ctx}
                    ingressi += 1
                    azione, motivo = "compra", "trend su (notizie storiche non disponibili)"
        A.scrivi_csv(nf, A.CAMPI_DEC, {
            "data": adesso.isoformat(timespec="seconds"), "prezzo": A.fp(prezzo), "media_veloce": A.fp(mv),
            "media_lenta": A.fp(ml), "trend": "su" if trend else "giu", "punteggio_notizie": "+0.000",
            "n_notizie": 0, "nuove": 0, "valutate_claude": 0, "shock": 0, "posizione": "si" if pos else "no",
            "azione": azione, "motivo": "[storico] " + motivo if motivo else "[storico]",
            "notizia_principale": "", "atr_pct": ctx["atr_pct"]})
    st = A.Stato("paper", cfg["nome"])
    oggi = datetime.now(tz).date().isoformat()
    st.d.update(pos=pos, giorno=giorno, ingressi_oggi=ingressi if giorno == oggi else 0,
                pnl_oggi=pnl_g if giorno == oggi else 0.0, ultima_uscita=ultima_uscita, bloccato=None)
    st.salva()
    log("storico %s: %d operazioni dal %s%s" % (cfg["nome"], n_op, dal, ", posizione ancora aperta" if pos else ""))
    return n_op
