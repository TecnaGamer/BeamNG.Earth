// Offline visual fixture: renders the actual search template + CSS with recorded-shaped data.
// This small template adapter is for screenshots without the game's Vue toolchain; it is not a Vue runtime test.
// node mod/tests/preview_mapsearch.mjs /tmp/beamng-search-preview
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import * as sm from '../beamng_earth/ui/ui-vue/mods/beamngEarth/mapsearch.js'
const out = resolve(process.argv[2] || '/tmp/beamng-search-preview')
mkdirSync(out, { recursive: true })
const vue = readFileSync(new URL('../beamng_earth/ui/ui-vue/mods/beamngEarth/EarthMap.vue', import.meta.url), 'utf8')
const template = vue.slice(vue.indexOf('      <div class="search"'), vue.indexOf('      <button class="map-btn" @click="nearestStation"'))
const marker = vue.slice(vue.indexOf('      <div v-if="searchPreview"'), vue.indexOf('      <!-- the pending target -->'))
const css = vue.split('<style scoped>')[1].split('</style>')[0]
const fixture = [
  { name: 'Local Motor Circuit', kind: 'race track', result_type: 'category', distance_mi: 8.6, country: 'United States', lat: 39.3, lon: -76.6 },
  { name: 'Hillside Karting', kind: 'karting', result_type: 'category', distance_mi: 21.3, country: 'United States', lat: 39.5, lon: -76.4 },
  { name: 'Circuit de Spa-Francorchamps', kind: 'race track', result_type: 'category', distance_mi: 3824.9, country: 'Belgium', lat: 50.4, lon: 5.9 },
  { name: 'Race Track Park', kind: 'park', result_type: 'name', detail: 'Park and recreation area', lat: 39.4, lon: -76.5, country: 'United States' },
  { name: 'Race Track Road', kind: 'street', result_type: 'address', detail: 'Maryland, United States', lat: 39.6, lon: -76.2, country: 'United States' },
]
// Interpret just the template directives used by the search panel (trusted repository template, fixture data only).
const adapter = `
function evaluate(expr, scope) { return Function('scope', 'with(scope) { return (' + expr + ') }')(scope) }
function render(node, scope) {
 if (node.nodeType === 3) return document.createTextNode(node.textContent.replace(/{{(.*?)}}/gs, (_, expr) => evaluate(expr, scope) ?? ''));
 if (node.nodeType !== 1) return document.createDocumentFragment();
 if (node.hasAttribute('v-for')) {
  const [, names, expression] = node.getAttribute('v-for').match(/^(.*?) in (.*)$/s);
  const keys = names.replace(/[()]/g, '').split(',').map(k => k.trim());
  const fragment = document.createDocumentFragment();
  evaluate(expression, scope).forEach((value, i) => { const child = Object.assign(Object.create(scope), { [keys[0]]: value, [keys[1] || '_index']: i }); const copy = node.cloneNode(true); copy.removeAttribute('v-for'); fragment.append(render(copy, child)); });
  return fragment;
 }
 if (node.hasAttribute('v-if') && !evaluate(node.getAttribute('v-if'), scope)) return document.createDocumentFragment();
 const out = node.cloneNode(false);
 for (const attr of [...node.attributes]) {
  if (attr.name.startsWith(':')) {
   out.removeAttribute(attr.name); const name = attr.name.slice(1), value = evaluate(attr.value, scope);
   if (name === 'key') continue;
   if (name === 'class') { for (const [key, on] of Object.entries(value)) if (on) out.classList.add(key) }
   else if (name === 'style') Object.assign(out.style, value);
   else if (value != null) out.setAttribute(name, String(value));
  } else if (attr.name === 'v-model') { out.value = evaluate(attr.value, scope); out.removeAttribute(attr.name) }
  else if (attr.name.startsWith('@') || attr.name.startsWith('v-') || attr.name === 'ref') out.removeAttribute(attr.name);
 }
 for (const child of [...node.childNodes]) out.append(render(child, scope));
 return out;
}
const fixtureTemplate = document.querySelector('#fixture');
const mount = document.querySelector('#mount');
for (const child of [...fixtureTemplate.content.childNodes]) mount.append(render(child, scope));
const pin = document.querySelector('#pin');
for (const child of [...document.querySelector('#marker').content.childNodes]) pin.append(render(child, scope));
document.body.dataset.ready = 'true';
`
for (const mode of ['results', 'wider', 'empty', 'idle', 'focused']) {
  const scope = {
    query: ['idle', 'focused'].includes(mode) ? '' : 'race track',
    searchFocused: mode === 'focused', searchToolsVisible: mode !== 'idle', searchPanelVisible: mode !== 'idle',
    results: ['empty', 'idle', 'focused'].includes(mode) ? [] : fixture, searching: mode === 'wider', searchWide: true,
    searchMsg: mode === 'empty' ? 'No places found. Try another category or add “near <place>”.' : '',
    resultIndex: mode === 'results' ? 0 : -1, moreFor: '', categoriesOpen: false,
    displayResults: fixture.map(r => sm.present(r, { lat: 39.28, lon: -76.59 })),
    searchPreview: mode === 'results' ? { x: 900, y: 430, offscreen: false, result: fixture[0] } : null,
  }
  writeFileSync(`${out}/${mode}.html`, `<!doctype html><meta charset="utf-8"><title>BeamNG.Earth search — offline fixture</title>
<style>${css}\nbody{margin:0;font-family:Arial,sans-serif}.earth-map{background:radial-gradient(ellipse at 70% 40%,#465447,#263b36 55%,#263039)}.fixture-grid{position:absolute;inset:0;background-image:linear-gradient(#9bb6ad14 1px,transparent 1px),linear-gradient(90deg,#9bb6ad14 1px,transparent 1px);background-size:96px 96px}.fixture-road{position:absolute;width:960px;height:200px;left:600px;top:340px;border:9px double #9a9a8670;transform:rotate(-26deg);border-radius:50%}.fixture-footer{position:absolute;bottom:22px;left:20px;color:#b2c4c0;font-size:12px}.marker{position:absolute}</style>
<div class="earth-map"><div class="fixture-grid"></div><div class="fixture-road"></div><div id="pin"></div>
<div class="map-top"><div class="map-title">BeamNG.Earth map</div><div id="mount" style="display:contents"></div>
<button class="map-btn">+ </button><button class="map-btn">−</button><button class="map-btn">Me</button><button class="map-btn">Layers</button></div>
<div class="fixture-footer">Offline search UI fixture · fictional nearby destinations · not an in-game capture</div></div>
<template id="fixture">${template}</template><template id="marker">${marker}</template>
<script type="module">const scope=${JSON.stringify(scope)};scope.sm=${JSON.stringify({ QUICK: sm.QUICK, CATEGORIES: sm.CATEGORIES })};scope.sm.icon=kind=>(${JSON.stringify(Object.fromEntries(sm.CATEGORIES.map(c=>[c.kind,c.path])) )})[kind];${adapter}</script>`)
}
console.log(`Offline UI fixtures: ${out}/{results,wider,empty,idle,focused}.html`)
