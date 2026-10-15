---
name: schematic-review
description: Use to review a KiCad 10 schematic for design errors before layout or fab - per-IC datasheet checks (power pins, decoupling, pull-ups, enable/reset/boot straps, voltage levels between rails, absolute max, unused pins, crystals, LEDs, connectors, ESD), plus ERC, floating pins, no-connect flags and title-block consistency. Triggers on "review the schematic", "check my schematic", "schematic review", "design review", "is this schematic right", "anything wrong with the circuit", "sanity check before layout/fab".
---

# Schematic review

Advice only. Never edit the schematic during a review and never block a release
with it; fixes are a separate step the user asks for.

## 1. Facts

    python "${CLAUDE_PLUGIN_ROOT}/scripts/sch_facts.py" <board.kicad_pcb | root.kicad_sch> <out dir>

Use a temp folder for `<out dir>` (e.g. `%TEMP%\fab-release-review\<board>`),
not the project. It writes:

- `facts.json` - read this whole. `parts` (value, footprint, MPN fields,
  datasheet, DNP, in_bom, every pin as `[function, net, pin type]`), `nets`
  (`"REF.PIN FUNCTION"` lists), `power_nets` (nets with a power pin), `erc`
  (counts, excluded count, violations; off-grid only counted), `sheets`
  (title blocks), and `flags`: mechanical red flags already found.
- `schematic.pdf` and `page<N>.png` (when PyMuPDF is installed) - the drawing.
  Look at the pages: notes on the sheets (truth tables, "DNP for variant X")
  often explain what the netlist alone makes look wrong.
- `netlist.xml`, `erc.json` - raw kicad-cli output for detail.

Work from the netlist, not the look of the drawing: a wire that looks connected
may not be, and the netlist is what gets built.

## 2. Checks

Go part by part, ICs first (U, IC, Q, regulators, connectors), then passives on
their nets. Fitted parts only, but note a DNP part that looks required.

For each IC get its datasheet: the `datasheet` field, else search by MPN, else
by value. When the `datasheet-pdf` skill is available, use it to read pin
tables and electrical tables; do not trust search snippets for numbers. Quote
the page/table for every number you rely on. Note which ICs you could not
check and why.

Per IC, against its datasheet:

- **Power**: every supply and ground pin on the right rail, exposed pad
  included. Rail voltage inside the recommended range and well below
  absolute max. Regulators: input range vs the rail feeding it, output vs
  what it supplies, dropout margin, EN not floating.
- **Decoupling**: a cap on each supply pin, values near the datasheet's
  (bulk + 100 nF typical); regulator in/out caps meet the datasheet minimum
  and type.
- **Straps**: reset, enable, boot/mode, address and config pins tied as the
  datasheet requires; pull-ups on open-drain/I2C lines (one set per bus);
  no input left floating.
- **Levels**: every signal between two parts on different rails - output
  high of the driver vs input threshold and abs max of the receiver (3.3 V
  into 1.8 V, 5 V into 3.3 V). Name both rails.
- **Unused pins** handled as the datasheet says (NC vs tie to GND/VDD).
- **Clocks**: crystal load caps from the crystal's CL and stray estimate.
- **Pin function vs net name**: a pin wired to a net whose name does not fit
  (TX to TX, VBUS on a sense net, swapped D+/D-) is a likely bug. Polarity
  matters for USB, CAN, RS-485, MIPI, HDMI; it does not for most FPGA I/O.
- **Footprint vs part**: footprint name that does not fit the value or MPN.

Board level:

- Connectors to the outside: ESD/TVS, series protection, reverse polarity,
  fuse or current limit on power inputs, USB CC resistors.
- LEDs have a current limit; current within rating at the rail voltage.
- Every `flags` entry: explain it or mark it as fine. `floating_pins` and
  `no_connect_flag_on_connected_pin` are usually real.
- ERC errors: say what each means here. Group warnings by type; skip
  drawing tidiness unless asked.
- Title blocks: rev/date the same on every sheet and matching the board.
- Buses: if the design uses buses and the `kicad-verify-schematic-bus`
  skill is available, run its checker; buses fail silently.

## 3. Report

Write `<board>.schematic-review.md` beside the board (ask first if one exists;
it may hold the user's notes) and summarise it in chat.

```
# Schematic review: <board> <rev>, <date>
Source: <commit or "uncommitted">, KiCad <version>. Facts: <parts>/<nets>/<ERC count>.

## Likely bugs
- **U13 EN (pin 3)** on /LDO_EN floats when Q2 is off: datasheet p.5 says EN
  must not float. Fix: 100k to VBAT+.
## Check
- ...
## Nits
- ...
## Not checked
- U7: no datasheet found for MPN ...
```

Ranks: **Likely bug** (would misbehave or be damaged), **Check** (depends on
something the schematic cannot show, or the datasheet is ambiguous), **Nit**
(cost, readability, BOM data). Each finding: ref and pin, net, evidence,
suggested fix. No praise, no padding; a short report of real findings beats
a long one.
