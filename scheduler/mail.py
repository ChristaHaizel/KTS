"""Sending mail through a provider's HTTP API instead of SMTP.

The host blocks outbound SMTP. A connection to port 587 times out rather than
being refused, which is what a blocked port looks like from the inside -
nothing answers, and eventually the socket gives up. Alternative ports exist
for that reason and may or may not be open.

The same providers accept the same message over HTTPS on 443, which no host
blocks because it is the port the web runs on. So when an API key is present
that is the road taken, and the SMTP question stops mattering.

Two providers, because they differ in the thing that decides whether anyone
except the administrator can reset a password:

  Resend  proves you may send by verifying a whole domain through DNS. Until
          then it delivers only to the address that owns the account, so every
          student's reset is refused.
  Brevo   proves it by verifying a single address you already own. Verify a
          personal mailbox and mail goes to anybody, no domain required.

Brevo is therefore the one that makes self-service resets work without owning
a domain, and is preferred when both are configured.

urllib from the standard library rather than requests: one fewer thing to
install on a free tier, for one POST.
"""
import json
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class MailDeliveryError(Exception):
    """Raised with what the provider said, for the operator to act on."""


def _split_address(address):
    """"Name <a@b.c>" into its two parts, either of which may be empty."""
    name, email = parseaddr(address or '')
    return name, email


class HttpEmailBackend(BaseEmailBackend):
    """The parts of talking to a mail API that do not vary by provider.

    Subclasses say where to post, how to authenticate, what shape the body
    takes, and how to read a refusal back to whoever configured it.
    """

    name = 'HTTP'
    endpoint = ''

    def __init__(self, fail_silently=False, api_key=None, timeout=None, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.api_key = api_key if api_key is not None else self.configured_key()
        self.timeout = (timeout if timeout is not None
                        else getattr(settings, 'EMAIL_TIMEOUT', 10))

    # -- provider specifics ------------------------------------------------

    @staticmethod
    def configured_key():
        raise NotImplementedError

    def headers(self):
        raise NotImplementedError

    def payload(self, message, recipients):
        raise NotImplementedError

    def explain(self, status, detail):
        raise NotImplementedError

    def missing_key_message(self):
        raise NotImplementedError

    # -- the shared part ---------------------------------------------------

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        if not self.api_key:
            if self.fail_silently:
                return 0
            raise MailDeliveryError(self.missing_key_message())

        sent = 0
        for message in email_messages:
            try:
                self._send(message)
            except Exception:
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent

    def _send(self, message):
        recipients = list(message.to or [])
        if not recipients:
            return

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(self.payload(message, recipients)).encode('utf-8'),
            headers={'Content-Type': 'application/json', **self.headers()},
            method='POST',
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise MailDeliveryError(
                self.explain(exc.code, self._detail(exc))) from None
        except urllib.error.URLError as exc:
            raise MailDeliveryError(
                f'Could not reach {self.name}: {exc.reason}.') from None

    @staticmethod
    def _detail(exc):
        """Whatever the provider said, if it said anything readable."""
        try:
            body = json.loads(exc.read().decode('utf-8'))
        except Exception:
            return ''
        if isinstance(body, dict):
            return str(body.get('message') or body.get('error') or '')
        return ''


# ---------------------------------------------------------------------------
# Brevo
# ---------------------------------------------------------------------------

def brevo_api_key():
    explicit = getattr(settings, 'BREVO_API_KEY', '')
    if explicit:
        return explicit
    # Following Brevo's SMTP instructions first is what puts it here.
    smtp_password = getattr(settings, 'EMAIL_HOST_PASSWORD', '') or ''
    if smtp_password.startswith('xkeysib-'):
        return smtp_password
    return ''


class BrevoBackend(HttpEmailBackend):
    name = 'Brevo'
    endpoint = 'https://api.brevo.com/v3/smtp/email'

    configured_key = staticmethod(brevo_api_key)

    def headers(self):
        return {'api-key': self.api_key, 'accept': 'application/json'}

    def payload(self, message, recipients):
        name, email = _split_address(message.from_email
                                     or settings.DEFAULT_FROM_EMAIL)
        sender = {'email': email}
        if name:
            sender['name'] = name

        body = {
            'sender': sender,
            'to': [{'email': address} for address in recipients],
            'subject': message.subject,
            'textContent': message.body,
        }
        if message.cc:
            body['cc'] = [{'email': address} for address in message.cc]
        if message.bcc:
            body['bcc'] = [{'email': address} for address in message.bcc]
        return body

    def missing_key_message(self):
        return ('No Brevo API key is configured. Set BREVO_API_KEY to the key '
                'from Brevo under SMTP & API - it begins "xkeysib-".')

    def explain(self, status, detail):
        advice = {
            401: 'Brevo rejected the API key. Check BREVO_API_KEY is the whole '
                 'key from SMTP & API, and that it has not been deleted.',
            400: 'Brevo would not accept the message. The commonest cause is a '
                 'sender address that has not been verified - DEFAULT_FROM_EMAIL '
                 'must be an address you have confirmed under Senders, by '
                 'clicking the link Brevo emails to it.',
            402: 'Brevo will not send any more today. The free plan has a daily '
                 'limit, which resets tomorrow.',
            429: 'Too many messages sent to Brevo just now. Wait a moment.',
        }.get(status, f'Brevo returned HTTP {status}.')
        return f'{advice} {detail}'.strip()


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------

def resend_api_key():
    """The key, wherever it has been put.

    RESEND_API_KEY is the name that says what it is. EMAIL_HOST_PASSWORD is
    where it ends up when someone follows Resend's SMTP instructions first,
    which is the likelier order of events - and a Resend key is recognisable,
    so there is no need to make them move it to get mail working.
    """
    explicit = getattr(settings, 'RESEND_API_KEY', '')
    if explicit:
        return explicit
    smtp_password = getattr(settings, 'EMAIL_HOST_PASSWORD', '') or ''
    if smtp_password.startswith('re_'):
        return smtp_password
    return ''


class ResendBackend(HttpEmailBackend):
    name = 'Resend'
    endpoint = 'https://api.resend.com/emails'

    configured_key = staticmethod(resend_api_key)

    def headers(self):
        return {'Authorization': f'Bearer {self.api_key}'}

    def payload(self, message, recipients):
        body = {
            'from': message.from_email or settings.DEFAULT_FROM_EMAIL,
            'to': recipients,
            'subject': message.subject,
            'text': message.body,
        }
        if message.cc:
            body['cc'] = list(message.cc)
        if message.bcc:
            body['bcc'] = list(message.bcc)
        return body

    def missing_key_message(self):
        return ('No Resend API key is configured. Set RESEND_API_KEY to the key '
                'from your Resend dashboard - it begins "re_".')

    def explain(self, status, detail):
        advice = {
            401: 'Resend rejected the API key. Check RESEND_API_KEY is the '
                 'whole key, copied from the dashboard, and has not been revoked.',
            403: 'Resend refused to send this. Until you verify a domain it '
                 'will only deliver to the address you signed up with, and the '
                 'from address must be on a domain you own.',
            422: 'Resend would not accept the message as addressed. The from '
                 'address must be on a domain you have verified.',
            429: 'Too many messages sent to Resend just now. Wait a moment.',
        }.get(status, f'Resend returned HTTP {status}.')
        return f'{advice} {detail}'.strip()
