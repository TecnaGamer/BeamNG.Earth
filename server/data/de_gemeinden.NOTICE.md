# German Gemeinden — Statistisches Bundesamt (Destatis)

`de_gemeinden.json.gz` lists Germany's Gemeinden by Kreis and which are a Stadt or a Markt, as `[Kreis, Gemeinde,
"S" | "M" | "G"]` rows. The town signs use it for the small line of an Ortsteil's Ortstafel: "Stadt Schrobenhausen",
"Markt Donaustauf", any other "Gemeinde Barbing" (`server/townentry.py` de_kind), and to tell which of a village's
places is its Gemeinde (de_town). `tools/build_de_gemeinden.py` makes it from the Gemeindeverzeichnis GV100AD
(Gebietsstand 30.09.2026; Satzart 60, Textkennzeichen 60 Markt, 61 / 62 / 63 / 67 Stadt, 64 Gemeinde),
https://www.destatis.de/DE/Themen/Laender-Regionen/Regionales/Gemeindeverzeichnis/_inhalt.html

© Daten (im Auftrag der Herausgebergemeinschaft Statistische Ämter des Bundes und der Länder) Statistisches Bundesamt
(Destatis), 2026. Vervielfältigung und Verbreitung, auch auszugsweise, mit Quellenangabe gestattet.
