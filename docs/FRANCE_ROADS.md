# France : routes, marquages et panneaux — essai 1.8.0

Extension expérimentale de la contribution `europe-infrastructure`, préparée sur
`df381e7fdffe557e81068b471b12eee98040f4cd` (1.7.3-beta, corrections Windows incluses).
Elle ne reprend pas le code de la PR suédoise et n'ajoute aucune dépendance de jeu.

La branche est désormais fusionnée avec la base officielle 1.8.0-beta ; voir
[EUROPE_180_MERGE.md](EUROPE_180_MERGE.md). Les commandes de lancement ci-dessous
restent valables après `install.py`. Les anciens installateurs portant `_173`
correspondent à l'étape historique 1.7.3 et ne sont pas nécessaires sur ce checkout.

## Lancer sur Windows

Fermer BeamNG.drive et le serveur précédent. Après application de l'extension,
depuis le dossier `BeamNG.Earth-PR` :

```powershell
py -3.12 .\install.py
$env:BNE_FR_ROADS = 'on'
$env:BNE_FR_HOUSES = 'on'
$env:BNE_FR_HOUSES_INFER = 'on'
py -3.12 .\start.py -- --regional-poles europe
```

Accepter le remplacement de la copie du mod si demandé. Garder le serveur ouvert,
puis ouvrir le niveau BeamNG.Earth en Freeroam. Le fichier `tester_france.bat`
active ces trois réglages puis lance le serveur dans son propre environnement.
Les nouveaux modèles de maisons, leur sélection régionale et leurs limites
sont décrits dans [FRANCE_HOUSES.md](FRANCE_HOUSES.md).

Le mode est désactivé par défaut. Pour comparer avec la contribution précédente,
fermer le jeu et le serveur, puis utiliser :

```powershell
$env:BNE_FR_ROADS = 'off'
$env:BNE_FR_HOUSES = 'off'
py -3.12 .\start.py -- --regional-poles europe
```

`off` conserve le kit européen précédent ; il ne désactive pas ce kit.
Le serveur et les fichiers Lua doivent provenir du même dossier. Le lanceur
officiel `.exe` ne charge pas ce nouveau code Python. Les clés de cache tiennent
compte du code et des réglages `BNE_`, dont ceux des routes et maisons : aucune suppression du cache de téléchargements
n'est nécessaire. La première génération dans ce mode peut prendre du temps.

## Ce qui change

| Élément | Comportement |
| --- | --- |
| Sens interdit | Fonction retirée à la demande d'Alexandre. Aucun placement et aucune inscription au catalogue de modèles du jeu. Les anciens fichiers de modèles, inutilisés, restent dans le paquet pour permettre la mise à jour et la restauration des essais précédents. |
| Cédez-le-passage français | Remplace l'apparence des panneaux existants par un AB3a avec panonceau M9c « CÉDEZ LE PASSAGE », sauf à proximité immédiate d'un feu. Les priorités et le comportement de l'IA ne changent pas. |
| Virages | Quatre modèles A1a/A1b/A1c/A1d ; supports français plats et courts (1,80 m au sommet). Estimation de virages prononcés et de successions de virages opposés sur les routes rurales ; panneaux à environ 150 m lorsque la longueur disponible le permet. |
| Largeur rurale | Le profil français remplace certaines largeurs génériques estimées, sans remplacer les largeurs cartographiées ou mesurées. La même `Spec` alimente chaussée, terrain et largeur utilisée par l'IA. |
| Lignes axiales | Blanc, unité française de 5/6/7,5 cm selon le type de route ; motifs T1/T'1, T3 sur les approches estimées de virage. Les données OSM de dépassement et de séparation des voies priment sur les estimations. |
| Lignes mixtes | Continue du côté où le dépassement est interdit, discontinue de l'autre ; lecture des interdictions avant/arrière et de la direction de la géométrie. Sans tags, estimation autour des virages prononcés ; une référence photographique particulière s'applique au chemin OSM 15954261 dans la forêt de Haguenau, voir les limites ci-dessous. |
| Rives | Suppression des lignes blanches systématiques sur les petites départementales. Estimation T2 3 m / 3,5 m sur les routes principales suffisamment larges, T4 39 m / 13 m sur autoroutes ; côté terre-plein continu. |
| Blocs décalés MRE | Deux parallélogrammes géométriques blancs, dimensions du schéma D9 : demi-largeur 10 cm, côtés longitudinaux 25 cm, décrochement 8 cm, angle 120°. Pas d'étirement d'une texture de tirets. Environ 15 m entre marques, sans autre ligne axiale ou de rive. |
| Flèches de rabattement | Flèches courbes blanches d'environ 6 m aux approches estimées des zones de virage où le dépassement se termine, avec ligne d'annonce T3. Forme géométrique représentative. |
| Poteaux ruraux | Suppression des rangées procédurales le long des routes rurales françaises ; ajout des poteaux individuels cartographiés dans la couche infrastructure Overture/OSM. La présence de la route ne déclenche aucun poteau. Le réseau haute tension et l'éclairage restent traités par leurs fonctions précédentes. |

