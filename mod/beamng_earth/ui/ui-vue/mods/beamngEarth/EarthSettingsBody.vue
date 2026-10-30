<template>
  <!-- One page of BeamNG.Earth's settings (earthSettingsStore.js: the schema and the shared state): its presets and
       rows, the game's own BngRow / BngSelect / BngSwitch ... Shown in the pause menu's BeamNG.Earth tab
       (EarthSettingsPage.vue) and in the full settings page (EarthSettings.vue). -->
  <div class="earth-main">
    <BngCardHeading>{{ category.name }}</BngCardHeading>
    <div v-if="category.intro" class="earth-intro">{{ category.intro }}</div>

    <!-- Quality presets: one press sets every setting below; changing one makes it Custom -->
    <template v-if="category.presets">
      <div class="earth-presets">
        <BngButton
          v-for="p in qualityPresets"
          :key="p.id"
          class="earth-preset"
          :accent="config.quality === p.id ? ACCENTS.main : ACCENTS.outlined"
          @click="setQuality(p.id)"
          @mouseenter="setHelp(p)"
          @focusin="setHelp(p)"
        >{{ p.name }}</BngButton>
      </div>
      <div class="earth-intro">
        {{ config.quality === "custom" ? "Custom: your own mix of the settings below." : presetLine }}
        <template v-if="detectedPick"> Recommended for this computer: {{ presetName(detectedPick) }}.</template>
      </div>
    </template>

    <BngRow
      v-for="item in rows"
      :key="item.id"
      class="earth-row"
      :vertical="item.type === 'text' || item.type === 'path'"
      @mouseenter="setHelp(item)"
      @focusin="setHelp(item)"
    >
      <template #label>{{ item.name }}</template>

      <BngSelect
        v-if="item.type === 'select'"
        :value="valueOf(item)"
        :options="item.options"
        :config="optionConfig"
        @valueChanged="val => selectOption(item, val)"
      />
      <BngSwitch
        v-else-if="item.type === 'bool'"
        :modelValue="config[item.id] === true"
        @update:modelValue="val => onSwitchChange(item, val)"
      />
      <BngSlider
        v-else-if="item.type === 'float'"
        :min="item.min"
        :max="item.max"
        :step="item.step || 0.01"
        :modelValue="config[item.id]"
        @update:modelValue="val => onSliderChange(item, val)"
        :with-input="true"
      />
      <div v-else-if="item.type === 'number'" class="earth-inline">
        <BngInput
          class="earth-number-input"
          :modelValue="numberText[item.id] ?? String(config[item.id] ?? '')"
          @update:modelValue="val => (numberText[item.id] = val)"
          @focus="typing(true)"
          @blur="typing(false); applyNumber(item)"
          @keydown.enter.stop.prevent="applyNumber(item)"
        />
        <span v-if="item.unit" class="earth-value">{{ item.unit }}</span>
      </div>
      <BngSwitch
        v-else-if="item.type === 'dev'"
        :modelValue="dev.on[item.id] === true"
        @update:modelValue="val => onDevChange(item, val)"
      />
      <span v-else-if="item.type === 'info'" class="earth-value">{{ infoValue(item) }}</span>
      <div v-else-if="item.type === 'text'" class="earth-text">{{ infoValue(item) }}</div>
      <BngButton v-else-if="item.type === 'feedback'" :accent="ACCENTS.outlined" @click="openFeedback(item.what)">{{ item.label }}</BngButton>

      <!-- Clear cache: asks in the row first -->
      <div v-else-if="item.type === 'action'" class="earth-inline">
        <span v-if="cacheConfirm" class="earth-value">Delete the downloaded elevation and imagery?</span>
        <span v-else-if="cacheResult" class="earth-value">{{ cacheResult }}</span>
        <BngButton v-if="cacheConfirm" :accent="ACCENTS.main" @click="confirmClearCache">Clear</BngButton>
        <BngButton v-if="cacheConfirm" :accent="ACCENTS.outlined" @click="cancelClearCache">Cancel</BngButton>
        <BngButton v-else :accent="ACCENTS.outlined" :disabled="cacheBusy" @click="askClearCache">{{ cacheBusy ? "Clearing..." : "Clear cache" }}</BngButton>
      </div>

      <!-- A path: the system's window, or typed -->
      <div v-else-if="item.type === 'path'" class="earth-path">
        <div class="earth-inline">
          <BngInput
            class="earth-path-input"
            v-model="pathText[item.what]"
            :placeholder="item.placeholder"
            @focus="typing(true)"
            @blur="typing(false)"
            @keydown.enter.stop.prevent="usePath(item)"
          />
          <BngButton :accent="ACCENTS.outlined" :disabled="!!pathBusy || !(pathText[item.what] || '').trim()" @click="usePath(item)">Use</BngButton>
          <BngButton :accent="ACCENTS.outlined" :disabled="!!pathBusy" @click="sendPath(item, true)">{{ pathBusy === item.what && pathPick ? "Window open..." : "Change..." }}</BngButton>
        </div>
        <div class="earth-note" :class="{ 'is-error': pathMsg[item.what] && !pathMsg[item.what].ok }">{{ pathNote(item) }}</div>
      </div>
    </BngRow>

    <!-- Developer: the overlay presets (session only) -->
    <div v-if="category.devPresets" class="earth-presets">
      <BngButton
        v-for="p in devPresets"
        :key="p.id"
        :accent="dev.lastPreset === p.id && dev.active ? ACCENTS.main : ACCENTS.outlined"
        @click="applyDevPreset(p.id)"
      >{{ p.label }}</BngButton>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive } from "vue"
