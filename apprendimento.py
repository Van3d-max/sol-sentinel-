"""Apprendimento dagli errori: parti condivise tra gli agenti e la revisione settimanale.

  - contesto():   la "fotografia" del mercato al momento di una possibile entrata
                  (ora, giorno, notizie, distanza dalla media, forza del trend, volatilita', obiettivo)
  - freno():      pausa automatica dopo una serie di perdite (non cambia le regole, ferma solo le entrate)
  - filtro_appreso(): applica i filtri approvati dalla revisione ("non entrare quando ...")
  - leggi_operazioni(): legge il registro delle operazioni chiuse

Un filtro appreso e' un dizionario, per esempio:
  {"id": "F1", "campo": "ora", "op": "tra", "valore": [0, 6], "motivo": "di notte perdeva"}
op: "<", ">", "tra" (estremi inclusi), "fuori" (fuori dall'intervallo), "in" (lista di valori).
Se la condizione e' vera, l'agente NON entra.
"""
import csv
import os
import time
from datetime import datetime, timedelta

CAMPI_CONTESTO = ["ctx_ora", "ctx_giorno", "ctx_notizie", "ctx_distanza_media_pct",
                  "ctx_forza_trend_pct", "ctx_atr_pct", "ctx_obiettivo_pct", "ctx_stop_pct"]
NOMI_CAMPI = {
    "ora": "ora del giorno (0-23)", "giorno": "giorno della settimana (0=lunedi')",
    "notizie": "punteggio notizie", "distanza_media_pct": "distanza del prezzo dalla media lenta (%)",
    "forza_trend_pct": "distanza tra media veloce e lenta (%)", "atr_pct": "volatilita' ATR (% del prezzo)",
    "obiettivo_pct": "take profit previsto (%)", "stop_pct": "distanza dello stop (%)",
}
FRENO_DEFAULT = {"attivo": True, "ultime_operazioni": 5, "perdita_max_eur": 8.0,
                 "perdita_7_giorni_eur": 12.0, "pausa_ore": 48}


def _num(x, default=None):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return default


def contesto(cfg, prezzo, mv, ml, a, agg, adesso):
    """Fotografia del momento: tutto cio' che e' noto PRIMA di decidere."""
    tr = cfg["trend"]
    tp = tr.get("take_profit_atr")
    return {
        "ora": adesso.hour,
        "giorno": adesso.weekday(),
        "notizie": round(float(agg["punteggio"]), 3),
        "distanza_media_pct": round((prezzo / ml - 1) * 100, 3),
        "forza_trend_pct": round((mv / ml - 1) * 100, 3),
        "atr_pct": round(a / prezzo * 100, 3),
        "obiettivo_pct": round(tp * a / prezzo * 100, 3) if tp else None,
        "stop_pct": round(tr["stop_atr"] * a / prezzo * 100, 3),
    }


def contesto_csv(ctx):
    if not ctx:
        return {}
    return {"ctx_" + k: ("" if v is None else v) for k, v in ctx.items()}


def leggi_operazioni(file):
    if not os.path.exists(file):
        return []
    with open(file, newline="", encoding="utf-8") as f:
        righe = list(csv.DictReader(f, delimiter=";"))
    for r in righe:
        r["netto"] = _num(r.get("risultato_netto_eur"), 0.0)
        try:
            r["chiusa"] = datetime.fromisoformat(r["data_chiusura"])
            r["aperta"] = datetime.fromisoformat(r["data_apertura"])
        except (KeyError, ValueError):
            r["chiusa"] = r["aperta"] = None
    return [r for r in righe if r["chiusa"]]


def freno(cfg, adesso):
    """Restituisce un motivo (stringa) se le entrate vanno sospese, altrimenti None."""
    f = dict(FRENO_DEFAULT, **cfg.get("freno", {}))
    if not f["attivo"]:
        return None
    ops = leggi_operazioni(cfg["file_operazioni"])
    if not ops:
        return None
    ultima = max(r["chiusa"] for r in ops)
    if adesso - ultima > timedelta(hours=f["pausa_ore"]):
        return None                                   # pausa gia' scontata: si riparte
    n = int(f["ultime_operazioni"])
    serie = ops[-n:]
    tot = sum(r["netto"] for r in serie)
    if len(serie) >= n and tot <= -abs(f["perdita_max_eur"]):
        fine = ultima + timedelta(hours=f["pausa_ore"])
        return "freno: ultime %d operazioni %+.2f EUR, pausa fino al %s" % (n, tot, fine.strftime("%d/%m %H:%M"))
    sett = [r for r in ops if adesso - r["chiusa"] <= timedelta(days=7)]
    tot7 = sum(r["netto"] for r in sett)
    if tot7 <= -abs(f["perdita_7_giorni_eur"]):
        fine = ultima + timedelta(hours=f["pausa_ore"])
        return "freno: ultimi 7 giorni %+.2f EUR, pausa fino al %s" % (tot7, fine.strftime("%d/%m %H:%M"))
    return None


def condizione(filtro, ctx):
    """True se il contesto ricade nella condizione del filtro (cioe': da evitare)."""
    v = ctx.get(filtro["campo"])
    if v is None or v == "":
        return False
    v = float(v)
    op, x = filtro["op"], filtro["valore"]
    if op == "<":
        return v < float(x)
    if op == ">":
        return v > float(x)
    if op == "tra":
        return float(x[0]) <= v <= float(x[1])
    if op == "fuori":
        return not (float(x[0]) <= v <= float(x[1]))
    if op == "in":
        return v in [float(y) for y in x]
    return False


def descrivi(filtro):
    nome = NOMI_CAMPI.get(filtro["campo"], filtro["campo"])
    op, x = filtro["op"], filtro["valore"]
    testo = {"<": "%s sotto %s" % (nome, x), ">": "%s sopra %s" % (nome, x),
             "tra": "%s tra %s e %s" % (nome, x[0], x[1]) if isinstance(x, list) else "",
             "fuori": "%s fuori da %s-%s" % (nome, x[0], x[1]) if isinstance(x, list) else "",
             "in": "%s in %s" % (nome, x)}.get(op, str(filtro))
    return "non entrare con " + testo


def filtro_appreso(cfg, ctx):
    for f in cfg.get("filtri_appresi", []):
        if condizione(f, ctx):
            return "filtro appreso %s: %s" % (f.get("id", "?"), descrivi(f))
    return None


def obiettivo_troppo_piccolo(cfg, ctx):
    """Con il take profit, entra solo se l'obiettivo copre commissioni + un minimo di guadagno."""
    minimo = cfg.get("regole", {}).get("obiettivo_min_pct")
    if minimo is None or ctx.get("obiettivo_pct") is None:
        return None
    if ctx["obiettivo_pct"] < minimo:
        return "obiettivo +%.2f%% sotto il minimo +%.2f%% (commissioni)" % (ctx["obiettivo_pct"], minimo)
    return None
