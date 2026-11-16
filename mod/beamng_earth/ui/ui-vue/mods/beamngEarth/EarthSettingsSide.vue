<template>
  <!-- Beside BeamNG.Earth's settings: the connection, an update, what the setting pointed at does. The pause menu's
       BeamNG.Earth tab shows it as the tab's second card; the full settings page (EarthSettings.vue) beside its rows,
       with Back (prop back). -->
  <div class="earth-side">
    <BngCardHeading>BeamNG.Earth</BngCardHeading>
    <div class="earth-status" :class="'is-' + bannerLevel">{{ bannerText || connectionText }}</div>
    <div v-if="update" class="earth-update">
      <span>Update: {{ update.latest }} (you have {{ update.current }})</span>
      <BngButton :accent="ACCENTS.outlined" @click="openUpdate">Download page</BngButton>
    </div>
    <div class="earth-loading">{{ statusText }}</div>
    <div v-if="offLevel" class="earth-offlevel">Not on the BeamNG.Earth level: graphics and world settings are saved
      and take effect when you load it.</div>

    <div class="earth-help">
      <div class="earth-help-title">{{ help ? help.name : "" }}</div>
      <div class="earth-help-text">{{ help ? help.desc : "Point at a setting to see what it does." }}</div>
      <div v-if="help && help.note" class="earth-note">{{ help.note }}</div>
    </div>

    <div class="earth-side-buttons">
      <BngButton :accent="ACCENTS.outlined" @click="showWelcome">Welcome page</BngButton>
      <BngButton v-if="back" :accent="ACCENTS.main" @click="close">Back</BngButton>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted } from "vue"
import { BngButton, BngCardHeading, ACCENTS } from "@/common/components/base"
import {
  help, update, bannerText, bannerLevel, connectionText, statusText, offLevel, openUpdate, showWelcome, close,
  startPolling, stopPolling,
} from "./earthSettingsStore.js"

defineProps({
  back: { type: Boolean, default: false },     // the full page's Back button
})

onMounted(startPolling)
onUnmounted(stopPolling)
</script>

<style scoped lang="scss">
.earth-side {
  display: flex;
  flex-direction: column;
  gap: 0.5em;
  height: 100%;
  padding: 0.5em 0.75em 1em;
}

.earth-loading,
.earth-note {
  font-size: 0.9em;
  color: var(--bng-cool-gray-300, #bbb);
}

.earth-status {
  font-weight: 600;
  white-space: pre-line;

  &.is-ok { color: var(--bng-add-green-400, #8f8); }
  &.is-warn { color: var(--bng-orange-300, #fa3); }
  &.is-error { color: var(--bng-add-red-400, #f88); }
}

.earth-offlevel {
  font-size: 0.9em;
  color: var(--bng-orange-300, #fa3);
}

.earth-update { display: flex; align-items: center; justify-content: space-between; gap: 0.5em; }

.earth-help {
  flex: 1 1 auto;
  padding-top: 0.5em;
  border-top: 1px solid rgba(255, 255, 255, 0.15);

  .earth-help-title { font-weight: 600; padding-bottom: 0.3em; }
  .earth-help-text { font-size: 0.95em; line-height: 1.4; color: var(--bng-cool-gray-200, #ccc); }
}

.earth-side-buttons {
  display: flex;
  gap: 0.5em;
  justify-content: flex-end;
}
</style>
