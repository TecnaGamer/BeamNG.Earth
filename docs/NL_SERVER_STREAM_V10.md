# V10 — Nederlandse borden vanuit de companion server

De server levert nu alleen de nabije bordgegevens via de bestaande niet-blokkerende TCP-verbinding met het spel. De client heeft de landelijke data-ZIP's niet meer nodig. De gedeelde modellen en textures blijven lokaal in de bestaande Nederlandse modelmod.

**De vier binnenste data-ZIP's hoeven niet te worden uitgepakt.** Als ze nu worden uitgepakt, kan die uitpakactie worden gestopt. Bewaar de oorspronkelijke vier ZIP's. Alleen de buitenste `BeamNG_Earth_Nederland_v9_datapakket.zip` wordt één keer uitgepakt, om deze vier bestanden te krijgen. Zet ze bij elkaar in één map op de machine waar de companion server draait; plaats ze voor servermodus niet in de mods-map van het spel.

De bestaande v9-bronselectie blijft behouden: 1.316.826 ondersteunde hoofdbronrecords uit de momentopname van 5 november 2026. Het betreft een nieuwe overdrachtswijze, geen nieuwe inventarisatie of oplossing van de eerder gemelde rotatie-/onderbordproblemen.

## Update installeren

Sluit BeamNG en de companion server voor het toepassen van de update. Pak de **kleine v10-update** uit. Open daarin PowerShell en voer uit met de echte paden naar de gebruikte BeamNG.Earth-repository en de bestaande Nederlandse v9-modelmod in de actieve mods-map:

```powershell
./tools/install_nl_stream.ps1 -ProjectDirectory 'PAD/NAAR/BeamNG.Earth' -ModelModZip 'PAD/NAAR/mods/BeamNG_Earth_NL_v9_mod.zip'
```

Dit script past de twee integratiebestanden met een gecontroleerde Git-patch aan, kopieert de nieuwe modules en werkt drie Lua-bestanden **binnen de bestaande model-ZIP** bij. Er worden geen modellen of textures uitgepakt. Het ZIP-bestand houdt zijn bestaande naam. Er komt eerst een reservekopie onder `backups/nl-streaming` in de gekozen repository. Bij een fout worden de gewijzigde bestanden hersteld.

Voor al uitgepakte mods is er een tweede vorm. Geef zowel de gebruikte Earth-modmap als de Nederlandse aanvulling op, zodat de drie Lua-bestanden in beide mappen overeenkomen en een oudere kopie de update niet overschaduwt:

```powershell
./tools/install_nl_stream.ps1 -ProjectDirectory 'PAD/NAAR/BeamNG.Earth' -GameModDirectories 'PAD/NAAR/mods/unpacked/beamng_earth','PAD/NAAR/mods/unpacked/nl_c1_proef'
```

Hierbij worden uitsluitend de drie Lua-bestanden bijgewerkt; modellen en gegevens blijven staan. Er wordt niets opnieuw uitgepakt.

Er zijn patches voor de oorspronkelijke ontwikkeltak en de huidige BGT-ontwikkeltak opgenomen. De BGT-variant is via een afzonderlijke driewegmerge voorbereid; bestaande BGT-aanpassingen blijven behouden. Als de versie niet overeenkomt, stopt het script voordat bronbestanden worden gewijzigd. Git moet beschikbaar zijn. Dit hulpmiddel is voor een repository-installatie, niet voor het herschrijven van een verpakte release-executable.

De draaiende BGT-server, de actieve spelmod en de andere werkmap zijn tijdens de ontwikkeling niet gewijzigd of herstart. De server- en speltest moeten na installatie nog met elkaar worden uitgevoerd.

## Server starten

Voeg deze optie aan de bestaande serveropdracht toe; behoud eventuele andere opties zoals de BGT-instellingen:

```text
--nl-signs-store "PAD/NAAR/MAP/MET/DE/VIER/DATA-ZIPS"
```

Via de bestaande startwrapper kan dit ook:

```powershell
python start.py -- --nl-signs-store 'PAD/NAAR/DATAMAP'
```

Als alternatief leest de server hetzelfde pad uit de omgevingsvariabele `BNE_NL_SIGNS_STORE`. Een bestaande uitgepakte tegelmap met `manifest.json` en `render` werkt ook, maar uitpakken is niet nodig. De server controleert bij ZIP-invoer de manifesttelling en weigert een onvolledige combinatie van delen. Gebruik één gegevensversie per map en herstart de server na het vervangen van de gegevens.

