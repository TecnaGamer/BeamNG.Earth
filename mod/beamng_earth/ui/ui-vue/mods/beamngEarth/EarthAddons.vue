<template>
  <!-- The Add-ons page (earthAddonsStore.js; Lua: settings.lua uiAddon* -> addons.lua; server: addons.py): the curated
       third-party add-ons as cards, one add-on's README and changelog (safe HTML from the server: our own tags only,
       links open in the browser through the server), Install / Update / Turn off / Remove for game mods, and the notice
       naming the author before the first install. -->
  <div class="addons">
    <!-- the list -->
    <template v-if="!openCard">
      <div class="addons-intro">
        Add-ons are made by other people, not by BeamNG.Earth. Each one is downloaded from its author's own GitHub
        releases.
      </div>
      <div v-if="inMp" class="addons-note is-warn">A BeamMP server is joined: add-ons can be installed or changed after you leave it.</div>
      <div v-if="!connected" class="addons-note is-warn">The BeamNG.Earth app is not connected: the list needs it.</div>
      <div v-else-if="!loaded" class="addons-note">Loading the list...</div>
      <div v-if="listError" class="addons-note is-error">{{ listError }}</div>

      <div
        v-for="c in cards"
        :key="c.id"
        class="addon-card"
        tabindex="0"
        @click="open(c.id)"
        @keydown.enter="open(c.id)"
      >
        <div class="addon-head">
          <span class="addon-name">{{ c.name }}</span>
          <span class="addon-author">by {{ c.author }}</span>
        </div>
        <div class="addon-summary">{{ c.summary }}</div>
        <div class="addon-meta">
          <span class="addon-badge" :class="{ 'is-on': c.installed && c.active !== false, 'is-update': c.update }">{{ stateLine(c) }}</span>
          <span v-if="c.reviewed" class="addon-badge is-reviewed">Reviewed by BeamNG.Earth</span>
          <span v-if="c.local" class="addon-badge is-local">Local</span>
          <span v-if="c.added" class="addon-badge is-added">Added by you</span>
          <span v-if="c.latestDate" class="addon-date">{{ day(c.latestDate) }}</span>
        </div>
        <div v-if="c.status && c.status.text" class="addons-note" :class="{ 'is-error': c.status.state === 'error' }">{{ progressText(c.status) }}</div>
        <div v-if="c.error" class="addons-note is-error">{{ c.error }}</div>
      </div>

      <div class="addons-actions">
        <BngButton :accent="ACCENTS.outlined" :disabled="!connected" @click="refresh">Check for updates</BngButton>
      </div>

      <!-- Add from GitHub: any repository with releases, put on the player's own list -->
      <div class="addon-add">
        <div class="addon-add-title">Add from GitHub</div>
        <div class="addons-note">Paste a GitHub repository (owner/name or its link). Its latest release is used: a .zip installs as a game mod, a .py runs beside the game. Only add things from people you trust: nobody has checked them.</div>
        <div class="addons-actions">
          <input @focus="typing(true)" @blur="typing(false)" v-model="repoText" class="addon-add-input" type="text" placeholder="github.com/owner/name" @keydown.enter="addRepo(repoText)" @keydown.stop @keyup.stop />
          <BngButton :accent="ACCENTS.main" :disabled="!connected || inMp || !repoText.trim() || (adding && adding.state === 'progress')" @click="addRepo(repoText)">Add</BngButton>
        </div>
        <div v-if="adding && adding.text" class="addons-note" :class="{ 'is-error': adding.state === 'error' }">{{ adding.text }}</div>
      </div>
    </template>

    <!-- one add-on -->
    <template v-else>
      <div class="addons-actions is-top">
        <BngButton :accent="ACCENTS.outlined" @click="back">Back</BngButton>
        <BngButton :accent="ACCENTS.outlined" @click="openRepo(openCard)">GitHub page</BngButton>
      </div>
      <div class="addon-head">
        <span class="addon-name">{{ openCard.name }}</span>
        <span class="addon-author">by {{ openCard.author }}</span>
      </div>
      <div class="addon-third">Third-party add-on, not made by BeamNG.Earth. {{ openCard.licence }}</div>
      <div class="addon-meta">
        <span class="addon-badge" :class="{ 'is-on': openCard.installed && openCard.active !== false, 'is-update': openCard.update }">{{ stateLine(openCard) }}</span>
        <span v-if="openCard.reviewed" class="addon-badge is-reviewed">Reviewed by BeamNG.Earth</span>
        <span v-if="openCard.local" class="addon-badge is-local">Local</span>
        <span v-if="openCard.added" class="addon-badge is-added">Added by you</span>
        <span v-if="openCard.size" class="addon-date">{{ size(openCard.size) }}</span>
      </div>
      <div v-if="openCard.note && !(openCard.kind === 'companion' && openCard.canInstall)" class="addons-note">{{ openCard.note }}</div>
      <div v-if="openCard.tooNew" class="addons-note is-warn">This add-on needs a newer BeamNG.Earth: update BeamNG.Earth first.</div>

      <!-- the notice before the first install -->
      <div v-if="consentFor === openCard.id" class="addon-consent">
        <div class="addon-consent-title">Install {{ openCard.name }}?</div>
        <div>
          This add-on is made by <b>{{ openCard.author }}</b>, not by BeamNG.Earth.
          <template v-if="openCard.added"><b>You added it yourself: it is not on BeamNG.Earth's list and nobody has checked it.</b> Install it only if you trust its author.</template>
          <template v-if="openCard.reviewed">BeamNG.Earth has looked through this version's files.</template>
          <template v-else>BeamNG.Earth has not reviewed this version.</template>
          <template v-if="openCard.kind === 'companion'">It is a program that runs on this computer beside the game (with BeamNG.Earth's own Python), with the same access as any program you run.</template>
          <template v-else>It runs inside your game like any other mod you install, with the same access.</template>
          <template v-if="openCard.local">It is a file on this computer (the local list).</template>
          <template v-else>It is downloaded from github.com/{{ openCard.repo }}</template><template v-if="lines(openCard.oldFiles).length">; the older copies {{ lines(openCard.oldFiles).join(", ") }} in your mods folder are removed</template>.
        </div>
        <div class="addons-actions">
          <BngButton :accent="ACCENTS.main" @click="install(openCard)">I agree, install</BngButton>
          <BngButton :accent="ACCENTS.outlined" @click="cancelConsent">Cancel</BngButton>
        </div>
      </div>

      <div v-else-if="openCard.kind === 'beamng-mod'" class="addons-actions">
        <BngButton
          v-if="!openCard.installed || openCard.update"
          :accent="ACCENTS.main"
          :disabled="!openCard.canInstall || inMp || busy(openCard)"
          @click="install(openCard)"
        >{{ openCard.installed ? "Update to " + openCard.latest : "Install" }}</BngButton>
        <BngButton
          v-if="openCard.installed && openCard.active !== false"
          :accent="ACCENTS.outlined"
          :disabled="inMp || busy(openCard)"
          @click="manage(openCard, 'disable')"
        >Turn off</BngButton>
        <BngButton
          v-if="openCard.installed && openCard.active === false"
          :accent="ACCENTS.outlined"
          :disabled="inMp || busy(openCard)"
          @click="manage(openCard, 'enable')"
        >Turn on</BngButton>
        <BngButton
          v-if="openCard.installed"
          :accent="ACCENTS.outlined"
          :disabled="inMp || busy(openCard)"
          @click="removeAsk = openCard.id"
        >Remove</BngButton>
      </div>
      <!-- a program beside the game: installed and run by BeamNG.Earth (addonrun.py) -->
      <div v-else-if="openCard.kind === 'companion' && openCard.canInstall" class="addons-actions">
        <BngButton
          v-if="!openCard.installed || openCard.update"
          :accent="ACCENTS.main"
          :disabled="busy(openCard)"
          @click="install(openCard)"
        >{{ openCard.installed ? "Update to " + openCard.latest : "Install" }}</BngButton>
        <BngButton v-if="openCard.installed && !openCard.running" :accent="ACCENTS.main" :disabled="busy(openCard)" @click="manage(openCard, 'start')">Start</BngButton>
        <BngButton v-if="openCard.running" :accent="ACCENTS.outlined" :disabled="busy(openCard)" @click="manage(openCard, 'stop')">Stop</BngButton>
        <BngButton v-if="openCard.running && openCard.page" :accent="ACCENTS.outlined" @click="openDownload(openCard.page)">Open its page</BngButton>
        <BngButton
          v-if="openCard.installed"
          :accent="ACCENTS.outlined"
          :disabled="busy(openCard)"
          @click="manage(openCard, openCard.autostart ? 'autostart_off' : 'autostart_on')"
        >{{ openCard.autostart ? "Starts with BeamNG.Earth: on" : "Starts with BeamNG.Earth: off" }}</BngButton>
        <BngButton v-if="openCard.installed" :accent="ACCENTS.outlined" :disabled="busy(openCard)" @click="removeAsk = openCard.id">Remove</BngButton>
      </div>
      <div v-if="openCard.kind === 'companion' && openCard.running && openCard.lanPage" class="addons-note">
        On a phone or tablet on the same Wi-Fi: {{ openCard.lanPage }}
      </div>
      <div v-if="openCard.kind === 'companion' && openCard.exitCode" class="addons-note is-error">
        It stopped with an error ({{ openCard.exitCode }}). Its last lines: {{ lines(openCard.logTail).join(" · ") }}
      </div>
      <div v-if="openCard.added && !openCard.installed" class="addons-actions">
        <BngButton :accent="ACCENTS.outlined" @click="forget(openCard)">Remove from my list</BngButton>
      </div>
      <div v-if="removeAsk === openCard.id" class="addons-actions">
        <span v-if="openCard.kind === 'companion'" class="addons-note">Remove {{ openCard.name }} and its own data (its recorded drives) from this computer?</span>
        <span v-else class="addons-note">Remove {{ openCard.name }} from your mods folder? Its own saves stay.</span>
        <BngButton :accent="ACCENTS.main" @click="removeAsk = ''; manage(openCard, 'uninstall')">Remove</BngButton>
        <BngButton :accent="ACCENTS.outlined" @click="removeAsk = ''">Cancel</BngButton>
      </div>
      <div v-if="openCard.status && openCard.status.text" class="addons-note" :class="{ 'is-error': openCard.status.state === 'error' }">{{ progressText(openCard.status) }}</div>
      <div v-if="actionMsg[openCard.id]" class="addons-note is-error">{{ actionMsg[openCard.id] }}</div>
      <div v-if="openCard.error" class="addons-note is-error">{{ openCard.error }}</div>

      <!-- downloads of a companion / link add-on (the latest release's files) -->
      <div v-if="openCard.kind !== 'beamng-mod' && latestDownloads.length" class="addons-actions">
        <span v-if="openCard.kind === 'companion'" class="addons-note">Its other downloads:</span>
        <BngButton v-for="d in latestDownloads" :key="d.url" :accent="ACCENTS.outlined" @click="openDownload(d.url)">{{ d.label }}</BngButton>
      </div>

      <div class="addon-tabs">
        <BngButton :accent="tab === 'readme' ? ACCENTS.main : ACCENTS.outlined" @click="tab = 'readme'">Description</BngButton>
        <BngButton :accent="tab === 'changes' ? ACCENTS.main : ACCENTS.outlined" @click="tab = 'changes'">Changelog</BngButton>
        <BngButton v-if="lines(openCard.options).length" :accent="tab === 'options' ? ACCENTS.main : ACCENTS.outlined" @click="openOptions">Options</BngButton>
      </div>
      <!-- the add-on's own options (its declaration: docs/ADDONS.md "Options"); saved by the server, a running program restarted -->
      <div v-if="tab === 'options' && lines(openCard.options).length" class="addon-options">
        <div v-for="o in lines(openCard.options)" :key="o.key" class="addon-option">
          <label class="addon-option-label">{{ o.label }}</label>
          <input @focus="typing(true)" @blur="typing(false)" v-if="o.type === 'int'" v-model.number="edits[o.key]" class="addon-add-input addon-option-num" type="number" :min="o.min" :max="o.max" step="1" @keydown.stop @keyup.stop />
          <input @focus="typing(true)" @blur="typing(false)" v-else-if="o.type === 'text'" v-model="edits[o.key]" class="addon-add-input" type="text" maxlength="200" @keydown.stop @keyup.stop />
          <select v-else-if="o.type === 'choice'" v-model="edits[o.key]" class="addon-add-input">
            <option v-for="ch in lines(o.choices)" :key="ch" :value="ch">{{ ch }}</option>
          </select>
          <BngButton v-else-if="o.type === 'bool'" :accent="edits[o.key] ? ACCENTS.main : ACCENTS.outlined" @click="edits[o.key] = !edits[o.key]">{{ edits[o.key] ? "On" : "Off" }}</BngButton>
          <span v-if="o.type === 'int'" class="addon-date">{{ o.min }} to {{ o.max }} (default {{ o.default }})</span>
          <div v-if="o.help" class="addons-note">{{ o.help }}</div>
        </div>
        <div class="addons-actions">
          <BngButton :accent="ACCENTS.main" :disabled="!connected || busy(openCard)" @click="saveOptions(openCard, edits)">Save</BngButton>
          <BngButton :accent="ACCENTS.outlined" @click="resetEdits(true)">Defaults</BngButton>
          <span v-if="openCard.running" class="addons-note">Saving restarts it with the new options.</span>
        </div>
      </div>
      <div v-if="!detail" class="addons-note">Loading...</div>
      <template v-else>
        <div v-if="detail.error" class="addons-note is-error">{{ detail.error }}</div>
        <!-- (the server's HTML: only its own tags, text escaped, no scripts, styles or addresses: mdhtml.rs) -->
        <div v-if="tab === 'options'"></div>
        <div v-else-if="tab === 'readme'" class="addon-md" @click="onTextClick($event, detail.readmeLinks)" v-html="detail.readme"></div>
        <template v-else>
          <div v-for="r in detail.releases || []" :key="r.tag" class="addon-release">
            <div class="addon-release-head">
              <span class="addon-name">{{ r.name || r.tag }}</span>
              <span class="addon-date">{{ r.tag }} · {{ day(r.date) }}<template v-if="r.prerelease"> · pre-release</template><template v-if="r.reviewed"> · reviewed</template></span>
            </div>
            <div class="addon-md" @click="onTextClick($event, r.links)" v-html="r.html"></div>
          </div>
          <div v-if="!(detail.releases || []).length" class="addons-note">No releases yet.</div>
        </template>
      </template>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue"
import { BngButton, ACCENTS } from "@/common/components/base"
import {
  cards, loaded, listError, connected, inMp, openCard, detail, consentFor, actionMsg,
  open, back, refresh, install, cancelConsent, manage, onTextClick, openRepo, openDownload,
  stateLine, size, day, progressText, start, stop, lines, adding, addRepo, forget, saveOptions,
} from "./earthAddonsStore.js"
import { typing } from "./earthSettingsStore.js"

const tab = ref("readme")
const removeAsk = ref("")
const repoText = ref("")
const edits = ref({})       // the Options tab's values being edited

function resetEdits(defaults) {
  const out = {}
  for (const o of lines(openCard.value && openCard.value.options)) out[o.key] = defaults ? o.default : o.value
  edits.value = out
}

function openOptions() {
  tab.value = "options"
  resetEdits(false)
}

function busy(c) { return !!(c.busy || (c.status && c.status.state === "progress")) }

// the newest release's extra downloads (LiveGPS's script and APK)
const latestDownloads = computed(() => {
  const rels = (detail.value && detail.value.releases) || []
  const r = rels.find(x => !x.prerelease) || rels[0]
  return (r && r.downloads) || []
})

onMounted(start)
onUnmounted(stop)
// (the game keeps the keyboard unless an input says it is being typed in: as EarthMap's and EarthMap3d's inputs)
onUnmounted(() => typing(false))
</script>

<style scoped lang="scss">
.addons {
  display: flex;
  flex-direction: column;
  gap: 0.5em;
  --bng-button-min-width: 4.5em;
}
.addons-intro, .addons-note, .addon-third {
  font-size: 0.9em;
  color: var(--bng-cool-gray-300, #bbb);
  padding: 0.1em 0.25em;
}
.addons-note.is-warn { color: var(--bng-add-orange-300, #fc6); }
.addons-note.is-error { color: var(--bng-add-red-400, #f88); }
.addon-card {
  padding: 0.6em 0.75em;
  border-radius: 0.4em;
  background: rgba(255, 255, 255, 0.06);
  cursor: pointer;
}
.addon-card:hover, .addon-card:focus { background: rgba(255, 102, 0, 0.18); outline: none; }
.addon-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.5em; }
.addon-name { font-weight: 600; color: var(--bng-cool-gray-100, #eee); }
.addon-author, .addon-date { font-size: 0.85em; color: var(--bng-cool-gray-400, #999); }
.addon-summary { font-size: 0.9em; color: var(--bng-cool-gray-200, #ccc); padding: 0.2em 0; }
.addon-meta { display: flex; flex-wrap: wrap; gap: 0.4em; align-items: center; }
.addon-badge {
  font-size: 0.8em;
  padding: 0.1em 0.5em;
  border-radius: 0.3em;
  background: rgba(255, 255, 255, 0.1);
  color: var(--bng-cool-gray-200, #ccc);
}
.addon-badge.is-on { background: rgba(80, 200, 120, 0.25); }
.addon-badge.is-update { background: rgba(255, 102, 0, 0.35); }
.addon-badge.is-reviewed { background: rgba(80, 150, 255, 0.3); }
.addon-badge.is-local { background: rgba(255, 180, 60, 0.3); }
.addon-badge.is-added { background: rgba(255, 90, 90, 0.3); }
.addon-options { margin: 0.6em 0; }
.addon-option { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5em; margin: 0.4em 0; }
.addon-option-label { min-width: 10em; font-weight: 600; }
.addon-option-num { flex: 0 0 8em; min-width: 6em; }
.addon-add { margin-top: 1.2em; padding-top: 0.8em; border-top: 1px solid rgba(255, 255, 255, 0.15); }
.addon-add-title { font-weight: 600; margin-bottom: 0.3em; }
.addon-add-input { flex: 1 1 18em; min-width: 12em; padding: 0.4em 0.6em; color: inherit; background: rgba(0, 0, 0, 0.35);
  border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 4px; font: inherit; }
.addons-actions { display: flex; flex-wrap: wrap; gap: 0.5em; align-items: center; }
.addons-actions.is-top { justify-content: space-between; }
.addon-consent {
  padding: 0.6em 0.75em;
  border-radius: 0.4em;
  border: 1px solid rgba(255, 102, 0, 0.6);
  background: rgba(255, 102, 0, 0.08);
  font-size: 0.9em;
  display: flex;
  flex-direction: column;
  gap: 0.5em;
}
.addon-consent-title { font-weight: 600; }
.addon-tabs { display: flex; gap: 0.5em; padding-top: 0.25em; }
.addon-release { padding: 0.4em 0; border-top: 1px solid rgba(255, 255, 255, 0.1); }
.addon-release-head { display: flex; flex-wrap: wrap; gap: 0.5em; align-items: baseline; }

/* the README's HTML (v-html: scoped styles reach it through :deep) */
.addon-md {
  font-size: 0.9em;
  line-height: 1.4;
  color: var(--bng-cool-gray-200, #ccc);
  overflow-wrap: anywhere;
  :deep(h1), :deep(h2), :deep(h3), :deep(h4), :deep(h5), :deep(h6) {
    color: var(--bng-cool-gray-100, #eee);
    margin: 0.7em 0 0.3em;
    line-height: 1.2;
  }
  :deep(h1) { font-size: 1.35em; }
  :deep(h2) { font-size: 1.2em; }
  :deep(h3) { font-size: 1.08em; }
  :deep(h4), :deep(h5), :deep(h6) { font-size: 1em; }
  :deep(p) { margin: 0.35em 0; }
  :deep(ul), :deep(ol) { margin: 0.3em 0; padding-left: 1.4em; }
  :deep(li) { margin: 0.15em 0; }
  :deep(code) { font-family: monospace; background: rgba(255, 255, 255, 0.08); padding: 0 0.25em; border-radius: 0.2em; }
  :deep(pre) { background: rgba(0, 0, 0, 0.3); padding: 0.5em; border-radius: 0.3em; overflow-x: auto; }
  :deep(pre code) { background: none; padding: 0; }
  :deep(blockquote) { margin: 0.4em 0; padding-left: 0.75em; border-left: 3px solid rgba(255, 255, 255, 0.2); }
  :deep(table) { border-collapse: collapse; margin: 0.4em 0; display: block; overflow-x: auto; }
  :deep(th), :deep(td) { border: 1px solid rgba(255, 255, 255, 0.15); padding: 0.2em 0.5em; text-align: left; }
  :deep(hr) { border: none; border-top: 1px solid rgba(255, 255, 255, 0.15); }
  :deep(a.md-link) { color: var(--bng-orange-300, #f93); cursor: pointer; text-decoration: underline; }
}
</style>
