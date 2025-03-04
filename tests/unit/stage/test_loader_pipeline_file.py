import os
from copy import deepcopy
from itertools import chain

import pytest

from dvc.dvcfile import PIPELINE_FILE, Dvcfile
from dvc.stage import PipelineStage, create_stage
from dvc.stage.loader import StageLoader
from dvc.stage.serialize import split_params_deps


@pytest.fixture
def stage_data():
    return {"cmd": "command", "deps": ["foo"], "outs": ["bar"]}


@pytest.fixture
def lock_data():
    return {
        "cmd": "command",
        "deps": [{"path": "foo", "md5": "foo_checksum"}],
        "outs": [{"path": "bar", "md5": "bar_checksum"}],
    }


def test_fill_from_lock_deps_outs(dvc, lock_data):
    stage = create_stage(
        PipelineStage, dvc, PIPELINE_FILE, deps=["foo"], outs=["bar"]
    )

    for item in chain(stage.deps, stage.outs):
        assert not item.checksum and not item.info

    StageLoader.fill_from_lock(stage, lock_data)

    assert stage.deps[0].info == {"md5": "foo_checksum"}
    assert stage.outs[0].info == {"md5": "bar_checksum"}


def test_fill_from_lock_params(dvc, lock_data):
    stage = create_stage(
        PipelineStage,
        dvc,
        PIPELINE_FILE,
        deps=["foo"],
        outs=["bar"],
        params=[
            "lorem",
            "lorem.ipsum",
            {"myparams.yaml": ["ipsum", "foobar"]},
        ],
    )
    lock_data["params"] = {
        "params.yaml": {
            "lorem": "lorem",
            "lorem.ipsum": ["i", "p", "s", "u", "m"],
        },
        "myparams.yaml": {
            # missing value in lock for `foobar` params
            "ipsum": "ipsum"
        },
    }
    params_deps = split_params_deps(stage)[0]
    assert set(params_deps[0].params) == {"lorem", "lorem.ipsum"}
    assert set(params_deps[1].params) == {"ipsum", "foobar"}
    assert not params_deps[0].info
    assert not params_deps[1].info

    StageLoader.fill_from_lock(stage, lock_data)
    assert params_deps[0].info == lock_data["params"]["params.yaml"]
    assert params_deps[1].info == lock_data["params"]["myparams.yaml"]


def test_fill_from_lock_missing_params_section(dvc, lock_data):
    stage = create_stage(
        PipelineStage,
        dvc,
        PIPELINE_FILE,
        deps=["foo"],
        outs=["bar"],
        params=["lorem", "lorem.ipsum", {"myparams.yaml": ["ipsum"]}],
    )
    params_deps = split_params_deps(stage)[0]
    StageLoader.fill_from_lock(stage, lock_data)
    assert not params_deps[0].info and not params_deps[1].info


def test_fill_from_lock_missing_checksums(dvc, lock_data):
    stage = create_stage(
        PipelineStage,
        dvc,
        PIPELINE_FILE,
        deps=["foo", "foo1"],
        outs=["bar", "bar1"],
    )

    StageLoader.fill_from_lock(stage, lock_data)

    assert stage.deps[0].info == {"md5": "foo_checksum"}
    assert stage.outs[0].info == {"md5": "bar_checksum"}
    assert not stage.deps[1].checksum and not stage.outs[1].checksum


def test_fill_from_lock_use_appropriate_checksum(dvc, lock_data):
    stage = create_stage(
        PipelineStage,
        dvc,
        PIPELINE_FILE,
        deps=["s3://dvc-temp/foo"],
        outs=["bar"],
    )
    lock_data["deps"] = [
        {"path": "s3://dvc-temp/foo", "md5": "high five", "etag": "e-tag"}
    ]
    StageLoader.fill_from_lock(stage, lock_data)
    assert stage.deps[0].checksum == "e-tag"
    assert stage.outs[0].checksum == "bar_checksum"


