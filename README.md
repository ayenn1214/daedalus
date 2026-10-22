# Daedalus

Native desktop parametric CAD with a AI modeling copilot (local Ollama or Claude).
Python · Qt (PySide6) · OpenCASCADE kernel (via build123d/OCP) · VTK viewport.

Formerly **Strata CAD**. Files save as `.daedalus`; old `.strata` files open as before. On first launch
Daedalus copies your Strata settings (API key, snaps, inventory location) and the inventory file
(`%APPDATA%\StrataCAD` → `%APPDATA%\Daedalus`) — the originals are left untouched.

## Run

Windows: install **Python 3.11** (64-bit; it sits alongside other versions), unzip, double-click **run_windows.bat**.
Python 3.11 is required on Windows: the sketch solver (python-solvespace) only ships Windows builds for 3.10/3.11,
and the CAD kernel/VTK don't have builds for 3.15 yet.
First run installs everything; later runs also pick up new packages after an update.

Manual:

```bash
py -3.11 -m venv .venv          # Windows; macOS/Linux: python3.11 -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
pip install -r requirements-optional.txt   # CoACD (exact physics shapes), pywin32 (SolidWorks) — optional
python -m daedalus                  # or: python -m daedalus part.daedalus
```

DWG files: install the free **ODA File Converter** (opendesign.com → Guest files); Daedalus finds it automatically.
DXF, PDF, SVG and STEP need nothing extra.

Copilot: click **API key** in the Copilot panel, or set `ANTHROPIC_API_KEY`.
Default model is `claude-opus-5-5` (change in the panel or `DAEDALUS_MODEL`).

Tests: `pip install pytest && pytest -q`

## Auto CAM + selection (v2.5)
- **Auto CAM** (Manufacture ▸ Auto CAM, CAM panel ▸ Auto CAM…, or ask the copilot): analyses the part —
  holes by size (through / blind / opening on the far side), pocket floors and steps, smallest inside corner,
  faces and side features the spindle can't reach — and recommends the side to machine from.
- Asks what it needs: side, controller, max rpm, G54 origin, units, stock allowance, the end mills, drills and
  face mill you have, which operations (face, pockets, spot, drill, tap, cut-out), drill cycle, G98/G99,
  coolant, program number (remembered).
- Builds the program: tools picked from your lists (pocket tool fits the inside corners, missing drill sizes
  → next smaller + ream/bore warning, taps for threaded holes), T-numbers in order of use, cycle time,
  warnings; **Second setup →** programs the flip side (only what setup 1 couldn't reach). Save G-code,
  Backplot, Quote.
- CAM: setup orientation (machine from any side), spot drilling, holes that open away from the spindle are
  never drilled from the wrong side.
- **Selection on the canvas**: Part · Face · Edge · Both toggle floats on the 3D view (also view bar, Edit
  menu, Ctrl+Shift+P/F/E/J). **Hover highlight** of the part, face or edge under the cursor (Edit ▸ Highlight
  under the cursor). "Both" picks an edge when you're on one, else the face.

## v2.4.3
- **Toolbars fit the window**: the ribbon and the view bar step down Full → Compact (views in a menu) → Icons
  only → scroll sideways as the window narrows. View ▸ Toolbar size: Auto / Full / Compact / Icons only.
  Pages, sketch and section bars no longer force the window to stay wide.
- **Which way is up?** after importing: pick the axis that was up in the source (Y-up preselected for
  SolidWorks / Fusion / Inventor… files, read from the STEP header), live preview, set on the floor,
  remembered per source program. Edit ▸ Which way is up… re-orients any time; Edit ▸ Ask … on import toggles it.

## v2.4.2
- STEP import: SolidWorks' instance ids (NAUO1, NAUO2…) and other exporter placeholders are no longer used as
  part names — parts take their product (file) name, numbered SolidWorks-style when repeated (Bracket-1, -2).

## v2.4.1 — canned cycles
- Drilling operations choose a canned cycle (CAM panel ▸ double-click an op / **Cycle…**, or the copilot):
  auto · G81 drill · G82 spot+dwell · G83 deep peck · G73 chip-break peck · G84 tap · G74 left tap · G85 ream ·
  G86 bore · G89 bore+dwell · G76 fine bore, with peck Q, dwell P, R clearance, G98/G99 retract, G76 shift.
- Auto: G83 when deeper than 3×Ø, else G81; G84 for taps; G82 for spot drills.
- Posts: Fanuc (P in ms, M29 rigid tap), Haas (P in s), LinuxCNC (G76 → G86), Siemens **MCALL CYCLE81–89**,
  GRBL writes the moves out (pecks, dwells; taps skipped with a note). G74 runs the spindle M4.
- Cycle times per cycle (pecks, retracts, dwells, tap in/out) feed CAM, quotes and the backplot.
- Backplot reads every cycle (G73/74/76/81–86/89, Q/P, G98/G99, G4) and Siemens MCALL CYCLE8x.

## SHOPQUOTE inside Daedalus (v2.4)
Manufacture tab ▸ **Quote** (Ctrl+Alt+Q, File ▸ Quote…, Home ▸ Quote / machine schedule). Fully native — no browser.
- **CNC machining** and **sheet / laser** quotes, process suggested from the shape (plate-like → sheet).
- Driven by the exact model: true volume and area, stock size with margin (material bought as stock), real
  cut length (every contour incl. holes) and pierce count, plate thickness, vertical holes. The CAM cycle time
  replaces the rate model when the part has CAM operations.
- Material from the model (density, price per kg); rates per material family; all parameters editable,
  "Save as defaults".
- Quantity breaks (1/5/10/25/100) with setup spread over the batch; copy as text, save CSV, log to history.
- **Machine schedule**: machines (type, $/h, hours/day), 14-day capacity calendar (booked / available, colour
  coded), bookings with computed finish dates, "Next free" start date; picking a machine uses its rate.
- **History** of quotes (export CSV). Data in `~/.daedalus/shop.json`.
- **G-code backplot**: Manufacture ▸ Backplot / File ▸ Simulate G-code file… — any .nc/.tap/.mpf: rapids red,
  feeds blue, arcs (I/J or R), G81/G83 cycles, inch programs; play / scrub with tool marker, time and distance
  stats; aligned to the part's CAM work offset when there is one.
- CAM: new **Siemens 840D/828D** post. Copilot tool `quote_part`.

## v2.3.4 — face & edge selection
- Select filter in the view bar: **Part · Face · Edge** (Ctrl+Shift+P / F / E, also Edit menu and right-click).
- Click selects a face or edge (Ctrl toggles, Shift adds), box-select works for faces/edges too (window /
  crossing); hover pre-highlights; hidden edges behind faces aren't picked.
- Status bar: count, total area (faces) or exact length (edges).
- **Fillet / Chamfer use the selected edges, Shell opens the selected faces** — recorded as `at(x,y,z)`
  selectors (the edge/face passing through that point), editable in the inspector.

## v2.3.3
- SolidWorks pull: STEP export tries Extension.SaveAs3 → Extension.SaveAs → typed IModelDoc2 → SaveAs3 → SaveAs
  (fixes "Member not found" on some SolidWorks versions); components read through IAssemblyDoc when needed;
  mates/properties are best-effort so geometry still comes across.
- Link errors name the API step that failed (file:line) for quick fixes. δ button / Ctrl+Shift+H restore the
  hotbar in the Link window. Example prompts insert verbatim.

## v2.3.2
- SolidWorks Link: fixed "TypeError: 'int' object is not callable" — pywin32's late binding runs zero-argument
  API methods on lookup (`doc.GetType` is already the number); every such call now works either way.
- Removed the start card from the empty 3D view. Default copilot model: qwen2.5:14b.

## v2.3.1 — CAD Link connection
- The Link window opens even when SolidWorks/Fusion isn't reachable yet and connects by itself once it is;
  reconnects automatically after SolidWorks restarts.
- Attaches through every registered `SldWorks.Application.N` ProgID and the Running Object Table.
- Status line says *why* it can't connect (not running · still starting · privilege mismatch · pywin32).
- **Diagnose** button: Python/pywin32, SLDWORKS.exe, elevation of both, ProgIDs, ROT, attach + API test — with
  fixes and a "Copy report" button. Fusion: link file, port, ping.
- Splash: oversized δ; Help ▸ Splash screen… editor (symbol, typeface, size, position, tilt, colours, own image).

## Viewing & selection (v2.3)

- **Display modes** (view bar, View ▸ Display mode, Alt+1…6): Shaded + edges, Shaded, Uncoloured, Hidden line,
  Wireframe, Translucent. Remembered between sessions.
- **Selection, SolidWorks-style by default**: click selects a part (Ctrl toggles, Shift adds), left-drag box
  selects — left→right selects parts fully inside (blue box), right→left selects anything touched (green box).
  Synced with the Objects list (Ctrl/Shift multi-select there too). Esc clears.
- **Mouse**: middle-drag rotate · Ctrl+middle pan · Shift+middle zoom · right-drag pan · right-click menu ·
  wheel zooms toward the cursor · double-click zooms to a part · Alt+left-drag rotates (laptops).
  View ▸ Mouse ▸ Classic restores left-drag rotate (Ctrl+left-drag box-selects).
- **View wheel** (Space in the view): radial picker — Top, Iso, Right, Back, Bottom, Front, Left, Normal to, Fit in
  the centre, display-mode chips underneath. Tap Space and click, or hold Space, flick and release; keys 1–8.
  Also a **View** ring in the δ hub. Views animate smoothly.
- Shortcuts: Ctrl+1…7 Front/Back/Left/Right/Top/Bottom/Iso, Ctrl+8 Normal to face, Ctrl+arrows rotate the view
  90°, Alt+← previous view, Shift+F zoom to selection.
- **Part tools**: Hide (H), Show all (Shift+H), Isolate (I), Delete (Del), Select all (Ctrl+A), Invert selection,
  Select all instances (Ctrl+Shift+A — e.g. every copy of one bolt in an imported assembly).
- **Right-click menu** on a part: zoom to, normal to face, hide, isolate, instances, orient, drop to floor, face
  down, material, delete. On empty space: views, display modes, fit, previous view, section, copy image.
- **Section view** (Ctrl+Shift+X): X/Y/Z cut plane with a slider and flip; cut faces shown tinted.
- File ▸ Save view as image / Copy view to clipboard (Ctrl+Shift+C).
- New splash screen.

## Large files, faster start-up, Home screen (v2.2)

- **Large STEP files**: assemblies are read with OCCT's XCAF reader in a background process (progress + Cancel,
  window stays responsive); repeated parts share one geometry. A 36 MB / 1,620-part STEP that ran out of memory
  now reads in ~12–18 s. Parts appear progressively, meshes are cached on disk (`~/.daedalus/meshcache`), and
  View ▸ Display quality ▸ Draft speeds up very big assemblies. Saved big assemblies open ~10× faster.
