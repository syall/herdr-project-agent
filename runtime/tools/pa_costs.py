#!/usr/bin/env python3
"""Cost tracking per space: tokens from the session files, estimated money from the profile's price table.
Python standard library only (3.7+). Linked as pa-costs.

  pa-costs collect [--space ID] [--quiet] [--resummarize]   read new session bytes into the space's cost ledger
  pa-costs report [--space ID] [--by KEY] [--since D] [--until D] [--json] [--billed] [--account-wide]
  pa-costs reconstruct [--space ID] [--dry-run]              find the space's sessions from before launch records
  pa-costs prices [--space ID]                               the price table in use, and models seen without a price
  pa-costs billed status|list|remove|disable|enable|fetch|import ...   billed amounts (the person's commands)

Files, in the space's state folder (costs/): sessions.jsonl (launch and other session records, append-only),
turns.jsonl (the cost ledger: tokens per settled segment, append-only), state.json (read offsets), summary.json
(what the panel reads). Billed rows: <HERDR_PA_HOME>/billing/. Counters and ids only: no session text is kept,
printed or logged. Nothing here uses the network or reads any account; billed rows come only from a script the
profile declares, which only the person turns on (pa-costs billed enable, in a terminal).
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True  # keep tools/ free of caches
import pa  # noqa: E402

CLAUDE_HOME = pa.CLAUDE_HOME
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(pa.HOME, ".codex")
KINDS = ("in", "out", "cw5m", "cw1h", "cr")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def zero():
    return {k: 0 for k in KINDS}


def total_tokens(tokens):
    return sum(int((tokens or {}).get(k) or 0) for k in KINDS)


# ---- price table (profile data: profiles/<name>/prices.json) --------------------------------------

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
        for k in KINDS + ("above",):
            v = long.get(k, 0)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                raise ValueError("{}: long.{} must be a number >= 0 or null".format(where, k))
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
    return {"currency": raw["currency"], "unit": raw.get("unit"), "source": raw.get("source"), "asOf": raw.get("asOf"),
            "note": raw.get("note"), "profile": profile.get("name"), "models": models, "aliases": aliases}, None


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


def row_money(row, table):
    """A ledger row's money, from its tokens, model and time only."""
    return money(row.get("tokens") or {}, row.get("long") or {}, price_for(table, row.get("model"), row.get("from")))


def price_rows(rows, table):
    """{"money", "unpricedTokens", "missing"} over rows. money is None when nothing is priced."""
    priced, unpriced, missing, any_priced = 0.0, 0, set(), False
    for row in rows:
        m = row_money(row, table)
        if m is None:
            unpriced += total_tokens(row.get("tokens"))
            missing.add(row.get("model") or "(unknown model)")
        else:
            priced += m
            any_priced = True
    return {"money": priced if any_priced else None, "unpricedTokens": unpriced, "missing": sorted(missing)}


# ---- command line ------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="pa-costs")
    parser.parse_args(argv)
    raise pa.Problem("give a subcommand: collect, report, reconstruct, prices or billed")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except pa.Problem as e:
        print("pa-costs: {}".format(e), file=sys.stderr)
        sys.exit(2)
