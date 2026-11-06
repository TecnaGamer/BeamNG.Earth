# V9 — landelijke George-tegels

De geografische beperking tot Blerick/Venlo is opgeheven. De openbare George-v5-bron is zonder gemeentefilter of wegbeheerderfilter opgehaald op 5 november 2026, met `status=PLACED` en CRS84. De landelijke bron bevat 1.973.849 records. Daarvan zijn 1.971.266 geldig voor verdere verwerking; 2.583 records met een verwijderdatum worden ook bij deze PLACED-query conservatief uitgesloten. Er zijn geen dubbele bron-ID's aangetroffen.

Er zijn **1.316.826 ondersteunde hoofdbronrecords** beschikbaar in **197.467 lokale weergavetegels**, samen 236.525.011 bytes ongecomprimeerde tegelinhoud. Dit is landelijke brondekking, geen verklaring dat ieder bestaand Nederlands verkeersbord aanwezig is of correct gemonteerd wordt. Een bronrecord kan bovendien meerdere platen hebben. Het aantal bronrecords is dus geen telling van de uiteindelijk zichtbare palen of platen.

## Installatie

Behoud de bestaande basisinstallatie van BeamNG.Earth. Schakel eerdere Nederlandse proefmods en eerdere landelijke datapakketten uit; vervang de v8-aanvulling door `BeamNG_Earth_NL_v9_mod.zip`. Zet die ZIP in de actieve BeamNG-gebruikersmap onder `mods`.

Pak **`BeamNG_Earth_Nederland_v9_datapakket.zip` één keer uit**. Zet de vier daarin opgenomen `BeamNG_Earth_Nederland_v9_data_01.zip` t/m `04.zip` ook in diezelfde `mods`-map, zonder deze vier ZIP's verder uit te pakken. De buitenste bundel-ZIP zelf is geen spelmod. Alle vier delen zijn nodig. Elk spelarchief blijft onder 50.001 bestanden; er is geen afhankelijkheid van ZIP64 voor een archief met bijna 200.000 bestanden.

Herstart het spel of de kaart om oude Lua-modules en objecten kwijt te raken. Open vervolgens BeamNG.Earth op de gewenste Nederlandse locatie. Voer deze regels afzonderlijk uit in de **GE-Lua**-console; voeg geen `lua` voor de regels toe:

```lua
extensions.load('beamngEarth_nltestset')
extensions.load('beamngEarth_nlndw')
beamngEarth_nlndw.startNetherlands()
```

De bestaande Venlo-proefstart blijft beschikbaar, maar selecteert niet vanzelf de landelijke dataset. Gebruik daarvoor de nieuwe landelijke start hierboven.

Rapport opvragen of bewaren en stoppen:

```lua
dump(beamngEarth_nlndw.report())
beamngEarth_nltestset.saveReport()
beamngEarth_nlndw.stop()
```

De modelaanvulling bevat de eerder aangepaste `trees.lua`, gebaseerd op de geïnstalleerde Earth 1.8.1-beta. Bij een andere basisversie moet deze module eerst worden vergeleken.

## Laden tijdens rijden

Het spel leest maximaal negen nabijgelegen tegels en kiest maximaal 40 hoofdbronrecords binnen 120 meter. Het scant niet alle landelijke bestanden en vraagt geen foto's, OCR of George-API aan. De catalogusillustraties zijn gedeelde lokale modellen/textures; er is geen foto per bordlocatie.

Ongewijzigde paalgroepen behouden hun sceneobjecten. Alleen nieuwe, gewijzigde of verdwenen groepen worden toegevoegd of verwijderd. Bij een gedeeltelijk beschikbare wegkaart worden overgeslagen plaatsingen na vijf seconden opnieuw onderzocht. Bij ankerwissels, teleporteren, stoppen of kaartsluiting worden oude objecten opgeruimd. Deze gedragingen zijn lokaal getest; landelijke FPS, laadtijd en geheugengebruik zijn nog niet in BeamNG gemeten. De limiet van 40 is bewust behouden voor deze volgende testversie.

De regels uit v8 blijven gelden: gedeelde palen voor passende bronposities en richtingen, eigen richting/beugels per plaat, onderborden aan hun hoofdplaat, paaltop onder de hoogste plaat en verplaatsing buiten de beschikbare wegcorridors. Ontbrekende weggegevens leiden tot overslaan. Bekende afwijkingen in bronrotaties, onvolledige wegbreedtes, boom-/gebouwruimte en de oude samengestelde wegmesh-borden zijn nog geen afgeronde punten.