Les poteaux utilitaires en ville sont conservés. La suppression des poteaux
procéduraux est limitée aux routes que le mod classe comme rurales en France,
avec `BNE_FR_ROADS=on`. Elle s'applique à toutes ces routes, et pas seulement aux
deux exemples photographiés ; la classification urbain/rural vient des données.

### Poteaux cartographiés dans toute la France

`mappedpoles.py` complète le traitement des infrastructures dans le constructeur
commun des tuiles (construction en direct, précalcul et réutilisation). Avec
`BNE_FR_ROADS=on` et le kit `--regional-poles europe` ou `fr-varied` :

- Les points `power_pole`, `utility_pole` et `communication_pole` de la couche
  infrastructure existante donnent les coordonnées et le côté cartographiés des
  supports. Les poteaux réservés à l'éclairage sont exclus de cet ajout.
- La position doit se trouver en France métropolitaine et hors de la zone
  classée urbaine par le mod. Il n'y a aucun nom de route, identifiant de village
  ou exception locale dans cette sélection.
- Un matériau renseigné `wood` ou `concrete` sélectionne le modèle français
  correspondant ; une hauteur valide renseigne son échelle. Sans ces précisions,
  l'apparence et la hauteur utilisent les préréglages représentatifs du kit.
  L'armement électrique n'est pas une reproduction de chaque réseau.
- Les câbles relient uniquement des supports cartographiés sur une même ligne
  `minor_line`, ou sur un `cable` / `communication_line` explicitement aérien.
  Les portées restent entre 5 et 80 m, sans franchir un support exclu ni couper
  un virage sans support. Un poteau isolé n'est pas relié au voisin le plus proche.
- Une ligne sans points de support ne provoque pas de poteaux à intervalle estimé.
  Les ouvrages souterrains, immergés ou désaffectés sont exclus ; les supports
  déjà représentés aux sommets des lignes haute tension ne sont pas doublés.
- Les positions et l'attribution de chaque support/portée à une seule tuile sont
  stables lors d'une revisite, d'un précalcul ou d'un changement d'ancrage.

La source est le millésime Overture chargé par le serveur, avec ses tags OSM ;
ce n'est pas une interrogation d'OSM en direct. Cet ajout ne lance pas de
téléchargement API supplémentaire et ne réactive pas les poteaux ruraux tirés
au hasard. Une absence dans cette source reste une information inconnue, et non
une preuve d'un réseau enterré ou de l'absence réelle de poteaux.

**Limite de l'exemple de Gunstett :** à proximité du point `48.91623, 7.78231`
fourni sur la capture, la consultation de l'API OSM du 3 novembre 2026 ne
contient pas la rangée de petits poteaux visible sur Street View. Les supports
électriques les plus proches dans cette réponse appartiennent aux lignes haute
tension voisines, à plusieurs centaines de mètres. Cette correction ne prétend
donc pas rétablir cette rangée non cartographiée. Le cas extrait et son URL
figurent dans `server/tests/fixtures/gunstett_mapped_power.json` ; aucune photo
n'a servi à ajouter des coordonnées dans la base. Les petits supports visibles
sur une photographie peuvent aussi porter des télécommunications.

### Corrections après l'essai en jeu du 3 novembre

- Le découpage de peinture conserve désormais les morceaux de plus de 2 cm,
  au lieu d'appliquer le seuil routier de 3 m : ce seuil supprimait les tirets
  urbains de 1,5 m dès qu'une autre route figurait dans la tuile. Les lignes
  traversent aussi une simple coupure cartographique entre deux tronçons alignés.
  Les véritables bras d'intersection continuent de couper la peinture.
