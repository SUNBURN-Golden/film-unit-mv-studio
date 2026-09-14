from engine.builds import allocate_build, list_builds
from engine.core import write


def test_builds_are_sorted_numerically_after_four_digits(tmp_path):
    for name in ["B0001", "B0100", "B9999", "B10000", "B10001",
                 "B12", "Bnot-a-build", "B1234-saved"]:
        write(tmp_path / "builds" / name / "build.json", {"build_id": name})
    (tmp_path / "builds/B20000").mkdir()
    assert [row["build_id"] for row in list_builds(tmp_path)] == [
        "B10001", "B10000", "B9999", "B0100", "B0001"]


def test_new_build_after_b9999_is_listed_first(tmp_path):
    write(tmp_path / "builds/B9999/build.json", {"build_id": "B9999"})
    created = allocate_build(tmp_path, "preview")
    assert created.name == "B10000"
    assert [row["build_id"] for row in list_builds(tmp_path)] == ["B10000", "B9999"]


def test_missing_build_directory_lists_empty(tmp_path):
    assert list_builds(tmp_path) == []
