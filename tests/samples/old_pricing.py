"""Frozen copy of the price-table code of runtime/tools/pa_costs.py at commit 99e7d8b (lines 39-152: KINDS, DATE,
_price_entry, load_prices, model_key, price_for, money; LONG from line 175), before t-032 J1 added price scopes.
Used only by tests/test_pa_costs.py (J1A_Parity) to show that a price file with the new keys is read by the old
code exactly as from its top-level models, and that the new code prices rows without facts as the old one did.
Do not edit: it is the reference for "the figures are unchanged" (t-032 R-P1, A-P1).
"""
import json
import os
import re

KINDS = ("in", "out", "cw5m", "cw1h", "cr")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LONG = 200000

def _price_entry(entry, where):
    if not isinstance(entry, dict) or not isinstance(entry.get("validFrom"), str) or not DATE.match(entry["validFrom"]):
        raise ValueError("{}: validFrom must be a date YYYY-MM-DD".format(where))
    for k in KINDS:
        v = entry.get(k, 0)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            raise ValueError("{}: price {} must be a number >= 0 or null".format(where, k))
    long = entry.get("long")
    if long is not None:
        if not isinstance(long, dict):
            raise ValueError("{}: long must be an object".format(where))
        for k in KINDS:
            v = long.get(k, 0)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise ValueError("{}: long.{} must be a number >= 0 or null".format(where, k))
        if "above" in long:
            v = long["above"]
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError("{}: long.above must be a positive whole number of prompt tokens, or left out".format(where))
    return entry


def load_prices(profile):
    """(table, problem). (None, None) when the profile has no prices.json; (None, why) when it is unusable,
    and then everything is shown as tokens only."""
    path = os.path.join(profile.get("_folder") or "", "prices.json")
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
            raise ValueError("no models object")
        if not isinstance(raw.get("currency"), str) or not raw["currency"]:
            raise ValueError("no currency")
        models = {}
        for name, entries in raw["models"].items():
            if not isinstance(entries, list):
                entries = [entries]
            models[name] = sorted((_price_entry(e, name) for e in entries), key=lambda e: e["validFrom"])
        aliases = []
        for a in raw.get("aliases") or []:
            if not isinstance(a, dict) or not isinstance(a.get("match"), str) or a.get("model") not in models:
                raise ValueError("an alias needs match (a regex) and model (a key in models)")
            aliases.append((re.compile(a["match"]), a["model"]))
    except (OSError, ValueError, re.error) as e:
        return None, "prices.json of profile {} is not usable ({}): tokens only".format(profile.get("name"), str(e)[:160])
    # The ledger keeps one long-context part, split at LONG prompt tokens. Another threshold is accepted but named.
    warnings = []
    for name, entries in sorted(models.items()):
        other = sorted({e["long"]["above"] for e in entries if e.get("long") and e["long"].get("above", LONG) != LONG})
        if other:
            warnings.append("prices.json of profile {}: {} long.above {} differs from the {} prompt tokens the ledger splits at: "
                            "calls in between are priced at the base rate".format(profile.get("name"), name[:80],
                                                                                   ", ".join(str(x) for x in other), LONG))
    return {"currency": raw["currency"], "unit": raw.get("unit"), "source": raw.get("source"), "asOf": raw.get("asOf"),
            "note": raw.get("note"), "profile": profile.get("name"), "models": models, "aliases": aliases,
            "warnings": warnings}, None


def model_key(table, model):
    if not table or not model:
        return None
    if model in table["models"]:
        return model
    for pattern, key in table["aliases"]:
        if pattern.search(model):
            return key
    return None


def price_for(table, model, when):
    """The price entry in effect for model at `when` (ISO time): the latest validFrom on or before its day."""
    key = model_key(table, model)
    if key is None:
        return None
    day, found = (when or "")[:10], None
    for entry in table["models"][key]:
        if entry["validFrom"] <= day:
            found = entry
    return found


def money(tokens, long, price):
    """Σ tokens x price / 1e6, the long-context part priced from price.long when there is one. None when a kind
    that has tokens has no price."""
    if not price:
        return None
    long = long or {}
    high = price.get("long") or price
    total = 0.0
    for k in KINDS:
        n, n_long = int(tokens.get(k) or 0), int(long.get(k) or 0)
        n_long = min(n_long, n)
        for count, p in ((n - n_long, price.get(k, 0)), (n_long, high.get(k, price.get(k, 0)))):
            if not count:
                continue
            if p is None:
                return None
            total += count * p / 1e6
    return total
