"""Schematic facts for a review: kicad-cli netlist + ERC boiled down to one JSON that is
small enough to read whole. No judgement here, only facts and mechanical red flags; the
review (Claude, a person) decides what matters.

    python sch_facts.py <board.kicad_pcb | root.kicad_sch> <out dir>

Writes facts.json, plus netlist.xml, erc.json and schematic.pdf from kicad-cli for detail,
and page<N>.png of the PDF when PyMuPDF is installed (for viewers that cannot open PDFs).
"""
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

# Counted, not listed: drawing tidiness, not circuit facts.
ERC_COUNT_ONLY = {"endpoint_off_grid"}
POWER = {"power_in", "power_out"}


def _fields(comp):
    return {f.get("name"): (f.text or "").strip() for f in comp.iter("field")}


def _title_blocks(design):
    out = []
    for s in design.iter("sheet"):
        tb = s.find("title_block")
        get = lambda k: (tb.findtext(k) or "").strip() if tb is not None else ""
        out.append({"path": s.get("name"), "file": get("source"), "title": get("title"),
                    "rev": get("rev"), "date": get("date"), "company": get("company")})
    return out


def _title_block_issues(sheets):
    if not sheets:
        return []
    root, issues = sheets[0], []
    for k in ("title", "rev", "date", "company"):
        if not root[k]:
            issues.append(f"root sheet has no {k}")
        issues += [f"{s['path']}: {k} {s[k]!r} differs from root {root[k]!r}"
                   for s in sheets[1:] if s[k] != root[k]]
    return issues


def _erc(erc):
    keep, counts, excluded = [], Counter(), 0
    for sheet in erc.get("sheets", []):
        for v in sheet.get("violations", []):
            if v.get("severity") == "exclusion" or v.get("excluded") is True:
                excluded += 1
                continue
            counts[f"{v.get('severity')}:{v.get('type')}"] += 1
            if v.get("type") not in ERC_COUNT_ONLY:
                keep.append({"severity": v.get("severity"), "type": v.get("type"),
                             "description": v.get("description"), "sheet": sheet.get("path"),
                             "items": [i.get("description") for i in v.get("items", [])]})
    keep.sort(key=lambda v: v["severity"] != "error")
    return {"counts": dict(counts.most_common()), "excluded": excluded, "violations": keep}


def facts(netlist_xml, erc):
    """kicad-cli kicadxml netlist text + ERC JSON dict -> facts dict."""
    root = ET.fromstring(netlist_xml)
    design = root.find("design")
    sheets = _title_blocks(design)

    parts = {}
    for c in root.find("components"):
        props = {p.get("name") for p in c.iter("property")}
        fields = {k: v for k, v in _fields(c).items()
                  if v and k not in ("Footprint", "Datasheet", "Description")}
        sheet = c.find("sheetpath")
        parts[c.get("ref")] = {
            "value": c.findtext("value", ""), "footprint": c.findtext("footprint", ""),
            "datasheet": _fields(c).get("Datasheet") or c.findtext("datasheet", ""),
            "description": c.findtext("description", "") or (
                c.find("libsource").get("description", "") if c.find("libsource") is not None else ""),
            "sheet": sheet.get("names") if sheet is not None else "",
            "dnp": "dnp" in props, "in_bom": "exclude_from_bom" not in props,
            "fields": fields, "pins": {}}

    nets, dangling, nc_on_net, nc_power, single, power = {}, [], [], [], [], []
    for n in root.find("nets"):
        name, nodes = n.get("name"), list(n)
        for nd in nodes:
            part = parts.get(nd.get("ref"))
            if part is not None:
                part["pins"][nd.get("pin")] = [nd.get("pinfunction", ""), name, nd.get("pintype", "")]
        flagged = [nd for nd in nodes if nd.get("pintype", "").endswith("no_connect")]
        pin = lambda nd: f"{nd.get('ref')}.{nd.get('pin')} {nd.get('pinfunction', '')}".rstrip()
        nc_power += [pin(nd) for nd in flagged if nd.get("pintype", "").split("+")[0] in POWER]
        if name.startswith("unconnected-"):
            # KiCad names every unwired pin's net this way; without a no-connect flag
            # the pin is simply floating.
            dangling += [pin(nd) for nd in nodes if nd not in flagged]
            continue
        nets[name] = [pin(nd) for nd in nodes]
        if len(nodes) > 1:
            nc_on_net += [f"{pin(nd)} on {name}" for nd in flagged]
        else:
            single.append(name)
        if any(nd.get("pintype", "").split("+")[0] in POWER for nd in nodes):
            power.append(name)

    mpn = lambda p: any(k.lower() in ("mpn", "manufacturer part number") for k in p["fields"])
    return {
        "source": design.findtext("source", ""), "tool": design.findtext("tool", ""),
        "sheets": sheets,
        "flags": {
            "title_block": _title_block_issues(sheets),
            "floating_pins": dangling,
            "no_connect_flag_on_connected_pin": nc_on_net,
            # Often fine (a second supply pin bonded inside), but the datasheet must say so.
            "no_connect_flag_on_power_pin": nc_power,
            "single_pin_nets": single,
            "fitted_parts_without_mpn": sorted(r for r, p in parts.items()
                                               if p["in_bom"] and not p["dnp"] and not mpn(p)),
        },
        "erc": _erc(erc),
        "power_nets": power,
        "parts": parts,
        "nets": nets,
    }


