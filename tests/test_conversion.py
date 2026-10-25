import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from gamdl.app import ConversionJobSpec, ConversionService


FFMPEG_COMMAND = shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG_COMMAND, "ffmpeg is required for conversion integration tests")
class ConversionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ConversionService()
        self.service.full_ffmpeg_path = FFMPEG_COMMAND

    def _create_sample_m4a(
        self,
        root: Path,
        sample_rate: int = 44100,
        *,
        include_cover: bool = True,
    ) -> Path:
        source = root / "sample.m4a"
        cover = root / "cover.jpg"
        subprocess.run(
            [
                FFMPEG_COMMAND,
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(source),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        audio = MP4(source)
        audio["\xa9nam"] = ["Song"]
        audio["\xa9ART"] = ["Artist"]
        audio["trkn"] = [(12, 13)]
        audio["disk"] = [(1, 1)]
        if include_cover:
            subprocess.run(
                [
                    FFMPEG_COMMAND,
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=32x32:d=0.1",
                    "-frames:v",
                    "1",
                    str(cover),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            audio["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return source

    def _create_sample_wav(self, root: Path, sample_rate: int) -> Path:
        source = root / "sample.wav"
        subprocess.run(
            [
                FFMPEG_COMMAND,
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=1",
                "-ar",
                str(sample_rate),
                str(source),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return source

    def test_flac_conversion_preserves_common_metadata_and_cover(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = self._create_sample_m4a(root)
            output = root / "output"
            output.mkdir()

            result = self.service.run(
                ConversionJobSpec(
                    input_mode="file",
                    input_path=str(source),
                    output_path=str(output),
                    target_format="flac",
                )
            )

            target = output / "sample.flac"
            self.assertEqual(result.converted_files, 1)
            self.assertTrue(target.exists())
            audio = FLAC(target)
            self.assertEqual(audio["title"], ["Song"])
            self.assertEqual(audio["artist"], ["Artist"])
            self.assertEqual(
                audio["description"],
                ["软件纯免费开源，无病毒，无额外广告，请确保你是从 GitHub @Mrgu2 下载的该软件。"],
            )
            self.assertEqual(audio["tracknumber"], ["12"])
            self.assertEqual(audio["tracktotal"], ["13"])
            self.assertEqual(audio["totaltracks"], ["13"])
            self.assertEqual(audio["discnumber"], ["1"])
            self.assertEqual(audio["disctotal"], ["1"])
            self.assertEqual(audio["totaldiscs"], ["1"])
            self.assertEqual(len(audio.pictures), 1)
            self.assertEqual(audio.info.sample_rate, 44100)

    def test_mp3_conversion_preserves_sample_rate_and_comment(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = self._create_sample_m4a(root)
            output = root / "output"
            output.mkdir()

            result = self.service.run(
                ConversionJobSpec(
                    input_mode="file",
                    input_path=str(source),
                    output_path=str(output),
                    target_format="mp3",
                )
            )

            target = output / "sample.mp3"
            self.assertEqual(result.converted_files, 1)
            self.assertTrue(target.exists())
            audio = MP3(target)
            self.assertEqual(audio.info.sample_rate, 44100)
            tags = ID3(target)
            self.assertEqual(str(tags["TIT2"]), "Song")
            self.assertEqual(str(tags["TPE1"]), "Artist")
            self.assertEqual(
                str(tags["COMM::eng"]),
                "软件纯免费开源，无病毒，无额外广告，请确保你是从 GitHub @Mrgu2 下载的该软件。",
            )
            self.assertTrue(tags.getall("APIC"))

    def test_mp3_conversion_succeeds_without_mp4_cover_art(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = self._create_sample_m4a(root, include_cover=False)
            output = root / "output"
            output.mkdir()

            result = self.service.run(
                ConversionJobSpec(
                    input_mode="file",
                    input_path=str(source),
                    output_path=str(output),
                    target_format="mp3",
                )
            )

            target = output / "sample.mp3"
            self.assertEqual(result.converted_files, 1)
            self.assertEqual(result.errors, 0)
            self.assertTrue(target.exists())
            tags = ID3(target)
            self.assertEqual(str(tags["TIT2"]), "Song")
            self.assertEqual(str(tags["TPE1"]), "Artist")
            self.assertFalse(tags.getall("APIC"))

    def test_directory_mode_preserves_relative_structure_and_skips_non_audio(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source_dir = root / "input"
            nested = source_dir / "nested"
            nested.mkdir(parents=True)
            source = self._create_sample_m4a(nested)
            (source_dir / "notes.txt").write_text("ignore", encoding="utf-8")
            output = root / "output"
            output.mkdir()

            result = self.service.run(
                ConversionJobSpec(
                    input_mode="directory",
                    input_path=str(source_dir),
                    output_path=str(output),
                    target_format="flac",
                )
            )

            self.assertEqual(result.converted_files, 1)
            self.assertEqual(result.skipped_files, 2)
            self.assertTrue((output / "nested" / "sample.flac").exists())

    def test_overwrite_false_skips_existing_target(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = self._create_sample_m4a(root)
            output = root / "output"
            output.mkdir()
            target = output / "sample.flac"
            target.write_text("existing", encoding="utf-8")

            result = self.service.run(
                ConversionJobSpec(
                    input_mode="file",
                    input_path=str(source),
                    output_path=str(output),
                    target_format="flac",
                    overwrite=False,
                )
            )

            self.assertEqual(result.skipped_files, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "existing")

    def test_mp3_rejects_unsupported_sample_rate_without_resampling(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = self._create_sample_wav(root, sample_rate=96000)
            output = root / "output"
            output.mkdir()

            result = self.service.run(
                ConversionJobSpec(
                    input_mode="file",
                    input_path=str(source),
                    output_path=str(output),
                    target_format="mp3",
                )
            )

            self.assertEqual(result.errors, 1)
            self.assertFalse((output / "sample.mp3").exists())


if __name__ == "__main__":
    unittest.main()
