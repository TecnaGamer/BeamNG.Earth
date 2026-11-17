"""netfetch's per-host request limits as JSON, for the cache proxy's upstream misses (cacheproxy.py; proxy.sh writes it).

    limits.py SERVER_DIR > limits.json      (run with the server's Python: netfetch imports its dependencies)

{"default": [rate, burst, max_conc], "hosts": {host: [rate, burst, max_conc]}}. A lane ("host#lane") is left out: the
proxy keeps one limit per host, the host's own.
"""
import json
import sys


def main(server_dir):
    sys.path.insert(0, server_dir)
    import netfetch

    def row(p):
        return [p.rate, p.burst, p.max_conc]

    hosts = {h: row(p) for h, p in netfetch.POLICIES.items() if "#" not in h and not p.denied}
    json.dump({"default": row(netfetch.DEFAULT_POLICY), "hosts": hosts}, sys.stdout, indent=1, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1])
