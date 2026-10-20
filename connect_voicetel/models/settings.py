# -*- coding: utf-8 -*-
import logging
import re
from urllib.parse import urljoin, urlparse

from odoo import fields, models, api, release
from odoo.exceptions import ValidationError
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from odoo.addons.connect.models.license import ODUIST_MODULES
from odoo.addons.connect.models.settings import debug

ODUIST_MODULES.append('connect_voicetel')


logger = logging.getLogger(__name__)

VOICETEL_LOG_LEVEL = logging.WARNING

MAX_EXTEN_LEN = 4

VOICETEL_PROTECTED_FIELDS = [
    "display_voicetel_api_key",
]

# VoiceML's public hostnames. The helper library builds every URL on the
# Twilio hosts; the HTTP client below rewrites them to the configured host.
DEFAULT_VOICETEL_API_HOST = 'voiceml.voicetel.com'
# Tenant SIP domains live under this suffix: the registrar only
# authenticates *.vml.voice.tel names (ADR-066 decision 9).
DEFAULT_VOICETEL_SIP_SUFFIX = 'vml.voice.tel'
DEFAULT_VOICETEL_STUN_SERVER = 'stun:stun.l.google.com:19302'
# The SIP over WebSocket listener is tied to the SIP domain:
# wss://<domain_name>:8443 (VoiceML Phone uses the same address).
VOICETEL_WSS_PORT = 8443

RE_TWILIO_HOST = re.compile(r'^https://[a-z0-9.-]+\.twilio\.com', re.IGNORECASE)


def format_connect_response(text):
    if not isinstance(text, str):
        text = str(text)
    symbol_pattern = re.compile(r"(\x08.)|\x08")
    text = symbol_pattern.sub("", text)
    color_pattern = re.compile(r"\x1b\[[\d;]+m")
    text = color_pattern.sub("", text)
    return text


def strip_number(number):
    """Strip number formatting"""
    if not isinstance(number, str):
        return number
    pattern = r"[\s\(\)\-\+]"
    return re.sub(pattern, "", number).lstrip("0")


class VoiceMLHttpClient(TwilioHttpClient):
    """Twilio helper HTTP client pointed at a VoiceML host.

    VoiceML implements the Twilio REST surface on a host of its own and
    documents this override as the integration path for the official helper
    libraries: every request the helper builds for ``https://*.twilio.com``
    is sent to the VoiceML host instead, with the path, parameters, Basic
    auth and response shapes untouched.
    """

    def __init__(self, host, **kwargs):
        super().__init__(**kwargs)
        self.voiceml_base = 'https://{}'.format(host.strip().rstrip('/'))

    def rewrite_url(self, url):
        return RE_TWILIO_HOST.sub(self.voiceml_base, url or '', count=1)

    def request(self, method, url, *args, **kwargs):
        return super().request(method, self.rewrite_url(url), *args, **kwargs)


