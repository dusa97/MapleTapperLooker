# Graph Report - MapleTapperLooker  (2026-09-10)

## Corpus Check
- Corpus is ~5,502 words - fits in a single context window. You may not need a graph.

## Summary
- 85 nodes · 116 edges · 10 communities (8 shown, 2 thin omitted)
- Extraction: 81% EXTRACTED · 19% INFERRED · 0% AMBIGUOUS · INFERRED: 22 edges (avg confidence: 0.82)
- Token cost: 0 input · 152,846 output

## Community Hubs (Navigation)
- Overlay Window UI
- Region Selector UI
- Detection Template Assets
- Watch Loop & Hotkey Control
- Combat Power Delta Samples
- Detection Core & Sign Classification
- Dependencies
- Template Matching Engine
- Region Persistence
- Skill CD Template (Variant)

## God Nodes (most connected - your core abstractions)
1. `OverlayApp` - 15 edges
2. `RegionSelector` - 13 edges
3. `check_once()` - 7 edges
4. `watch()` - 6 edges
5. `_extract_template_from_ocr()` - 5 edges
6. `_preprocess()` - 5 edges
7. `_classify_sign()` - 4 edges
8. `_count_via_template()` - 4 edges
9. `+delata.png (Combat Power +538,683 screenshot)` - 4 edges
10. `Combat Power Change (-2,453,883) Screenshot` - 4 edges

## Surprising Connections (you probably didn't know these)
- `combat_power_template.png (auto-generated template image)` --semantically_similar_to--> `attack_power_template.png (UI Template Crop)`  [INFERRED] [semantically similar]
  combat_power_template.png → attack_power_template.png
- `combat_power_template.png (auto-generated template image)` --semantically_similar_to--> `skill_cd_template.png (cooldown text template crop)`  [INFERRED] [semantically similar]
  combat_power_template.png → skill_cd_template.png
- `+delata.png (Combat Power +538,683 screenshot)` --conceptually_related_to--> `Combat Power Change (-2,453,883) Screenshot`  [INFERRED]
  +delata.png → -delata.png

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Screen Capture & Automation Pipeline** — requirements_pyautogui, requirements_opencv_python, requirements_pillow, requirements_numpy [INFERRED 0.75]
- **OCR Text Recognition Pipeline** — requirements_pytesseract, requirements_pillow, requirements_numpy [INFERRED 0.75]

## Communities (10 total, 2 thin omitted)

### Community 0 - "Overlay Window UI"
Cohesion: 0.12
Nodes (8): OverlayApp, Runs entirely on the MAIN thread via root.after() polling. The watch loop runs…, Called from watch thread — just set a flag; UI update happens in after() tick., Called from watch thread — flips the border to a distinct grey while paused., Called from watch thread — replace the box's caught-text; UI syncs in after()…, Resize the window + canvas to match self.region, then redraw…, Called every 50 ms on the main thread to sync hit state → border colour +…, Start overlay window + launch watch loop in background thread, then enter…

### Community 2 - "Detection Template Assets"
Cohesion: 0.22
Nodes (10): Attack Power Stat (Game UI Readout), attack_power_template.png (UI Template Crop), Template Matching (OpenCV-style Game Automation), Auto-Built OpenCV Template Matching Mechanism, "Combat Power Change" UI Label, Combat Power Change Detection Profile, combat_power_template.png (auto-generated template image), skill_cd_template.png (cooldown text template crop) (+2 more)

### Community 3 - "Watch Loop & Hotkey Control"
Cohesion: 0.29
Nodes (7): beep(), _load_templates(), Load all template files for the active profile. Returns list of arrays., Detects if "Skill Cooldowns -2 sec" appears on 2 separate lines in a screen…, _toggle_pause(), watch(), _watch_fn()

### Community 4 - "Combat Power Delta Samples"
Cohesion: 0.39
Nodes (8): Brightness-Based Sign Classification Technique (main.py rationale), Combat Power Change UI Popup Concept, Combat Power Change (-2,453,883) Screenshot, Negative Delta / Combat Power Loss Concept, Brightness-Based Sign Classification Technique (gain vs loss), Combat Power Change UI Popup, +delata.png (Combat Power +538,683 screenshot), Positive Combat Power Delta / Gain (+538,683)

### Community 5 - "Detection Core & Sign Classification"
Cohesion: 0.32
Nodes (8): Image, check_once(), _classify_sign(), _extract_template_from_ocr(), _preprocess(), Run OCR on *img*, find the first line matching TARGET_TEXT, and crop out its…, Given the matched label's bbox (bx, by, bw, bh) in OCR_SCALE-upscaled pixel…, Upscale x2 + greyscale — matches what the template was built from.

### Community 6 - "Dependencies"
Cohesion: 0.40
Nodes (6): keyboard, NumPy, opencv-python, Pillow, PyAutoGUI, pytesseract

### Community 7 - "Template Matching Engine"
Cohesion: 0.50
Nodes (4): _count_via_template(), template can be a single array or a list — uses max score across all., _save_template(), ndarray

### Community 9 - "Skill CD Template (Variant)"
Cohesion: 0.67
Nodes (3): Cooldown countdown text overlay ("-2 sec"), Skill Cooldown Template Crop 2 ("-2 sec" text), Template-matching reference image asset

## Knowledge Gaps
- **8 isolated node(s):** `Attack Power Stat (Game UI Readout)`, `Template Matching (OpenCV-style Game Automation)`, `Skill Cooldown Indicator (in-game UI element)`, `OpenCV Template Matching (game automation technique)`, `Cooldown countdown text overlay ("-2 sec")` (+3 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 28 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `OverlayApp` connect `Overlay Window UI` to `Region Persistence`, `Watch Loop & Hotkey Control`?**
  _High betweenness centrality (0.256) - this node is a cross-community bridge._
- **Why does `RegionSelector` connect `Region Selector UI` to `Region Persistence`, `Watch Loop & Hotkey Control`?**
  _High betweenness centrality (0.170) - this node is a cross-community bridge._
- **Why does `_extract_template_from_ocr()` connect `Detection Core & Sign Classification` to `Watch Loop & Hotkey Control`, `Template Matching Engine`?**
  _High betweenness centrality (0.025) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `watch()` (e.g. with `beep()` and `_toggle_pause()`) actually correct?**
  _`watch()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Attack Power Stat (Game UI Readout)`, `Template Matching (OpenCV-style Game Automation)`, `Skill Cooldown Indicator (in-game UI element)` to the rest of the system?**
  _8 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Overlay Window UI` be split into smaller, more focused modules?**
  _Cohesion score 0.12105263157894737 - nodes in this community are weakly interconnected._