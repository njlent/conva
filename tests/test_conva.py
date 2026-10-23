import os

import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as ocio
import pytest

from conva import cli, color, convert, sequence


def write_exr(path, px, dtype=oiio.HALF):
    h, w, c = px.shape
    buf = oiio.ImageBuf(oiio.ImageSpec(w, h, c, dtype))
    buf.set_pixels(oiio.ROI(0, w, 0, h, 0, 1, 0, c), px.astype(np.float32))
    assert buf.write(str(path))


def read(path):
    return oiio.ImageBuf(str(path)).get_pixels(oiio.FLOAT)


@pytest.fixture
def plate(tmp_path):
    px = np.zeros((8, 16, 4), np.float32)
    px[..., :3] = 0.18
    px[..., 3] = 1.0
    for f in (1001, 1002, 1003, 1005):
        write_exr(tmp_path / f"plate.{f}.exr", px)
    return tmp_path


# ---------------------------------------------------------------- sequences

@pytest.mark.parametrize(
    "path,head,pad,tail",
    [
        ("a/plate.####.exr", os.path.join("a", "plate."), 4, ".exr"),
        ("plate.%04d.exr", "plate.", 4, ".exr"),
        ("plate.%d.exr", "plate.", 1, ".exr"),
        ("plate_@@@.png", "plate_", 3, ".png"),
        ("plate.$F4.exr", "plate.", 4, ".exr"),
        ("v002/shot_v002.####.exr", os.path.join("v002", "shot_v002."), 4, ".exr"),
    ],
)
def test_parse_pattern(path, head, pad, tail):
    p = sequence.parse_pattern(path)
    assert (p.head, p.pad, p.tail) == (head, pad, tail)


def test_parse_pattern_none():
    assert sequence.parse_pattern("plate.1001.exr") is None


def test_frame_ranges():
    assert sequence.parse_frame_range("1-3") == [1, 2, 3]
    assert sequence.parse_frame_range("1001-1009x4") == [1001, 1005, 1009]
    assert sequence.parse_frame_range("1,5,10-12") == [1, 5, 10, 11, 12]
    assert sequence.parse_frame_range("1:5:2") == [1, 3, 5]
    with pytest.raises(ValueError):
        sequence.parse_frame_range("5-1")


def test_summarize():
    assert sequence.summarize_frames([1, 2, 3, 5, 7, 9, 20]) == "1-3, 5-9x2, 20"


def test_existing_frames(plate):
    assert sequence.parse_pattern(str(plate / "plate.####.exr")).existing_frames() == [1001, 1002, 1003, 1005]


# ---------------------------------------------------------------- color

@pytest.fixture(scope="module")
def cfg():
    return color.load_config(None)


def test_config_aliases():
    assert color.resolve_config_uri("aces1.3").startswith("ocio://studio-config-v2")
    assert color.load_config("aces1.3").getName().startswith("studio-config-v2")


def test_resolve_names(cfg):
    assert color.resolve_colorspace(cfg, "acescg") == "ACEScg"
    assert color.resolve_colorspace(cfg, "srgb_tx") == "sRGB Encoded Rec.709 (sRGB)"
    assert color.resolve_colorspace(cfg, "slog3 sgamut3 cine") == "S-Log3 S-Gamut3.Cine"
    with pytest.raises(color.ColorError, match="ACEScg"):
        color.resolve_colorspace(cfg, "acesgc")
    assert color.resolve_display(cfg, "srgb") == "sRGB - Display"
    assert color.resolve_view(cfg, "sRGB - Display", "sdr 100") == "ACES 2.0 - SDR 100 nits (Rec.709)"


def test_pipeline_matches_ocio(cfg):
    pipe, _ = color.build_pipeline(
        cfg, input_cs="acescg", output_cs="srgb_tx", display=None, view=None, looks=None, inv_display=None, inv_view=None
    )
    rgb = np.full((4, 4, 3), 0.18, np.float32)
    pipe.apply(rgb)
    ref = np.full((1, 3), 0.18, np.float32)
    cfg.getProcessor("ACEScg", "srgb_tx").getDefaultCPUProcessor().applyRGB(ref)
    assert np.allclose(rgb, ref[0], atol=1e-6)
    assert abs(rgb[0, 0, 1] - 0.46) < 0.02


def test_chunked_apply_matches(cfg):
    pipe, _ = color.build_pipeline(
        cfg, input_cs="acescg", output_cs=None, display="srgb", view=None, looks=None, inv_display=None, inv_view=None
    )
    rgb = np.random.default_rng(0).random((700, 900, 3), dtype=np.float32) * 4
    ref = rgb.copy()
    pipe._cpu.applyRGB(ref)
    pipe.apply(rgb)
    assert np.array_equal(rgb, ref)


def test_inverse_display_roundtrip(cfg):
    fwd, _ = color.build_pipeline(
        cfg, input_cs="acescg", output_cs=None, display="srgb", view=None, looks=None, inv_display=None, inv_view=None
    )
    inv, _ = color.build_pipeline(
        cfg, input_cs=None, output_cs="acescg", display=None, view=None, looks=None, inv_display="srgb", inv_view=None
    )
    rgb = np.array([[[0.18, 0.05, 0.3]]], np.float32)
    orig = rgb.copy()
    fwd.apply(rgb)
    inv.apply(rgb)
    assert np.allclose(rgb, orig, atol=1e-3)


# ---------------------------------------------------------------- end to end

def run(*args):
    return cli.main([str(a) for a in args])