- **Start-up**: ~40% faster (lazy scikit-learn inside build123d, lean VTK modules, lighter Qt style), with a
  splash screen while the CAD kernel loads.
- **Home screen** (File ▸ Home, Ctrl+Shift+0, or the Home button above the view): New / Open / Open STEP,
  recent files as thumbnail cards with search, filters, pins, "Show in folder", missing-file detection;
  shortcuts to Std parts, Inventory, SolidWorks and Fusion Link.
- **QoL**: File ▸ Open Recent, autosave every 2 min with crash recovery on the Home screen, optional "Reopen the
  last file" at start-up, window size/position remembered.

## What works (v1.7)

| Area | Status |
|---|---|
| History | Ordered features, rollback bar, suppress, reorder, rename (refs follow), delete with dependency warning, per-feature error isolation |
| Features | Sketch (rect/circle/slot/polygon, add/subtract) · Extrude (join/cut/intersect, blind/through/mid-plane, draft) · Revolve · Hole (simple/counterbore/countersink) · Fillet · Chamfer · Shell · Linear & Circular pattern · Mirror |
| Sketcher | Draw lines (chained, auto horizontal/vertical), rectangles, circles, arcs; snap to points and existing body edges; relations (H, V, merge, point-on, parallel, perpendicular, equal, tangent, midpoint, symmetric, fix, construction); smart dimensions with draggable labels; drag geometry with live solving; DOF readout and colour state (copper = free, grey = defined, red = conflict); conflicting edits rejected; local undo |
| 3D Sketch | Lines, splines, 3-point arcs drawn in the 3D view on XY/YZ/XZ (Tab) or locked along X/Y/Z; auto along-axis within 3°; snaps to sketch points and body vertices; relations along X/Y/Z, perpendicular, equal, merge, point-on, midpoint, fix, construction; dimensions length, distance, ΔX/ΔY/ΔZ, angle; consistent redundant relations tolerated (closed 3D loops) |
| Frame | Square/rect/round tube, angle, channel, flat/square/round bar along every 3D-sketch curve; mitred corners along connected chains; junctions joined; cut list with centerline and long-point lengths |
| Sweep | 2D profile along a 3D or 2D sketch path, optionally auto-placed normal to the path start |
| Doodle → model | Draw a rough part on the Doodle page (mouse or pen, pressure-aware) or import a photo of a hand sketch. **Make sketch** (offline): strokes → lines, arcs, circles; ends joined across strokes; small gaps closed; near-H/V snapped; scaled to your size → editable constrained sketch + extrude, with a live copper preview while you draw. **Ask Claude**: sends the drawing + notes; Claude models it with features, then gets isometric/top renders of its result and fixes differences (one undo step for all of it) |
| Layers | Every feature lives on a layer; new ones go on the active layer (Layers panel or the Layer box above the view). Per layer: colour (faces drawn in it), 👁 visible (display only), 🔒 locked (no edits by you or Claude), ⏻ in model (off = left out of the rebuild). Move features via History right-click, the Inspector's Layer box, or *Move selected*. Saved in the file, undoable, and available to Claude (`add_layer`, `set_layer`, `move_to_layer`, `layer` on every add) |
| Objects & mates | Several **objects** (bodies) per file, each with its own feature history; new features go into the active object (Objects panel, or click a face). **Mates** (Assembly ▸ Mate, click two faces): coincident, distance, parallel, perpendicular, angle, concentric/hinge, lock — only types that fit the two faces are offered; over-defining mates are rejected. Mates store geometry, not face numbers, so they follow design changes. **Move** mode: drag an object and the mates carry connected ones along (a hinged arm swings, a slider slides); assembly DOF shown. **Drive**: sweep a distance/angle mate and watch the mechanism move; **Interference** check; per-object mass. Claude tools: `add_object`, `place_object`, `add_mate`, `drive_mate`, `check_interference` |
| Physics | **Assembly ▸ Simulate** / Physics tab (MuJoCo rigid-body engine): gravity, mass and inertia from each solid × material, contact and friction between parts, mates become real joints (slide / hinge / ball, worked out from whatever mates you used; four-bar style loops closed with constraints), unmated parts fall and tumble, fixed parts are ground. Motors on mates: speed (deg/s or mm/s), torque/force, servo to position; springs with damping. Exact collision shapes: parts are split along their own faces and around holes into convex pieces (cached), so a pin drops through a hole that a convex hull would plug. Background run, playback with scrubbing/speed/loop, traces (drive position/speed/torque, object speeds, kinetic energy, contacts), **Keep position** writes a frame back to the model (undoable). Claude: `set_physics`, `set_drive`, `run_simulation` |
| Materials | 65 standard materials with full engineering data (density, E, ν, yield, UTS, fatigue strength, conductivity, specific heat, expansion): carbon/alloy/structural steels (1018, 1020, 1045, 4140, 4340, A36, A572, A500, A513, 1095, A2), stainless (304, 316, 410, 17-4PH), cast & ductile iron, aluminium (1100, 2024, 3003, 5052, 6061, 6063, 7075, A356), copper/brass/bronze, titanium, magnesium, Inconel, zinc, 16 plastics (ABS, PLA, PETG, nylons, PC, acetal, HDPE, UHMW, PP, PTFE, PEEK, PVC, acrylic…), rubber, G-10, CFRP, woods, MDF, glass, alumina. Searchable picker by category; per-object materials; custom materials saved in the file. Used by mass, physics and every study |
| Simulation (FEA) | **Simulation** tab / Studies panel. Quadratic-tetrahedron FEA (gmsh mesh, scikit-fem). Pick faces in the view for fixtures (fixed, roller/symmetry, pin, elastic support) and loads (force, pressure, torque, bearing, gravity, rotation). Studies: **static** (von Mises, principal stresses, displacement, factor of safety, reaction forces, thermal stress from a thermal study or ΔT) · **frequency** (natural frequencies, animated mode shapes, free-free supported) · **buckling** (load factors, mode shapes) · **thermal** steady or transient (temperature, convection, heat flux/power, radiation, internal heat) · **fatigue** (S-N with Marin factors, Goodman/Gerber/Soderberg/ASME, reversed/zero-based/any R → life, damage, fatigue FoS) · **harmonic** (forced vibration by modal superposition). Contour plots with deformation scale and legend; results flagged out of date when the part or material changes. Checked against beam/column/torsion/conduction theory (1–6 %) |
| Flow (CFD) | Lattice-Boltzmann solver (numba, multi-core): **external** flow around the visible parts (drag, lift, Cd, wake) or **internal** flow through passages (pressure drop, flow rate). Air, water, oil, glycerin; laminar or LES above Re≈1000. Velocity/pressure slices you can move along X/Y/Z, streamlines, force history plot. Drag coefficients are estimates (expect ±15–25 % at default resolution; raise it to refine) |
| Reference & advanced features | **Plane** (offset, angled about x/y, on a picked face, 3 points) and **Axis** (hole/shaft, 2 points, 2 planes) features you sketch on / revolve and pattern about · **Loft** through sketches · **Rib** from an open line · **Draft** side faces · cosmetic **Thread** · **Hole wizard** (M3–M24, #4-40–3/4-10; tap / close / normal / loose; threads called out on drawings) |
| Sketch tools | Trim (T), Extend (E), Offset, Fillet (tangent + radius relations), Mirror (symmetric relations), Linear and Circular pattern |
| Sheet metal | Base flange (holes kept) + edge flanges with true cylindrical bends. **Gauge tables** (steel, galvanized, stainless, aluminium, metric); bend radius; **K-factor, bend allowance or bend deduction**; flange **position** (material inside/outside, bend outside, bend centreline) and **length reference** (outer / inner virtual sharp, tangent); any angle up or down; start/end offsets; **hems** (closed / open); **bend reliefs** (rectangular, obround) and **corner reliefs** (square, circular). **Fold/Unfold** toggles the part between folded and flat; **Edge flange** adds a flange to the edge face you click. Flat pattern with bend lines to DXF or a drawing |
| Weldments | Frame end caps, coped T-joints, corner gussets, weld counts in the cut list |
| Snapping & relations | **Sketch**: endpoints, midpoints, centres, quadrants, intersections, on-curve, perpendicular / tangent from the last point, H/V inference lines to other points, grid and angle steps — each snap adds the matching relation (midpoint, point-on, ⊥, H/V…) and drops it if it would over-define. 🧲 Snap menu to switch each type, grid spacing, angle step and automatic relations. New relations: concentric, coradial, collinear. **3D** (🧲 Snaps in the view bar and 3D-sketch bar, each type switchable): end points, centre points, midpoints, quadrants, edge × edge intersections, origin, face centres, centre of mass, bounding-box centre, perpendicular and tangent from the previous point, nearest on edge, on face (with its normal), edge extensions, along X/Y/Z, parallel to edges, aligned with points, plus length / angle increments, grid and snap distance. Used by 3D sketches (snaps add midpoint / on-line / ⊥ relations), **Move** (grab a corner or centre and drop it exactly on another part; Shift = free), **Measure** (live distance readout), and **SmartMates** (Alt-drag a face onto another part: planar → coincident, round → concentric). **Drawings** use the same 2D snaps |
| Production | **Parts library**: ISO/ANSI socket head, hex, flat head screws, nuts, washers, ball bearings — pick a hole and they are placed and mated · **BOM** with mass and cost, Excel/CSV export (BOM, cut list, flat patterns) · **linked part files** (STEP/.daedalus) that update when changed · **exploded view** · **gear** and **rack** mates (solver and physics) |
| 2D drawings | Drawing tab / Ctrl+D. Sheets A4–A0, Letter–ANSI E, ISO first-angle or ANSI third-angle; standard views at an automatic scale with hidden lines, centre marks, overall dimensions and hole/thread callouts; section (hatched) and detail views; flat-pattern drawings; balloons + parts list; title block. Drafting tools: line, polyline, rectangle, circle, arc, text, smart/diameter/radius/angle dimensions, leader notes, balloons, layers and line styles, snapping, move/delete, undo. **Options**: standard, units, decimals, text/arrow sizes, hatch, fonts, display toggles, DXF version |
| 2D files | Export PDF (vector, all sheets), DXF (layers, ISO linetypes, real DIMENSION entities, R12–R2018), SVG, PNG, DWG (through the free ODA File Converter when installed). **Open and edit** DXF, DWG (ODA), SVG and HPGL/PLT: layers, colours, linetypes, text, dimensions, hatches and blocks come in as editable entities. DWF is a closed Autodesk viewer format — use PDF instead |
| 3D export options | STEP **AP214** (default; names, colours, assemblies), **AP242** or AP203; mm or inch; mesh tolerance; ASCII/binary STL; one file per object; PLY and VRML added; **write STEP + IGES on every save** (Export opts). STEP is the recommended neutral format; IGES for legacy systems |
| CAM | Manufacture tab / CAM panel: stock from the part + margins, tool table, **facing**, **pocket** (every floor, islands kept, largest tool that fits), **outside profile**, **drilling** (peck G83) and **tapping** (G84) of every vertical hole; feeds/speeds from the material; toolpath preview in 3D; G-code for **Fanuc**, Haas, LinuxCNC/Mach and GRBL; cycle-time estimate |
| Manufacturability | Draft and undercut check against a pull direction, wall-thickness map, 3D-print overhang map with best build direction |
| More simulation | **Assembly static** (parts bonded at touching faces, per-part materials, **bolt connectors** with preload and bolt safety factor) · **Drop test** (implicit dynamics with floor contact: impact stress, g-load, contact time, animated) · **Topology optimisation** (SIMP; result becomes a new solid) · **Design study** (sweep any dimension; mass, stress, deflection, frequency… table, plot, CSV) |
| Images to Claude | 📎 attach any photo/drawing/screenshot in the Copilot panel; optional render check after building |
| Parametric dims | Sketch dimensions appear as live fields in the Inspector and drive the solid; Claude can change one with `set_dimension` |
| Sketch planes | XY/XZ/YZ + offset, or any planar side face of the body (top/bottom/front/back/left/right) |
| Inspector | Form generated from each feature's parameter spec; live rebuild |
| Viewport | Shaded/edges/wireframe/translucent, standard views, face picking (pick a face → Sketch/Hole uses it), triad |
| Evaluate | Mass properties with material library |
| Files | `.daedalus` (JSON history; imported geometry embedded). **Import** (File ▸ Import, Ctrl+I, or drag files onto the window): STEP assemblies → one object per part with names, colours, placement · IGES · BREP · STL/OBJ/3MF meshes → solids (flat faces merged, cavities kept) you can sketch on, cut and fillet · DXF → editable sketch (units, arcs, bulges, splines; stray lines become construction) · SolidWorks .sldprt/.sldasm (see below). **Export** (Ctrl+E): STEP assembly, IGES, BREP, STL, OBJ, 3MF (with colours), glTF/GLB, and any sketch as DXF (History right-click). Claude: `import_file`, `export_file` |
| Undo | Full snapshot undo/redo; one Claude prompt = one undo step |
| Standard parts | **Your own reusable parts library** (Assembly ▸ Std parts / Save std, File ▸ Standard parts library… Ctrl+L). Save any object (keeps its full feature history, so it stays editable) or add a STEP / IGES / BREP / STL / OBJ / 3MF / .daedalus file, with name, part number, category, kind, description, vendor, cost, tags and an auto thumbnail. Browse/search with previews; insert **linked** (edit the library part → every project updates on open / Links) or as an embedded copy, at a picked point. Saving under an existing part number makes a new version. **Adding a file reads its part info:** STEP product id/name/description, custom properties exported into STEP (Part Number, Description, Material, Vendor, Revision… from SolidWorks/Inventor/Creo), material (matched to the material library so mass is right), IGES product id/author, STL solid name, vendor file names (McMaster `91251A542_…`), and SolidWorks custom properties + material for .sldprt/.sldasm (read when SolidWorks converts the file; the bridge now writes `<name>.daedalus-props.json`). Everything is pre-filled for you to confirm. **Duplicate check on save:** every part gets a geometry fingerprint (volume, area, orientation-independent inertia shape, face-type mix, hole/shaft diameters) and saving compares it plus part number, vendor part number and name against the library — moved, rotated or mirrored copies score 100 %. Matches show in a *Similar in library* panel (identical / very similar / similar) with *Replace it* or *Use existing instead* (swaps the object for the library part at the same position); an identical match asks before saving a duplicate. The library browser shows similar parts per part and has *Find duplicates…*. Claude's save tools won't create an identical duplicate without your say-so. **Where used + inventory:** each part's *Used in* tab lists inventory projects (qty/unit × units = needed, also through weldments) with active production orders on top — status, due date, fab dates — then other projects and the Daedalus files that contain it (recorded on every save). The *Inventory* tab shows stock, bin, reorder point, on order and the shortfall for active projects, with Add to inventory / Link existing / Sync info / Open in inventory. Filters: In production, Used in projects, Low stock. Replacing a part that's in production names the projects and asks first. Inventory parts linked to a standard part show `[std]` and an *Insert in model* button; Send BOM links them automatically. Claude: `std_part_usage`. Standard parts carry their part number, description and cost into the BOM and Send BOM. Library is a folder — point it at a shared drive for the team (Library folder…). Claude: `std_library_search`, `insert_standard_part`, `save_standard_part`, `add_standard_part_file` |
| Inventory | **Holocron inventory, native** (toolbelt ▸ Inventory, File ▸ Inventory…, Ctrl+Shift+I). Dashboard, Projects (qty per unit × units to build, weldment expansion), Production Gantt, Locations (rack/shelf/bin serials), Parts (fasteners auto-named from spec, purchased, manufactured, weldments, 3 suppliers, CAD/USD), Vendors + on-time scorecard, Clients, Reorder (suggested qty, part orders, batch order/receive, Excel), Shortages across projects, Activity log with Undo, Import/Export (same Excel columns as the web app). **Send BOM** turns the open design into a project (library fasteners matched by spec, frames → weldments with cut-list members); BOM dialog shows stock, bin and shortfall per build; parts library shows stock. Data: one local JSON file (`%APPDATA%\Daedalus\holocron-inventory.json`) — point every PC at a shared-drive copy for a team; changes appear within seconds. Claude: `inventory_stock_check`, `send_bom_to_inventory`, `inventory_search`, `inventory_record` |
| Copilot | Claude edits the history through tools generated from the same feature registry; errors are fed back so it self-corrects |

## Themes (v1.7)

View ▸ Theme: **Graphite** (default) or **Black** — pure black panels, inputs, 3D view, sketcher, doodle paper
and drawing sheets (shown as black paper with light lines; exports and prints stay black on white), with every
colour kept: copper accent, red/green status, blue selection, coloured result plots, parts and layers.
Applies after a restart (offered when you switch); `DAEDALUS_THEME=black` forces it for one run.

## CAD Link: SolidWorks + Fusion (v2.1)
The Link works with **SolidWorks, Fusion, or both at once** (CAD selector at the top of the Link window). Everything in the SolidWorks Link section below — BOM & inventory, library & fasteners, copilot, δ hotbar, open in Daedalus — works the same on Fusion.

**Fusion setup (once):** `python -m daedalus --install-fusion-addin` (or CAD Link ▸ Install Fusion add-in…), then in Fusion: Utilities ▸ Add-Ins ▸ Scripts and Add-Ins ▸ Add-Ins ▸ **DaedalusLink** ▸ Run, tick *Run on Startup*. Start the Link with `python -m daedalus --fusion` or pick *Fusion* in the selector. The add-in listens on 127.0.0.1 only, with a per-session token (in `~/.daedalus/fusion-link.json`), and runs every request on Fusion's main thread.

Fusion specifics: part number and description are the component's own properties; HolocronId / DaedalusStdId and other custom properties are stored as component attributes (group *Daedalus*); library parts and fasteners are imported as STEP (no conversion step); mating to the selected hole/face creates a **joint** (revolute on a hole, rigid on a face); exports STEP / STL / F3D / IGES; command ids are Fusion's string ids; macros don't apply.

**Both connected:** *Send to Fusion / Send to SolidWorks* (Document tab, Analysis hotbar, or ask the copilot) moves the active document across as STEP with its properties — into the other side's open assembly when there is one, else as a new document. One standard library and one inventory serve both: a part saved to the library from either inserts into the other, and BOMs from both land in the same Holocron projects, matched by HolocronId.

Demo sessions: CAD Link ▸ SolidWorks / Fusion demo session (or `--sw --demo`, `--fusion --demo`).

The Fusion add-in was written against the Fusion API reference; its request plumbing is tested here against a mock Fusion API, but the first run inside Fusion is the real test — each failed request returns the exception of the API call involved.

## SolidWorks Link (v2.0)
Daedalus as an integration tool for SolidWorks. SolidWorks keeps the 3D modelling; Daedalus adds its subsystems on the open SolidWorks document. Daedalus still runs on its own as before.

**Start:** `python -m daedalus --sw` (SolidWorks running, `pip install pywin32`), or inside Daedalus: SolidWorks ▸ SolidWorks Link… (Ctrl+Shift+W). `--sw --demo` / SolidWorks ▸ …demo runs a simulated SolidWorks session (lift-cart assembly) so every feature can be tried without SolidWorks.

Link window (follows the active SolidWorks document):
- **Document** — custom properties table (edit → *Write to SolidWorks*), material, mass; Rebuild, Save, STEP, PDF; *Open in Daedalus*; *From Daedalus…*.
- **BOM & Inventory** — parts list rolled up by file + configuration, checked against Holocron stock for N builds (need, stock, bin, short). *Send BOM to inventory* creates/links parts, allocates them to a project and writes `HolocronId` (and a part number where missing) back to each SolidWorks part, so the next send matches the same parts.
- **Library** — Daedalus standard parts and the ISO/ASME fastener/bearing library inserted into the SolidWorks assembly; each is converted to a native .SLDPRT once and cached; with a hole/face selected in SolidWorks it is mated (concentric / coincident). *Save active part to library* stores a SolidWorks part as a standard part (properties carried over; the original .SLDPRT is reused when inserting it back into SolidWorks).
- **Copilot** — the Ollama (or Claude) copilot with SolidWorks tools: BOM, properties, fasteners, standard parts, inventory stock/send/search, views/rebuild/save/export/macros, open in Daedalus. It doesn't model geometry.
- **δ hotbar over SolidWorks** — stays on top of the SolidWorks window: Views, SolidWorks (rebuild, save, STEP/PDF, properties), Inventory, Library, Analysis (open in Daedalus → Simulation, Physics, CAM, Drawing; Daedalus → SolidWorks), Macros (a folder of .swp files, plus `swlink/commands` entries `Label=commandId`).
- **Round trip** — *Open in Daedalus* exports the active document from the running SolidWorks (STEP + mates + properties) and opens it in the Daedalus window (same process) on the page you picked. Native Daedalus: SolidWorks ▸ Pull active SolidWorks model / Send this part to SolidWorks.

Property names used: `PartNo` (also reads Part Number/PartNumber/Number), `Description`, `HolocronId`, `DaedalusStdId`, `Cost`.

The COM side was written against the documented SolidWorks API but could not be run here — the first session on a SolidWorks PC is the real test; failures are reported per action with the API call involved.

## Local AI copilot — Ollama (v1.12)
The copilot now runs on **Ollama** (local models, no API key, no cost) by default; Claude stays selectable.
1. Install Ollama (ollama.com) and pull a tool-calling model: `ollama pull qwen2.5:14b` (or qwen3:14b, qwen3:32b, llama3.1:8b, mistral-small). For drawings/photos also pull a vision model: `ollama pull gemma3:12b` (or qwen2.5vl, llama3.2-vision).
2. Copilot panel → *Ollama (local)* → pick the model. The gear button sets the server (default `http://localhost:11434`, or the `OLLAMA_HOST` env var), the context window (32k default) and the tool set.
- **Tool set:** *Core* (32 tools — sketch, extrude/revolve/sweep, holes, fillets, patterns, objects, mates, orient, materials, fasteners, import, BOM) is the default because small models get lost with 89 tools; *All* adds simulation, drawings, CAM and inventory.
- *Test connection* lists your models with their tools/vision support. Attaching an image to a non-vision model is caught before sending.
- Models that print tool calls as JSON text instead of real tool calls are still understood.
- Quality depends on the model: 14B+ models handle simple parts and edits; complex multi-feature parts work best on 32B-class models.

## Import hub (v1.11)
**Assembly ▸ Import** (File ▸ Import part…, Ctrl+Shift+M) — one search across the whole system:
- **Projects** — other Daedalus projects (recent files + *Project folders…*, one sub-folder deep).
- **Standard parts** — your saved standard library.
- **Inventory** — Holocron parts with CAD: a linked standard part, a fastener spec (maps to the ISO/ASME library, metric and inch), or a CAD file in the part's drawing link.
- **Fasteners & bearings** — the built-in library (search "shcs m8", "6204", …).
- **Browse file…** — any Daedalus / STEP / IGES / BREP / STL file.

Connected:
- *Keep linked* — the object re-reads its source when it changes (on open or Links).
- *Connect to the picked face* — flat face → the part sits on it (coincident); hole/shaft → concentric; fasteners go into the hole with nut/washer.
- Each object remembers where it came from: the design tree shows it (e.g. `inventory · linked`) with *Open source* / *Update from source*; Send BOM matches inventory imports to the exact same inventory part.
- Claude can do it too (`import_search`, `import_part`).
- File ▸ Import geometry into this part… (Ctrl+I) still adds a file's geometry as a feature of the current part.

## Object manipulation (v1.10)
Assembly tab (and the hub's Assembly toolbar):
- **Rotate** — drag an object to spin it about its centre: left/right about the view's up axis, up/down tips it. Shift = 15° steps on one axis.
- **Turntable** — rotate about the floor: drag left/right to turn about the vertical axis; the object stays on the floor. Shift = 15° steps.
- **Orient** — exact X/Y/Z angles, Reset, ±90° quick turns (optionally keeping the object on the floor), Drop to floor, Picked face down.
- **Drop** — lower the selected object onto z = 0. **Face down** — turn it so the clicked face points down, then set it on the floor.
- Each action is one undo step; mated objects follow. Also in the design tree (right-click an object) and for Claude (`orient_object`).

## Design tree (v1.9)
- The History panel is now a full design tree: document → Mates, Studies, material, Top/Front/Right planes, Origin, Center of mass, then the features (single part) or one node per object with its own *Mates in …*, material, origin and features.
- Feature icons match the toolbar; cuts get the scissors, layer colour tints the icon, errors are red, suppressed/rolled-back items are dimmed.
- Filter box at the top narrows the tree by name, type or summary.
- Double-click: material → material dialog, plane → look at it, Center of mass → mass properties, mate → Mates tab, study → Studies tab, sketch → edit, object → make active. Right-click keeps all the feature commands plus Expand/Collapse all.
- Expansion state is kept across rebuilds.

## Tool hub (v1.8)
- A draggable **δ** floats over the viewport. Click it to fan out a ring of categories (Model, Features, Assembly, Simulation, Evaluate, Manufacture, Drawing, Inventory).
- Click a category to open its toolbar. Toolbars are pinned: they follow the hub when you drag it and stay open until you close them (× or click the category again). Up to three at once: above-right, below-right, and a vertical one on the left; a fourth replaces the oldest. They flip sides near the viewport edge.
- Right-click the δ for collapse, close all, reset position, hide. View ▸ Tool hub (Ctrl+Shift+H) toggles it. Position, open toolbars and ring state are remembered.
- Hover a category for its name; open categories are copper. Toolbars have a header with the category icon, uniform buttons (8 per row, 10 per column when vertical), soft shadow and fade-in.
- The ribbon is unchanged; the hub runs the same actions.
- **Claude toggle** (v1.8.1): the *Claude* button at the right of the view bar, View ▸ Claude panel (Ctrl+Shift+K), or the hub's right-click menu shows/hides the Claude panel. Remembered between sessions.

## Performance (v1.7.2)
- Incremental rebuild: each feature's result is cached by a hash of everything before it, so only features from the edit point down re-run; an unchanged rebuild is ~1 ms.
- Face/layer attribution is computed lazily, only when a face is actually picked or coloured.
- The viewport reuses tessellation for unchanged bodies, and back-to-back redraws share one frame.
- Snapping is vectorised: one projection per object instead of one per point, and on-face hits use a cached per-body intersector (~0.1 ms instead of ~100 ms).
- Holes cut all positions in one boolean.

## Interface (v1.6)

Fusion-based dark theme that renders the same on Windows, Linux and macOS: Phosphor line icons on the toolbelt,
view bar and copilot (via `qtawesome`; text glyphs if it's missing), consistent inputs / arrows / checkboxes /
scrollbars / tables, primary buttons in copper, no stock icons on dialog buttons. The Design dock hides on the
Inventory, Drawing and Doodle pages; an empty document shows a start card in the 3D view; the copilot shows example
prompts as cards and your messages as blocks.

## Architecture

```
daedalus/
  core/features.py   Feature types. PARAMS spec drives UI form, validation, save, AND Claude tool schema
  core/sketch2d.py   Constrained sketch model, SolveSpace solver, DOF analysis, profile → faces
  core/sketch3d.py   3D sketch model, SolveSpace 3D solve with redundancy handling, path chains
  core/doodle.py     Freehand strokes → lines/arcs/circles → constrained sketch (no ML, offline)
  core/layers.py     Layer model + face→feature attribution for per-layer colour/visibility
  core/assembly.py   Objects, mates, geometric refs that survive rebuilds, LM pose solver, DOF, drive
  core/materials.py  Material library (65 standard materials) + custom materials
  core/features2.py  Reference planes/axes, loft, rib, draft, thread, sheet metal, gussets
  core/standards.py  ISO/ANSI fastener + bearing tables and models
  core/sketch_ops.py Trim, extend, offset, fillet, mirror, patterns
  sim/               FEA (mesh.py gmsh, fea.py solvers, fatigue.py), CFD (cfd.py LBM), studies.py,
                     advanced.py (assembly FEA, bolts, drop test, topology, design studies)
  drawing/           Drawing model, HLR views + auto annotations, Qt rendering (PDF/SVG/PNG), DXF/DWG/SVG/HPGL I/O
  manufacturing/     cam.py (2.5D toolpaths + posts), printability.py (draft, thickness, overhang)
  core/physics.py    MuJoCo model from objects/mates (null-space joints, loop closures), convex decomposition, drives
  core/selectors.py  Geometric edge/face selectors ("vertical&linear", "top") — survive upstream edits
  core/document.py   History, rebuild, undo, I/O, mass props (no Qt)
  io/formats.py      Importers/exporters (STEP/IGES/BREP/STL/OBJ/3MF/glTF/DXF)
  io/swbridge.py     SolidWorks over COM → STEP + mates; batch CLI
  stdparts/          Standard parts library: library.py (folder of part + info + thumbnail), metadata.py (file part info),
                     usage.py (where used, production, inventory link),
                     similarity.py (geometry fingerprints, duplicate check), thumbs.py
  inventory/         Holocron inventory: model.py (rules, import/export), store.py (local JSON file), cad.py (BOM sync)
  ai/tools.py        Tool schemas + executor over Document
  ai/copilot.py      Claude agent loop (no Qt)
  ui/sketcher.py     Interactive 2D sketch editor
  ui/sketch3d_editor.py  3D sketch editing inside the viewport
  ui/doodle.py       Doodle page (pen canvas, photo underlay, live clean-up preview)
  ui/layers.py       Layers panel
  ui/assembly_ui.py  Move/Mate modes, Objects + Mates panels, Drive dialog
  ui/                Main window, toolbelt, history, inspector, viewport, copilot panel
```

Adding a feature = one class in `features.py`. It appears in the Inspector, file format,
and Claude's toolset automatically.

## Sketcher keys

`S` select · `L` line · `R` rectangle · `C` circle · `A` arc · `D` dimension · `H`/`V` horizontal/vertical ·
`X` construction · `Del` delete · `F` fit · `Esc` cancel · `Ctrl+Z`/`Ctrl+Y` undo/redo · `Ctrl+Enter` finish.
Wheel zoom, middle-drag pan, drag points/lines, drag dimension labels, double-click a dimension to edit it.

## 3D sketch keys

`L` line · `P` spline (Enter/right-click finishes) · `A` arc (start, middle, end) · `S` select ·
`Tab` next drawing plane · `X`/`Y`/`Z` lock the next point along an axis (with a selection: add along-axis) ·
`Del` delete · `Esc` cancel · `Ctrl+Z`/`Ctrl+Y`. Middle-drag rotates, Shift/Ctrl+middle-drag pans,
right-click ends a chain, double-click a dimension to edit it, drag a point to move it on the active plane.

## Assembly tips

Model each part at its own origin in its own object (⬚ Object), then Assembly ▸ **Mate**: click a face on
each part and pick the mate. One object stays ⚓ fixed (ground). A hinge = concentric + a face mate (1 DOF);
a slider = coincident + parallel faces. **Move** (Esc to finish) drags with the mates enforced; middle-drag
still rotates the view. **Drive** needs a distance or angle mate. Double-click a mate to change its value.

## SolidWorks files

`.sldprt`/`.sldasm` are a closed format — nothing open-source reads them. Daedalus uses SolidWorks itself:

- **SolidWorks on this PC (Windows):** `pip install pywin32`, then just import the file. Daedalus drives SolidWorks
  in the background to write STEP + a mates file, and brings in every component *and its mates*
  (coincident, distance, parallel, perpendicular, angle, concentric, hinge, lock). Other mate types are listed as skipped.
- **SolidWorks on another PC:** there run `python -m daedalus.io.swbridge <file or folder>`; copy the `.step` and
  `.daedalus-mates.json` it writes next to the SolidWorks file, then import the SolidWorks file here.
- **No SolidWorks:** in SolidWorks, Save As ▸ STEP AP214 and import that (geometry, names, colours, placement; no mates).

The bridge is written to the documented SolidWorks API but hasn't been run against a real SolidWorks install yet —
report the message it prints if a file fails.

## Simulation tips

Set the material first (◈ Material). New study → **+ Fixture** / **+ Load** → click faces in the view → Add → **Run**.
A static study needs fixtures that stop all rigid motion. Start with the auto mesh; set a smaller element size in
Options… where stresses concentrate and re-run until the peak stops changing. Factor of safety uses yield
(ductile) or tensile strength (brittle). Fatigue reads a static study (option *source*): the static load is the
peak of the cycle. Flow: pick external/internal, fluid, speed and direction in Options…; first runs compile the
solver (~20 s once).

## Physics tips

Fix one object (⚓) as ground. Mates you'd use for motion are the joints: a hinge = concentric + a face mate;
a slider = two face mates. Put a **drive** on the mate you want to power (+ Drive in the Physics tab): a distance
or angle mate with a drive is released and becomes the motor axis. Parts that overlap at the start get pushed apart
violently — check Interference first. The first run of a new shape takes a moment to build its collision pieces;
after that it's cached. "Convex hulls" is faster but fills holes and pockets.

## Doodle tips

Draw outlines as one stroke or several — ends within ~12 px are joined. Holes: a quick closed loop.
Right-click a stroke to erase it, Ctrl+Z removes the last one. Set **Size** to the real longest side;
written dimensions in a photo or the notes field are used by Claude, not by the offline clean-up.
After **Make sketch**, double-click the sketch to dimension it to final sizes.

## Next milestones

1. ~~Interactive 2D sketcher with constraints & dimensions~~ (v0.2)
2. ~~3D sketch, sweep, frame members with cut list~~ (v0.3)
3. ~~Doodle / photo → model (offline clean-up + Claude vision with render check)~~ (v0.4)
3b. ~~Layers~~ (v0.5)
3c. ~~Multiple objects, mates, constrained motion~~ (v0.6)
3d. ~~Import/export: STEP assemblies, IGES, meshes, DXF, SolidWorks bridge~~ (v0.7)
3e. ~~Rigid-body physics: gravity, contacts, joints from mates, motors, springs~~ (v0.8)
3f. ~~Material library, FEA (static/modal/buckling/thermal/fatigue/harmonic), CFD~~ (v0.9)
3g. ~~Reference geometry, loft/rib/draft/thread, sketch tools, sheet metal, weldment finishing~~ (v1.0)
3h. ~~Parts library, hole wizard, BOM + Excel, linked parts, explode, gear/rack mates~~ (v1.0)
3i. ~~2D drawings, DXF/DWG/SVG editing, export options~~ (v1.0)
3j. ~~CAM 2.5D + G-code, manufacturability checks~~ (v1.0)
3k. ~~Assembly FEA + bolts, drop test, topology optimisation, design studies~~ (v1.0)
4. Next: cam mates, contact (no-penetration) FEA, nonlinear materials, 3D adaptive CAM, drawing GD&T symbols
5. Background rebuild thread + progress for heavy parts
6. Copilot: selection-aware prompts ("fillet these"); doodle in 3D for frame layouts

## Not SolidWorks

Workflow is familiar (history tree, feature-based modeling, standard views), but naming,
layout, visuals and file format are Daedalus's own. No SolidWorks code, icons, assets or
trade dress are used; don't add any.

## v2.9.0 — Units, measure, HSS builder

**Units** (Edit ▸ Units…, the status-bar switcher, or Evaluate ▸ Units). Geometry is still stored exactly; units only
change what you see and type.
- Unit systems:
  - SI: MMGS, mm·kg·N·MPa, MKS, CGS.
  - US customary: IPS (inch), fractional inch, FPS (foot).
  - Construction:
    - ft-in-fraction (architectural, `5'-6 1/4"`, slope as x/12, plf, psf, kip, yd³);
    - decimal feet (civil, grades in %);
    - metric construction (mm, m², m³, kN, kN/m, kPa).
- 30+ measurement types, each with its own unit. Among them:
  - length, angle, slope or pitch, area, volume (including board feet and yd³);
  - mass, density, mass per length;
  - force, moment, stress or pressure, line load, area load, stiffness;
  - velocity, acceleration, rpm, energy, power, temperature;
  - second moment of area, section modulus, mass moment of inertia, flow.
- Per-type overrides, decimals, fractional rounding (1/2 to 1/128), and a default for new documents.
- Every field accepts any unit or an expression: `1/2"`, `3' 4-1/2"`, `25 mm + 2`, `2 m`, `45°`, `4/12`, `72°F`.
  This covers the Inspector, sketch dimensions and the 3D sketch.
- Unit converter: type a value, see it in every unit, plus ft-in to 1/16 and 1/32.

**Measure** (Evaluate ▸ Measure opens the Measure panel). It has three modes:
- **Points:** distance, ΔX/ΔY/ΔZ, plan length, rise, slope or pitch.
- **Entities:** vertices, edges and faces.
  - One edge: length, radius, diameter, arc angle, chord, centre, slope.
  - One face: area, perimeter, surface type, radius, tilt.
  - Two picks: minimum, centre and maximum distance; normal distance; axis-to-axis distance; angle between lines or faces.
  - Three or more: total length or area, path length, enclosed area.
- **Body:** volume, area, mass, weight, centre of mass, bounding box, board feet.

Mass properties and the cut list now show values in your units, and the cut list includes weights.

**HSS builder** (Features ▸ HSS):
- Standard sizes, each with its own corner-radius rule:
  - AISC / A500 (`HSS4x4x1/4`, `HSS6.625x0.280`): design wall 0.93·t, nominal weight;
  - CSA G40.20/21 (`HSS 102x102x6.4`);
  - EN 10219 and EN 10210 (`SHS`/`RHS`/`CHS`).
- **Non-standard sections:** any B × H × t, or OD × t, with a corner radius of 2t, 1.5t, 2.5t or 3t, a custom radius, or sharp corners.
- **Section properties** (in your units): A, Ix, Iy, Sx, Sy, Zx, Zy, rx, ry, J, C, b/t, h/t, weight per length, perimeter.
  These match the AISC Manual to within 1–2 %.
- **Inserting a section:**
  - As a straight member (`hss` feature): length, direction, start point, section rotation, mitred ends and cap plates.
  - Or as a Frame along a 3D sketch.
  - Or to update the selected HSS or Frame feature.
- The Frame feature gains `standard`, `designation` and `corner_radius`; cut lists carry weight.

Icarus tools: `set_units`, `convert_units`, `measure`, `hss_sizes`, `add_hss`.

## v2.8.2 — Speed

| Operation | Before | After |
|---|---|---|
| Edit a parameter + redraw (120-solid part) | 537 ms | 85 ms |
| Edit a parameter (300-part model) | 1.34 s | 0.09 s |
| Redraw (300 parts) | 230 ms | 25 ms |
| Undo (300 parts) | 1.35 s | 0.10 s |
| Main window build | 0.50 s | 0.33 s |

- Validity checks and face counts cached per solid; booleans only touch nearby solids.
- Rebuild cache sized to the feature tree, so it no longer thrashes on big models.
- Meshing reuses each face's triangulation and only meshes faces that changed.
- Viewport skips unchanged objects; history and object trees rebuild only when they actually change.
- Ribbon tabs are built when first opened; icons render straight from the bundled font.
- The installer pre-compiles Python files so the first launch is faster.
- Most of the remaining start-up time is spent loading the CAD kernel libraries.

## v2.8.1 — Design Data (calculators + your handbooks)

Evaluate ▸ **Design data** (also Help menu). Calculators grouped like the Design Data Handbook chapters; forms are
generated from one spec, results show pass/fail notes, *Use the part's material* fills E, ν, Sy, Su, density, and
*Copy report* puts a text report on the clipboard.

| Chapter | Calculators |
|---|---|
| Stress analysis | Principal stresses / Mohr + four failure theories · bending + torsion + axial on a bar · Hertz contact · columns (Euler / Johnson / Rankine-Gordon) · impact stress |
| Working stresses | Fatigue (Marin factors; Soderberg, Goodman, Gerber, ASME-elliptic) · notch sensitivity (Neuber) · factor of safety (n = a·b·c, typical values) |
| Design of shafts | ASME code diameter (Km/Kt, keyway, hollow, axial load, twist check, preferred size) · critical speed (Dunkerley / Rayleigh) · torsion of rectangular bars |
| Keys, pins, cotters | Parallel key (ISO/DIN section, shear and crushing length) · socket-and-spigot cotter joint · knuckle joint |
| Gears | Spur (Lewis + Barth, Buckingham dynamic and wear, auto module) · helical geometry and forces, face-width rules · worm efficiency, self-locking, forces |
| Couplings, clutches, brakes | Rigid flange and muff couplings · plate and cone clutches · band, block brakes · brake energy / temperature rise |
| Appendix | Section properties · pressure cylinders (thin, Lamé, Clavarino, Barlow) · power ⇄ torque ⇄ speed |

**Reference library** — add your own handbooks (PDF or text). PDFs with text are read directly; scanned PDFs are
OCR'd when Tesseract is installed; an OCR'd .txt with `##### PDF page N` markers works too. Kept locally in
`~/.daedalus/reference`, searchable in the dialog. Icarus / Claude get `design_calc` and `search_reference` (and
cite document + page).

## v2.8 — configurations, subassemblies, curves, surfaces, GD&T, more mates and sheet metal

Closes the eight biggest gaps from the SOLIDWORKS cross-reference.

1. **Configurations + equations** — Model ▸ *Equations* / *Configs*. Global variables (`W = 25`), equations driving any
   parameter or sketch dimension (`Extrude1.depth = W * 2`, `Sketch1.D3 = Hole1.diameter + 1`; trig in degrees,
   `iif(cond, a, b)`). Configurations override parameters, suppression, variables, material and objects; a *Modify
   Configurations* grid; design table in/out (.xlsx / .csv). Driven values are greyed in the Inspector.
2. **Subassemblies + component patterns** — Assembly ▸ *Subassembly* groups the selected objects, rigid (moves as
   one) or flexible; nested; dissolve; save as its own file; tree in the Objects panel. *Pattern* ▸ linear / circular /
   mirror: instances share the seed's geometry, follow it when it moves, count in the BOM (mirrors as opposite-hand).
3. **Curves** — Helix (pitch / height / turns, taper, left-hand), spiral, curve through XYZ points, projected and
   composite curves; *Spring* (closed + ground ends); Thread ▸ `modelled` cuts the real 60° V (shafts and tapped holes);
   Sweep ▸ `frenet` for helical paths.
4. **Fillet / chamfer / extrude options** — variable-radius, face and full-round fillets; distance-distance,
   distance-angle and vertex chamfers; extrude up to vertex / surface / offset from surface / next, and two directions.
5. **Drawings** — GD&T feature control frames (14 characteristics, Ⓜ/Ⓛ, datums), datum symbols, surface-finish
   symbols, ordinate dimensions, revision table (title block REV follows), auxiliary views, broken views (dimensions
   keep the true length). All export to PDF / DXF.
6. **Mates** — tangent, limit distance / angle, width, symmetric, path, screw, cam, slot. The mate menu offers them by
   what you picked; width / symmetric ask for the extra faces. Solver now takes Gauss-Newton steps first (faster, no
   drift on large corrections).
7. **Sheet metal** — mitred corners (miter flange), sketched bend, convert a shelled solid to sheet metal (unfolds
   with K-factor bend deductions, folded ⇄ flat), forming tools (dimple, rib, louver, extruded hole).
8. **Surfaces** — new *Surfaces* tab: extruded, revolved, lofted, planar, offset, filled surfaces; delete face (heal
   or keep as surface); knit (→ solid); thicken. Drawn translucent.

Icarus / Claude get matching tools (`configuration`, `subassembly`, `component_pattern`, `add_revision`, …) and
`add_*` for every new feature.

## v2.7 — SolidWorks-style sketch entities and tools

Sketch toolbar buttons are flyouts. Click the arrow to pick a variant; the button then repeats the last one you picked.

| Flyout | Variants |
| --- | --- |
| Line | Line · Centerline · Midpoint line · Infinite line |
| Rectangle | Corner · Center · 3 point corner · 3 point center · Parallelogram |
| Slot | Straight · Centerpoint straight · 3 point arc · Centerpoint arc |
| Circle | Circle · Perimeter circle |
| Arc | Centerpoint · Tangent (from the end of a line or arc) · 3 point |
| Polygon | 3–40 sides; the click sets a vertex or the middle of a side |
| Ellipse | Ellipse · Partial ellipse |
| Conic | Parabola · Conic with a Rho value |
| Spline | Through points; double-click, Enter or right-click finishes, click the first point to close it |
| Point | |

Each shape adds the relations that keep it that shape. For example, slots get tangent arcs and equal ends, polygons get equal sides on a construction circle, and center rectangles get construction diagonals with the centre at their midpoint.

**Also new**
- **Text:** font, height, angle, bold and italic. `{PartNo}`, `{Description}`, `{Material}`, `{Name}`, `{File}` and `{Date}` are replaced with the document's values.
- **Equation driven curve:** explicit y(x) or parametric x(t), y(t), in radians, anchored at the origin.
- **Sketch tools:** chamfer, split, convert model edges, move / copy / rotate / scale, divide a curve into equal segments, and belt / chain around selected circles (shows the length).
- **Rapid Sketch** (Edit menu): click a flat face to start a sketch on it.
- New entities snap, export to DXF and work with mirror and patterns.
- New keys: P point, G slot, N spline, O polygon.

## v2.6.2 — choose the sketch plane

- **Sketch now asks where to draw.** The choices are the origin planes (Top XY, Front XZ, Right YZ), the part's flat sides (top, bottom, front, back, left, right), and any reference planes.
  - Each choice is previewed in the 3D view as a teal plane, with an arrow showing which way the sketch faces.
  - An offset (mm) moves the sketch plane along its normal. Double-click a choice to start sketching.
  - The face you clicked is pre-selected; otherwise the last origin plane you used.
- **Other ways to pick a plane:**
  - Right-click a face in the view ▸ *Sketch on this face*.
  - Right-click Top/Front/Right plane or a reference plane in the feature tree ▸ *Sketch on this plane*.
- **Turn the prompt off:** Edit ▸ *Ask for the sketch plane*, or untick "Ask every time" in the dialog. Sketch then uses the clicked face, else Top (XY).

## v2.6.1 — installer and setup check (Python + Icarus agent)

**install.bat** (double-click; PowerShell does the work in install.ps1)

1. Finds Python 3.11 64-bit. If it's missing, installs it (winget, else the python.org installer) for the current user only, without touching PATH. Microsoft Store Python is skipped.
2. Builds `.venv`, installs the requirements and optional packages, then checks them.
3. Adds Desktop and Start-menu shortcuts with the δ icon.
4. Sets up the **Icarus agent**: installs Ollama (winget, else OllamaSetup.exe) and starts it.
5. Picks a model from your RAM (24 GB+ → qwen2.5:14b, 12 GB+ → 7b, else 3b), downloads it, and tells Icarus to use it.

Options: `-NoAgent`, `-AgentOnly`, `-Model qwen2.5:14b`, `-NoShortcut`, `-Yes` (no questions), `-CheckOnly` (report only).
`run_windows.bat` offers to run the installer when Python 3.11 is missing.

**Setup check**

- `python -m daedalus --check` prints the report. Add `--python-only` or `--json` as needed. The exit code is 1 if something required is missing.
- **Help ▸ Setup check (Python + Icarus)…** shows the same report in the app. Its buttons install missing packages, install or start Ollama, and download the model with a progress bar. You can also pick the Icarus model there.
- Icarus links to the setup check when Ollama doesn't answer.

## v2.6 — speed, floor grid, sidebar islands, Icarus, more CAD links

**Speed**
- Anti-aliasing is FXAA by default instead of MSAA ×4, which roughly halves frame time on integrated graphics. Change it in View ▸ Performance.
- Big models drop edge lines and anti-aliasing while the view moves, then restore them on release. Toggle this in View ▸ Performance ▸ Simplify big models while the view moves.
- Hover highlighting waits for the cursor to settle, so nothing is picked while the mouse sweeps across the view.
- Picking uses cached cell locators.
- Wheel zoom and assembly drags draw one frame per event-loop pass.
- No renders while the view is hidden, so start-up is faster.
- The Ollama model list loads in the background and never freezes the window.
- View ▸ Performance ▸ Graphics info… shows the GPU, triangle count and frame time, and warns about software rendering (fix: Windows Graphics settings ▸ High performance for python.exe).

**Floor grid** (G, or View ▸ Floor grid)
- A see-through grid on the XY plane, spaced and sized to the model.
- X and Y axes are tinted red and green.
- It is never picked and never part of zoom-to-fit.

**Sidebar islands** (View ▸ Sidebar, or right-click any sidebar tab)
- Three tabbed islands; any panel can go in any island or be hidden.
- Presets: three islands, two, one tabbed, tree + properties.
- Islands can sit side by side, and tabs drag to reorder.
- Every split drags to any size, down to closed, and the sidebar and Icarus docks go down to 60 px wide.
- Layout and sizes are remembered.

**Icarus**
- The AI copilot is now called Icarus, with the δ mark in its header.
- The new logo is also used for the window/taskbar icon and the Home screen.

**CAD Link: Rhino, Inventor, Solid Edge**
- *Rhino 7/8*: CAD Link ▸ Install Rhino link… copies a script and puts the Rhino command on the clipboard. It uses the same JSON protocol as the Fusion add-in.
  - Supports document user text, objects and blocks as components, STEP / IGES / STL / 3DM / OBJ in and out, views, and commands.
  - Rhino has no mates.
- *Inventor* (COM) supports iProperties, occurrences, mass, STEP export/import, `.ipt` library parts, camera views and commands.
- *Solid Edge* (COM) supports properties, occurrences, STEP export/import, `.par` library parts, named views and commands.
- All three are in the Link window's selector, have Pull / Send in the CAD Link menu, and have Diagnose checks.
- Command-line flags: `--rhino`, `--inventor`, `--solidedge`.
- Untested against the real programs (no Windows or licences here).

## v2.5.1 — teal accent

- Accent is now **#0A3A36** everywhere orange was: primary buttons, checked states, tabs, checkboxes, hub, view wheel, Home, Quote, canvas pick.
- Lighter tints of the same hue are used where #0A3A36 would vanish on a dark background: `ACCENT_LINE` (borders, underlines, icons) and `ACCENT_TEXT` (highlighted text).
- 3D selection, hover pre-highlight and sketch lines are teal.

