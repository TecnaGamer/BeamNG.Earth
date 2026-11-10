# Panneaux de localité européens — composition et limites

Cette révision porte sur les **56 pays et territoires déjà sélectionnables** par
le kit européen, y compris les micro-États, îles Britanniques et pays du Caucase.
Chaque pays possède maintenant un profil de composition explicite. Il s'agit de
variantes de décor documentées, pas d'une reproduction homologuée de chaque
panneau, de chaque région ou de chaque époque.

## Ce qui change

- Largeur calculée à partir du contour réel des lettres de la police choisie.
- Hauteur, marges, bordure, rayon des angles et hauteur de lettres par famille
  nationale : le rectangle de 70 cm commun à presque tous les pays disparaît.
- Casse locale conservée ; les noms allemands, autrichiens, néerlandais, belges
  et roumains ne sont pas transformés systématiquement en capitales.
- Les noms longs agrandissent d'abord le panneau ; une légère réduction uniforme
  puis une coupure sur les espaces ou traits d'union évitent les lettres écrasées.
  Les mots exceptionnellement longs peuvent passer sur plusieurs lignes.
- Centrage sur l'encre visible, y compris accents et jambages. Les lettres ne
  subissent aucune déformation horizontale supplémentaire.
- Cadre métallique arrondi et épaisseur réelle ; les poteaux suivent la hauteur
  effective du panneau. La largeur utilisée pour éviter la chaussée est celle
  du maillage final.
- Sorties avec trait diagonal borné, cartouches français uniquement pour une
  référence D réellement disponible, silhouettes nationales simplifiées.
- Les matériaux et métriques des panneaux de direction existants sont conservés.
  Un seul atlas partagé de 4096 × 2048 pixels sert à tous les noms : aucun fichier
  de texture par ville, aucun chargement de police système en jeu.
- La signature du cache comprend la composition et les coordonnées de l'atlas.
  Après redémarrage, les tuiles concernées sont reconstruites ; les données
  téléchargées ne sont pas effacées.

## Typographies

| Utilisation | Fichier intégré | Statut |
| --- | --- | --- |
| Suède | Tratex Vit, fourni par Transportstyrelsen | Véritable fichier de l'autorité, destiné aux illustrations. |
| Royaume-Uni et familles proches | AG Transport Heavy | Reconstruction des dessins DfT, avec provenance Crown/OGL. Ne prétend pas reproduire ANWB-C ou toutes les variantes locales. |
| Espagne | CCRIGE, Javi Casas | Numérisation libre des dessins espagnols, SIL OFL 1.1. |
| Norvège | Trafikk Regular, Aitor | Interprétation libre des spécifications norvégiennes ; quelques lettres sont volontairement modifiées. |
| France | Typoroute 2002 L1 100, Gwenaël Jouvin | Numérisation de l’alphabet L1 réglementaire, SIL OFL 1.1. Le TTF original est conservé, avec son crénage et ses traits d’union contextuels ; version de test signalée par son auteur. |
| Allemagne et plusieurs familles continentales | D-DIN | **Substitut inspiré DIN**, pas une certification DIN 1451. |
| Pologne | D-DIN Expanded | **Substitut**, pas Drogowskaz. La numérisation trouvée comporte des conditions supplémentaires de notification à l'auteur ; elle n'est pas incluse. |
| Autriche, Slovaquie | D-DIN | **Substitut**, pas TERN, qui est proposé sous licence commerciale. |
| Suisse | D-DIN | **Substitut**, pas ASTRA Frutiger. |
| Autres alphabets et glyphes absents | DejaVu Sans | Repli Unicode ; accents, cyrillique, grec, arménien et géorgien conservés lorsque disponibles. |

Les sources et conditions accompagnent les fichiers dans `tools/fonts/town/` et
le matériel du mod dans `regional_signs/TOWN-FONTS-NOTICE.md`. Le fichier
`docs/TOWN_SIGN_COVERAGE.json` indique le profil et le niveau de vérification de
chaque pays. Un alphabet national mentionné dans une liste ne signifie pas que
sa version numérique exacte est incluse.

