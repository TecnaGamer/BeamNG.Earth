# NDW-koppeling — beperkte Venlo-proef

Nieuwste databron en tegellader: zie `NL_GEORGE_V5.md`. De v4-werkwijze hieronder blijft uitsluitend als historische proefbeschrijving bewaard.

Er wordt pas een pull request geopend nadat het volledige afgesproken werk klaar en getest is. Deze stap voegt een echte-locatieproef toe en publiceert niets.

Nieuwste uitbreiding: zie `NL_VARIANTS_V5.md`. Onderstaande aantallen beschrijven de oorspronkelijke v4-selectie; het meegeleverde indexbestand is inmiddels v5.

## Bron en selectie

Officiële NDW current-state API v4, opgevraagd op 5 november 2026 met `countyCode=GM0983&status=PLACED`. De gemeenteselectie is één keer lokaal gecachet; de landelijke download is niet ingelezen. Documentatie: https://docs.ndw.nu/data-uitwisseling/interface-beschrijvingen/verkeersborden-api/ . Het NDW-bronbestand en de onderliggende bronfoto's zijn geen textures: de gecontroleerde RVV-assets blijven gebruikt worden.

De selectiebox is west 6.13278, zuid 51.36111, oost 6.15078, noord 51.37311. Dat is het gebied rond de eerdere Venlo-test. Van 15.012 gemeentelijke bronobjecten vallen er 798 binnen deze box. Daarvan zijn 40 dicht bij het midden geselecteerd: 39 B6 en één C1. Nog 30 geschikte objecten vallen buiten de proeflimiet. Verder worden 197 objecten met onderborden, 428 met ontbrekende assets en 103 zonevarianten expliciet geregistreerd. Dit is geen afkeuring van de NDW-data: onze assetbibliotheek ondersteunt die varianten nog niet.

De import bewaart UUID, WGS84, RVV-code, zwarte code, zonecode, onderborden, bronrichting, NEN-richting, validatie, datum en wegvak. Ontbrekende richting wordt niet als nul ingevuld. Niet-geplaatste borden, RD-coördinaten, ongeldige locaties, ontbrekende assets en dubbele bron-ID's worden geweigerd. De uitvoer bevat een bronhash, issues en projectcompatibele zoom-17-tilekeys. Nabijgelegen borden worden niet automatisch samengevoegd tot één paal.

## Import uitvoeren

```sh
node tools/import_ndw_signs.mjs venlo.geojson output-directory 6.13278,51.36111,6.15078,51.37311
```

Gebruik een WGS84 FeatureCollection van de officiële current-state API, geen eventsfeed. Invoer maximaal 50 MB, gebied maximaal 0,02 graden per as en maximaal 40 geselecteerde objecten. De selectie is deterministisch. Voor een nieuw gemeentegebied moet de bron expliciet worden opgehaald; het script doet geen verborgen netwerkverzoeken.

## In BeamNG controleren — nog niet uitgevoerd

De gebruiker heeft voor deze stap gevraagd zonder speltest verder te werken. De nieuwe import en streaming zijn daarom lokaal getest, maar nog niet live geaccepteerd. De eerdere v3-praktijkmetingen bewijzen geen acceptatie van deze nieuwe functionaliteit.

Plaats de auto rond 51.36711, 6.14178 en laad in GE-Lua:

```lua
extensions.load('beamngEarth_nltestset')
extensions.load('beamngEarth_nlndw')
beamngEarth_nlndw.start('/levels/beamng_earth/data/nl_ndw_venlo.json','travel_bearing')
```

Het tweede argument is een expliciete richtingsinterpretatie voor de proef. `travel_bearing` betekent bronhoek als rijrichting waarvoor het bord geldt, met de voorkant naar naderend verkeer; `front_bearing` betekent bronhoek als normaal van de voorkant. De historische NDW-toelichting beschrijft de kompasstand vanuit het betreffende verkeer, maar de huidige API-veldtabel geeft alleen het hoekbereik. Controleer daarom fysiek de zichtzijde bij een bekend kruispunt vóór definitieve acceptatie. `nenTurningDirection` wordt bewaard en niet ongemerkt als dezelfde hoek behandeld.

Alleen geselecteerde borden binnen 120 m worden getoond. De set wordt maximaal eenmaal per seconde herzien en alleen opnieuw gebouwd als de gewenste borden of het anker veranderen. Pending teleport ruimt direct op; een nieuw stabiel anker laat de bronposities opnieuw omrekenen. Geen automatische activering bij het opstarten van Earth.

Stoppen en rapporteren:

```lua
dump(beamngEarth_nlndw.report())
require('beamngEarth/nlcheck').save()
beamngEarth_nlndw.stop()
```

De demonstratiemontage is expliciet aangenomen: een originele 330 cm-flespaal, bordtop 2,35 m boven TerrainBlock en paaltop 5 cm lager. Het bronbestand levert geen gemeten montagehoogtes, paalaantallen of betrouwbare montagegroepering. Een paal kan bij een echte locatie dus ongeschikt zijn; deze proef claimt geen exacte fysieke reproductie. Bruggen, water, gebouwen en de eventuele afstand tussen bronpunt en echte paal vereisen nog controle.

## Lokale validatie en vervolg

Tests dekken exacte assetselectie, nul versus ontbrekende richting, zone- en onderborduitsluiting, CRS, dubbele ID's, determinisme en selectiegrenzen. Met de echte projectgeodesie worden vier windrichtingen, hoogte, tilekeys en foutafhandeling getest. Streamingtests controleren in- en uitladen tijdens verplaatsen, hergebruik van ongewijzigde objecten, teleport, nieuw anker en ontbrekend terrein. De onafhankelijke scène-audit ondersteunt ook de echte bron-ID's.

Nog nodig: de nieuwe proef visueel in Earth controleren; de ontbrekende bibliotheek en onderborden aanvullen; bronoriëntatie en montagebeleid definitief bevestigen; integratie met bestaande gegenereerde snelheidsborden zodat er geen dubbele plaatsingen ontstaan; prestaties/geheugen en uitgebreidere praktijkacceptatie. Pas daarna volgt de pull request.