import { BngButton, BngSwitch, BngSlider, BngInput, BngSelect, BngRow, BngCardHeading, ACCENTS } from "@/common/components/base"
import {
  config, dev, help, qualityPresets, devPresets, presetLine, detectedPick, optionConfig, categoryNamed, visibleItems,
  cacheConfirm, cacheBusy, cacheResult, pathText, pathBusy, pathPick, pathMsg,
  setQuality, presetName, valueOf, selectOption, onSwitchChange, onSliderChange, onNumberEnter, onDevChange, infoValue,
  openFeedback,
  confirmClearCache, askClearCache, usePath, sendPath, pathNote, typing, applyDevPreset,
} from "./earthSettingsStore.js"

const props = defineProps({
  page: { type: String, default: "Graphics" },     // the page's name (settingsSchema's)
})

const category = computed(() => categoryNamed(props.page))
const rows = computed(() => visibleItems(category.value))

function setHelp(item) { help.value = item }
function cancelClearCache() { cacheConfirm.value = false }
// typed numbers (type "number"): the text being typed per setting, until Enter or leaving the box applies it
const numberText = reactive({})
function applyNumber(item) {
  const t = numberText[item.id]
  if (t === undefined) return
  onNumberEnter(item, t)
  delete numberText[item.id]          // (shows the value in use again: the clamped one, or the old one after a bad entry)
}
</script>

<style scoped lang="scss">
/* Only spacing: the cards, rows and controls are the game's own */
.earth-number-input { width: 7em; }
.earth-main {
  display: flex;
  flex-direction: column;
  gap: 0.25em;
  max-height: 80vh;
  overflow-y: auto;
  padding: 0.5em 0.75em 1em;
}

.earth-intro,
.earth-note {
  font-size: 0.9em;
  color: var(--bng-cool-gray-300, #bbb);
  padding: 0.2em 0.25em 0.5em;
}

.earth-note.is-error { color: var(--bng-add-red-400, #f88); }

.earth-presets {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(5.5em, 1fr));     /* (5 in a row on the full page, fewer in the pause menu's card) */
  gap: 0.5em;
  padding: 0.25em 0 0.5em;
}

.earth-value { color: var(--bng-cool-gray-200, #ccc); text-align: right; }

.earth-text {
  white-space: pre-wrap;
  font-size: 0.9em;
  line-height: 1.35;
  color: var(--bng-cool-gray-200, #ccc);
}

.earth-inline {
  display: flex;
  align-items: center;
  gap: 0.5em;
}

.earth-path {
  display: flex;
  flex-direction: column;
  width: 100%;

  .earth-path-input { flex: 1 1 auto; min-width: 0; }
}
</style>
