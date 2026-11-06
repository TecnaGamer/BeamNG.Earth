"""The German junction signs switch: the settings page's "German junction signs" (the mod's german_junctions
message, Server.dej_on), or BNE_DEJUNCTIONS=on / off to force it whatever the page says. Nothing else is here yet."""
import os

ENV = os.environ.get("BNE_DEJUNCTIONS", "")


def enabled(server_flag):
    """Whether the German junction signs are built: BNE_DEJUNCTIONS=on / off forces it, anything else follows
    server_flag (Server.dej_on)."""
    if ENV == "on":
        return True
    if ENV == "off":
        return False
    return bool(server_flag)
