# BeamNG.Earth localization

Choose **Interface → Interface language** from the BeamNG.Earth settings page. **Automatic (game language)** follows BeamNG.drive. The list is built from the installed catalogs. English and Polish are included.

The in-game preference lives in `settings/beamngEarth-language.json` in the current BeamNG user folder. The mod saves it separately from graphics, world, and server settings. Changing it updates the mod's open Vue views, the pause menu buttons, and Lua messages as they are redrawn. An already displayed game confirmation dialog retains the text with which it opened. The host game's HUD app library and key bindings follow the game's language. Route metadata is presented to BeamNG when the routes register.

The launcher selects a language at process startup. Use `--language pl-PL` or `BNE_LANGUAGE=pl-PL` to override the operating system locale. `--language` takes priority over the environment variable. Server logs, command-line flags, protocol values and third-party content use their original conventions.

## Update a translation

The mod catalogs live in [the locales folder](../mod/beamng_earth/ui/ui-vue/mods/beamngEarth/locales). They are UTF-8 JSON objects. `en.json` is the reference, and `pl-PL.json` contains Polish messages. Dot-separated keys group texts by feature.

1. Find the message in `en.json` by its English wording or key.
2. Edit the same key in your language. Copy placeholder names exactly and use plain text.
3. Run the validator and check the affected view in BeamNG.drive.
4. Open an issue or pull request in this repository. Include the locale, key, the suggested wording and, where helpful, a screenshot showing the context.

Labels in the host game's HUD app library and input binding screen have their own namespaced `beamngEarth.*` files in [the native locale folder](../mod/beamng_earth/locales/translations). The game discovers these files using its own locale IDs such as `en-US` and `pl_PL`.

## Add a language

1. Add a file named for the locale, such as `pt-BR.json`.
2. Register it in `catalogs.json` with its name in that language and its direction (`ltr` or `rtl`). Declare an optional alias for an engine locale that should use that translation. For example, the supported alias `pl` resolves to `pl-PL`. A separate general-purpose `pl` catalog can take over that alias in the future.
3. Copy only the entries you can translate. The player can already select the language with a partial file.
4. For translations of the HUD library and controls, also add a game-owned catalog using the locale ID recognized by your BeamNG.drive installation.

The sequence is regional locale, progressively less specific locales, and English. Locale matching accepts the hyphens and underscores used by the UI, with case-insensitive matching. Invalid text or incompatible placeholders fall through to the next catalog. Vue uses the selected language for display numbers and dates, with the source message's language for numbers embedded in a fallback message. Place names, coordinates, configuration keys, action IDs and values on the wire are separate from the messages. Game display settings still select the available metric or imperial units. Catalog metadata sets text direction at feature roots. A future RTL translation needs layout and input testing.

## Add a message

Add an English sentence or label with a stable key that describes its purpose. In Vue import `t` from `./i18n.js`. Bind the output as text or an ordinary text attribute. Leave input values and preference IDs as their existing values. In Lua use `require('beamngEarth/i18n').t(key, params)`. The launcher uses `localization.t(key, **params)` for its `launcher.*` messages.

```vue
<span>{{ t('settings.updateYouHave', { latest: update.latest, current: update.current }) }}</span>
```

Write named placeholders as `{latest}` and `{current}`. Every translation of that key must use the same names. Pass strings and finite numbers. Templates are interpolated once and placed as text. An absent or invalid parameter renders as an em dash. A missing English key also renders an em dash and appears in diagnostics. For a notice kept in Vue state, `notice(key, params)` defers translation until the view renders it.

Plural messages are JSON objects keyed by cardinal category. Include `other` and the same placeholders in every form, including `{count}`. The Polish download indicator uses the four forms below.

```json
{
  "hud.loading.files": {
    "one": "pobieranie: {count} plik",
    "few": "pobieranie: {count} pliki",
    "many": "pobieranie: {count} plików",
    "other": "pobieranie: {count} pliku"
  }
}
```

Pass `count` as a number. Vue uses `Intl.PluralRules` with the locale that supplied the message. Lua implements the shipped English and Polish cardinal rules. If a future language needs other categories for Lua-owned messages, extend that function and the tests. The launcher uses scalar messages.

## Validate and inspect coverage

Run these commands from the repository root.

```shell
python tools/check_locales.py
python tools/check_locales.py --format json --require pl-PL
node --test mod/tests/test_i18n.mjs
python tools/run_lua_tests.py test_i18n
python server/tests/test_localization.py
```

The report compares every registered catalog with English and flags malformed JSON, duplicate keys, invalid value types, placeholder and plural mismatches, unknown keys and missing or unused references. Keep translation references as literal keys or in explicit lookup tables so the validator can find them. Coverage is by catalog entry. It measures the keys in the catalog and depends on the inventory of strings brought into the system. Partial locales pass unless marked required with `--require`. The quality gate requires Polish coverage.

For live diagnostics, run `window.beamngEarthLocalization()` in BeamNG.drive's UI JavaScript console. It reports the detected game language, preference, active catalog, coverage, missing keys, fallback lookups observed after diagnostics were enabled, and validation messages. For the Lua side, run `dump(require('beamngEarth/i18n').report())` in the game engine Lua console. Both diagnostics deduplicate warnings. The console tools avoid adding a persistent developer panel to the HUD.

## Check a change in the game

1. Use a disposable BeamNG user folder and install the modified mod. Set the game language to Polish, leave the mod set to Automatic and open its pause-menu settings and full settings page.
2. Switch to English and back to Polish with settings open. Check that the selected page, visible rows, tooltips, side card and pause menu labels update. Reopen the game and confirm that the choice and existing graphics and world settings were retained.
3. Set the game to a language absent from the manifest and select Automatic in the mod. Check English fallback. In a test copy, remove one Polish key and check that its English text renders alongside Polish. Restore the original catalog after testing.
4. Open the welcome screen, 2D and 3D maps, search for a location, create and rename a bookmark, set a GPS route and teleport. Repeat with both game measurement units. Check that text wraps at normal UI scale and at higher DPI.
5. Start the companion server. Check the offline banner, loading HUD (1, 2, 5 and 21 downloads), auto-drive, map cache and add-on messages. Join a BeamMP Earth server to check its recommendation dialog, the player list, and the HUD during mod reloads.
6. Check the source launcher with `--language pl-PL`, including a failed connection and the update prompt when available. Test each supported packaged OS before a release.
