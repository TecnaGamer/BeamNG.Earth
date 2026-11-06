# Kleine Nederlandse verkeersbordentest: 40 borden

Deze stap volgt op de door de gebruiker in BeamNG gecontroleerde C1-proef: texture, paaltop, achterkant, vouwrand en twee beugels waren zichtbaar. De nieuwe set is nog niet in het echte spel geaccepteerd. Er wordt geen landelijke NDW-import gestart.

## Inhoud

| Groep | RVV-code | Aantal |
|---|---|---:|
| Snelheid | A1-50 | 10 |
| Voorrang | B6 | 10 |
| Verbod | C1 | 10 |
| Waarschuwing | J16 | 10 |

Dit zijn **40 exemplaren van vier representatieve codes**, geen bibliotheek van 40 verschillende borden. De borden staan in een herkenbare synthetische testopstelling van ongeveer 36×21 m. Zij beschrijven geen werkelijke verkeerssituatie. Eén C1 en één A1-50 delen een paal. De overige borden hebben elk een paal: 39 palen, 40 beugelsets en 40 bordplaten, samen 119 TSStatic-objecten.

Drie oorspronkelijke paallengtes worden gebruikt: 300, 330 en 360 cm. De paaltop blijft steeds 5 cm onder het hoogste bord. Begraving is per combinatie 60–110 cm, zonder de paal te schalen of meer geometrie te geven. Een paal heeft nog steeds 32 vertices en 60 driehoeken. Het samengestelde voorbeeld heeft voldoende ruimte tussen beide borden. De beugelparen staan 28 cm uit elkaar, passend binnen de driehoekige platen; de twee losse beugels hergebruiken de oorspronkelijke geometrie.

Ronde borden zijn 60 cm breed, driehoekige 70 cm breed met de bronverhouding. Alle platen hebben 3 mm vlakdikte en een 12 mm omgezette achterrand. Afgeronde driehoeken volgen de contour van de bronafbeelding. Dit zijn gekozen proefafmetingen, geen uitspraak over een specifieke geïnventariseerde montage.

## Installatie en plaatsing

1. Sluit BeamNG af. Pak `BeamNG_Earth_NL_testset_40_mod.zip` uit.
2. Kopieer de mappen `levels` en `lua` naar `mods/unpacked/nl_c1_proef`. De oude C1-proef kan aanwezig blijven, maar verwijder een geplaatst proefbord voordat je de nieuwe set plaatst. Gebruik geen tweede actieve kopie van deze testmod.
3. Start BeamNG, kies een ruime vlakke plek en zet een auto stil. Gridmap ondersteunt de visuele eerste controle. Voor de geografische controle start je daarna een Nederlandse locatie in BeamNG.Earth en wacht je tot de kaart stabiel is.
4. Open de console, kies **GE Lua** en voer uit:

```lua
extensions.load('beamngEarth_nltestset')
beamngEarth_nltestset.nearCar(-90)
```

De set start tien meter verder in zowel X als Y en strekt zich nog 36 m in X en 21 m in Y uit. Kies dus vooraf een geschikte open plek. De getallen zijn gekozen demonstratieplaatsen. Er vindt geen automatische berm-, weg- of waterherkenning plaats. De functie retourneert **119** bij succesvolle plaatsing. Alle objecten hebben collision uit.

Gebruik F11 en de vrije camera om de vier rijen en de combinatiepaal te inspecteren. De eerste snelheidsbordpositie blijft leeg: dat bord deelt de eerste C1-paal. De kijkrichting -90 is expliciet gekozen in de actuele spelwereld, geen NDW-richting. Opnieuw `nearCar` vervangt de huidige set zonder duplicaten.

```lua
beamngEarth_nltestset.clear()
```

Dit ruimt alles op. Houd de auto voor inspectie binnen 150 m van de testopstelling; daarbuiten wordt de set verwijderd.

## Geografische controle

De module leest alleen de bestaande `beamngEarth_core._state()`; zij wijzigt geen core, server of kaartgenerator. Bij een actieve Earth-sessie gebruikt zij het bestaande `anchor:worldToGeodetic` voor de bemonsterde lokale plaatsen en `geotile.tileOf`/`key` voor zoom-17-tilekoppelingen. Per paal worden werkelijke berekende lat/lon, hoogte en tilekey in het rapport vastgelegd. In Gridmap zonder actieve Earth-sessie meldt het rapport uitdrukkelijk `geographic=false` en blijven geografische coördinaten leeg.

In een actieve Earth-sessie wordt per paal de hoogte van `bngEarthTerrain` gebruikt. Botsingsvlakken van daken en objecten worden overgeslagen. Zonder Earth gebruikt de Gridmap-proef de algemene surface-query. Niet-eindige hoogtes en verschillen van 30 m of meer met de referentieauto worden geweigerd vóór een bestaande set wordt verwijderd. Dit is een bruikbaarheidsfilter, geen bewijs van een juiste bermlocatie. Controleer de maaiveldhoogte visueel.

Bij teleport, pending rebase, ankerwissel, verbroken actieve Earth-status of einde van de kaart wordt de set verwijderd. Dit is veilige tijdelijke lifecycle-ondersteuning: geen blijvende productie-streaming of automatische terugplaatsing na een rebase. In deze stap zijn nog geen NDW-objecten geïmporteerd en is het beheerdersbestand niet beschikbaar.

## Prestaties meten

Kies een vaste camera waarin de hele set zichtbaar is. Houd de auto, camera, grafische instellingen, weer en tijd gelijk. Wacht tot de textures en shaders klaar zijn. Voer daarna uit:

