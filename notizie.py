"""Agente notizie: raccoglie titoli su una criptovaluta, li valuta e calcola un punteggio.

- Fonti: feed RSS (CoinDesk, Cointelegraph, Decrypt...) e GDELT, scaricati in parallelo.
- Ogni titolo viene valutato UNA volta sola e salvato in cache (veloce ed economico).
- Valutazione: Claude (un'unica chiamata per tutti i titoli nuovi) oppure, senza chiave
  API o se la chiamata fallisce, un dizionario di parole chiave.
- Il punteggio finale pesa di piu' le notizie recenti e quelle piu' rilevanti.

L'AI qui NON decide operazioni: produce solo un numero da -1 (molto negativo) a +1.
"""
import hashlib
import html
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (agente-notizie)"}
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# ------------------------------------------------------------------ dizionario (versione 2)
LESSICO_VERSIONE = 2

# parola o frase -> peso. "*" alla fine = vale anche per le parole che iniziano cosi'.
POSITIVE = {
    "etf approval": 1.0, "approves": 0.9, "approved": 0.9, "approval": 0.7, "greenlight*": 0.8,
    "staking etf": 0.8, "spot etf": 0.6, "etf": 0.3, "inflow*": 0.6, "partnership": 0.6,
    "partners with": 0.6, "integrat*": 0.5, "adopt*": 0.6, "launches on": 0.5, "listing": 0.4,
    "lists": 0.4, "upgrade": 0.5, "firedancer": 0.5, "mainnet": 0.4, "treasury": 0.5,
    "accumulat*": 0.5, "buys": 0.4, "bought": 0.4, "institutional": 0.4, "record": 0.3,
    "all-time high": 0.4, "milestone": 0.3, "wins": 0.4, "settle*": 0.4, "dismiss*": 0.5,
    "launch*": 0.3, "tokeniz*": 0.35, "adds": 0.3, "expands": 0.3, "backs": 0.3, "invests": 0.4,
}
NEGATIVE = {
    "outage": 1.0, "halt*": 1.0, "goes down": 0.8, "went down": 0.8, "exploit*": 0.9,
    "hack": 0.8, "hacked": 0.9, "drained": 0.9, "rug pull": 0.8, "vulnerabilit*": 0.7,
    "bug": 0.4, "lawsuit": 0.6, "sues": 0.6, "sued": 0.6, "charges": 0.6, "fraud": 0.7,
    "delist*": 0.9, "ban": 0.7, "bans": 0.7, "banned": 0.7, "investigat*": 0.5,
    "outflow*": 0.5, "rejects": 0.7, "rejected": 0.7, "rejection": 0.7, "delay*": 0.4,
    "postpone*": 0.4, "congestion": 0.5, "unlock": 0.4, "sell pressure": 0.4,
}
# movimenti di prezzo gia' avvenuti: notizia "vecchia" per il mercato, peso basso
PREZZO = {
    "surge*": 0.25, "soar*": 0.25, "rall*": 0.25, "jump*": 0.2, "gain*": 0.15, "climb*": 0.15,
    "plunge*": -0.25, "crash*": -0.3, "tumbl*": -0.25, "drop*": -0.15, "fall*": -0.15,
    "slump*": -0.25, "dump*": -0.25, "sell-off": -0.25, "selloff": -0.25,
}
# la notizia parla d'altro: Solana e' solo citata
ALTRI_SOGGETTI = ["binance", "coinbase", "bybit", "kraken", "okx", "ftx", "bitcoin", "btc",
                  "ethereum", "eth", "xrp", "ripple", "tether", "usdt", "circle", "dogecoin",
                  "north korea", "lazarus", "hacker", "hackers", "scammer", "exploiter"]
RICICLAGGIO = ["launder*", "moves", "moved", "swaps", "swapped", "bridges", "bridged",
               "transfers", "transferred", "funnel*", "converts"]
RECUPERO = ["restored", "resumes", "resumed", "back online", "recover*", "denies", "denied",
            "no evidence", "unaffected", "not affected", "fixed", "patched", "returns"]
# problema di un'app o di un progetto SU Solana, non della rete Solana
ECOSISTEMA = ["protocol", "dex", "wallet", "app", "project", "memecoin", "meme coin", "token",
              "defi", "platform", "exchange", "bridge"]
SPECULATIVO = ["could", "may", "might", "prediction", "forecast", "will ", "can ", "should",
               "analyst says", "opinion", "price analysis", "here's why", "what's next"]


