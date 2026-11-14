# Typoroute --- Caractères de signalisation
Version 0.91.2

Mars 2024

Gwenaël Jouvin - gwenael.jouvin@laposte.net


## 1 --- Préambule
La signalisation routière française utilise à la fois des dessins
et des inscriptions écrites.
Ces dernières sont composées à l’aide de caractères au dessin spécifique
qu’il est difficile de retrouver sous forme numérique.
L’administration en charge des transports n’en a pas réalisé
et les quelques versions disponibles
ne le sont ni sous licence libre,
ni ne sont conformes aux spécifications techniques.

Les polices de la collection Typoroute sont réalisées
afin de remédier à ces problèmes.

---

Cette version
n’est diffusée que pour essais préalables
et n’est pas une version aboutie.
Elle reste à compléter
et peut comporter
des défauts de dessin ou de réglage.

Les utilisateurs & utilisatrices sont invités à signaler tout défaut à l’auteur.

### Avertissement

Malgré tout le soin apporté à sa réalisation,
une police de caractères n’est pas un outil de dessin
et ne permet pas la confection de signaux.

Seuls des caractères au format de logiciels de dessin
utilisés conjointement aux règles de composition
permettent une réalisation correcte.


## 2 --- Généralités

### 2.1 --- Description

La collection de polices « Typoroute »
permet de reproduire les caractères utilisés sur les signaux routiers français
depuis 1955.

Différentes polices correspondent
aux périodes qui les ont vu apparaître :

* Typoroute 55 correspond aux polices L1 à L4 publiées dans l’Instruction interministérielle de 1955.
Le P et le R de la gamme L1 avaient été préalablement publiés par une circulaire de 1952.

* Typoroute 77 correspond aux polices L1, L2 et L4 remaniées,
publiées dans l’édition 1977 de la première partie du livre I de l’instruction.

* Typoroute 88 correspond aux deux caractères de marquage routier
apparues lors de la refonte de la septième partie du livre I de l’instruction, en 1988.

* Typoroute 2002 contient les polices L1, L2, L4, L5, L11 et L12,
apparues dans l’édition 2002 de l’instruction.


Dans chacune de ces collections, plusieurs groupes de polices se distinguent :

* les linéales utilisées pour les mentions courantes de direction,
classées en 3 polices de la plus large (L1) à la plus étroite (L3) ;

* les cursives, utilisées pour les mentions d’intérêt local,
composées avec la police L4
qui évoluera sous une forme linéale oblique ;

* les matricielles, utilisées dans des afficheurs numériques
en signalisation dynamique.


### 2.2 --- Nom
Le nom « Typoroute » donné à cette collection de fontes
fait directement référence à la fois à la typographie
et à l’objet pour lesquelles
ces fontes sont utilisées.

### 2.3 --- Correspondance avec la réglementation
Les différentes fontes proposées
correspondent aux caractères définis dans les instructions interministérielles sur la signalisation routière.

Malgré la recherche de la précision,
quelques ajustements marginaux ont été effectués par souci de cohérence géométrique.
Ces différences mineures seront détaillées dans une documentation à paraître.

La réalisation de polices de caractères
nécessite par ailleurs de trouver un compromis
entre légèreté des fichiers
et fidélité au modèle.
Par conséquent,
certaines détails jugés sans conséquence sur l’aspect général
ne correspondent pas rigoureusement au dessin originel.


## 3 --- Installation

### 3.1 --- Systèmes de type Unix

Copier les fichiers de fonte d’extension `ttf`
vers un des répertoires
`~/.fonts`, `~/.local/share/fonts` ou `/usr/share/fonts`
puis exécuter la commande `$ fc-cache -fv` pour les référencer.

### 3.2 --- Apple

Voir la documentation du constructeur :
http://support.apple.com/fr-fr/HT201749

### 3.3 --- Windows

Télécharger les fichiers de fonte d’extension `ttf`,
les sélectionner, effectuer un clic-droit
et cliquer avec le bouton gauche sur `Installer`

Autre méthode,
copier les fichiers d’extension `ttf`
vers le répertoire
`C:\Windows\fonts`.


## 4 --- Description des polices

### 4.1 --- Typoroute 88
Cette police, déclinée en deux fontes 7R et 7U,
est composée de 10 chiffres et 26 lettres destinées au marquage routier.

Les caractères sont anamorphosés, étirés le long d’un axe vertical
afin d’être lisibles depuis un véhicule.
Ils ont été dessinés d’après les planches annexées à l’édition 1988 de la 7e partie du livre I
de l’instruction sur la signalisation.

