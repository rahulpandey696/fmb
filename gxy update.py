#!/usr/bin/env python3
"""
gxy_update.py  -  Add / remove nodes in a Flood Modeller .gxy file from CSV lists.

What it does
------------
* REMOVE list : deletes each node's [section] and rewires its connections so the
                upstream node(s) link straight to the downstream node(s).
* ADD list    : creates a [section] for each new node, places it at the midpoint
                between its previous and next nodes, and splits the
                previous -> next connection into previous -> new -> next.
* Removals are processed first, then additions (so "replace node A with B"
  works: remove A, then add B between A's old neighbours).
* Rewrites [Connections] with fresh numbering and ConnectionCount.
* Everything else in the file is kept as-is. The input file is never modified.
* A .log file is written next to the output listing every change and warning.

CSV formats (header row optional, extra columns ignored)
--------------------------------------------------------
Add CSV    : unit_name, type, previous_node, next_node
Remove CSV : unit_name, type

"type" is the unit type as it appears in the .gxy section names, e.g.
    RIVER_SECTION   (or "RIVER SECTION")  ->  [RIVER_SECTION_<name>]
    INTERPOLATE                           ->  [INTERPOLATE__<name>]
    QTBDY                                 ->  [QTBDY__<name>]
    JUNCTION_OPEN                         ->  [JUNCTION_OPEN_<name>]
A type with no subtype gets a double underscore, matching Flood Modeller.
For existing nodes (previous/next/remove) the type is optional; it is only
used to pick the right node if the same label appears under several types.

Usage
-----
    python gxy_update.py          (then answer the prompts)
"""

import csv
import os
import sys
from datetime import datetime

LOG = []


def log(msg):
    print(msg)
    LOG.append(msg)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
def ask_path(prompt, hint, must_exist=True, optional=False, default=None):
    while True:
        print(f"\n{prompt}")
        print(f"  Hint: {hint}")
        if default:
            print(f"  Press Enter to use: {default}")
        elif optional:
            print("  Press Enter to skip.")
        raw = input("> ").strip().strip('"').strip("'")
        if not raw:
            if default:
                raw = default
            elif optional:
                return None
            else:
                print("  A path is required.")
                continue
        raw = os.path.abspath(os.path.expanduser(raw))
        if must_exist and not os.path.isfile(raw):
            print(f"  File not found: {raw}")
            continue
        return raw


def ask_yes_no(prompt, default=False):
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}] > ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
def looks_like_header(row):
    first = row[0].lower() if row else ""
    second = row[1].lower() if len(row) > 1 else ""
    return ("name" in first or "label" in first or first in ("unit", "node")
            or second in ("type", "unit type", "node type", "unit_type"))


def read_csv(path, ncols):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                raw_rows = list(csv.reader(f))
            break
        except UnicodeDecodeError:
            continue
    rows = []
    for i, row in enumerate(raw_rows):
        row = [c.strip() for c in row]
        if not any(row):
            continue
        if i == 0 and looks_like_header(row):
            continue
        row += [""] * (ncols - len(row))
        if not row[0]:
            log(f"  WARNING: {os.path.basename(path)} line {i + 1}: empty unit name, skipped")
            continue
        rows.append(row[:ncols])
    return rows


# --------------------------------------------------------------------------
# GXY model
# --------------------------------------------------------------------------
def norm_type(t):
    t = (t or "").strip().upper().replace(" ", "_").replace("-", "_")
    while "__" in t:
        t = t.replace("__", "_")
    return t.strip("_")


def make_key(unit_type, label):
    t = norm_type(unit_type)
    return f"{t}_{label}" if "_" in t else f"{t}__{label}"


