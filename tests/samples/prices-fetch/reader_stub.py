#!/usr/bin/env python3
"""t-032 J2 test reader (neutral, made up): turns tests/samples/prices-fetch/list-*.json into the reader contract's
lines (spec R-F3, A-F2). A file with "raw" is printed as it is and the stub exits with its "exit" (refusal cases).
Its last stderr line names the environment's keys (never values), so a test can check what the reader was given."""
import json
import os
import sys


def main(argv):
    if "--file" not in argv or "--region" not in argv:
        print("usage: reader_stub.py --file F --region R", file=sys.stderr)
        return 2
    path, region = argv[argv.index("--file") + 1], argv[argv.index("--region") + 1] or None
    with open(path) as f:
        data = json.load(f)
    try:
        if "raw" in data:
            sys.stdout.write(data["raw"])
            return int(data.get("exit", 0))
        if data.get("format") != "neutral-list-1":
            print("not a neutral-list-1 file", file=sys.stderr)
            return 6
        for left in data.get("leftOut") or []:
            print("left out: {}".format(left), file=sys.stderr)
        free = data.get("regionFree", ["global"])
        for r in data["rows"]:
            line = {"provider": r["provider"], "account": r.get("account"), "endpoint": r.get("endpoint"),
                    "region": None if r.get("endpoint") in free else region, "model": r["model"], "kind": r["kind"],
                    "price": r["price"], "effectiveDate": r.get("effective", data["effective"]),
                    "ref": {"sku": r.get("sku", "SKU-" + r["model"]), "usagetype": "{}-{}-{}".format(region, r.get("endpoint"), r["kind"]),
                            "rateCode": "RC-1", "raw": str(r["price"])}}
            if r["price"] is None:
                line["why"] = r.get("why", "no row")
            print(json.dumps(line))
        print(json.dumps({"done": True, "source": {"url": data["url"], "version": data["version"],
                                                   "publicationDate": data["publication"]}}))
        return 0
    finally:
        print("env: " + ",".join(sorted(os.environ)), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
