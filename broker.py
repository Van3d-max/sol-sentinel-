"""Accesso a Coinbase Advanced Trade (live) e simulatore (paper).

Entrambi espongono gli stessi metodi, cosi' il bot non sa se i soldi sono veri.
"""
import json
import os
import time
import uuid
from decimal import Decimal, ROUND_DOWN

GRANULARITA = "ONE_HOUR"
SECONDI_CANDELA = 3600


class ErroreBroker(Exception):
    pass


def _d(resp):
    """Converte le risposte dell'SDK in dict normali."""
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    if hasattr(resp, "to_dict"):
        return resp.to_dict()
    return dict(resp)


def arrotonda_giu(valore, incremento):
    v, inc = Decimal(str(valore)), Decimal(str(incremento))
    if inc <= 0:
        return v
    return (v / inc).to_integral_value(rounding=ROUND_DOWN) * inc


def fmt(d):
    """Decimal -> stringa senza notazione scientifica e senza zeri inutili."""
    s = format(Decimal(d), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def crea_client(config, serve_auth):
    try:
        from coinbase.rest import RESTClient
    except ImportError:
        raise ErroreBroker("Manca la libreria: esegui  pip install -r requirements.txt")
    key = os.environ.get("COINBASE_API_KEY")
    secret = os.environ.get("COINBASE_API_SECRET")
    file_chiave = config.get("file_chiave_api", "cdp_api_key.json")
    if key and secret:
        return RESTClient(api_key=key, api_secret=secret.replace("\\n", "\n"), timeout=15)
    if os.path.exists(file_chiave):
        return RESTClient(key_file=file_chiave, timeout=15)
    if serve_auth:
        raise ErroreBroker(
            f"Chiave API non trovata: metti il file '{file_chiave}' nella cartella "
            "oppure imposta COINBASE_API_KEY e COINBASE_API_SECRET.")
    return RESTClient(timeout=15)  # solo endpoint pubblici


class DatiMercato:
    """Candele, prezzi e info coppie dagli endpoint pubblici di Coinbase."""

    def __init__(self, client):
        self.c = client
        self._info = {}

    def info_coppia(self, coppia):
        if coppia not in self._info:
            p = _d(self.c.get_public_product(coppia))
            if not p.get("product_id"):
                raise ErroreBroker(f"Coppia {coppia} non trovata")
            self._info[coppia] = p
        return self._info[coppia]

    def prezzo(self, coppia):
        p = _d(self.c.get_public_product(coppia))
        return float(p["price"])

    def candele(self, coppia, quante=300, fine=None):
        """Ultime candele orarie CHIUSE, dalla piu' vecchia alla piu' recente."""
        ora = int(time.time()) if fine is None else int(fine)
        tutte = {}
        fine_req = ora
        while len(tutte) < quante:
            n = min(350, quante - len(tutte) + 2)
            inizio = fine_req - n * SECONDI_CANDELA
            r = _d(self.c.get_public_candles(coppia, start=str(inizio), end=str(fine_req),
                                             granularity=GRANULARITA, limit=n))
            lotto = r.get("candles", [])
            if not lotto:
                break
            for x in lotto:
                t = int(x["start"])
                tutte[t] = {"t": t, "open": float(x["open"]), "high": float(x["high"]),
                            "low": float(x["low"]), "close": float(x["close"]),
                            "volume": float(x["volume"])}
            fine_req = inizio
        chiuse = [v for t, v in sorted(tutte.items()) if t + SECONDI_CANDELA <= ora]
        return chiuse[-quante:]


class BrokerCoinbase:
    """Ordini veri su Coinbase Advanced Trade."""

    modalita = "live"

    def __init__(self, client, config):
        self.c = client
        self.cfg = config
        self.dati = DatiMercato(client)

    # --- dati
    def info_coppia(self, coppia):
        return self.dati.info_coppia(coppia)

    def prezzo(self, coppia):
        return self.dati.prezzo(coppia)

    def candele(self, coppia, quante=300):
        return self.dati.candele(coppia, quante)

    def saldi(self):
        out, cursore = {}, None
        while True:
            r = _d(self.c.get_accounts(limit=250, cursor=cursore))
            for a in r.get("accounts", []):
                out[a["currency"]] = out.get(a["currency"], 0.0) + float(a["available_balance"]["value"])
            if not r.get("has_next"):
                return out
            cursore = r.get("cursor")

    # --- ordini
    def _esito(self, r, cosa):
        r = _d(r)
        if not r.get("success"):
            err = r.get("error_response") or r.get("failure_reason") or r
            raise ErroreBroker(f"{cosa} rifiutato da Coinbase: {err}")
        return r["success_response"]["order_id"]

    def stato_ordine(self, order_id):
        o = _d(self.c.get_order(order_id)).get("order", {})
        return {
            "id": order_id,
            "stato": o.get("status", "UNKNOWN"),
            "quantita": float(o.get("filled_size") or 0),
            "prezzo_medio": float(o.get("average_filled_price") or 0),
            "commissioni": float(o.get("total_fees") or 0),
            "controvalore": float(o.get("filled_value") or 0),
        }

    def _attendi_esecuzione(self, order_id, secondi=30):
        fine = time.time() + secondi
        while time.time() < fine:
            s = self.stato_ordine(order_id)
            if s["stato"] == "FILLED":
                return s
            if s["stato"] in ("CANCELLED", "EXPIRED", "FAILED"):
                raise ErroreBroker(f"Ordine {order_id} finito in stato {s['stato']}")
            time.sleep(1)
        s = self.stato_ordine(order_id)
        if s["quantita"] > 0:
            return s
        raise ErroreBroker(f"Ordine {order_id} non eseguito entro {secondi}s")

    def compra_mercato(self, coppia, euro):
        info = self.info_coppia(coppia)
        q = arrotonda_giu(euro, info["quote_increment"])
        oid = self._esito(self.c.market_order_buy(
            client_order_id=str(uuid.uuid4()), product_id=coppia, quote_size=fmt(q)), "Acquisto")
        return self._attendi_esecuzione(oid)

    def vendi_mercato(self, coppia, quantita):
        info = self.info_coppia(coppia)
        q = arrotonda_giu(quantita, info["base_increment"])
        oid = self._esito(self.c.market_order_sell(
            client_order_id=str(uuid.uuid4()), product_id=coppia, base_size=fmt(q)), "Vendita")
        return self._attendi_esecuzione(oid)

    def piazza_stop(self, coppia, quantita, prezzo_stop, prezzo_limite):
        info = self.info_coppia(coppia)
        q = arrotonda_giu(quantita, info["base_increment"])
        stop = arrotonda_giu(prezzo_stop, info["quote_increment"])
        lim = arrotonda_giu(prezzo_limite, info["quote_increment"])
        oid = self._esito(self.c.stop_limit_order_gtc_sell(
            client_order_id=str(uuid.uuid4()), product_id=coppia, base_size=fmt(q),
            limit_price=fmt(lim), stop_price=fmt(stop),
            stop_direction="STOP_DIRECTION_STOP_DOWN"), "Stop loss")
        # verifica che sia davvero aperto
        time.sleep(1)
        s = self.stato_ordine(oid)
        if s["stato"] in ("CANCELLED", "EXPIRED", "FAILED"):
            raise ErroreBroker(f"Stop loss {oid} in stato {s['stato']} subito dopo l'invio")
        return oid

    def annulla(self, order_id):
        r = _d(self.c.cancel_orders(order_ids=[order_id]))
        res = (r.get("results") or [{}])[0]
        return bool(res.get("success"))


class BrokerPaper:
    """Prezzi veri, ordini simulati. Gli stop simulati sono salvati su file."""

    modalita = "paper"

    def __init__(self, dati, config, file_ordini="paper_ordini.json"):
        self.dati = dati
        self.cfg = config
        self.fee = config["commissione_taker"]
        self.file = file_ordini
        self.ordini = {}
        if os.path.exists(self.file):
            with open(self.file) as f:
                self.ordini = json.load(f)

    def _salva(self):
        with open(self.file, "w") as f:
            json.dump(self.ordini, f, indent=2)

    def info_coppia(self, coppia):
        return self.dati.info_coppia(coppia)

    def prezzo(self, coppia):
        return self.dati.prezzo(coppia)

    def candele(self, coppia, quante=300):
        return self.dati.candele(coppia, quante)

    def saldi(self):
        return {"EUR": 1000.0}

    def compra_mercato(self, coppia, euro):
        p = self.prezzo(coppia)
        fee = euro * self.fee
        q = float(arrotonda_giu((euro - fee) / p, self.info_coppia(coppia)["base_increment"]))
        return {"id": "paper-" + uuid.uuid4().hex[:10], "stato": "FILLED", "quantita": q,
                "prezzo_medio": p, "commissioni": fee, "controvalore": q * p}

    def vendi_mercato(self, coppia, quantita):
        p = self.prezzo(coppia)
        lordo = quantita * p
        return {"id": "paper-" + uuid.uuid4().hex[:10], "stato": "FILLED", "quantita": quantita,
                "prezzo_medio": p, "commissioni": lordo * self.fee, "controvalore": lordo}

    def piazza_stop(self, coppia, quantita, prezzo_stop, prezzo_limite):
        oid = "paper-stop-" + uuid.uuid4().hex[:10]
        self.ordini[oid] = {"coppia": coppia, "quantita": quantita, "stop": prezzo_stop,
                            "limite": prezzo_limite, "stato": "OPEN", "innescato": False}
        self._salva()
        return oid

    def stato_ordine(self, order_id):
        o = self.ordini.get(order_id)
        if o is None:
            return {"id": order_id, "stato": "UNKNOWN", "quantita": 0, "prezzo_medio": 0,
                    "commissioni": 0, "controvalore": 0}
        if o["stato"] == "OPEN":
            p = self.prezzo(o["coppia"])
            if p <= o["stop"]:
                o["innescato"] = True
            # stop-limit: si esegue solo a un prezzo >= limite
            if o["innescato"] and p >= o["limite"]:
                lordo = o["quantita"] * p
                o.update(stato="FILLED", prezzo_medio=p, controvalore=lordo,
                         commissioni=lordo * self.fee)
            self._salva()
        if o["stato"] == "FILLED":
            return {"id": order_id, "stato": "FILLED", "quantita": o["quantita"],
                    "prezzo_medio": o["prezzo_medio"], "commissioni": o["commissioni"],
                    "controvalore": o["controvalore"]}
        return {"id": order_id, "stato": o["stato"], "quantita": 0, "prezzo_medio": 0,
                "commissioni": 0, "controvalore": 0}

    def annulla(self, order_id):
        o = self.ordini.get(order_id)
        if o and o["stato"] == "OPEN":
            o["stato"] = "CANCELLED"
            self._salva()
            return True
        return False
