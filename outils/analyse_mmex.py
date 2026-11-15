#!/usr/bin/env python3
"""Analyse quotidienne du fichier de comptes MMEX de Yann (LECTURE SEULE).

Usage :
    python3 -I outils/analyse_mmex.py FICHIER [--depuis AAAA-MM-JJ] [--aujourdhui AAAA-MM-JJ]

FICHIER : le fichier .mmb (base SQLite), ou le JSON renvoyé par l'outil Drive
download_file_content (champ "content" en base64).
--depuis : date de la dernière analyse (par défaut : la veille).

Le fichier n'est jamais modifié : il est ouvert en lecture seule.
"""
import argparse
import base64
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile

BUDGET_REVOLUT = 1300.0
SEUIL_COMPTE_BAS = 100.0
SEUIL_GROSSE_OPERATION = 500.0
# Revenus prévus dans l'échéancier mais pas garantis (intérim de Yann) : exclus des prévisions.
REVENUS_INCERTAINS = {"travail"}


def ouvrir(chemin):
    with open(chemin, "rb") as f:
        debut = f.read(16)
    if not debut.startswith(b"SQLite format 3"):
        with open(chemin, encoding="utf-8") as f:
            data = json.load(f)
        brut = base64.b64decode(data["content"])
        tmp = tempfile.NamedTemporaryFile(suffix=".mmb", delete=False)
        tmp.write(brut)
        tmp.close()
        chemin = tmp.name
    return sqlite3.connect(f"file:{os.path.abspath(chemin)}?mode=ro", uri=True)


