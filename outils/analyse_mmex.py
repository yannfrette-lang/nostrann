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

# Rôle de chaque compte courant (règle de Yann du 15/11/2026) :
#  - n° 1 « CHARGES VALERIE » : uniquement les charges (crédits, assurances, énergie, eau,
#    abonnements, impôts) ; après la fin des crédits immobiliers, les travaux au fur et à mesure.
#  - n° 2 « ALIMENTATION YANN » : nourriture, essence, coiffeur, vêtements, cadeaux, restaurants,
#    santé (médecins, pharmacie, Afflelou), tabac, achats Amazon, argent donné à Adèle et Pierre.
# Les abonnements Amazon classés en charges fixes restent sur le n° 1.
VIE_COURANTE = ("carrefour", "intermarch", "leclerc", "aldi", "lidl", "auchan", "grand frais",
                "boulangerie", "patapain", "feuillette", "melvana", "restaurant", "mc donald",
                "dominos", "coiffeur", "kiabi", "total", "esso", "carburant", "essence",
                "station", "cadeau", "revolut",
                # santé
                "pharmacie", "afflelou", "opticien", "médecin", "medecin", "docteur", "dermatologue",
                "podologue", "dentiste", "kiné", "kine", "laboratoire", "clinique", "radiolog", "cpam",
                # tabac, Amazon, enfants
                "tabac", "cigarette", "amazon", "adele", "adèle", "pierre")
CHARGES = ("maif", "edf", "veolia", "orange", "canal", "sofinco", "cofidis", "mobilize",
           "stelantis", "stellantis", "topolino", "tresor public", "diagamter", "notaire",
           "brico", "leroy merlin", "travaux", "froeling", "epson", "microsoft", "google",
           "apple", "ring alarme", "soch", "apgis")