class Gxy:
    def __init__(self, path):
        with open(path, "rb") as f:
            data = f.read()
        self.newline = "\r\n" if b"\r\n" in data else "\n"
        text = data.decode("latin-1")  # lossless round-trip of every byte
        self.preamble = []
        self.sections = []  # list of [name, [lines]]
        current = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                current = [s[1:-1], []]
                self.sections.append(current)
            elif current is None:
                self.preamble.append(line)
            else:
                current[1].append(line)

        # Pull out the connections section
        self.conns = []
        self.conn_extra = []
        self.conn_index = None
        for i, (name, lines) in enumerate(self.sections):
            if name.strip().lower() == "connections":
                self.conn_index = i
                for line in lines:
                    if "=" not in line:
                        if line.strip():
                            self.conn_extra.append(line)
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k.lower() == "connectioncount":
                        continue
                    if k.isdigit() and "," in v:
                        a, b = v.split(",", 1)
                        self.conns.append((a.strip(), b.strip()))
                    else:
                        self.conn_extra.append(line)
                break
        if self.conn_index is None:
            self.sections.append(["Connections", []])
            self.conn_index = len(self.sections) - 1
            log("  NOTE: no [Connections] section found - one will be created.")

        seen = set()
        for name, _ in self.sections:
            if name in seen:
                log(f"  WARNING: duplicate section [{name}] in input; first one is used for lookups")
            seen.add(name)

    # ---- node helpers ----------------------------------------------------
    def node_keys(self):
        return [n for n, lines in self.sections if self._xy_lines(lines)]

    @staticmethod
    def _xy_lines(lines):
        found = {}
        for idx, line in enumerate(lines):
            if "=" in line:
                k = line.split("=", 1)[0].strip().lower()
                if k in ("x", "y"):
                    found[k] = idx
        return found if len(found) == 2 else None

    def section(self, key):
        for sec in self.sections:
            if sec[0] == key:
                return sec
        return None

    def get_xy(self, key):
        sec = self.section(key)
        if not sec:
            return None
        pos = self._xy_lines(sec[1])
        if not pos:
            return None
        xs = sec[1][pos["x"]].split("=", 1)[1].strip()
        ys = sec[1][pos["y"]].split("=", 1)[1].strip()
        try:
            return float(xs), float(ys), xs, ys
        except ValueError:
            return None

    def resolve(self, label, unit_type=""):
        """Find the section key for a node label (type optional)."""
        keys = self.node_keys()
        if unit_type:
            exact = make_key(unit_type, label)
            if exact in keys:
                return exact
        for case_sensitive in (True, False):
            if case_sensitive:
                cands = [k for k in keys if k.endswith("_" + label)]
            else:
                cands = [k for k in keys if k.upper().endswith("_" + label.upper())]
            if unit_type and len(cands) > 1:
                t = norm_type(unit_type) + "_"
                typed = [k for k in cands if k.upper().startswith(t)]
                if typed:
                    cands = typed
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                log(f"  WARNING: label '{label}' is ambiguous ({', '.join(cands)}); "
                    f"add/fix the type column")
                return None
        return None

    # ---- edits -----------------------------------------------------------
    def remove_node(self, label, unit_type):
        key = self.resolve(label, unit_type)
        if not key:
            log(f"  SKIPPED remove '{label}': not found in .gxy")
            return
        involved = [i for i, c in enumerate(self.conns) if key in c]
        preds = [a for a, b in self.conns if b == key and a != key]
        succs = [b for a, b in self.conns if a == key and b != key]
        pos = involved[0] if involved else len(self.conns)
        self.conns = [c for c in self.conns if key not in c]
        bridges = [(p, s) for p in preds for s in succs
                   if p != s and (p, s) not in self.conns]
        self.conns[pos:pos] = bridges
        self.sections = [s for s in self.sections if s[0] != key]
        # connection section index may have shifted
        self.conn_index = next(i for i, s in enumerate(self.sections)
                               if s[0].strip().lower() == "connections")
        msg = f"  Removed [{key}]"
        if bridges:
            msg += "; reconnected " + ", ".join(f"{a} -> {b}" for a, b in bridges)
        elif involved:
            msg += "; it was an end node, links dropped"
        log(msg)

    def add_node(self, label, unit_type, prev_label, next_label):
        if not unit_type:
            log(f"  SKIPPED add '{label}': type column is empty")
            return
        key = make_key(unit_type, label)
        if self.section(key):
            log(f"  SKIPPED add '{label}': [{key}] already exists")
            return
        pk = self.resolve(prev_label) if prev_label else None
        nk = self.resolve(next_label) if next_label else None
        if prev_label and not pk:
            log(f"  WARNING add '{label}': previous node '{prev_label}' not found")
        if next_label and not nk:
            log(f"  WARNING add '{label}': next node '{next_label}' not found")
        if not pk and not nk:
            log(f"  SKIPPED add '{label}': no previous or next node to place it from")
            return

        # Coordinates
        p_xy = self.get_xy(pk) if pk else None
        n_xy = self.get_xy(nk) if nk else None
        ref = p_xy or n_xy
        dec = len(ref[2].split(".")[1]) if "." in ref[2] else 3
        if p_xy and n_xy:
            x, y = (p_xy[0] + n_xy[0]) / 2, (p_xy[1] + n_xy[1]) / 2
        else:
            x, y = ref[0] + 5.0, ref[1] + 5.0
            log(f"  NOTE add '{label}': only one neighbour found, placed 5 m off it - "
                f"move it in Flood Modeller")
        new_sec = [key, [f"x={x:.{dec}f}", f"y={y:.{dec}f}"]]

        # Insert section after previous node (or before next node)
        if pk:
            idx = next(i for i, s in enumerate(self.sections) if s[0] == pk) + 1
        elif nk:
            idx = next(i for i, s in enumerate(self.sections) if s[0] == nk)
        else:
            idx = self.conn_index
        if idx > self.conn_index:
            idx = self.conn_index
        self.sections.insert(idx, new_sec)
        self.conn_index = next(i for i, s in enumerate(self.sections)
                               if s[0].strip().lower() == "connections")

        # Connections
        if pk and nk and (pk, nk) in self.conns:
            i = self.conns.index((pk, nk))
            self.conns[i:i + 1] = [(pk, key), (key, nk)]
            log(f"  Added [{key}] between {pk} and {nk}")
        else:
            new_links = []
            if pk:
                new_links.append((pk, key))
            if nk:
                new_links.append((key, nk))
            # put them next to an existing link of the neighbour if possible
            anchor = next((i for i, c in enumerate(self.conns)
                           if (pk and pk in c) or (nk and nk in c)), len(self.conns) - 1)
            self.conns[anchor + 1:anchor + 1] = new_links
            note = " (no direct link existed between them - check the network)" \
                if pk and nk else ""
            log(f"  Added [{key}] linked " +
                ", ".join(f"{a} -> {b}" for a, b in new_links) + note)

    # ---- output ----------------------------------------------------------
    def write(self, path):
        conn_lines = [f"ConnectionCount={len(self.conns)}"]
        conn_lines += [f"{i}={a},{b}" for i, (a, b) in enumerate(self.conns)]
        conn_lines += self.conn_extra
        self.sections[self.conn_index][1] = conn_lines + [""]

        out = list(self.preamble)
        for name, lines in self.sections:
            out.append(f"[{name}]")
            out.extend(lines)
        text = self.newline.join(out)
        if not text.endswith(self.newline):
            text += self.newline
        with open(path, "wb") as f:
            f.write(text.encode("latin-1", errors="replace"))

    def check(self):
        keys = set(self.node_keys())
        dangling = sorted({n for c in self.conns for n in c if n not in keys})
        for n in dangling:
            log(f"  WARNING: connection refers to [{n}] which has no x/y section")
        linked = {n for c in self.conns for n in c}
        orphans = sorted(keys - linked)
        if orphans:
            log(f"  NOTE: {len(orphans)} node(s) have no connections: "
                + ", ".join(orphans[:10]) + (" ..." if len(orphans) > 10 else ""))