- Un matériau blanc opaque propre au mode France remplace le matériau à texture
  des lignes génériques. Les UV des petits polygones MRE et des flèches ne
  peuvent plus tomber dans une partie transparente de cette texture.
- Une tuile contenant cette peinture emploie une tolérance de suivi du sol de
  2 mm pour la chaussée et la peinture. Les approximations indépendantes de
  2 cm pouvaient auparavant masquer une ligne placée seulement 1 cm au-dessus
  de la route. Un cas reproduit avec le maillage natif passe de −5,2 mm sous
  l'asphalte à +10,4 mm au-dessus. Ce suivi augmente le nombre de triangles
  (80 à 480 sur ce petit exemple) ; son coût en partie reste à mesurer.
- Les quatre chemins OSM testés de la D503 portent `lane_markings=no`.
  Cette information décrit l'absence de séparation des voies ; elle n'interdit
  plus le MRE de guidage sur une route étroite non divisée en voies.

Les nouveaux panneaux utilisent trois LOD, des matériaux enregistrés côté jeu et
des collisions simples pour les supports. Les faces sont dessinées par un
générateur, sans pixels prélevés dans Plonk It ou Street View. Le texte emploie le
DejaVu déjà livré dans la contribution (licence dans `tools/fonts/DejaVu-LICENSE.txt`),
comme substitut visuel à l'alphabet officiel. Génération reproductible :
`py -3.12 tools/build_french_traffic.py`.

### Largeurs de repli — estimations de décor

| Route rurale, sans largeur ni nombre de voies renseignés | Largeur de chaussée |
| --- | ---: |
| Primary | 7,0 m |
| Secondary | 6,0 m |
| Tertiary | 5,0 m |
| Unclassified | 4,5 m |
| Residential | 5,0 m |
| Service | 3,5 m |

Deux voies sur une route bidirectionnelle conservent le repli de sa classe : ce
nombre de voies n'est pas une largeur mesurée de 6 m. Les autres nombres de voies
renseignés utilisent 3 m par voie pour ce repli. Les voies à sens
unique, bretelles, chaussées séparées et giratoires conservent leurs règles
spécifiques ; une largeur explicitement cartographiée reste prioritaire. Le repli
ne remodèle pas les rues urbaines ni les tabliers de ponts et tunnels.

Le MRE de repli concerne les routes rurales bidirectionnelles de classes primary,
secondary, tertiary et unclassified, larges de 4 à moins de 5,20 m. Ce seuil bas et
le choix de classes sont une politique de décor : la réglementation autorise ce
marquage sans imposer sa présence. Les chemins de service restent exclus.
`lane_markings=no` supprime les séparations de voies, mais conserve le MRE sur
les routes étroites éligibles et les éventuelles marques de rive indépendantes.

## Limites précises

- Ce n'est pas une reproduction relevée de chaque route française. Le tracé vient
  toujours d'Overture/OSM ; cette extension améliore les largeurs et la distribution
  des marquages, pas la totalité des terrassements ou de la géométrie routière.
