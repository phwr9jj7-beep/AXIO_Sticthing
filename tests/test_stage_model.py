"""
Keyence tile step measured from the tiles, not from the scan-region corners (issue #12).

The synthetic dataset reproduces the real failure: the .bcf corner points imply an x-step 2 %
shorter than the true one, odd (serpentine) rows sit a few pixels off the even rows, and the
tiles really overlap at the true step.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest
import tifffile
from scipy import ndimage as ndi

from axio_stitching import stage_model
from axio_stitching.stage_model import StageModel, fit_stage_model, measure_stage_model, sample_chains
from axio_stitching.tile_sources import TileSourceError, resolve_tiles

TW, TH = 512, 384
SX, SY = 360, 270          # true steps (30 % overlap in x)
BX, BY = 2, 4              # odd-row offsets
ROWS, COLS = 3, 4
CAL_UM = 0.75488358        # um / px
EDGE_SX = SX * 0.98        # what the corner points imply: 2 % short


def _truth(r: int, c: int) -> tuple[int, int]:
    o = r % 2
    return c * SX + o * BX, r * SY + o * BY


def _write_bcf(path: Path, names: dict[tuple[int, int], str], edge_sx: float, edge_sy: float) -> None:
    cal_nm = CAL_UM * 1000.0
    cal_int = struct.unpack("<q", struct.pack("<d", cal_nm))[0]
    span_x = edge_sx * (COLS - 1) * cal_nm
    span_y = edge_sy * (ROWS - 1) * cal_nm
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("GroupFileProperty/Image/properties.xml",
                   f'<Store><Calibration Type="System.Double">{cal_int}</Calibration></Store>')
        z.writestr("GroupFileProperty/Image/OriginalImageSize/properties.xml",
                   f"<Store><Width>{TW}</Width><Height>{TH}</Height></Store>")
        z.writestr("GroupFileProperty/ImageJoint/properties.xml",
                   f"<Store><Row>{ROWS}</Row><Column>{COLS}</Column></Store>")
        pts = [(0, int(span_y)), (int(span_x), int(span_y)), (int(span_x), 0), (0, 0)]
        for i, (x, y) in enumerate(pts):
            z.writestr(f"GroupFileProperty/ImageJoint/EdgePoint{i}/properties.xml",
                       f"<Store><Enabled>True</Enabled><X>{x}</X><Y>{y}</Y><Z>0</Z></Store>")
        file_list = bytearray(struct.pack("<I", len(names)))
        for (r, c), fn in sorted(names.items(), key=lambda kv: kv[1]):
            rec = bytearray(58)
            rec[0] = 8
            rec[1:9] = b"Channel4"
            struct.pack_into("<i", rec, 17, r)
            struct.pack_into("<i", rec, 21, c)
            fb = fn.encode("latin1")
            rec[25] = len(fb)
            rec[26:26 + len(fb)] = fb
            file_list += rec
        z.writestr("GroupFileProperty/ImageList/FileList", bytes(file_list))


def make_keyence_dataset(root: Path, *, featureless: bool = False, edge_sx: float = EDGE_SX) -> Path:
    """Tiles cut from one textured plane at the TRUE positions, plus a .bcf with short corners."""
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(2026)
    h = (ROWS - 1) * SY + BY + TH + 8
    w = (COLS - 1) * SX + BX + TW + 8
    if featureless:
        plane = np.full((h, w), 3000.0)
    else:
        plane = ndi.gaussian_filter(rng.random((h, w)), 1.5) * 40000
        blobs = np.zeros((h, w))
        blobs[rng.integers(0, h, 400), rng.integers(0, w, 400)] = 1
        plane += ndi.gaussian_filter(blobs, 4) * 4e5
    names: dict[tuple[int, int], str] = {}
    k = 0
    for r in range(ROWS):
        cols = range(COLS) if r % 2 == 0 else reversed(range(COLS))   # serpentine order
        for c in cols:
            k += 1
            x, y = _truth(r, c)
            fn = f"KEY_{k:05d}_CH4.tif"
            tile = np.clip(plane[y:y + TH, x:x + TW], 0, 65535).astype(np.uint16)
            tifffile.imwrite(str(root / fn), tile)
            names[(r, c)] = fn
    bcf = root / "KEY.bcf"
    _write_bcf(bcf, names, edge_sx, SY)
    return bcf


@pytest.fixture()
def keyence_dataset(tmp_path: Path) -> Path:
    return make_keyence_dataset(tmp_path / "keyence")


class TestMeasuredLayout:
    def test_step_and_odd_row_offsets_are_measured(self, keyence_dataset):
        r = resolve_tiles(keyence_dataset)
        sm = r.stage_model
        assert sm["method"] == "measured"
        assert abs(sm["sx"] - SX) < 0.5
        assert abs(sm["sy"] - SY) < 0.5
        assert abs(sm["bx"] - BX) < 1.0
        assert abs(sm["by"] - BY) < 1.0

    def test_every_tile_lands_on_its_true_position(self, keyence_dataset):
        r = resolve_tiles(keyence_dataset)
        got = {t["filename"]: (t["x"], t["y"]) for t in r.scenes[0]}
        k = 0
        errors = []
        for row in range(ROWS):
            cols = range(COLS) if row % 2 == 0 else reversed(range(COLS))
            for c in cols:
                k += 1
                tx, ty = _truth(row, c)
                gx, gy = got[f"KEY_{k:05d}_CH4.tif"]
                errors.append(np.hypot(gx - tx, gy - ty))
        assert max(errors) < 1.5

    def test_the_corner_point_error_is_reported(self, keyence_dataset):
        r = resolve_tiles(keyence_dataset)
        assert any("corner points imply a tile step" in w for w in r.warnings)
        assert r.stage_model["edgepoint_error_fraction"][0] == pytest.approx(0.02, abs=0.003)
        assert any("tile step measured" in n for n in r.notes)

    def test_edgepoints_mode_reproduces_the_legacy_layout(self, keyence_dataset):
        r = resolve_tiles(keyence_dataset, keyence_step="edgepoints")
        assert r.stage_model["method"] == "edgepoints"
        xs = sorted({round(t["x"], 3) for t in r.scenes[0]})
        assert xs[1] == pytest.approx(EDGE_SX, abs=0.01)

    def test_overlap_mode_uses_the_overlap_fraction(self, keyence_dataset):
        r = resolve_tiles(keyence_dataset, keyence_step="overlap", overlap=0.25)
        assert r.stage_model["method"] == "overlap"
        assert sorted({t["x"] for t in r.scenes[0]})[1] == pytest.approx(TW * 0.75)

    def test_an_unknown_mode_is_rejected(self, keyence_dataset):
        with pytest.raises(TileSourceError, match="keyence_step"):
            resolve_tiles(keyence_dataset, keyence_step="guess")


class TestFallbacks:
    def test_featureless_tiles_fall_back_to_corners_with_a_warning(self, tmp_path):
        bcf = make_keyence_dataset(tmp_path / "flat", featureless=True)
        r = resolve_tiles(bcf)
        assert r.stage_model["method"] == "edgepoints"
        assert any("could not measure the tile step" in w for w in r.warnings)

    def test_measured_mode_refuses_to_fall_back(self, tmp_path):
        bcf = make_keyence_dataset(tmp_path / "flat2", featureless=True)
        with pytest.raises(TileSourceError, match="could not measure"):
            resolve_tiles(bcf, keyence_step="measured")

    def test_missing_tiles_fall_back(self, keyence_dataset):
        for tif in keyence_dataset.parent.glob("*.tif"):
            tif.unlink()
        r = resolve_tiles(keyence_dataset)
        assert r.stage_model["method"] == "edgepoints"
        assert any("could not measure" in w for w in r.warnings)


class TestFitAndSampling:
    def _pairs(self, sx=1330.0, sy=997.5, kx=0.3, ky=-0.1, bx=1.6, by=4.6, rows=range(0, 20), cols=range(0, 10)):
        m = StageModel(sx=sx, sy=sy, kx=kx, ky=ky, bx=bx, by=by)
        out = []
        for r in rows:
            for c in cols:
                x0, y0 = m.position(r, c)
                x1, y1 = m.position(r, c + 1)
                out.append(dict(r=r, c=c, r2=r, c2=c + 1, dx=x1 - x0, dy=y1 - y0, ncc=0.9))
                x2, y2 = m.position(r + 1, c)
                out.append(dict(r=r, c=c, r2=r + 1, c2=c, dx=x2 - x0, dy=y2 - y0, ncc=0.9))
        return out

    def test_fit_recovers_all_six_parameters(self):
        m, why = fit_stage_model(self._pairs())
        assert m is not None, why
        for name, want in dict(sx=1330.0, sy=997.5, kx=0.3, ky=-0.1, bx=1.6, by=4.6).items():
            assert getattr(m, name) == pytest.approx(want, abs=1e-6)

    def test_one_parity_does_not_degenerate(self):
        # Only even->odd vertical pairs (the reference implementation's first failure mode
        # was the mirror image: only odd rows). The fit must stay finite and absorb the
        # offset into the step rather than splitting it into +/- huge values.
        pairs = self._pairs(rows=[0, 2, 4, 6])
        m, _ = fit_stage_model(pairs)
        assert m is not None
        assert m.bx == 0.0 and m.by == 0.0
        assert m.sy == pytest.approx(997.5 + 4.6, abs=1e-6)

    def test_low_ncc_pairs_are_ignored_and_too_few_fail(self):
        pairs = [dict(p, ncc=0.1) for p in self._pairs()]
        m, why = fit_stage_model(pairs)
        assert m is None and "too few usable" in why

    def test_chains_cover_both_parities(self):
        grid = {(r, c): f"{r}_{c}" for r in range(73) for c in range(51)}
        chains = sample_chains(grid)
        horizontal_rows = {ch[0][0] for ch in chains if ch[0][0] == ch[-1][0]}
        assert {r % 2 for r in horizontal_rows} == {0, 1}
        vertical = [ch for ch in chains if ch[0][1] == ch[-1][1] and ch[0][0] != ch[-1][0]]
        covered = {(a[0], b[0]) for ch in vertical for a, b in zip(ch, ch[1:])}
        assert {r for r, _ in covered} == set(range(72))   # every row step is measured


class TestCache:
    def test_a_second_resolution_reuses_the_measurement(self, keyence_dataset, monkeypatch):
        first = resolve_tiles(keyence_dataset).stage_model

        def boom(*_a, **_k):
            raise AssertionError("tiles were measured again")

        monkeypatch.setattr(stage_model, "_offset", boom)
        second = resolve_tiles(keyence_dataset).stage_model
        assert second["method"] == "measured"
        assert second["sx"] == pytest.approx(first["sx"], abs=1e-3)

    def test_the_cache_can_be_disabled(self, keyence_dataset, monkeypatch):
        resolve_tiles(keyence_dataset)
        monkeypatch.setenv("AXIO_STITCHING_CACHE", "0")
        calls = []
        real = stage_model._offset

        def spy(*a, **k):
            calls.append(1)
            return real(*a, **k)

        monkeypatch.setattr(stage_model, "_offset", spy)
        resolve_tiles(keyence_dataset)
        assert calls

    def test_measure_stage_model_api(self, keyence_dataset):
        grid = {}
        k = 0
        for r in range(ROWS):
            for c in (range(COLS) if r % 2 == 0 else reversed(range(COLS))):
                k += 1
                grid[(r, c)] = f"KEY_{k:05d}_CH4.tif"
        m, why = measure_stage_model(grid, keyence_dataset.parent, (EDGE_SX, SY))
        assert m is not None, why
        assert m.residual_sd_x < 1.0 and m.residual_sd_y < 1.0