def _re(termini):
    parti = []
    for t in termini:
        stella = t.endswith("*")
        t = re.escape(t.rstrip("*"))
        parti.append(r"\b" + t + (r"\w*" if stella else r"\b"))
    return re.compile("|".join(parti), re.I)


_RE_POS = {k: _re([k]) for k in POSITIVE}
_RE_NEG = {k: _re([k]) for k in NEGATIVE}
_RE_PREZZO = {k: _re([k]) for k in PREZZO}
_RE_ALTRI = _re(ALTRI_SOGGETTI)
_RE_RICICLAGGIO = _re(RICICLAGGIO)
_RE_RECUPERO = _re(RECUPERO)
_RE_ECOSISTEMA = _re(ECOSISTEMA)
_RE_FONTE = re.compile(r"\s+[-|]\s+[^-|]{2,40}$")       # " - CoinDesk" alla fine del titolo
_RE_SPEC = re.compile("|".join(re.escape(x) for x in SPECULATIVO), re.I)


def _testo(el):
    return html.unescape(re.sub(r"<[^>]+>", " ", el or "")).strip() if el else ""


def _data(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def leggi_rss(url, timeout=10):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        campi = {c.tag.split("}")[-1]: c for c in it}
        titolo = _testo(campi["title"].text) if "title" in campi else ""
        link_el = campi.get("link")
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href", "")).strip()
        # attenzione: un elemento XML senza figli vale False, quindi niente "or"
        data_el = next((campi[k] for k in ("pubDate", "published", "updated", "date") if k in campi), None)
        desc_el = next((campi[k] for k in ("description", "summary") if k in campi), None)
        ts = _data(data_el.text if data_el is not None else None)
        if titolo and ts:
            out.append({"titolo": titolo, "link": link, "ts": ts,
                        "fonte": urlparse(link or url).netloc.replace("www.", ""),
                        "testo": _testo(desc_el.text if desc_el is not None else "")[:300]})
    return out


def leggi_gdelt(query, ore, timeout=15):
    r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc", headers=UA, timeout=timeout,
                     params={"query": "%s sourcelang:english" % query, "mode": "artlist",
                             "format": "json", "maxrecords": 75, "timespan": "%dh" % ore,
                             "sort": "datedesc"})
    r.raise_for_status()
    try:
        dati = r.json()
    except ValueError:
        return []
    out = []
    for a in dati.get("articles", []):
        try:
            ts = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        except (KeyError, ValueError):
            continue
        out.append({"titolo": a.get("title", "").strip(), "link": a.get("url", ""), "ts": ts,
                    "fonte": a.get("domain", "gdelt"), "testo": ""})
    return out