La France utilise maintenant Typoroute L1 pour EB10/EB20 et les cartouches D. Les avances, paires de crénage et la variante contextuelle du trait d’union proviennent du fichier original. Les ligatures facultatives SUR/SOUS restent désactivées pour conserver le nom fourni. Les textures sont générées sans déformation horizontale. Voir [le dépôt de l’auteur](https://framagit.org/gwenj/typoroute).

## Noms secondaires et variantes

Les exemples de référence allemands, autrichiens et néerlandais comportent
parfois commune, district ou autre ligne secondaire. Le moteur accepte jusqu'à
deux `admin_lines` explicites, plus petites que le nom principal. **Les données
de localités actuellement embarquées ne contiennent pas ces administrations** :
elles ne sont pas déduites ou inventées. Les petites lignes de certaines cartes
de démonstration proviennent seulement des photos de référence.

Le panneau vert polonais E-17a nomme une localité ; le D-42 blanc à silhouette
marque une zone bâtie. Cette révision améliore E-17a/E-18a et ne les fusionne pas
avec D-42. Les panneaux bleus de nom de lieu scandinaves ou baltes restent aussi
distincts des panneaux réglementaires d'agglomération.

En Suisse et au Liechtenstein, les variantes bleue/blanche suivent la classe de
route disponible. En Turquie, les villages/hameaux identifiés ont une variante
blanche, les autres localités la variante bleue. Le bandeau de vitesse roumain
n'est affiché qu'avec une demande explicite de cette variante, pas à partir d'une
simple vitesse de route. Aucune vitesse de navigation n'est changée.

Les traductions, noms bilingues absents, blasons, population, noms de districts
et panneaux de jumelage ne sont pas générés. Les emplacements français observés
restent issus de leurs données cartographiées ; ailleurs les transitions bâties
restent estimées comme auparavant. La refonte du graphisme ne transforme pas
ces estimations en inventaire exact des panneaux installés.

## Références principales consultées le 4 novembre 2026

| Pays / sujet | Source primaire | Utilisation |
| --- | --- | --- |
| France | [IISR, 5e partie, Cerema](https://equipementsdelaroute.cerema.fr/IMG/pdf/2025_09_03_iisr_5epartie_vc_20240315_relu_jcb_cle28888d.pdf), [EB10/EB20, Légifrance](https://www.legifrance.gouv.fr/loda/article_lc/LEGIARTI000049319411) | Nom, bordures, cartouche, dimensions dépendant de la composition. |
| Allemagne | [VwV-StVO](https://www.verwaltungsvorschriften-im-internet.de/bsvwvbund_26012001_S3236420014.htm), [StVO annexe 3](https://www.gesetze-im-internet.de/stvo_2013/anlage_3.html) | Ortstafel 310/311, hiérarchie du nom et des mentions. |
| Autriche | [StVZVO, annexe 3](https://ris.bka.gv.at/eli/bgbl/ii/1998/238/ANL3/NOR40156588), [StVO §53](https://www.ris.bka.gv.at/NormDokument.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10011336&Paragraf=53) | Famille 960 × 630 mm et hauteur adaptée. |
| Belgique | [SPW, typographie](https://securotheque.wallonie.be/equipements/signalisation-c/generalites/typographie-et-composition-du-message-sur-les-signaux-routiers), [arrêté 1976 consolidé](https://wallex.wallonie.be/eli/arrete/1976/10/11/1976101105/2024/02/19) | Casse et famille F1 ; silhouette simplifiée guidée aussi par la photo Schilde. |
| Pays-Bas | [CROW, chapitre H](https://kennisbank.crow.nl/public/gastgebruiker/WOBU/Verkeerstekens_%28toepassing%2C_plaatsing_en_uitvoering%29/Hoofdstuk_H._Bebouwde_kom/29513) | H1 bleu, nom et mention municipale conditionnelle. |
| Pologne | [Gov.pl, dessins des panneaux](https://www.gov.pl/attachment/9a372081-6a9b-4843-9a39-8b38da07436d) | E-17a, hauteur 530 mm et largeur liée au nom, distinction de D-42. |
| Roumanie | Photos Recea fournies pour ce travail | Variante blanche simple et lettres plus étroites. Dimensions choisies pour le décor, pas présentées comme normatives. |
| Royaume-Uni | [DfT, Traffic Signs Manual](https://www.gov.uk/government/publications/traffic-signs-manual), [dessins de l'alphabet](https://www.gov.uk/government/publications/traffic-signs-working-drawings-tsrgd-2016-schedule-17) | Famille Transport et composition. |
| Irlande | [Department of Transport, Traffic Signs Manual](https://www.gov.ie/en/department-of-transport/publications/traffic-signs-manual/) | Distinction des lignes de langue ; pas de traduction fabriquée. |
| Suède | [F10 Platsmärke](https://www.transportstyrelsen.se/sv/vagtrafik/trafikregler-och-vagmarken/vagmarken/lokaliseringsmarken-for-vagvisning/platsmarke/), [Tratex](https://www.transportstyrelsen.se/sv/vagtrafik/trafikregler-och-vagmarken/vagmarken/om-vagmarken/teckensnitt/) | Panneau bleu de nom de lieu et police Vit. |
| Norvège | [Statens vegvesen, manuel 050 partie 4B](https://vegvesen.brage.unit.no/vegvesen-xmlui/bitstream/handle/11250/189861/handbok_050_2009_Del_4B.pdf?isAllowed=y&sequence=1) | 727.1 bleu, nom éventuellement sur plusieurs lignes. |
| Espagne | [BOE, catalogue 2025](https://www.boe.es/eli/es/rd/2025/06/10/465/dof/spa/pdf) | S-500 et alphabet CCRIGE. Variante blanche à bordure noire retenue. |
| Portugal | [IMT, critères d'utilisation](https://www.imt-ip.pt/wp-content/uploads/IMTT/Portugues/InfraestruturasRodoviarias/InovacaoNormalizacao/Divulgao%20Tcnica/SinalizacaoVerticalCriteriosUtilizacao.pdf) | N1a de localité. |
| Italie | [MIT, directive et annexe](https://www.mit.gov.it/sites/default/files/media/normativa/2016-02/Decreto_Ministeriale_protocollo_777_27-04-2006_all_1.pdf) | Panneau de centre habité, distinction de la limite municipale. |
| Suisse | [Ordonnance signalisation, Police Berne](https://www.police.be.ch/content/dam/police/dokumente/police-be-ch/d/sicherheitsunternehmen/signalisationsverordnung-de.pdf) | Variantes bleue/blanche. |
| Danemark | [Retsinformation 425/2023](https://retsinformation.dk/eli/lta/2023/425), [426/2023](https://retsinformation.dk/eli/lta/2023/426) | Famille de zone bâtie E55, silhouette simplifiée. |
| Estonie | [Riigi Teataja](https://www.riigiteataja.ee/et/dyn/103062026012/id/103032011006) | Distinction zone bâtie / nom et famille du symbole. |
| Lettonie | [Likumi, règles de circulation](https://m.likumi.lv/ta/id/274865-celu-satiksmes-noteikumi?hl=lv-LV), [CSDD, illustrations](https://www.lps.lv/uploads/docs_module/10_CSDD.pdf) | Panneau bleu de localité et sortie barrée ; pas une zone bâtie blanche. |
| Finlande | [Finlex 379/2020](https://www.finlex.fi/en/legislation/2020/379) | Panneaux de zone bâtie distincts du nom bleu conservé. |
| Hongrie | [NJT 20/1984](https://njt.jog.gov.hu/jogszabaly/1984-20-20-20) | Variante blanche à bordure noire. |
| Turquie | [KGM, manuel 2015](https://www.kgm.gov.tr/SiteCollectionDocuments/KGMdocuments/Trafik/IsaretlerElKitabi/TrafikIsaretleriElKitabi2015.pdf) | B9 selon la catégorie de localité. |

Pour les autres entrées de la matrice, une famille de décor cohérente est
conservée et ajustée. Leur dessin national complet n'a pas été vérifié dans une
spécification primaire. Les dimensions ne sont donc pas étiquetées « officielles ».

## Refaire les previews et vérifier

```sh
python tools/preview_town_signs.py build/town-previews
python tools/run_tests.py test_town_layouts test_regional_furniture test_europe_infrastructure test_regional_geotiles
```

Les previews utilisent les triangles, UV et textures du générateur réel. Elles
sont rendues hors jeu en projection frontale ; éclairage, import BeamNG, rendu
de nuit et lisibilité en mouvement restent à vérifier dans le jeu sous Windows.
Les atlas déjà générés sont fournis. Les reconstruire exige Pillow et fontTools,
uniquement au développement : `python tools/build_town_fonts.py`.

## Avec les panneaux de localité de BeamNG.Earth (Settings > World > Town signs)

Quand le réglage *Town signs* est actif (par défaut), c'est `server/townentry.py` qui place les panneaux d'entrée
et de sortie (limites de commune, agglomérations). Avec le kit régional activé, les panneaux français (et
monégasques) gardent cet emplacement mais sont dessinés avec ce modèle : proportions EB10 / EB20 de
`townlayout.py`, lettrage Typoroute L1 de l'atlas, sortie au dos du même panneau, poteaux du kit
(`roads.kit_town_boards`). Sans le kit, les faces de `townentry.py` restent inchangées.
