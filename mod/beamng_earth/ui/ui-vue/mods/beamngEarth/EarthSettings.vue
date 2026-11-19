<template>
  <!-- BeamNG.Earth's settings as a page of its own (a route: the main menu has no pause menu; in a level the pause
       menu's BeamNG.Earth tab shows the same pages, EarthSettingsPage.vue): the game's LayoutMenu (its tabs, cards,
       back handling and controller hints), the pages' rows (EarthSettingsBody.vue) and the side card
       (EarthSettingsSide.vue), one shared state (earthSettingsStore.js). -->
  <LayoutMenu
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
import { ref, computed, onMounted, onUnmounted } from "vue"
import { LayoutMenu } from "@/common/layouts"
import { icons } from "@/common/components/base"
import EarthSettingsBody from "./EarthSettingsBody.vue"
import EarthSettingsSide from "./EarthSettingsSide.vue"
import { settingsSchema, PAGE_ICONS, startPolling, stopPolling } from "./earthSettingsStore.js"

const section = ref(0)        // the tab shown
// LayoutMenu's tabs (its own tab bar: Q / E on a controller), one per page
const tabs = computed(() => settingsSchema.map(c => ({ id: c.name, label: t(c.label), icon: icons[PAGE_ICONS[c.name]] })))
const pageName = computed(() => (settingsSchema[section.value] || settingsSchema[0]).name)

onMounted(startPolling)
onUnmounted(stopPolling)
</script>
