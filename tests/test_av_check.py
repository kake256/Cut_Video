"""Audio timestamp check run on every rendered Short before it can be uploaded."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from moment_retrieval import av_check


class PacketRuleTest(unittest.TestCase):
    def test_only_inner_packets_must_be_one_frame(self):
        self.assertEqual([], av_check.packet_problems([1024] * 5))
        # Priming packet and the last (partial) packet may be any length.
        self.assertEqual([], av_check.packet_problems([1024, 1024, 1024, 300]))
        self.assertEqual([], av_check.packet_problems([2048, 1024, 1024, 300]))
        # One sample of timestamp rounding is not a problem; a gap of 96 ms is.
        self.assertEqual([], av_check.packet_problems([1024, 1023, 1025, 1022, 1026, 1024, 64]))
        self.assertEqual([(2, 5632)], av_check.packet_problems([1024, 1024, 5632, 1024, 64]))
        # The broken files: the first real frame 1310 long, then frames 1 sample apart.
        self.assertEqual([(1, 1310)], av_check.packet_problems([1024, 1310, 1024, 1024, 114]))
        self.assertEqual([(2, 1), (3, 215)], av_check.packet_problems([1024, 1024, 1, 215, 1024, 64]))

    def test_length_gap(self):
        streams = [{"codec_type": "video", "duration": "58.36"}, {"codec_type": "audio", "duration": "58.31"}]
        self.assertAlmostEqual(0.05, av_check.length_gap(streams), places=3)
        self.assertIsNone(av_check.length_gap(streams[:1]))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class RealFileTest(unittest.TestCase):
    """One-pass loudnorm on a quiet-then-loud signal, with and without the retiming filter."""

    def render(self, folder: Path, name: str, audio_chain: str) -> Path:
        output = folder / name
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=black:s=64x64:r=30:d=8",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=8",
             "-filter_complex", f"[1:a]volume='if(lt(t,3),0.05,0.8)':eval=frame,{audio_chain}[aout]",
             "-map", "0:v", "-map", "[aout]", "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", "-b:a", "128k", str(output)],
            check=True, capture_output=True, timeout=120)
        return output

    def test_retimed_render_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = ("loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,"
                     f"aformat=sample_rates=48000:channel_layouts=stereo,{av_check.AUDIO_RETIME}")
            output = self.render(Path(tmp), "fixed.mp4", chain)
            self.assertEqual([], av_check.audio_timing_problems(output))
            av_check.verify_audio_timing(output)

    def test_shifted_audio_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Audio that starts 0.5 s late: frames still 1024 long, but the lengths no longer match.
            output = self.render(Path(tmp), "late.mp4", "adelay=500|500,atrim=0:8.5")
            problems = av_check.audio_timing_problems(output)
            self.assertTrue(any("長さ" in problem for problem in problems), problems)
            with self.assertRaises(av_check.AudioTimingError):
                av_check.verify_audio_timing(output)


if __name__ == "__main__":
    unittest.main()