class Settings(models.Model):
    _inherit = "connect.settings"

    voicetel_account_sid = fields.Char(string="Account SID")
    # Never grant this to connect.group_webhook: the webhook user is the
    # identity of all public webhook controllers, and get_param() returns
    # groups-restricted fields to group members. Signature validation in
    # the controllers reads the key via sudo() and is not affected.
    voicetel_api_key = fields.Char(groups="base.group_erp_manager")
    display_voicetel_api_key = fields.Char()
    voicetel_api_host = fields.Char(
        string='API Host', default=DEFAULT_VOICETEL_API_HOST, required=True,
        help='Hostname of the VoiceML REST API, without scheme or path.')
    voicetel_sip_suffix = fields.Char(
        string='SIP Domain Suffix', default=DEFAULT_VOICETEL_SIP_SUFFIX,
        required=True,
        help='Tenant SIP domains are created as <label>.<suffix>. The VoiceML '
             'registrar authenticates registrations and calls under this '
             'suffix only.')
    voicetel_stun_server = fields.Char(
        string='STUN Server', default=DEFAULT_VOICETEL_STUN_SERVER,
        help='STUN server for the browser phone. The WSS edge forces ICE, so '
             'a symmetric NAT without STUN leaves callers with one-way audio.')
    voicetel_phone_enabled = fields.Boolean(
        string='Browser Phone', default=True,
        help='Load the VoiceTel web phone for users whose browser phone is '
             'enabled.')
    voicetel_balance = fields.Char(readonly=True)
    voicetel_auto_sync = fields.Boolean(default=True)
    voicetel_verify_requests = fields.Boolean(
        default=True, string="Verify VoiceTel Requests"
    )
    voicetel_fetch_call_prices = fields.Boolean(
        default=False,
        string="Fetch Call Prices",
        help="Fetch call prices from the VoiceML API after call completion."
    )

    @api.model
    def _voicetel_api_url(self):
        """Public URL callbacks are built on.

        The connect `api_url` parameter wins; when it is unset (a fresh
        install, the test rig) fall back to the instance's web.base.url. The
        computed connect.settings fields of the same name must not be read
        through get_param(), which would return a recordset.
        """
        # connect.api_url is the public URL set by the admin; when unset it
        # is seeded to web.base.url by set_defaults. Read the parameter
        # directly: the settings record is transient and tests set the same
        # parameter through it.
        params = self.env['ir.config_parameter'].sudo()
        url = params.get_param('connect.api_url')
        if url:
            return url
        return params.get_param('web.base.url') or ''

    @api.model
    def _voicetel_api_host(self):
        host = (self.sudo().get_param('voicetel_api_host') or '').strip()
        return (host or DEFAULT_VOICETEL_API_HOST).lower().rstrip('/')

    @api.model
    def get_media_auth(self, media_url):
        """VoiceML API credentials, and only for the VoiceML host.

        Recording media is served by the API with HTTP Basic auth. The host
        check is not cosmetic: once a tenant's own bucket took the recording
        the API answers with a redirect to a presigned S3 URL, and the API
        key must not be sent there.
        """
        host = (urlparse(media_url or '').hostname or '').lower()
        if host != self._voicetel_api_host():
            return super().get_media_auth(media_url)
        settings = self.sudo()
        account_sid = settings.get_param('voicetel_account_sid')
        api_key = settings.get_param('voicetel_api_key')
        if not (account_sid and api_key):
            return super().get_media_auth(media_url)
        return (account_sid, api_key)

    @api.model
    def get_voicetel_client(self):
        try:
            # connect.settings is admin-only; credentials are read with sudo()
            # below, so no caller-level model access check is needed here.
            account_sid = self.sudo().get_param("voicetel_account_sid")
            api_key = self.sudo().get_param("voicetel_api_key")
            http_client = VoiceMLHttpClient(self._voicetel_api_host())
            client = Client(account_sid, api_key, http_client=http_client)
            client.http_client.logger.setLevel(VOICETEL_LOG_LEVEL)
            return client
        except Exception as e:
            if "Credentials are required" in str(e):
                raise ValidationError("Set the VoiceTel Account SID and API key first!")
            else:
                raise

    def voicetel_sync(self):
        if not (
            self.sudo().get_param("voicetel_account_sid")
            and self.sudo().get_param("voicetel_api_key")
        ):
            raise ValidationError("You must set the Account SID and API key!")
        api_url_check = self.check_api_url()
        if api_url_check:
            raise ValidationError(api_url_check)
        try:
            self.env["connect.voicetel.voiceml"].sync()
            self.env["connect.voicetel.domain"].sync()
            self.env["connect.voicetel.number"].sync()
            self.env["connect.voicetel.outgoing_callerid"].sync()
            self.connect_notify("VoiceTel account synced successfully", title="Sync Complete")
        except Exception as e:
            message = str(e)
            if ('Authentication Error' in message or '20003' in message
                    or getattr(e, 'code', None) == 20003):
                raise ValidationError(
                    'Error authenticating requests to the VoiceML API! Check your API key!'
                )
            raise

    @api.model
    def voicetel_get_phone_settings(self):
        """Web phone configuration for the JsSIP client.

        The payload keys mirror what the phone component consumes in
        ``initUserAgent``; the per-user half (SIP credential, domain) comes
        from ``res.users.get_voicetel_sip_user_config``.
        """
        get_param = self.sudo().get_param
        return {
            "phone_enabled": bool(get_param("voicetel_phone_enabled")),
            "user_agent": {
                "phone_sip_protocol": "wss",
                "phone_stun_server": get_param("voicetel_stun_server") or "",
            },
        }

    def voicetel_webhook_url(self, path):
        """Absolute Odoo URL for a VoiceML callback.

        VoiceML signs the URL as transmitted and has no edge concept, so no
        fragment is ever appended here (unlike the Twilio module's #e=edge).
        """
        api_url = self.sudo()._voicetel_api_url()
        return urljoin(api_url, path)

    def compute_voicetel_sip_uri(self, user):
        return "sip:{}".format(user.connect_user.voicetel_uri)

    def voicetel_external_call_route(self, number, callerId, status_url):
        call_duration_limit = int(self.sudo().get_param('call_duration_limit'))
        voiceml = """
        <Response>
            <Dial callerId="{}" timeLimit="{}"><Number statusCallback='{}' statusCallbackEvent='initiated answered completed'>{}</Number></Dial>
        </Response>
        """.format(
            callerId, call_duration_limit, status_url, number
        )
        return voiceml

    @api.model
    def originate_call(self, number, res_model=None, res_id=None, user=None, **kwargs):
        # Dispatch by the user's click-to-call provider; fall through to
        # other installed telephony modules when it is not VoiceTel.
        if self._get_originate_provider(user) != 'voicetel':
            return super().originate_call(
                number, res_model=res_model, res_id=res_id, user=user, **kwargs)
        self.env["oduist.license"].check_license("connect", silent=False)
        number = strip_number(number)
        if len(number) > MAX_EXTEN_LEN:
            number = "+{}".format(number)
        client = self.get_voicetel_client()
        partner_id = False
        obj = self.env[res_model].browse(res_id) if res_model and res_id else False
        if res_model == "res.partner" and obj:
            partner_id = res_id
        elif obj and hasattr(obj, "partner_id") and obj.partner_id:
            partner_id = obj.partner_id.id
        elif obj and hasattr(obj, "partner") and obj.partner:
            partner_id = obj.partner.id
        if not user:
            user = self.env.user
        pbx_user = user.connect_user
        if not pbx_user:
            raise ValidationError("User does not have a PBX account defined!")
        if not (pbx_user.voicetel_sip_enabled and pbx_user.voicetel_username
                and pbx_user.voicetel_domain):
            raise ValidationError(
                "Enable the VoiceTel SIP phone for user {} first!".format(
                    pbx_user.name))
        # The agent leg is the user's SIP address on the tenant domain: the
        # registrar rings every device registered with that credential, the
        # browser phone included (ADR-066 decision 4).
        to = self.compute_voicetel_sip_uri(user)
        exten = self.env["connect.voicetel.exten"].search(
            [("number", "=", number)], limit=1
        )
        status_url = self.voicetel_webhook_url("voicetel/webhook/callstatus")
        if exten:
            # An extension-less caller must still present an identity: an
            # empty caller ID makes the platform substitute a number of its own.
            callerId = pbx_user.voicetel_caller_id()
            # Rendering the destination dialplan is system work: it reads the
            # callee's connect.user, which the record rules keep private to
            # its owner. Without sudo, calling a colleague's extension fails
            # with an AccessError on connect.user.
            voiceml = exten.sudo().render()
        else:
            default_number = self.env[
                "connect.voicetel.outgoing_callerid"
            ].search([("is_default", "=", True)], limit=1)
            if pbx_user.voicetel_outgoing_callerid:
                callerId = pbx_user.voicetel_outgoing_callerid.number
            else:
                callerId = default_number.number
            if not callerId:
                raise ValidationError(
                    "Select a default outgoing caller ID first!")
            voiceml = self.voicetel_external_call_route(
                number, callerId, status_url
            )
        record = pbx_user.record_calls
        record_status_url = self.voicetel_webhook_url(
            "voicetel/webhook/recordingstatus")
        debug(self, 'Originate destination VoiceML: {}'.format(voiceml))
        channel = client.calls.create(
            twiml=voiceml,
            to=to,
            from_=callerId,
            status_callback=status_url,
            record=record,
            recording_channels="dual",
            recording_status_callback=record_status_url,
            recording_status_callback_event=["completed"],
            status_callback_event=["initiated", "answered", "completed"],
        )
        self.env["connect.channel"].sudo().create(
            {
                "sid": channel.sid,
                "technical_direction": "outbound-api",
                "call_type": 'phone',
                "caller_user": user.id,
                "caller_pbx_user": pbx_user.id,
                "partner": partner_id,
                "called": number,
                "caller": callerId,
            }
        )

    def get_voicetel_balance(self):
        """Fetch the current VoiceML account balance."""
        try:
            client = self.get_voicetel_client()
            try:
                balance_item = client.api.v2010.account.balance.fetch()
                currency = getattr(balance_item, 'currency', 'USD')
                balance_value = getattr(balance_item, 'balance', '0.00')
                balance = "${} {}".format(balance_value, currency)
            except Exception as balance_error:
                if (
                    '20404' in str(balance_error)
                    or 'not found' in str(balance_error).lower()
                ):
                    balance = "Balance API not available for this account"
                    self.set_param('voicetel_balance', balance)
                    self.connect_notify(
                        "VoiceTel Balance: {}.".format(balance),
                        title="Balance Info",
                    )
                    return balance
                else:
                    raise balance_error
            self.set_param('voicetel_balance', balance)
            self.connect_notify(
                "VoiceTel Balance: {}".format(balance), title="Balance Update"
            )
            return balance
        except Exception as e:
            error_msg = "Failed to fetch VoiceTel balance: {}".format(str(e))
            self.connect_notify(error_msg, title="Balance Error", warning=True)
            raise ValidationError(error_msg)

    def write(self, vals):
        if self.env.context.get("skip_protected_fields"):
            return super(Settings, self).write(vals)
        res = super(Settings, self).write(vals)
        changed_fields = {}
        for field_name in VOICETEL_PROTECTED_FIELDS:
            if vals.get(field_name):
                changed_fields.update(
                    {
                        field_name.replace("display_", ""): vals.get(
                            field_name
                        ),
                        field_name: "*" * len(vals.get(field_name)),
                    }
                )
        if changed_fields:
            self.with_context(
                skip_protected_fields=True
            ).sudo().write(changed_fields)
        if release.version_info[0] >= 17:
            self.env.registry.clear_cache()
        else:
            self.clear_caches()
        return res
