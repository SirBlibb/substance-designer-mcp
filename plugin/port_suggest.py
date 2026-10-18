"""
port_suggest.py - pure-Python helpers for port-ID validation messages.

Kept free of sd.api imports so it can be unit-tested outside Substance Designer
and hot-reloaded by the plugin's reload_plugin command.
"""
import difflib


def rank_ports(wanted, port_ids):
    """Return port_ids ordered by similarity to `wanted` (closest first).

    Case-insensitive; a substring match in either direction ranks above a
    plain fuzzy match so e.g. 'input' -> 'input1' and 'Output' -> 'Noise_Output'
    surface first. Ties keep the original order.
    """
    w = str(wanted or "").lower()

    def score(pid):
        p = pid.lower()
        ratio = difflib.SequenceMatcher(None, w, p).ratio()
        if w and (w in p or p in w):
            ratio += 1.0
        return ratio

    return sorted(port_ids, key=score, reverse=True)


def port_error(kind, wanted, node_label, valid_ids, other_kind=None, other_ids=None):
    """Build a 'port not found' error message listing valid IDs, closest first.

    kind: "Input" or "Output"; node_label: e.g. "1234 (sbs::compositing::blur)".
    other_kind/other_ids: the node's ports in the other direction, listed for context
    (a common mistake is passing an output ID as to_input or vice versa).
    """
    ranked = rank_ports(wanted, valid_ids)
    msg = "{} port '{}' not found on node {}.".format(kind, wanted, node_label)
    if ranked:
        msg += " Did you mean '{}'? Valid {} port IDs (closest first): {}.".format(
            ranked[0], kind.lower(), ranked)
    else:
        msg += " The node has no {} ports.".format(kind.lower())
    if other_kind and other_ids:
        msg += " {} port IDs: {}.".format(other_kind, list(other_ids))
    return msg
