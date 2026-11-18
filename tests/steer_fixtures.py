"""Fixture helpers that steer what plain git says a commit's parents are (t-052 J1 round 3, verify MF-R2-1).

Used by tests/test_pa_gate.py and tests/test_pa_push_gate_git.py. git runs with the caller's environment (the test
modules' temp homes, no global or system config), only on repositories the tests made under their temp folders.
"""
import os
import struct
import subprocess


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if r.returncode:
        raise AssertionError("git {} failed in {}: {}".format(" ".join(args), repo, r.stderr))
    return r.stdout.strip()


def forge_parent(repo, child, parent):
    """Rewrite the repository's commit-graph so that `child`'s first parent reads as `parent` (generation v1, the
    child's level set one above the parent's). The commit objects are unchanged; plain git believes the graph."""
    git(repo, "-c", "commitGraph.generationVersion=1", "commit-graph", "write", "--reachable", "--no-progress")
    graph = os.path.join(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"), "objects", "info", "commit-graph")
    data = bytearray(open(graph, "rb").read())
    chunks = {}
    for i in range(data[6] + 1):
        cid, off = struct.unpack(">4sQ", data[8 + 12 * i:20 + 12 * i])
        chunks[cid] = off
    count = (chunks[b"CDAT"] - chunks[b"OIDL"]) // 20
    oids = [bytes(data[chunks[b"OIDL"] + 20 * i:chunks[b"OIDL"] + 20 * i + 20]).hex() for i in range(count)]
    at = lambda sha: chunks[b"CDAT"] + 36 * oids.index(sha) + 20
    level = struct.unpack(">I", data[at(parent) + 8:at(parent) + 12])[0] >> 2
    data[at(child):at(child) + 4] = struct.pack(">I", oids.index(parent))
    low = struct.unpack(">I", data[at(child) + 8:at(child) + 12])[0] & 3
    data[at(child) + 8:at(child) + 12] = struct.pack(">I", ((level + 1) << 2) | low)
    os.chmod(graph, 0o644)
    with open(graph, "wb") as f:
        f.write(bytes(data))