- Les données ne décrivent pas partout les marquages ni leurs changements au
  mètre près. Le chemin OSM 15954261 (ancienne D27, aujourd'hui D927) reçoit une
  ligne mixte représentative des photos, continue du côté de la circulation vers
  le nord-ouest, sans rives. Cette référence concerne un tronçon identifié de
  3,8 km : les transitions réelles des autorisations de dépassement sur toute
  cette longueur n'ont pas été relevées. Les données OSM explicites priment sur
  ce choix de décor et l'inversion du sens de la géométrie est traitée.
  Les panneaux de virage et flèches restent des estimations, pas un inventaire.
- Les virages sont détectés sur chaque partie routière entière. Une route découpée
  en segments courts peut manquer de longueur pour annoncer un virage à 150 m ;
  le panneau est alors omis. Les données de pente, visibilité et danger réel ne
  suffisent pas à classer les virages comme un gestionnaire routier.
- Les règles OSM connues s'appliquent au segment fourni par le jeu. Une restriction
  découpée différemment dans la source pourra être moins précise dans le jeu.
- Le MRE est une sélection automatique sur les routes éligibles, pas une preuve
  que toutes ces routes le portent. Sur la D503, le résultat dépend aussi de la
  largeur/nombre de voies présents dans la carte.
- Un tronçon qui traverse une frontière ou dont le pays est incertain conserve
  le comportement précédent. La sélection France teste les sommets et milieux des
  segments sur les polygones du kit, sans deviner le pays à partir d'un aéroport.
- Les panneaux n'ajoutent pas de règles de circulation à l'IA ; ils suivent les
  informations de sens unique existantes. Rien n'est ajouté pour les plaques
  d'immatriculation.
- Les modèles et exports sont testés hors jeu. Le chargement/import des nouveaux
  COLLADA, l'éclairage et les performances en partie restent à vérifier sur
  Windows/BeamNG.drive. Cette extension ne corrige pas le crash Vulkan observé
  précédemment.

## Parcours de validation en jeu

1. D503/Rue de Lembach entre Wingen et Lembach, `49.01471, 7.79754` : largeur, blocs MRE,
   absence de rives systématiques et des poteaux ruraux procéduraux. Attendre la
   fin du chargement avant la comparaison ; un nombre de voies ou une largeur
   cartographiée peut modifier la sélection du MRE.
2. Ancienne D27 dans la forêt de Haguenau : largeur, ligne mixte et absence des
   rives systématiques. La position exacte des transitions reste estimée.
3. Entrée de giratoire français : triangle inversé, texte et accent du M9c,
   orientation face à l'approche, absence de panneau au milieu de la voie.
4. Boulevard de l'Europe/D263, `48.81489, 7.78360`, puis une chaussée séparée :
   présence des tirets courts, continuité à travers les coupures de tronçons et
   visibilité de la peinture depuis la voiture et en caméra aérienne.
5. Longues départementales sinueuses : A1a/A1b et successions A1c/A1d, flèches
   courbes dirigées vers la voie de droite, pas de rangée de panneaux sur un virage.
6. Frontière France/Allemagne, autre pays européen, puis mode `off` : portée
   géographique, retour aux règles précédentes et absence de mélange de cache.
7. Revisiter une zone précalculée puis téléporter à proximité : persistance des
   marquages et des modèles, absence de doublons aux limites de tuiles.

## Validation automatisée

`test_france_roads` contient 40 tests : frontières réelles, conservation des
largeurs de carte et mesurées, cohérence des voies, directions des lignes mixtes,
dimensions MRE, motifs de rive, absence de sens interdits, orientation des
panneaux, modèles/UV/matériaux, génération reproductible, construction native
de tuile, cache séparé, précalcul/réutilisation et rebase. Les régressions de
peinture couvrent les géométries/tags OSM réels des trois exemples, les tirets
courts dans une tuile complète, les coupures alignées, les intersections et la
hauteur de la peinture au-dessus des triangles réels d'asphalte.

Ces données OSM servent de cas reproductibles, pas de copie du cache Overture
utilisé chez Alexandre. Le téléchargement de ce cache n'a pas abouti ici ;
les vérifications natives ne remplacent pas le nouvel essai dans BeamNG.drive.

La comparaison des modèles régénérés utilise une tolérance absolue de `1e-12`
sur les tableaux flottants COLLADA, pour les arrondis proches de zéro entre
Windows et Linux. Les structures XML, indices et matériaux restent vérifiés ;
les changements de géométrie sont détectés. Les textures sont comparées pixel
par pixel après décodage PNG, indépendamment de leur compression. Un test
supplémentaire confirme la conservation des poteaux utilitaires en ville.

Le texte M9c est composé à partir des pixels de l'atlas DejaVu déjà livré avec
le kit (`regional_signs/sign_font_opacity.data.png` et `server/data/sign_font.json`),
avec positions et dimensions explicites. Le générateur n'utilise plus le moteur
de polices TrueType du système pour cette texture : FreeType/RAQM ne peuvent
donc plus modifier le rendu des lettres sur une autre machine. La comparaison
pixel par pixel reste stricte. Un test vérifie la génération du panonceau même
quand le chargement des polices système est indisponible.

Le dernier échec Windows a été reproduit précisément avec l'ancien générateur :
le moteur Pillow BASIC produit 53 909 composantes différentes sur 1 048 576,
avec les mêmes cinq premiers écarts que le journal Windows ; RAQM reproduit
l'ancienne texture. Le nouveau générateur produit zéro différence avec les
pixels livrés sous Pillow 12.3.0 et 13.0.0, sans passer par ces moteurs. Les 40
tests du module ont été exécutés avec ces deux versions de Pillow sur Linux ;
le contrôle Windows et l'essai en jeu restent à effectuer chez le contributeur.

`test_mapped_poles` ajoute 17 tests de placement et d'intégration : positions,
bois/béton et hauteur, câbles connus ou enterrés, télécommunications, supports
isolés, absence de poteaux inventés, conservation de la ville et des autres pays,
déduplication avec les pylônes, limites de tuiles, changement d'ancrage, requêtes
de hauteur locales et exemple OSM réel de Gunstett. Un précalcul complet teste
aussi l'export natif du câble et la conservation des supports lors de la
réutilisation puis de deux changements d'ancrage. Le rendu et les performances
dans BeamNG.drive restent à valider par Alexandre.

24 modules ciblés sont vérifiés pour l'extension et le kit précédent, y compris
`test_powerlines` pour le réseau haute tension existant, `test_france_houses`
et les 74 tests existants de `test_buildings`. Les deux
tests d'intégration qui exigent un cache réel de Baltimore sont ignorés par le
lanceur d'installation ; l'accord Lua est également ignoré si LuaJIT n'est pas
installé. Ils ne sont pas comptés comme réussis.

Un contrôle plus large de `test_roads` a relevé un échec préexistant :
`PaintRoadsTest.test_bare_verge_beside_roads` (`9 != 0`, au bord d'une route de 8 m).
Le même échec a été reproduit sur le commit de départ `df381e7`, sans cette
extension. Il n'est pas modifié ou masqué ici.

## Sources et choix visuels

- [Cerema, officialisation du marquage des routes étroites (27 mars 2024)](https://www.cerema.fr/fr/actualites/officialisation-du-marquage-routes-etroites).
- [Arrêté du 15 mars 2024, article 16 : IISR 118-13 et annexe D9](https://www.legifrance.gouv.fr/jorf/article_jo/JORFARTI000049314226).
- [Ministère, circulaire du 7 juillet 1992, schéma coté du MRE conservé par Cerema](https://www.cerema.fr/system/files/documents/2021/12/pdf_3-marquage_voie_etroite.pdf).
- [IISR, versions consolidées des neuf parties (Cerema)](https://equipementsdelaroute.cerema.fr/versions-consolidees-des-9-parties-de-l-a528.html), notamment parties 2, 3 et 7.
- [Arrêté du 24 novembre 1967, AB3a, M9c et panneaux A1/B1 (Légifrance)](https://www.legifrance.gouv.fr/loda/id/LEGITEXT000006075080/2024-04-15).
- Recherche déjà présente : `docs/research/2026-10-28-road-markings-standards.md`.
- [Pillow, ImageFont : FreeType et choix du moteur de texte selon sa disponibilité](https://pillow.readthedocs.io/en/stable/reference/ImageFont.html). Le générateur M9c évite cette dépendance au rendu de l'hôte en utilisant les glyphes déjà rasterisés du kit.
- [OpenStreetMap, signification de lane_markings](https://wiki.openstreetmap.org/wiki/Key:lane_markings).
- [Chemin OSM 15954261, ancienne D27](https://www.openstreetmap.org/way/15954261).
  `server/tests/fixtures/france_road_examples.json` contient les géométries et
  tags utiles de sept chemins consultés le 3 novembre 2026 via l'API officielle,
  avec leurs URL sources. © OpenStreetMap contributors, licence ODbL 1.0.
- [Overture, classes des infrastructures](https://docs.overturemaps.org/schema/reference/base/types/infrastructure_class/) et [géométries/tags sources](https://docs.overturemaps.org/schema/reference/base/infrastructure/).
- [OSM, poteaux électriques](https://wiki.openstreetmap.org/wiki/Tag:power%3Dpole), [poteaux utilitaires](https://wiki.openstreetmap.org/wiki/Tag:man_made%3Dutility_pole) et [réseaux aériens français](https://wiki.openstreetmap.org/wiki/Power_networks/France/Aerien).
- `server/tests/fixtures/gunstett_mapped_power.json` : extrait géographique de
  l'API OSM autour de la capture, avec provenance et attribution ODbL.
- Captures fournies par Alexandre (Plonk It, D27, D503) utilisées comme références
  visuelles uniquement. Aucune photographie n'est distribuée dans l'extension.
