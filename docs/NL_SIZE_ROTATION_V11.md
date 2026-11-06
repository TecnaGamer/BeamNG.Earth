# V11 — bordmaten uit wegcontext en controle van rotatie

Deze kleine update bevat ook de serverstreaming van v10. De landelijke dataset en de modellen worden hergebruikt; geen nieuwe data-ZIP's downloaden of uitpakken. Het gaat om code en een klein bestand met vormprofielen.

De maatkeuze geldt voor de NDW/George-platen van deze renderer. Borden uit een oudere samengestelde wegmesh gebruiken een andere generator en kunnen nog afwijkend blijven; een screenshot alleen bewijst niet uit welke laag een bord komt.

## Maatkeuze

Voor herkende ronde standaardplaten wordt de nominale diameter 600 / 800 / 1000 mm voor respectievelijk type I / II / III. Voor herkende driehoekige platen wordt de nominale breedte 700 / 900 / 1100 mm. De keuze gebruikt de nabijgelegen navigatieweg: tot 50 km/h type I, boven 50 tot 80 type II, daarboven type III. Een als autosnelweg geclassificeerd wegvak behoudt type III bij een tijdelijke lagere maximumsnelheid.

Dit is een **afleiding**, geen gemeten borddiameter uit George. De grootte komt niet uit het snelheidsgetal op het bord. De referentie is [CROW, maatvoering verkeersborden](https://kennisbank.crow.nl/public/gastgebruiker/WOBI/Verkeerstekens_(toepassing,_plaatsing_en_uitvoering)/Maatvoering/29540). Bijzondere uitvoeringen, type-0-borden op bijvoorbeeld gele kokers, bijzondere formaten en lokale grotere uitvoeringen zijn niet automatisch uit de bron vast te stellen.

De server geeft nu de wegcategorie en oorspronkelijke maximumsnelheid afzonderlijk mee, naast de bestaande AI-rijsnelheid die in bochten lager kan liggen. Als de snelheid uit een klasse-aanname komt wordt dit gemarkeerd. Met een oudere server is alleen een indicatie uit de AI-snelheid beschikbaar; dat kan op een bocht te laag zijn. Voor de volledige werking moeten server en client beide worden bijgewerkt.

De wegkoppeling is ruimtelijk, niet een bewezen overeenkomst met het NWB-wegvaknummer. Bij kruisende wegen of bijna even nabije parallelle wegen met conflicterende categorie blijft de maat onzeker. Ontbrekende/ambigue context en bijzondere plaatvormen behouden de voorlopige catalogusmaat, met een vermelding in het rapport. Hoogteverschillen bij viaducten blijven een aandachtspunt: een plattegrond alleen garandeert geen juiste wegvakselectie.

Er zijn 88 ronde en 36 driehoekige vormprofielen herkend in de 669 gekoppelde assetvarianten. De overige 545 varianten zijn bijzondere of nog niet ondersteunde maatvormen. Afgeronde vierkante platen worden daarbij niet als cirkels behandeld. De vormprofielen komen uit de bestaande plaatomtrekken, niet uit nieuwe foto's.

De plaat wordt alleen in zijn eigen vlak geschaald. Paaldiameter, beugelomvang en plaatdiepte worden niet mee opgeblazen. De tussenafstand van beugels wordt uit de bestaande drie sets gekozen. Onderborden schalen voorlopig mee met hun hoofdbord; dat is een montage-aanname en geen gevalideerde volledige onderbordmaattabel. De ruimte onder de stapel blijft ten opzichte van de vorige generator behouden, de bovenkant schuift zo nodig omhoog en de paal wordt opnieuw uit de drie lengtes gekozen. De paaltop blijft 5 cm onder het bovenste bord. Een onpassende stapel wordt overgeslagen.

## Richtingscontrole

De bestaande omzetting van bronrichting naar wereldrichting en BeamNG-quaternion is opnieuw gecontroleerd voor noord, oost, zuid, west en de diagonalen, voor beide ondersteunde bronconventies. Daaruit kwam geen algemene 90- of 180-gradenfout naar voren.

Bij een eenduidige nabijgelegen wegas wordt een afwijking tot maximaal 20 graden op de as uitgelijnd. De oorspronkelijke bronrichting blijft in het rapport staan. Grotere afwijkingen blijven ongewijzigd en krijgen `large_axis_mismatch_review`; op een kruising blijft de bronrichting behouden. Dit lost niet alle verkeerde bronrichtingen automatisch op. Een wegas heeft twee mogelijke kijkrichtingen en bewijst dus niet welke bordkant voor moet staan.

Een mogelijk omgekeerde voorkant op een eenrichtingsweg wordt apart gemarkeerd (`possible_reverse_facing`). Dit is een controlepunt, geen automatische omdraaiing: bepaalde borden kunnen bewust op verkeer uit de verboden richting zijn gericht. De standaard `travel_bearing`-interpretatie blijft behouden; de algemene v5-beschrijving noemt alleen een bordoriëntatie en bewijst niet voor elk bronrecord dezelfde invoerconventie.

Bij een nieuw toegepaste navigatiekaart wordt de bestaande selectie opnieuw beoordeeld. Zonder wijziging worden sceneobjecten hergebruikt. Er komt geen continue landelijke herberekening of extra fotoverkeer bij.

## Installeren

Sluit BeamNG en de companionserver. Pak alleen de kleine v11-update uit. Voor uitgepakte mods:

```powershell
./tools/install_nl_stream.ps1 -ProjectDirectory 'PAD/NAAR/BeamNG.Earth' -GameModDirectories 'PAD/NAAR/mods/unpacked/beamng_earth','PAD/NAAR/mods/unpacked/nl_c1_proef'
```

Voor een bestaande Nederlandse model-ZIP kan in plaats daarvan `-ModelModZip 'PAD/NAAR/modelmod.zip'` worden gebruikt. De updater maakt reservekopieën en voegt de integratie samen met de ondersteunde basis-/BGT-versie. Ook de navigatiemodule wordt bijgewerkt. De BGT-variant behoudt de extra breedtes bij tunnelportalen; de nieuwe maatcontext gebruikt een apart veld.

Start de bestaande server met `--nl-signs-store 'MAP/MET/DE/VIER/DATA-ZIPS'` voor automatische serverstreaming, zoals beschreven in `NL_SERVER_STREAM_V10.md`. De lokale v9-datamodus kan eveneens de nieuwe clientrenderer gebruiken. Herstart de kaart of het spel om geladen Lua-code te vervangen.

In `beamngEarth_nlndw.report().render.outcome.locations` staat per paal een `boards`-lijst met nominale breedte/hoogte, gekozen type, herkomst van de maatkeuze, wegcontext, oorspronkelijke richting, toegepaste richting en eventuele richtingswaarschuwing. Het rapport van `beamngEarth_nltestset.saveReport()` bewaart die informatie.

De wijzigingen zijn lokaal getest op maatdrempels, autosnelwegen met lage snelheid, ambiguïteit, oude servers, plaat-/paalschaal, hoogte/vrije paaltop, bronrichting versus kleine correcties en bestaande AI-/verkeersregels. Een nieuwe visuele speltest is nog nodig. Er is geen pull request gemaakt en de actieve spel-/BGT-installatie is tijdens ontwikkeling niet aangepast.
