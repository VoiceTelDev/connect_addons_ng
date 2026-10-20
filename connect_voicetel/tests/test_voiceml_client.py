# -*- coding: utf-8 -*-
"""The Twilio helper library is pointed at the VoiceML host (ADR-066 decision 3)."""
from odoo.tests import tagged

from odoo.addons.connect_voicetel.models.settings import VoiceMLHttpClient

from .common import VoiceTelTestCommon


@tagged('at_install', '-post_install')
class TestVoiceMLClient(VoiceTelTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings = cls.env['connect.settings'].sudo()
        cls.settings.set_param('voicetel_account_sid', 'AC' + '1' * 32)
        cls.settings.set_param('voicetel_api_key', 'k' * 64)
        cls.settings.set_param('voicetel_api_host', 'voiceml.voicetel.com')
        cls.env['ir.config_parameter'].sudo().set_param(
            'connect.api_url', 'https://odoo.test/')
        cls.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', 'https://odoo.test/')

    def test_every_twilio_host_is_rewritten_to_the_voiceml_host(self):
        client = VoiceMLHttpClient('voiceml.voicetel.com')

        self.assertEqual(
            client.rewrite_url(
                'https://api.twilio.com/2010-04-01/Accounts/AC1/Calls.json'),
            'https://voiceml.voicetel.com/2010-04-01/Accounts/AC1/Calls.json')
        self.assertEqual(
            client.rewrite_url('https://routes.twilio.com/v2/PhoneNumbers/%2B1'),
            'https://voiceml.voicetel.com/v2/PhoneNumbers/%2B1')
        self.assertEqual(
            client.rewrite_url('https://api.ashburn.us1.twilio.com/x'),
            'https://voiceml.voicetel.com/x')

    def test_a_non_twilio_url_is_left_alone(self):
        client = VoiceMLHttpClient('voiceml.voicetel.com')

        self.assertEqual(
            client.rewrite_url('https://example.com/2010-04-01/x'),
            'https://example.com/2010-04-01/x')

    def test_the_client_carries_the_account_credentials_and_host(self):
        client = self.settings.get_voicetel_client()

        self.assertEqual(client.username, 'AC' + '1' * 32)
        self.assertEqual(client.password, 'k' * 64)
        self.assertIsInstance(client.http_client, VoiceMLHttpClient)
        self.assertEqual(
            client.http_client.voiceml_base, 'https://voiceml.voicetel.com')

    def test_the_host_setting_is_normalized(self):
        self.settings.set_param('voicetel_api_host', ' VoiceML.Example.NET/ ')

        self.assertEqual(self.settings._voicetel_api_host(), 'voiceml.example.net')

    def test_callback_urls_carry_no_fragment(self):
        url = self.settings.voicetel_webhook_url('voicetel/webhook/callstatus')

        self.assertEqual(url, 'https://odoo.test/voicetel/webhook/callstatus')

    def test_phone_settings_payload(self):
        self.settings.set_param('voicetel_stun_server', 'stun:stun.example.com')

        payload = self.settings.voicetel_get_phone_settings()

        self.assertTrue(payload['phone_enabled'])
        self.assertEqual(payload['user_agent']['phone_sip_protocol'], 'wss')
        self.assertEqual(
            payload['user_agent']['phone_stun_server'], 'stun:stun.example.com')
