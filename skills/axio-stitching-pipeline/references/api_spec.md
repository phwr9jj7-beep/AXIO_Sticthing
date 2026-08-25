# AXIO Stitching — MCP Tool API Reference

## axio_inspect_metadata

**Input Schema:**
```json
{
  "xml_path": {
    "type": "string",
    "description": "Absolute path to Zeiss _info.xml or _meta.xml"
  }
}
```

**Output Schema:**
```json
{
  "xml_path": "string (absolute path)",
  "xml_type": "string ('info' | 'meta')",
  "scenes": [
    {
      "scene_id": 0,
      "tiles": [
        {
          "filename": "string",
          "x": 0.0,
          "y": 0.0,
          "w": 1020,
          "h": 1020
        }
      ],
      "total_tiles": 732,
      "cols": null,
      "rows": null
    }
  ],
  "total_scenes": 1,
  "total_tiles": 732,
  "pixel_scale_um": null
}
```

---

## axio_stitch

**Input Schema:**
```json
{
  "xml_path": "string (required)",
  "out_dir": "string (required)",
  "correction": "string ('basicpy'|'median'|'spatial'|'none') default: 'basicpy'",
  "algorithm": "string ('phase'|'sift'|'coordinate') default: 'phase'",
  "scene": "integer|null default: null (all scenes)",
  "ref_channel": "integer default: 0",
  "ref_tag": "string default: ''",
  "target_tags": "string (comma-separated) default: ''",
  "alignment_mode": "string ('reference'|'average'|'max_projection') default: 'reference'",
  "z_mode": "string ('none'|'mip_align_3d'|'ref_slice_3d'|'mip_output_only') default: 'none'",
  "ref_z_slice": "integer default: 0"
}
```

**Output Schema (Success):**
```json
{
  "success": true,
  "output_paths": ["E:/output/stitched_scene0_phase.tif"],
  "preview_paths": ["E:/output/stitched_scene0_phase_preview.png"],
  "duration_seconds": 245.3,
  "scenes_processed": 1,
  "tiles_processed": 732,
  "error_message": null
}
```

**Output Schema (Failure):**
```json
{
  "success": false,
  "output_paths": [],
  "preview_paths": [],
  "duration_seconds": 0.0,
  "scenes_processed": 0,
  "tiles_processed": 0,
  "error_message": "Description of what went wrong"
}
```

---

## axio_validate_config

**Input Schema:**
```json
{
  "xml_path": "string (required)",
  "out_dir": "string (required)",
  "correction": "string default: 'basicpy'",
  "algorithm": "string default: 'phase'"
}
```

**Output Schema:**
```json
{
  "valid": true,
  "warnings": ["N tile files missing in raw data directory."],
  "errors": []
}
```

---

## axio_list_algorithms

**Input Schema:** (no parameters)

**Output Schema:**
```json
{
  "version": "1.1.0",
  "corrections": ["basicpy", "median", "spatial", "none"],
  "algorithms": ["phase", "sift", "coordinate"],
  "alignment_modes": ["reference", "average", "max_projection"],
  "z_modes": ["none", "mip_align_3d", "ref_slice_3d", "mip_output_only"]
}
```

---

## Performance Characteristics

| Dataset Size | Algorithm | Correction | Estimated Time |
|---|---|---|---|
| 100 tiles, 1 scene | phase | none | ~30 s |
| 100 tiles, 1 scene | phase | basicpy | ~3-5 min |
| 700 tiles, 1 scene | phase | basicpy | ~20-40 min |
| 700 tiles, 1 scene | phase | median | ~10-15 min |
| 700 tiles, 1 scene | coordinate | none | ~5 min (canvas only) |
| 3D, 700 tiles × 10Z | phase | basicpy | ~4-6 hrs |

*Estimates based on Zeiss 20x objective with 10% overlap on 1020×1020 tiles.*

---

## Error Codes

| Error String | Cause | Resolution |
|---|---|---|
| `BaSiCPy package is required` | basicpy not installed | `pip install basicpy` |
| `OpenCV not installed for SIFT` | cv2 missing | `pip install opencv-python` |
| `No tiles matched reference tag` | ref_tag not found in filenames | Check tag spelling |
| `XML file does not exist` | Invalid path | Verify absolute path |
| `N tile files missing` | Raw TIFFs not in XML directory | Ensure raw data present |
| `Could not find grid coordinates` | meta.xml parsing failure | Check _meta.xml format |

---

## Algorithm Decision Tree

```
Need stitching?
├─ Do you trust stage coordinates?
│   ├─ YES → coordinate (fastest, no registration)
│   └─ NO → Need feature matching?
│       ├─ LOW contrast images → sift (robust but slow)
│       └─ Normal fluorescence → phase (default, fast)
│
└─ Which correction?
    ├─ Best quality → basicpy (slow: fits 300 samples)
    ├─ Good balance → median (faster: 1D smooth)
    ├─ Uneven background → spatial (rolling ball)
    └─ Skip → none (raw tiles)
```
