# Balises de rive — recherches et réglages v4

29 octobre 2026. Complément au prototype Europe v3, base publique 1.7.1-beta.
Les modèles de balises restent sans collision.

## Pourquoi elles manquaient en Allemagne

La v3 tirait au sort 35 % des seules routes `primary`/`secondary` longues
d'au moins 250 m, puis utilisait 90 m d'intervalle. Toutes les petites portions
cartographiques et les classes `tertiary`/`unclassified` étaient exclues.
La photo fournie de la B427 à Winden sert de référence visuelle, pas de statistique.

La v4 sépare **présence sur le réseau** et **espacement là où des balises sont
installées**. Un espacement réglementaire ne fournit pas un taux national.
Les guides GeoGuessr aident à reconnaître les silhouettes ; aucune fréquence
numérique n'est inventée à partir de ces guides. Les références numériques
ci-dessous viennent de documents d'administrations ou de manuels de signalisation.

## Profils ajustés

Le tableau décrit le décor inféré au sol, hors agglomération, bretelles,
autoroutes et giratoires. « Principales » signifie ici `primary`/`secondary` ;
« régionales » ajoute `tertiary` ; « rurales » ajoute `unclassified`.
Ces correspondances avec les réseaux administratifs sont des approximations.

| Pays | Éléments sourcés | Réglage du prototype |
| --- | --- | --- |
| Allemagne | StVO, signe 620 : 50 m normalement, resserrement en courbe. | Toutes les classes rurales revêtues ≥ 3,5 m, deux côtés, 50 m. Aucun tirage de présence ni seuil de 250 m. |
| Autriche | Carinthie 2019 : schéma à 33 m ; recherche ministérielle : routes importantes, sans garantie sur tout le réseau local. | Régionales revêtues ≥ 4 m, 33 m. |
| Suisse | Directive de Lucerne : généralement 50 m, moins selon courbes et visibilité. | Régionales revêtues ≥ 4 m, 50 m. Source cantonale, pas inventaire national. |
| Tchéquie | BESIP : 50 m sur les parties droites. | Rurales revêtues ≥ 3,5 m, 50 m ; tableau de courbes slovaque utilisé comme approximation. |
| Slovaquie | TP 105/2022 : implantation continue ; 50 m puis 40/30/20/10/5 m selon le rayon, côtés opposés. | Rurales revêtues ≥ 3,5 m, même tableau. Réflecteurs fixés aux glissières non ajoutés ici. |
| Danemark | N41 : normalement 100 m, rapprochement en courbe. | Régionales revêtues ≥ 4 m, 100 m. |
| Pologne | GDDKiA : notamment routes nationales et de voïvodie ; 100 m normalement, moins en courbe. | Régionales revêtues ≥ 4 m, 100 m. |
| Espagne | OC 309/90 et projet de Bizkaia : 50 m en ligne droite, densité accrue en courbe. | Régionales revêtues ≥ 4 m, vitesse connue ≥ 60 km/h ; tableau de courbes adapté. Hectomètres non numérotés. |
| Italie | DPR 495, art. 173 : besoin de guidage, continuité sur le tronçon équipé, maximum 50 m droit. | Régionales revêtues ≥ 4 m, vitesse connue ≥ 60 km/h ; courbes 6/8/12/20/30/50 m. |
| Suède | VGU 2015 et NVDB : 50 m droit, critères de vitesse, trafic et éclairage. | Principales ≥ 5,5 m, vitesse connue ≥ 80 km/h, éclairage connu exclu ; 25 m en courbe. Références historiques, pas certification des dernières VGU. |
| Norvège | N300 de 2012 : réseau principal rapide ou salé, non éclairé ; 50/25/10 m et extérieur seul dans les courbes très serrées. | Principales ≥ 6,5 m, vitesse connue ≥ 80 km/h, éclairage connu exclu. Tableau historique repris ; trafic et salage inconnus. |
| Islande | Vegagerðin 2.1 : axes principaux et liaisons fréquentées à 50 m des deux côtés ; petites routes à 100 m d'un côté. | Principales : deux côtés/50 m. Tertiaires et non classées : un côté/100 m ; largeur ≥ 3 m, gravier admis, éclairage connu exclu. Classes utilisées à défaut du trafic. |
| Royaume-Uni | TSM4 §15 : marqueurs de danger d'emploi local ; guidage aussi par lignes de rive et plots réfléchissants. | Suppression des rangées systématiques en ligne droite ; maintien des marqueurs locaux de virage v3. |
| Irlande | TSM6 §6.22 : usage là où nécessaire ; potelets possibles quand les plots de chaussée ne conviennent pas. | Emploi local en virage avec modèle irlandais. Aucune assertion d'absence totale sur les lignes droites. |

