import unittest
import math
from dataclasses import FrozenInstanceError
from pathlib import Path

from borasuki.profile import ReferenceProfile, build_encode_args, validate_output_path, validate_output_tracks, cugan_tile


class ReferenceProfileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd()
        self.profile = ReferenceProfile()
        self.ffmpeg = self.root / "runtime" / "ffmpeg.exe"
        self.source = self.root / "Anime bölüm 01 & özel.mp4"
        self.output = self.root / "Anime bölüm 01 2x.mkv"

    def test_user_reference_parameters(self):
        self.assertEqual(self.profile.cugan_options(), {
            "noise": 1, "scale": 2, "tilesize": [480, 270],
        })
        self.assertEqual(self.profile.tensorrt_options(), {"fp16": True, "device_id": 0})
        self.assertEqual(self.profile.contrast, 1.03)

    def test_1080p_grid_includes_overlap_without_an_extra_row_or_column(self):
        tile = cugan_tile(1920, 1080, 1500)
        count = lambda sizes: math.prod(math.ceil((length - 8) / (size - 8))
                                       for length, size in zip((1920, 1080), sizes))
        self.assertEqual(tile, [486, 276])
        self.assertEqual(count([480, 270]), 25)
        self.assertEqual(count(tile), 16)
        self.assertTrue(all(size % 2 == 0 for size in tile))

    def test_tile_change_preserves_low_vram_and_other_resolution_policy(self):
        self.assertEqual(cugan_tile(1920, 1080, 1499), [240, 136])
        self.assertEqual(cugan_tile(1080, 1920, 1499), [240, 136])
        for width, height in [(1280, 720), (3840, 2160), (480, 270)]:
            self.assertEqual(cugan_tile(width, height, 4000), [480, 270])

    def test_portrait_1080p_grid_has_sixteen_tiles_without_increasing_tile_area(self):
        tile = cugan_tile(1080, 1920, 1500)
        count = lambda sizes: math.prod(math.ceil((length - 8) / (size - 8))
                                       for length, size in zip((1080, 1920), sizes))
        self.assertEqual(tile, [276, 486])
        self.assertEqual(count([480, 270]), 24)
        self.assertEqual(count(tile), 16)
        self.assertEqual(math.prod(tile), math.prod(cugan_tile(1920, 1080, 1500)))
        self.assertTrue(all(size % 2 == 0 for size in tile))

    def test_gpu_selection_reaches_backend(self):
        self.assertEqual(ReferenceProfile(gpu_id=2).tensorrt_options()["device_id"], 2)

    def test_profile_cannot_be_changed_mid_job(self):
        with self.assertRaises(FrozenInstanceError):
            self.profile.gpu_id = 2

    def test_4x_cannot_be_requested(self):
        with self.assertRaises(TypeError):
            ReferenceProfile(scale=4)

    def test_invalid_gpu_and_tiles(self):
        for field in ("gpu_id", "tile_width", "tile_height"):
            for value in (-1, True, 1.5, "1", None):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    ReferenceProfile(**{field: value})
        for field in ("tile_width", "tile_height"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ReferenceProfile(**{field: 0})

    def test_options_do_not_share_mutable_tile_list(self):
        options = self.profile.cugan_options()
        options["tilesize"][0] = 1
        self.assertEqual(self.profile.cugan_options()["tilesize"], [480, 270])

    def test_encode_preserves_reference_quality_and_optional_streams(self):
        args = build_encode_args(self.ffmpeg, self.source, self.output, self.profile)
        pairs = list(zip(args, args[1:]))
        for pair in (
            ("-c:v", "libx265"), ("-preset", "slow"), ("-crf", "15"),
            ("-pix_fmt", "yuv420p10le"), ("-map", "0:v:0"),
            ("-map", "1:a?"), ("-map", "1:s?"),
            ("-c:a", "copy"), ("-c:s", "copy"),
            ("-x265-params", "no-sao=1:aq-mode=3:qcomp=0.70:psy-rd=1.6:psy-rdoq=2.0:bframes=8"),
        ):
            self.assertIn(pair, pairs)
        self.assertNotIn("-r", args)

    def test_unicode_and_shell_characters_are_one_argument(self):
        args = build_encode_args(self.ffmpeg, self.source, self.output, self.profile)
        self.assertEqual(args[0], str(self.ffmpeg))
        self.assertEqual(args[-1], str(self.output))
        self.assertIn(str(self.source), args)
        self.assertNotIn("|", args)

    def test_existing_output_is_never_silently_overwritten(self):
        args = build_encode_args(self.ffmpeg, self.source, self.output, self.profile)
        self.assertIn("-n", args)
        self.assertNotIn("-y", args)

    def test_output_cannot_equal_input(self):
        with self.assertRaises(ValueError):
            build_encode_args(self.ffmpeg, self.output, self.output, self.profile)

    def test_case_only_difference_cannot_overwrite_input_on_windows(self):
        with self.assertRaises(ValueError):
            build_encode_args(self.ffmpeg, self.root / "SAME.mkv", self.root / "same.mkv", self.profile)

    def test_absolute_executable_and_input_required(self):
        for ffmpeg, source in ((Path("ffmpeg.exe"), self.source), (self.ffmpeg, Path("in.mp4"))):
            with self.subTest(ffmpeg=ffmpeg, source=source), self.assertRaises(ValueError):
                build_encode_args(ffmpeg, source, self.output, self.profile)

    def test_invalid_output_names(self):
        for name in ("CON.mkv", "nul.MKV", "COM1.mkv", "LPT².mkv", "bad?.mkv",
                     "bad\x00.mkv", "bad\n.mkv", "name.mkv ", "name.avi", 'bad".mkv'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_output_path(self.root / name)
        with self.assertRaises(ValueError):
            validate_output_path(Path("relative.mkv"))

    def test_invalid_parent_components(self):
        for parent in ("NUL", "trailing.", "trailing ", "..", "bad:directory"):
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                validate_output_path(self.root / parent / "out.mkv")

    def test_valid_unicode_output(self):
        validate_output_path(self.output)
        validate_output_path(self.root / 'Anime bölüm 01 2x.mp4')

    def test_mp4_requires_explicit_track_loss_confirmation(self):
        output = self.root / 'video.mp4'
        media = {'streams':['audio', 'subtitle', 'attachment'], 'track_codecs':[
            {'type':'audio', 'codec':'aac'}, {'type':'subtitle', 'codec':'ass'},
            {'type':'attachment', 'codec':'ttf'}]}
        with self.assertRaisesRegex(ValueError, 'error.mp4_confirm_required'):
            validate_output_tracks(output, media)
        validate_output_tracks(output, media, True)
        validate_output_tracks(self.output, media)

    def test_mp4_never_discards_audio_without_compatible_copy(self):
        output = self.root / 'video.mp4'
        media = {'streams':['audio'], 'track_codecs':[{'type':'audio', 'codec':'flac'}]}
        with self.assertRaisesRegex(ValueError, 'error.mp4_tracks'):
            validate_output_tracks(output, media, True)
        media['track_codecs'][0]['codec'] = 'aac'
        validate_output_tracks(output, media)
        with self.assertRaisesRegex(ValueError, 'error.mp4_tracks'):
            validate_output_tracks(output, {'streams':['audio']}, True)


if __name__ == "__main__":
    unittest.main()