def eur(x):
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fichier")
    ap.add_argument("--aujourdhui", default=dt.date.today().isoformat())
    ap.add_argument("--depuis")
    a = ap.parse_args()
    auj = dt.date.fromisoformat(a.aujourdhui)
    depuis = dt.date.fromisoformat(a.depuis) if a.depuis else auj - dt.timedelta(days=1)
    c = ouvrir(a.fichier)

    c.executescript("""
    create temp view t as select c.*, substr(TRANSDATE,1,10) d from CHECKINGACCOUNT_V1 c
      where coalesce(DELETEDTIME,'')='';
    create temp view mv as
      select ACCOUNTID acc, d, case TRANSCODE when 'Deposit' then TRANSAMOUNT else -TRANSAMOUNT end amt from t
      union all select TOACCOUNTID, d, TOTRANSAMOUNT from t where TRANSCODE='Transfer';
    """)
    comptes = c.execute("select ACCOUNTID, ACCOUNTNAME, INITIALBAL from ACCOUNTLIST_V1 where STATUS='Open' order by ACCOUNTNAME").fetchall()
    soldes = {}
    for acc, nom, init in comptes:
        s = init + (c.execute("select coalesce(sum(amt),0) from mv where acc=? and d<=?", (acc, auj.isoformat())).fetchone()[0])
        soldes[acc] = (nom, round(s, 2))

    alertes = []
    print(f"=== Analyse MMEX du {auj.strftime('%d/%m/%Y')} (lecture seule) ===\n")
    print("Soldes :")
    courants = livrets = placements = 0.0
    for acc, (nom, s) in soldes.items():
        print(f"  - {nom} : {eur(s)}")
        n = nom.upper()
        if "LDD" in n or " LA " in f" {n} ":
            livrets += s
        elif "PEA" in n or "NATIXIS" in n:
            placements += s
        else:
            courants += s
            if s < SEUIL_COMPTE_BAS:
                alertes.insert(0, f"solde bas sur « {nom} » : {eur(s)}")
    print(f"  Total comptes courants : {eur(courants)} | livrets : {eur(livrets)} | placements : {eur(placements)}\n")

    nouv = c.execute("""
      select t.d, a.ACCOUNTNAME, t.TRANSCODE, t.TRANSAMOUNT, coalesce(p.PAYEENAME,''), coalesce(g.CATEGNAME,''), coalesce(t.NOTES,'')
      from t join ACCOUNTLIST_V1 a on a.ACCOUNTID=t.ACCOUNTID
      left join PAYEE_V1 p on p.PAYEEID=t.PAYEEID left join CATEGORY_V1 g on g.CATEGID=t.CATEGID
      where substr(t.LASTUPDATEDTIME,1,10) >= ? order by t.d""", (depuis.isoformat(),)).fetchall()
    print(f"Opérations saisies ou modifiées depuis le {depuis.strftime('%d/%m/%Y')} : {len(nouv)}")
    for d, compte, code, mt, tiers, cat, notes in nouv:
        signe = "+" if code == "Deposit" else ("→" if code == "Transfer" else "-")
        print(f"  - {d} | {compte} | {signe}{eur(mt)} | {tiers} | {cat} {('| ' + notes.strip()) if notes.strip() else ''}")
        if code != "Transfer" and mt >= SEUIL_GROSSE_OPERATION:
            alertes.append(f"opération importante : {signe}{eur(mt)} {tiers} le {d}")
    print()

    debut_mois = auj.replace(day=1).isoformat()
    rows = c.execute("""
      select coalesce(g.CATEGNAME,'?'), sum(case TRANSCODE when 'Deposit' then TRANSAMOUNT else -TRANSAMOUNT end)
      from t left join CATEGORY_V1 g on g.CATEGID=t.CATEGID
      where TRANSCODE<>'Transfer' and t.d>=? and t.d<=? group by 1 order by 2""", (debut_mois, auj.isoformat())).fetchall()
    print(f"Mois en cours (depuis le {auj.replace(day=1).strftime('%d/%m')}) par catégorie :")
    revolut = 0.0
    for cat, s in rows:
        print(f"  - {cat} : {eur(s)}")
        if "REVOLUT" in cat.upper():
            revolut = -s
    jour = auj.day
    print(f"  Revolut : {eur(revolut)} sur un budget de {eur(BUDGET_REVOLUT)} ({revolut / BUDGET_REVOLUT:.0%} au jour {jour})\n")
    if revolut > BUDGET_REVOLUT:
        alertes.insert(0, f"budget Revolut dépassé : {eur(revolut)} / {eur(BUDGET_REVOLUT)}")

    fin = (auj + dt.timedelta(days=7)).isoformat()
    prevus = c.execute("""
      select substr(b.NEXTOCCURRENCEDATE,1,10), b.ACCOUNTID, b.TOACCOUNTID, b.TRANSCODE, b.TRANSAMOUNT, coalesce(p.PAYEENAME,''), coalesce(b.NOTES,''), 'échéancier'
      from BILLSDEPOSITS_V1 b left join PAYEE_V1 p on p.PAYEEID=b.PAYEEID
      where substr(b.NEXTOCCURRENCEDATE,1,10) > ? and substr(b.NEXTOCCURRENCEDATE,1,10) <= ?
      union all
      select t.d, t.ACCOUNTID, t.TOACCOUNTID, t.TRANSCODE, t.TRANSAMOUNT, coalesce(p.PAYEENAME,''), coalesce(t.NOTES,''), 'déjà saisie'
      from t left join PAYEE_V1 p on p.PAYEEID=t.PAYEEID
      where t.d > ? and t.d <= ?
      order by 1""", (auj.isoformat(), fin, auj.isoformat(), fin)).fetchall()
    print("Mouvements prévus dans les 7 prochains jours :")
    besoin = {}
    for d, acc, vers, code, mt, tiers, notes, source in prevus:
        nom = soldes.get(acc, (str(acc), 0))[0]
        if code == "Transfer":
            dest = soldes.get(vers, (str(vers), 0))[0]
            print(f"  - {d} | {nom} → {dest} | {eur(mt)} | virement {notes.strip()} ({source})")
            besoin[acc] = besoin.get(acc, 0) - mt
            besoin[vers] = besoin.get(vers, 0) + mt
        elif code == "Deposit" and tiers.strip().lower() in REVENUS_INCERTAINS:
            print(f"  - {d} | {nom} | +{eur(mt)} | {tiers} ({source}, revenu incertain : NON compté)")
        else:
            signe = "+" if code == "Deposit" else "-"
            print(f"  - {d} | {nom} | {signe}{eur(mt)} | {tiers} {notes.strip()} ({source})")
            besoin[acc] = besoin.get(acc, 0) + (mt if code == "Deposit" else -mt)
    if not prevus:
        print("  (rien)")
    for acc, delta in besoin.items():
        nom, s = soldes.get(acc, (str(acc), 0))
        if s + delta < 0:
            alertes.insert(0, f"DÉCOUVERT prévu sur « {nom} » d'ici 7 jours : {eur(s)} puis {eur(s + delta)}")
    print()

    vieux = c.execute("select count(*) from t where coalesce(STATUS,'')<>'R' and d < ?",
                      ((auj - dt.timedelta(days=30)).isoformat(),)).fetchone()[0]
    if vieux:
        alertes.append(f"{vieux} opération(s) de plus de 30 jours non rapprochée(s)")

    print("Alertes :")
    for x in alertes or ["aucune"]:
        print(f"  - {x}")
    resume = f"Comptes {auj.strftime('%d/%m')} : courants {eur(courants)}, livrets {eur(livrets)}, Revolut {eur(revolut)}/{eur(BUDGET_REVOLUT)}"
    resume += f". {len(nouv)} nouvelle(s) op." + (f" ⚠ {alertes[0]}" if alertes else " RAS")
    print("\nRÉSUMÉ TÉLÉPHONE : " + resume[:200])


if __name__ == "__main__":
    sys.exit(main())
