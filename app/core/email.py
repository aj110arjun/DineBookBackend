from email.message import EmailMessage
from html import escape
import smtplib

from fastapi import HTTPException

from app.core.config import settings


def send_branded_email(
    *,
    to: str,
    subject: str,
    title: str,
    intro: str,
    detail: str,
    plain_text: str,
    error_detail: str,
    highlight_label: str | None = None,
    highlight_value: str | None = None,
    action_label: str | None = None,
    action_url: str | None = None,
) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise HTTPException(status_code=503, detail="Email delivery is not configured. Please contact support.")

    safe_title = escape(title)
    highlight = ""
    if highlight_label and highlight_value:
        highlight = f"""
        <div style="margin:26px 0 22px;padding:20px;border:1px solid #eadfd8;border-radius:12px;background:#fbf7f4;text-align:center">
          <div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#7b6d67">{escape(highlight_label)}</div>
          <div style="margin-top:8px;font-family:Arial,sans-serif;font-size:28px;font-weight:700;letter-spacing:5px;color:#6f1d2c;word-break:break-word">{escape(highlight_value)}</div>
        </div>"""
    action = ""
    if action_label and action_url:
        action = f'<p style="margin:26px 0;text-align:center"><a href="{escape(action_url, quote=True)}" style="display:inline-block;padding:13px 24px;border-radius:8px;background:#6f1d2c;color:#ffffff;text-decoration:none;font-weight:700">{escape(action_label)}</a></p>'

    html_body = f"""<!doctype html>
<html><body style="margin:0;padding:32px 12px;background:#f5f1ec;font-family:Arial,Helvetica,sans-serif;color:#302a27">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(detail)}</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #eee7e2">
    <tr><td style="padding:24px 32px;background:#6f1d2c;color:#ffffff"><div style="font-family:Georgia,serif;font-size:26px;font-weight:700;letter-spacing:.3px">DineBook</div><div style="margin-top:5px;font-size:12px;color:#f1dfe2">A seat at something special</div></td></tr>
    <tr><td style="padding:34px 32px 26px"><div style="margin-bottom:10px;font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#9b756e">DineBook account</div><h1 style="margin:0 0 16px;font-family:Georgia,serif;font-size:28px;line-height:1.25;color:#302a27">{safe_title}</h1><p style="margin:0;font-size:16px;line-height:1.7;color:#554b46">{escape(intro)}</p>{highlight}<p style="margin:0;font-size:14px;line-height:1.75;color:#6f625c">{escape(detail)}</p>{action}<p style="margin:26px 0 0;font-size:14px;line-height:1.7;color:#554b46">Warmly,<br><strong>The DineBook team</strong></p></td></tr>
    <tr><td style="padding:18px 32px;background:#fbf9f7;border-top:1px solid #eee7e2;font-size:12px;line-height:1.6;color:#8a7d76">This is an automated message from DineBook. If you did not expect this email, you can safely ignore it.</td></tr>
  </table>
</body></html>"""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email
    message["To"] = to
    message.set_content(plain_text)
    message.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(status_code=503, detail=error_detail) from exc
