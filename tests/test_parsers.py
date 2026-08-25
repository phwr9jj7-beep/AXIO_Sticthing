"""test_parsers.py — XML parser unit tests."""

import pytest
from pathlib import Path
from axio_stitching.parsers import parse_info_xml, parse_zeiss_xml


class TestParseInfoXml:
    def test_returns_scenes_dict(self, info_xml_path):
        scenes = parse_info_xml(info_xml_path)
        assert isinstance(scenes, dict)
        assert len(scenes) == 2  # StartS=0 and StartS=1

    def test_scene_tile_count(self, info_xml_path):
        scenes = parse_info_xml(info_xml_path)
        assert len(scenes[0]) == 4
        assert len(scenes[1]) == 1

    def test_tile_has_required_fields(self, info_xml_path):
        scenes = parse_info_xml(info_xml_path)
        tile = scenes[0][0]
        assert "filename" in tile
        assert "x" in tile
        assert "y" in tile
        assert "w" in tile
        assert "h" in tile

    def test_tile_dimensions(self, info_xml_path):
        scenes = parse_info_xml(info_xml_path)
        tile = scenes[0][0]
        assert tile["w"] == 1020
        assert tile["h"] == 1020

    def test_empty_xml_returns_empty(self, tmp_path):
        import xml.etree.ElementTree as ET
        xml_path = tmp_path / "empty.xml"
        ET.ElementTree(ET.Element("ZoomImageDocument")).write(str(xml_path))
        result = parse_info_xml(xml_path)
        assert result == {}


class TestParseZeissXml:
    def test_info_xml_detected(self, info_xml_path):
        scenes, xml_type, scale = parse_zeiss_xml(info_xml_path)
        assert xml_type == "info"
        assert scale is None
        assert 0 in scenes

    def test_returns_typed_tiles(self, info_xml_path):
        scenes, _, _ = parse_zeiss_xml(info_xml_path)
        for tile in scenes[0]:
            assert isinstance(tile["x"], float)
            assert isinstance(tile["y"], float)