Resserrements simplifiés : Allemagne/Suisse, 25 m sous R=300 m et 10 m sous
R=100 m ; Autriche, 16,5/11 m ; Danemark, 50 m sous R=2000 m et 25 m sous
R=250 m ; Pologne, 50/25 m. Ces seuils sont des choix de décor lorsque la source
dit seulement de rapprocher les balises. Islande : intervalle divisé par deux
dans les courbes. La vitesse inconnue n'est pas traitée comme une limitation basse.
Les seuils de largeur et les classes ne sont pas des exigences légales déduites.

## France et profils encore provisoires

**France inchangée** : J6 sur 8 % des longues portions principales éligibles,
90 m ; J1/J3/J4 conservées. Ce 8 % traduit la préférence de cet essai, pas une
mesure nationale. Le Cerema ne fournit pas ce pourcentage.

Liechtenstein reprend provisoirement la Suisse. Guernesey, Gibraltar, Île de Man
et Jersey reprennent provisoirement l'emploi local britannique.

AD, AL, AX, BA, BE, BG, BY, CY, EE, FI, FO, GR, HR, HU, LT, LU, LV, MC, MD, ME,
MK, MT, NL, PT, RO, RS, RU, SI, SM, TR, UA, VA, XK, GE, AM et AZ conservent
**le réglage de présence v3 provisoire** : 35 % des longues portions principales,
90 m, plus les balises en virage. Leurs modèles nationaux restent actifs.
Il manque une base suffisamment solide pour certifier leur fréquence respective.
Le mot logiciel `legacy` désigne ce maintien, pas une norme. La référence
finlandaise 27k/2023 a été remplacée en septembre 2024 et n'est pas appliquée
comme une règle actuelle vérifiée.

## Comportement technique et limites

- La phase stable choisit le premier **emplacement**, jamais la présence de toute
  une route dans les nouveaux profils. 50 m donnent 20 balises par côté/km droit.
- Les fragments courts participent sans imposer une balise à chaque morceau de
  quelques mètres. Aux raccords, l'intervalle peut varier : l'origine reste propre
  à chaque identifiant. Cela évite que des voisins chargés dans une autre tuile
  décalent une rangée entière. Ce n'est pas un relevé d'implantation au mètre près.
- Le rayon est estimé sur la géométrie horizontale. La cadence est resserrée dans
  la même rangée ; les anciens substituts J1 ne se superposent pas à cette rangée.
- Le pays est testé à chaque accotement et objet effectif. À géométries complètes
  identiques, les tuiles contiguës se partagent exactement les objets.
- Pas de comptages complets, d'inventaire des balises, de brouillard, de salage
  ni d'éclairage exhaustif. Les bâtons de neige ne sont pas assimilés aux balises.
- Jeton de cache Europe renouvelé ; modèles, trottoirs, ARRÊT/DUR, STOP publics
  et IA conservés. Aucune nouvelle dépendance ni modification de binaire natif.

## Sources

Consultées le 29 octobre 2026. Les éditions anciennes sont signalées ; une
reproduction de manuel est distinguée du site de l'autorité émettrice.