```lua
beamngEarth_nltestset.benchmark()
```

De module verwijdert de set, wacht 3 seconden en bemonstert 10 seconden zonder borden. Daarna plaatst hij dezelfde set terug, wacht opnieuw 3 seconden en bemonstert 10 seconden met borden. Na circa 26 seconden staat de set terug en verschijnt een voltooiingsmelding in de console. Blijf gedurende de meting stilstaan. Herhaal drie keer voor een bruikbare vergelijking. Elke opgeslagen rapportage bevat de laatste run; bewaar elke run onder een eigen naam.

```lua
dump(beamngEarth_nltestset.report())
beamngEarth_nltestset.saveReport()
```

Het rapport staat in de actieve BeamNG-gebruikersmap onder `settings/beamngEarth-nl-testset-report.json`. Het bevat gemiddelde FPS, mediaan en p95 van frametijden, frames en meetduur per fase. Voeg zelf spelversie, hardware, renderer, resolutie, grafische instellingen, vaste camera en screenshots toe. De meting gebruikt `onUpdate(dtReal)` en omvat de hele lopende scène; het is geen geïsoleerde GPU-benchmark. Er is geen meting van totaal procesgeheugen/VRAM of Forest-vergelijking. Een FPS-limiet kan verschil maskeren.

### Werkelijk resultaat bij oplevering

| Onderdeel | Status |
|---|---|
| Aantallen en hergebruik | 40 borden, 39 palen, 119 objecten, 8 modelbestanden, 5 materialen |
| Geometriebudget | 20.580 zichtbare driehoeken, collision uit |
| Textures | Vier 1024×1024 RGB 8-bit; bronbeelden visueel bekeken |
| Modelcontroles | Gesloten bordplaten/vouwranden, winding, afmetingen, montage en materiaalpaden geslaagd |
| Flespalen | Oorspronkelijke posities behouden; 3 lengtes gebruikt |
| Geografische module | Echte projectgeodesie round-trip en tilekeys lokaal getest |
| Lifecycle en meting | Plaatsing, vervanging, ondergrondfout, rebase, afstand, kaartafsluiting en meetfasen met spelstubs getest |
| Echte import van de 40-set | Visueel gecontroleerd in Gridmap; 119 echte scèneobjecten geteld in Earth |
| FPS | Gridmap: 74,942 zonder / 74,971 met set; circa 75 FPS begrenst de conclusie |
| VRAM/RAM | Niet gemeten |

De Lua-test voert 60 FPS als kunstmatige invoer aan om de berekening te controleren. Dat is nadrukkelijk geen gemeten BeamNG-prestatie. De bestanden op schijf en theoretische texturegroottes zijn evenmin runtime-geheugenmetingen.

## Bronnen, bouwen en ontbrekend werk

Bronbeelden: Bouwe Brouwer naar RVV 1990, publiekdomeinverklaring per bestand op [A1](https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_A1_2023.svg), [B6](https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_B6_2023.svg), [C1](https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_C1_2023.svg) en [J16](https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_J16_2023.svg). Geen startertexturen of productfoto's gebruikt. Broncache, conversie en herkomst van de gebruikersgeometrie staan in `tools/nl_test_source/README.md`; hashes en ontbrekende codes staan in het assetmanifest.

```sh
node tools/generate_nl_testset.mjs
node tools/test_nl_testset.mjs --lua-bin luajit
```

De normale generator werkt offline zonder extra packages; alleen optionele herconversie van SVG's heeft de vermelde, vastgelegde Node-packages nodig. De set gebruikt PNG-brontextures om de texture cooker in deze test te kunnen controleren. Voor publicatie moeten gevalideerde, in BeamNG geconverteerde DDS-bestanden worden meegeleverd, volgens de [texture cooker-documentatie](https://docs.beamng.com/modding/materials/texture_cooker/).

Nog ontbrekend: alle overige RVV-codes, parametrische snelheden en onderborden, NDW-schema/import, betrouwbare bronoriëntatie en montage-ID's, dubbele palen, blijvende tile-streaming, Forest-vergelijking, onbeperkte herhaalde FPS-metingen en geheugenmetingen. De PR blijft een lokaal voorbereid concept, zonder landelijke uitrol of automatische productieactivatie.

## Echte Earth-controle 2026-11-05

BeamNG 0.40.4, Direct3D12; Earth 1.8.1 beta, nabij Venlo. Ankerwissel en teleport verwijderen elk de set van 119 naar nul objecten. De onafhankelijke scènecontrole bevestigt nul achtergebleven TSStatic-objecten na teleport. De afstandscontrole ruimde de set eveneens op.

De onafhankelijke terreinmeting vond vóór correctie maximaal 2,8734 m verschil tussen de gekozen surface en het terrein. De Earth-plaatsing gebruikt daarom nu de TerrainBlock-hoogte. Na correctie: 39 gecontroleerde palen, maximaal 7,643e-7 m positioneringsfout en 1,295e-9 m verschil tussen opgeslagen grondhoogte en terrein. Dit bewijst de hoogte voor de proefpunten, geen juiste bermkeuze: de synthetische rasteropstelling kan gebouwen, water of wegen overlappen. Bruggen vereisen later een expliciet beleid.

Bewijs en beperkingen: `docs/research/nl_sign_test_2026-11-05/`. Onafhankelijke audit: `local audit=require("beamngEarth/nlcheck"); audit.save()`; uitvoer in `settings/beamngEarth-nl-scene-audit.json`. Herstart de kaart na het vervangen van Lua-bestanden.
