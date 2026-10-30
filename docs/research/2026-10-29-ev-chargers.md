# EV chargers: the stock models and charging (probe, 2026-10-29)

`probes/ev_charger.lua`, BeamNG.drive 0.40.3, the Earth level, an electric car (`sbr`, `electric_500`: the stock
configs whose "Fuel Type" is "Battery" are `sbr_electric_300/500/800`).

## Charging works through the stock gas station POI

A `BeamNGGameplayArea` + `BeamNGPointOfInterest` pair per charger, handed to `freeroam_gasStations.formatGasStationPoi`
through `onGetRawPoiListForLevel` with `energyTypes = { 'electricEnergy' }`, the same as our pumps with
`{ 'gasoline', 'diesel' }`:
- the POI's map icon is `poi_charge_round` and its marker is `electric` (`freeroam/gasStations.lua` lines 55-61);
- parked in an area, the stock panel shows "Electric Energy" and "Recharge Vehicle";
- `freeroam_gasStations.refuelCar(facility, { electricEnergy = true }, car)` took the battery from 10 % to 100 % at
  once (freeroam: `setEnergyStorageEnergy` to `maxEnergy`; career goes through `career_modules_fuel`).

An area of 5.0 x 2.4 x 2.75 m (`setScale`; a stall in front of the charger) worked.

## The models

All load with their level's `art/shapes/buildings/main.materials.json`. Object boxes (m, local axes):

| Model | x | y | z |
|---|---|---|---|
| East Coast `eca_charging_station.DAE` | -0.32 … 0.33 | -0.25 … 0.25 | 0 … 1.61 |
| West Coast `s_fuel_charger_car_2022.dae` | -0.49 … 0.49 | -0.44 … 0.25 | 0 … 2.20 |
| West Coast `s_fuel_charger_car_2020.dae` ("FAST CHARGING") | -0.46 … 0.46 | -0.67 … 0.25 | 0 … 2.30 |
| West Coast `s_fuel_charger_concrete_base.dae` | -0.53 … 0.53 | -0.38 … 0.38 | -0.50 … 0.05 |
| Italy `charging_station_a.DAE` | -0.25 … 0.30 | -0.25 … 0.25 | 0 … 1.68 |
| Italy `charging_station_b.DAE` (round post) | -0.25 … 0.25 | -0.25 … 0.25 | 0 … 1.71 |
| Hirochi `hr_charging_station.DAE` (East Coast's shape) | -0.32 … 0.33 | -0.25 … 0.25 | 0 … 1.61 |

- **Facing:** every model's front (screen, cables) is on the same side: placed with `quatFromEuler(0, 0, a)`, where
  a = the car's heading angle, they all faced the car. One yaw offset serves all of them.
- **The concrete base** is a flat slab, hardly visible on asphalt: not used.
- **Picks:** West Coast's 2020 "FAST CHARGING" unit for big sites outside Europe and the US East, its 2022 unit for
  small ones; East Coast's unit in the US East; Italy's `a` for big sites in Europe, its round post `b` for small
  ones.

## Probe pitfall

A loaded extension is a global of its name. The first probe named its extension as its own global
(`bneEvProbe`): loading it replaced the global's table, so the hook handed `formatGasStationPoi` nil and the stock
`gasStationCenterRadius` failed every frame (plus `kdtreebox2d.lua:202`), which also starved the MCP server. The
probe's extension is `bneEvProbePois` now.
