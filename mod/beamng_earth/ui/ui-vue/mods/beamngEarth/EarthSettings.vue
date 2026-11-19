<template>
  <!-- BeamNG.Earth's settings as a page of its own (a route: the main menu has no pause menu; in a level the pause
       menu's BeamNG.Earth tab shows the same pages, EarthSettingsPage.vue): the game's LayoutMenu (its tabs, cards,
       back handling and controller hints), the pages' rows (EarthSettingsBody.vue) and the side card
       (EarthSettingsSide.vue), one shared state (earthSettingsStore.js). -->
  <LayoutMenu
    ref="layout"
    class="earth-settings"
    :tabs="tabs"
    v-model:selected-tab="section"
    main-card-class="menu-content-card-1--extra-wide"
    side-card-class="menu-content-card-2--wide"
  >
    <template #card1>
      <EarthSettingsBody :page="pageName" />
    </template>
    <template #card2>
      <EarthSettingsSide :back="true" />
    </template>
  </LayoutMenu>
</template>

<script setup>
import { t } from "./i18n.js"
import { ref, computed, watch, onMounted, onUnmounted } from "vue"
import { LayoutMenu } from "@/common/layouts"
import { icons } from "@/common/components/base"
import EarthSettingsBody from "./EarthSettingsBody.vue"
import EarthSettingsSide from "./EarthSettingsSide.vue"
import { settingsSchema, PAGE_ICONS, startPolling, stopPolling } from "./earthSettingsStore.js"

const section = ref(0)        // the tab shown
const layout = ref(null)
// LayoutMenu's tabs (its own tab bar: Q / E on a controller), one per page
const tabs = computed(() => settingsSchema.map(c => ({ id: c.name, label: t(c.label), icon: icons[PAGE_ICONS[c.name]] })))
const pageName = computed(() => (settingsSchema[section.value] || settingsSchema[0]).name)

watch(section, () => {
  const tabs = layout.value?.$el?.querySelector?.(".menu-tabs")
  tabs?.querySelector(".tab-active-tab")?.scrollIntoView({ block: "nearest", inline: "nearest" })
}, { flush: "post" })

onMounted(startPolling)
onUnmounted(stopPolling)
</script>

<style scoped>
.earth-settings :deep(.menu-tabs-switcher) {
  max-width: 100%;
}

.earth-settings :deep(.menu-tabs) {
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: thin;
}
</style>