# --------------------------------------------------------------------------
def main():
    print("=" * 66)
    print(" Flood Modeller .gxy node updater")
    print(" Removes nodes, adds new ones, and rewires [Connections].")
    print(" Tip: in Windows Explorer, Shift + right-click a file >")
    print("      'Copy as path', then paste it here (quotes are fine).")
    print("=" * 66)

    gxy_in = ask_path("1) Input .gxy file",
                      r"e.g. C:\Models\River\River_Model.gxy (it will NOT be changed)")
    add_csv = ask_path("2) CSV of nodes TO ADD",
                       "columns: unit_name, type, previous_node, next_node",
                       optional=True)
    rem_csv = ask_path("3) CSV of nodes TO REMOVE",
                       "columns: unit_name, type",
                       optional=True)
    if not add_csv and not rem_csv:
        print("\nNothing to do - no CSV given.")
        return

    base, ext = os.path.splitext(gxy_in)
    default_out = f"{base}_updated{ext or '.gxy'}"
    while True:
        gxy_out = ask_path("4) Output .gxy file",
                           "a new file name; keep the .gxy name matching your .dat "
                           "when you use it in Flood Modeller",
                           must_exist=False, default=default_out)
        if not gxy_out.lower().endswith(".gxy"):
            gxy_out += ".gxy"
        if os.path.abspath(gxy_out) == os.path.abspath(gxy_in):
            print("  Output must be different from the input file.")
            continue
        if os.path.exists(gxy_out) and not ask_yes_no(f"  {gxy_out} exists. Overwrite?"):
            continue
        break

    print()
    log(f"gxy_update run {datetime.now():%Y-%m-%d %H:%M}")
    log(f"Input : {gxy_in}")
    g = Gxy(gxy_in)
    n_nodes, n_conns = len(g.node_keys()), len(g.conns)
    log(f"Read {n_nodes} nodes, {n_conns} connections")

    if rem_csv:
        rows = read_csv(rem_csv, 2)
        log(f"\nRemoving {len(rows)} node(s) from {os.path.basename(rem_csv)}")
        for name, typ in rows:
            g.remove_node(name, typ)

    if add_csv:
        rows = read_csv(add_csv, 4)
        log(f"\nAdding {len(rows)} node(s) from {os.path.basename(add_csv)}")
        for name, typ, prev, nxt in rows:
            g.add_node(name, typ, prev, nxt)

    log("\nChecks")
    g.check()
    g.write(gxy_out)
    log(f"\nNodes: {n_nodes} -> {len(g.node_keys())}   "
        f"Connections: {n_conns} -> {len(g.conns)}")
    log(f"Output: {gxy_out}")

    log_path = os.path.splitext(gxy_out)[0] + "_log.txt"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")
    print(f"Log   : {log_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as exc:  # keep the window open on double-click
        print(f"\nERROR: {exc}")
    if sys.stdin.isatty() and os.name == "nt":
        input("\nPress Enter to close...")
