"""Revisione settimanale: gli agenti imparano dai propri errori, ma solo con prove.

1. Rilegge le operazioni chiuse di ogni agente con la "fotografia" del momento d'entrata
   (ora, giorno, notizie, distanza dalla media, forza del trend, volatilita', obiettivo).
2. Statistiche: dove si vince e dove si perde, quanto pesano le commissioni, motivi d'uscita.
3. Cerca da sola i filtri "non entrare quando ..." che avrebbero evitato perdite, e chiede a Claude
   di spiegare le cause e proporne altri.
4. OGNI filtro proposto (dai numeri o da Claude) passa un esame: deve migliorare il risultato sia nella
   prima sia nella seconda meta' delle operazioni, senza scartarne troppe. Solo chi lo passa
   diventa una proposta da approvare.
5. Scrive REVISIONE.md (+ copia in revisioni/) e proposte.json. Le proposte si applicano con
   applica.py (o dal workflow "Applica proposta"): nessun cambio di regole senza approvazione.

Uso: python revisione.py           (con ANTHROPIC_API_KEY nell'ambiente usa anche Claude)
"""
import csv
import glob
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import apprendimento as ap

CARTELLA = os.path.dirname(os.path.abspath(__file__))
os.chdir(CARTELLA)
TZ = ZoneInfo("Europe/Rome")
MIN_OPERAZIONI = 8          # sotto questa soglia niente proposte: sarebbe fortuna, non apprendimento
MAX_SCARTATE = 0.6          # un filtro non puo' eliminare piu' del 60% delle operazioni
MIN_SCARTATE = 2
FILE_PROPOSTE = "proposte.json"
AUTO_DEFAULT = {"auto": False, "max_filtri": 3, "min_operazioni": 30, "guadagno_min_eur": 8.0, "meta_min_eur": 3.0,
                "verifica_dopo_operazioni": 8, "nuovi_a_settimana": 1, "quarantena_giorni": 28}
CAMPI = ["ora", "giorno", "notizie", "distanza_media_pct", "forza_trend_pct", "atr_pct", "obiettivo_pct", "stop_pct"]


# ------------------------------------------------------------------ dati

