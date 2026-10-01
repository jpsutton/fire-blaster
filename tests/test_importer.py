from fireblaster import importer
from fireblaster.profiles import ProfileSet

GOOD = "0000 0073 0000 0002 0020 0020 0040 0CC8"
GOOD_ALT = "0000 0073 0000 0002 0040 0020 0020 0CC8"


def entry(cid, brand, type_id, codes, confidence=6, projector=None):
    return {
        "configuration": {
            "id": cid,
            "projector": projector,
            "name": f"Code Group {cid}",
            "brand": {"name": brand},
            "deviceType": {"id": type_id},
            "codeSet": {
                "blastCount": 2,
                "confidence": {"irConfidence": confidence},
                "irCodes": [
                    {"code1": c1, "code2": c2, "deviceFunction": {"name": fn}} for fn, c1, c2 in codes
                ],
            },
        }
    }


DB = {
    "#1": entry(1, "LG", 1, [("VOLUME_UP", GOOD, ""), ("POWER_TOGGLE", GOOD, GOOD_ALT)]),
    "#2": entry(2, "Bose", 3, [("VOLUME_UP", GOOD, "")]),
    "#3": entry(3, "Junk", 1, [("VOLUME_UP", "not pronto", "")]),
    "#4": entry(4, "LG", 1, [("VOLUME_UP", GOOD, "")], projector=True),
}


def test_import_writes_loadable_profiles(tmp_path):
    stats = importer.import_db(DB, tmp_path, {"tv"}, None)
    assert stats["profiles written"] == 1
    assert stats["skipped (device type)"] == 2
    assert stats["entries without usable codes"] == 1
    assert stats["undecodable codes"] == 1

    path = tmp_path / "tv" / "lg-1.toml"
    assert path.exists()

    profiles = ProfileSet.load([tmp_path])
    p = profiles.get("amazon-1")
    assert p.brand == "LG" and p.device_type == "tv" and p.blast_count == 2 and p.confidence == 6
    assert p.codes["POWER_TOGGLE"] == (GOOD, GOOD_ALT)
    assert len(p.variants("POWER_TOGGLE")) == 2
    assert p.variants("VOLUME_UP")[0].carrier == 36045


def test_brand_filter(tmp_path):
    stats = importer.import_db(DB, tmp_path, None, {"bose"})
    assert stats["profiles written"] == 1
    assert (tmp_path / "avr" / "bose-2.toml").exists()


def test_projectors_split_from_tvs(tmp_path):
    importer.import_db(DB, tmp_path, {"projector"}, None)
    assert [p.name for p in (tmp_path / "projector").iterdir()] == ["lg-4.toml"]


def test_toml_escaping():
    text = importer.to_toml(
        {"id": "x", "brand": 'Q"uote', "name": "n\\m", "device_type": "tv", "confidence": 0,
         "blast_count": 1, "codes": {"ODD KEY": [GOOD]}, "source_id": 9}
    )
    import tomllib

    data = tomllib.loads(text)
    assert data["brand"] == 'Q"uote' and data["name"] == "n\\m"
    assert data["codes"]["ODD KEY"] == [GOOD]
