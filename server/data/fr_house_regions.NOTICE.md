# Contours utilisés pour les maisons françaises

`fr_house_regions.zip` contient des contours administratifs issus du jeu
[Contours administratifs](https://www.data.gouv.fr/datasets/contours-administratifs)
publié par Etalab / data.gouv.fr, à partir notamment d'IGN ADMIN EXPRESS.
La fiche du jeu les distribue sous
[Open Database License 1.0 (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).
Attribution : Etalab / data.gouv.fr, IGN ADMIN EXPRESS, et contributeurs
OpenStreetMap pour les données dont ils sont la source.

Les trois sources récupérées le 3 novembre 2026 sont :

- [Départements 2026, simplification 100 m](https://etalab-datasets.geo.data.gouv.fr/contours-administratifs/2026/geojson/departements-100m.geojson).
- [Communes de la Communauté d'agglomération du Pays basque, EPCI 200067106](https://geo.api.gouv.fr/epcis/200067106/communes?fields=nom,code,contour&format=geojson&geometry=contour).
- [Communes de Toulouse Métropole, EPCI 243100518](https://geo.api.gouv.fr/epcis/243100518/communes?fields=nom,code,contour&format=geojson&geometry=contour).

Le paquet conserve les géométries, codes et noms, trie les entités et ne retient
que les 96 départements métropolitains. Les autres fichiers contiennent
respectivement 158 et 37 communes. Les URL, empreintes SHA-256 des sources,
attribution, licence et date sont aussi incluses dans `sources.json`.
Les données GeoJSON extraites du ZIP restent disponibles sous ODbL ; les choix
de styles du code ne sont pas des données officielles d'architecture.

Empreinte SHA-256 du ZIP livré :
`18db140bedce39d9eef1dd9e899eee305dee5c823b9c3e792e1683969991e86c`.
Reconstruction : `tools/build_france_house_regions.py --source-dir CHEMIN` avec
les trois sources indiquées dans ce script. Aucune requête à ces API n'est
nécessaire au fonctionnement du jeu.

Les limites départementales et intercommunales servent de zonage pratique.
Elles ne sont pas présentées comme des frontières précises de styles culturels.