def leggi_csv(file):
    if not os.path.exists(file):
        return []
    with open(file, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def configurazioni():
    out = []
    for f in sorted(glob.glob("config_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                c = json.load(fh)
        except (OSError, ValueError):
            continue
        if "file_operazioni" in c and "file_decisioni" in c:
            out.append((f, c))
    return out


def ricostruisci_contesto(op, decisioni):
    """Per le operazioni registrate prima dell'apprendimento: ricava la fotografia dal registro decisioni."""
    ctx = {}
    for k in CAMPI:
        v = ap._num(op.get("ctx_" + k))
        if v is not None:
            ctx[k] = v
    if len(ctx) >= 4:
        return ctx
    a = op["aperta"]
    ctx.setdefault("ora", a.hour)
    ctx.setdefault("giorno", a.weekday())
    if ap._num(op.get("notizie_ingresso")) is not None:
        ctx.setdefault("notizie", ap._num(op.get("notizie_ingresso")))
    vicina = None
    for d in decisioni:
        if d.get("azione") != "compra":
            continue
        try:
            t = datetime.fromisoformat(d["data"])
        except (KeyError, ValueError):
            continue
        if abs((t - a).total_seconds()) <= 300:
            vicina = d
            break
    if vicina:
        p, mv, ml = (ap._num(vicina.get(k)) for k in ("prezzo", "media_veloce", "media_lenta"))
        if p and ml:
            ctx.setdefault("distanza_media_pct", round((p / ml - 1) * 100, 3))
        if mv and ml:
            ctx.setdefault("forza_trend_pct", round((mv / ml - 1) * 100, 3))
        if ap._num(vicina.get("atr_pct")) is not None:
            ctx.setdefault("atr_pct", ap._num(vicina.get("atr_pct")))
    return ctx


# ------------------------------------------------------------------ statistiche

def riassunto(ops):
    if not ops:
        return {"n": 0}
    netti = [o["netto"] for o in ops]
    comm = sum(ap._num(o.get("commissioni_eur"), 0) for o in ops)
    vinte = [x for x in netti if x > 0]
    perse = [x for x in netti if x <= 0]
    eq = picco = calo = 0.0
    for x in netti:
        eq += x
        picco = max(picco, eq)
        calo = max(calo, picco - eq)
    motivi = {}
    for o in ops:
        m = (o.get("motivo_uscita") or "?").split(":")[0]
        motivi.setdefault(m, [0, 0.0])
        motivi[m][0] += 1
        motivi[m][1] += o["netto"]
    durate = [ap._num(o.get("durata_ore")) for o in ops if ap._num(o.get("durata_ore")) is not None]
    return {"n": len(ops), "vinte_pct": round(100 * len(vinte) / len(ops), 1), "totale": round(sum(netti), 2),
            "media": round(sum(netti) / len(ops), 2), "vincita_media": round(sum(vinte) / len(vinte), 2) if vinte else 0,
            "perdita_media": round(sum(perse) / len(perse), 2) if perse else 0, "commissioni": round(comm, 2),
            "senza_commissioni": round(sum(netti) + comm, 2), "calo_massimo": round(calo, 2),
            "durata_media_ore": round(sum(durate) / len(durate), 1) if durate else None,
            "motivi_uscita": {k: {"n": v[0], "eur": round(v[1], 2)} for k, v in motivi.items()}}


def per_fascia(ops):
    """Risultato per fasce di ogni caratteristica: dove si vince e dove si perde."""
    out = {}
    fasce_ora = [("notte 0-6", 0, 6), ("mattina 7-12", 7, 12), ("pomeriggio 13-18", 13, 18), ("sera 19-23", 19, 23)]
    for nome, a, b in fasce_ora:
        g = [o for o in ops if a <= o["ctx"].get("ora", -1) <= b]
        if g:
            out.setdefault("ora", {})[nome] = {"n": len(g), "eur": round(sum(o["netto"] for o in g), 2)}
    for nome, giorni in (("feriali", range(0, 5)), ("weekend", (5, 6))):
        g = [o for o in ops if o["ctx"].get("giorno") in giorni]
        if g:
            out.setdefault("giorno", {})[nome] = {"n": len(g), "eur": round(sum(o["netto"] for o in g), 2)}
    for campo in CAMPI[2:]:
        valori = sorted(o["ctx"][campo] for o in ops if o["ctx"].get(campo) is not None)
        if len(valori) < 4:
            continue
        med = valori[len(valori) // 2]
        basso = [o for o in ops if o["ctx"].get(campo) is not None and o["ctx"][campo] < med]
        alto = [o for o in ops if o["ctx"].get(campo) is not None and o["ctx"][campo] >= med]
        out[campo] = {"sotto %.3f" % med: {"n": len(basso), "eur": round(sum(o["netto"] for o in basso), 2)},
                      "da %.3f in su" % med: {"n": len(alto), "eur": round(sum(o["netto"] for o in alto), 2)}}
    return out


# ------------------------------------------------------------------ filtri: candidati ed esame

def candidati(ops):
    c = [{"campo": "ora", "op": "tra", "valore": [a, b]} for a, b in ((0, 6), (7, 12), (13, 18), (19, 23))]
    c.append({"campo": "giorno", "op": "in", "valore": [5, 6]})
    for campo in CAMPI[2:]:
        valori = sorted(o["ctx"][campo] for o in ops if o["ctx"].get(campo) is not None)
        if len(valori) < MIN_OPERAZIONI:
            continue
        soglie = sorted({round(valori[int(len(valori) * q)], 3) for q in (0.25, 0.5, 0.75)})
        for s in soglie:
            c.append({"campo": campo, "op": "<", "valore": s})
            c.append({"campo": campo, "op": ">", "valore": s})
    return c


def esame(filtro, ops):
    """Quanto avrebbe cambiato il risultato evitare le entrate che ricadono nel filtro."""
    if not ops:
        return {"passa": False, "motivo": "nessuna operazione"}
    scartate = [o for o in ops if ap.condizione(filtro, o["ctx"])]
    meta = len(ops) // 2
    prima, seconda = ops[:meta], ops[meta:]
    effetto = lambda gruppo: -sum(o["netto"] for o in gruppo if ap.condizione(filtro, o["ctx"]))
    ris = {"operazioni": len(ops), "scartate": len(scartate),
           "guadagno_eur": round(effetto(ops), 2), "prima_meta_eur": round(effetto(prima), 2),
           "seconda_meta_eur": round(effetto(seconda), 2),
           "vinte_scartate": sum(1 for o in scartate if o["netto"] > 0)}
    if len(ops) < MIN_OPERAZIONI:
        ris.update(passa=False, motivo="servono almeno %d operazioni (ora %d)" % (MIN_OPERAZIONI, len(ops)))
    elif len(scartate) < MIN_SCARTATE:
        ris.update(passa=False, motivo="riguarda troppo poche operazioni")
    elif len(scartate) > MAX_SCARTATE * len(ops):
        ris.update(passa=False, motivo="eliminerebbe troppe operazioni")
    elif ris["prima_meta_eur"] <= 0 or ris["seconda_meta_eur"] <= 0:
        ris.update(passa=False, motivo="non migliora in entrambe le meta' (potrebbe essere fortuna)")
    else:
        ris.update(passa=True, motivo="migliora in entrambe le meta'")
    return ris


# ------------------------------------------------------------------ Claude

def chiedi_analisi(nome, stats, fasce, ops, migliori, cfg):
    chiave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not chiave:
        return None, []
    import notizie as nz
    ultime = [{"apertura": o["data_apertura"], "netto": round(o["netto"], 2), "uscita": o.get("motivo_uscita"),
               "durata_ore": o.get("durata_ore"), "contesto": o["ctx"]} for o in ops[-20:]]
    dati = {"agente": nome, "coppia": cfg.get("coppia"), "regole": cfg.get("regole"), "trend": cfg.get("trend"),
            "statistiche": stats, "per_fascia": fasce, "ultime_operazioni": ultime,
            "filtri_candidati_migliori": migliori, "filtri_gia_attivi": cfg.get("filtri_appresi", [])}
    sistema = ("Sei il revisore di un bot di trading spot in simulazione. Analizzi i suoi errori con onesta': "
               "con poche operazioni dillo chiaramente e non inventare schemi. Rispondi in italiano semplice.")
    utente = ("Dati dell'ultima settimana (JSON):\n%s\n\n"
              "Scrivi un rapporto breve in markdown con tre sezioni: '### Cosa e' successo', "
              "'### Perche' ha perso (cause probabili)', '### Cosa proverei'. Niente titolo principale.\n"
              "Poi, su una riga a parte, scrivi esattamente ===FILTRI=== seguito da un array JSON (anche vuoto) "
              "di al massimo 3 filtri 'non entrare quando', ciascuno nel formato "
              "{\"campo\": uno tra %s, \"op\": \"<\"|\">\"|\"tra\"|\"fuori\"|\"in\", \"valore\": numero o [min,max] o lista, "
              "\"motivo\": \"spiegazione breve\"}. Verranno testati sui dati prima di essere proposti."
              % (json.dumps(dati, ensure_ascii=False, default=str), CAMPI))
    modelli = [cfg.get("revisione", {}).get("modello"), cfg.get("notizie", {}).get("modello")]
    for modello in [m for m in modelli if m]:
        try:
            testo = nz.chiedi_claude(chiave, modello, sistema, utente, max_tokens=4000, timeout=120)
        except Exception as e:
            print("Claude (%s) non disponibile: %s" % (modello, e))
            continue
        rapporto, _, resto = testo.partition("===FILTRI===")
        filtri = []
        try:
            for f in json.loads(resto[resto.find("["):resto.rfind("]") + 1] or "[]"):
                if isinstance(f, dict) and f.get("campo") in CAMPI and f.get("op") in ("<", ">", "tra", "fuori", "in"):
                    filtri.append({k: f[k] for k in ("campo", "op", "valore", "motivo") if k in f})
        except ValueError:
            pass
        return rapporto.strip(), filtri
    return None, []


# ------------------------------------------------------------------ proposte

def leggi_proposte():
    if os.path.exists(FILE_PROPOSTE):
        with open(FILE_PROPOSTE, encoding="utf-8") as f:
            return json.load(f)
    return []


def stessa_regola(a, b):
    return a["campo"] == b["campo"] and a["op"] == b["op"] and json.dumps(a["valore"]) == json.dumps(b["valore"])


def filtri_attivi_report(cfg, decisioni, ops):
    righe = []
    for f in cfg.get("filtri_appresi", []):
        bloccate = sum(1 for d in decisioni if ("filtro appreso %s:" % f.get("id")) in (d.get("motivo") or ""))
        dal = f.get("applicato")
        dopo = [o for o in ops if dal and o["data_apertura"] >= dal]
        righe.append("- **%s** — %s (dal %s): ha bloccato %d entrate; operazioni da allora %d, %+.2f EUR"
                     % (f.get("id"), ap.descrivi(f), (dal or "?")[:10], bloccate, len(dopo), sum(o["netto"] for o in dopo)))
    return righe


def prossimo_id_filtro():
    ids = []
    for f in glob.glob("config_*.json"):
        try:
            with open(f, encoding="utf-8") as fh:
                ids += [x.get("id", "") for x in json.load(fh).get("filtri_appresi", [])]
        except (OSError, ValueError):
            pass
    return "F%d" % (1 + max([int(x[1:]) for x in ids if x[1:].isdigit()] or [0]))


def in_quarantena(file_cfg, campo, proposte, adesso, giorni):
    """Un tipo di filtro tolto da poco non puo' rientrare subito (evita il ping-pong)."""
    for p in proposte:
        if p["config"] == file_cfg and p["filtro"]["campo"] == campo and p["stato"] == "rimossa automaticamente":
            try:
                if adesso - datetime.fromisoformat(p["deciso"]) <= timedelta(days=giorni):
                    return True
            except (KeyError, ValueError):
                continue
    return False


def salva_config(file_cfg, cfg):
    with open(file_cfg + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(file_cfg + ".tmp", file_cfg)


def automatico(file_cfg, cfg, ops, proposte, adesso):
    """Modalita' automatica: toglie i filtri che hanno peggiorato i risultati e applica le proposte forti."""
    a = dict(AUTO_DEFAULT, **cfg.get("apprendimento", {}))
    note = []
    if not a["auto"]:
        return note
    media = lambda g: sum(o["netto"] for o in g) / len(g)
    # 1) verifica dei filtri attivi: dopo N operazioni, se la media e' peggiorata rispetto a prima, si toglie
    tenuti = []
    for f in cfg.get("filtri_appresi", []):
        dal = f.get("applicato", "")
        prima = [o for o in ops if o["data_apertura"] < dal]
        dopo = [o for o in ops if o["data_apertura"] >= dal]
        if prima and len(dopo) >= a["verifica_dopo_operazioni"] and media(dopo) < media(prima):
            note.append("- ❌ Tolto **%s** (%s): da quando era attivo %d operazioni con media %+.2f € contro %+.2f € di prima."
                        % (f.get("id"), ap.descrivi(f), len(dopo), media(dopo), media(prima)))
            for p in proposte:
                if p.get("filtro_id") == f.get("id"):
                    p.update(stato="rimossa automaticamente", deciso=adesso.isoformat(timespec="seconds"))
        else:
            tenuti.append(f)
    cambiato = len(tenuti) != len(cfg.get("filtri_appresi", []))
    cfg["filtri_appresi"] = tenuti
    # 2) applicazione delle proposte forti: una a settimana, finche' c'e' posto
    applicati = 0
    for p in sorted([p for p in proposte if p["config"] == file_cfg and p["stato"] == "da approvare"],
                    key=lambda p: -p["test"]["guadagno_eur"]):
        if len(cfg["filtri_appresi"]) >= a["max_filtri"] or applicati >= a["nuovi_a_settimana"] \
                or len(ops) < a["min_operazioni"]:
            break
        t = p["test"]
        forte = (t["guadagno_eur"] >= a["guadagno_min_eur"] and t["prima_meta_eur"] >= a["meta_min_eur"]
                 and t["seconda_meta_eur"] >= a["meta_min_eur"])
        if not forte or any(g["campo"] == p["filtro"]["campo"] for g in cfg["filtri_appresi"]) \
                or in_quarantena(file_cfg, p["filtro"]["campo"], proposte, adesso, a["quarantena_giorni"]):
            continue
        filtro = dict(p["filtro"], id=prossimo_id_filtro(), applicato=adesso.isoformat(timespec="seconds"), proposta=p["id"])
        cfg["filtri_appresi"].append(filtro)
        salva_config(file_cfg, cfg)
        p.update(stato="applicata automaticamente", deciso=filtro["applicato"], filtro_id=filtro["id"])
        note.append("- ✅ Applicato **%s** (%s): %s." % (filtro["id"], p["id"], ap.descrivi(filtro)))
        cambiato = False
        applicati += 1
    if cambiato:
        salva_config(file_cfg, cfg)
    return note


def main():
    adesso = datetime.now(TZ)
    proposte = leggi_proposte()
    oggi = adesso.strftime("%Y%m%d")
    auto = any(dict(AUTO_DEFAULT, **c.get("apprendimento", {}))["auto"] for _, c in configurazioni())
    L = ["# Revisione settimanale degli agenti", "",
         ("Generata il %s. Modalità **automatica**: le proposte forti vengono applicate da sole (al massimo 3 filtri "
          "per agente) e un filtro che peggiora i risultati viene tolto da solo." if auto else
          "Generata il %s. Gli agenti non cambiano regole da soli: le proposte qui sotto entrano in funzione "
          "solo se le approvi (workflow **Applica proposta**, oppure `python applica.py --id ...`).")
         % adesso.strftime("%d/%m/%Y %H:%M"), ""]
    nuove = sum(1 for p in proposte if p["id"].startswith("P%s-" % oggi))
    for file_cfg, cfg in configurazioni():
        quar = dict(AUTO_DEFAULT, **cfg.get("apprendimento", {}))["quarantena_giorni"]
        nome = cfg.get("nome", file_cfg)
        decisioni = leggi_csv(cfg["file_decisioni"])
        ops = ap.leggi_operazioni(cfg["file_operazioni"])
        for o in ops:
            o["ctx"] = ricostruisci_contesto(o, decisioni)
        settimana = [o for o in ops if adesso - o["chiusa"].astimezone(TZ) <= timedelta(days=7)]
        st_tot, st_sett = riassunto(ops), riassunto(settimana)
        fasce = per_fascia(ops)
        L += ["## Agente %s (%s)" % (nome, cfg.get("coppia")), ""]
        if not ops:
            L += ["Nessuna operazione chiusa finora: niente da imparare.", ""]
        else:
            L += ["| | Operazioni | Vinte | Risultato | Senza commissioni | Commissioni | Calo massimo |",
                  "|---|---|---|---|---|---|---|"]
            for etichetta, s in (("Ultimi 7 giorni", st_sett), ("Dall'inizio", st_tot)):
                if s["n"]:
                    L.append("| %s | %d | %.0f%% | %+.2f € | %+.2f € | %.2f € | %.2f € |" % (
                        etichetta, s["n"], s["vinte_pct"], s["totale"], s["senza_commissioni"], s["commissioni"], s["calo_massimo"]))
                else:
                    L.append("| %s | 0 | — | — | — | — | — |" % etichetta)
            L += ["", "Motivi di uscita: " + ", ".join("%s %d (%+.2f €)" % (k, v["n"], v["eur"])
                                                    for k, v in st_tot["motivi_uscita"].items()), ""]
        stato_freno = ap.freno(cfg, adesso)
        L.append("**Freno automatico:** %s" % (stato_freno or "non attivo, l'agente puo' entrare"))
        attivi = filtri_attivi_report(cfg, decisioni, ops)
        L += ["", "**Filtri appresi attivi:**"] + (attivi or ["- nessuno"]) + [""]

        # esame dei filtri: prima i numeri, poi le idee di Claude
        esaminati = []
        for f in candidati(ops):
            e = esame(f, ops)
            esaminati.append((f, e, "dati"))
        migliori = sorted([x for x in esaminati if x[1].get("guadagno_eur", 0) > 0],
                          key=lambda x: -x[1]["guadagno_eur"])[:8]
        rapporto, filtri_claude = (chiedi_analisi(nome, st_tot, fasce, ops, [dict(f, test=e) for f, e, _ in migliori], cfg)
                                   if ops else (None, []))
        for f in filtri_claude:
            esaminati.append((f, esame(f, ops), "claude"))
        if rapporto:
            L += ["### Analisi di Claude", "", rapporto, ""]
        elif len(ops) < MIN_OPERAZIONI:
            L += ["_Analisi di Claude non richiesta o non disponibile._", ""]

        promossi = []
        for f, e, fonte in sorted(esaminati, key=lambda x: -x[1].get("guadagno_eur", 0)):
            if e["passa"] and not any(stessa_regola(f, p[0]) for p in promossi) \
                    and not any(stessa_regola(f, g) for g in cfg.get("filtri_appresi", [])) \
                    and not any(p["config"] == file_cfg and stessa_regola(f, p["filtro"]) and p["stato"] == "da approvare"
                                for p in proposte) \
                    and not any(p[0]["campo"] == f["campo"] for p in promossi) \
                    and not in_quarantena(file_cfg, f["campo"], proposte, adesso, quar):
                promossi.append((f, e, fonte))
            if len(promossi) == 3:
                break
        L += ["### Proposte di questa settimana", ""]
        if len(ops) < MIN_OPERAZIONI:
            L.append("Dati insufficienti: %d operazioni, ne servono almeno %d per proporre regole affidabili. "
                     "Il freno automatico e le regole attuali restano in funzione." % (len(ops), MIN_OPERAZIONI))
        elif not promossi:
            L.append("Nessun filtro ha superato l'esame: nessuna regola da cambiare per ora.")
        for f, e, fonte in promossi:
            nuove += 1
            pid = "P%s-%d" % (oggi, nuove)
            filtro = {k: f[k] for k in ("campo", "op", "valore")}
            filtro["motivo"] = f.get("motivo") or "evitava %d entrate per %+.2f EUR nei dati" % (e["scartate"], e["guadagno_eur"])
            proposte.append({"id": pid, "config": file_cfg, "agente": nome, "filtro": filtro, "test": e,
                             "fonte": fonte, "creata": adesso.isoformat(timespec="seconds"), "stato": "da approvare"})
            L.append("- **%s** — %s. Nei dati: avrebbe evitato %d operazioni su %d (%d erano vinte), "
                     "**%+.2f €** (prima metà %+.2f €, seconda metà %+.2f €). Fonte: %s."
                     % (pid, ap.descrivi(filtro), e["scartate"], e["operazioni"], e["vinte_scartate"],
                        e["guadagno_eur"], e["prima_meta_eur"], e["seconda_meta_eur"],
                        "Claude" if fonte == "claude" else "analisi dei numeri"))
        if filtri_claude:
            scartati = [(f, e) for f, e, fo in esaminati if fo == "claude" and not e["passa"]]
            if scartati:
                L += ["", "Idee di Claude scartate dall'esame:"]
                L += ["- %s → %s" % (ap.descrivi(f), e["motivo"]) for f, e in scartati]
        note = automatico(file_cfg, cfg, ops, proposte, adesso)
        if note:
            L += ["", "**Cambi automatici di questa settimana:**"] + note
        L.append("")
    in_attesa = [p for p in proposte if p["stato"] == "da approvare"]
    L += ["## Proposte in attesa" + (" (non abbastanza forti per l'applicazione automatica)" if auto else " di approvazione"), ""]
    L += ["- **%s** (%s): %s — %+.2f € nei dati" % (p["id"], p["agente"], ap.descrivi(p["filtro"]), p["test"]["guadagno_eur"])
          for p in in_attesa] or ["Nessuna."]
    testo = "\n".join(L) + "\n"
    with open("REVISIONE.md", "w", encoding="utf-8") as f:
        f.write(testo)
    os.makedirs("revisioni", exist_ok=True)
    with open(os.path.join("revisioni", adesso.strftime("%Y-%m-%d") + ".md"), "w", encoding="utf-8") as f:
        f.write(testo)
    with open(FILE_PROPOSTE, "w", encoding="utf-8") as f:
        json.dump(proposte, f, indent=2, ensure_ascii=False)
    print(testo)


if __name__ == "__main__":
    main()