## Brongegevens die nog geen weergave hebben

De build houdt per hoofdrecord de eerste blokkerende reden bij:

| Reden | Records |
|---|---:|
| Hoofdvariant/waarde ontbreekt in assets | 245.235 |
| Exact onderbord ontbreekt | 350.813 |
| Zonevariant ontbreekt of is onduidelijk | 55.420 |
| Montage niet langs de weg | 1.835 |
| Richting ontbreekt/is ongeldig | 873 |
| Combinatie onderbord niet ondersteund | 259 |
| Montagegrens proefweergave | 5 |

De oorspronkelijke bron blijft volledig in de offline bouwcache bewaard; het spelpakket bevat alleen de ondersteunde weergaverecords. Er zijn 481.533 onderbordonderdelen in de geldige bron, waarvan 431.336 met de broncode OTHER. Dit zijn onderbordonderdelen, geen extra hoofdrecords; tel deze cijfers niet bij de tabel op. Ontbrekende teksten, tijden, afstanden en lokale plaatsnamen worden niet verzonnen.

De expliciete richtingskoppelingen omvatten C4/D5/D6 met L/R en D2 met LO/RO. D2-LO/RO verwijst naar links-/rechtsonder; LB/RB of een ontbrekende richting wordt niet stilzwijgend omgezet. [NDW beschrijft de wijziging naar LO/RO in de George-releases](https://docs.ndw.nu/en/handleidingen/George/releases/). De eerdere catalogusbeperkingen rond voorlopige maten en de visuele acceptatie van 800 vermeldingen blijven gelden; zie `NL_V8_CATALOG_PLACEMENT.md`.

## Reproduceerbare bouw

Node 22.13 of nieuwer is nodig voor de ingebouwde SQLite. Download alleen tijdens de offline voorbereiding, nooit in de spelrenderer:

```powershell
node tools/fetch_george_national.mjs work/george-cache 2026-11-05 --netherlands
node tools/build_george_tiles.mjs work/george-cache/netherlands-v5-2026-11-05.geojson.gz work/george-netherlands PLACED --runtime-only
node tools/test_george_national_coverage.mjs work/george-netherlands
./tools/package_nl_preview.ps1 -Store work/george-netherlands -OutputDirectory dist/nl-v9
```

Gebruik een nieuwe uitvoermap voor iedere tegelbouw en pakketbouw. De pakketbouwer gebruikt de gewijzigde/nieuwe bestanden tegenover Git HEAD en is bedoeld voor de huidige ontwikkeltak met alle catalogusassets; geen algemene releasebouwer. De installatiehandleiding moet voor het bundelen ook als `Nederland_v9_installatie_en_resultaten.md` in de uitvoermap staan. Landelijke ruwe gegevens, tijdelijke SQLite en honderdduizenden tegels horen niet in de Git-diff.

De downloader bewaart herkomst en een SHA-256-hash en controleert een bestaande cache voor hergebruik. De tegelbouwer leest GeoJSON/gzip stapsgewijs, gebruikt een tijdelijke schijfindex en schrijft `manifest.json` pas na een volledige succesvolle bouw. Een afgebroken map zonder manifest is geen bruikbare dataset. De bronhash van deze momentopname is `7f545be46781076ef6eb7a213de582ec7e7a196cb4401067c9e51bd27f9dcc8f` voor de gecachte gzip.

## Controle en vervolg

Geslaagd: landelijke verwerking en tellingen; gegevens aanwezig rond Blerick, Amsterdam, Rotterdam, Den Haag, Utrecht, Groningen, Maastricht, Enschede, Leeuwarden, Vlissingen en Den Burg op Texel; onderbordgroepering en bronherkomst; richtingsaliassen; begrensd lokaal laden; hergebruik van ongewijzigde sceneobjecten; herproberen bij ontbrekende wegen; teleport-/ankeropruiming; gesloten catalogusgeometrie/materialen; Forest-regressies (137 controles).

Deze versie is zonder nieuwe speltest gebouwd, conform het verzoek voorlopig verder te werken zonder speltest. Volgende inhoudelijke stappen zijn de nog ontbrekende parametrische onderbord-/zonevarianten, resterende richtingsafwijkingen en praktijkmetingen van de landelijke streaming. Een pull request wordt pas gemaakt nadat het afgesproken geheel klaar en gecontroleerd is.
