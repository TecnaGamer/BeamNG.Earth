# Uitbreiding v5: zone 30 en onderborden

Nieuwste databron en tegellader: zie `NL_GEORGE_V5.md`. De v4-werkwijze hieronder blijft uitsluitend als historische proefbeschrijving bewaard.

Deze stap bouwt verder op de beperkte Venlo-proef. Er is geen pull request geopend en de nieuwe onderdelen zijn nog niet in BeamNG getest, overeenkomstig de keuze om voorlopig zonder speltest door te werken.

## Toegevoegd

- A1-30-ZB: begin zone 30, plaat 60 × 75 cm.
- A2-30-ZE: einde zone 30, plaat 60 × 75 cm.
- OB504: pijlonderbord links- en rechtsonder, plaat 60 × 20 cm.
- Korte beugelset met 10 cm onderlinge afstand voor het lage onderbord. De bestaande set heeft 28 cm onderlinge afstand.
- Montage van maximaal twee expliciet opgeloste onderbordassets aan dezelfde paal, met 6 cm vrije ruimte tussen platen. Geen automatische afleiding uit lege brongegevens.

De platen gebruiken dezelfde generator voor 3 mm plaatdikte, 12 mm vouwrand en gescheiden voor- en achterkant. Hun UV-verhouding herstelt de oorspronkelijke bronverhouding; de vierkante PNG-brontexture mag daarom niet als maatvaste afbeelding van de plaat worden beschouwd. De bestaande paalvorm en positie 5 cm onder de bovenste bordrand blijven behouden. Maten en montage zijn voorbeeldmaten, geen uit NDW gemeten constructie.

## Herkomst

De originele SVG's worden lokaal bewaard en gehasht. Begin en einde zone: Fry1989, met kleurcorrecties door SilverJapan2006, gebaseerd op onder meer Bouwe Brouwer; op Commons aangemerkt als publiek domein voor Nederlandse RVV-borden. OB504: Ad Verburg, CC0 1.0. Geen startertexturen, productfoto's of verzonnen symbolen.

- https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_A1_30_Zone.svg
- https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_A1_30_Eindzone.svg
- https://commons.wikimedia.org/wiki/File:Netherlands_traffic_sign_OB504.svg

De bronbeelden en geconverteerde textures zijn visueel gecontroleerd. De huidige bibliotheek bevat zeven exacte varianten: A1-50, B6, C1, J16 en de drie hierboven. Er is nog geen volledige parametrische snelheidsbibliotheek. Een andere snelheid, zoneherhaling of andere zonecode wordt niet door een vergelijkbaar bord vervangen.

## Effect op Venlo

De bestaande cache is hergebruikt; er is geen tweede NDW-download gedaan. Binnen dezelfde box liggen 798 records. Nu passen 154 records bij de ondersteunde assets en selectievoorwaarden, waarvan de dichtstbijzijnde 40 worden gebruikt. De nieuwe proefselectie bevat 17 B6, 11 begin-zone-30 en 12 einde-zone-30.

De overige 758 records worden geregistreerd: 114 buiten de proeflimiet, 428 zonder correct asset, 19 met andere zonevarianten en 197 met nog niet renderbare onderborden. De laatste groep bevat samen 210 onderborden: 120 zonder tekst/symbool, 45 uitzonderingen zonder benoemde doelgroep, 42 overige teksten en drie afstanden waarvoor nog geen gecontroleerde texture bestaat. Afstanden en tijdvensters worden wel genormaliseerd en bewaard. Lege tekst wordt niet behandeld alsof er geen onderbord is. Alleen de tekst “uitgezonderd” rechtvaardigt bijvoorbeeld geen fietsersymbool.

OB504 is als gecontroleerd bibliotheekasset en expliciete montage beschikbaar. Het is niet aan deze onvolledige NDW-records gekoppeld en verschijnt daardoor niet automatisch in de echte Venlo-selectie.

## Bouwen en controleren

```sh
node tools/generate_nl_testset.mjs
node tools/test_nl_testset.mjs --fengari PATH/TO/lua-cli.js
node tools/test_ndw_import.mjs
```

De generator blijft offline. De optionele bronconversie ondersteunt expliciete codes:

```sh
node tools/nl_test_source/prepare.cjs PATH/TO/RENDERER/PACKAGES A1-30-ZB A2-30-ZE OB504
```

Geslaagd: gesloten geometrie, winding, UV/materialen, RGB-textures, reproduceerbare generatie, exacte zonekoppeling, onduidelijke zonevarianten weigeren, onderbordgegevens bewaren, kortere beugelkeuze, montage van zonebord plus onderbord, geodesie en bestaande streaming-/opruimtests. De montagecombinatie in de test is uitsluitend een geometrische test, geen claim dat die combinatie op een bronlocatie voorkomt.

Installatie en start blijven zoals in `NL_NDW_IMPORT.md`; vervang de vorige proefmod, voorkom dubbele actieve kopieën en herstart de kaart. De bijgewerkte `nl_ndw_venlo.json` is meegeleverd. Na een nieuwe livecontrole volgen verdere RVV-varianten, correcte tekstonderborden, montagebeleid en prestatietests. Een pull request komt pas na afronding en acceptatie van het geheel.