def _id(titolo):
    norm = re.sub(r"[^a-z0-9]+", " ", titolo.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def raccogli(cfg, log=print):
    """Scarica tutte le fonti in parallelo e tiene solo i titoli sulla moneta, senza doppioni."""
    n = cfg["notizie"]
    ore = n["finestra_ore"]
    ora = time.time()
    lavori = [("rss", u) for u in n["feed_rss"]]
    if n.get("gdelt", True):
        lavori.append(("gdelt", n["query_gdelt"]))

    def esegui(lav):
        tipo, arg = lav
        try:
            return lav, (leggi_rss(arg) if tipo == "rss" else leggi_gdelt(arg, ore)), None
        except Exception as e:  # una fonte che non risponde non ferma le altre
            return lav, [], e

    filtro = re.compile("|".join(n["parole_chiave"]), re.I)
    voci, stato = {}, {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for (tipo, arg), lista, err in ex.map(esegui, lavori):
            nome = arg if tipo == "gdelt" else urlparse(arg).netloc
            stato[nome] = "errore: %s" % type(err).__name__ if err else len(lista)
            for v in lista:
                if ora - v["ts"] > ore * 3600 or v["ts"] > ora + 600:
                    continue
                if tipo == "rss" and not filtro.search(v["titolo"] + " " + v["testo"]):
                    continue
                k = _id(v["titolo"])
                if k not in voci or v["ts"] < voci[k]["ts"]:
                    v["id"] = k
                    voci[k] = v
    return sorted(voci.values(), key=lambda v: v["ts"], reverse=True), stato


# ------------------------------------------------------------------ valutazione

def punteggio_lessico(titolo, testo, coin_re):
    """Dizionario v2: pesa le parole, capisce chi e' il soggetto, i recuperi e il riciclaggio."""
    t = _RE_FONTE.sub("", titolo)
    pos = sum(w for k, w in POSITIVE.items() if _RE_POS[k].search(t))
    neg = sum(w for k, w in NEGATIVE.items() if _RE_NEG[k].search(t))
    prezzo = sum(w for k, w in PREZZO.items() if _RE_PREZZO[k].search(t))
    if not (pos or neg or prezzo) and testo:            # niente nel titolo: guarda il sommario, piano
        pos = 0.5 * sum(w for k, w in POSITIVE.items() if _RE_POS[k].search(testo))
        neg = 0.5 * sum(w for k, w in NEGATIVE.items() if _RE_NEG[k].search(testo))

    # rilevanza: Solana e' il soggetto del titolo?
    m = coin_re.search(t)
    altro = _RE_ALTRI.search(t)
    if not m:
        r = 0.3
    elif altro and altro.start() < m.start():
        r = 0.35                                         # prima si parla di altro (es. "Binance hacker ... Solana")
    elif m.start() <= len(t) * 0.6:
        r = 0.8
    else:
        r = 0.55

    if neg and _RE_RECUPERO.search(t):                   # "rete ripristinata dopo l'outage"
        neg, pos = 0.0, pos + 0.2
    if neg and altro and _RE_RICICLAGGIO.search(t):      # fondi rubati altrove che passano su Solana
        neg *= 0.2
    elif neg and altro and (not m or altro.start() < m.start()):
        neg *= 0.4                                       # il problema e' di un altro
    elif neg and _RE_ECOSISTEMA.search(t):
        neg = min(neg, 1.0) * 0.6                        # colpito un progetto su Solana, non la rete

    s = pos - neg + prezzo
    if t.strip().endswith("?") or _RE_SPEC.search(t):
        s *= 0.5                                         # ipotesi, previsioni, opinioni
    s = max(-1.0, min(1.0, s))
    return {"s": round(s, 2), "r": r, "m": "lessico", "v": LESSICO_VERSIONE}


def chiedi_claude(chiave, modello, sistema, utente, max_tokens=1500, timeout=45):
    r = requests.post(ANTHROPIC_URL, timeout=timeout, headers={
        "x-api-key": chiave, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": modello, "max_tokens": max_tokens, "system": sistema,
              "messages": [{"role": "user", "content": utente}]})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def _json(testo, apre, chiude):
    a, b = testo.find(apre), testo.rfind(chiude)
    if a < 0 or b <= a:
        raise ValueError("risposta senza JSON")
    return json.loads(testo[a:b + 1])


def _oggetti(testo):
    """Legge gli oggetti {...} uno per uno: se la risposta e' tagliata, tiene quelli completi."""
    out = []
    for m in re.finditer(r"\{[^{}]*\}", testo):
        try:
            out.append(json.loads(m.group(0)))
        except ValueError:
            continue
    return out


def punteggio_claude(voci, moneta, chiave, modello):
    righe = "\n".join("%d. [%s] %s" % (i + 1, v["fonte"], v["titolo"]) for i, v in enumerate(voci))
    sistema = ("You are a crypto news analyst. For each headline, judge its likely short-term "
               "price impact (next 24h) on %s. Reply ONLY with compact JSON, no markdown, no spaces." % moneta)
    utente = ("Headlines:\n%s\n\nReturn a JSON array with one object per headline: "
              "{\"n\":number,\"r\":0..1 (how much it concerns %s specifically),"
              "\"s\":-1..1 (negative = bearish, positive = bullish, 0 = neutral or old news)}. "
              "Compact JSON array only." % (righe, moneta))
    testo = chiedi_claude(chiave, modello, sistema, utente, max_tokens=60 + 40 * len(voci))
    out = {}
    for d in _oggetti(testo):
        try:
            i = int(d["n"]) - 1
            s = float(d.get("s", d.get("punteggio")))
            r = float(d.get("r", d.get("rilevanza")))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < len(voci):
            out[voci[i]["id"]] = {"s": max(-1.0, min(1.0, s)), "r": max(0.0, min(1.0, r)), "m": "claude"}
    if not out:
        raise ValueError("risposta non leggibile: %r" % testo[:160])
    return out


class Cache:
    def __init__(self, file):
        self.file = file
        self.d = {}
        if os.path.exists(file):
            try:
                with open(file, encoding="utf-8") as f:
                    self.d = json.load(f)
            except ValueError:
                self.d = {}

    def salva(self, giorni=4):
        limite = time.time() - giorni * 86400
        self.d = {k: v for k, v in self.d.items() if v.get("ts", 0) > limite}
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.file)


def valuta(voci, cfg, cache, log=print):
    """Aggiunge s (sentiment) e r (rilevanza) a ogni voce, chiamando Claude solo per le nuove."""
    n = cfg["notizie"]
    coin_re = re.compile("|".join(n["parole_chiave"]), re.I)
    chiave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    usa_claude = bool(chiave) and n.get("usa_claude", True)

    def da_valutare(v):
        c = cache.d.get(v["id"])
        if c is None:
            return True
        if c.get("m") == "lessico":                      # rivaluta: dizionario nuovo o Claude ora disponibile
            return usa_claude or c.get("v") != LESSICO_VERSIONE
        return False

    nuove = [v for v in voci if da_valutare(v)]
    risultati = {}
    if nuove and usa_claude:
        for i in range(0, len(nuove), 15):  # al massimo 15 titoli per chiamata
            blocco = nuove[i:i + 15]
            try:
                risultati.update(punteggio_claude(blocco, n["moneta"], chiave, n["modello"]))
            except requests.HTTPError as e:
                corpo = e.response.text[:200] if e.response is not None else ""
                log("Claude non disponibile (HTTP %s %s): uso il dizionario" % (
                    getattr(e.response, "status_code", "?"), corpo))
                break
            except Exception as e:
                log("Claude non disponibile (%s: %s): uso il dizionario" % (type(e).__name__, str(e)[:200]))
                break
    for v in nuove:
        p = risultati.get(v["id"]) or punteggio_lessico(v["titolo"], v["testo"], coin_re)
        cache.d[v["id"]] = dict(p, ts=v["ts"], titolo=v["titolo"][:160])
    for v in voci:
        c = cache.d[v["id"]]
        v.update(s=c["s"], r=c["r"], m=c["m"])
    if nuove:
        cache.salva()
    return voci, len(nuove), sum(1 for v in nuove if risultati.get(v["id"]))


def aggrega(voci, cfg, ora=None):
    """Media pesata: le notizie recenti e rilevanti contano di piu'. Rileva gli 'shock'."""
    n = cfg["notizie"]
    ora = ora or time.time()
    num = den = 0.0
    contributi, shock = [], []
    for v in voci:
        eta_h = max(0.0, (ora - v["ts"]) / 3600)
        w = 0.5 ** (eta_h / n["emivita_ore"]) * v["r"]
        num += w * v["s"]
        den += w
        contributi.append((abs(w * v["s"]), v))
        if (eta_h <= n["shock_ore"] and v["r"] >= n["shock_rilevanza"]
                and v["s"] <= n["shock_punteggio"]):
            shock.append(v)
    # con poche notizie il punteggio viene "tirato" verso 0 (neutro)
    punteggio = num / (den + n["prudenza"]) if den else 0.0
    principali = [v for _, v in sorted(contributi, key=lambda x: x[0], reverse=True)[:3]]
    return {"punteggio": round(punteggio, 3), "n": len(voci), "peso": round(den, 2),
            "shock": shock, "principali": principali}


def dibattito(contesto, cfg, log=print):
    """Ispirato a TradingAgents: analista rialzista vs ribassista + risk manager.
    Viene chiamato SOLO prima di un acquisto e puo' solo approvarlo o bloccarlo."""
    d = cfg["dibattito"]
    chiave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not d.get("attivo") or not chiave:
        return {"decisione": "approva", "motivo": "dibattito disattivato"}
    sistema = ("You simulate a small trading desk: a bull analyst, a bear analyst and a risk "
               "manager. The risk manager has the final word and must reject when evidence is "
               "weak or risk is high. Reply ONLY with a JSON object.")
    utente = ("Proposed trade: BUY %s spot, no leverage.\nMarket data: %s\nRecent headlines "
              "(score -1..1):\n%s\n\nReturn {\"rialzista\": one sentence, \"ribassista\": one "
              "sentence, \"decisione\": \"approva\" or \"rifiuta\", \"motivo\": one sentence in "
              "Italian}." % (contesto["moneta"], json.dumps(contesto["mercato"]),
                             "\n".join("- (%+.1f) %s" % (v["s"], v["titolo"]) for v in contesto["titoli"])))
    try:
        r = _json(chiedi_claude(chiave, d["modello"], sistema, utente, max_tokens=600), "{", "}")
        if r.get("decisione") not in ("approva", "rifiuta"):
            raise ValueError("decisione non valida")
        return r
    except Exception as e:
        log("Dibattito non riuscito (%s): per prudenza l'acquisto e' bloccato" % type(e).__name__)
        return {"decisione": "rifiuta", "motivo": "dibattito non riuscito"}
