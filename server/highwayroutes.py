"""Network identities, independent of road class or geographic rendering guesses."""
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import NamedTuple

DATA = Path(__file__).with_name('data')/'shields'


class Route(NamedTuple):
    network: str
    ref: str
    modifier: str = ''


@lru_cache(maxsize=1)
def registry():
    return json.loads((DATA/'networks.json').read_text(encoding='utf-8'))


def network(value):
    value = str(value or '').strip()
    aliases = registry()['aliases']
    value = aliases.get(value.upper(), value.upper())
    if value in registry()['networks']:
        return value
    parts=value.split(':')
    for end in range(len(parts)-1,1,-1):
        base=':'.join(parts[:end])
        if base in registry()['networks'] and all(p in registry()['modifiers'] for p in parts[end:]):
            return base
    return ''


def _ref(value, net):
    value = str(value or '').strip().upper()
    if net=='US:I' and re.fullmatch(r'H-?[123]',value):
        value=value.replace('-','')
    pattern=registry()['networks'].get(net,{}).get('ref_pattern',r'\d{1,4}[A-Z]?')
    return value if re.fullmatch(pattern,value) else ''


def _modifier(value):
    aliases=registry().get('modifier_aliases',{})
    return ' '.join(aliases.get(word,word) for word in str(value or '').upper().split())


def recognize(tags, relations=(), state='', destination=False):
    """Relation-first. Way refs supplement only networks absent from relations.

    Bare numbers require network tags; SR/SH require state context. destination:ref is
    read only for destination boards, never as the route of the current road.
    """
    found = set()
    for rel in (() if destination else relations):
        if rel.get('type', 'route') != 'route' or rel.get('route', 'road') != 'road':
            continue
        net=network(rel.get('network'))
        ref=_ref(rel.get('ref'),net)
        ref_modifier=''
        if net and not ref:
            relation_state=registry()['networks'][net].get('state',state)
            for candidate in recognize({'ref':rel.get('ref'),'network':net},state=relation_state):
                if candidate.network==net or net.startswith(candidate.network+':'):
                    ref=candidate.ref;ref_modifier=candidate.modifier;break
        if net and ref:
            raw=str(rel.get('network','')).upper()
            raw=registry()['aliases'].get(raw,raw)
            suffix=raw[len(net):].strip(':').replace(':',' ')
            modifier=_modifier(rel.get('modifier') or suffix or ref_modifier)
            found.add(Route(net, ref, modifier))
    preferred = {r.network for r in found}
    key = 'destination:ref' if destination else 'ref'
    for raw in re.split(r'[;|/]',str(tags.get(key, '') or '')):
        raw = raw.strip().upper()
        m = re.fullmatch(r'([A-Z:]+)[ .-]+([A-Z0-9-]+)(?:\s+([A-Z ]+))?', raw)
        if not m:
            m=re.fullmatch(r'([A-Z:]+?)(\d{1,4}[A-Z]?)(?:\s+([A-Z ]+))?',raw)
        if m:
            prefix, ref, modifier = m.groups()
            net = registry()['prefixes'].get(prefix, '')
            contextual=registry().get('context_prefixes',{}).get(prefix)
            if contextual:
                net=contextual['network'] if state==contextual['state'] else ''
            if prefix in ('SR', 'SH', 'WYO'):
                net = 'US:WY' if prefix == 'WYO' else ('US:'+state if state else '')
            net = network(net or (prefix if not contextual else ''))
        elif _ref(raw,network(tags.get('network'))):
            net, ref, modifier = network(tags.get('network')), raw, ''
        else:
            bare=re.fullmatch(r'([A-Z0-9-]+)\s+([A-Z ]+)',raw)
            if bare and network(tags.get('network')):
                net,ref,modifier=network(tags.get('network')),*bare.groups()
            else:
                continue
        for choice in registry()['networks'].get(net,{}).get('ref_networks',()):
            if re.fullmatch(choice['pattern'],ref):net=choice['network'];break
        ref=_ref(ref,net)
        modifier=_modifier(modifier or tags.get('modifier'))
        if modifier and not all(word in registry()['modifiers'] for word in modifier.split()):
            continue
        if net and ref and net not in preferred:
            found.add(Route(net, ref, modifier))
    return sorted(found)


def panel(route, direction=''):
    config = registry()['networks'][route.network]
    result=dict(network=route.network, kind=config['kind'], state=config.get('state',''),
                number=route.ref, modifier=route.modifier, direction=direction,
                template=template_for(route.network,route.ref,route.modifier))
    if config.get('display_ref'):
        result['display_number']=re.sub(*config['display_ref'],route.ref)
    return result


def template_for(net, ref, modifier=''):
    config=registry()['networks'].get(net,{})
    template=config.get('template')
    for choice in config.get('width_variants',()):
        if len(ref)>=choice['min_chars']:
            template=choice['template']
    variant=config.get('design_variants',{}).get(modifier,template)
    if isinstance(variant,dict):
        return variant.get('wide_template',variant['template']) if len(ref)>=variant.get('min_chars',3) else variant['template']
    return variant


def identity(panel):
    net = panel.get('network') or {'interstate':'US:I','us':'US:US'}.get(panel.get('kind'), 'US:'+panel.get('state',''))
    return net, panel['number'], panel.get('modifier',''), panel.get('direction','')


def current_panel(panel):
    """Cached shards/inventories do not freeze an obsolete template choice."""
    raw=str(identity(panel)[0]).upper()
    raw=registry()['aliases'].get(raw,raw)
    net=network(raw)
    if not net:
        return dict(panel,kind='state',template=None) if panel.get('network') else panel
    modifier=_modifier(panel.get('modifier') or raw[len(net):].strip(':').replace(':',' '))
    return dict(panel,network=net,modifier=modifier,template=template_for(net,panel['number'],modifier))