De server meldt de beschikbaarheid in zijn normale begroetingsbericht. De bijgewerkte client schakelt de bronstreaming daarna automatisch in zodra Earth actief is. De v9-consoleopdracht `startNetherlands()` is voor de oude lokale modus en is hierbij niet nodig. Zonder geconfigureerde dataset of bij een oudere server blijft de nieuwe bronstreaming uit. Een fout in de optionele bordendienst blokkeert geen terrein-/wegopbouw.

Rapportage in de GE-Lua-console:

```lua
dump(require('beamngEarth/nlremote').report())
dump(beamngEarth_nlndw.report())
```

## Omvang en cache

De client bewaart maximaal negen nabijgelegen tegels, inclusief lege tegels, in het geheugen. Alleen ontbrekende tegels worden aangevraagd; oude worden verwijderd. Er is één aanvraag tegelijk, een tijdslimiet van tien seconden en een wachttijd bij fouten. Antwoorden met een oud aanvraagnummer, een andere datasetrevisie, ongeldige velden of een verkeerde tegel worden niet geplaatst. Teleporteren, een nieuwe wereldankerpositie en opnieuw verbinden maken lopende aanvragen ongeldig. Buiten het Nederlandse zoekgebied (3–8 graden oosterlengte, 50–54 graden noorderbreedte) worden geen bordaanvragen gestuurd.

De bestaande weergavegrens blijft maximaal 40 hoofdbronrecords binnen 120 meter. Paalgroepering, onderbordvolgorde, plaatsingscontrole tegen wegcorridors en hergebruik van ongewijzigde sceneobjecten gebruiken dezelfde renderer als v9.

De server houdt een gedeelde begrensde cache van maximaal 128 tegels en 16 MiB aan oorspronkelijke tegelinhoud bij. De ZIP-bestandsindex wordt eenmalig op de server opgebouwd, niet in het spel. Dat kost geheugen op de server, maar er vindt geen landelijke scan per aanvraag plaats. Bord-I/O heeft één eigen worker met maximaal acht lopende/wachtende opdrachten voor alle verbindingen samen. Per verbinding is één aanvraag tegelijk toegestaan, maximaal twee nieuwe aanvragen per seconde. De maximale berichtgrootte voor deze dienst is 2 MiB en maximaal 5.000 bronrecords per antwoord.

Er worden geen foto's of NDW-aanvragen in het spel of in deze endpoint gedaan. Het pad naar de data wordt uitsluitend door de serverbeheerder ingesteld; een client kan geen bestandspaden kiezen.

## Gecontroleerd resultaat

Een afzonderlijke lokale TCP-test gebruikte de echte vier landelijke ZIP's, de echte serverdispatcher, de gewone protocolframing en de echte nieuwe Lua-client onder LuaJIT 2.1. Alle gecontroleerde bron-ID's, exacte coördinaten, bordcodes, richtingen en onderbordvolgordes kwamen ongewijzigd over. Het betrof Blerick, Amsterdam, Rotterdam, Den Haag, Utrecht, Groningen, Maastricht, Enschede, Leeuwarden, Vlissingen en Den Burg op Texel.

De elf antwoorden samen waren 114.665 bytes, tegenover 238.125 bytes aan overeenkomstige v9-tegelinhoud: **51,85% minder bytes in deze steekproef**. Dit is een vergelijking van de compacte overdracht met de oude ongecomprimeerde tegel-JSON, geen extra compressie van de landelijke download-ZIP. Er zijn nul individuele tegelbestanden uitgepakt. De gemeten lokale aanvraagtijden zijn geen FPS-meting of prestatiegarantie voor een externe server of het spel.

Verder geslaagd: acht Python-tests voor archieven/protocol/afhandeling, de Lua-tests voor cache/late antwoorden/herverbinden/teleport/negatieve cache, de remote modus van de bestaande tegelrenderer zonder lokale bestandslezingen en de bestaande core-regressies (286 + 64 controles), plus framing en plaatsings-/opruimtests. De testafhankelijkheid `lupa==2.6` is alleen voor ontwikkeling; de spel- en servercode krijgen geen nieuwe externe afhankelijkheid.

Een nieuwe visuele BeamNG-test is nog niet uitgevoerd. Onderbordvarianten, bronrotaties, modelmaten, resterende oude bordenlagen en landelijke spelprestatievalidatie blijven het eerder beschreven vervolgwerk. Er is geen pull request gemaakt.
