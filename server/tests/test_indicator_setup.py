"""Install-time checks for GNOME's otherwise invisible indicator host."""
import unittest
from unittest.mock import call, patch

from oneaxe_voice.desktop_setup import enable_indicator_host


class IndicatorSetupTests(unittest.TestCase):
    @patch('oneaxe_voice.desktop_setup.run')
    def test_enables_existing_host_and_master_switch(self, run):
        run.side_effect = ['other-extension\nubuntu-appindicators@ubuntu.com', 'true', '', '']
        enable_indicator_host()
        self.assertEqual(run.call_args_list, [
            call('gnome-extensions', 'list'),
            call('gsettings', 'get', 'org.gnome.shell', 'disable-user-extensions'),
            call('gsettings', 'set', 'org.gnome.shell', 'disable-user-extensions', 'false'),
            call('gnome-extensions', 'enable', 'ubuntu-appindicators@ubuntu.com'),
        ])

    @patch('oneaxe_voice.desktop_setup.run', return_value='some-other-extension')
    def test_missing_host_does_not_change_desktop_settings(self, run):
        with self.assertRaisesRegex(ValueError, '缺少 GNOME AppIndicator'):
            enable_indicator_host()
        run.assert_called_once_with('gnome-extensions', 'list')