def hors_role(compte, categorie, tiers, montant):
    """Renvoie la raison si la dépense n'est pas sur le bon compte, sinon None."""
    n, cat, t = compte.upper(), (categorie or "").upper(), (tiers or "").strip().lower()
    if "CHARGES" in n and "CHARGES FIXES" not in cat and (
            "REVOLUT" in cat or t.startswith("dr ") or any(k in t for k in VIE_COURANTE)):
        return "dépense de vie courante : à payer par le compte n° 2 Alimentation"
    if "ALIMENTATION" in n:
        if t.startswith("crédit agricole") and montant < 20:
            return None  # frais de tenue du compte n° 2 lui-même
        if "amazon" in t and "CHARGES FIXES" not in cat:
            return None  # achats Amazon : compte n° 2 (abonnement Prime : charge du n° 1)
        if "CHARGES FIXES" in cat or "CHAILLES" in cat or any(k in t for k in CHARGES):
            return "charge ou travaux : à payer par le compte n° 1 Charges"
    return None


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
    ecarts = c.execute("""
      select t.d, a.ACCOUNTNAME, coalesce(g.CATEGNAME,''), coalesce(p.PAYEENAME,''), t.TRANSAMOUNT,
             substr(t.LASTUPDATEDTIME,1,10) >= ?
      from t join ACCOUNTLIST_V1 a on a.ACCOUNTID=t.ACCOUNTID
      left join PAYEE_V1 p on p.PAYEEID=t.PAYEEID left join CATEGORY_V1 g on g.CATEGID=t.CATEGID
      where t.TRANSCODE='Withdrawal' and t.d>=? and t.d<=? order by t.d""",
                       (depuis.isoformat(), debut_mois, auj.isoformat())).fetchall()
    ecarts = [(d, cpt, tiers, mt, raison, recent) for d, cpt, cat, tiers, mt, recent in ecarts
              if (raison := hors_role(cpt, cat, tiers, mt))]
    # Une dépense payée avec la carte de l'autre compte est en règle si le bon compte la rembourse
    # par virement (ex. Diagamter, Claude, Epson remboursés par le n° 1 ; courses remboursées par le n° 2) :
    # même montant, nom noté dans le libellé du virement, ou somme de deux dépenses.
    virements = [[dt.date.fromisoformat(d), round(mt, 2), (notes or "").lower(), False, a, b]
                 for d, mt, notes, a, b in c.execute("""
      select t.d, t.TRANSAMOUNT, t.NOTES, a.ACCOUNTNAME, b.ACCOUNTNAME from t
      join ACCOUNTLIST_V1 a on a.ACCOUNTID=t.ACCOUNTID join ACCOUNTLIST_V1 b on b.ACCOUNTID=t.TOACCOUNTID
      where t.TRANSCODE='Transfer' order by t.d""")]
    rembourses = set()

    def candidats(e):
        return [v for v in virements if not v[3] and v[5] == e[1] and v[4] != e[1]
                and ("CHARGES" in v[4].upper() or "ALIMENTATION" in v[4].upper())
                and abs((v[0] - dt.date.fromisoformat(e[0])).days) <= 31]

    for e in ecarts:
        cle = "".join(ch for ch in e[2].lower() if ch.isalpha())[:4]
        for v in candidats(e):
            if abs(v[1] - e[3]) < 0.01 or (len(cle) == 4 and cle in v[2]):
                v[3] = True
                rembourses.add(e)
                break
    reste = [e for e in ecarts if e not in rembourses]
    for i, e in enumerate(reste):
        for f in reste[i + 1:]:
            if e[1] != f[1] or e in rembourses or f in rembourses:
                continue
            v = next((v for v in candidats(e) if abs(v[1] - e[3] - f[3]) < 0.01), None)
            if v:
                v[3] = True
                rembourses.update((e, f))
    ecarts = [e for e in ecarts if e not in rembourses]
    print("Rôle des comptes (n° 1 = charges, n° 2 = vie courante), mois en cours :")
    for d, cpt, tiers, mt, raison, recent in sorted(rembourses):
        print(f"  - {d} | {cpt} | -{eur(mt)} | {tiers.strip()} → remboursé par virement de l'autre compte : OK")
    for d, cpt, tiers, mt, raison, recent in ecarts:
        print(f"  - {d} | {cpt} | -{eur(mt)} | {tiers.strip()} → {raison}")
        if recent:
            alertes.append(f"mauvais compte : {tiers.strip()} {eur(mt)} sur « {cpt} »")
    if not ecarts:
        print("  (tout est sur le bon compte)")
    print()

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

    # Indicateurs de gestion « en bon père de famille »
    def mois_precedent(d, n):
        y, m = d.year, d.month - n
        while m <= 0:
            m += 12
            y -= 1
        return dt.date(y, m, 1)

    def flux(debut, fin_excl):
        r = c.execute("""
          select coalesce(g.CATEGNAME,'?'), sum(case TRANSCODE when 'Deposit' then TRANSAMOUNT else -TRANSAMOUNT end)
          from t left join CATEGORY_V1 g on g.CATEGID=t.CATEGID
          where TRANSCODE<>'Transfer' and t.d>=? and t.d<? group by 1""", (debut.isoformat(), fin_excl.isoformat())).fetchall()
        rev = sum(s for cat, s in r if any(k in cat.upper() for k in ("SALAIRE", "REVENU", "PLACEMENT")))
        dep = -sum(s for cat, s in r if cat.upper().startswith("A") or cat == "?")
        return rev, dep

    debut_m = auj.replace(day=1)
    stats = []
    for n in (3, 2, 1):
        d0 = mois_precedent(debut_m, n)
        d1 = mois_precedent(debut_m, n - 1) if n > 1 else debut_m
        stats.append((d0, *flux(d0, d1)))
    print("Indicateurs (3 derniers mois complets, hors virements internes, achat immobilier et épargne) :")
    for d0, rev, dep in stats:
        taux = (rev - dep) / rev if rev else 0
        print(f"  - {d0.strftime('%m/%Y')} : revenus {eur(rev)} | dépenses courantes {eur(dep)} | solde {eur(rev - dep)} | taux d'épargne {taux:.0%}")
    dep_moy = sum(s[2] for s in stats) / 3
    rev_moy = sum(s[1] for s in stats) / 3
    mois_couverts = livrets / dep_moy if dep_moy else 0
    rev_m, dep_m = flux(debut_m, auj + dt.timedelta(days=1))
    print(f"  Moyenne : revenus {eur(rev_moy)} | dépenses courantes {eur(dep_moy)} par mois")
    rythme = f"{dep_m / auj.day * 30:,.0f}".replace(",", " ")
    print(f"  Mois en cours au jour {auj.day} : revenus {eur(rev_m)} | dépenses courantes {eur(dep_m)} (rythme : {rythme} € sur 30 jours)")
    print(f"  Épargne de précaution : livrets {eur(livrets)} = {str(round(mois_couverts, 1)).replace('.', ',')} mois de dépenses courantes (objectif : 3 à 6 mois)\n")
    if mois_couverts < 3:
        alertes.append(f"épargne de précaution faible : {str(round(mois_couverts, 1)).replace('.', ',')} mois de dépenses")

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
