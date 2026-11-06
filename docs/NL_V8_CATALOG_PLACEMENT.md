# V8 — gedeelde palen, bermplaatsing en volledige catalogusinventaris

## Resultaat

De 800 vermeldingen op de opgegeven Informatiebord-overzichtspagina zijn gecachet en omgezet naar 830 afzonderlijke bordplaten, in 827 verschillende modellen. Dubbele verwijzingen kunnen een model delen. Iedere plaat heeft gesloten geometrie, een achterzijde, 3 mm plaatdikte en een 12 mm vouwrand. De bronillustraties zijn als RGB 8-bit 512×512-textures opgeslagen; de mesh/UV-verhouding herstelt de oorspronkelijke verhoudingen. Er zijn 28 vermeldingen met meerdere losse platen. Het zijn dus geen 800 verschillende RVV-hoofdcodes.

De 7 eerder gebruikte varianten blijven beschikbaar, met voorkeur voor de bestaande vectorafbeeldingen waar de code exact overeenkomt. Er zijn 662 extra eenduidige éénplaatkoppelingen toegevoegd: de gecombineerde assetregistratie heeft 669 sleutels. Alle 800 oorspronkelijke vermeldingen blijven daarnaast volledig als code-naar-plaatonderdelenmanifest beschikbaar, inclusief combinaties en formaatvarianten. Een afzonderlijke catalogusvermelding met meerdere platen wordt niet stilzwijgend als één groot bord getekend of aan een onduidelijk NDW-record gekoppeld.

De nieuwe modellen zijn geometrisch gecontroleerd, maar nog niet alle 800 visueel in BeamNG beoordeeld. Hun schaal is voorlopig 0,6 m breed per afgesplitste plaat met behoud van beeldverhouding, geen op productmaten gevalideerde maatbibliotheek. Dat is vooral voor plaatsnaamborden, bewegwijzering en bebakeningsborden nog een beperking. De oorspronkelijke drie flespalen en de eerdere gecontroleerde bordmaten blijven behouden. De status per catalogusmodel vermeldt de openstaande spelcontrole.

Bron: https://www.informatiebord.nl/oefenen/verkeersborden-overzicht/ . Bronpagina, afbeeldings-URL en hash zijn vastgelegd. Dit pakket gebruikt de op verzoek gecachete standaardbordillustraties; er wordt geen afzonderlijke exclusieve licentie of vrijgave voor de styling van de website geclaimd. Dit is geen verklaring dat iedere afbeelding op de website zonder verdere beoordeling mag worden hergepubliceerd.

## Eén paal voor meerdere bronborden

Bronrecords binnen 0,75 m worden gegroepeerd als de richtingen vrijwel gelijk of vrijwel tegenovergesteld zijn (20 graden tolerantie). Haaks op elkaar gerichte borden krijgen automatisch geen gedeelde paal. De bron-ID's blijven in het rapport terug te vinden.

Borden voor dezelfde kijkrichting worden met 6 cm tussenruimte gestapeld. De standaardvolgorde volgt de RVV-bijlage: later genoemde hoofdborden komen boven eerder genoemde. Een expliciet meegegeven `mount_order` wordt gerespecteerd; het standaard George-v5-bestand bevat zelf geen gemeten volgorde van meerdere hoofdborden. Onderborden blijven direct aan hun eigen hoofdbord gekoppeld, met behoud van hun door George aangeleverde index/groepering. De bron ondersteunt niet automatisch de exacte fysieke montage van iedere paal.

Voor de plaatsingsregel is de officiële BABW-toelichting geraadpleegd: https://zoek.officielebekendmakingen.nl/stcrt-1997-239-p16-SC11635.pdf . Dit is de technische standaardvolgorde in de generator, geen verklaring dat iedere gegenereerde combinatie juridisch of constructief op elke locatie toegestaan is.

Bij rug-aan-rugmontage heeft elke plaat zijn eigen draaihoek en beugels, terwijl de paal wordt gedeeld. De bovenkanten worden uitgelijnd; de paaltop blijft 5 cm onder de hoogste plaat. De generator kiest uit de drie bestaande paallengtes. Als een stapel niet past met voldoende grondinsteek, wordt hij geregistreerd en overgeslagen. Voor lage platen zijn beugelsets met 10 cm of 3 cm tussenruimte toegevoegd.

## Niet in de bekende rijbaan plaatsen

De plaatsingsmodule gebruikt de bestaande navigatieweggeometrie van Earth/BeamNG, inclusief de radii/breedtes van de wegverbindingen. Er wordt conservatief extra ruimte om de rijbaan aangehouden. Een bronpunt binnen een wegcorridor wordt naar een nabije kandidaat buiten alle nabijgelegen corridors verplaatst. De oorspronkelijke zijde heeft voorkeur; exact op de hartlijn wordt de rechterzijde van de bronrijrichting als eerste onderzocht. De verplaatsing is begrensd tot 15 meter.

Een kandidaat die nog in een kruisende rijbaan valt wordt geweigerd. Bij ontbrekende weggeometrie, te grote afstand tot een bekende weg of geen vrije kandidaat wordt het bord niet geplaatst. De verplaatsing en reden van overslaan worden gerapporteerd. De hoogte komt op de gekozen positie uit het bestaande TerrainBlock.