#### 4.1.1 --- 7U
La fonte « urbaine » correspond aux caractères conçus pour des vitesses
inférieures à 60 km/h
Cette vitesse sera portée à 70 km/h ultérieurement.
Leur hauteur de composition est de 1,50 m
et l’espace entre les caractères est réglementé à 15 cm.

#### 4.1.2 --- 7R
La fonte « rurale » correspond aux caractères conçus pour des vitesses
supérieures à 60 km/h
Cette vitesse sera portée à 70 km/h ultérieurement.
Leur hauteur de composition est de 4 m,
l’espace entre les caractères n’est pas précisé dans la réglementation
et est fixé arbitrairement à 20 cm.


### 4.2 --- Typoroute 2002
Cette police est déclinée en plusieurs fontes L1, L2, L4, L5, L11 et L12.
Elle est composée d’un alphabet latin réduit aux capitales pour les gammes L1 et L2,
à ce même alphabet accompagné de minuscules pour les gammes L4 et L5
et d’alphabets propres aux afficheurs numériques pour les gammes L11 et L12.

#### 4.2.1 --- L1
Fonte de signalisation
utilisée pour les mentions principales
sur les panneaux à fond blanc ou jaune.

Elle est disponible avec des espacements inter-lettres variables.
L’espacement par défaut est désigné « 100 % ».

Les lettres ne sont disponibles qu’en capitales,
sauf pour celles présentes dans les unités de mesure
utilisées en signalisation.


#### 4.2.2 --- L11
Fonte à chasse fixe,
elle propose les caractères utilisables sur les afficheurs matriciels 5×7
utilisés en signalisation dynamique,
notamment sur les panneaux à messages variables.

Bien que non définis dans les textes réglementaires,
des minuscules accentuées sont disponibles.


#### 4.2.3 --- L12
Fonte à chasse fixe,
elle correspond aux caractères utilisables sur les afficheurs à segments
utilisés en signalisation dynamique.

Elle propose des caractères à la fois pour afficheurs 16 segments et 7 segments.
Ces derniers sont accessibles par les fonctions Opentype `salt` ou `ss01`.


## 5 --- Hauteurs de composition

Les signaux sont composés avec des caractères de hauteur prédéterminée.

En raison de contraintes de conception,
les caractères sont dessinés de manière
à occuper 70 % de la hauteur nominale de la police.
Ainsi, un document composé en 10 pt verra la hauteur des caractères s’établir à 7 pt.

Réciproquement,
l’utilisateur qui souhaite une hauteur de composition précise
devra augmenter la hauteur de la police d’un facteur 10/7.


## 6 --- Fonctions Opentype
Certaines fonctions permettent de reproduire
certaines caractéristiques propres aux différentes fontes.

### 6.1 --- Typoroute 88

Option `kern`

Conçue comme une police à chasse fixe
grâce à ses lettres très verticales,
une table de crénage rudimentaire
est tout de même réalisée afin d’ajuster l’écartement entre certaines lettres,
notamment le A, le V et le Y.


### 6.2 --- Typoroute 2002

#### 6.2.1 --- L1

* Option `kern`

Les espaces entre les lettres
sont définis précisément
depuis la parution de l’instruction interministérielle sur la signalisation de direction de 1982.
Ils sont retranscrits par l’utilisation d’une table de crénage par classes.

Par ailleurs, cette option
est utilisée pour gérer certains espaces spécifiques,
comme celui entre lettres et trait d’union.

Pour un bon fonctionnement de la police,
elle doit être activée.

* Option `dlig`

Option qui permet le remplacement automatique
des mots « SUR » et « SOUS »
par les symboles correspondants <sup>s</sup>/ et <sup>s</sup>/<sub>s</sub>.

Elle sera activée à la discrétion de l’utilisateur.


* Option `calt`

Cette option est utilisée de manière conditionnelle
pour gérer les espaces entre trait-d’union et lettres,
notamment lorsqu’il se trouve en début de ligne
dans les mentions à plus d’une ligne.

Pour un bon fonctionnement de la police,
elle doit être activée.


* Option `tnum`

Option qui donne l’accès à des chiffres à chasse fixe,
adaptés à la composition de tableaux.

Cette option ne doit pas être utilisée
dans la composition de signaux.


* Option `ss01`

Option qui donne l’accès aux chiffres 6 et 9
échangés et retournés,
comme on le constate parfois sur certains signaux.


* Option `ss02`

Option permettant de gérer la substitution du trait d’union
lors de la composition des mentions à plus d’une ligne.

C’est une dépendance de l’option `calt`
qui n’est pas destinée à être utilisée seule.