def test_sequence_to_png(plate):
    rc = run("-v", "error", "-i", plate / "plate.####.exr", "-ics", "acescg", "-ocs", "srgb_tx", plate / "out" / "p.%04d.png")
    assert rc == 0
    assert sorted(os.listdir(plate / "out")) == ["p.1001.png", "p.1002.png", "p.1003.png", "p.1005.png"]
    px = read(plate / "out" / "p.1001.png")
    assert abs(px[0, 0, 0] - 0.46) < 0.02


def test_frames_and_renumber(plate):
    rc = run("-v", "error", "-i", plate / "plate.####.exr", "-frames", "1002-1005", "-renumber", "1",
             "-ics", "acescg", "-ocs", "srgb_tx", plate / "o" / "f.###.jpg")
    assert rc == 0
    # 1004 is missing, so 1002, 1003, 1005 -> 1, 2, 4
    assert sorted(os.listdir(plate / "o")) == ["f.001.jpg", "f.002.jpg", "f.004.jpg"]


def test_directory_output_and_format(plate):
    rc = run("-v", "error", "-i", plate / "plate.####.exr", "-ics", "acescg", "-ocs", "srgb_tx", "-f", "webp", str(plate / "web") + "/")
    assert rc == 0
    assert len(os.listdir(plate / "web")) == 4


def test_alpha_preserved(tmp_path):
    px = np.zeros((4, 4, 4), np.float32)
    px[..., :3] = 0.09  # premultiplied: color 0.18 at alpha 0.5
    px[..., 3] = 0.5
    write_exr(tmp_path / "a.exr", px)
    assert run("-v", "error", "-i", tmp_path / "a.exr", "-ics", "acescg", "-ocs", "srgb_tx", tmp_path / "a.png") == 0
    cfg_ = oiio.ImageSpec()
    cfg_.attribute("oiio:UnassociatedAlpha", 1)
    straight = oiio.ImageBuf(str(tmp_path / "a.png"), 0, 0, cfg_).get_pixels(oiio.FLOAT)
    assert abs(straight[0, 0, 3] - 0.5) < 0.01
    assert abs(straight[0, 0, 0] - 0.46) < 0.02  # converted from unpremultiplied 0.18


def test_alpha_dropped_for_jpeg(tmp_path):
    write_exr(tmp_path / "a.exr", np.ones((4, 4, 4), np.float32))
    assert run("-v", "error", "-i", tmp_path / "a.exr", "-ics", "acescg", "-ocs", "srgb_tx", tmp_path / "a.jpg") == 0
    assert oiio.ImageBuf(str(tmp_path / "a.jpg")).spec().nchannels == 3


def test_resize_and_depth(tmp_path):
    write_exr(tmp_path / "a.exr", np.full((10, 20, 3), 0.18, np.float32))
    assert run("-v", "error", "-i", tmp_path / "a.exr", "-ics", "acescg", "-ocs", "acescct", "-s", "10x-1", "-d", "16",
               tmp_path / "a.tif") == 0
    spec = oiio.ImageBuf(str(tmp_path / "a.tif")).spec()
    assert (spec.width, spec.height, str(spec.format)) == (10, 5, "uint16")


def test_grayscale_input(tmp_path):
    buf = oiio.ImageBuf(oiio.ImageSpec(4, 4, 1, oiio.UINT8))
    oiio.ImageBufAlgo.fill(buf, (0.5,))
    buf.write(str(tmp_path / "g.png"))
    assert run("-v", "error", "-i", tmp_path / "g.png", "-ics", "srgb_tx", "-ocs", "acescg", tmp_path / "g.exr") == 0
    assert oiio.ImageBuf(str(tmp_path / "g.exr")).spec().nchannels == 3


@pytest.mark.parametrize("ext", ["avif", "heic", "qoi", "webp", "jxl", "jp2", "dpx", "tga", "bmp", "hdr"])
def test_formats_roundtrip(tmp_path, ext):
    # (OpenJPEG can't write images much smaller than 32px)
    write_exr(tmp_path / "a.exr", np.full((32, 32, 3), 0.18, np.float32))
    assert run("-v", "error", "-i", tmp_path / "a.exr", "-ics", "acescg", "-ocs", "srgb_tx", tmp_path / f"a.{ext}") == 0
    assert run("-v", "error", "-i", tmp_path / f"a.{ext}", "-ics", "srgb_tx", "-ocs", "acescg", tmp_path / "back.exr") == 0
    assert np.allclose(read(tmp_path / "back.exr")[..., :3], 0.18, atol=0.02)


def test_overwrite_flags(tmp_path):
    write_exr(tmp_path / "a.exr", np.ones((2, 2, 3), np.float32))
    args = ["-v", "error", "-i", tmp_path / "a.exr", "-ics", "acescg", "-ocs", "srgb_tx", tmp_path / "a.png"]
    assert run(*args) == 0
    assert run(*args, "-n") == 0
    assert run(*args, "-y") == 0


def test_errors(tmp_path, plate):
    assert run("-v", "quiet", "-i", tmp_path / "missing.exr", "-ocs", "srgb", tmp_path / "x.png") == 1
    assert run("-v", "quiet", "-i", plate / "plate.####.exr", "-ics", "acescg", tmp_path / "single.png") == 1
    assert run("-v", "quiet", "-i", plate / "plate.1001.exr", "-ics", "nope", tmp_path / "x.png") == 1
    assert run("-v", "quiet", "-i", plate / "plate.1001.exr", "-ics", "acescg", tmp_path / "x.psd") == 1


def test_size_parsing():
    assert convert.parse_size("50%", 100, 50) == (50, 25)
    assert convert.parse_size("-1x10", 100, 50) == (20, 10)
    assert convert.parse_size("0.5", 100, 50) == (50, 25)