Dit is een controle tegen de beschikbare weggeometrie, geen beeldherkenning van asfalt. Parkeerstroken, onvolledige breedtes, ontbrekende wegen of afwijkende gerenderde randen kunnen alsnog een visuele afwijking geven. De nieuwe regels zijn lokaal getest en moeten in de echte scène worden gecontroleerd voordat we kunnen stellen dat de zichtbare asfaltgrens overal wordt gerespecteerd. Gebouw-, boom-, water- en vrije-loopruimtecontrole is nog niet volledig uitgewerkt.

## Oude automatisch gegenereerde borden

De Forest-laag krijgt een lokale vervangingsregel. Alleen een automatisch gegenereerd STOP-/voorrangsbord of een bijbehorend Forest-snelheidsbord binnen 8 m en met overeenkomende kijkrichting (30 graden tolerantie) wordt verborgen wanneer een passend bronbord aanwezig is. Tegenovergestelde benaderingen en begroeiing blijven staan. Na opruimen van de bronset verschijnt de oorspronkelijke laag weer. Ongewijzigde vervangingspunten veroorzaken geen extra herbouw.

De wijziging is gebaseerd op dezelfde `trees.lua` als de geïnstalleerde Earth 1.8.1-beta; dat is gecontroleerd. De mod bevat daarom ook deze aangepaste Forest-module. Een andere basisversie moet eerst worden vergeleken. Snelheidsborden die onderdeel van een samengestelde wegtegelmesh zijn, vallen niet onder de Forest-regel; die kunnen nog blijven staan. Er wordt geen volledige oude bordenlaag wereldwijd uitgeschakeld.

## Gegevens en laadtijd

George blijft de bron voor locaties, richtingen en onderbordgegevens. Catalogusillustraties zijn gedeelde assets, geen foto's die per bronlocatie worden opgehaald. Het spel doet geen foto-, OCR- of API-aanvragen. Alleen de materialen van de geselecteerde zichtbare borden worden geregistreerd; het grote catalogusmaterialenbestand wordt niet telkens volledig ingeladen.

Dezelfde Venlo-selectie bevat 798 bronrecords, verdeeld over 56 tegels. Met de nieuwe exacte assetkoppelingen komen 526 records in aanmerking. Onvolledige OTHER-onderborden en niet exact overeenkomende varianten blijven onopgelost. De zichtbare proef blijft maximaal 40 hoofdbronrecords binnen 120 m, met een cache van maximaal negen lokale tegels. Heel Nederland is nog niet ingeladen en landelijke prestaties zijn nog niet gemeten.

## Installeren en controleren

Vervang de vorige proefmod volledig door v8. Laat geen oudere versies tegelijk actief. Herstart de kaart of het spel; een geladen Lua-module wordt niet vanzelf vervangen. De gedeelde-paallogica betreft George-bronrecords; een achterblijvend object uit een andere laag kan dus niet als tweede bronbord aan dezelfde paal worden gehangen.

```lua
extensions.load('beamngEarth_nltestset')
extensions.load('beamngEarth_nlndw')
beamngEarth_nlndw.startStore('/levels/beamng_earth/data/george_venlo/manifest.json','travel_bearing')
```

Rapport: `beamngEarth_nlndw.report()`. Stoppen: `beamngEarth_nlndw.stop()`. De catalogus is na uitpakken te bekijken via `catalogus_overzicht.html` in de mod. Daarin zijn alle 800 codes en hun afzonderlijke platen doorzoekbaar.

## Validatie

Geslaagd: 800 bronvermeldingen en 830 plaatonderdelen, 827 gesloten modellen, indexbereik, niet-ontaarde driehoeken, frontwinding, RGB/power-of-two textures, materiaalkoppelingen en bestandsverwijzingen. De bestaande renderer-/hoogte-/geodesie-/rotatie-/lifecycle-tests slagen. De nieuwe tests controleren gedeelde en tegenovergestelde kijkrichtingen, beugelhoeken, bronvolgorde en RVV-prioriteit, wegcorridors, kruisingen, begrensde verschuiving en weigering zonder wegdata. De bestaande Forest-suite plus lokale vervangingscontrole heeft 137 geslaagde controles, nul fouten, uitgevoerd in een echte Lua-5.4-VM; dit vervangt geen BeamNG/LuaJIT-praktijktest.

De nieuwe modellen, nieuwe schaalkeuzes, verplaatsing, samenvoeging en Forest-vervanging zijn nog niet live in BeamNG geaccepteerd. Er is geen nieuw FPS-/VRAM-/RAM-resultaat geclaimd. Er is geen pull request geopend.

## Reproduceren

```sh
node tools/fetch_nl_catalog_source.mjs
node tools/build_nl_site_catalog.mjs OUTPUT-CATALOG-DIRECTORY PATH-TO-PNGJS-PACKAGES
node tools/generate_nl_testset.mjs
node tools/test_nl_site_catalog.mjs
node tools/test_nl_testset.mjs --fengari PATH-TO-lua-cli.js
node tools/run_lua_wasm.cjs PATH-TO-WASMOON-PACKAGES
```

De broncache wordt geverifieerd en hergebruikt. PNG-conversie gebruikt de reeds vastgelegde pngjs-versie. De optionele Lua-testomgeving is vastgelegd in `tools/lua_test_runtime/package.json`. Het bronpakket bevat wijzigingen tegen repositorybasis `b397e20dce08f040cc62b817cd7c0a2ad28cc85f`, inclusief cached bronillustraties, scripts, manifesten en tests.
