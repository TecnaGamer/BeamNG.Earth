<template>
  <div>
    <div class="controls">
      <input v-model="entry" type="password" aria-label="Mapillary client access token" maxlength="4096"
        autocomplete="off" spellcheck="false" placeholder="Paste a client access token"
        @focus="typing(true)" @blur="typing(false)" @keydown.enter.stop.prevent="save(entry)" />
      <BngButton :accent="ACCENTS.main" :disabled="busy || !entry.trim()" @click="save(entry)">Save token</BngButton>
      <BngButton :accent="ACCENTS.outlined" :disabled="busy || mode === 'rules'" @click="save('')">Use road rules only</BngButton>
    </div>
    <div role="status">{{ message || hints[mode] }}</div>
    <BngButton v-if="changed && status.restart?.restartable && !status.restart.busy" :accent="ACCENTS.outlined"
      @click="restartServer">Restart world server</BngButton>
  </div>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref } from "vue"
import { BngButton, ACCENTS } from "@/common/components/base"
import { engineLua, restartServer, status, typing } from "./earthSettingsStore.js"

const entry = ref("")
const mode = ref("default")
const busy = ref(false)
const changed = ref(false)
const message = ref("")
const hints = {
  default: "Optional. A token set in the launcher or environment also works.",
  saved: "A token is saved for this game profile.",
  rules: "This profile uses road rules only.",
}
onMounted(() => engineLua("beamngEarth_settings and beamngEarth_settings.uiMapillaryStatus()", value => {
  if (!changed.value && Object.hasOwn(hints, value)) mode.value = value
}))
onBeforeUnmount(() => { entry.value = ""; typing(false) })
function save(raw) {
  const token = raw.trim()
  if (busy.value || (raw && !token)) return
  if (token && !/^[!-~]{1,4096}$/.test(token)) {
    message.value = "Enter a client access token without spaces."
    return
  }
  busy.value = true
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiMapillarySet(${JSON.stringify(token)})`, result => {
    busy.value = false
    if (!result?.ok) {
      message.value = result?.error || "Could not save the Mapillary setting."
      return
    }
    entry.value = ""
    mode.value = token ? "saved" : "rules"
    changed.value = true
    message.value = hints[mode.value] + " Restart the world server to apply."
  })
}
</script>

<style scoped>
.controls { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5em; }
input { flex: 1 1 14em; min-width: 8em; padding: 0.45em; font: inherit; }
</style>