def test_fill_from_lock_with_missing_sections(dvc, lock_data):
    stage = create_stage(
        PipelineStage, dvc, PIPELINE_FILE, deps=["foo"], outs=["bar"]
    )
    lock = deepcopy(lock_data)
    del lock["deps"]
    StageLoader.fill_from_lock(stage, lock)
    assert not stage.deps[0].checksum
    assert stage.outs[0].checksum == "bar_checksum"

    lock = deepcopy(lock_data)
    del lock["outs"]
    StageLoader.fill_from_lock(stage, lock)
    assert stage.deps[0].checksum == "foo_checksum"
    assert not stage.outs[0].checksum


def test_fill_from_lock_empty_data(dvc):
    stage = create_stage(
        PipelineStage, dvc, PIPELINE_FILE, deps=["foo"], outs=["bar"]
    )
    StageLoader.fill_from_lock(stage, None)
    assert not stage.deps[0].checksum and not stage.outs[0].checksum
    StageLoader.fill_from_lock(stage, {})
    assert not stage.deps[0].checksum and not stage.outs[0].checksum


def test_load_stage(dvc, stage_data, lock_data):
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data, lock_data)

    assert stage.wdir == os.path.abspath(os.curdir)
    assert stage.name == "stage-1"
    assert stage.cmd == "command"
    assert stage.path == os.path.abspath(PIPELINE_FILE)
    assert stage.deps[0].def_path == "foo"
    assert stage.deps[0].checksum == "foo_checksum"
    assert stage.outs[0].def_path == "bar"
    assert stage.outs[0].checksum == "bar_checksum"


def test_load_stage_outs_with_flags(dvc, stage_data, lock_data):
    stage_data["outs"] = [{"foo": {"cache": False}}]
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data, lock_data)
    assert stage.outs[0].use_cache is False


def test_load_stage_no_lock(dvc, stage_data):
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data)
    assert stage.deps[0].def_path == "foo" and stage.outs[0].def_path == "bar"
    assert not stage.deps[0].checksum
    assert not stage.outs[0].checksum


def test_load_stage_with_params(dvc, stage_data, lock_data):
    lock_data["params"] = {"params.yaml": {"lorem": "ipsum"}}
    stage_data["params"] = ["lorem"]
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data, lock_data)

    params, deps = split_params_deps(stage)
    assert deps[0].def_path == "foo" and stage.outs[0].def_path == "bar"
    assert params[0].def_path == "params.yaml"
    assert params[0].info == {"lorem": "ipsum"}
    assert deps[0].checksum == "foo_checksum"
    assert stage.outs[0].checksum == "bar_checksum"


@pytest.mark.parametrize("typ", ["metrics", "plots"])
def test_load_stage_with_metrics_and_plots(dvc, stage_data, lock_data, typ):
    stage_data[typ] = stage_data.pop("outs")
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data, lock_data)

    assert stage.outs[0].def_path == "bar"
    assert stage.outs[0].checksum == "bar_checksum"


def test_load_changed_command(dvc, stage_data, lock_data):
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data)
    assert not stage.cmd_changed
    assert stage.cmd == "command"

    lock_data["cmd"] = "different-command"
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data, lock_data)
    assert stage.cmd_changed
    assert stage.cmd == "command"


def test_load_stage_wdir_and_path_correctly(dvc, stage_data, lock_data):
    stage_data["wdir"] = "dir"
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    stage = StageLoader.load_stage(dvcfile, "stage-1", stage_data, lock_data)

    assert stage.wdir == os.path.abspath("dir")
    assert stage.path == os.path.abspath(PIPELINE_FILE)


def test_load_stage_mapping(dvc, stage_data, lock_data):
    dvcfile = Dvcfile(dvc, PIPELINE_FILE)
    loader = StageLoader(dvcfile, {"stage": stage_data}, {"stage": lock_data})
    assert len(loader) == 1
    assert "stage" in loader
    assert "stage1" not in loader
    assert loader.keys() == {"stage"}
    assert isinstance(loader["stage"], PipelineStage)
