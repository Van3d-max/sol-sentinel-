"""Applica, rifiuta o rimuove le regole proposte dalla revisione settimanale.

  python applica.py --id P20261115-1         applica la proposta (diventa un filtro attivo)
  python applica.py --rifiuta P20261115-1    la scarta
  python applica.py --rimuovi F1             disattiva un filtro gia' attivo
  python applica.py --elenco                 mostra proposte e filtri attivi
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import apprendimento as ap

CARTELLA = os.path.dirname(os.path.abspath(__file__))
os.chdir(CARTELLA)
FILE_PROPOSTE = "proposte.json"


def carica(file):
    with open(file, encoding="utf-8") as f:
        return json.load(f)


def salva(file, dati):
    with open(file + ".tmp", "w", encoding="utf-8") as f:
        json.dump(dati, f, indent=2, ensure_ascii=False)
    os.replace(file + ".tmp", file)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--id")
    p.add_argument("--rifiuta")
    p.add_argument("--rimuovi")
    p.add_argument("--elenco", action="store_true")
    a = p.parse_args()
    adesso = datetime.now(ZoneInfo("Europe/Rome")).isoformat(timespec="seconds")
    proposte = carica(FILE_PROPOSTE) if os.path.exists(FILE_PROPOSTE) else []

    if a.elenco or not (a.id or a.rifiuta or a.rimuovi):
        for pr in proposte:
            print("%-14s %-13s %-8s %s" % (pr["id"], pr["stato"], pr["agente"], ap.descrivi(pr["filtro"])))
        for f in sorted(glob.glob("config_*.json")):
            for fl in carica(f).get("filtri_appresi", []):
                print("ATTIVO %-6s %-20s %s" % (fl["id"], f, ap.descrivi(fl)))
        return

    if a.id or a.rifiuta:
        pid = a.id or a.rifiuta
        pr = next((x for x in proposte if x["id"] == pid.strip()), None)
        if not pr:
            sys.exit("Proposta %s non trovata in %s" % (pid, FILE_PROPOSTE))
        if pr["stato"] != "da approvare":
            sys.exit("La proposta %s e' gia' '%s'" % (pid, pr["stato"]))
        if a.rifiuta:
            pr.update(stato="rifiutata", deciso=adesso)
            salva(FILE_PROPOSTE, proposte)
            print("Proposta %s rifiutata." % pid)
            return
        cfg = carica(pr["config"])
        attivi = cfg.setdefault("filtri_appresi", [])
        tutti = [fl["id"] for f in glob.glob("config_*.json") for fl in carica(f).get("filtri_appresi", [])]
        n = 1 + max([int(x[1:]) for x in tutti if x[1:].isdigit()] or [0])
        filtro = dict(pr["filtro"], id="F%d" % n, applicato=adesso, proposta=pid)
        attivi.append(filtro)
        salva(pr["config"], cfg)
        pr.update(stato="applicata", deciso=adesso, filtro_id=filtro["id"])
        salva(FILE_PROPOSTE, proposte)
        print("Applicata %s come filtro %s su %s: %s" % (pid, filtro["id"], pr["config"], ap.descrivi(filtro)))
        return

    for f in sorted(glob.glob("config_*.json")):
        cfg = carica(f)
        attivi = cfg.get("filtri_appresi", [])
        resto = [x for x in attivi if x.get("id") != a.rimuovi.strip()]
        if len(resto) != len(attivi):
            cfg["filtri_appresi"] = resto
            salva(f, cfg)
            print("Filtro %s rimosso da %s" % (a.rimuovi, f))
            return
    sys.exit("Filtro %s non trovato" % a.rimuovi)


if __name__ == "__main__":
    main()
