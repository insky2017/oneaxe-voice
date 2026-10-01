import unittest

from oneaxe_voice.preview_layout import anchor_from_position, monitor_for_window, overlay_position


class PreviewLayoutTests(unittest.TestCase):
    def test_default_top_and_other_edges(self):
        area = (100, 50, 1200, 800)
        size = (300, 80)
        self.assertEqual(overlay_position(area, size), (550, 78))
        self.assertEqual(overlay_position(area, size, "bottom"), (550, 742))
        self.assertEqual(overlay_position(area, size, "left"), (128, 410))
        self.assertEqual(overlay_position(area, size, "right"), (972, 410))

    def test_custom_anchor_clamps_and_survives_resolution_change(self):
        area = (0, 0, 1000, 800)
        size = (200, 100)
        anchor = anchor_from_position(area, size, (400, 350))
        self.assertEqual(anchor, [0.5, 0.5])
        self.assertEqual(overlay_position((1000, 0, 1600, 900), size, "custom", anchor), (1700, 400))
        self.assertEqual(overlay_position(area, size, "custom", [5, -2]), (800, 0))

    def test_tiny_monitor_never_moves_outside(self):
        self.assertEqual(overlay_position((10, 20, 100, 50), (200, 90)), (10, 20))

    def test_target_monitor_uses_window_center(self):
        monitors = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]
        self.assertEqual(monitor_for_window(monitors, (2300, 100, 700, 600)), monitors[1])
        self.assertEqual(monitor_for_window(monitors, (-300, 100, 100, 100)), monitors[0])
        self.assertEqual(monitor_for_window(monitors, None), monitors[0])


if __name__ == "__main__":
    unittest.main()
