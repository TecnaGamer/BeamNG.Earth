# Rotatiecorrectie v7

De lader beweegt mee; de melding dat alleen de rotonde laadde is door de gebruiker ingetrokken. Het pakket blijft wel beperkt tot de Venlo-box, de beschikbare assets, 120 meter zichtbereik en maximaal 40 nieuwe hoofdborden tegelijk.

## Hersteld

De quaternion die aan BeamNG werd doorgegeven gebruikte het verkeerde teken. Daardoor werden richtingen gespiegeld; bij sommige windrichtingen was dat veel duidelijker dan bij andere. De correctie volgt de al bestaande `geodesy.beamngRotate`-conventie van het project. De test controleert nu de daadwerkelijk aan `setPosRot` doorgegeven quaternion, op 0, 45, 90, 180, 270 en 315 graden, en niet alleen een los berekende hoek.

Daarnaast kregen beugels de algemene opstellingshoek in plaats van de draaihoek van hun eigen bord. Bordplaat en beugels krijgen nu dezelfde quaternion. Dit voorkomt dat een gedraaide beugel door de voorkant van de plaat steekt.

De uitgebreide geometrie-, hoogte-, geodesie- en rotatietests slagen. Een nieuwe visuele controle in BeamNG is nog nodig. De broninterpretatie `travel_bearing` blijft expliciet; er is geen willekeurige extra halve draai toegevoegd.

## Oude borden

De oude STOP-/voorrangsborden komen uit Earths aparte, automatisch uit het wegennet gegenereerde Forest-objecten (`server/furniture.py`, groepen `stop` en `giveway` in `trees.lua`). De algemene snelheidsborden zijn eveneens een andere laag. George voegt momenteel echte bronobjecten toe; het vervangt die oude lagen nog niet.

Deze v7-correctie verwijdert die andere lagen niet. Een algemene uitschakeling zou ook borden verwijderen waarvoor onze bibliotheek nog geen vervanging heeft, en zou buiten het proefgebied ingrijpen. Een juiste vervanging per gebied/bronobject blijft integratiewerk; dubbele lagen zijn dus nog een bekende beperking. De verschillende maten en verschoven locaties op de screenshots zijn geen bewijs van twee George-objecten met hetzelfde bron-ID.

## Installeren

Vervang de vorige proefmod door v7; laat geen dubbele versies actief. De verbeterde Lua-module wordt in een draaiende sessie niet vanzelf opnieuw geladen. Herstart daarom de kaart of het spel om oude instanties en de geladen module te verwijderen. Start daarna opnieuw:

```lua
extensions.load('beamngEarth_nltestset')
extensions.load('beamngEarth_nlndw')
beamngEarth_nlndw.startStore('/levels/beamng_earth/data/george_venlo/manifest.json','travel_bearing')
```

Er is geen pull request geopend. De volledige vervanging van de oude bordenlaag is nog niet afgerond.
