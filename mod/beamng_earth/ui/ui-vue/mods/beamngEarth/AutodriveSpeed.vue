<template>
  <div class="auto-speed" role="group" aria-label="Autodrive cruise speed offset">
    <span aria-live="polite">Cruise: limit {{ offset < 0 ? '−' : '+' }} {{ Math.abs(offset) }} mph</span>
    <button type="button" :disabled="offset <= -100" @click="adjust(-5)"
            title="Decrease autodrive cruise speed by 5 mph">−5 mph</button>
    <button type="button" :disabled="offset >= 100" @click="adjust(5)"
            title="Increase autodrive cruise speed by 5 mph">+5 mph</button>
    <button type="button" :disabled="offset === 0" @click="adjust(0)"
            title="Cruise at the road's speed limit">Reset</button>
    <span v-if="error" role="alert">{{ error }}</span>
  </div>
</template>

<script setup>
import { ref } from "vue"

const props = defineProps({ offset: { type: Number, default: 0 }, sendLua: { type: Function, required: true } })
const emit = defineEmits(["change"])
const error = ref("")
function adjust(step) {
  if (step !== -5 && step !== 0 && step !== 5) return
  props.sendLua(`beamngEarth_settings and beamngEarth_settings.uiGpsSpeedOffset(${step}) or nil`, r => {
    error.value = r && r.ok ? "" : (r && r.error) || "Could not update cruise speed"
    if (r && r.ok) emit("change", r.value)
  })
}
</script>

<style scoped>
.auto-speed { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; font-size: 12px; }
.auto-speed button { color: inherit; background: #ffffff12; border: 1px solid #ffffff40; border-radius: 4px; padding: 4px 8px; font: inherit; cursor: pointer; }
.auto-speed button:hover:not(:disabled) { background: #ffffff28; }
.auto-speed button:disabled { opacity: .4; cursor: default; }
</style>
