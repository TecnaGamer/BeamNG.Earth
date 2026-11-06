# George v5 — datagedreven verwerking en lokale tegelbestanden

George is vanaf deze stap de primaire gestructureerde bron, via de openbare NDW current-state API v5. Het spel downloadt geen foto's, doet geen beeldherkenning en vraagt geen individuele verkeersborden op. De eerdere foto-observatie wordt niet gebruikt om records automatisch aan te vullen.

## Wat daadwerkelijk is gecontroleerd

Op 5 november 2026 is de officiële Swagger-configuratie en het v5-schema gecontroleerd. De v5-export ondersteunt `supplementarySigns` met `signCode`, `text`, `openingHours` en `index`. Meerdere onderdelen met dezelfde index vormen samen één onderbord; zij mogen niet als losse platen worden geplaatst. De export bevat ook `conditions`, bronwijzigingsdatum, wegvak, eigenaar en de hoofd-/zonebordvelden. `placement` gebruikt bijvoorbeeld ALONG en `zoneCode` BEGIN/END; dat verschilt van v4.

Bronnen:

- https://wegkenmerken.ndw.nu/api/open-api/swagger-ui/index.html
- https://wegkenmerken.ndw.nu/api/open-api/api-docs/v5
- https://docs.ndw.nu/handleidingen/George/onderborden/

Het exacte openbare schema is meegeleverd in `tools/george_v5_schema.json`. De voorbeelddata komt uit dezelfde eerdere Venlo-box, via een expliciete `status=PLACED`-query in WGS84. V5 exporteert zelf geen statusveld; daarom is die queryprovenance een vereiste van de import. Een los v5-bestand wordt niet zonder meer als een bestand met geplaatste borden behandeld.

## Geen magische aanvulling van ontbrekende data

De echte v5-selectie bevat 798 hoofdborden met 212 onderbordonderdelen: 203 OTHER, vijf OB254 en vier OB401. Een groot deel van de bronhiaten blijft dus ook in George aanwezig. OTHER wordt niet veranderd in een geraden OB-code. Lege of onvolledige waarden blijven expliciet onopgelost. Een nieuwere datastructuur garandeert geen volledige landelijke broninhoud.

Alle 798 hoofdborden zijn als gestructureerde gegevens verwerkt in 56 bestaande zoom-17-projecttegels. Er zijn momenteel 154 renderbare hoofdborden met de ondersteunde assetvarianten. Andere codes en onderborden blijven in de gegevens en in het foutenoverzicht beschikbaar voor verdere bibliotheekuitbreiding, maar worden nog niet als een onvolledige of vervangende constructie getoond.

## Werkwijze voor landelijke schaal

1. Download een officiële export vooraf en cache die lokaal, samen met query en bronhash.
2. Verwerk de GeoJSON of gecomprimeerde GeoJSON sequentieel. De landelijke features-array hoeft niet in het geheugen te staan.
3. Indexeer bron-ID's en tegelkoppelingen tijdelijk in SQLite op schijf. Dubbele IDs worden in quarantaine gezet; er wordt geen willekeurige winnaar gekozen.
4. Schrijf volledige genormaliseerde gegevenstegels voor verwerking en afzonderlijke compacte rendertegels voor het spel. De laatste bevatten alleen IDs, positie, assetverwijzing, richting en expliciet ondersteunde onderborden/montageaannames.
5. Schrijf de manifestmarkering pas nadat de invoer en alle tegels succesvol zijn verwerkt. Maak elke bouw in een nieuwe map; oude tegels mogen niet blijven hangen.
6. Het spel berekent zelf bestaande projecttegelkeys en leest maximaal een buurt van 3 × 3 lokale rendertegels. Geen landelijke index of scan van alle borden in het spel. Ook afwezige tegels worden gecachet.

De compacte bestanden voor de hele Venlo-box zijn samen 49.025 bytes. Dat is bestandsgrootte, geen RAM-, GPU- of laadtijdmeting. De tegellader is lokaal getest; een nieuwe praktijkmeting in BeamNG is nog niet uitgevoerd. De volledige landelijke export is in deze stap niet gedownload of geïmporteerd.

De zichtbare set blijft voor de huidige acceptatieproef begrensd tot de dichtstbijzijnde 40 ondersteunde borden binnen 120 meter. Bestaande data wordt bij stilstaan hergebruikt. Verplaatsing vernieuwt alleen de relevante buurt; een ankerwissel rekent opnieuw om met de projectgeodesie. Dit is een basis voor landelijke dekking, nog geen gevalideerde volledige productie-integratie op landelijke dichtheid.

## Tools

Node 22.13 of nieuwer is nodig voor de ingebouwde SQLite-module; geen externe Node-packages nodig voor de import. De kleine fetchtool is bewust begrensd op een expliciete proefbox. De tegelbouwer kan ook een grotere officiële export streamend verwerken.

```sh
node tools/fetch_george_v5.mjs CACHE-DIRECTORY 6.13278,51.36111,6.15078,51.37311
node tools/build_george_tiles.mjs CACHED-FILE.geojson NEW-OUTPUT-DIRECTORY PLACED
node tools/test_george_v5.mjs
```

De fetchtool hergebruikt een bestaande cache alleen als URL en hash kloppen. De fetchtool draait uitsluitend vooraf, niet vanuit het spel. Voor een andere datum moet een nieuwe cacheversie worden gekozen. De bronhash van de tegelbouw identificeert precies het gebruikte invoerbestand.

## Starten in BeamNG — nog niet live getest

De meegeleverde mod bevat alleen het lokale Venlo-proefgebied en de assets, niet heel Nederland. Vervang de vorige proefmod, voorkom dubbele actieve kopieën en herstart de kaart.

```lua
extensions.load('beamngEarth_nltestset')
extensions.load('beamngEarth_nlndw')
beamngEarth_nlndw.startStore('/levels/beamng_earth/data/george_venlo/manifest.json','travel_bearing')
```

De richtingsinterpretatie en de aangenomen paalmontage hebben dezelfde nog te controleren beperkingen als de vorige proef. Rapportage: `beamngEarth_nlndw.report()`. Stoppen: `beamngEarth_nlndw.stop()`.

## Verificatie en resterend werk

Geslaagd: echte v5-invoer en veldmapping, behoud van groepering/volgorde/waarden/condities, geen foto-URL's in uitvoer, gecachete download zonder herhaalverzoek, streaming van gzip, deterministische uitvoer, quarantaine van dubbele IDs, ongeldige coördinaten, geen voltooiingsmanifest bij afgebroken invoer, gebruik van echte projectgeodesie/tegelkeys, 3 × 3 begrenzing, hergebruik van bestanden, dichtstbijzijnde-40-selectie, teleport-/rebase-opruiming en verwijderen van verafgelegen caches.

Nog nodig voor het einddoel: verdere correcte RVV-/onderbordassets en parametrische tekstvarianten, bronkwaliteit en volledigheid per regio, bevestiging van richting/montagebeleid, integratie met bestaande serverstreaming en gegenereerde snelheidsborden, praktijkmetingen bij grotere aantallen en landelijke schaalvalidatie. De pull request blijft uitgesteld tot het afgesproken geheel klaar en getest is.
