import { messageText, t } from "./i18n.js"

const errors = new Map([
  ["type a latitude and longitude", "map.error.coordinateRequired"],
  ["not a latitude, longitude", "map.error.invalidCoordinates"],
  ["two latitudes or two longitudes", "map.error.duplicateAxis"],
  ["latitude must be between -90 and 90", "map.error.latitudeRange"],
  ["longitude must be between -180 and 180", "map.error.longitudeRange"],
  ["latitude must be -90..90 and longitude -180..180", "map.error.locationRange"],
  ["no such place", "map.error.placeMissing"],
  ["no such recent place", "map.error.recentMissing"],
  ["type a name", "map.error.needName"],
  ["Companion server not connected", "map.error.disconnected"],
  ["Load the BeamNG.Earth level first", "map.error.levelRequired"],
  ["Loading the ground: wait a moment", "map.error.groundLoading"],
  ["teleport refused (see the status line)", "map.error.teleportRefused"],
  ["Position unknown (not in a BeamNG.Earth world yet)", "map.error.positionUnknown"],
  ["BeamNG.Earth is not loaded", "common.modUnavailable"],
])

export function mapMessage(value) {
  const key = errors.get(value)
  return key ? t(key) : messageText(value)
}
