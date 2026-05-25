# LLM-Wiki Schema Specification

This document defines the organization, conventions, and layout of the internal AROS `.wiki/` directory.

---

## 📂 Directory Structure

All files in the wiki must conform to the following hierarchy:

```
.wiki/
├── index.md            # Wiki Home & Table of Contents (Auto-updated)
├── overview.md         # Project description, datasets, and pipeline details
├── timeline.md         # Chronology of imaging runs, development, and decisions
├── log.md              # Daily operational log of actions
├── SCHEMA.md           # This schema definition file
└── system/
    └── lessons-learned.md  # Critical code, environment, and system-level insights
```

---

## ✍️ Formatting & Links

1. **Strict Wikilinks**: Use Obsidian-style double bracket wikilinks (e.g., `[[overview]]`) to link between wiki pages.
2. **File References**: Use absolute `file:///` markdown link notation to point to code files, scripts, or data outputs outside the wiki (e.g., `[run_pipeline.py](file:///e:/Oohashi3DWholeBrainProj/AXIO_Sticthing/scripts/run_pipeline.py)`).
3. **No Placeholders**: All documentation must be grounded in actual code or files on disk. Do not use generic filler text.
