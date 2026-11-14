# Your own 3D objects (local models)

Besides the models of the open library [3dmr.eu](https://3dmr.eu), the server places your own models wherever
you want them: a mast, a monument, a building of your town nobody has uploaded yet. This folder is an example
(`fire_tower_20m.stl`, a 20 m fire lookout tower, and its entry in `local_models.json`).

1. Find the game's user folder (Windows: `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current`, Linux / Proton:
   `~/.local/share/BeamNG/BeamNG.drive/current`) and in it `temp\beamngEarth\threedmr_cache\` (the server
   makes it the first time it looks for models; make it yourself if it is not there yet).
2. Put your model file there: a binary `.stl` (one colour) or a `.glb` (glTF 2.0 binary, with its textures).
   Units are metres; up is +Z for an STL, +Y for a glTF (as exported). The middle of its outline (its
   bounding box seen from above) stands at the given position, its floor on the ground. It is not
   turned: face it the way you want in your modelling tool (+X east, +Y north for an STL).
3. Add an entry for it to `local_models.json` in the same folder (a JSON list, one entry per model):

   ```json
   [
     {
       "file": "fire_tower_20m.stl",
       "lat": 50.24378,
       "lon": 137.93470,
       "factor": [0.784, 0.063, 0.18],
       "title": "Fire lookout tower, 20 m"
     }
   ]
   ```

   - `file`: the model's file name in that folder;
   - `lat`, `lon`: where it stands (degrees, WGS84: right-click on openstreetmap.org, "Show address");
   - `factor` (optional): its colour as sRGB `[r, g, b]` from 0 to 1 (for an STL, which has none of its own);
   - `title` (optional): a name, for you and the log.
4. Drive there (the tiles already loaded keep what they were built with: drive away and back). Places
   visited before are built again: the server's caches know the catalog and its files.

Procedural buildings whose footprint lies under the model are left out, so they do not poke through it.

## Sharing a model with everyone

Upload it to [3dmr.eu](https://3dmr.eu) (an OpenStreetMap account is the login; choose the licence, CC0 or
CC BY 4.0) and tag the OpenStreetMap object it stands for with `3dmr=<the model's id>`. Every BeamNG.Earth
player then gets it where it stands, downloaded when they first drive near it. 3dmr.eu's guide asks for
metres and the model's front towards the south; the server also turns it to match the OpenStreetMap
building's outline.
