import unittest

from borasuki.progress import RenderEstimate
from borasuki.pipeline import inference_frame


class EstimateTests(unittest.TestCase):
    def test_verified_cycle_ignores_encoder_flush_burst_and_includes_startup(self):
        estimate = RenderEstimate()
        estimate.begin(0)
        estimate.observe(1, 10, 999)
        estimate.observe(25, 20, 975)
        estimate.observe(48, 20.1, 952)
        estimate.complete(48, 24)
        self.assertEqual(estimate.speed, 2)
        estimate.begin(25)
        estimate.observe(1, 35, 951, 10)
        self.assertEqual(estimate.observe(48, 35.1, 904, 10), (2, 452))
        estimate.complete(48, 73)
        self.assertAlmostEqual(estimate.speed, 96 / 72)
        self.assertEqual(estimate.remaining(96, 10), 72)

    def test_invalid_cycle_and_new_instance_do_not_reuse_old_speed(self):
        estimate = RenderEstimate()
        with self.assertRaises(ValueError):
            estimate.complete(48, 24)
        estimate.begin(10)
        for frames, now in ((0, 15), (10, 10), (10, 9)):
            with self.assertRaises(ValueError):
                estimate.complete(frames, now)
        self.assertIsNone(RenderEstimate().remaining(100, 4))

    def test_vspipe_counter_does_not_use_its_startup_average_fps(self):
        self.assertEqual(inference_frame('Frame: 12/64 (0.02 fps)'), 12)
        self.assertIsNone(inference_frame('Output 64 frames in 123 seconds'))
    def test_cold_build_is_not_extrapolated_to_remaining_frames(self):
        estimate = RenderEstimate()
        estimate.begin(0)
        self.assertEqual(estimate.observe(0, 320, 1000), (None, None))
        self.assertEqual(estimate.observe(1, 330, 999), (None, None))
        speed, eta = estimate.observe(11, 331, 989, 4)
        self.assertEqual(speed, 10)
        self.assertEqual(eta, 99)

    def test_recent_rate_adapts_and_stall_does_not_claim_progress(self):
        estimate = RenderEstimate()
        estimate.begin(0)
        for second in range(20):
            estimate.observe(second * 10 + 1, second + 1, 500)
        for second in range(20, 34):
            speed, eta = estimate.observe(191 + (second - 19) * 5, second + 1, 500)
        self.assertEqual(speed, 5)
        self.assertEqual(eta, 100)
        speed, eta = estimate.observe(261, 47, 500)
        self.assertEqual(speed, 0)
        self.assertIsNone(eta)

    def test_warm_startup_is_charged_per_remaining_segment(self):
        estimate = RenderEstimate()
        estimate.begin(0)
        estimate.observe(1, 300, 999)
        estimate.observe(11, 301, 989)
        estimate.begin(305)
        speed, eta = estimate.observe(1, 308, 500, 3)
        self.assertEqual(speed, 10)
        self.assertEqual(eta, 59)
        self.assertEqual(estimate.observe(11, 309, 490, 3), (10, 58))

    def test_resume_is_a_new_measurement_not_old_frame_credit(self):
        estimate = RenderEstimate()
        estimate.begin(100)
        estimate.observe(1, 103, 99)
        self.assertEqual(estimate.observe(3, 104, 97), (2, 49))