#### 6.2.2 --- L11, L12

* Option `zero`

Ces polices sont définies dans la réglementation
avec un zéro identique à la lettre « O ».
Afin de faciliter la lecture de certains messages,
un zéro barré a été créé
et accessible en activant cette option.


#### 6.2.3 --- L12

* Options `salt` et `ss01`

Par défaut, la fonte L12 reproduit les caractères d’un afficheur 16 segments.
L’activation des « alternatives » `salt` ou `ss01` permet d’obtenir seize caractères
reproduisant un afficheur à 7 segments, de 0 à 9 et de A à F.


## 7 --- Fichiers de dessin `dxf`

### 7.1 --- Typoroute 88

Les caractères utilisés en marquage routier
ont la particularité d’être _anamorphosés_
afin que leur forme,
transformée par la perspective lorsqu’ils sont vus depuis un poste de conduite,
soit aisément reconnaissable par les conducteurs.

Ainsi, les courbes ne sont plus des arcs de cercle
et les lignes horizontales sont épaissies pour compenser l’écrasement
de la perspective.
Le facteur d’étirement peut être facilement déterminé
en mesurant la lettre _E_.

Le dessin original est réalisé sans tenir compte de cette anamorphose
et a recours à des arcs de cercle là où ils sont requis ;
de même,
les lignes horizontales sont de même épaisseur que les lignes verticales.


## 8 --- Fichiers de dessin `svg`

Les fichiers fournis sont utilisables
en reprenant les blocs de code correspondant à chaque caractère.
Leur aperçu est réalisable avec le logiciel `inkscape`.

### 8.1 --- Typoroute 88

Le dessin des caractères est dérivé des dessins originaux au format `dxf`.
Le résultat final, correspondant au tracé publié dans l’Instruction interministérielle de 1988,
est obtenu en anamorphosant les dessins originaux par un facteur d’étirement vertical
qui n’est pas intégré aux coordonnées du dessin,
mais réalisé à l’aide de l’attribut `svg` `transform(scale())`.

Ce facteur est de 2 pour les caractères 7U
et de 3 pour les caractères 7R.


## 9 --- Licence
La licence applicable aux fichiers de police mis à disposition dans le répertoire `ttf`
est la SIL OFL
reproduite dans le fichier LICENSE.md.

---

*Copyright (c) 2024, Gwenaël Jouvin (gwenael.jouvin@laposte.net),
with Reserved Font Name “Typoroute”.*

---

Les fichiers de dessin mis à disposition dans les répertoires `dxf` et `svg`
sont diffusés libres de droits d’auteur,
sous les conditions de [Creative commons CC 0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).


## 10 --- À venir

Une documentation illustrée et plus aboutie
sera fournie avec la version stable des polices.

D’autres fontes sont en cours de réalisation
et seront publiées ultérieurement,
ainsi que les dessins ayant servi à leur réalisation.



## 11 --- Journal des modifications

#### Version 0.91.2 -- 2024-03-11

* Typoroute 2002 L11

Ajout de symboles manquants

Redéfinition de l’espace entre les symboles à environ 2/7 Hc
comme précisé dans la norme NF P98 532-9

#### Version 0.91.1 -- 2023-06-07

* Modification des méta-données
de manière à ce que toutes les fontes
soient visibles dans les différents logiciels.

* Typoroute 88 7R :

modification du crénage S-[CGOQ]

ajout des crénages P-A et P-J


### Version 0.91 -- 2023-05-29

* Mise à disposition de la fonte :

Typoroute 2002 L1 100

* Correction des méta-données des fontes :

Typoroute 88 7R, 7U ;
Typoroute 2002 L11, L12

* Corrections de ce fichier README


### Version 0.90 -- 2023-05-24

Version initiale.

Mise à disposition des fontes :

Typoroute 88 7R, 7U

Typoroute 2002 L11, L12


## Table des matières

1 --- Préambule

2 --- Généralités

* 2.1 --- Description

* 2.2 --- Nom

* 2.3 --- Correspondance avec la réglementation

3 --- Installation

* 3.1 --- Systèmes de type Unix

* 3.2 --- Apple

* 3.3 --- Windows

4 --- Description des polices

* 4.1 --- Typoroute 88

* 4.2 --- Typoroute 2002

5 --- Hauteurs de composition

6 --- Fonctions Opentype

* 6.1 --- Typoroute 88

* 6.2 --- Typoroute 2002

7 --- Fichiers de dessin `dxf`

8 --- Fichiers de dessin `svg`

9 --- Licence

10 --- À venir

11 --- Journal des modifications
