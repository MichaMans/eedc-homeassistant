"""Prüfstand Wärme/Klima — nachgestellte Anlagen für HAOS-Lab und Demo-DB (WK-15).

Ein Datenmodul (`daten.py`), zwei Schreiber:

* ``scripts/seed-pruefstand-waerme-klima.py --db PFAD`` schreibt direkt in eine
  SQLite-Datei (Demo-DB r28),
* dasselbe Skript mit ``--api URL`` schreibt über die echten Routen
  (HAOS-Lab, nur Stdlib — es läuft notfalls auf dem Lab selbst).

Die Geräte bilden die Lagen aus ``docs/HANDBUCH_WAERME_KLIMA.md`` §6 nach.
"""
