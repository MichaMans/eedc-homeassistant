#!/usr/bin/env python3
"""Installationen im Zugriffslog des Community-Servers zählen — je Tag und Variante.

Liest (nur lesend) die Access-Logs des Nginx Proxy Managers auf dem
Docker-Server, filtert die API-Zeilen von energy.raunet.eu und schreibt je
**abgeschlossenem** Tag eine Zeile pro User-Agent-Familie nach
``~/claude/github-traffic/community-zugriffe.csv``:

    date,ua,anfragen,adressen,anlagen_hashes,submits_ok,submits_fehler

* ``ua`` — ``eedc-homeassistant/<v>`` und ``eedc/<v>`` (ab dem Release mit dem
  eigenen User-Agent, 17.09.2026), ``python-httpx`` (ältere eedc-Clients) oder
  ``browser`` (die Website).
* ``adressen`` — verschiedene Client-Adressen. ⚠ Vor dem Netz-Umbau am 16.09.2026
  stand bei jeder Zeile die Docker-Gateway-Adresse ``172.18.0.1``; ältere Tage
  zählen deshalb 1. Adressen wechseln bei Privatanschlüssen täglich — sie sind
  Adress-Tage, keine Installationen.
* ``anlagen_hashes`` — verschiedene Hashes in ``GET /api/benchmark/anlage/<hash>``:
  Installationen, die geteilt haben und die Community-Seite öffneten. Das ist
  der belastbarste Zähler, unabhängig von Adressen.
* ``submits_ok`` / ``submits_fehler`` — ``POST /api/submit`` mit 200 bzw. allem
  anderen (400/422/keine Antwort).

Der Proxy rotiert seine Logs etwa alle vier Tage; der tägliche Lauf aus
``github-traffic.sh`` reicht, um nichts zu verlieren. Ein Tag wird nur
geschrieben, wenn er im Log vollständig ist (gestern und älter) und noch nicht
in der CSV steht — der Lauf ist wiederholbar.

Aufruf:  python3 scripts/community-zugriffe.py [--host gernot@192.168.1.3] [--nur-anzeigen]
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

CSV_PFAD = Path.home() / "claude" / "github-traffic" / "community-zugriffe.csv"
SPALTEN = ["date", "ua", "anfragen", "adressen", "anlagen_hashes", "submits_ok", "submits_fehler"]
HOST_DEFAULT = "gernot@192.168.1.3"
NPM_CONTAINER = "nginxproxymanager-app-1"
COMMUNITY_HOST = "energy.raunet.eu"

# Zeilenformat des NPM-Access-Logs (proxy-host-*_access.log):
# [17/Sep/2026:10:59:42 +0000] - 400 400 - POST https energy.raunet.eu "/api/submit"
#   [Client 46.5.202.88] [Length 59] [Gzip -] [Sent-to eedc-community-api] "python-httpx/0.28.1" "-"
ZEILE = re.compile(
    r'^\[(?P<ts>[^\]]+)\] - (?P<status>[0-9-]+) [0-9-]+ - (?P<meth>\w+) https (?P<host>\S+) "(?P<pfad>[^"]+)" '
    r'\[Client (?P<ip>[^\]]+)\] \[Length [^\]]*\] \[Gzip [^\]]*\] \[Sent-to [^\]]*\] "(?P<ua>[^"]*)" "[^"]*"'
)
HASH_PFAD = re.compile(r"^/api/benchmark/anlage/([0-9a-f]{64})")


def log_zeilen(host: str) -> list[str]:
    """Alle API-Zeilen des Community-Hosts aus den aktuellen und rotierten Logs — lesend."""
    remote = (
        f'docker exec {NPM_CONTAINER} sh -c "cd /data/logs; '
        f"(cat proxy-host-*_access.log; zcat proxy-host-*_access.log.*.gz 2>/dev/null) "
        f"| grep -F 'https {COMMUNITY_HOST} \\\"/api/'\""
    )
    ergebnis = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, remote],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if ergebnis.returncode not in (0, 1):  # grep: 1 = keine Treffer
        sys.exit(f"ssh/docker fehlgeschlagen ({ergebnis.returncode}): {ergebnis.stderr.strip()[:300]}")
    return ergebnis.stdout.splitlines()


def ua_familie(ua: str) -> str:
    if ua.startswith(("eedc-homeassistant/", "eedc/")):
        return ua
    if ua.startswith("python-httpx"):
        return "python-httpx"
    return "browser"


def auswerten(zeilen: list[str]) -> dict[tuple[date, str], dict]:
    je_tag_ua: dict[tuple[date, str], dict] = defaultdict(
        lambda: {"anfragen": 0, "adressen": set(), "hashes": set(), "submits_ok": 0, "submits_fehler": 0}
    )
    for raw in zeilen:
        m = ZEILE.match(raw.strip())
        if not m:
            continue
        tag = datetime.strptime(m["ts"].split(" ")[0], "%d/%b/%Y:%H:%M:%S").date()
        eintrag = je_tag_ua[(tag, ua_familie(m["ua"]))]
        eintrag["anfragen"] += 1
        eintrag["adressen"].add(m["ip"])
        h = HASH_PFAD.match(m["pfad"])
        if h and m["meth"] == "GET":
            eintrag["hashes"].add(h.group(1))
        if m["meth"] == "POST" and m["pfad"] == "/api/submit":
            if m["status"] == "200":
                eintrag["submits_ok"] += 1
            else:
                eintrag["submits_fehler"] += 1
    return je_tag_ua


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default=HOST_DEFAULT)
    parser.add_argument("--nur-anzeigen", action="store_true", help="nichts schreiben")
    args = parser.parse_args()

    je_tag_ua = auswerten(log_zeilen(args.host))
    heute = date.today()
    vorhanden: set[tuple[str, str]] = set()
    if CSV_PFAD.exists():
        with CSV_PFAD.open(encoding="utf-8") as f:
            vorhanden = {(z["date"], z["ua"]) for z in csv.DictReader(f)}

    neue = []
    for (tag, ua), e in sorted(je_tag_ua.items()):
        if tag >= heute:  # der laufende Tag ist unvollständig
            continue
        if (tag.isoformat(), ua) in vorhanden:
            continue
        neue.append([tag.isoformat(), ua, e["anfragen"], len(e["adressen"]), len(e["hashes"]),
                     e["submits_ok"], e["submits_fehler"]])

    for zeile in neue:
        print(",".join(str(x) for x in zeile))
    if args.nur_anzeigen:
        print(f"[{heute}] community-zugriffe: {len(neue)} neue Zeile(n) — nicht geschrieben")
        return

    CSV_PFAD.parent.mkdir(parents=True, exist_ok=True)
    neu_anlegen = not CSV_PFAD.exists()
    with CSV_PFAD.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if neu_anlegen:
            w.writerow(SPALTEN)
        w.writerows(neue)
    print(f"[{heute}] community-zugriffe: {len(neue)} neue Zeile(n) -> {CSV_PFAD}")


if __name__ == "__main__":
    main()
