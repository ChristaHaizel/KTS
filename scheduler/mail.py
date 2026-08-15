"""Sending mail through Resend's HTTP API instead of SMTP.

The host blocks outbound SMTP. A connection to smtp.resend.com:587 times out
rather than being refused, which is what a blocked port looks like from the
inside - nothing answers, and eventually the socket gives up. Alternative
ports exist for exactly this reason and may or may not be open.

The same provider accepts the same message over HTTPS on 443, which no host
blocks because it is the port the web runs on. So when an API key is
configured that is the road taken, and the SMTP question stops mattering.

Uses urllib from the standard library rather than requests: one fewer thing to
install on a free tier, for one POST.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

ENDPOINT = 'https://api.resend.com/emails'


class MailDeliveryError(Exception):
    """Raised with what the provider said, for the operator to act on."""


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


class ResendBackend(BaseEmailBackend):
    """A Django email backend that posts to Resend rather than dialling SMTP."""

    def __init__(self, fail_silently=False, api_key=None, timeout=None, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.api_key = api_key if api_key is not None else resend_api_key()
        self.timeout = (timeout if timeout is not None
                        else getattr(settings, 'EMAIL_TIMEOUT', 10))

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        if not self.api_key:
            if self.fail_silently:
                return 0
            raise MailDeliveryError(
                'No Resend API key is configured. Set RESEND_API_KEY to the key '
                'from your Resend dashboard - it begins "re_".'
            )

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

        payload = {
            'from': message.from_email or settings.DEFAULT_FROM_EMAIL,
            'to': recipients,
            'subject': message.subject,
            'text': message.body,
        }
        if message.cc:
            payload['cc'] = list(message.cc)
        if message.bcc:
            payload['bcc'] = list(message.bcc)

        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise MailDeliveryError(self._explain(exc)) from None
        except urllib.error.URLError as exc:
            raise MailDeliveryError(
                f'Could not reach Resend: {exc.reason}.'
            ) from None

    @staticmethod
    def _explain(exc):
        """Turn Resend's response into something that says what to change."""
        try:
            detail = json.loads(exc.read().decode('utf-8')).get('message', '')
        except Exception:
            detail = ''

        advice = {
            401: 'Resend rejected the API key. Check RESEND_API_KEY is the '
                 'whole key, copied from the dashboard, and has not been revoked.',
            403: 'Resend refused to send this. Until you verify a domain it '
                 'will only deliver to the address you signed up with, and the '
                 'from address must be on a domain you own.',
            422: 'Resend would not accept the message as addressed. The from '
                 'address must be on a domain you have verified.',
            429: 'Too many messages sent to Resend just now. Wait a moment.',
        }.get(exc.code, f'Resend returned HTTP {exc.code}.')

        return f'{advice} {detail}'.strip()
