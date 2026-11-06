# V12 — alleen de nieuwe RVV-borden

Deze update schakelt de oude automatisch geplaatste verkeersborden van Earth volledig uit. De zichtbare RVV-platen uit de NDW/George-renderer blijven staan. Dit is geen verwijdering op basis van een kleine zoekcirkel rond een nieuw bord: ook waar NDW-data ontbreekt of een bordvariant nog niet ondersteund is, verschijnt geen oud vervangend bord.

De volgende oude bronnen worden afgehandeld:

- Forest-objecten voor STOP, voorrang verlenen (inclusief landvarianten) en oude snelheidsborden. Gekoppelde plaat-/paalonderdelen worden samen verwijderd.
- De afzonderlijke wegenmeshes voor oude snelheidsborden, afritten, afstands-/mijlpaalborden, invoegwaarschuwingen en de eerder gegenereerde richtingpanelen (`dirs`).
- Nog zichtbare oude borden uit vervangen tegels die al in een verwijderwachtrij staan.

De opruiming gebruikt de bekende eigenaren van die objecten. Er wordt niet willekeurig ieder object met “sign” in de naam verwijderd. Zo blijven de RVV-objecten, bomen, verkeerslichten, reclameborden op gebouwen, wegmarkeringen, rails en ander straatmeubilair behouden. De prioriteitsregels voor AI blijven eveneens intact. De standaard Earth-levelbestanden bevatten geen aparte laag oude statische verkeersborden die daarnaast gewist moet worden; handmatig door een gebruiker geplaatste objecten vallen niet onder deze automatische laagopruiming.

## Laden, herverbinden en instellingen

Zodra Earth actief is en de actuele speler-/camerapositie binnen Nederland ligt, staat RVV-only aan. Buiten Nederland blijven de normale borden actief en stopt de Nederlandse bronrenderer. Onbekende positie en een nog niet afgeronde teleport schakelen de onderdrukking niet in. De test gebruikt de volledige officiële landsgrens van Kadaster/PDOK (Landgebied LND6030, opgehaald 6 november 2026), inclusief gaten/enclaves, lokaal verpakt in `nl_country.lua`; er is geen netwerkverzoek per positie. [Grensbron](https://api.pdok.nl/kadaster/bestuurlijkegebieden/ogc/v1/collections/landgebied). Al geladen oude borden worden direct uit de beheerde scèneobjecten verwijderd. Nieuw ontvangen tegels worden gefilterd, ook als ze na een teleport of met vertraging aankomen. Het normale `highwaySigns`-vinkje kan oude borden in deze modus niet opnieuw inschakelen.

De client meldt de modus tijdens de bestaande serverbegroeting en bij een wijziging. De server stopt oude bordenopdrachten, sluit hun workertracker en laat de bestaande oude bordentegels opruimen. Een resultaat van een afgesloten oude opdracht wordt niet meer uitgestuurd. Dit geldt per verbinding; een andere client zonder deze RVV-modus krijgt zijn eigen gewone beleid.

Als de RVV-dataset ontbreekt of de verbinding uitvalt, blijven de oude borden binnen Nederland uit. Dat kan lege plekken opleveren: het vult ontbrekende gegevens niet met verzonnen borden. Normale andere BeamNG-levels zonder actieve Earth-wereld krijgen deze filter niet. Bij een oudere server werkt de lokale filter ook; het vermijden van serverwerk vereist de nieuwe servercode.

De instelling is losgekoppeld van de oude nabijheidsvervanging. Opruimen van de nieuwe bronset laat oude borden dus niet meer terugkomen. De clientrapportage via `require('beamngEarth/nlremote').report()` toont `rvv_only` en `legacy_suppression_active`.

## Installeren

V12 bevat ook de serverstreaming en maat-/rotatieverbeteringen van v10/v11. De modellen en landelijke gegevens blijven dezelfde: geen nieuwe grote databundel downloaden of uitpakken.

Sluit eerst BeamNG en de companionserver. Voor uitgepakte mods:

```powershell
./tools/install_nl_stream.ps1 -ProjectDirectory 'PAD/NAAR/BeamNG.Earth' -GameModDirectories 'PAD/NAAR/mods/unpacked/beamng_earth','PAD/NAAR/mods/unpacked/nl_c1_proef'
```

Bij een bestaande Nederlandse model-ZIP kan `-ModelModZip 'PAD/NAAR/modelmod.zip'` worden gebruikt. De updater bewaart reservekopieën en werkt alleen code en de kleine maatprofielen bij. Het pakket bevat gecontroleerde integratie-/upgradepatches voor de gebruikte basis en BGT-varianten, inclusief de eerder gebruikte v10/v11-opstelling. Bij afwijkende broncode stopt de updater om bestaande wijzigingen te behouden.

Start daarna de server met de bestaande opties en `--nl-signs-store 'MAP/MET/DE/VIER/DATA-ZIPS'`, en herstart het spel. De actieve installatie wordt niet tijdens deze ontwikkeling aangepast of herstart.

## Verificatie

Gecontroleerd: directe verwijdering van geladen oude Forest-/meshborden, late tegels, afwezige RVV-data, instellingen die oude borden niet mogen terugbrengen, behoud van onafhankelijk geplaatste RVV-objecten en ander straatmeubilair, afhandeling van oude objectwachtrijen en hergebruik/vrijgeven van meshslots. Servertests controleren het sluiten van oude opdrachten en het opruimen van bordentegels.

Een nieuwe visuele BeamNG-test na installatie staat nog open. De al beschreven beperkingen van NDW-volledigheid, bijzondere bordmaten en twijfelachtige bronrotaties blijven gelden. Er is geen pull request gemaakt.
