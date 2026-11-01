"""Indicatori e segnale "trend + ritracciamento".

Usato sia dal bot sia dal backtest, cosi' la logica e' identica nei due casi.
Le candele sono liste di dict {t, open, high, low, close, volume}, in ordine
cronologico (la piu' vecchia per prima) e SOLO chiuse.
"""


def sma(valori, n):
    out = [None] * len(valori)
    if n <= 0 or len(valori) < n:
        return out
    s = sum(valori[:n])
    out[n - 1] = s / n
    for i in range(n, len(valori)):
        s += valori[i] - valori[i - n]
        out[i] = s / n
    return out


def rsi(chiusure, n=14):
    """RSI di Wilder."""
    out = [None] * len(chiusure)
    if len(chiusure) <= n:
        return out
    guadagni = perdite = 0.0
    for i in range(1, n + 1):
        d = chiusure[i] - chiusure[i - 1]
        guadagni += max(d, 0)
        perdite += max(-d, 0)
    mg, mp = guadagni / n, perdite / n
    out[n] = 100.0 if mp == 0 else 100 - 100 / (1 + mg / mp)
    for i in range(n + 1, len(chiusure)):
        d = chiusure[i] - chiusure[i - 1]
        mg = (mg * (n - 1) + max(d, 0)) / n
        mp = (mp * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if mp == 0 else 100 - 100 / (1 + mg / mp)
    return out


def atr(candele, n=14):
    """ATR di Wilder."""
    out = [None] * len(candele)
    if len(candele) <= n:
        return out
    tr = [candele[0]["high"] - candele[0]["low"]]
    for i in range(1, len(candele)):
        h, l, pc = candele[i]["high"], candele[i]["low"], candele[i - 1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(candele)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def calcola_indicatori(candele, p):
    c = [x["close"] for x in candele]
    return {
        "close": c,
        "sma_lunga": sma(c, p["sma_lunga"]),
        "sma_veloce": sma(c, p["sma_veloce"]),
        "sma_media": sma(c, p["sma_media"]),
        "rsi": rsi(c, p["rsi_periodo"]),
        "atr": atr(candele, p["atr_periodo"]),
    }


def candele_minime(p):
    return max(p["sma_lunga"], p["sma_media"], p["rsi_periodo"] + 2, p["atr_periodo"] + 2) + 5


def segnale(ind, i, p, commissioni_andata_ritorno=0.0):
    """Valuta il segnale d'acquisto sulla candela chiusa i.

    Ritorna (compra: bool, dettagli: dict).
    """
    vals = [ind[k][i] for k in ("sma_lunga", "sma_veloce", "sma_media", "rsi", "atr")]
    if i < 1 or any(v is None for v in vals) or ind["rsi"][i - 1] is None:
        return False, {"motivo": "dati insufficienti"}
    close = ind["close"][i]
    sl, sv, sm, r, a = vals
    r_prec = ind["rsi"][i - 1]
    d = {"close": close, "sma200": sl, "sma20": sv, "sma50": sm,
         "rsi": r, "rsi_prec": r_prec, "atr": a}

    if not close > sl:
        d["motivo"] = "prezzo sotto SMA lunga"
        return False, d
    if not sv > sm:
        d["motivo"] = "SMA veloce non sopra SMA media"
        return False, d
    soglia = p["rsi_soglia"]
    if not (r_prec < soglia <= r):
        d["motivo"] = "nessun rimbalzo RSI"
        return False, d
    # Filtro opzionale: il take profit deve valere almeno X volte le commissioni.
    k = p.get("min_tp_su_commissioni", 0) or 0
    if k > 0 and commissioni_andata_ritorno > 0:
        if (p["tp_atr"] * a / close) < k * commissioni_andata_ritorno:
            d["motivo"] = "take profit troppo piccolo rispetto alle commissioni"
            return False, d
    d["motivo"] = "trend rialzista + rimbalzo RSI"
    return True, d


def livelli(prezzo_entrata, atr_val, p):
    return (prezzo_entrata - p["sl_atr"] * atr_val,
            prezzo_entrata + p["tp_atr"] * atr_val)