1. **DE-StVO-620** : [StVO, Anlage 4](https://www.gesetze-im-internet.de/stvo_2013/anlage_4.html).
2. **AT-Kaernten-2019** : [manuel de Carinthie, équipement routier](https://strassenbau.ktn.gv.at/DE/repos/files/strassenbau/export156456_inuse/Dateien/PDF/Abt.17/Erhaltung/Erhaltungshandbuch_2019.pdf?exp=818927&fps=7d060e1d7d1385065b21011a4703052fe156604e) ; [AUTO-NOM, 2017, §4.1](https://fti-mobilitaetswende.at/resources/pdf/projektberichte/AUTO-NOM_2017_Teil2_Rahmenbedingungen_Infrastruktur_1101.pdf).
3. **CH-Luzern-653.301** : [directive Leiteinrichtungen](https://vif.lu.ch/-/media/VIF/Dokumente/download/fachordner/Verkehrsmassnahmen/653_Richtlinien_Verkehrsmassnahmen/653_301_Richtlinie_Leiteinrichtungen.pdf).
4. **CZ-BESIP** : [conseils du service ministériel BESIP](https://besip.gov.cz/Clanky/Bezpecne-na-prazdniny-Zpomalte-udrzujte-odstup-a-p).
5. **SK-TP105-2022** : [TP 105, §§4.1 et 6.2](https://www.ssc.sk/files/documents/technicke-predpisy/tp/tp_105_2022.pdf).
6. **DK-N41** : [règlement 2511/2021](https://retsinformation.dk/eli/lta/2021/2511) ; [emploi, 426/2023](https://www.retsinformation.dk/eli/lta/2023/426).
7. **PL-GDDKiA-2022** : [GDDKiA Wrocław, 19 octobre 2022](https://www.gov.pl/web/gddkia-wroclaw/abc-oznakowania-drog---slupki-prowadzace).
8. **ES-OC309 / Bizkaia** : [OC 309/90, document ministériel reproduit](https://studylib.es/doc/6357976/hitos-de-arista---ministerio-de-fomento) ; [projet de Bizkaia, §4.2.1](https://www.bizkaia.eus/home2/Archivos/DPTO10/Temas/Pdf/X0000232-PC-Anejo%2013%20Se%C3%B1alizaci%C3%B3n_Rev1.pdf).
9. **IT-DPR495-art173** : [texte législatif reproduit par l'Université d'Urbino](https://olympus.uniurb.it/index.php?Itemid=137&catid=5&id=15022:dpr49592&option=com_content&view=article).
10. **SE-VGU2015 / NVDB2013** : [VGU 2015:086](https://trafikverket.diva-portal.org/smash/get/diva2%3A1542414/FULLTEXT01.pdf) ; [NVDB TDOK 2013:0381 v9, Kantstolpe](https://www.nvdb.se/globalassets/upload/styrande-och-vagledande-dokument/tdok-2013-0381-regler-for-insamling-och-leverans-ver-9.pdf).
11. **NO-N300-2012** : [N300 partie 2, signe 920, reproduction du manuel de Statens vegvesen](https://www.saferoad-traffic.no/uploads/hb-n300-del2.pdf).
12. **IS-Vegagerdin-2.1** : [Vegagerðin, 2.1 Stikur](https://www.vegagerdin.is/vegagerdin/gagnasafn/leidbeiningar-og-reglur/12194-2/2-1-stikur).
13. **UK-TSM4-15** : [Department for Transport, chapitre 4, 2018, §15](https://assets.publishing.service.gov.uk/media/5c41d683ed915d38b0437c2d/traffic-signs-manual-chapter-4.pdf).
14. **IE-TSM6-6.22** : [TSM chapitre 6, octobre 2025, §§6.22.5–6.22.7](https://www.roadguidelines.ie/wp-content/uploads/2025/11/TSM-Chapter-6-Final-Version-3-October-2025-2025-10-22.pdf).
15. **France** : [Cerema, IISR partie 1, 2023](https://equipementsdelaroute.cerema.fr/IMG/pdf/iisr_1epartie_vc_20230411-mb_cle51d1d3.pdf) ; [circulaire historique J6, 2000](https://dtrf.cerema.fr/pdf/pj/Dtrf/0002/Dtrf-0002409/TO2409.pdf).
16. **Finlande, non appliquée** : [Väylävirasto 27k/2023, remplacée](https://aineistot.vayla.fi/api/file/ava/Julkaisut/Vaylavirasto/vo_2023-27k_reunapaalut_web.pdf).

Les vérifications automatiques concernent le logiciel. L'aspect, l'import et
les performances dans BeamNG.drive sur Windows restent à vérifier en jeu.
