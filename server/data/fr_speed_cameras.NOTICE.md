# French fixed speed cameras

Ministère de l’Intérieur, [Liste des radars fixes en France](https://www.data.gouv.fr/datasets/liste-des-radars-fixes-en-france), December 2025 snapshot, published 30 December 2025. The dataset is distributed under the [Etalab Open Licence 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/).

The compressed catalogue retains 2,565 official locations with a published speed limit, of types ETF, ETD, ETT and ETU. Each record contains the ID, type, passenger-car speed limit and coordinates. The input CSV SHA-256 is 7ba0b36d002d6910fd6f9ba8e83379d5ea57b046366658be703bd1f074ee4803. The mod matches each site to a compatible ground carriageway. It estimates the monitored direction from the mapped traffic flow or the side of the road and places the cabinet off the carriageway. Warning sign positions are generated along the matching approach.

The two sign faces reproduce SR3a and SR3b from annex 27 of the consolidated [Instruction interministérielle sur la signalisation routière, fifth part](https://equipementsdelaroute.cerema.fr/versions-consolidees-de-2025-des-9-parties-de-l-a528.html). The warning positions follow the deployment distances in the [Ministry’s response to written question 10861](https://questions.assemblee-nationale.fr/dyn/16/questions/QANR5L16QE10861.pdf).

The simulation applies the fixed-camera margins in [article 6 of the order of 4 June 2009](https://www.legifrance.gouv.fr/loda/article_lc/LEGIARTI000020779832) and uses the thresholds published by [Sécurité routière](https://www.securite-routiere.gouv.fr/radars/differents-types-de-radars/radars-fixes/radars-de-controle-de-la-vitesse-fixes). The HUD reports the corrected speed to hundredths where applicable. The simulated fine follows the [Service Public schedule](https://www.service-public.gouv.fr/particuliers/vosdroits/F19460). For a retained excess of 50 km/h or more, it shows the fixed-rate amount and the court maximum under [article L413-1](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000051877176/).

Rebuild the catalogue from the [December 2025 CSV](https://www.data.gouv.fr/api/1/datasets/r/17f7cfd9-a5fe-4b6a-9f5d-3625feaa396e).

~~~sh
python tools/build_fr_speed_cameras.py jeu-de-donnees-liste-des-radars-fixes-en-france-12-2025.csv
python tools/build_fr_radar_props.py
~~~

For new sign textures, pass the extracted artwork using the generator’s SR3a and SR3b arguments.