def dump(f):
    """JSON with one part / net per line: about half the size of indent=1, and every line
    stays readable on its own."""
    def block(k, v):
        if k in ("parts", "nets"):
            rows = ",\n".join(f"  {json.dumps(r)}: {json.dumps(x, ensure_ascii=False)}"
                              for r, x in v.items())
            return f" {json.dumps(k)}: {{\n{rows}\n }}"
        return f" {json.dumps(k)}: {json.dumps(v, indent=1, ensure_ascii=False)}"
    return "{\n" + ",\n".join(block(k, v) for k, v in f.items()) + "\n}\n"


def main(argv):
    src, out = Path(argv[0]).resolve(), Path(argv[1])
    sch = src.with_suffix(".kicad_sch")
    if not sch.exists():
        sys.exit(f"No schematic {sch.name} beside {src.name}")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prefab_gate"))
    from gate.kicad import locate_cli
    cli = locate_cli()
    out.mkdir(parents=True, exist_ok=True)
    for args in (["netlist", "--format", "kicadxml", "-o", out / "netlist.xml"],
                 ["erc", "--severity-all", "--format", "json", "-o", out / "erc.json"],
                 ["pdf", "-o", out / "schematic.pdf"]):
        cmd = [cli, "sch", *(["export"] if args[0] != "erc" else []), *args, sch]
        r = subprocess.run([str(a) for a in cmd], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"kicad-cli failed: {' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    f = facts((out / "netlist.xml").read_text(encoding="utf-8"),
              json.loads((out / "erc.json").read_text(encoding="utf-8")))
    (out / "facts.json").write_text(dump(f), encoding="utf-8")
    try:
        import fitz  # PyMuPDF, optional
        for i, page in enumerate(fitz.open(out / "schematic.pdf"), 1):
            page.get_pixmap(dpi=110).save(out / f"page{i}.png")
    except ImportError:
        pass
    fl = f["flags"]
    print(f"{len(f['parts'])} parts, {len(f['nets'])} nets, {len(f['power_nets'])} power nets; "
          f"ERC {sum(f['erc']['counts'].values())} ({f['erc']['excluded']} excluded); "
          + ", ".join(f"{k} {len(v)}" for k, v in fl.items()) + f"\n-> {out / 'facts.json'}")


if __name__ == "__main__":
    main(sys.argv[1:])
