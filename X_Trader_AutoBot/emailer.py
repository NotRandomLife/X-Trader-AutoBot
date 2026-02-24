import smtplib
from email.message import EmailMessage


class EmailSender:
    def __init__(self, host, port, secure, user, password, mail_to):
        self.host = host
        self.port = int(port)
        self.secure = bool(secure)
        self.user = user
        self.password = password
        self.mail_to = mail_to

    @staticmethod
    def from_settings(s: dict):
        provider = (s.get("email_provider") or "custom").lower()
        secure = bool(s.get("smtp_secure", True))

        host = (s.get("smtp_host") or "").strip()
        port = int(s.get("smtp_port") or 0)

        if provider == "gmail":
            host = host or "smtp.gmail.com"
            port = port or 465
            secure = True
        elif provider == "outlook":
            host = host or "smtp.office365.com"
            port = port or 587
            secure = False
        elif provider == "yahoo":
            host = host or "smtp.mail.yahoo.com"
            port = port or 465
            secure = True

        return EmailSender(
            host=host,
            port=port,
            secure=secure,
            user=(s.get("smtp_user") or "").strip(),
            password=(s.get("smtp_pass") or "").strip(),
            mail_to=(s.get("mail_to") or "").strip(),
        )

    def send(self, subject: str, body: str):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.user
        msg["To"] = self.mail_to
        msg.set_content(body)

        if self.secure:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=20) as smtp:
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
